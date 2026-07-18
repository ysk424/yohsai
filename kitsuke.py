# SPDX-License-Identifier: GPL-3.0-or-later
"""Incremental, transient cloth simulation for the Yohsai Kitsuke workflow."""

from dataclasses import dataclass
from uuid import uuid4

import bpy
import numpy as np
from mathutils import Matrix, Vector
from mathutils.bvhtree import BVHTree

from .cosserat_native import (
    NativeCosseratError,
    NativeCosseratRuntime,
)
from .mesh_loader import (
    GRAINLINE_EDGE_FAMILY_ATTRIBUTE,
    GRAINLINE_EDGE_PROXY,
    GRAINLINE_EDGE_WARP,
    GRAINLINE_EDGE_WEFT,
    GRAINLINE_FACE_QUAD_ATTRIBUTE,
    LOCKED_OBJECT_KEY,
    SewingError,
    build_sewing_plan,
    compute_seam_count_overrides,
    participating_parts,
    remesh_with_seam_counts,
)


STEPS_PER_CLICK = 8
SOLVER_ITERATIONS = 20
MIN_SOLVER_ITERATIONS = 1
MAX_SOLVER_ITERATIONS = 128
COLLISION_SEARCH_M = 0.04
ZERO_GRAVITY_M_PER_SECOND_SQUARED = 0.0
NORMAL_GRAVITY_M_PER_SECOND_SQUARED = 9.81
KITSUKE_BACKEND_STABLE_COSSERAT = "STABLE_COSSERAT"
DEFAULT_KITSUKE_BACKEND = KITSUKE_BACKEND_STABLE_COSSERAT
KITSUKE_BACKENDS = frozenset((KITSUKE_BACKEND_STABLE_COSSERAT,))

_STATE_EPOCH_KEY = "yohsai_kitsuke_epoch"
_STATE_REVISION_KEY = "yohsai_kitsuke_revision"
_STATE_SEAMS_KEY = "yohsai_kitsuke_seams"
_STATE_SEAM_REST_KEY = "yohsai_kitsuke_seam_rest"
_STATE_MATRIX_KEY = "yohsai_kitsuke_matrix"
_STATE_BACKEND_KEY = "yohsai_kitsuke_backend"
_STATE_PARTS_KEY = "yohsai_kitsuke_parts"
_VELOCITY_ATTRIBUTE = "yohsai_kitsuke_velocity"
_RUNTIME_EPOCH = uuid4().hex


class KitsukeError(RuntimeError):
    """The current Blender state cannot be advanced by Kitsuke."""


@dataclass(frozen=True)
class _PartRange:
    obj: bpy.types.Object
    start: int
    count: int
    locked: bool = False


@dataclass(frozen=True)
class _BodySnapshot:
    vertices: np.ndarray
    faces: np.ndarray
    bvh: BVHTree
    ray_distance: float
    bounds_minimum: np.ndarray
    bounds_maximum: np.ndarray


@dataclass(frozen=True)
class _ClothTopology:
    edges: np.ndarray
    edge_rest_lengths: np.ndarray
    quads: np.ndarray
    quad_rest_metrics: np.ndarray
    bends: np.ndarray
    bend_rest_lengths: np.ndarray


_sessions: dict[int, "_KitsukeSession"] = {}


def _matrix_tuple(matrix: Matrix) -> tuple[float, ...]:
    return tuple(value for row in matrix for value in row)


def _parts(collection: bpy.types.Collection) -> list[bpy.types.Object]:
    return sorted(
        (
            obj
            for obj in collection.objects
            if obj.type == "MESH" and obj.get("yohsai_role") == "part"
        ),
        key=lambda obj: int(obj.get("yohsai_panel_index", 0)),
    )


def _persisted_state_is_current(collection: bpy.types.Collection | None) -> bool:
    if collection is None:
        return False
    return (
        str(collection.get(_STATE_EPOCH_KEY, "")) == _RUNTIME_EPOCH
        and int(collection.get(_STATE_REVISION_KEY, 0)) > 0
    )


