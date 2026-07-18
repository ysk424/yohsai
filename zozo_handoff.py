# SPDX-License-Identifier: GPL-3.0-or-later
"""Create a non-destructive Yohsai hand-off for ZOZO Contact Solver."""

from __future__ import annotations

from dataclasses import dataclass
import re

import bmesh
import bpy
import numpy as np
from mathutils import Vector
from mathutils.bvhtree import BVHTree

from .kitsuke import KitsukeError, completed_kitsuke_handoff


ZOZO_MCP_PORT = 9633
ZOZO_CONTACT_GAP_M = 0.001
# ZOZO keeps a loose stitch edge open by 1.1 * (gap_a + gap_b) + 1e-5.
ZOZO_STITCH_OPENING_M = 1.1 * (2.0 * ZOZO_CONTACT_GAP_M) + 1.0e-5
MAX_HANDOFF_SEAM_DISTANCE_M = 0.01
# Clearance the self-intersection resolver leaves between cloth layers so ppf's
# exact edge/coplanar test passes; kept below the ZOZO stitch opening.
_SELF_INTERSECTION_CLEARANCE_M = 0.001
_BODY_CLEARANCE_M = 1.1 * ZOZO_CONTACT_GAP_M
_HANDOFF_COLLECTION_ROLE = "zozo_handoff"
_HANDOFF_CLOTH_ROLE = "zozo_cloth"
_HANDOFF_BODY_ROLE = "zozo_body"


class ZozoHandoffError(RuntimeError):
    """The current Yohsai state cannot safely be handed to ZOZO."""


@dataclass(frozen=True)
class ZozoPreparation:
    collection: bpy.types.Collection
    cloth_object: bpy.types.Object
    body_object: bpy.types.Object
    seam_count: int
    maximum_input_seam_distance_m: float
    minimum_output_seam_distance_m: float
    cloth_group_name: str
    body_group_name: str
    project_name: str

    def mcp_configuration(self, scene: bpy.types.Scene) -> dict:
        fps = max(1, int(round(float(scene.render.fps) / float(scene.render.fps_base))))
        return {
            "port": ZOZO_MCP_PORT,
            "cloth_object": self.cloth_object.name,
            "body_object": self.body_object.name,
            "cloth_group": self.cloth_group_name,
            "body_group": self.body_group_name,
            "scene_parameters": {
                "step_size": 0.005,
                "frame_count": max(1, int(scene.frame_end) - int(scene.frame_start) + 1),
                "frame_rate": fps,
                "gravity": [0.0, 0.0, -9.81],
                "inactive_momentum_frames": 5,
                "project_name": self.project_name,
            },
            "cloth_properties": {
                "contact_gap": ZOZO_CONTACT_GAP_M,
                "contact_offset": 0.0,
                "deformation_damping": 0.005,
                "bending_damping": 0.002,
                "bend_rest_angle_source": "FROM_GEOMETRY",
            },
            "body_properties": {
                "contact_gap": ZOZO_CONTACT_GAP_M,
                "contact_offset": 0.0,
            },
            "capture_timeout_seconds": 300.0,
        }


def _world_vertices(obj: bpy.types.Object) -> np.ndarray:
    local = np.empty((len(obj.data.vertices), 3), dtype=np.float64)
    obj.data.vertices.foreach_get("co", local.ravel())
    matrix = np.asarray([tuple(row) for row in obj.matrix_world], dtype=np.float64)
    return np.ascontiguousarray(local @ matrix[:3, :3].T + matrix[:3, 3])


def _evaluated_body_bvh(context, body: bpy.types.Object) -> tuple[BVHTree, np.ndarray]:
    depsgraph = context.evaluated_depsgraph_get()
    evaluated = body.evaluated_get(depsgraph)
    mesh = evaluated.to_mesh()
    try:
        if not mesh.vertices or not mesh.polygons:
            raise ZozoHandoffError("The selected Body needs a surface mesh for ZOZO contact.")
        matrix = evaluated.matrix_world
        vertices = [matrix @ vertex.co for vertex in mesh.vertices]
        faces = [tuple(polygon.vertices) for polygon in mesh.polygons]
        bvh = BVHTree.FromPolygons(vertices, faces, all_triangles=False)
        if bvh is None:
            raise ZozoHandoffError("The selected Body could not be converted to a contact surface.")
        centroid = np.mean(np.asarray([tuple(point) for point in vertices], dtype=np.float64), axis=0)
        return bvh, centroid
    finally:
        evaluated.to_mesh_clear()


