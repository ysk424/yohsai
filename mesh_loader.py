# SPDX-License-Identifier: GPL-3.0-or-later
"""Create an initial cloth-ready Blender mesh from Yohsai pattern JSON."""

from __future__ import annotations

import math
import json
from dataclasses import dataclass
from itertools import permutations
from typing import Any, Iterable

import bpy
from mathutils import Vector
from mathutils.bvhtree import BVHTree
from mathutils.geometry import barycentric_transform, delaunay_2d_cdt


MESH_SPACING_M = 0.01
PANEL_GAP_M = 0.10
WORLD_Y_M = -1.0
BOTTOM_Z_M = 0.01
COLLECTION_PREFIX = "CLOTHES_"
GRAINLINE_EDGE_FAMILY_ATTRIBUTE = "yohsai_grainline_family"
GRAINLINE_FACE_QUAD_ATTRIBUTE = "yohsai_grainline_quad"
GRAINLINE_EDGE_PROXY = 0
GRAINLINE_EDGE_WARP = 1
GRAINLINE_EDGE_WEFT = 2
GRAINLINE_EDGE_TRANSITION = 3


class MeshLoadError(ValueError):
    """Validated JSON cannot be converted into the initial Blender mesh."""


class SewingError(ValueError):
    """Loaded pattern parts cannot be converted into an unambiguous sewn mesh."""


class UpdateError(ValueError):
    """A revised pattern cannot atomically replace the current panel meshes."""


@dataclass(frozen=True)
class EdgeMeta:
    sewing_group: str | None = None
    fold: bool = False
    ring: bool = False


@dataclass
class PanelGeometry:
    panel_id: str
    update_label: str | None
    instance_id: str
    mirror_side: str
    vertices: list[Vector]
    construction_vertices: list[Vector]
    pattern_vertices: list[Vector]
    edges: list[tuple[int, int]]
    faces: list[tuple[int, ...]]
    edge_meta: dict[tuple[int, int], EdgeMeta]
    edge_rest: dict[tuple[int, int], float]
    edge_family: dict[tuple[int, int], int]
    quads: list[tuple[int, int, int, int]]
    face_quads: dict[tuple[int, ...], int]
    ring_closed: bool


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


def _segment_points(segment: dict[str, Any], spacing: float, count: int | None = None) -> list[Vector]:
    start = _point(segment.get("start"), "segment.start")
    end = _point(segment.get("end"), "segment.end")
    kind = segment.get("type")
    if kind == "line":
        length = _distance(start, end)
        sample_count = count if count is not None else max(1, math.ceil(length / spacing))
        return [start.lerp(end, index / sample_count) for index in range(sample_count + 1)]
    if kind != "cubic":
        raise MeshLoadError(f"Unsupported JSON segment type: {kind!r}")
    control1 = _point(segment.get("control1"), "segment.control1")
    control2 = _point(segment.get("control2"), "segment.control2")
    estimates = [_cubic(start, control1, control2, end, index / 128.0) for index in range(129)]
    length = sum(_distance(a, b) for a, b in zip(estimates, estimates[1:]))
    sample_count = count if count is not None else max(1, math.ceil(length / spacing))
    return [_cubic(start, control1, control2, end, index / sample_count) for index in range(sample_count + 1)]


def _segment_meta(segment: dict[str, Any]) -> EdgeMeta:
    label = segment.get("sewing_group")
    if label is not None:
        if not isinstance(label, str) or len(label) != 1 or not label.isascii() or not label.isalpha():
            raise MeshLoadError(f"Invalid sewing group: {label!r}")
        label = label.upper()
    fold = bool(segment.get("fold", False))
    ring = bool(segment.get("ring", False))
    if ring and (label is not None or fold):
        raise MeshLoadError("A RING edge cannot also be a sewing or fold edge.")
    return EdgeMeta(label, fold, ring)


def _sample_segment(
    segment: dict[str, Any], spacing: float, count: int | None = None
) -> tuple[list[Vector], list[EdgeMeta]]:
    points = _segment_points(segment, spacing, count)
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
    ring_indices = [index for index, segment in enumerate(segments) if bool(segment.get("ring", False))]
    if len(fold_indices) > 1:
        raise MeshLoadError(f"Panel {panel.get('id')!r} has more than one fold segment.")
    if ring_indices and len(ring_indices) != 2:
        raise MeshLoadError(f"Panel {panel.get('id')!r} must have exactly two RING segments.")
    if ring_indices and fold_indices:
        raise MeshLoadError(f"Panel {panel.get('id')!r} cannot combine RING and @W in one panel.")

    if not fold_indices:
        ring_count = None
        if ring_indices:
            ring_count = max(
                len(_segment_points(segments[index], spacing)) - 1
                for index in ring_indices
            )
        points: list[Vector] = []
        metadata: list[EdgeMeta] = []
        for segment_index, segment in enumerate(segments):
            sampled, sampled_meta = _sample_segment(
                segment, spacing, ring_count if segment_index in ring_indices else None
            )
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
    mirrored_metadata = [EdgeMeta(meta.sewing_group, False, False) for meta in reversed(nonfold_metadata)]

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
    margin = spacing * 0.12
    result: list[Vector] = []
    # Pattern-page coordinates are the material frame.  Sampling integer
    # multiples of the pitch (rather than panel-local offsets) keeps every
    # panel on the same authored warp/weft lattice: page Y is warp and page X
    # is weft.
    first_x = math.ceil(min_x / spacing)
    last_x = math.floor(max_x / spacing)
    first_y = math.ceil(min_y / spacing)
    last_y = math.floor(max_y / spacing)
    for grid_y in range(first_y, last_y + 1):
        y = grid_y * spacing
        for grid_x in range(first_x, last_x + 1):
            x = grid_x * spacing
            point = Vector((x, y))
            if _point_in_polygon(point, polygon) and min(
                _point_segment_distance(point, polygon[index], polygon[(index + 1) % len(polygon)])
                for index in range(len(polygon))
            ) > margin:
                result.append(point)
    return result