def _read_persisted_state(
    collection: bpy.types.Collection,
    parts: list[_PartRange],
    seam_count: int,
    backend: str | None = None,
) -> tuple[int, np.ndarray, np.ndarray] | None:
    if not _persisted_state_is_current(collection):
        return None
    stored_backend = str(collection.get(_STATE_BACKEND_KEY, KITSUKE_BACKEND_STABLE_COSSERAT))
    if backend is not None and stored_backend != backend:
        raise KitsukeError(
            f"The restored Kitsuke state uses {stored_backend}, not {backend}. "
            "Select the restored solver or restart from Sewing."
        )
    try:
        revision = int(collection[_STATE_REVISION_KEY])
        seam_rest = np.asarray(collection[_STATE_SEAM_REST_KEY], dtype=np.float32)
    except (KeyError, TypeError, ValueError) as exc:
        raise KitsukeError("The stored Kitsuke Undo state is incomplete. Reload the pattern before continuing.") from exc
    if seam_rest.shape != (seam_count,) or not np.all(np.isfinite(seam_rest)):
        raise KitsukeError("The stored Kitsuke seam state no longer matches the current sewing constraints.")

    velocity_blocks: list[np.ndarray] = []
    for part in parts:
        attribute = part.obj.data.attributes.get(_VELOCITY_ATTRIBUTE)
        if (
            attribute is None
            or attribute.domain != "POINT"
            or attribute.data_type != "FLOAT_VECTOR"
            or len(attribute.data) != part.count
        ):
            raise KitsukeError(f"{part.obj.name} has no valid Kitsuke velocity state for Undo recovery.")
        block = np.empty((part.count, 3), dtype=np.float32)
        attribute.data.foreach_get("vector", block.ravel())
        if not np.all(np.isfinite(block)):
            raise KitsukeError(f"{part.obj.name} has a non-finite Kitsuke velocity state.")
        try:
            stored_matrix = tuple(float(value) for value in part.obj[_STATE_MATRIX_KEY])
        except (KeyError, TypeError, ValueError) as exc:
            raise KitsukeError(f"{part.obj.name} has no valid Object Mode transform for Undo recovery.") from exc
        if len(stored_matrix) != 16:
            raise KitsukeError(f"{part.obj.name} has an invalid Object Mode transform for Undo recovery.")
        if not np.allclose(stored_matrix, _matrix_tuple(part.obj.matrix_world), rtol=0.0, atol=1.0e-7):
            block.fill(0.0)
        velocity_blocks.append(block)
    return revision, seam_rest, np.concatenate(velocity_blocks).astype(np.float32)


def _read_persisted_seams(collection: bpy.types.Collection, vertex_count: int) -> np.ndarray:
    try:
        values = np.asarray(collection[_STATE_SEAMS_KEY], dtype=np.int32)
    except (KeyError, TypeError, ValueError) as exc:
        raise KitsukeError("The stored GRAVITY sewing pairs are incomplete; press GRAVITY again to rebuild them.") from exc
    if not len(values) or len(values) % 2:
        raise KitsukeError("The stored GRAVITY sewing pairs are invalid; press GRAVITY again to rebuild them.")
    seams = values.reshape((-1, 2))
    if np.any(seams < 0) or np.any(seams >= vertex_count) or np.any(seams[:, 0] == seams[:, 1]):
        raise KitsukeError("The stored Kitsuke sewing pairs no longer match the panel vertices.")
    return seams


def _write_velocity_state(part: _PartRange, velocities: np.ndarray) -> None:
    mesh = part.obj.data
    attribute = mesh.attributes.get(_VELOCITY_ATTRIBUTE)
    if attribute is not None and (attribute.domain != "POINT" or attribute.data_type != "FLOAT_VECTOR"):
        mesh.attributes.remove(attribute)
        attribute = None
    if attribute is None:
        attribute = mesh.attributes.new(name=_VELOCITY_ATTRIBUTE, type="FLOAT_VECTOR", domain="POINT")
    attribute.data.foreach_set("vector", np.asarray(velocities, dtype=np.float32).ravel())


def _clear_persisted_state(collection: bpy.types.Collection) -> None:
    for key in (
        _STATE_EPOCH_KEY,
        _STATE_REVISION_KEY,
        _STATE_SEAMS_KEY,
        _STATE_SEAM_REST_KEY,
        _STATE_BACKEND_KEY,
        _STATE_PARTS_KEY,
    ):
        if key in collection:
            del collection[key]
    for obj in _parts(collection):
        if _STATE_MATRIX_KEY in obj:
            del obj[_STATE_MATRIX_KEY]
        attribute = obj.data.attributes.get(_VELOCITY_ATTRIBUTE)
        if attribute is not None:
            obj.data.attributes.remove(attribute)


