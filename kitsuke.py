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
    SewingError,
    build_sewing_plan,
)


TIME_STEP = 1.0 / 240.0
STEPS_PER_CLICK = 8
SOLVER_ITERATIONS = 16
MIN_SOLVER_ITERATIONS = 1
MAX_SOLVER_ITERATIONS = 128
CONTACT_THICKNESS_M = 0.005
CONTACT_CORRECTION_MAX_M = 0.0002
COLLISION_SEARCH_M = 0.04
DEFAULT_GRAVITY_M_PER_SECOND_SQUARED = 1.0
SEAM_ATTRACTION_FORCE = 300.0
SEAM_CAPTURE_DISTANCE_M = 0.002
STRETCH_RELAXATION = 1.0
SHEAR_RELAXATION = 0.02
BEND_RELAXATION = 0.0001
KITSUKE_BACKEND_STABLE_COSSERAT = "STABLE_COSSERAT"
KITSUKE_BACKEND_TAICHI = "TAICHI"
DEFAULT_KITSUKE_BACKEND = KITSUKE_BACKEND_STABLE_COSSERAT
KITSUKE_BACKENDS = frozenset((KITSUKE_BACKEND_STABLE_COSSERAT, KITSUKE_BACKEND_TAICHI))

_STATE_EPOCH_KEY = "yohsai_kitsuke_epoch"
_STATE_REVISION_KEY = "yohsai_kitsuke_revision"
_STATE_SEAMS_KEY = "yohsai_kitsuke_seams"
_STATE_SEAM_REST_KEY = "yohsai_kitsuke_seam_rest"
_STATE_MATRIX_KEY = "yohsai_kitsuke_matrix"
_STATE_BACKEND_KEY = "yohsai_kitsuke_backend"
_VELOCITY_ATTRIBUTE = "yohsai_kitsuke_velocity"
LOCKED_OBJECT_KEY = "yohsai_kitsuke_locked"
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


@dataclass(frozen=True)
class _ClothTopology:
    edges: np.ndarray
    edge_rest_lengths: np.ndarray
    quads: np.ndarray
    quad_rest_metrics: np.ndarray
    bends: np.ndarray
    bend_rest_lengths: np.ndarray


_sessions: dict[int, "_KitsukeSession"] = {}
_taichi = None
_runtime_type = None


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
    stored_backend = str(collection.get(_STATE_BACKEND_KEY, KITSUKE_BACKEND_TAICHI))
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
        raise KitsukeError("The stored Kitsuke sewing pairs are incomplete. Run Sewing again.") from exc
    if not len(values) or len(values) % 2:
        raise KitsukeError("The stored Kitsuke sewing pairs are invalid. Run Sewing again.")
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
    for key in (_STATE_EPOCH_KEY, _STATE_REVISION_KEY, _STATE_SEAMS_KEY, _STATE_SEAM_REST_KEY, _STATE_BACKEND_KEY):
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


def _world_vertices(obj: bpy.types.Object) -> np.ndarray:
    matrix = obj.matrix_world
    return np.asarray([tuple(matrix @ vertex.co) for vertex in obj.data.vertices], dtype=np.float32)


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
        raise KitsukeError("Sewing required: verify the current pattern connectivity before Kitsuke.")
    try:
        plan = build_sewing_plan(collection)
    except SewingError as exc:
        raise KitsukeError(f"Sewing required: {exc}") from exc
    expected = [part.obj.name for part in part_ranges]
    if [part.name for part in plan.parts] != expected:
        raise KitsukeError("Sewing required: the verified panel set no longer matches the current objects.")
    return np.asarray([(a, b) for _label, a, b in plan.connections], dtype=np.int32).reshape((-1, 2))


def _body_snapshot(context, body: bpy.types.Object) -> _BodySnapshot:
    if body is None:
        raise KitsukeError("Select a mesh Body before using Kitsuke.")
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
        vertices = np.asarray([tuple(matrix @ vertex.co) for vertex in mesh.vertices], dtype=np.float32)
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
    return _BodySnapshot(vertices, faces, bvh, max(diagonal * 2.0, 1.0))


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