def _edge_key(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a < b else (b, a)


def _face_key(face: Iterable[int]) -> tuple[int, ...]:
    return tuple(sorted(face))


def _grid_coordinate(value: float, spacing: float) -> int | None:
    coordinate = int(round(value / spacing))
    # mathutils.Vector stores float32 components, so page coordinates near a
    # metre need a sub-micron tolerance to round-trip a 5 mm lattice index.
    if abs(value - coordinate * spacing) <= spacing * 1.0e-4:
        return coordinate
    return None


def _grainline_topology(
    pattern_vertices: list[Vector],
    edges: list[tuple[int, int]],
    faces: list[tuple[int, ...]],
    spacing: float,
) -> tuple[
    dict[tuple[int, int], int],
    list[tuple[int, int, int, int]],
    dict[tuple[int, ...], int],
]:
    """Separate square material cells from the triangulated proxy surface."""
    grid_vertices: dict[tuple[int, int], int] = {}
    ambiguous_grid_vertices: set[tuple[int, int]] = set()
    for vertex_index, point in enumerate(pattern_vertices):
        grid_x = _grid_coordinate(point.x, spacing)
        grid_y = _grid_coordinate(point.y, spacing)
        if grid_x is None or grid_y is None:
            continue
        key = (grid_x, grid_y)
        if key in ambiguous_grid_vertices:
            continue
        if key in grid_vertices:
            # A RING seam may weld coincident pattern coordinates.  Such a
            # boundary location is deliberately left in the transition strip.
            grid_vertices.pop(key)
            ambiguous_grid_vertices.add(key)
            continue
        grid_vertices[key] = vertex_index

    edge_keys = {_edge_key(*edge) for edge in edges}
    face_indices = {
        _face_key(face): index
        for index, face in enumerate(faces)
        if len(face) == 3
    }
    quads: list[tuple[int, int, int, int]] = []
    face_quads: dict[tuple[int, ...], int] = {}
    proxy_diagonals: set[tuple[int, int]] = set()
    for grid_x, grid_y in sorted(grid_vertices):
        corner_keys = (
            (grid_x, grid_y),
            (grid_x + 1, grid_y),
            (grid_x + 1, grid_y + 1),
            (grid_x, grid_y + 1),
        )
        if not all(key in grid_vertices for key in corner_keys):
            continue
        bottom_left, bottom_right, top_right, top_left = (
            grid_vertices[key] for key in corner_keys
        )
        sides = (
            _edge_key(bottom_left, bottom_right),
            _edge_key(bottom_right, top_right),
            _edge_key(top_right, top_left),
            _edge_key(top_left, bottom_left),
        )
        if not all(side in edge_keys for side in sides):
            continue

        first_diagonal = _edge_key(bottom_left, top_right)
        second_diagonal = _edge_key(bottom_right, top_left)
        if (first_diagonal in edge_keys) == (second_diagonal in edge_keys):
            continue
        if first_diagonal in edge_keys:
            triangle_keys = (
                _face_key((bottom_left, bottom_right, top_right)),
                _face_key((bottom_left, top_right, top_left)),
            )
            diagonal = first_diagonal
        else:
            triangle_keys = (
                _face_key((bottom_left, bottom_right, top_left)),
                _face_key((bottom_right, top_right, top_left)),
            )
            diagonal = second_diagonal
        if not all(key in face_indices for key in triangle_keys):
            continue
        if any(key in face_quads for key in triangle_keys):
            raise MeshLoadError("A proxy triangle was assigned to more than one grainline quad.")

        quad_index = len(quads)
        quads.append((bottom_left, bottom_right, top_right, top_left))
        for key in triangle_keys:
            face_quads[key] = quad_index
        proxy_diagonals.add(diagonal)

    tolerance = spacing * 1.0e-4
    edge_family: dict[tuple[int, int], int] = {}
    for edge in edges:
        key = _edge_key(*edge)
        if key in proxy_diagonals:
            family = GRAINLINE_EDGE_PROXY
        else:
            a, b = (pattern_vertices[index] for index in edge)
            delta = b - a
            if abs(delta.x) <= tolerance and abs(delta.y) > tolerance:
                family = GRAINLINE_EDGE_WARP
            elif abs(delta.y) <= tolerance and abs(delta.x) > tolerance:
                family = GRAINLINE_EDGE_WEFT
            else:
                family = GRAINLINE_EDGE_TRANSITION
        edge_family[key] = family
    return edge_family, quads, face_quads


def _find_vertex(points: list[Vector], target: Vector, tolerance: float = 1.0e-8) -> int:
    for index, point in enumerate(points):
        if _distance(point, target) <= tolerance:
            return index
    raise MeshLoadError("A fold endpoint was not found on the expanded boundary.")


def _marked_edge_chains(
    edges: list[tuple[int, int]], edge_meta: dict[tuple[int, int], EdgeMeta], marker: str
) -> list[list[int]]:
    marked = [edge for edge in edges if bool(getattr(edge_meta.get(_edge_key(*edge)), marker, False))]
    adjacency: dict[int, set[int]] = {}
    for a, b in marked:
        adjacency.setdefault(a, set()).add(b)
        adjacency.setdefault(b, set()).add(a)
    if any(len(neighbors) > 2 for neighbors in adjacency.values()):
        raise MeshLoadError(f"{marker.upper()} edges form a branching path.")
    chains: list[list[int]] = []
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
        endpoints = [vertex for vertex in component if len(adjacency[vertex]) == 1]
        if len(endpoints) != 2:
            raise MeshLoadError(f"{marker.upper()} edges must form open paths before welding.")
        ordered = [min(endpoints)]
        previous = None
        current = ordered[0]
        while current not in endpoints or len(ordered) == 1:
            following = adjacency[current] - ({previous} if previous is not None else set())
            if not following:
                break
            vertex = next(iter(following))
            ordered.append(vertex)
            previous, current = current, vertex
        chains.append(ordered)
    return chains


def _boundary_x_at_y(points: list[Vector], y: float) -> float:
    candidates: list[tuple[float, float]] = []
    for start, end in zip(points, points[1:]):
        delta = end.y - start.y
        if abs(delta) <= 1.0e-12:
            candidates.append((abs(y - start.y), (start.x + end.x) * 0.5))
            continue
        factor = (y - start.y) / delta
        clamped = max(0.0, min(1.0, factor))
        projected_y = start.y + delta * clamped
        candidates.append((abs(y - projected_y), start.x + (end.x - start.x) * clamped))
    if not candidates:
        raise MeshLoadError("A RING boundary has no usable edges.")
    return min(candidates, key=lambda item: item[0])[1]


def _ring_construction_vertices(
    pattern_vertices: list[Vector], ring_chains: list[list[int]], top: Vector
) -> list[Vector]:
    left_indices, right_indices = sorted(
        ring_chains, key=lambda chain: sum(pattern_vertices[index].x for index in chain) / len(chain)
    )
    direct = sum(abs(pattern_vertices[a].y - pattern_vertices[b].y) for a, b in zip(left_indices, right_indices))
    reverse = sum(
        abs(pattern_vertices[a].y - pattern_vertices[b].y)
        for a, b in zip(left_indices, reversed(right_indices))
    )
    if reverse < direct:
        right_indices = list(reversed(right_indices))
    left = [pattern_vertices[index] for index in left_indices]
    right = [pattern_vertices[index] for index in right_indices]
    widths = [(right_point - left_point).length for left_point, right_point in zip(left, right)]
    circumference = sum(widths) / len(widths)
    if circumference <= 1.0e-10:
        raise MeshLoadError("RING edges do not enclose a usable sleeve width.")
    radius = circumference / (2.0 * math.pi)
    axis_center = sum(point.y for point in pattern_vertices) / len(pattern_vertices)

    top_left = _boundary_x_at_y(left, top.y)
    top_right = _boundary_x_at_y(right, top.y)
    if top_right - top_left <= 1.0e-10:
        raise MeshLoadError("@TOP cannot define the sleeve's upward direction.")
    top_u = (top.x - top_left) / (top_right - top_left)

    result: list[Vector] = []
    for point in pattern_vertices:
        left_x = _boundary_x_at_y(left, point.y)
        right_x = _boundary_x_at_y(right, point.y)
        if right_x - left_x <= 1.0e-10:
            raise MeshLoadError("RING boundaries cross while constructing the sleeve tube.")
        u = (point.x - left_x) / (right_x - left_x)
        angle = 2.0 * math.pi * (u - top_u)
        result.append(Vector((point.y - axis_center, radius * math.sin(angle), radius * math.cos(angle))))
    return result


def _weld_ring(
    pattern_vertices: list[Vector],
    construction_vertices: list[Vector],
    edges: list[tuple[int, int]],
    faces: list[tuple[int, ...]],
    edge_meta: dict[tuple[int, int], EdgeMeta],
) -> tuple[
    list[Vector], list[Vector], list[tuple[int, int]], list[tuple[int, ...]],
    dict[tuple[int, int], EdgeMeta], dict[tuple[int, int], float]
]:
    chains = _marked_edge_chains(edges, edge_meta, "ring")
    if len(chains) != 2 or len(chains[0]) != len(chains[1]):
        raise MeshLoadError("The two RING boundaries must produce matching vertex counts.")
    first, second = chains
    direct = sum(abs(pattern_vertices[a].y - pattern_vertices[b].y) for a, b in zip(first, second))
    reverse = sum(abs(pattern_vertices[a].y - pattern_vertices[b].y) for a, b in zip(first, reversed(second)))
    if reverse < direct:
        second = list(reversed(second))

    representative = {right: left for left, right in zip(first, second)}
    groups: dict[int, list[int]] = {}
    for index in range(len(pattern_vertices)):
        groups.setdefault(representative.get(index, index), []).append(index)
    kept = sorted(groups)
    new_index = {old: index for index, old in enumerate(kept)}

    new_pattern = [
        sum((pattern_vertices[index] for index in groups[old]), Vector((0.0, 0.0))) / len(groups[old])
        for old in kept
    ]
    new_construction = [
        sum((construction_vertices[index] for index in groups[old]), Vector((0.0, 0.0, 0.0))) / len(groups[old])
        for old in kept
    ]

    def remap_vertex(index: int) -> int:
        return new_index[representative.get(index, index)]

    new_faces: list[tuple[int, ...]] = []
    for face in faces:
        remapped = tuple(remap_vertex(index) for index in face)
        if len(set(remapped)) < 3:
            continue
        new_faces.append(remapped)

    meta_values: dict[tuple[int, int], list[EdgeMeta]] = {}
    rest_values: dict[tuple[int, int], list[float]] = {}
    for a, b in edges:
        key = _edge_key(remap_vertex(a), remap_vertex(b))
        if key[0] == key[1]:
            continue
        meta_values.setdefault(key, []).append(edge_meta.get(_edge_key(a, b), EdgeMeta()))
        rest_values.setdefault(key, []).append((pattern_vertices[a] - pattern_vertices[b]).length)

    new_meta: dict[tuple[int, int], EdgeMeta] = {}
    for key, values in meta_values.items():
        labels = {value.sewing_group for value in values if value.sewing_group}
        if len(labels) > 1:
            raise MeshLoadError("RING welding merged conflicting sewing edges.")
        meta = EdgeMeta(next(iter(labels), None), any(value.fold for value in values), False)
        if meta.sewing_group or meta.fold:
            new_meta[key] = meta
    new_rest = {key: sum(values) / len(values) for key, values in rest_values.items()}
    return new_pattern, new_construction, sorted(rest_values), new_faces, new_meta, new_rest


def _triangulate_panel(
    panel: dict[str, Any], spacing: float, mirror_side: str = ""
) -> PanelGeometry:
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
            input_meta.append(EdgeMeta(None, True, False))

    grid_points = _interior_grid(outline, spacing)
    input_vertices.extend(grid_points)
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
    if any(len(face) != 3 for face in faces):
        raise MeshLoadError(f"Panel {panel_id!r} triangulation produced a non-triangular proxy face.")
    triangles = [tuple(face) for face in faces]
    if not triangles:
        raise MeshLoadError(f"Panel {panel_id!r} triangulation produced no faces.")

    edge_meta: dict[tuple[int, int], EdgeMeta] = {}
    for edge, origins in zip(edges, original_edges):
        labels: set[str] = set()
        fold = False
        ring = False
        for origin in origins:
            if 0 <= origin < len(input_meta):
                meta = input_meta[origin]
                if meta.sewing_group:
                    labels.add(meta.sewing_group)
                fold = fold or meta.fold
                ring = ring or meta.ring
        if len(labels) > 1:
            raise MeshLoadError(f"Panel {panel_id!r} triangulation merged conflicting sewing edges.")
        if labels or fold or ring:
            edge_meta[_edge_key(*edge)] = EdgeMeta(next(iter(labels), None), fold, ring)

    update_label = panel.get("label")
    if update_label is not None and (not isinstance(update_label, str) or not update_label):
        raise MeshLoadError(f"Panel {panel_id!r} has an invalid update label.")
    pattern_vertices = list(vertices)
    result_edges = list(edges)
    result_faces = triangles
    ring_closed = any(meta.ring for meta in edge_meta.values())
    if ring_closed:
        top = _point(panel.get("top"), "panel.top")
        ring_chains = _marked_edge_chains(result_edges, edge_meta, "ring")
        if len(ring_chains) != 2:
            raise MeshLoadError(f"Panel {panel_id!r} must triangulate to two RING boundary paths.")
        construction_vertices = _ring_construction_vertices(pattern_vertices, ring_chains, top)
        (
            pattern_vertices,
            construction_vertices,
            result_edges,
            result_faces,
            edge_meta,
            edge_rest,
        ) = _weld_ring(pattern_vertices, construction_vertices, result_edges, result_faces, edge_meta)
        for face in result_faces:
            a, b, c = (construction_vertices[index] for index in face[:3])
            normal = (b - a).cross(c - a)
            center = (a + b + c) / 3.0
            radial = Vector((0.0, center.y, center.z))
            if normal.length_squared > 1.0e-16 and radial.length_squared > 1.0e-16:
                if normal.dot(radial) < 0.0:
                    result_faces = [tuple(reversed(item)) for item in result_faces]
                break
    else:
        construction_vertices = [Vector((point.x, 0.0, point.y)) for point in pattern_vertices]
        edge_rest = {
            _edge_key(a, b): (pattern_vertices[a] - pattern_vertices[b]).length
            for a, b in result_edges
        }

    edge_family, quads, face_quads = _grainline_topology(
        pattern_vertices,
        result_edges,
        result_faces,
        spacing,
    )

    mirrored = mirror_side == "RIGHT"
    if mirrored:
        center_x = (min(point.x for point in pattern_vertices) + max(point.x for point in pattern_vertices)) * 0.5
        pattern_vertices = [Vector((2.0 * center_x - point.x, point.y)) for point in pattern_vertices]
        construction_vertices = [Vector((-point.x, point.y, point.z)) for point in construction_vertices]
        quads = [(bottom_right, bottom_left, top_left, top_right) for bottom_left, bottom_right, top_right, top_left in quads]
        result_faces = [tuple(reversed(face)) for face in result_faces]

    base_instance = str(update_label or panel_id)
    instance_id = f"{base_instance}:{mirror_side}" if mirror_side else base_instance
    return PanelGeometry(
        panel_id=panel_id,
        update_label=update_label,
        instance_id=instance_id,
        mirror_side=mirror_side,
        vertices=[point.copy() for point in construction_vertices],
        construction_vertices=[point.copy() for point in construction_vertices],
        pattern_vertices=[point.copy() for point in pattern_vertices],
        edges=result_edges,
        faces=result_faces,
        edge_meta=edge_meta,
        edge_rest=edge_rest,
        edge_family=edge_family,
        quads=quads,
        face_quads=face_quads,
        ring_closed=ring_closed,
    )


def _panel_geometries(panels: list[dict[str, Any]], spacing: float) -> list[PanelGeometry]:
    result: list[PanelGeometry] = []
    for panel in panels:
        if bool(panel.get("mirror", False)):
            result.append(_triangulate_panel(panel, spacing, "LEFT"))
            result.append(_triangulate_panel(panel, spacing, "RIGHT"))
        else:
            result.append(_triangulate_panel(panel, spacing))
    return result


def _pack_panels(panels: list[PanelGeometry], gap: float) -> None:
    bounds = [
        (
            min(vertex.x for vertex in panel.vertices),
            max(vertex.x for vertex in panel.vertices),
            min(vertex.z for vertex in panel.vertices),
            (min(vertex.y for vertex in panel.vertices) + max(vertex.y for vertex in panel.vertices)) * 0.5,
        )
        for panel in panels
    ]
    total_width = sum(max_x - min_x for min_x, max_x, _min_z, _center_y in bounds) + gap * max(0, len(panels) - 1)
    cursor = -total_width / 2.0
    for panel, (min_x, max_x, min_z, center_y) in zip(panels, bounds):
        shift = Vector((cursor - min_x, WORLD_Y_M - center_y, BOTTOM_Z_M - min_z))
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

    rest_attribute = mesh.attributes.new(name="yohsai_pattern_edge_rest", type="FLOAT", domain="EDGE")
    for key, value in panel.edge_rest.items():
        edge_index = mesh_edge_lookup.get(key)
        if edge_index is None:
            raise MeshLoadError("A pattern rest edge was lost while creating the Blender mesh.")
        rest_attribute.data[edge_index].value = value

    family_attribute = mesh.attributes.new(
        name=GRAINLINE_EDGE_FAMILY_ATTRIBUTE, type="INT", domain="EDGE"
    )
    for edge in mesh.edges:
        key = _edge_key(*edge.vertices)
        family_attribute.data[edge.index].value = panel.edge_family.get(
            key, GRAINLINE_EDGE_TRANSITION
        )

    panel_attribute = mesh.attributes.new(name="panel_index", type="INT", domain="FACE")
    quad_attribute = mesh.attributes.new(
        name=GRAINLINE_FACE_QUAD_ATTRIBUTE, type="INT", domain="FACE"
    )
    for polygon in mesh.polygons:
        panel_attribute.data[polygon.index].value = panel_index
        quad_attribute.data[polygon.index].value = panel.face_quads.get(
            _face_key(polygon.vertices), -1
        )

    pattern_attribute = mesh.attributes.new(name="yohsai_pattern_position", type="FLOAT_VECTOR", domain="POINT")
    for item, point in zip(pattern_attribute.data, panel.pattern_vertices):
        item.vector = (point.x, point.y, 0.0)

    construction_attribute = mesh.attributes.new(
        name="yohsai_construction_position", type="FLOAT_VECTOR", domain="POINT"
    )
    for item, point in zip(construction_attribute.data, panel.construction_vertices):
        item.vector = point


def _sewing_signature(document: dict[str, Any]) -> str:
    groups = document.get("sewing_groups")
    if not isinstance(groups, dict):
        raise MeshLoadError("Yohsai JSON has no sewing_groups object.")
    normalized: dict[str, list[tuple[str, int]]] = {}
    for label, references in groups.items():
        if not isinstance(label, str) or not isinstance(references, list):
            raise MeshLoadError("Yohsai JSON has an invalid sewing group.")
        values: list[tuple[str, int]] = []
        for reference in references:
            if not isinstance(reference, dict):
                raise MeshLoadError("Yohsai JSON has an invalid sewing reference.")
            try:
                values.append((str(reference["panel"]), int(reference["segment"])))
            except (KeyError, TypeError, ValueError) as exc:
                raise MeshLoadError("Yohsai JSON has an invalid sewing reference.") from exc
        normalized[label.upper()] = sorted(values)
    panels = document.get("panels")
    if not isinstance(panels, list):
        raise MeshLoadError("Yohsai JSON has no panels array.")
    construction: list[dict[str, object]] = []
    for panel in panels:
        if not isinstance(panel, dict):
            raise MeshLoadError("Yohsai JSON contains an invalid panel.")
        segments = panel.get("segments")
        if not isinstance(segments, list):
            raise MeshLoadError("Yohsai JSON contains an invalid panel segment array.")
        construction.append({
            "id": str(panel.get("id", "")),
            "mirror": bool(panel.get("mirror", False)),
            "top": panel.get("top"),
            "ring": [index for index, segment in enumerate(segments) if bool(segment.get("ring", False))],
        })
    return json.dumps(
        {"groups": normalized, "construction": construction}, sort_keys=True, separators=(",", ":")
    )


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

    if not all(isinstance(panel, dict) for panel in panels_json):
        raise MeshLoadError("Yohsai JSON contains an invalid panel.")
    panels = _panel_geometries(panels_json, MESH_SPACING_M)
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
            center = Vector((
                (min(vertex.x for vertex in panel.vertices) + max(vertex.x for vertex in panel.vertices)) / 2.0,
                (min(vertex.y for vertex in panel.vertices) + max(vertex.y for vertex in panel.vertices)) / 2.0,
                (min(vertex.z for vertex in panel.vertices) + max(vertex.z for vertex in panel.vertices)) / 2.0,
            ))
            vertices = [tuple(vertex - center) for vertex in panel.vertices]
            mesh.from_pydata(vertices, panel.edges, panel.faces)
            mesh.validate(verbose=False, clean_customdata=False)
            mesh.update(calc_edges=True, calc_edges_loose=True)
            _write_panel_mesh_attributes(mesh, panel, panel_index)
            obj.location = center

            obj["yohsai_schema"] = "yohsai-pattern/1.0.0"
            obj["yohsai_role"] = "part"
            obj["yohsai_collection"] = name
            obj["yohsai_source_svg"] = str(source.get("svg_path", ""))
            obj["yohsai_mesh_spacing_m"] = MESH_SPACING_M
            obj["yohsai_panel_id"] = panel.panel_id
            obj["yohsai_panel_label"] = panel.update_label or ""
            obj["yohsai_panel_instance"] = panel.instance_id
            obj["yohsai_panel_index"] = panel_index
            obj["yohsai_mirror_side"] = panel.mirror_side
            obj["yohsai_ring_closed"] = panel.ring_closed

        collection["yohsai_schema"] = "yohsai-pattern/1.0.0"
        collection["yohsai_role"] = "clothes"
        collection["yohsai_source_svg"] = str(source.get("svg_path", ""))
        collection["yohsai_sewing_signature"] = _sewing_signature(document)
        collection["yohsai_sewing_verified"] = False
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


def _pattern_positions(obj: bpy.types.Object) -> list[Vector]:
    attribute = obj.data.attributes.get("yohsai_pattern_position")
    if attribute is None or attribute.domain != "POINT" or len(attribute.data) != len(obj.data.vertices):
        raise UpdateError(
            f"{obj.name} has no original pattern coordinates. Load it again with the current Yohsai version."
        )
    return [Vector((item.vector[0], item.vector[1])) for item in attribute.data]


def _construction_positions(obj: bpy.types.Object) -> list[Vector]:
    attribute = obj.data.attributes.get("yohsai_construction_position")
    if attribute is None or attribute.domain != "POINT" or len(attribute.data) != len(obj.data.vertices):
        raise UpdateError(
            f"{obj.name} has no construction coordinates. Load it again with the current Yohsai version."
        )
    return [Vector(item.vector) for item in attribute.data]


def _transfer_deformation(obj: bpy.types.Object, panel: PanelGeometry) -> list[Vector]:
    old_flat = _pattern_positions(obj)
    old_faces = [tuple(polygon.vertices) for polygon in obj.data.polygons]
    if not old_faces:
        raise UpdateError(f"{obj.name} has no faces for deformation transfer.")
    old_world = [obj.matrix_world @ vertex.co for vertex in obj.data.vertices]
    if panel.ring_closed:
        if not bool(obj.get("yohsai_ring_closed", False)):
            raise UpdateError(f"Panel #{panel.update_label} changed from flat to RING construction; load it again.")
        old_construction = _construction_positions(obj)
        bvh = BVHTree.FromPolygons(old_construction, old_faces, all_triangles=False)
        transferred: list[Vector] = []
        for point in panel.construction_vertices:
            location, _normal, face_index, _distance = bvh.find_nearest(point)
            if face_index is None or location is None:
                raise UpdateError(f"Panel #{panel.update_label} could not transfer its RING deformation.")
            face = old_faces[int(face_index)]
            if len(face) != 3:
                raise UpdateError(f"{obj.name} contains a non-triangular face.")
            a, b, c = face
            transferred.append(
                barycentric_transform(
                    location,
                    old_construction[a], old_construction[b], old_construction[c],
                    old_world[a], old_world[b], old_world[c],
                )
            )
        return transferred
    if bool(obj.get("yohsai_ring_closed", False)):
        raise UpdateError(f"Panel #{panel.update_label} removed its RING construction; load it again.")
    old_min = Vector((min(point.x for point in old_flat), min(point.y for point in old_flat)))
    old_max = Vector((max(point.x for point in old_flat), max(point.y for point in old_flat)))
    new_min = Vector((min(point.x for point in panel.pattern_vertices), min(point.y for point in panel.pattern_vertices)))
    new_max = Vector((max(point.x for point in panel.pattern_vertices), max(point.y for point in panel.pattern_vertices)))
    old_size = old_max - old_min
    new_size = new_max - new_min
    if min(old_size.x, old_size.y, new_size.x, new_size.y) <= 1.0e-10:
        raise UpdateError(f"Panel #{panel.update_label} has degenerate bounds.")

    flat3 = [Vector((point.x, point.y, 0.0)) for point in old_flat]
    bvh = BVHTree.FromPolygons(flat3, old_faces, all_triangles=False)
    transferred: list[Vector] = []
    for point in panel.pattern_vertices:
        normalized = Vector(((point.x - new_min.x) / new_size.x, (point.y - new_min.y) / new_size.y))
        old_point = Vector((old_min.x + normalized.x * old_size.x, old_min.y + normalized.y * old_size.y, 0.0))
        location, _normal, face_index, _distance = bvh.find_nearest(old_point)
        if face_index is None or location is None:
            raise UpdateError(f"Panel #{panel.update_label} could not transfer its deformation.")
        face = old_faces[int(face_index)]
        if len(face) != 3:
            raise UpdateError(f"{obj.name} contains a non-triangular face.")
        a, b, c = face
        transferred.append(
            barycentric_transform(location, flat3[a], flat3[b], flat3[c], old_world[a], old_world[b], old_world[c])
        )
    return transferred


def _remove_sewn_preview(collection: bpy.types.Collection) -> None:
    for obj in list(collection.objects):
        if obj.get("yohsai_role") != "sewn":
            continue
        mesh = obj.data
        bpy.data.objects.remove(obj, do_unlink=True)
        if mesh.users == 0:
            bpy.data.meshes.remove(mesh)


def update_clothes_mesh(context, collection: bpy.types.Collection, document: dict[str, Any]) -> tuple[bool, int]:
    """Recut all labeled panels, transfer their current pose, and atomically replace their meshes."""
    if collection is None or collection.get("yohsai_role") != "clothes":
        raise UpdateError("No loaded Yohsai clothes collection is selected.")
    source = document.get("source")
    panels_json = document.get("panels")
    if not isinstance(source, dict) or not isinstance(panels_json, list) or not panels_json:
        raise UpdateError("Updated Yohsai JSON has no valid source or panels.")
    old_source = str(collection.get("yohsai_source_svg", ""))
    new_source = str(source.get("svg_path", ""))
    if not old_source or not new_source or bpy.path.abspath(old_source) != bpy.path.abspath(new_source):
        raise UpdateError("Update must use the same PDF file as the selected Clothes collection.")

    parts = sorted(
        (obj for obj in collection.objects if obj.type == "MESH" and obj.get("yohsai_role") == "part"),
        key=lambda obj: int(obj.get("yohsai_panel_index", 0)),
    )
    if not all(isinstance(panel, dict) for panel in panels_json):
        raise UpdateError("Updated Yohsai JSON contains an invalid panel.")
    panels = _panel_geometries(panels_json, MESH_SPACING_M)
    if len(parts) != len(panels):
        raise UpdateError(f"Panel object count changed: expected {len(parts)}, found {len(panels)}.")
    old_by_instance: dict[str, bpy.types.Object] = {}
    for obj in parts:
        label = str(obj.get("yohsai_panel_label", ""))
        if not label:
            raise UpdateError(f"{obj.name} has no # panel label. Load the labeled pattern again first.")
        instance_id = str(obj.get("yohsai_panel_instance", label))
        if instance_id in old_by_instance:
            raise UpdateError(f"Existing panel instance {instance_id!r} is duplicated.")
        old_by_instance[instance_id] = obj

    new_by_instance: dict[str, PanelGeometry] = {}
    for panel in panels:
        if not panel.update_label:
            raise UpdateError(f"Updated panel {panel.panel_id!r} has no # label.")
        if panel.instance_id in new_by_instance:
            raise UpdateError(f"Updated panel instance {panel.instance_id!r} is duplicated.")
        new_by_instance[panel.instance_id] = panel
    if set(old_by_instance) != set(new_by_instance):
        missing = sorted(set(old_by_instance) - set(new_by_instance))
        unexpected = sorted(set(new_by_instance) - set(old_by_instance))
        raise UpdateError(f"Panel labels changed or mirror instances changed; missing={missing}, unexpected={unexpected}.")

    prepared: list[tuple[bpy.types.Object, bpy.types.Mesh, PanelGeometry]] = []
    try:
        for instance_id, obj in old_by_instance.items():
            panel = new_by_instance[instance_id]
            world_positions = _transfer_deformation(obj, panel)
            inverse = obj.matrix_world.inverted_safe()
            local_positions = [inverse @ point for point in world_positions]
            mesh = bpy.data.meshes.new(f"{obj.name}_UPDATE")
            mesh.from_pydata(local_positions, panel.edges, panel.faces)
            mesh.validate(verbose=False, clean_customdata=False)
            mesh.update(calc_edges=True, calc_edges_loose=True)
            _write_panel_mesh_attributes(mesh, panel, int(obj.get("yohsai_panel_index", 0)))
            for material in obj.data.materials:
                mesh.materials.append(material)
            prepared.append((obj, mesh, panel))
    except Exception:
        for _obj, mesh, _panel in prepared:
            if mesh.users == 0:
                bpy.data.meshes.remove(mesh)
        raise

    old_meshes: list[bpy.types.Mesh] = []
    for obj, mesh, panel in prepared:
        old_meshes.append(obj.data)
        obj.data = mesh
        obj["yohsai_source_svg"] = new_source
        obj["yohsai_mesh_spacing_m"] = MESH_SPACING_M
        obj["yohsai_panel_id"] = panel.panel_id
        obj["yohsai_panel_label"] = panel.update_label
        obj["yohsai_panel_instance"] = panel.instance_id
        obj["yohsai_mirror_side"] = panel.mirror_side
        obj["yohsai_ring_closed"] = panel.ring_closed
        obj.hide_set(False)
        obj.hide_render = False
    _remove_sewn_preview(collection)
    for mesh in old_meshes:
        if mesh.users == 0:
            bpy.data.meshes.remove(mesh)

    old_signature = str(collection.get("yohsai_sewing_signature", ""))
    new_signature = _sewing_signature(document)
    sewing_changed = old_signature != new_signature
    collection["yohsai_source_svg"] = new_source
    collection["yohsai_sewing_signature"] = new_signature
    if sewing_changed:
        collection["yohsai_sewing_verified"] = False
    context.view_layer.update()
    return sewing_changed, sum(len(obj.data.vertices) for obj in parts)


@dataclass(frozen=True)
class _SeamChain:
    obj: bpy.types.Object
    vertices: tuple[int, ...]
    world_points: tuple[Vector, ...]
    edge_lengths: tuple[float, ...]
    closed: bool


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
    rest_attribute = mesh.attributes.get("yohsai_pattern_edge_rest")
    valid_rest = (
        rest_attribute is not None
        and rest_attribute.domain == "EDGE"
        and len(rest_attribute.data) == len(mesh.edges)
    )
    rest_by_edge: dict[tuple[int, int], float] = {}
    for edge in marked_edges:
        a, b = edge.vertices
        adjacency.setdefault(a, set()).add(b)
        adjacency.setdefault(b, set()).add(a)
        rest_by_edge[_edge_key(a, b)] = (
            float(rest_attribute.data[edge.index].value)
            if valid_rest else (mesh.vertices[a].co - mesh.vertices[b].co).length
        )
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
        closed = not endpoints
        if not closed and len(endpoints) != 2:
            raise SewingError(f"Sewing group {label} is not a continuous path on {obj.name}.")
        if closed:
            if any(len(adjacency[vertex]) != 2 for vertex in component):
                raise SewingError(f"Sewing group {label} is not a simple closed path on {obj.name}.")
            start = min(component)
            ordered = [start]
            previous = None
            current = start
            while True:
                candidates = adjacency[current] - ({previous} if previous is not None else set())
                if previous is None:
                    following = min(candidates)
                else:
                    following = next(iter(candidates))
                if following == start:
                    break
                if following in ordered:
                    raise SewingError(f"Cannot order closed sewing group {label} on {obj.name}.")
                ordered.append(following)
                previous, current = current, following
            if set(ordered) != component:
                raise SewingError(f"Cannot order closed sewing group {label} on {obj.name}.")
        else:
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
        pairs = list(zip(ordered, ordered[1:]))
        if closed:
            pairs.append((ordered[-1], ordered[0]))
        edge_lengths = tuple(rest_by_edge[_edge_key(a, b)] for a, b in pairs)
        chains.append(_SeamChain(obj, tuple(ordered), points, edge_lengths, closed))
    return chains


def _direction_cost(left: _SeamChain, right: _SeamChain, reverse: bool) -> float:
    if left.closed or right.closed:
        raise SewingError("Closed sewing paths require circular matching.")
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


def _cumulative_positions(edge_lengths: tuple[float, ...], vertex_count: int, closed: bool = False) -> list[float]:
    distances = [0.0]
    for length in edge_lengths[:vertex_count - 1]:
        distances.append(distances[-1] + length)
    total = sum(edge_lengths) if closed else distances[-1]
    if total <= 1.0e-10:
        raise SewingError("A sewing path has zero length.")
    return [distance / total for distance in distances]


def _ordered_vertex_pairs(left: _SeamChain, right: _SeamChain, label: str) -> list[tuple[int, int]]:
    if left.closed or right.closed:
        raise SewingError(f"Sewing group {label} mixes unsupported open and closed paths.")
    forward_cost = _direction_cost(left, right, False)
    reverse_cost = _direction_cost(left, right, True)
    if abs(forward_cost - reverse_cost) <= 1.0e-6:
        raise SewingError(f"Sewing direction for group {label} is ambiguous; move the parts closer to their intended seams.")
    right_vertices = list(right.vertices)
    right_points = right.world_points
    right_lengths = right.edge_lengths
    if reverse_cost < forward_cost:
        right_vertices.reverse()
        right_points = tuple(reversed(right_points))
        right_lengths = tuple(reversed(right_lengths))

    left_positions = _cumulative_positions(left.edge_lengths, len(left.vertices))
    right_positions = _cumulative_positions(right_lengths, len(right_vertices))
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


@dataclass(frozen=True)
class _GlobalSeamPath:
    vertices: tuple[int, ...]
    world_points: tuple[Vector, ...]
    edge_lengths: tuple[float, ...]


def _closure_cost(first: _SeamChain, second: _SeamChain, reverse_second: bool) -> float:
    if first.closed or second.closed:
        raise SewingError("Only open paths can be joined into a composite sewing loop.")
    second_start = second.world_points[-1] if reverse_second else second.world_points[0]
    second_end = second.world_points[0] if reverse_second else second.world_points[-1]
    return (
        (first.world_points[-1] - second_start).length
        + (second_end - first.world_points[0]).length
    )


def _global_chain(chain: _SeamChain, offset: int) -> _GlobalSeamPath:
    return _GlobalSeamPath(
        tuple(offset + vertex for vertex in chain.vertices),
        chain.world_points,
        chain.edge_lengths,
    )


def _composite_loop(
    first: _SeamChain, second: _SeamChain, offsets: dict[bpy.types.Object, int]
) -> _GlobalSeamPath:
    reverse_second = _closure_cost(first, second, True) < _closure_cost(first, second, False)
    second_vertices = list(second.vertices)
    second_points = list(second.world_points)
    second_lengths = list(second.edge_lengths)
    if reverse_second:
        second_vertices.reverse()
        second_points.reverse()
        second_lengths.reverse()
    vertices = tuple(
        [offsets[first.obj] + vertex for vertex in first.vertices]
        + [offsets[second.obj] + vertex for vertex in second_vertices]
    )
    points = first.world_points + tuple(second_points)
    # The two zero-length entries are virtual joins at the already sewn body
    # endpoints. They close the parameter loop without adding pattern length.
    lengths = first.edge_lengths + (0.0,) + tuple(second_lengths) + (0.0,)
    return _GlobalSeamPath(vertices, points, lengths)


def _reorder_closed_path(path: _GlobalSeamPath, order: list[int]) -> _GlobalSeamPath:
    edge_lookup = {
        _edge_key(path.vertices[index], path.vertices[(index + 1) % len(path.vertices)]): path.edge_lengths[index]
        for index in range(len(path.vertices))
    }
    vertices = tuple(path.vertices[index] for index in order)
    points = tuple(path.world_points[index] for index in order)
    lengths = tuple(
        edge_lookup[_edge_key(vertices[index], vertices[(index + 1) % len(vertices)])]
        for index in range(len(vertices))
    )
    return _GlobalSeamPath(vertices, points, lengths)


def _normalized_closed_pairs(left: _GlobalSeamPath, right: _GlobalSeamPath) -> list[tuple[int, int]]:
    left_positions = _cumulative_positions(left.edge_lengths, len(left.vertices), True)
    right_positions = _cumulative_positions(right.edge_lengths, len(right.vertices), True)
    pairs = [(left.vertices[0], right.vertices[0])]
    left_index = right_index = 0
    while left_index < len(left.vertices) - 1 or right_index < len(right.vertices) - 1:
        next_left = left_positions[left_index + 1] if left_index + 1 < len(left_positions) else math.inf
        next_right = right_positions[right_index + 1] if right_index + 1 < len(right_positions) else math.inf
        if abs(next_left - next_right) <= 1.0e-9:
            left_index += 1
            right_index += 1
        elif next_left < next_right:
            left_index += 1
        else:
            right_index += 1
        pair = (left.vertices[left_index], right.vertices[right_index])
        if pair != pairs[-1]:
            pairs.append(pair)
    return pairs


def _circular_alignment(
    left: _GlobalSeamPath, right: _GlobalSeamPath
) -> tuple[float, list[tuple[int, int]]]:
    best: tuple[float, list[tuple[int, int]]] | None = None
    count = len(right.vertices)
    for reverse in (False, True):
        base = list(range(count)) if not reverse else list(reversed(range(count)))
        for rotation in range(count):
            order = base[rotation:] + base[:rotation]
            candidate = _reorder_closed_path(right, order)
            pairs = _normalized_closed_pairs(left, candidate)
            left_points = {vertex: point for vertex, point in zip(left.vertices, left.world_points)}
            right_points = {vertex: point for vertex, point in zip(candidate.vertices, candidate.world_points)}
            cost = sum((left_points[a] - right_points[b]).length for a, b in pairs) / len(pairs)
            if best is None or cost < best[0]:
                best = (cost, pairs)
    if best is None:
        raise SewingError("Cannot align closed sewing paths.")
    return best


def _multipart_closed_pairs(
    by_object: dict[bpy.types.Object, list[_SeamChain]],
    offsets: dict[bpy.types.Object, int],
    label: str,
) -> list[tuple[int, int]]:
    closed = [chain for chains in by_object.values() for chain in chains if chain.closed]
    open_by_object = {
        obj: [chain for chain in chains if not chain.closed]
        for obj, chains in by_object.items()
        if any(not chain.closed for chain in chains)
    }
    if not closed or len(open_by_object) != 2:
        raise SewingError(
            f"Sewing group {label} needs closed RING paths and open paths on exactly two body parts."
        )
    count = len(closed)
    first_obj, second_obj = sorted(open_by_object, key=lambda obj: int(obj.get("yohsai_panel_index", 0)))
    first = open_by_object[first_obj]
    second = open_by_object[second_obj]
    if len(first) != count or len(second) != count or count > 8:
        raise SewingError(
            f"Sewing group {label} cannot pair its {count} closed path(s) with the body paths."
        )

    body_candidates: list[tuple[float, tuple[int, ...]]] = []
    for order in permutations(range(count)):
        cost = sum(
            min(_closure_cost(left, second[index], False), _closure_cost(left, second[index], True))
            for left, index in zip(first, order)
        )
        body_candidates.append((cost, order))
    body_candidates.sort(key=lambda item: item[0])
    body_order = body_candidates[0][1]
    body_loops = [
        _composite_loop(left, second[index], offsets)
        for left, index in zip(first, body_order)
    ]
    closed_loops = [_global_chain(chain, offsets[chain.obj]) for chain in closed]

    assignments: list[tuple[float, tuple[int, ...], list[list[tuple[int, int]]]]] = []
    for order in permutations(range(count)):
        total = 0.0
        pair_sets: list[list[tuple[int, int]]] = []
        for body_loop, closed_index in zip(body_loops, order):
            cost, pairs = _circular_alignment(body_loop, closed_loops[closed_index])
            total += cost
            pair_sets.append(pairs)
        assignments.append((total, order, pair_sets))
    assignments.sort(key=lambda item: item[0])
    return [pair for pair_set in assignments[0][2] for pair in pair_set]


@dataclass(frozen=True)
class SewingPlan:
    parts: tuple[bpy.types.Object, ...]
    labels: tuple[str, ...]
    connections: tuple[tuple[str, int, int], ...]


def build_sewing_plan(collection: bpy.types.Collection) -> SewingPlan:
    """Validate and return reusable global-index sewing connections for separate parts."""
    if collection is None or collection.get("yohsai_role") != "clothes":
        raise SewingError("No loaded Yohsai clothes collection is selected.")
    parts = tuple(sorted(
        (obj for obj in collection.objects if obj.type == "MESH" and obj.get("yohsai_role") == "part"),
        key=lambda obj: int(obj.get("yohsai_panel_index", 0)),
    ))
    if len(parts) < 2:
        raise SewingError("Sewing needs at least two separate cloth parts.")
    labels = tuple(sorted(set().union(*(_sewing_labels(obj.data) for obj in parts))))
    if not labels:
        raise SewingError("The loaded cloth parts contain no sewing groups.")

    offsets: dict[bpy.types.Object, int] = {}
    offset = 0
    for obj in parts:
        offsets[obj] = offset
        offset += len(obj.data.vertices)
    connections: list[tuple[str, int, int]] = []
    spring_keys: set[tuple[int, int]] = set()
    for label in labels:
        by_object = {obj: chains for obj in parts if (chains := _seam_chains(obj, label))}
        if any(chain.closed for chains in by_object.values() for chain in chains):
            pairs = _multipart_closed_pairs(by_object, offsets, label)
        else:
            if len(by_object) != 2:
                raise SewingError(f"Sewing group {label} must occur on exactly two different cloth parts.")
            first_obj, second_obj = sorted(by_object, key=lambda obj: int(obj.get("yohsai_panel_index", 0)))
            pairs = [
                (offsets[first_obj] + left_vertex, offsets[second_obj] + right_vertex)
                for left_chain, right_chain in _pair_chains(by_object[first_obj], by_object[second_obj], label)
                for left_vertex, right_vertex in _ordered_vertex_pairs(left_chain, right_chain, label)
            ]
        for a, b in pairs:
            key = _edge_key(a, b)
            if key in spring_keys:
                raise SewingError("Two sewing groups produce the same sewing spring.")
            spring_keys.add(key)
            connections.append((label, a, b))
    return SewingPlan(parts, labels, tuple(connections))


def create_sewn_mesh(
    context,
    collection: bpy.types.Collection,
) -> bpy.types.Object:
    """Merge positioned source parts and add loose sewing-spring edges."""
    if collection is None or collection.get("yohsai_role") != "clothes":
        raise SewingError("No loaded Yohsai clothes collection is selected.")
    if any(obj.get("yohsai_role") == "sewn" for obj in collection.objects):
        raise SewingError(f"{collection.name} already has a sewn mesh.")
    context.view_layer.update()
    plan = build_sewing_plan(collection)
    parts = list(plan.parts)
    labels = list(plan.labels)

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
    for label, a, b in plan.connections:
        spring_indices[label].append(len(edges))
        edges.append((a, b))

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
        collection["yohsai_sewing_verified"] = True

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