def _sewn_preview(collection: bpy.types.Collection) -> bpy.types.Object | None:
    previews = [obj for obj in collection.objects if obj.get("yohsai_role") == "sewn"]
    if len(previews) > 1:
        raise KitsukeError(f"{collection.name} contains more than one Sewing preview.")
    return previews[0] if previews else None


def _mesh_local_vertices(mesh: bpy.types.Mesh) -> np.ndarray:
    """Read mesh coordinates through Blender's bulk collection API."""
    result = np.empty((len(mesh.vertices), 3), dtype=np.float32)
    mesh.vertices.foreach_get("co", result.ravel())
    return result


def _transform_points(points: np.ndarray, matrix: Matrix) -> np.ndarray:
    """Apply one affine Blender matrix to a contiguous point block."""
    transform = np.asarray([tuple(row) for row in matrix], dtype=np.float32)
    transformed = np.asarray(points, dtype=np.float32) @ transform[:3, :3].T
    transformed += transform[:3, 3]
    return np.ascontiguousarray(transformed, dtype=np.float32)


def _world_vertices(obj: bpy.types.Object) -> np.ndarray:
    return _transform_points(_mesh_local_vertices(obj.data), obj.matrix_world)


def _cloth_topology(parts: list[_PartRange]) -> _ClothTopology:
    """Read the Body-independent material metric stored on each pattern mesh."""
    edges: list[tuple[int, int]] = []
    edge_rest_lengths: list[float] = []
    quads: list[tuple[int, int, int, int]] = []
    quad_rest_metrics: list[tuple[float, float, float]] = []
    bends: list[tuple[int, int, int]] = []
    bend_rest_lengths: list[tuple[float, float]] = []

    for part in parts:
        mesh = part.obj.data
        pattern_attribute = mesh.attributes.get("yohsai_pattern_position")
        rest_attribute = mesh.attributes.get("yohsai_pattern_edge_rest")
        family_attribute = mesh.attributes.get(GRAINLINE_EDGE_FAMILY_ATTRIBUTE)
        quad_attribute = mesh.attributes.get(GRAINLINE_FACE_QUAD_ATTRIBUTE)
        if (
            pattern_attribute is None
            or pattern_attribute.domain != "POINT"
            or pattern_attribute.data_type != "FLOAT_VECTOR"
            or len(pattern_attribute.data) != len(mesh.vertices)
        ):
            raise KitsukeError(
                f"{part.obj.name} has no valid pattern coordinates. Load it again before Kitsuke."
            )
        if (
            rest_attribute is None
            or rest_attribute.domain != "EDGE"
            or rest_attribute.data_type != "FLOAT"
            or len(rest_attribute.data) != len(mesh.edges)
        ):
            raise KitsukeError(
                f"{part.obj.name} has no valid material edge lengths. Load it again before Kitsuke."
            )
        if (
            family_attribute is None
            or family_attribute.domain != "EDGE"
            or family_attribute.data_type != "INT"
            or len(family_attribute.data) != len(mesh.edges)
        ):
            raise KitsukeError(
                f"{part.obj.name} has no valid grainline edge families. Load it again before Kitsuke."
            )
        if (
            quad_attribute is None
            or quad_attribute.domain != "FACE"
            or quad_attribute.data_type != "INT"
            or len(quad_attribute.data) != len(mesh.polygons)
        ):
            raise KitsukeError(
                f"{part.obj.name} has no valid grainline quad map. Load it again before Kitsuke."
            )

        pattern = np.asarray(
            [tuple(float(value) for value in item.vector) for item in pattern_attribute.data],
            dtype=np.float64,
        )
        families = np.asarray([int(item.value) for item in family_attribute.data], dtype=np.int32)
        local_rest = np.asarray([float(item.value) for item in rest_attribute.data], dtype=np.float64)
        if not np.all(np.isfinite(pattern)) or not np.all(np.isfinite(local_rest)):
            raise KitsukeError(f"{part.obj.name} contains non-finite material rest data.")

        axial_adjacency: dict[tuple[int, int], list[int]] = {}
        for edge in mesh.edges:
            family = int(families[edge.index])
            if family == GRAINLINE_EDGE_PROXY:
                continue
            a, b = (int(value) for value in edge.vertices)
            rest_length = float(local_rest[edge.index])
            if not rest_length > 1.0e-8:
                raise KitsukeError(f"{part.obj.name} contains a zero-length material edge.")
            edges.append((part.start + a, part.start + b))
            edge_rest_lengths.append(rest_length)
            if family in (GRAINLINE_EDGE_WARP, GRAINLINE_EDGE_WEFT):
                axial_adjacency.setdefault((family, a), []).append(b)
                axial_adjacency.setdefault((family, b), []).append(a)

        quad_groups: dict[int, set[int]] = {}
        for polygon in mesh.polygons:
            quad_index = int(quad_attribute.data[polygon.index].value)
            if quad_index >= 0:
                quad_groups.setdefault(quad_index, set()).update(int(value) for value in polygon.vertices)
        for quad_index in sorted(quad_groups):
            corners = quad_groups[quad_index]
            if len(corners) != 4:
                raise KitsukeError(
                    f"{part.obj.name} grainline quad {quad_index} does not contain four shared vertices."
                )
            center = pattern[list(corners), :2].mean(axis=0)
            ordered = sorted(
                corners,
                key=lambda vertex: float(
                    np.arctan2(pattern[vertex, 1] - center[1], pattern[vertex, 0] - center[0])
                ),
            )
            p0, p1, p2, p3 = (pattern[vertex] for vertex in ordered)
            u = 0.5 * ((p1 - p0) + (p2 - p3))
            v = 0.5 * ((p3 - p0) + (p2 - p1))
            uu = float(np.dot(u, u))
            vv = float(np.dot(v, v))
            uv = float(np.dot(u, v))
            if uu <= 1.0e-16 or vv <= 1.0e-16:
                raise KitsukeError(f"{part.obj.name} grainline quad {quad_index} has a degenerate metric.")
            quads.append(tuple(part.start + vertex for vertex in ordered))
            quad_rest_metrics.append((uu, vv, uv))

        for (family, center_vertex), neighbors in sorted(axial_adjacency.items()):
            if len(neighbors) < 2:
                continue
            best: tuple[float, int, int] | None = None
            for left_index, left in enumerate(neighbors[:-1]):
                left_direction = pattern[left, :2] - pattern[center_vertex, :2]
                left_length = float(np.linalg.norm(left_direction))
                if left_length <= 1.0e-8:
                    continue
                for right in neighbors[left_index + 1 :]:
                    right_direction = pattern[right, :2] - pattern[center_vertex, :2]
                    right_length = float(np.linalg.norm(right_direction))
                    if right_length <= 1.0e-8:
                        continue
                    cosine = float(np.dot(left_direction, right_direction) / (left_length * right_length))
                    if cosine <= -0.95 and (best is None or cosine < best[0]):
                        best = (cosine, left, right)
            if best is None:
                continue
            _cosine, left, right = best
            left_length = float(np.linalg.norm(pattern[left] - pattern[center_vertex]))
            right_length = float(np.linalg.norm(pattern[right] - pattern[center_vertex]))
            bends.append((part.start + left, part.start + center_vertex, part.start + right))
            bend_rest_lengths.append((left_length, right_length))

    return _ClothTopology(
        np.asarray(edges, dtype=np.int32).reshape((-1, 2)),
        np.asarray(edge_rest_lengths, dtype=np.float32),
        np.asarray(quads, dtype=np.int32).reshape((-1, 4)),
        np.asarray(quad_rest_metrics, dtype=np.float32).reshape((-1, 3)),
        np.asarray(bends, dtype=np.int32).reshape((-1, 3)),
        np.asarray(bend_rest_lengths, dtype=np.float32).reshape((-1, 2)),
    )