def _project_body_penetrations(
    body: _BodySnapshot,
    positions: np.ndarray,
    velocities: np.ndarray,
    locked: np.ndarray,
) -> bool:
    changed = False
    for index, point in enumerate(positions):
        if locked[index]:
            continue
        if not _inside_body(body, point):
            continue
        location, _normal, face_index, _distance = body.bvh.find_nearest(
            Vector(tuple(float(value) for value in point))
        )
        if face_index is None or location is None:
            continue
        direction = location - Vector(tuple(float(value) for value in point))
        if direction.length_squared <= 1.0e-16:
            continue
        projected = location + direction.normalized() * CONTACT_THICKNESS_M
        positions[index] = projected
        velocities[index] = 0.0
        changed = True
    return changed


def _body_collision_candidates(positions: np.ndarray, body: _BodySnapshot) -> np.ndarray:
    """Return the nearest Body triangle for nearby or penetrating cloth vertices."""
    pairs: list[tuple[int, int]] = []
    for vertex_index, point in enumerate(positions):
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
    pairs = _body_collision_candidates(positions, body)
    if not len(pairs):
        return pairs
    return pairs[locked[pairs[:, 0]] == 0]


def _ensure_taichi():
    global _taichi, _runtime_type
    if _taichi is not None:
        return _taichi, _runtime_type
    try:
        import taichi as ti
    except ImportError as exc:
        raise KitsukeError(
            "Taichi is not installed for this Blender build. Reinstall the complete Yohsai package with its platform wheel."
        ) from exc
    try:
        ti.init(arch=ti.gpu, default_fp=ti.f32, fast_math=True, offline_cache=True)
    except Exception:
        try:
            ti.reset()
            ti.init(arch=ti.cpu, default_fp=ti.f32, fast_math=True, offline_cache=True)
        except Exception as exc:
            raise KitsukeError(f"Taichi could not initialize an automatic GPU or CPU backend: {exc}") from exc
    _taichi = ti
    _runtime_type = _create_runtime_type(ti)
    return _taichi, _runtime_type


