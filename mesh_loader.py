# SPDX-License-Identifier: GPL-3.0-or-later
"""Create an initial cloth-ready Blender mesh from Yohsai pattern JSON."""

from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import permutations
from typing import Any, Iterable

import bpy
from mathutils import Vector
from mathutils.geometry import delaunay_2d_cdt


MESH_SPACING_M = 0.01
PANEL_GAP_M = 0.10
WORLD_Y_M = -1.0
BOTTOM_Z_M = 0.01
COLLECTION_PREFIX = "CLOTHES_"


class MeshLoadError(ValueError):
    """Validated JSON cannot be converted into the initial Blender mesh."""


class SewingError(ValueError):
    """Loaded pattern parts cannot be converted into an unambiguous sewn mesh."""


@dataclass(frozen=True)
class EdgeMeta:
    sewing_group: str | None = None
    fold: bool = False


@dataclass
class PanelGeometry:
    panel_id: str
    vertices: list[Vector]
    edges: list[tuple[int, int]]
    faces: list[tuple[int, ...]]
    edge_meta: dict[tuple[int, int], EdgeMeta]


def _point(value: object, field: str) -> Vector:
    if not isinstance(value, list) or len(value) != 2:
        raise MeshLoadError(f"{field} must be a two-number array.")
    try:
        result = Vector((float(value[0]), float(value[1])))
    except (TypeError, ValueError) as exc:
        raise MeshLoadError(f"{field} contains an invalid coordinate.") from exc
    if not all(math.isfinite(component) for component in result):
        raise MeshLoadError(f"{field} contains a non-finite coordinate.")
    return result


def _distance(a: Vector, b: Vector) -> float:
    return (a - b).length


def _cubic(start: Vector, control1: Vector, control2: Vector, end: Vector, t: float) -> Vector:
    inverse = 1.0 - t
    return (
        start * inverse**3
        + control1 * (3.0 * inverse**2 * t)
        + control2 * (3.0 * inverse * t**2)
        + end * t**3
    )


def _segment_points(segment: dict[str, Any], spacing: float) -> list[Vector]:
    start = _point(segment.get("start"), "segment.start")
    end = _point(segment.get("end"), "segment.end")
    kind = segment.get("type")
    if kind == "line":
        length = _distance(start, end)
        count = max(1, math.ceil(length / spacing))
        return [start.lerp(end, index / count) for index in range(count + 1)]
    if kind != "cubic":
        raise MeshLoadError(f"Unsupported JSON segment type: {kind!r}")
    control1 = _point(segment.get("control1"), "segment.control1")
    control2 = _point(segment.get("control2"), "segment.control2")
    estimates = [_cubic(start, control1, control2, end, index / 128.0) for index in range(129)]
    length = sum(_distance(a, b) for a, b in zip(estimates, estimates[1:]))
    count = max(1, math.ceil(length / spacing))
    return [_cubic(start, control1, control2, end, index / count) for index in range(count + 1)]


def _segment_meta(segment: dict[str, Any]) -> EdgeMeta:
    label = segment.get("sewing_group")
    if label is not None:
        if not isinstance(label, str) or len(label) != 1 or not label.isascii() or not label.isalpha():
            raise MeshLoadError(f"Invalid sewing group: {label!r}")
        label = label.upper()
    return EdgeMeta(label, bool(segment.get("fold", False)))


def _sample_segment(segment: dict[str, Any], spacing: float) -> tuple[list[Vector], list[EdgeMeta]]:
    points = _segment_points(segment, spacing)
    if len(points) < 2 or any(_distance(a, b) <= 1.0e-10 for a, b in zip(points, points[1:])):
        raise MeshLoadError("A panel contains a zero-length sampled edge.")
    return points, [_segment_meta(segment)] * (len(points) - 1)


def _reflect(point: Vector, line_start: Vector, line_end: Vector) -> Vector:
    axis = line_end - line_start
    length_squared = axis.length_squared
    if length_squared <= 1.0e-16:
        raise MeshLoadError("A fold edge has zero length.")
    projection = line_start + axis * ((point - line_start).dot(axis) / length_squared)
    return projection * 2.0 - point