def _seam_constraints(preview: bpy.types.Object, part_ranges: list[_PartRange]) -> np.ndarray:
    names = list(preview.get("yohsai_source_parts", []))
    expected = [part.obj.name for part in part_ranges]
    if names != expected:
        raise KitsukeError("The Sewing preview no longer matches its source cloth objects.")
    expected_vertices = sum(part.count for part in part_ranges)
    if len(preview.data.vertices) != expected_vertices:
        raise KitsukeError("The Sewing preview vertex count no longer matches its source cloth objects.")

    spring_edges: set[int] = set()
    for attribute in preview.data.attributes:
        if attribute.name.startswith("sewing_spring_") and attribute.domain == "EDGE":
            spring_edges.update(index for index, value in enumerate(attribute.data) if bool(value.value))
    if not spring_edges:
        raise KitsukeError("The Sewing preview contains no sewing constraints.")
    return np.asarray(
        [preview.data.edges[index].vertices[:] for index in sorted(spring_edges)],
        dtype=np.int32,
    ).reshape((-1, 2))


def _seam_constraints_from_parts(collection: bpy.types.Collection, part_ranges: list[_PartRange]) -> np.ndarray:
    if not bool(collection.get("yohsai_sewing_verified", False)):
        raise KitsukeError("Automatic Sewing is required before GRAVITY can advance.")
    try:
        plan = build_sewing_plan(collection)
    except SewingError as exc:
        raise KitsukeError(f"Automatic Sewing failed: {exc}") from exc
    expected = [part.obj.name for part in part_ranges]
    if [part.name for part in plan.parts] != expected:
        raise KitsukeError("Automatic Sewing failed: the verified panel set no longer matches the current objects.")
    return np.asarray([(a, b) for _label, a, b in plan.connections], dtype=np.int32).reshape((-1, 2))