def _create_runtime_type(ti):
    @ti.data_oriented
    class _TaichiRuntime:
        def __init__(self, positions, velocities, seams, topology, body, locked):
            self.vertex_count = len(positions)
            self.edge_count = len(topology.edges)
            self.quad_count = len(topology.quads)
            self.bend_count = len(topology.bends)
            self.x = ti.Vector.field(3, dtype=ti.f32, shape=self.vertex_count)
            self.previous = ti.Vector.field(3, dtype=ti.f32, shape=self.vertex_count)
            self.velocity = ti.Vector.field(3, dtype=ti.f32, shape=self.vertex_count)
            self.locked = ti.field(dtype=ti.i32, shape=self.vertex_count)
            self.delta = ti.Vector.field(3, dtype=ti.f32, shape=self.vertex_count)
            self.count = ti.field(dtype=ti.i32, shape=self.vertex_count)
            self.seams = ti.Vector.field(2, dtype=ti.i32, shape=len(seams))
            self.seam_rest = ti.field(dtype=ti.f32, shape=len(seams))
            self.seam_captured = ti.field(dtype=ti.i32, shape=len(seams))
            self.edges = ti.Vector.field(2, dtype=ti.i32, shape=max(1, self.edge_count))
            self.edge_rest = ti.field(dtype=ti.f32, shape=max(1, self.edge_count))
            self.quads = ti.Vector.field(4, dtype=ti.i32, shape=max(1, self.quad_count))
            self.quad_rest = ti.Vector.field(3, dtype=ti.f32, shape=max(1, self.quad_count))
            self.bends = ti.Vector.field(3, dtype=ti.i32, shape=max(1, self.bend_count))
            self.bend_rest = ti.Vector.field(2, dtype=ti.f32, shape=max(1, self.bend_count))
            self.body_x = ti.Vector.field(3, dtype=ti.f32, shape=len(body.vertices))
            self.body_faces = ti.Vector.field(3, dtype=ti.i32, shape=len(body.faces))
            self.x.from_numpy(positions)
            self.velocity.from_numpy(velocities)
            self.locked.from_numpy(locked)
            self.seams.from_numpy(seams)
            seam_rest = np.zeros(len(seams), dtype=np.float32)
            self.seam_rest.from_numpy(seam_rest)
            seam_distances = np.linalg.norm(
                positions[seams[:, 1]] - positions[seams[:, 0]], axis=1
            )
            self.seam_captured.from_numpy(
                (seam_distances <= SEAM_CAPTURE_DISTANCE_M).astype(np.int32)
            )
            if self.edge_count:
                self.edges.from_numpy(topology.edges)
                self.edge_rest.from_numpy(topology.edge_rest_lengths)
            if self.quad_count:
                self.quads.from_numpy(topology.quads)
                self.quad_rest.from_numpy(topology.quad_rest_metrics)
            if self.bend_count:
                self.bends.from_numpy(topology.bends)
                self.bend_rest.from_numpy(topology.bend_rest_lengths)
            self.body_x.from_numpy(body.vertices)
            self.body_faces.from_numpy(body.faces)

        @ti.kernel
        def replace_state(
            self,
            positions: ti.types.ndarray(dtype=ti.f32, ndim=2),
            velocities: ti.types.ndarray(dtype=ti.f32, ndim=2),
            locked: ti.types.ndarray(dtype=ti.i32, ndim=1),
        ):
            for index in range(self.vertex_count):
                self.x[index] = ti.Vector([positions[index, 0], positions[index, 1], positions[index, 2]])
                self.velocity[index] = ti.Vector([velocities[index, 0], velocities[index, 1], velocities[index, 2]])
                self.locked[index] = locked[index]

        @ti.kernel
        def integrate(self, dt: ti.f32, gravity_magnitude: ti.f32):
            gravity = ti.Vector([0.0, 0.0, -gravity_magnitude])
            for index in range(self.vertex_count):
                self.previous[index] = self.x[index]
                if self.locked[index] == 0:
                    self.velocity[index] += gravity * dt
                    self.x[index] += self.velocity[index] * dt
                else:
                    self.velocity[index] = ti.Vector.zero(ti.f32, 3)

        @ti.kernel
        def clear_corrections(self):
            for index in range(self.vertex_count):
                self.delta[index] = ti.Vector.zero(ti.f32, 3)
                self.count[index] = 0

        @ti.func
        def add_distance_correction(self, a: ti.i32, b: ti.i32, target_length: ti.f32, relaxation: ti.f32):
            difference = self.x[b] - self.x[a]
            length = difference.norm()
            if length > 1.0e-8:
                free_a = 1 if self.locked[a] == 0 else 0
                free_b = 1 if self.locked[b] == 0 else 0
                weight = free_a + free_b
                correction = difference * (relaxation * (length - target_length) / (length * ti.max(1, weight)))
                if free_a:
                    for axis in ti.static(range(3)):
                        ti.atomic_add(self.delta[a][axis], correction[axis])
                    ti.atomic_add(self.count[a], 1)
                if free_b:
                    for axis in ti.static(range(3)):
                        ti.atomic_add(self.delta[b][axis], -correction[axis])
                    ti.atomic_add(self.count[b], 1)

        @ti.kernel
        def seam_corrections(self):
            for index in self.seams:
                if self.seam_captured[index] != 0:
                    pair = self.seams[index]
                    self.add_distance_correction(pair[0], pair[1], self.seam_rest[index], 1.0)

        @ti.kernel
        def apply_seam_attraction(self, dt: ti.f32):
            for index in self.seams:
                if self.seam_captured[index] == 0:
                    pair = self.seams[index]
                    difference = self.x[pair[1]] - self.x[pair[0]]
                    length = difference.norm()
                    if length > 1.0e-8:
                        impulse = difference * (SEAM_ATTRACTION_FORCE * dt / length)
                        if self.locked[pair[0]] == 0:
                            for axis in ti.static(range(3)):
                                ti.atomic_add(self.velocity[pair[0]][axis], impulse[axis])
                        if self.locked[pair[1]] == 0:
                            for axis in ti.static(range(3)):
                                ti.atomic_add(self.velocity[pair[1]][axis], -impulse[axis])

        @ti.kernel
        def update_seam_capture(self):
            for index in self.seams:
                if self.seam_captured[index] == 0:
                    pair = self.seams[index]
                    current = self.x[pair[1]] - self.x[pair[0]]
                    previous = self.previous[pair[1]] - self.previous[pair[0]]
                    if current.norm() <= SEAM_CAPTURE_DISTANCE_M or current.dot(previous) <= 0.0:
                        self.seam_captured[index] = 1

        @ti.kernel
        def stretch_corrections(self):
            for index in range(self.edge_count):
                edge = self.edges[index]
                self.add_distance_correction(edge[0], edge[1], self.edge_rest[index], STRETCH_RELAXATION)

        @ti.kernel
        def shear_corrections(self):
            for index in range(self.quad_count):
                quad = self.quads[index]
                x0 = self.x[quad[0]]
                x1 = self.x[quad[1]]
                x2 = self.x[quad[2]]
                x3 = self.x[quad[3]]
                u = 0.5 * ((x1 - x0) + (x2 - x3))
                v = 0.5 * ((x3 - x0) + (x2 - x1))
                value = u.dot(v) - self.quad_rest[index][2]
                gradients = ti.Matrix.rows([
                    -0.5 * (u + v),
                    0.5 * (v - u),
                    0.5 * (u + v),
                    0.5 * (u - v),
                ])
                denominator = 0.0
                for corner in ti.static(range(4)):
                    if self.locked[quad[corner]] == 0:
                        denominator += gradients[corner, 0] ** 2 + gradients[corner, 1] ** 2 + gradients[corner, 2] ** 2
                if denominator > 1.0e-16:
                    multiplier = -SHEAR_RELAXATION * value / denominator
                    for corner in ti.static(range(4)):
                        vertex = quad[corner]
                        if self.locked[vertex] == 0:
                            for axis in ti.static(range(3)):
                                ti.atomic_add(self.delta[vertex][axis], multiplier * gradients[corner, axis])
                            ti.atomic_add(self.count[vertex], 1)

        @ti.kernel
        def bend_corrections(self):
            for index in range(self.bend_count):
                bend = self.bends[index]
                rest = self.bend_rest[index]
                coefficients = ti.Vector([
                    1.0 / rest[0],
                    -(1.0 / rest[0] + 1.0 / rest[1]),
                    1.0 / rest[1],
                ])
                curvature = (
                    coefficients[0] * self.x[bend[0]]
                    + coefficients[1] * self.x[bend[1]]
                    + coefficients[2] * self.x[bend[2]]
                )
                denominator = 0.0
                for point in ti.static(range(3)):
                    if self.locked[bend[point]] == 0:
                        denominator += coefficients[point] ** 2
                if denominator > 1.0e-8:
                    for point in ti.static(range(3)):
                        vertex = bend[point]
                        if self.locked[vertex] == 0:
                            scale = -BEND_RELAXATION * coefficients[point] / denominator
                            for axis in ti.static(range(3)):
                                ti.atomic_add(self.delta[vertex][axis], scale * curvature[axis])
                            ti.atomic_add(self.count[vertex], 1)

        @ti.kernel
        def replace_seam_state(self, values: ti.types.ndarray(dtype=ti.f32, ndim=1)):
            for index in self.seams:
                self.seam_rest[index] = values[index]

        @ti.kernel
        def apply_corrections(self):
            for index in range(self.vertex_count):
                if self.locked[index] == 0 and self.count[index] > 0:
                    self.x[index] += self.delta[index] / ti.cast(self.count[index], ti.f32)

        @ti.func
        def closest_triangle_point(self, point, a, b, c):
            ab = b - a
            ac = c - a
            ap = point - a
            d1 = ab.dot(ap)
            d2 = ac.dot(ap)
            result = a
            if d1 <= 0.0 and d2 <= 0.0:
                result = a
            else:
                bp = point - b
                d3 = ab.dot(bp)
                d4 = ac.dot(bp)
                if d3 >= 0.0 and d4 <= d3:
                    result = b
                else:
                    vc = d1 * d4 - d3 * d2
                    if vc <= 0.0 and d1 >= 0.0 and d3 <= 0.0:
                        v = d1 / (d1 - d3)
                        result = a + v * ab
                    else:
                        cp = point - c
                        d5 = ab.dot(cp)
                        d6 = ac.dot(cp)
                        if d6 >= 0.0 and d5 <= d6:
                            result = c
                        else:
                            vb = d5 * d2 - d1 * d6
                            if vb <= 0.0 and d2 >= 0.0 and d6 <= 0.0:
                                w = d2 / (d2 - d6)
                                result = a + w * ac
                            else:
                                va = d3 * d6 - d5 * d4
                                if va <= 0.0 and (d4 - d3) >= 0.0 and (d5 - d6) >= 0.0:
                                    w = (d4 - d3) / ((d4 - d3) + (d5 - d6))
                                    result = b + w * (c - b)
                                else:
                                    denominator = 1.0 / (va + vb + vc)
                                    v = vb * denominator
                                    w = vc * denominator
                                    result = a + ab * v + ac * w
            return result

        @ti.kernel
        def body_collisions(self, candidates: ti.types.ndarray(dtype=ti.i32, ndim=2), candidate_count: ti.i32):
            for pair_index in range(candidate_count):
                vertex = candidates[pair_index, 0]
                triangle = self.body_faces[candidates[pair_index, 1]]
                a = self.body_x[triangle[0]]
                b = self.body_x[triangle[1]]
                c = self.body_x[triangle[2]]
                normal = (b - a).cross(c - a).normalized(1.0e-8)
                closest = self.closest_triangle_point(self.x[vertex], a, b, c)
                separation = self.x[vertex] - closest
                distance = separation.norm()
                signed_distance = separation.dot(normal)
                if distance < CONTACT_THICKNESS_M * 2.0 and signed_distance < CONTACT_THICKNESS_M:
                    correction = normal * (CONTACT_THICKNESS_M - signed_distance)
                    correction_length = correction.norm()
                    if correction_length > CONTACT_CORRECTION_MAX_M:
                        correction *= CONTACT_CORRECTION_MAX_M / correction_length
                    for axis in ti.static(range(3)):
                        ti.atomic_add(self.delta[vertex][axis], correction[axis])
                    ti.atomic_add(self.count[vertex], 1)

        @ti.kernel
        def update_velocities(self, dt: ti.f32):
            for index in range(self.vertex_count):
                if self.locked[index] == 0:
                    self.velocity[index] = (self.x[index] - self.previous[index]) / dt
                else:
                    self.velocity[index] = ti.Vector.zero(ti.f32, 3)

        def advance(self, body_candidates, gravity_magnitude, solver_iterations):
            for _step in range(STEPS_PER_CLICK):
                self.apply_seam_attraction(TIME_STEP)
                self.integrate(TIME_STEP, gravity_magnitude)
                self.update_seam_capture()
                for _iteration in range(solver_iterations):
                    self.update_seam_capture()
                    self.clear_corrections()
                    self.seam_corrections()
                    self.apply_corrections()
                    self.clear_corrections()
                    self.shear_corrections()
                    self.bend_corrections()
                    self.apply_corrections()
                    self.clear_corrections()
                    self.stretch_corrections()
                    self.apply_corrections()
                    self.clear_corrections()
                    self.stretch_corrections()
                    self.apply_corrections()
                    self.clear_corrections()
                    self.stretch_corrections()
                    self.apply_corrections()
                    self.clear_corrections()
                    self.stretch_corrections()
                    self.apply_corrections()
                    self.clear_corrections()
                    self.seam_corrections()
                    self.apply_corrections()
                    self.clear_corrections()
                    if len(body_candidates):
                        self.body_collisions(body_candidates, len(body_candidates))
                    self.apply_corrections()
                for _cleanup in range(solver_iterations * 4):
                    self.clear_corrections()
                    self.stretch_corrections()
                    self.apply_corrections()
                    self.clear_corrections()
                    self.seam_corrections()
                    self.apply_corrections()
                self.clear_corrections()
                if len(body_candidates):
                    self.body_collisions(body_candidates, len(body_candidates))
                self.apply_corrections()
                self.update_velocities(TIME_STEP)

        def state(self):
            return self.x.to_numpy(), self.velocity.to_numpy()

        def seam_state(self):
            return self.seam_rest.to_numpy()

    return _TaichiRuntime