def _signed_area(points: list[Vector]) -> float:
    return 0.5 * sum(
        point.x * points[(index + 1) % len(points)].y - points[(index + 1) % len(points)].x * point.y
        for index, point in enumerate(points)
    )


def _reverse_loop(points: list[Vector], metadata: list[EdgeMeta]) -> tuple[list[Vector], list[EdgeMeta]]:
    count = len(points)
    reversed_points = list(reversed(points))
    reversed_meta = [metadata[(count - 2 - index) % count] for index in range(count)]
    return reversed_points, reversed_meta


def _panel_outline(
    panel: dict[str, Any], spacing: float
) -> tuple[list[Vector], list[EdgeMeta], list[Vector]]:
    segments = panel.get("segments")
    if not isinstance(segments, list) or len(segments) < 3:
        raise MeshLoadError(f"Panel {panel.get('id')!r} needs at least three segments.")
    fold_indices = [index for index, segment in enumerate(segments) if bool(segment.get("fold", False))]
    if len(fold_indices) > 1:
        raise MeshLoadError(f"Panel {panel.get('id')!r} has more than one fold segment.")

    if not fold_indices:
        points: list[Vector] = []
        metadata: list[EdgeMeta] = []
        for segment in segments:
            sampled, sampled_meta = _sample_segment(segment, spacing)
            if not points:
                points.extend(sampled)
            else:
                if _distance(points[-1], sampled[0]) > 1.0e-8:
                    raise MeshLoadError(f"Panel {panel.get('id')!r} segments are not continuous.")
                points.extend(sampled[1:])
            metadata.extend(sampled_meta)
        if _distance(points[-1], points[0]) > 1.0e-8:
            raise MeshLoadError(f"Panel {panel.get('id')!r} is not closed.")
        points.pop()
        if len(points) != len(metadata):
            raise MeshLoadError("Internal boundary sampling error.")
        if _signed_area(points) < 0.0:
            points, metadata = _reverse_loop(points, metadata)
        return points, metadata, []

    fold_index = fold_indices[0]
    fold_segment = segments[fold_index]
    fold_points, _fold_metadata = _sample_segment(fold_segment, spacing)
    if fold_segment.get("type") != "line":
        raise MeshLoadError(f"Panel {panel.get('id')!r} fold segment must be straight in version 1.")
    fold_start, fold_end = fold_points[0], fold_points[-1]

    # Follow the authored non-fold boundary from the fold end back to its start.
    nonfold_points = [fold_end]
    nonfold_metadata: list[EdgeMeta] = []
    for offset in range(1, len(segments)):
        segment = segments[(fold_index + offset) % len(segments)]
        sampled, sampled_meta = _sample_segment(segment, spacing)
        if _distance(nonfold_points[-1], sampled[0]) > 1.0e-8:
            raise MeshLoadError(f"Panel {panel.get('id')!r} segments are not continuous.")
        nonfold_points.extend(sampled[1:])
        nonfold_metadata.extend(sampled_meta)
    if _distance(nonfold_points[-1], fold_start) > 1.0e-8:
        raise MeshLoadError(f"Panel {panel.get('id')!r} fold does not close the boundary.")

    reflected = [_reflect(point, fold_start, fold_end) for point in nonfold_points]
    mirrored_points = list(reversed(reflected))  # fold start -> fold end
    mirrored_metadata = [EdgeMeta(meta.sewing_group, False) for meta in reversed(nonfold_metadata)]

    # Close the original non-fold path with its mirrored counterpart. Endpoints
    # lie on the fold and are welded by using only one copy of each.
    points = nonfold_points + mirrored_points[1:-1]
    metadata = nonfold_metadata + mirrored_metadata
    if len(points) != len(metadata):
        raise MeshLoadError("Internal fold expansion error.")
    if _signed_area(points) < 0.0:
        points, metadata = _reverse_loop(points, metadata)
    return points, metadata, fold_points


def _point_segment_distance(point: Vector, start: Vector, end: Vector) -> float:
    delta = end - start
    if delta.length_squared <= 1.0e-20:
        return _distance(point, start)
    factor = max(0.0, min(1.0, (point - start).dot(delta) / delta.length_squared))
    return _distance(point, start + delta * factor)