def _component_normal(
    bvh: BVHTree,
    body_centroid: np.ndarray,
    positions: np.ndarray,
    component: list[int],
) -> tuple[np.ndarray, float]:
    center = np.mean(positions[component], axis=0)
    nearest = bvh.find_nearest(Vector(center))
    if nearest is not None:
        location, surface_normal, _face_index, _distance = nearest
        normal = np.asarray(tuple(surface_normal), dtype=np.float64)
        normal_length = float(np.linalg.norm(normal))
        if normal_length > 1.0e-10:
            normal /= normal_length
            toward_cloth = center - np.asarray(tuple(location), dtype=np.float64)
            if float(np.dot(toward_cloth, normal)) < 0.0:
                normal *= -1.0
        else:
            normal = center - body_centroid
    else:
        normal = center - body_centroid
    length = float(np.linalg.norm(normal))
    if length <= 1.0e-10:
        normal = np.asarray((0.0, 0.0, 1.0), dtype=np.float64)
    else:
        normal /= length

    minimum_distance = float("inf")
    for vertex_index in component:
        hit = bvh.find_nearest(Vector(positions[vertex_index]))
        if hit is not None:
            minimum_distance = min(minimum_distance, float(hit[3]))
    return normal, minimum_distance


def _seam_components(seams: np.ndarray) -> tuple[dict[int, set[int]], list[list[int]]]:
    adjacency: dict[int, set[int]] = {}
    for a_value, b_value in seams:
        a, b = int(a_value), int(b_value)
        adjacency.setdefault(a, set()).add(b)
        adjacency.setdefault(b, set()).add(a)
    components: list[list[int]] = []
    remaining = set(adjacency)
    while remaining:
        start = min(remaining)
        stack = [start]
        component: list[int] = []
        remaining.remove(start)
        while stack:
            vertex = stack.pop()
            component.append(vertex)
            for neighbor in sorted(adjacency[vertex], reverse=True):
                if neighbor in remaining:
                    remaining.remove(neighbor)
                    stack.append(neighbor)
        components.append(sorted(component))
    return adjacency, components


def _greedy_colors(adjacency: dict[int, set[int]], component: list[int]) -> dict[int, int]:
    """Color one local stitch graph so every loose edge gets a positive gap."""
    colors: dict[int, int] = {}
    # High-degree vertices first keeps unequal seam samplings at two layers in
    # the common case, while still handling an occasional three-way junction.
    for vertex in sorted(component, key=lambda item: (-len(adjacency[item]), item)):
        used = {colors[neighbor] for neighbor in adjacency[vertex] if neighbor in colors}
        color = 0
        while color in used:
            color += 1
        colors[vertex] = color
    return colors