class _KitsukeSession:
    def __init__(self, context, collection, body, preview, backend: str):
        objects = _parts(collection)
        if len(objects) < 2:
            raise KitsukeError("Kitsuke needs at least two cloth objects.")
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
            if backend == KITSUKE_BACKEND_STABLE_COSSERAT:
                self.runtime = NativeCosseratRuntime(
                    self.positions,
                    self.velocities,
                    self.seams,
                    self.topology,
                    self.body,
                    self.locked,
                )
            else:
                _ti, runtime_type = _ensure_taichi()
                self.runtime = runtime_type(
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
            for vertex, world_position in zip(obj.data.vertices, selection):
                vertex.co = inverse @ Vector(tuple(float(value) for value in world_position))
            obj.data.update()
            self.positions[part.start : part.start + part.count] = _world_vertices(obj)
            obj.hide_set(False)
            obj.hide_render = False
        if self.backend == KITSUKE_BACKEND_STABLE_COSSERAT:
            # Blender mesh coordinates are the undoable authority. Keep the
            # live native state on their exact float32 round trip so the next
            # click and an Undo-reconstructed repeat start bit-for-bit alike.
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

    def advance(self, context, gravity_magnitude: float, solver_iterations: int):
        self._read_user_transforms()
        # The legacy backend retains a one-shot correction for vertices already
        # inside Body; the native backend uses only its incremental contact path.
        if (
            self.backend != KITSUKE_BACKEND_STABLE_COSSERAT
            and _project_body_penetrations(self.body, self.positions, self.velocities, self.locked)
        ):
            self.runtime.replace_state(self.positions, self.velocities, self.locked)
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
            native_detail = ""
            if self.backend == KITSUKE_BACKEND_STABLE_COSSERAT:
                stats = self.runtime.last_stats
                native_detail = (
                    f"material {int(stats.get('edge_count', 0))} edges/"
                    f"{int(stats.get('quad_count', 0))} quads/"
                    f"{int(stats.get('bend_count', 0))} bends, "
                    f"Body candidates {int(stats.get('body_candidate_count', 0))}"
                )
            detail = f"; {native_detail}" if native_detail else ""
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
    if backend == KITSUKE_BACKEND_STABLE_COSSERAT:
        return (
            f"Kitsuke: square-lattice cloth + constant-force seams, {STEPS_PER_CLICK} steps; "
            f"material/contact {solver_iterations} iterations; gravity -Z {gravity_magnitude:.3g} m/s²"
        )
    ti, _runtime = _ensure_taichi()
    arch = str(ti.lang.impl.current_cfg().arch).split(".")[-1]
    return (
        f"Kitsuke: {STEPS_PER_CLICK} steps x {solver_iterations} iterations on {arch}; "
        f"gravity -Z {gravity_magnitude:.3g} m/s², constant-force seams"
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


def has_kitsuke_session(collection: bpy.types.Collection | None) -> bool:
    return collection is not None and (
        collection.as_pointer() in _sessions or _persisted_state_is_current(collection)
    )