def _body_snapshot(context, body: bpy.types.Object) -> _BodySnapshot:
    if body is None:
        raise KitsukeError("Select a mesh Body before pressing GRAVITY.")
    if body.type != "MESH":
        raise KitsukeError(
            f"Body '{body.name}' is {body.type}, not MESH. Select the character's actual skin mesh."
        )
    depsgraph = context.evaluated_depsgraph_get()
    evaluated = body.evaluated_get(depsgraph)
    mesh = evaluated.to_mesh()
    try:
        mesh.calc_loop_triangles()
        matrix = evaluated.matrix_world
        vertices = _transform_points(_mesh_local_vertices(mesh), matrix)
        faces = np.asarray([triangle.vertices[:] for triangle in mesh.loop_triangles], dtype=np.int32)
        # A reflected Object/parent transform reverses geometric winding after
        # vertices enter world space. Preserve the mesh's authored outward side
        # so native Body contact cannot turn its push-out into an inward pull.
        if matrix.to_3x3().determinant() < 0.0:
            faces = faces[:, (0, 2, 1)]
    finally:
        evaluated.to_mesh_clear()
    if not len(vertices) or not len(faces):
        raise KitsukeError("Body has no triangles for collision detection.")
    bvh = BVHTree.FromPolygons(
        [Vector(tuple(float(value) for value in vertex)) for vertex in vertices],
        [tuple(int(value) for value in face) for face in faces],
        all_triangles=True,
    )
    diagonal = float(np.linalg.norm(vertices.max(axis=0) - vertices.min(axis=0)))
    return _BodySnapshot(
        vertices,
        faces,
        bvh,
        max(diagonal * 2.0, 1.0),
        vertices.min(axis=0),
        vertices.max(axis=0),
    )


_PARITY_DIRECTIONS = tuple(
    Vector(direction).normalized()
    for direction in ((1.0, 0.371, 0.529), (-0.417, 1.0, 0.263), (0.193, -0.487, 1.0))
)


def _ray_intersection_count(body: _BodySnapshot, point: Vector, direction: Vector) -> int:
    count = 0
    origin = point.copy()
    remaining = body.ray_distance
    epsilon = max(body.ray_distance * 1.0e-7, 1.0e-7)
    while remaining > epsilon:
        location, _normal, face_index, distance = body.bvh.ray_cast(origin, direction, remaining)
        if face_index is None or location is None or distance is None:
            break
        count += 1
        advance = float(distance) + epsilon
        origin += direction * advance
        remaining -= advance
        if count > 1024:
            break
    return count


def _inside_body(body: _BodySnapshot, point: np.ndarray) -> bool:
    origin = Vector(tuple(float(value) for value in point))
    odd_votes = sum(_ray_intersection_count(body, origin, direction) % 2 for direction in _PARITY_DIRECTIONS)
    return odd_votes >= 2


def _body_collision_candidates(
    positions: np.ndarray,
    body: _BodySnapshot,
    eligible: np.ndarray | None = None,
) -> np.ndarray:
    """Return the nearest Body triangle for nearby or penetrating cloth vertices."""
    pairs: list[tuple[int, int]] = []
    # Keep the broad phase conservative at the float32 boundary: it may admit a
    # point just beyond the exact search distance, but must never reject one the
    # BVH would accept.
    bounds_padding = COLLISION_SEARCH_M + 1.0e-6
    expanded_minimum = body.bounds_minimum - bounds_padding
    expanded_maximum = body.bounds_maximum + bounds_padding
    candidate_mask = np.all(
        (positions >= expanded_minimum) & (positions <= expanded_maximum),
        axis=1,
    )
    if eligible is not None:
        candidate_mask &= eligible
    for vertex_index in np.flatnonzero(candidate_mask):
        point = positions[vertex_index]
        _location, _normal, face_index, _distance = body.bvh.find_nearest(
            Vector(tuple(float(value) for value in point)), COLLISION_SEARCH_M
        )
        if face_index is None and _inside_body(body, point):
            _location, _normal, face_index, _distance = body.bvh.find_nearest(
                Vector(tuple(float(value) for value in point))
            )
        if face_index is not None:
            pairs.append((vertex_index, int(face_index)))
    if not pairs:
        return np.empty((0, 2), dtype=np.int32)
    return np.asarray(pairs, dtype=np.int32)