def _open_stitches(
    positions: np.ndarray,
    seams: np.ndarray,
    bvh: BVHTree,
    body_centroid: np.ndarray,
) -> tuple[np.ndarray, float, float]:
    before = np.linalg.norm(positions[seams[:, 0]] - positions[seams[:, 1]], axis=1)
    maximum_before = float(np.max(before)) if before.size else 0.0

    # Matched 1:1 gather sewing closes every seam except a few isolated pinch
    # points (shoulder / underarm) where the body sits between the two sides and
    # holds them apart.  Those residual pairs are excluded from the ZOZO stitch
    # opening below and welded shut afterwards, so they play no part in the layer
    # spacing of their neighbours.
    residual = np.nonzero(before > MAX_HANDOFF_SEAM_DISTANCE_M)[0]
    open_seams = np.delete(seams, residual, axis=0)

    result = positions.copy()
    adjacency, components = _seam_components(open_seams)
    for component in components:
        normal, minimum_body_distance = _component_normal(
            bvh, body_centroid, result, component
        )
        colors = _greedy_colors(adjacency, component)
        component_set = set(component)
        projections = [
            abs(float(np.dot(result[int(b)] - result[int(a)], normal)))
            for a, b in open_seams
            if int(a) in component_set
        ]
        layer_spacing = ZOZO_STITCH_OPENING_M + max(projections, default=0.0)
        clearance = (
            max(0.0, _BODY_CLEARANCE_M - minimum_body_distance)
            if np.isfinite(minimum_body_distance)
            else 0.0
        )
        for vertex in component:
            result[vertex] += normal * (clearance + colors[vertex] * layer_spacing)

    # Fill each residual pinch hole by welding its pair onto whichever endpoint
    # sits farther outside the body, so the adjacent panel triangles stretch
    # across the gap without bending the panels or being driven into the body.
    # Like a real 2 mm stitch line this is not perfect, which is fine for a
    # garment.
    for index in residual:
        first, second = int(seams[index, 0]), int(seams[index, 1])
        _, _, _, distance_first = bvh.find_nearest(Vector(result[first]))
        _, _, _, distance_second = bvh.find_nearest(Vector(result[second]))
        anchor = first if (distance_first or 0.0) >= (distance_second or 0.0) else second
        result[first] = result[anchor]
        result[second] = result[anchor]

    if open_seams.size:
        after = np.linalg.norm(result[open_seams[:, 0]] - result[open_seams[:, 1]], axis=1)
        minimum_after = float(np.min(after))
        if minimum_after + 1.0e-8 < ZOZO_STITCH_OPENING_M:
            raise ZozoHandoffError(
                "Could not create a positive ZOZO contact gap at every sewing edge."
            )
    else:
        minimum_after = ZOZO_STITCH_OPENING_M
    return result, maximum_before, minimum_after


def _resolve_self_intersections(
    obj: bpy.types.Object, body_bvh: BVHTree, max_passes: int = 40
) -> int:
    """Unfold the drape's self-intersecting regions so ppf/ZOZO accepts the mesh.

    ppf rejects any shell whose edge pierces another triangle at rest, and gather
    sewing leaves the excess fabric folded onto itself at the shoulders, bust and
    sides.  Pushing folds apart cascades and never converges, so instead each
    intersecting cluster and its two-ring neighbourhood is strongly smoothed,
    which unfolds the crumple.  The folds sit outside the body, so flattening
    them does not drive cloth inward; any vertex still left inside is clamped
    back out as a fallback.  ppf then re-sews and re-drapes the smoothed region
    with contact.  Returns the number of intersections still present.
    """
    mesh = obj.data
    world = obj.matrix_world
    inverse = world.inverted_safe()
    bm = bmesh.new()
    bm.from_mesh(mesh)
    bmesh.ops.triangulate(bm, faces=bm.faces)  # keeps vertex indexing
    bm.verts.ensure_lookup_table()
    bm.faces.ensure_lookup_table()
    tris = [[v.index for v in f.verts] for f in bm.faces]
    face_verts = [set(t) for t in tris]
    coords = np.array([list(world @ v.co) for v in bm.verts], dtype=np.float64)
    adjacency: list[list[int]] = [[] for _ in range(len(bm.verts))]
    seen = [set() for _ in range(len(bm.verts))]
    edges: list[tuple[int, int]] = []
    for edge in bm.edges:
        a, b = edge.verts[0].index, edge.verts[1].index
        edges.append((a, b))
        if b not in seen[a]:
            adjacency[a].append(b); seen[a].add(b)
        if a not in seen[b]:
            adjacency[b].append(a); seen[b].add(a)

    def involved_vertices(current: np.ndarray) -> set[int]:
        # ppf detects real intersections (edge-vs-triangle plus a coplanar
        # fallback) with exact predicates, which a float ray test cannot mirror
        # exactly.  A strict tree and a 1 mm-inflated tree do not even nest, so
        # take the union of both: the strict tree catches real piercings, the
        # inflated tree catches near-touches and coplanar overlaps, and the
        # resolver smooths until neither reports a hit -- leaving ppf's exact
        # test a clearance below the >=2.2 mm ZOZO stitch openings, so the
        # intentional loose stitches are never flagged.
        points = [Vector(p) for p in current]
        strict = BVHTree.FromPolygons(points, tris, all_triangles=True, epsilon=0.0)
        inflated = BVHTree.FromPolygons(
            points, tris, all_triangles=True, epsilon=_SELF_INTERSECTION_CLEARANCE_M
        )
        hits: set[int] = set()
        for a, b in inflated.overlap(inflated):
            if a < b and not (face_verts[a] & face_verts[b]):
                hits.update(tris[a]); hits.update(tris[b])
        for tree in (strict, inflated):
            for a, b in edges:
                start = Vector(current[a])
                direction = Vector(current[b]) - start
                length = direction.length
                if length < 1.0e-9:
                    continue
                direction = direction / length
                hit = tree.ray_cast(start + direction * 1.0e-8, direction, length - 2.0e-8)
                if hit and hit[0] is not None:
                    face = hit[2]
                    if face is not None and a not in tris[face] and b not in tris[face]:
                        hits.add(a); hits.add(b); hits.update(tris[face])
        return hits

    for _pass in range(max_passes):
        involved = involved_vertices(coords)
        if not involved:
            break
        for _ring in range(2):  # solve the neighbourhood around each hit, not just the hit
            grown = set(involved)
            for vertex in involved:
                grown.update(adjacency[vertex])
            involved = grown
        for _sub in range(4):
            updates = {
                vertex: coords[adjacency[vertex]].mean(axis=0)
                for vertex in involved if adjacency[vertex]
            }
            for vertex, target in updates.items():
                coords[vertex] = 0.6 * target + 0.4 * coords[vertex]

    # Fallback: clamp any vertex left inside the body back onto its surface.
    for index in range(len(coords)):
        location, normal, _face, _distance = body_bvh.find_nearest(Vector(coords[index]))
        if location is not None and (Vector(coords[index]) - location).dot(normal) < 0.0:
            coords[index] = np.array(location + normal * _BODY_CLEARANCE_M)

    # Persist the resolved positions AND the triangulation: ppf triangulates any
    # remaining quads with its own diagonal, which can re-introduce an
    # intersection the resolver already cleared on its triangulation.  Writing
    # the triangulated mesh back hands ppf exactly the triangles that were
    # verified intersection-free.
    remaining = len(involved_vertices(coords))
    bm.verts.ensure_lookup_table()
    for index in range(len(coords)):
        bm.verts[index].co = inverse @ Vector(coords[index])
    bm.to_mesh(mesh)
    bm.free()
    mesh.update()
    return remaining