def _point_in_polygon(point: Vector, polygon: list[Vector]) -> bool:
    inside = False
    previous = polygon[-1]
    for current in polygon:
        if (current.y > point.y) != (previous.y > point.y):
            crossing_x = (previous.x - current.x) * (point.y - current.y) / (previous.y - current.y) + current.x
            if point.x < crossing_x:
                inside = not inside
        previous = current
    return inside


def _interior_grid(polygon: list[Vector], spacing: float) -> list[Vector]:
    min_x = min(point.x for point in polygon)
    max_x = max(point.x for point in polygon)
    min_y = min(point.y for point in polygon)
    max_y = max(point.y for point in polygon)
    row_step = spacing * math.sqrt(3.0) / 2.0
    margin = spacing * 0.12
    result: list[Vector] = []
    row = 0
    y = min_y + row_step * 0.5
    while y < max_y:
        x = min_x + spacing * (0.5 if row % 2 == 0 else 1.0)
        while x < max_x:
            point = Vector((x, y))
            if _point_in_polygon(point, polygon) and min(
                _point_segment_distance(point, polygon[index], polygon[(index + 1) % len(polygon)])
                for index in range(len(polygon))
            ) > margin:
                result.append(point)
            x += spacing
        row += 1
        y += row_step
    return result


def _edge_key(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a < b else (b, a)


def _find_vertex(points: list[Vector], target: Vector, tolerance: float = 1.0e-8) -> int:
    for index, point in enumerate(points):
        if _distance(point, target) <= tolerance:
            return index
    raise MeshLoadError("A fold endpoint was not found on the expanded boundary.")


def _triangulate_panel(panel: dict[str, Any], spacing: float) -> PanelGeometry:
    panel_id = str(panel.get("id", "panel"))
    outline, outline_meta, fold_points = _panel_outline(panel, spacing)
    if len(outline) < 3 or abs(_signed_area(outline)) <= 1.0e-12:
        raise MeshLoadError(f"Panel {panel_id!r} has a degenerate expanded outline.")

    input_vertices = [point.copy() for point in outline]
    input_edges = [(index, (index + 1) % len(outline)) for index in range(len(outline))]
    input_meta = list(outline_meta)
    if fold_points:
        fold_indices = [_find_vertex(input_vertices, fold_points[0])]
        for point in fold_points[1:-1]:
            fold_indices.append(len(input_vertices))
            input_vertices.append(point.copy())
        fold_indices.append(_find_vertex(input_vertices, fold_points[-1]))
        for start, end in zip(fold_indices, fold_indices[1:]):
            input_edges.append((start, end))
            input_meta.append(EdgeMeta(None, True))

    input_vertices.extend(_interior_grid(outline, spacing))
    try:
        output = delaunay_2d_cdt(
            input_vertices,
            input_edges,
            [list(range(len(outline)))],
            1,
            1.0e-9,
            True,
        )
    except Exception as exc:
        raise MeshLoadError(f"Panel {panel_id!r} triangulation failed: {exc}") from exc
    vertices, edges, faces, _orig_vertices, original_edges, _original_faces = output
    triangles = [tuple(face) for face in faces if len(face) >= 3]
    if not triangles:
        raise MeshLoadError(f"Panel {panel_id!r} triangulation produced no faces.")

    edge_meta: dict[tuple[int, int], EdgeMeta] = {}
    for edge, origins in zip(edges, original_edges):
        labels: set[str] = set()
        fold = False
        for origin in origins:
            if 0 <= origin < len(input_meta):
                meta = input_meta[origin]
                if meta.sewing_group:
                    labels.add(meta.sewing_group)
                fold = fold or meta.fold
        if len(labels) > 1:
            raise MeshLoadError(f"Panel {panel_id!r} triangulation merged conflicting sewing edges.")
        if labels or fold:
            edge_meta[_edge_key(*edge)] = EdgeMeta(next(iter(labels), None), fold)

    return PanelGeometry(panel_id, list(vertices), list(edges), triangles, edge_meta)


def _pack_panels(panels: list[PanelGeometry], gap: float) -> None:
    bounds = [
        (
            min(vertex.x for vertex in panel.vertices),
            max(vertex.x for vertex in panel.vertices),
            min(vertex.y for vertex in panel.vertices),
        )
        for panel in panels
    ]
    total_width = sum(max_x - min_x for min_x, max_x, _min_y in bounds) + gap * max(0, len(panels) - 1)
    cursor = -total_width / 2.0
    for panel, (min_x, max_x, min_y) in zip(panels, bounds):
        shift = Vector((cursor - min_x, BOTTOM_Z_M - min_y))
        for vertex in panel.vertices:
            vertex += shift
        cursor += max_x - min_x + gap


def _next_clothes_name() -> str:
    index = 1
    while f"{COLLECTION_PREFIX}{index:03d}" in bpy.data.collections or f"{COLLECTION_PREFIX}{index:03d}" in bpy.data.objects:
        index += 1
    return f"{COLLECTION_PREFIX}{index:03d}"


def _set_boolean_edge_attribute(mesh: bpy.types.Mesh, name: str, edge_indices: Iterable[int]) -> None:
    attribute = mesh.attributes.new(name=name, type="BOOLEAN", domain="EDGE")
    for index in edge_indices:
        attribute.data[index].value = True


def _write_panel_mesh_attributes(
    mesh: bpy.types.Mesh,
    panel: PanelGeometry,
    panel_index: int,
) -> None:
    mesh_edge_lookup = {_edge_key(*edge.vertices): edge.index for edge in mesh.edges}
    sewing_edges: dict[str, list[int]] = {}
    fold_edges: list[int] = []
    for key, meta in panel.edge_meta.items():
        edge_index = mesh_edge_lookup.get(key)
        if edge_index is None:
            raise MeshLoadError("A constrained metadata edge was lost while creating the Blender mesh.")
        if meta.sewing_group:
            sewing_edges.setdefault(meta.sewing_group, []).append(edge_index)
        if meta.fold:
            fold_edges.append(edge_index)
    for label, indices in sorted(sewing_edges.items()):
        _set_boolean_edge_attribute(mesh, f"sewing_{label}", indices)
    _set_boolean_edge_attribute(mesh, "fold", fold_edges)

    panel_attribute = mesh.attributes.new(name="panel_index", type="INT", domain="FACE")
    for polygon in mesh.polygons:
        panel_attribute.data[polygon.index].value = panel_index


def create_clothes_mesh(context, document: dict[str, Any]) -> bpy.types.Collection:
    """Create one editable Blender object per expanded pattern panel."""
    if document.get("schema") != "yohsai-pattern" or document.get("version") != "1.0.0":
        raise MeshLoadError("Unsupported Yohsai JSON schema.")
    if document.get("units") != "m":
        raise MeshLoadError("Yohsai mesh loading requires meter units.")
    source = document.get("source")
    panels_json = document.get("panels")
    if not isinstance(source, dict) or not isinstance(panels_json, list) or not panels_json:
        raise MeshLoadError("Yohsai JSON has no valid source or panels.")

    panels = [_triangulate_panel(panel, MESH_SPACING_M) for panel in panels_json]
    _pack_panels(panels, PANEL_GAP_M)

    name = _next_clothes_name()
    collection = bpy.data.collections.new(name)
    created_objects: list[bpy.types.Object] = []
    try:
        context.scene.collection.children.link(collection)
        for panel_index, panel in enumerate(panels):
            object_name = f"{name}_PART_{panel_index + 1:03d}"
            mesh = bpy.data.meshes.new(object_name)
            obj = bpy.data.objects.new(object_name, mesh)
            collection.objects.link(obj)
            created_objects.append(obj)
            center_x = (min(vertex.x for vertex in panel.vertices) + max(vertex.x for vertex in panel.vertices)) / 2.0
            center_z = (min(vertex.y for vertex in panel.vertices) + max(vertex.y for vertex in panel.vertices)) / 2.0
            vertices = [(vertex.x - center_x, 0.0, vertex.y - center_z) for vertex in panel.vertices]
            mesh.from_pydata(vertices, panel.edges, panel.faces)
            mesh.validate(verbose=False, clean_customdata=False)
            mesh.update(calc_edges=True, calc_edges_loose=True)
            _write_panel_mesh_attributes(mesh, panel, panel_index)
            obj.location = (center_x, WORLD_Y_M, center_z)

            obj["yohsai_schema"] = "yohsai-pattern/1.0.0"
            obj["yohsai_role"] = "part"
            obj["yohsai_collection"] = name
            obj["yohsai_source_svg"] = str(source.get("svg_path", ""))
            obj["yohsai_mesh_spacing_m"] = MESH_SPACING_M
            obj["yohsai_panel_id"] = panel.panel_id
            obj["yohsai_panel_index"] = panel_index

        collection["yohsai_schema"] = "yohsai-pattern/1.0.0"
        collection["yohsai_role"] = "clothes"
        collection["yohsai_source_svg"] = str(source.get("svg_path", ""))
        context.view_layer.update()

        for selected in context.selected_objects:
            selected.select_set(False)
        for obj in created_objects:
            obj.select_set(True)
        context.view_layer.objects.active = created_objects[0]
        return collection
    except Exception:
        for obj in created_objects:
            mesh = obj.data
            bpy.data.objects.remove(obj, do_unlink=True)
            if mesh.name in bpy.data.meshes:
                bpy.data.meshes.remove(mesh)
        if collection.name in bpy.data.collections:
            bpy.data.collections.remove(collection)
        raise


@dataclass(frozen=True)
class _SeamChain:
    obj: bpy.types.Object
    vertices: tuple[int, ...]
    world_points: tuple[Vector, ...]


def _sewing_labels(mesh: bpy.types.Mesh) -> set[str]:
    labels: set[str] = set()
    for attribute in mesh.attributes:
        if attribute.name.startswith("sewing_") and len(attribute.name) == len("sewing_A"):
            label = attribute.name[-1].upper()
            if label.isascii() and label.isalpha() and attribute.domain == "EDGE":
                labels.add(label)
    return labels


def _seam_chains(obj: bpy.types.Object, label: str) -> list[_SeamChain]:
    mesh = obj.data
    attribute = mesh.attributes.get(f"sewing_{label}")
    if attribute is None or attribute.domain != "EDGE":
        return []
    marked_edges = [edge for edge in mesh.edges if bool(attribute.data[edge.index].value)]
    if not marked_edges:
        return []

    adjacency: dict[int, set[int]] = {}
    for edge in marked_edges:
        a, b = edge.vertices
        adjacency.setdefault(a, set()).add(b)
        adjacency.setdefault(b, set()).add(a)
    if any(len(neighbors) > 2 for neighbors in adjacency.values()):
        raise SewingError(f"Sewing group {label} branches on {obj.name}.")

    chains: list[_SeamChain] = []
    remaining = set(adjacency)
    while remaining:
        component: set[int] = set()
        pending = [next(iter(remaining))]
        while pending:
            vertex = pending.pop()
            if vertex in component:
                continue
            component.add(vertex)
            remaining.discard(vertex)
            pending.extend(adjacency[vertex] - component)
        endpoints = sorted(vertex for vertex in component if len(adjacency[vertex]) == 1)
        if len(endpoints) != 2:
            raise SewingError(f"Sewing group {label} is not an open continuous path on {obj.name}.")
        ordered = [endpoints[0]]
        previous = None
        current = endpoints[0]
        while current != endpoints[1]:
            candidates = adjacency[current] - ({previous} if previous is not None else set())
            if len(candidates) != 1:
                raise SewingError(f"Cannot order sewing group {label} on {obj.name}.")
            following = next(iter(candidates))
            ordered.append(following)
            previous, current = current, following
        points = tuple(obj.matrix_world @ mesh.vertices[index].co for index in ordered)
        chains.append(_SeamChain(obj, tuple(ordered), points))
    return chains


def _direction_cost(left: _SeamChain, right: _SeamChain, reverse: bool) -> float:
    right_start = right.world_points[-1] if reverse else right.world_points[0]
    right_end = right.world_points[0] if reverse else right.world_points[-1]
    return (left.world_points[0] - right_start).length + (left.world_points[-1] - right_end).length


def _pair_chains(left: list[_SeamChain], right: list[_SeamChain], label: str) -> list[tuple[_SeamChain, _SeamChain]]:
    if len(left) != len(right):
        raise SewingError(f"Sewing group {label} has different numbers of continuous paths on its two parts.")
    if len(left) > 8:
        raise SewingError(f"Sewing group {label} has too many separate paths for automatic pairing.")
    candidates: list[tuple[float, tuple[int, ...]]] = []
    for order in permutations(range(len(right))):
        cost = 0.0
        for left_chain, right_index in zip(left, order):
            right_chain = right[right_index]
            cost += min(_direction_cost(left_chain, right_chain, False), _direction_cost(left_chain, right_chain, True))
        candidates.append((cost, order))
    candidates.sort(key=lambda item: item[0])
    if len(candidates) > 1 and abs(candidates[1][0] - candidates[0][0]) <= 1.0e-6:
        raise SewingError(f"Sewing group {label} has an ambiguous path pairing; move the parts closer to their intended seams.")
    return [(left_chain, right[right_index]) for left_chain, right_index in zip(left, candidates[0][1])]


def _cumulative_positions(points: tuple[Vector, ...]) -> list[float]:
    distances = [0.0]
    for start, end in zip(points, points[1:]):
        distances.append(distances[-1] + (end - start).length)
    if distances[-1] <= 1.0e-10:
        raise SewingError("A sewing path has zero length.")
    return [distance / distances[-1] for distance in distances]


def _ordered_vertex_pairs(left: _SeamChain, right: _SeamChain, label: str) -> list[tuple[int, int]]:
    forward_cost = _direction_cost(left, right, False)
    reverse_cost = _direction_cost(left, right, True)
    if abs(forward_cost - reverse_cost) <= 1.0e-6:
        raise SewingError(f"Sewing direction for group {label} is ambiguous; move the parts closer to their intended seams.")
    right_vertices = list(right.vertices)
    right_points = right.world_points
    if reverse_cost < forward_cost:
        right_vertices.reverse()
        right_points = tuple(reversed(right_points))

    left_positions = _cumulative_positions(left.world_points)
    right_positions = _cumulative_positions(right_points)
    pairs = [(left.vertices[0], right_vertices[0])]
    left_index = right_index = 0
    while left_index < len(left.vertices) - 1 or right_index < len(right_vertices) - 1:
        next_left = left_positions[left_index + 1] if left_index + 1 < len(left_positions) else math.inf
        next_right = right_positions[right_index + 1] if right_index + 1 < len(right_positions) else math.inf
        if abs(next_left - next_right) <= 1.0e-9:
            left_index += 1
            right_index += 1
        elif next_left < next_right:
            left_index += 1
        else:
            right_index += 1
        pair = (left.vertices[left_index], right_vertices[right_index])
        if pair != pairs[-1]:
            pairs.append(pair)
    return pairs


def create_sewn_mesh(context, collection: bpy.types.Collection) -> bpy.types.Object:
    """Merge positioned source parts and add loose sewing-spring edges."""
    if collection is None or collection.get("yohsai_role") != "clothes":
        raise SewingError("No loaded Yohsai clothes collection is selected.")
    if any(obj.get("yohsai_role") == "sewn" for obj in collection.objects):
        raise SewingError(f"{collection.name} already has a sewn mesh.")
    parts = sorted(
        (obj for obj in collection.objects if obj.type == "MESH" and obj.get("yohsai_role") == "part"),
        key=lambda obj: int(obj.get("yohsai_panel_index", 0)),
    )
    if len(parts) < 2:
        raise SewingError("Sewing needs at least two separate cloth parts.")
    context.view_layer.update()

    labels = sorted(set().union(*(_sewing_labels(obj.data) for obj in parts)))
    if not labels:
        raise SewingError("The loaded cloth parts contain no sewing groups.")

    chains_by_label: dict[str, dict[bpy.types.Object, list[_SeamChain]]] = {}
    for label in labels:
        by_object = {obj: chains for obj in parts if (chains := _seam_chains(obj, label))}
        if len(by_object) != 2:
            raise SewingError(f"Sewing group {label} must occur on exactly two different cloth parts.")
        chains_by_label[label] = by_object

    vertices: list[tuple[float, float, float]] = []
    edges: list[tuple[int, int]] = []
    faces: list[tuple[int, ...]] = []
    offsets: dict[bpy.types.Object, int] = {}
    boundary_attributes: dict[str, list[int]] = {label: [] for label in labels}
    fold_indices: list[int] = []
    face_panel_indices: list[int] = []
    for obj in parts:
        mesh = obj.data
        offsets[obj] = len(vertices)
        offset = offsets[obj]
        vertices.extend(tuple(obj.matrix_world @ vertex.co) for vertex in mesh.vertices)
        for edge in mesh.edges:
            new_index = len(edges)
            edges.append((edge.vertices[0] + offset, edge.vertices[1] + offset))
            for label in labels:
                attribute = mesh.attributes.get(f"sewing_{label}")
                if attribute is not None and bool(attribute.data[edge.index].value):
                    boundary_attributes[label].append(new_index)
            fold = mesh.attributes.get("fold")
            if fold is not None and bool(fold.data[edge.index].value):
                fold_indices.append(new_index)
        faces.extend(tuple(vertex + offset for vertex in polygon.vertices) for polygon in mesh.polygons)
        face_panel_indices.extend([int(obj.get("yohsai_panel_index", 0))] * len(mesh.polygons))

    spring_indices: dict[str, list[int]] = {label: [] for label in labels}
    spring_keys: set[tuple[int, int]] = set()
    for label, by_object in chains_by_label.items():
        first_obj, second_obj = sorted(by_object, key=lambda obj: int(obj.get("yohsai_panel_index", 0)))
        for left_chain, right_chain in _pair_chains(by_object[first_obj], by_object[second_obj], label):
            for left_vertex, right_vertex in _ordered_vertex_pairs(left_chain, right_chain, label):
                edge = (offsets[first_obj] + left_vertex, offsets[second_obj] + right_vertex)
                key = _edge_key(*edge)
                if key in spring_keys:
                    raise SewingError("Two sewing groups produce the same sewing spring.")
                spring_keys.add(key)
                spring_indices[label].append(len(edges))
                edges.append(edge)

    name = f"{collection.name}_SEWN"
    mesh = bpy.data.meshes.new(name)
    obj = bpy.data.objects.new(name, mesh)
    try:
        collection.objects.link(obj)
        mesh.from_pydata(vertices, edges, faces)
        mesh.validate(verbose=False, clean_customdata=False)
        mesh.update(calc_edges=True, calc_edges_loose=True)
        for label in labels:
            _set_boolean_edge_attribute(mesh, f"sewing_{label}", boundary_attributes[label])
            _set_boolean_edge_attribute(mesh, f"sewing_spring_{label}", spring_indices[label])
        _set_boolean_edge_attribute(mesh, "fold", fold_indices)
        panel_attribute = mesh.attributes.new(name="panel_index", type="INT", domain="FACE")
        for polygon, panel_index in zip(mesh.polygons, face_panel_indices):
            panel_attribute.data[polygon.index].value = panel_index

        obj["yohsai_schema"] = "yohsai-pattern/1.0.0"
        obj["yohsai_role"] = "sewn"
        obj["yohsai_collection"] = collection.name
        obj["yohsai_source_svg"] = str(collection.get("yohsai_source_svg", ""))
        obj["yohsai_sewing_groups"] = labels
        obj["yohsai_source_parts"] = [part.name for part in parts]

        for selected in context.selected_objects:
            selected.select_set(False)
        for part in parts:
            part.hide_set(True)
            part.hide_render = True
            part.select_set(False)
        obj.select_set(True)
        context.view_layer.objects.active = obj
        return obj
    except Exception:
        bpy.data.objects.remove(obj, do_unlink=True)
        if mesh.name in bpy.data.meshes:
            bpy.data.meshes.remove(mesh)
        raise