def _unlocked_body_collision_candidates(positions: np.ndarray, body: _BodySnapshot, locked: np.ndarray) -> np.ndarray:
    return _body_collision_candidates(positions, body, locked == 0)


class _KitsukeSession:
    def __init__(self, context, collection, body, preview, backend: str):
        objects = list(participating_parts(collection))
        if len(objects) < 2:
            raise KitsukeError(
                "Automatic Sewing needs at least two pending or completed parts."
            )
        ranges: list[_PartRange] = []
        position_blocks: list[np.ndarray] = []
        locked_blocks: list[np.ndarray] = []
        offset = 0
        for obj in objects:
            if any(abs(float(scale) - 1.0) > 1.0e-5 for scale in obj.scale):
                raise KitsukeError(f"Apply Scale on {obj.name} before Kitsuke; moving and rotating are supported, scaling is not.")
            block = _world_vertices(obj)
            locked = bool(obj.get(LOCKED_OBJECT_KEY, False))
            ranges.append(_PartRange(obj, offset, len(block), locked))
            position_blocks.append(block)
            locked_blocks.append(np.full(len(block), 1 if locked else 0, dtype=np.int32))
            offset += len(block)
        self.collection = collection
        self.backend = backend
        self.parts = ranges
        self.positions = np.concatenate(position_blocks).astype(np.float32)
        self.locked = np.concatenate(locked_blocks).astype(np.int32)
        if preview is not None:
            self.seams = _seam_constraints(preview, ranges)
        elif _persisted_state_is_current(collection):
            self.seams = _read_persisted_seams(collection, len(self.positions))
        else:
            self.seams = _seam_constraints_from_parts(collection, ranges)
        persisted = None if preview is not None else _read_persisted_state(collection, ranges, len(self.seams), backend)
        if persisted is None:
            self.revision = 0
            self.velocities = np.zeros_like(self.positions)
        else:
            self.revision, persisted_seams, self.velocities = persisted
        self.body = _body_snapshot(context, body)
        self.topology = _cloth_topology(ranges)
        self.body_pointer = body.as_pointer()
        self.matrices = {part.obj.name: _matrix_tuple(part.obj.matrix_world) for part in ranges}
        self.preview = preview
        try:
            self.runtime = NativeCosseratRuntime(
                self.positions,
                self.velocities,
                self.seams,
                self.topology,
                self.body,
                self.locked,
            )
        except NativeCosseratError as exc:
            raise KitsukeError(str(exc)) from exc
        if persisted is not None:
            self.runtime.replace_seam_state(persisted_seams)

    def _read_user_transforms(self):
        context_changed = False
        for part_index, part in enumerate(self.parts):
            obj = part.obj
            if len(obj.data.vertices) != part.count:
                raise KitsukeError(
                    f"{obj.name} topology changed after Sewing. Change topology in the pattern and load it again."
                )
            if any(abs(float(scale) - 1.0) > 1.0e-5 for scale in obj.scale):
                raise KitsukeError(f"Apply Scale on {obj.name} before Kitsuke; moving and rotating are supported, scaling is not.")
            matrix = _matrix_tuple(obj.matrix_world)
            block = _world_vertices(obj)
            selection = slice(part.start, part.start + part.count)
            locked = bool(obj.get(LOCKED_OBJECT_KEY, False))
            locked_value = 1 if locked else 0
            if part.locked != locked or np.any(self.locked[selection] != locked_value):
                self.locked[selection] = locked_value
                self.velocities[selection] = 0.0
                self.parts[part_index] = _PartRange(obj, part.start, part.count, locked)
                context_changed = True
            if matrix != self.matrices[obj.name] or (
                self.preview is None
                and not np.allclose(block, self.positions[selection], rtol=0.0, atol=1.0e-6)
            ):
                selection = slice(part.start, part.start + part.count)
                self.positions[selection] = block
                self.velocities[selection] = 0.0
                self.matrices[obj.name] = matrix
                context_changed = True
        if context_changed:
            self.runtime.replace_state(self.positions, self.velocities, self.locked)

    def _scatter(self, context):
        for part in self.parts:
            obj = part.obj
            inverse = obj.matrix_world.inverted_safe()
            selection = self.positions[part.start : part.start + part.count]
            local_positions = _transform_points(selection, inverse)
            obj.data.vertices.foreach_set("co", local_positions.ravel())
            obj.data.update()
            self.positions[part.start : part.start + part.count] = _world_vertices(obj)
            obj.hide_set(False)
            obj.hide_render = False
        # Blender mesh coordinates are the undoable authority. Keep the live
        # native state on their exact float32 round trip so the next click and
        # an Undo-reconstructed repeat start bit-for-bit alike.
        self.runtime.replace_state(self.positions, self.velocities, self.locked)
        if self.preview is not None:
            mesh = self.preview.data
            bpy.data.objects.remove(self.preview, do_unlink=True)
            if mesh.users == 0:
                bpy.data.meshes.remove(mesh)
            self.preview = None
        for selected in context.selected_objects:
            selected.select_set(False)
        for part in self.parts:
            part.obj.select_set(True)
        context.view_layer.objects.active = self.parts[0].obj
        context.view_layer.update()

    def _persist_undo_state(self):
        for part in self.parts:
            selection = self.velocities[part.start : part.start + part.count]
            _write_velocity_state(part, selection)
            part.obj[_STATE_MATRIX_KEY] = list(_matrix_tuple(part.obj.matrix_world))
        self.revision += 1
        self.collection[_STATE_SEAMS_KEY] = [int(value) for value in self.seams.ravel()]
        self.collection[_STATE_SEAM_REST_KEY] = [float(value) for value in self.runtime.seam_state()]
        self.collection[_STATE_REVISION_KEY] = self.revision
        self.collection[_STATE_EPOCH_KEY] = _RUNTIME_EPOCH
        self.collection[_STATE_BACKEND_KEY] = self.backend
        self.collection[_STATE_PARTS_KEY] = [part.obj.name for part in self.parts]

    def advance(self, context, gravity_magnitude: float, solver_iterations: int):
        self._read_user_transforms()
        previous_positions = self.positions.copy()
        previous_velocities = self.velocities.copy()
        previous_seams = self.runtime.seam_state()
        body_candidates = _unlocked_body_collision_candidates(self.positions, self.body, self.locked)
        try:
            self.runtime.advance(body_candidates, gravity_magnitude, solver_iterations)
        except NativeCosseratError as exc:
            self.runtime.replace_state(previous_positions, previous_velocities, self.locked)
            self.runtime.replace_seam_state(previous_seams)
            raise KitsukeError(str(exc)) from exc
        positions, velocities = self.runtime.state()
        if not np.all(np.isfinite(positions)) or not np.all(np.isfinite(velocities)):
            self.positions = previous_positions
            self.velocities = previous_velocities
            self.runtime.replace_state(self.positions, self.velocities, self.locked)
            self.runtime.replace_seam_state(previous_seams)
            stats = self.runtime.last_stats
            detail = (
                f"; material {int(stats.get('edge_count', 0))} edges/"
                f"{int(stats.get('quad_count', 0))} quads/"
                f"{int(stats.get('bend_count', 0))} bends, "
                f"Body candidates {int(stats.get('body_candidate_count', 0))}"
            )
            raise KitsukeError(
                f"The simulation returned a non-finite state{detail} "
                "and was rolled back without changing the cloth."
            )
        self.positions, self.velocities = positions, velocities
        self._scatter(context)
        self._persist_undo_state()