def _pattern_positions(obj: bpy.types.Object) -> list[tuple[float, float]]:
    attribute = obj.data.attributes.get("yohsai_pattern_position")
    if (
        attribute is None
        or attribute.domain != "POINT"
        or attribute.data_type != "FLOAT_VECTOR"
        or len(attribute.data) != len(obj.data.vertices)
    ):
        raise ZozoHandoffError(
            f"{obj.name} has no valid Yohsai pattern coordinates; load the pattern again."
        )
    return [(float(item.vector[0]), float(item.vector[1])) for item in attribute.data]


def _set_uv(uv_layer, loop_index: int, value: tuple[float, float]) -> None:
    modern = getattr(uv_layer, "uv", None)
    if modern is not None:
        modern[loop_index].vector = value
    else:
        uv_layer.data[loop_index].uv = value


def _remove_object_and_owned_mesh(obj: bpy.types.Object) -> None:
    mesh = obj.data if obj.type == "MESH" else None
    bpy.data.objects.remove(obj, do_unlink=True)
    if mesh is not None and mesh.users == 0:
        bpy.data.meshes.remove(mesh)


def _handoff_collection(context, source: bpy.types.Collection) -> bpy.types.Collection:
    matches = [
        collection
        for collection in bpy.data.collections
        if collection.get("yohsai_role") == _HANDOFF_COLLECTION_ROLE
        and collection.get("yohsai_source_collection") == source.name
    ]
    handoff = matches[0] if matches else bpy.data.collections.new(f"{source.name}_ZOZO")
    if not matches:
        context.scene.collection.children.link(handoff)
    handoff["yohsai_role"] = _HANDOFF_COLLECTION_ROLE
    handoff["yohsai_source_collection"] = source.name
    for collection in matches:
        for obj in list(collection.objects):
            if (
                obj.get("yohsai_source_collection") == source.name
                and obj.get("yohsai_role") in {_HANDOFF_CLOTH_ROLE, _HANDOFF_BODY_ROLE}
            ):
                _remove_object_and_owned_mesh(obj)
    return handoff


