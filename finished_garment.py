# SPDX-License-Identifier: GPL-3.0-or-later
"""Create a portable, rig-independent finished garment snapshot."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

import bpy
import numpy as np

from .kitsuke import KitsukeError, completed_kitsuke_handoff


_OUTPUT_COLLECTION_ROLE = "finished_garment_output"
_OUTPUT_OBJECT_ROLE = "finished_garment"
_PATTERN_ATTRIBUTE = "yohsai_pattern_position"
_PATTERN_UV = "Yohsai Pattern"
_AREA_EPSILON_M2 = 1.0e-12


class FinishedGarmentError(RuntimeError):
    """The current Yohsai state cannot produce a safe finished mesh."""


@dataclass(frozen=True)
class FinishedGarmentResult:
    collection: bpy.types.Collection
    object: bpy.types.Object
    vertex_count: int
    face_count: int
    removed_collapsed_face_count: int
    seam_count: int
    maximum_input_seam_distance_m: float
    maximum_weld_displacement_m: float
    warnings: tuple[str, ...]


class _DisjointSet:
    def __init__(self, size: int):
        self.parent = list(range(size))
        self.rank = [0] * size

    def find(self, value: int) -> int:
        parent = self.parent[value]
        if parent != value:
            self.parent[value] = self.find(parent)
        return self.parent[value]

    def union(self, first: int, second: int) -> None:
        a = self.find(first)
        b = self.find(second)
        if a == b:
            return
        if self.rank[a] < self.rank[b]:
            a, b = b, a
        self.parent[b] = a
        if self.rank[a] == self.rank[b]:
            self.rank[a] += 1


def _world_vertices(obj: bpy.types.Object) -> np.ndarray:
    local = np.empty((len(obj.data.vertices), 3), dtype=np.float64)
    obj.data.vertices.foreach_get("co", local.ravel())
    matrix = np.asarray([tuple(row) for row in obj.matrix_world], dtype=np.float64)
    return np.ascontiguousarray(local @ matrix[:3, :3].T + matrix[:3, 3])


def _pattern_positions(obj: bpy.types.Object) -> list[tuple[float, float]]:
    attribute = obj.data.attributes.get(_PATTERN_ATTRIBUTE)
    if (
        attribute is None
        or attribute.domain != "POINT"
        or attribute.data_type != "FLOAT_VECTOR"
        or len(attribute.data) != len(obj.data.vertices)
    ):
        raise FinishedGarmentError(
            f"{obj.name} has no valid Yohsai pattern coordinates; load the pattern again."
        )
    values = [(float(item.vector[0]), float(item.vector[1])) for item in attribute.data]
    if not all(isfinite(u) and isfinite(v) for u, v in values):
        raise FinishedGarmentError(f"{obj.name} contains a non-finite pattern coordinate.")
    return values


def _uv_value(layer, loop_index: int) -> tuple[float, float]:
    modern = getattr(layer, "uv", None)
    value = modern[loop_index].vector if modern is not None else layer.data[loop_index].uv
    return float(value[0]), float(value[1])


def _set_uv(layer, loop_index: int, value: tuple[float, float]) -> None:
    modern = getattr(layer, "uv", None)
    if modern is not None:
        modern[loop_index].vector = value
    else:
        layer.data[loop_index].uv = value


def _normal_matrix(obj: bpy.types.Object):
    linear = obj.matrix_world.to_3x3()
    if abs(float(linear.determinant())) <= 1.0e-12:
        raise FinishedGarmentError(f"{obj.name} has a non-invertible Object transform.")
    return linear.inverted().transposed()


def _corner_normals(obj: bpy.types.Object) -> list[tuple[float, float, float]]:
    mesh = obj.data
    values = getattr(mesh, "corner_normals", None)
    if values is None or len(values) != len(mesh.loops):
        return []
    transform = _normal_matrix(obj)
    result: list[tuple[float, float, float]] = []
    for item in values:
        normal = transform @ item.vector
        if normal.length_squared <= 1.0e-20:
            return []
        normal.normalize()
        result.append(tuple(normal))
    return result


def _color_value(attribute, index: int) -> tuple[float, float, float, float]:
    item = attribute.data[index]
    value = getattr(item, "color", None)
    if value is None:
        value = getattr(item, "color_srgb", (0.0, 0.0, 0.0, 1.0))
    return tuple(float(component) for component in value)


def _new_output_names(source_name: str) -> tuple[str, str]:
    serial = 1
    while True:
        suffix = f"{serial:03d}"
        collection_name = f"{source_name}_FINISHED_{suffix}"
        object_name = f"{source_name}_FINISHED_GARMENT_{suffix}"
        if (
            bpy.data.collections.get(collection_name) is None
            and bpy.data.objects.get(object_name) is None
        ):
            return collection_name, object_name
        serial += 1


def _weld_vertices(
    positions: np.ndarray, seams: np.ndarray
) -> tuple[np.ndarray, np.ndarray, float, float]:
    vertex_count = len(positions)
    if seams.ndim != 2 or seams.shape[1:] != (2,):
        raise FinishedGarmentError("The completed sewing pairs are invalid.")
    if (
        np.any(seams < 0)
        or np.any(seams >= vertex_count)
        or np.any(seams[:, 0] == seams[:, 1])
    ):
        raise FinishedGarmentError("The completed sewing pairs no longer match the current mesh.")

    disjoint = _DisjointSet(vertex_count)
    for first, second in seams:
        disjoint.union(int(first), int(second))

    members: dict[int, list[int]] = {}
    for vertex in range(vertex_count):
        members.setdefault(disjoint.find(vertex), []).append(vertex)
    groups = sorted(members.values(), key=lambda values: values[0])

    old_to_new = np.empty(vertex_count, dtype=np.int32)
    welded = np.empty((len(groups), 3), dtype=np.float64)
    maximum_displacement = 0.0
    for new_index, group in enumerate(groups):
        point = positions[group].mean(axis=0)
        welded[new_index] = point
        old_to_new[group] = new_index
        maximum_displacement = max(
            maximum_displacement,
            float(np.max(np.linalg.norm(positions[group] - point, axis=1))),
        )

    distances = np.linalg.norm(positions[seams[:, 0]] - positions[seams[:, 1]], axis=1)
    maximum_distance = float(np.max(distances)) if distances.size else 0.0
    return welded, old_to_new, maximum_distance, maximum_displacement


def _face_area(points: np.ndarray) -> float:
    normal = np.zeros(3, dtype=np.float64)
    for index, point in enumerate(points):
        following = points[(index + 1) % len(points)]
        normal += np.cross(point, following)
    return 0.5 * float(np.linalg.norm(normal))


def _orient_and_validate_faces(
    faces: list[list[int]], positions: np.ndarray
) -> list[bool]:
    edge_uses: dict[tuple[int, int], list[tuple[int, bool]]] = {}
    face_keys: set[tuple[int, ...]] = set()
    for face_index, face in enumerate(faces):
        if len(face) < 3 or len(set(face)) != len(face):
            raise FinishedGarmentError("Welding would create a degenerate face.")
        if _face_area(positions[face]) <= _AREA_EPSILON_M2:
            raise FinishedGarmentError("Welding would create a zero-area face.")
        key = tuple(sorted(face))
        if key in face_keys:
            raise FinishedGarmentError("Welding would create a duplicate face.")
        face_keys.add(key)
        for index, first in enumerate(face):
            second = face[(index + 1) % len(face)]
            edge = (min(first, second), max(first, second))
            edge_uses.setdefault(edge, []).append((face_index, first < second))

    adjacency: list[list[tuple[int, bool]]] = [[] for _ in faces]
    for uses in edge_uses.values():
        if len(uses) > 2:
            raise FinishedGarmentError("Welding would create non-manifold garment topology.")
        if len(uses) == 2:
            (first_face, first_direction), (second_face, second_direction) = uses
            relation = first_direction == second_direction
            adjacency[first_face].append((second_face, relation))
            adjacency[second_face].append((first_face, relation))

    flips: list[bool | None] = [None] * len(faces)
    for start in range(len(faces)):
        if flips[start] is not None:
            continue
        flips[start] = False
        stack = [start]
        while stack:
            face = stack.pop()
            for neighbor, relation in adjacency[face]:
                expected = bool(flips[face]) ^ relation
                if flips[neighbor] is None:
                    flips[neighbor] = expected
                    stack.append(neighbor)
                elif bool(flips[neighbor]) != expected:
                    raise FinishedGarmentError("The welded garment has inconsistent face orientation.")

    # Reject bow-tie vertices whose incident faces form more than one fan.
    incident: list[list[int]] = [[] for _ in range(len(positions))]
    for face_index, face in enumerate(faces):
        for vertex in face:
            incident[vertex].append(face_index)
    for vertex, vertex_faces in enumerate(incident):
        if len(vertex_faces) < 2:
            continue
        allowed = set(vertex_faces)
        reached = {vertex_faces[0]}
        stack = [vertex_faces[0]]
        while stack:
            face = stack.pop()
            for neighbor, _relation in adjacency[face]:
                if neighbor in allowed and neighbor not in reached:
                    reached.add(neighbor)
                    stack.append(neighbor)
        if reached != allowed:
            raise FinishedGarmentError(
                f"Welding would create a non-manifold vertex at output vertex {vertex}."
            )
    return [bool(value) for value in flips]


def _remove_output(collection, obj, mesh) -> None:
    if obj is not None and obj.name in bpy.data.objects:
        bpy.data.objects.remove(obj, do_unlink=True)
    if mesh is not None and mesh.name in bpy.data.meshes and mesh.users == 0:
        bpy.data.meshes.remove(mesh)
    if collection is not None and collection.name in bpy.data.collections:
        bpy.data.collections.remove(collection)


def create_finished_garment(
    context, collection: bpy.types.Collection | None
) -> FinishedGarmentResult:
    """Create a new welded snapshot without modifying Yohsai or earlier outputs."""
    if collection is None or collection.get("yohsai_role") != "clothes":
        raise FinishedGarmentError("Select a loaded Yohsai Clothes collection first.")
    try:
        parts, seams = completed_kitsuke_handoff(collection)
    except KitsukeError as exc:
        raise FinishedGarmentError(str(exc)) from exc

    all_parts = sorted(
        (
            obj
            for obj in collection.objects
            if obj.type == "MESH" and obj.get("yohsai_role") == "part"
        ),
        key=lambda obj: int(obj.get("yohsai_panel_index", 0)),
    )
    if [part.name for part in parts] != [part.name for part in all_parts]:
        raise FinishedGarmentError(
            "Not every clothes part is included in the completed garment; finish GRAVITY first."
        )

    context.view_layer.update()
    position_blocks = [_world_vertices(part) for part in parts]
    positions = np.concatenate(position_blocks)
    if not np.all(np.isfinite(positions)):
        raise FinishedGarmentError("The garment contains a non-finite vertex position.")
    welded, old_to_new, maximum_distance, maximum_displacement = _weld_vertices(
        positions, seams
    )

    warnings: list[str] = []
    if any(part.modifiers for part in parts):
        warnings.append("source modifiers were not included")

    uv_names = sorted(
        {
            layer.name
            for part in parts
            for layer in part.data.uv_layers
            if layer.name != _PATTERN_UV
        }
    )
    color_names = sorted(
        {attribute.name for part in parts for attribute in part.data.color_attributes}
    )
    missing_uvs = {
        name for name in uv_names if any(part.data.uv_layers.get(name) is None for part in parts)
    }
    missing_colors = {
        name
        for name in color_names
        if any(part.data.color_attributes.get(name) is None for part in parts)
    }
    if missing_uvs:
        warnings.append(
            "missing UV maps were filled with (0, 0): " + ", ".join(sorted(missing_uvs))
        )
    if missing_colors:
        warnings.append(
            "missing color attributes were filled with black: "
            + ", ".join(sorted(missing_colors))
        )

    faces: list[list[int]] = []
    face_panel_indices: list[int] = []
    face_part_indices: list[int] = []
    face_material_indices: list[int] = []
    face_smooth: list[bool] = []
    face_uvs: list[dict[str, list[tuple[float, float]]]] = []
    face_colors: list[dict[str, list[tuple[float, float, float, float]]]] = []
    face_normals: list[list[tuple[float, float, float]]] = []
    edge_sharp: dict[tuple[int, int], bool] = {}
    materials: list[bpy.types.Material | None] = []
    any_custom_normals = any(
        bool(getattr(part.data, "has_custom_normals", False)) for part in parts
    )
    active_uv_names = {
        part.data.uv_layers.active.name
        for part in parts
        if part.data.uv_layers.active is not None
    }

    offset = 0
    for part_index, (part, block) in enumerate(zip(parts, position_blocks)):
        mesh = part.data
        pattern = _pattern_positions(part)
        determinant = float(part.matrix_world.to_3x3().determinant())
        if abs(determinant) <= 1.0e-12:
            raise FinishedGarmentError(f"{part.name} has a non-invertible Object transform.")
        reverse_transform = determinant < 0.0
        transformed_normals = _corner_normals(part) if any_custom_normals else []
        if any_custom_normals and len(transformed_normals) != len(mesh.loops):
            warnings.append(f"{part.name} custom normals could not be copied")
            transformed_normals = []

        material_offset = len(materials)
        if len(part.material_slots):
            # Object-linked slots must be baked to their effective material too;
            # reading mesh.materials alone would silently lose those assignments.
            materials.extend(slot.material for slot in part.material_slots)
        else:
            materials.append(None)

        for edge in mesh.edges:
            first = int(old_to_new[offset + int(edge.vertices[0])])
            second = int(old_to_new[offset + int(edge.vertices[1])])
            if first == second:
                continue
            key = (min(first, second), max(first, second))
            edge_sharp[key] = edge_sharp.get(key, False) or bool(
                getattr(edge, "use_edge_sharp", False)
            )

        for polygon in mesh.polygons:
            vertices = [int(value) for value in polygon.vertices]
            loops = [int(value) for value in polygon.loop_indices]
            if reverse_transform:
                vertices.reverse()
                loops.reverse()
            faces.append([int(old_to_new[offset + vertex]) for vertex in vertices])
            face_panel_indices.append(int(part.get("yohsai_panel_index", part_index)))
            face_part_indices.append(part_index)
            face_material_indices.append(material_offset + int(polygon.material_index))
            face_smooth.append(bool(polygon.use_smooth))

            uv_values: dict[str, list[tuple[float, float]]] = {
                _PATTERN_UV: [pattern[vertex] for vertex in vertices]
            }
            for name in uv_names:
                layer = mesh.uv_layers.get(name)
                uv_values[name] = (
                    [_uv_value(layer, loop) for loop in loops]
                    if layer is not None
                    else [(0.0, 0.0)] * len(loops)
                )
            face_uvs.append(uv_values)

            color_values: dict[str, list[tuple[float, float, float, float]]] = {}
            for name in color_names:
                attribute = mesh.color_attributes.get(name)
                if attribute is None:
                    color_values[name] = [(0.0, 0.0, 0.0, 1.0)] * len(loops)
                elif attribute.domain == "POINT":
                    color_values[name] = [_color_value(attribute, vertex) for vertex in vertices]
                elif attribute.domain == "CORNER":
                    color_values[name] = [_color_value(attribute, loop) for loop in loops]
                else:
                    color_values[name] = [
                        _color_value(attribute, int(polygon.index))
                    ] * len(loops)
            face_colors.append(color_values)

            normals = (
                [transformed_normals[loop] for loop in loops]
                if transformed_normals
                else []
            )
            face_normals.append(normals)
        offset += len(block)

    # A many-part seam junction can legitimately join two adjacent boundary
    # vertices of one source triangle.  That triangle then has no surface area
    # in the welded topology and must disappear.  Keep every parallel
    # face-corner/face-domain payload aligned while dropping only those faces;
    # all other zero-area and non-manifold outcomes remain hard failures below.
    kept_faces = [
        face_index
        for face_index, face in enumerate(faces)
        if len(set(face)) == len(face)
    ]
    removed_collapsed_face_count = len(faces) - len(kept_faces)
    if removed_collapsed_face_count:
        faces = [faces[index] for index in kept_faces]
        face_panel_indices = [face_panel_indices[index] for index in kept_faces]
        face_part_indices = [face_part_indices[index] for index in kept_faces]
        face_material_indices = [face_material_indices[index] for index in kept_faces]
        face_smooth = [face_smooth[index] for index in kept_faces]
        face_uvs = [face_uvs[index] for index in kept_faces]
        face_colors = [face_colors[index] for index in kept_faces]
        face_normals = [face_normals[index] for index in kept_faces]
        warnings.append(
            f"{removed_collapsed_face_count} face(s) collapsed at seam junctions and were removed"
        )
    if not faces:
        raise FinishedGarmentError("Welding would remove every garment face.")

    flips = _orient_and_validate_faces(faces, welded)
    for face_index, flip in enumerate(flips):
        if not flip:
            continue
        faces[face_index].reverse()
        for values in face_uvs[face_index].values():
            values.reverse()
        for values in face_colors[face_index].values():
            values.reverse()
        if face_normals[face_index]:
            face_normals[face_index] = [
                tuple(-component for component in normal)
                for normal in reversed(face_normals[face_index])
            ]

    collection_name, object_name = _new_output_names(collection.name)
    output_collection = None
    output_mesh = None
    output_object = None
    try:
        output_collection = bpy.data.collections.new(collection_name)
        context.scene.collection.children.link(output_collection)
        output_mesh = bpy.data.meshes.new(f"{object_name}_MESH")
        output_object = bpy.data.objects.new(object_name, output_mesh)
        output_collection.objects.link(output_object)
        output_mesh.from_pydata([tuple(point) for point in welded], [], faces)
        output_mesh.update(calc_edges=True, calc_edges_loose=True)
        if len(output_mesh.vertices) != len(welded) or len(output_mesh.polygons) != len(faces):
            raise FinishedGarmentError("The finished topology changed while creating the mesh.")

        for material in materials:
            output_mesh.materials.append(material)
        for polygon, material_index, smooth in zip(
            output_mesh.polygons, face_material_indices, face_smooth
        ):
            polygon.material_index = material_index
            polygon.use_smooth = smooth

        panel_attribute = output_mesh.attributes.new(
            name="panel_index", type="INT", domain="FACE"
        )
        source_attribute = output_mesh.attributes.new(
            name="yohsai_source_part", type="INT", domain="FACE"
        )
        for item, value in zip(panel_attribute.data, face_panel_indices):
            item.value = value
        for item, value in zip(source_attribute.data, face_part_indices):
            item.value = value

        output_edges = {
            tuple(sorted((int(edge.vertices[0]), int(edge.vertices[1])))): edge
            for edge in output_mesh.edges
        }
        for key, sharp in edge_sharp.items():
            edge = output_edges.get(key)
            if edge is not None and hasattr(edge, "use_edge_sharp"):
                edge.use_edge_sharp = sharp

        for name in [_PATTERN_UV, *uv_names]:
            layer = output_mesh.uv_layers.new(name=name, do_init=False)
            loop_index = 0
            for values in face_uvs:
                for value in values[name]:
                    _set_uv(layer, loop_index, value)
                    loop_index += 1
        active_uv = next(iter(active_uv_names)) if len(active_uv_names) == 1 else _PATTERN_UV
        active_layer = output_mesh.uv_layers.get(active_uv) or output_mesh.uv_layers.get(
            _PATTERN_UV
        )
        if active_layer is not None:
            output_mesh.uv_layers.active = active_layer
            output_mesh.uv_layers.active_render = active_layer

        for name in color_names:
            source_types = {
                attribute.data_type
                for part in parts
                if (attribute := part.data.color_attributes.get(name)) is not None
            }
            data_type = "FLOAT_COLOR" if "FLOAT_COLOR" in source_types else "BYTE_COLOR"
            attribute = output_mesh.color_attributes.new(
                name=name, type=data_type, domain="CORNER"
            )
            loop_index = 0
            for values in face_colors:
                for value in values[name]:
                    attribute.data[loop_index].color = value
                    loop_index += 1

        if any_custom_normals and all(face_normals):
            custom_normals = [normal for values in face_normals for normal in values]
            try:
                output_mesh.normals_split_custom_set(custom_normals)
            except (AttributeError, RuntimeError, TypeError, ValueError):
                warnings.append("custom normals could not be installed and were recalculated")
        output_mesh.update()

        output_collection["yohsai_role"] = _OUTPUT_COLLECTION_ROLE
        output_collection["yohsai_source_collection"] = collection.name
        output_object["yohsai_schema"] = "yohsai-finished-garment/1.0.0"
        output_object["yohsai_role"] = _OUTPUT_OBJECT_ROLE
        output_object["yohsai_source_collection"] = collection.name
        output_object["yohsai_source_parts"] = [part.name for part in parts]
        output_object["yohsai_source_revision"] = int(
            collection.get("yohsai_kitsuke_revision", 0)
        )

        for selected in context.selected_objects:
            selected.select_set(False)
        output_object.select_set(True)
        context.view_layer.objects.active = output_object
        context.view_layer.update()
        return FinishedGarmentResult(
            collection=output_collection,
            object=output_object,
            vertex_count=len(output_mesh.vertices),
            face_count=len(output_mesh.polygons),
            removed_collapsed_face_count=removed_collapsed_face_count,
            seam_count=len(seams),
            maximum_input_seam_distance_m=maximum_distance,
            maximum_weld_displacement_m=maximum_displacement,
            warnings=tuple(dict.fromkeys(warnings)),
        )
    except Exception:
        _remove_output(output_collection, output_object, output_mesh)
        raise