def advance_kitsuke(
    context,
    collection: bpy.types.Collection,
    body: bpy.types.Object,
    gravity_magnitude: float,
    solver_iterations: int = SOLVER_ITERATIONS,
    backend: str = DEFAULT_KITSUKE_BACKEND,
) -> str:
    """Advance one fixed Kitsuke interval and restore the separate cloth objects."""
    solver_iterations = max(MIN_SOLVER_ITERATIONS, min(MAX_SOLVER_ITERATIONS, int(solver_iterations)))
    if backend not in KITSUKE_BACKENDS:
        raise KitsukeError(f"Unknown Kitsuke solver backend: {backend}")
    if collection is None or collection.get("yohsai_role") != "clothes":
        raise KitsukeError("No loaded Yohsai clothes collection is selected.")
    key = collection.as_pointer()
    session = _sessions.get(key)
    preview = _sewn_preview(collection)
    if session is not None and preview is not None and session.preview is None:
        # Undoing the first Kitsuke restores the verified preview. Reconstruct
        # the transient runtime from that authoritative Blender state.
        close = getattr(session.runtime, "close", None)
        if close is not None:
            close()
        session = None
        _sessions.pop(key, None)
    if session is None:
        session = _KitsukeSession(context, collection, body, preview, backend)
        _sessions[key] = session
    elif body is None or body.as_pointer() != session.body_pointer:
        raise KitsukeError("The Body used by this Kitsuke session cannot be changed after its first step.")
    elif backend != session.backend:
        raise KitsukeError(
            f"This live Kitsuke session uses {session.backend}. Undo to Sewing or reload the pattern before changing solvers."
        )
    session.advance(context, gravity_magnitude, solver_iterations)
    return (
        f"GRAVITY: square-lattice cloth + constant-force seams, {STEPS_PER_CLICK} steps; "
        f"material/contact {solver_iterations} iterations; gravity -Z {gravity_magnitude:.3g} m/s²"
    )