def _create_cloth_object(
    handoff: bpy.types.Collection,
    source: bpy.types.Collection,
    parts: list[bpy.types.Object],
    positions: np.ndarray,
    seams: np.ndarray,
) -> bpy.types.Object:
    vertices = [tuple(point) for point in positions]
    edges: list[tuple[int, int]] = []
    faces: list[tuple[int, ...]] = []
    face_uvs: list[tuple[tuple[float, float], ...]] = []
    face_panel_indices: list[int] = []
    face_material_indices: list[int] = []
    vertex_part_indices: list[int] = []
    materials: list[bpy.types.Material] = []
    material_slots: dict[int, int] = {}
    offset = 0
    for part_index, obj in enumerate(parts):
        mesh = obj.data
        pattern = _pattern_positions(obj)
        edges.extend(
            (int(edge.vertices[0]) + offset, int(edge.vertices[1]) + offset)
            for edge in mesh.edges
        )
        panel_index = int(obj.get("yohsai_panel_index", part_index))
        local_materials: dict[int, int] = {}
        for index, material in enumerate(mesh.materials):
            if material is None:
                continue
            pointer = int(material.as_pointer())
            if pointer not in material_slots:
                material_slots[pointer] = len(materials)
                materials.append(material)
            local_materials[index] = material_slots[pointer]
        for polygon in mesh.polygons:
            polygon_vertices = tuple(int(vertex) + offset for vertex in polygon.vertices)
            faces.append(polygon_vertices)
            face_uvs.append(tuple(pattern[int(vertex)] for vertex in polygon.vertices))
            face_panel_indices.append(panel_index)
            face_material_indices.append(local_materials.get(int(polygon.material_index), 0))
        vertex_part_indices.extend([part_index] * len(mesh.vertices))
        offset += len(mesh.vertices)

    stitch_keys = {tuple(sorted((int(a), int(b)))) for a, b in seams}
    edges.extend((int(a), int(b)) for a, b in seams)
    name = f"{source.name}_ZOZO_CLOTH"
    mesh = bpy.data.meshes.new(name)
    cloth = bpy.data.objects.new(name, mesh)
    try:
        handoff.objects.link(cloth)
        mesh.from_pydata(vertices, edges, faces)
        mesh.update(calc_edges=True, calc_edges_loose=True)
        if len(mesh.vertices) != len(vertices) or len(mesh.polygons) != len(faces):
            raise ZozoHandoffError("The ZOZO hand-off topology changed while creating the mesh.")

        for material in materials:
            mesh.materials.append(material)
        for polygon, material_index in zip(mesh.polygons, face_material_indices):
            polygon.material_index = material_index

        panel_attribute = mesh.attributes.new(name="panel_index", type="INT", domain="FACE")
        for item, value in zip(panel_attribute.data, face_panel_indices):
            item.value = value
        part_attribute = mesh.attributes.new(name="yohsai_source_part", type="INT", domain="POINT")
        for item, value in zip(part_attribute.data, vertex_part_indices):
            item.value = value
        stitch_attribute = mesh.attributes.new(name="yohsai_zozo_stitch", type="BOOLEAN", domain="EDGE")
        found_stitches = 0
        for edge in mesh.edges:
            key = tuple(sorted((int(edge.vertices[0]), int(edge.vertices[1]))))
            if key in stitch_keys:
                stitch_attribute.data[edge.index].value = True
                found_stitches += 1
        if found_stitches != len(stitch_keys):
            raise ZozoHandoffError("A loose ZOZO stitch edge was lost while creating the mesh.")

        uv_layer = mesh.uv_layers.new(name="Yohsai Pattern", do_init=False)
        for polygon, uvs in zip(mesh.polygons, face_uvs):
            for loop_index, uv in zip(polygon.loop_indices, uvs):
                _set_uv(uv_layer, int(loop_index), uv)
        mesh.uv_layers.active = uv_layer
        mesh.uv_layers.active_render = uv_layer

        cloth["yohsai_schema"] = "yohsai-pattern/1.0.0"
        cloth["yohsai_role"] = _HANDOFF_CLOTH_ROLE
        cloth["yohsai_source_collection"] = source.name
        cloth["yohsai_source_parts"] = [part.name for part in parts]
        cloth["yohsai_zozo_contact_gap_m"] = ZOZO_CONTACT_GAP_M
        cloth["yohsai_zozo_stitch_opening_m"] = ZOZO_STITCH_OPENING_M
        return cloth
    except Exception:
        _remove_object_and_owned_mesh(cloth)
        raise


def _create_body_object(
    handoff: bpy.types.Collection,
    source: bpy.types.Collection,
    body: bpy.types.Object,
) -> bpy.types.Object:
    duplicate = body.copy()
    duplicate.data = body.data.copy()
    duplicate.name = f"{source.name}_ZOZO_BODY"
    duplicate.data.name = f"{duplicate.name}_MESH"
    # Blender object copies inherit custom properties.  ZOZO UUIDs must stay
    # unique or assigning this collider could steal the source Body's group.
    if "_solver_uuid" in duplicate:
        del duplicate["_solver_uuid"]
    handoff.objects.link(duplicate)
    duplicate["yohsai_role"] = _HANDOFF_BODY_ROLE
    duplicate["yohsai_source_collection"] = source.name
    duplicate["yohsai_source_body"] = body.name
    duplicate.display_type = "WIRE"
    duplicate.show_in_front = True
    duplicate.hide_render = True
    return duplicate


def _project_name(collection_name: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_-]+", "_", collection_name).strip("_")
    return f"yohsai_{value or 'clothes'}"


def prepare_for_zozo(
    context,
    collection: bpy.types.Collection | None,
    body: bpy.types.Object | None,
) -> ZozoPreparation:
    """Create solver-owned cloth/body copies and leave Yohsai untouched."""
    if collection is None or collection.get("yohsai_role") != "clothes":
        raise ZozoHandoffError("Select a loaded Yohsai Clothes collection first.")
    if body is None or body.type != "MESH":
        raise ZozoHandoffError("Select a mesh Body before Prepare for ZOZO.")
    try:
        parts, seams = completed_kitsuke_handoff(collection)
    except KitsukeError as exc:
        raise ZozoHandoffError(str(exc)) from exc
    if seams.size == 0:
        raise ZozoHandoffError("The completed GRAVITY state has no sewing edges.")

    context.view_layer.update()
    positions = np.concatenate([_world_vertices(part) for part in parts])
    if not np.all(np.isfinite(positions)):
        raise ZozoHandoffError("The completed cloth contains a non-finite vertex position.")
    bvh, body_centroid = _evaluated_body_bvh(context, body)
    positions, maximum_before, minimum_after = _open_stitches(
        positions, seams, bvh, body_centroid
    )

    handoff = _handoff_collection(context, collection)
    cloth = _create_cloth_object(handoff, collection, parts, positions, seams)
    # Unfold the gather-drape's self-intersections so ppf/ZOZO accepts the shell.
    _resolve_self_intersections(cloth, bvh)
    try:
        body_copy = _create_body_object(handoff, collection, body)
    except Exception:
        _remove_object_and_owned_mesh(cloth)
        raise

    for selected in context.selected_objects:
        selected.select_set(False)
    cloth.select_set(True)
    context.view_layer.objects.active = cloth
    context.view_layer.update()

    cloth_group_name = f"Yohsai {collection.name} Cloth"
    body_group_name = f"Yohsai {collection.name} Body"
    cloth["yohsai_zozo_group"] = cloth_group_name
    body_copy["yohsai_zozo_group"] = body_group_name
    return ZozoPreparation(
        collection=handoff,
        cloth_object=cloth,
        body_object=body_copy,
        seam_count=len(seams),
        maximum_input_seam_distance_m=maximum_before,
        minimum_output_seam_distance_m=minimum_after,
        cloth_group_name=cloth_group_name,
        body_group_name=body_group_name,
        project_name=_project_name(collection.name),
    )