def clear_sessions() -> None:
    for session in _sessions.values():
        close = getattr(session.runtime, "close", None)
        if close is not None:
            close()
    _sessions.clear()


def reset_runtime_epoch() -> None:
    """Invalidate all live and saved recovery state before Blender loads a file."""
    global _RUNTIME_EPOCH
    clear_sessions()
    _RUNTIME_EPOCH = uuid4().hex


def clear_kitsuke_session(collection: bpy.types.Collection | None) -> None:
    if collection is not None:
        session = _sessions.pop(collection.as_pointer(), None)
        if session is not None:
            close = getattr(session.runtime, "close", None)
            if close is not None:
                close()
        _clear_persisted_state(collection)


def adapt_seam_counts(context, collection: bpy.types.Collection | None) -> set[str]:
    """Equalize every sewing seam's two sides to matching vertex counts so they
    pair 1:1 (the longer edge gathers between its matched vertices).

    Only mismatched panels are recut, and when nothing needs adapting the pass
    is a no-op.  A recut changes topology, so the now-stale kitsuke session and
    persisted state are dropped and sewing rebuilds on the new meshes.
    """
    if collection is None:
        return set()
    overrides = compute_seam_count_overrides(collection)
    if not overrides:
        return set()
    changed = remesh_with_seam_counts(context, collection, overrides)
    if changed:
        clear_kitsuke_session(collection)
    return changed


def completed_kitsuke_parts(collection: bpy.types.Collection | None) -> list[bpy.types.Object]:
    """Return only the parts written by the most recently completed Kitsuke step."""
    if collection is None:
        return []
    session = _sessions.get(collection.as_pointer())
    if session is not None and session.revision > 0:
        return [part.obj for part in session.parts]
    if not _persisted_state_is_current(collection):
        return []
    names = [str(name) for name in collection.get(_STATE_PARTS_KEY, [])]
    parts = {obj.name: obj for obj in _parts(collection)}
    return [parts[name] for name in names if name in parts]


def completed_kitsuke_handoff(
    collection: bpy.types.Collection | None,
) -> tuple[list[bpy.types.Object], np.ndarray]:
    """Return the ordered parts and sewing pairs from the last completed step.

    Solver hand-off code must use the exact vertex ordering persisted by
    Kitsuke.  Rebuilding a sewing plan here could silently choose a different
    path after the user has moved a completed panel.
    """
    parts = completed_kitsuke_parts(collection)
    if collection is None or len(parts) < 2:
        raise KitsukeError(
            "Prepare needs a completed GRAVITY step with at least two sewn parts."
        )
    expected_names = [str(name) for name in collection.get(_STATE_PARTS_KEY, [])]
    if [part.name for part in parts] != expected_names:
        raise KitsukeError(
            "A part from the completed GRAVITY step is missing; press GRAVITY again before Prepare."
        )
    vertex_count = sum(len(part.data.vertices) for part in parts)
    session = _sessions.get(collection.as_pointer())
    if session is not None and session.revision > 0:
        seams = np.asarray(session.seams, dtype=np.int32).copy()
    else:
        seams = _read_persisted_seams(collection, vertex_count).copy()
    return parts, seams


def has_kitsuke_session(collection: bpy.types.Collection | None) -> bool:
    return collection is not None and (
        collection.as_pointer() in _sessions or _persisted_state_is_current(collection)
    )
