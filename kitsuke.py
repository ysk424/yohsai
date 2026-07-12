# SPDX-License-Identifier: GPL-3.0-or-later
"""Incremental, transient cloth simulation for the Yohsai Kitsuke workflow."""

from dataclasses import dataclass
from uuid import uuid4

import bpy
import numpy as np
from mathutils import Matrix, Vector
from mathutils.bvhtree import BVHTree

from .mesh_loader import SewingError, build_sewing_plan


TIME_STEP = 1.0 / 240.0
STEPS_PER_CLICK = 16
SOLVER_ITERATIONS = 1
SEAM_PROJECTION_PASSES = 4
CONTACT_THICKNESS_M = 0.002
COLLISION_SEARCH_M = 0.04
DEFAULT_SEAM_CLOSURE_PER_CLICK_M = 0.030
VELOCITY_DAMPING_PER_SECOND = 4.0
MAX_SPEED_M_PER_SECOND = 1.0
MAX_CONSTRAINT_CORRECTION_M = 0.010
MAX_DISPLACEMENT_PER_CLICK_M = 0.1
DEFAULT_GRAVITY_M_PER_SECOND_SQUARED = 1.0

_STATE_EPOCH_KEY = "yohsai_kitsuke_epoch"
_STATE_REVISION_KEY = "yohsai_kitsuke_revision"
_STATE_SEAMS_KEY = "yohsai_kitsuke_seams"
_STATE_SEAM_REST_KEY = "yohsai_kitsuke_seam_rest"
_STATE_MATRIX_KEY = "yohsai_kitsuke_matrix"
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
    locked: bool


@dataclass(frozen=True)
class _BodySnapshot:
    vertices: np.ndarray
    faces: np.ndarray
    bvh: BVHTree
    ray_distance: float


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
) -> tuple[int, np.ndarray, np.ndarray] | None:
    if not _persisted_state_is_current(collection):
        return None
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
    for key in (_STATE_EPOCH_KEY, _STATE_REVISION_KEY, _STATE_SEAMS_KEY, _STATE_SEAM_REST_KEY):
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


def _triangles(mesh: bpy.types.Mesh, offset: int = 0) -> list[tuple[int, int, int]]:
    mesh.calc_loop_triangles()
    return [tuple(vertex + offset for vertex in triangle.vertices) for triangle in mesh.loop_triangles]


def _world_vertices(obj: bpy.types.Object) -> np.ndarray:
    matrix = obj.matrix_world
    return np.asarray([tuple(matrix @ vertex.co) for vertex in obj.data.vertices], dtype=np.float32)


def _pattern_rest_vertices(obj: bpy.types.Object) -> np.ndarray:
    attribute = obj.data.attributes.get("yohsai_pattern_position")
    if attribute is None or attribute.domain != "POINT" or len(attribute.data) != len(obj.data.vertices):
        raise KitsukeError(f"{obj.name} has no authoritative flat-pattern coordinates. Load the pattern again.")
    return np.asarray([(item.vector[0], 0.0, item.vector[1]) for item in attribute.data], dtype=np.float32)


def _edge_constraints(parts: list[_PartRange], rest_positions: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    edges: list[tuple[int, int]] = []
    for part in parts:
        edges.extend(
            (part.start + edge.vertices[0], part.start + edge.vertices[1])
            for edge in part.obj.data.edges
        )
    indices = np.asarray(edges, dtype=np.int32).reshape((-1, 2))
    rest = np.linalg.norm(rest_positions[indices[:, 0]] - rest_positions[indices[:, 1]], axis=1).astype(np.float32)
    return indices, rest


def _bending_constraints(parts: list[_PartRange], rest_positions: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    pairs: list[tuple[int, int]] = []
    for part in parts:
        mesh = part.obj.data
        mesh.calc_loop_triangles()
        edge_opposites: dict[tuple[int, int], list[int]] = {}
        for triangle in mesh.loop_triangles:
            a, b, c = triangle.vertices
            for edge, opposite in (((a, b), c), ((b, c), a), ((c, a), b)):
                edge_opposites.setdefault(tuple(sorted(edge)), []).append(opposite)
        for opposites in edge_opposites.values():
            if len(opposites) == 2 and opposites[0] != opposites[1]:
                pairs.append((part.start + opposites[0], part.start + opposites[1]))
    indices = np.asarray(pairs, dtype=np.int32).reshape((-1, 2))
    if not len(indices):
        return indices, np.empty(0, dtype=np.float32)
    rest = np.linalg.norm(rest_positions[indices[:, 0]] - rest_positions[indices[:, 1]], axis=1).astype(np.float32)
    return indices, rest


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


def _triangle_grid(
    vertices: np.ndarray,
    faces: np.ndarray,
    cell_size: float,
    padding: float,
) -> dict[tuple[int, int, int], list[int]]:
    grid: dict[tuple[int, int, int], list[int]] = {}
    for face_index, face in enumerate(faces):
        points = vertices[face]
        lower = np.floor((points.min(axis=0) - padding) / cell_size).astype(np.int32)
        upper = np.floor((points.max(axis=0) + padding) / cell_size).astype(np.int32)
        for x in range(int(lower[0]), int(upper[0]) + 1):
            for y in range(int(lower[1]), int(upper[1]) + 1):
                for z in range(int(lower[2]), int(upper[2]) + 1):
                    grid.setdefault((x, y, z), []).append(face_index)
    return grid


def _collision_candidates(
    query_vertices: np.ndarray,
    triangle_vertices: np.ndarray,
    faces: np.ndarray,
    exclusions: dict[int, set[int]] | None = None,
) -> np.ndarray:
    cell_size = COLLISION_SEARCH_M
    grid = _triangle_grid(triangle_vertices, faces, cell_size, COLLISION_SEARCH_M)
    pairs: list[tuple[int, int]] = []
    for vertex_index, point in enumerate(query_vertices):
        cell = tuple(np.floor(point / cell_size).astype(np.int32))
        excluded = exclusions.get(vertex_index, set()) if exclusions else set()
        for face_index in grid.get(cell, ()):
            if face_index not in excluded:
                pairs.append((vertex_index, face_index))
    if not pairs:
        return np.empty((0, 2), dtype=np.int32)
    return np.asarray(pairs, dtype=np.int32)


def _body_collision_candidates(positions: np.ndarray, body: _BodySnapshot) -> np.ndarray:
    """Return only the nearest Body triangle for each nearby cloth vertex."""
    pairs: list[tuple[int, int]] = []
    for vertex_index, point in enumerate(positions):
        _location, _normal, face_index, _distance = body.bvh.find_nearest(
            Vector(tuple(float(value) for value in point)), COLLISION_SEARCH_M
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


def _self_exclusions(
    vertex_count: int,
    faces: np.ndarray,
    edges: np.ndarray,
    seams: np.ndarray,
) -> dict[int, set[int]]:
    neighbors = [set((index,)) for index in range(vertex_count)]
    for a, b in edges:
        neighbors[int(a)].add(int(b))
        neighbors[int(b)].add(int(a))
    for a, b in seams:
        neighbors[int(a)].update((int(b),))
        neighbors[int(b)].update((int(a),))
    vertex_faces: list[set[int]] = [set() for _ in range(vertex_count)]
    for face_index, face in enumerate(faces):
        for vertex in face:
            vertex_faces[int(vertex)].add(face_index)
    exclusions: dict[int, set[int]] = {}
    for vertex in range(vertex_count):
        excluded: set[int] = set()
        for neighbor in neighbors[vertex]:
            excluded.update(vertex_faces[neighbor])
        exclusions[vertex] = excluded
    return exclusions


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
        def __init__(self, positions, velocities, edges, edge_rest, bends, bend_rest, seams, faces, body, locked):
            self.vertex_count = len(positions)
            self.x = ti.Vector.field(3, dtype=ti.f32, shape=self.vertex_count)
            self.previous = ti.Vector.field(3, dtype=ti.f32, shape=self.vertex_count)
            self.velocity = ti.Vector.field(3, dtype=ti.f32, shape=self.vertex_count)
            self.locked = ti.field(dtype=ti.i32, shape=self.vertex_count)
            self.delta = ti.Vector.field(3, dtype=ti.f32, shape=self.vertex_count)
            self.count = ti.field(dtype=ti.i32, shape=self.vertex_count)
            self.seam_delta = ti.Vector.field(3, dtype=ti.f32, shape=self.vertex_count)
            self.seam_count = ti.field(dtype=ti.i32, shape=self.vertex_count)
            self.edges = ti.Vector.field(2, dtype=ti.i32, shape=len(edges))
            self.edge_rest = ti.field(dtype=ti.f32, shape=len(edges))
            self.bends = ti.Vector.field(2, dtype=ti.i32, shape=max(len(bends), 1))
            self.bend_rest = ti.field(dtype=ti.f32, shape=max(len(bends), 1))
            self.bend_count = len(bends)
            self.seams = ti.Vector.field(2, dtype=ti.i32, shape=len(seams))
            self.seam_rest = ti.field(dtype=ti.f32, shape=len(seams))
            self.faces = ti.Vector.field(3, dtype=ti.i32, shape=len(faces))
            self.body_x = ti.Vector.field(3, dtype=ti.f32, shape=len(body.vertices))
            self.body_faces = ti.Vector.field(3, dtype=ti.i32, shape=len(body.faces))
            self.x.from_numpy(positions)
            self.velocity.from_numpy(velocities)
            self.locked.from_numpy(locked)
            self.edges.from_numpy(edges)
            self.edge_rest.from_numpy(edge_rest)
            if len(bends):
                self.bends.from_numpy(bends)
                self.bend_rest.from_numpy(bend_rest)
            self.seams.from_numpy(seams)
            seam_rest = np.linalg.norm(positions[seams[:, 0]] - positions[seams[:, 1]], axis=1).astype(np.float32)
            self.seam_rest.from_numpy(seam_rest)
            self.faces.from_numpy(faces)
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

        @ti.kernel
        def clear_seam_projection(self):
            for index in range(self.vertex_count):
                self.seam_delta[index] = ti.Vector.zero(ti.f32, 3)
                self.seam_count[index] = 0

        @ti.func
        def add_distance(self, a: ti.i32, b: ti.i32, rest: ti.f32, stiffness: ti.f32):
            difference = self.x[b] - self.x[a]
            length = difference.norm()
            if length > 1.0e-8:
                magnitude = (length - rest) * (0.5 * stiffness)
                magnitude = ti.max(-MAX_CONSTRAINT_CORRECTION_M, ti.min(MAX_CONSTRAINT_CORRECTION_M, magnitude))
                correction = difference * (magnitude / length)
                for axis in ti.static(range(3)):
                    ti.atomic_add(self.delta[a][axis], correction[axis])
                    ti.atomic_add(self.delta[b][axis], -correction[axis])
                ti.atomic_add(self.count[a], 1)
                ti.atomic_add(self.count[b], 1)

        @ti.func
        def add_max_distance(self, a: ti.i32, b: ti.i32, max_length: ti.f32, stiffness: ti.f32):
            difference = self.x[b] - self.x[a]
            length = difference.norm()
            if length > max_length and length > 1.0e-8:
                magnitude = (length - max_length) * (0.5 * stiffness)
                magnitude = ti.min(MAX_CONSTRAINT_CORRECTION_M, magnitude)
                correction = difference * (magnitude / length)
                for axis in ti.static(range(3)):
                    ti.atomic_add(self.delta[a][axis], correction[axis])
                    ti.atomic_add(self.delta[b][axis], -correction[axis])
                ti.atomic_add(self.count[a], 1)
                ti.atomic_add(self.count[b], 1)

        @ti.func
        def add_seam_projection(self, a: ti.i32, b: ti.i32, max_length: ti.f32):
            difference = self.x[b] - self.x[a]
            length = difference.norm()
            if length > max_length and length > 1.0e-8:
                unlocked = (1 - self.locked[a]) + (1 - self.locked[b])
                if unlocked > 0:
                    share = 1.0 / ti.cast(unlocked, ti.f32)
                    correction = difference * (((length - max_length) * share) / length)
                    if self.locked[a] == 0:
                        for axis in ti.static(range(3)):
                            ti.atomic_add(self.seam_delta[a][axis], correction[axis])
                        ti.atomic_add(self.seam_count[a], 1)
                    if self.locked[b] == 0:
                        for axis in ti.static(range(3)):
                            ti.atomic_add(self.seam_delta[b][axis], -correction[axis])
                        ti.atomic_add(self.seam_count[b], 1)

        @ti.kernel
        def distance_corrections(self):
            for index in self.edges:
                pair = self.edges[index]
                self.add_distance(pair[0], pair[1], self.edge_rest[index], 0.95)
            for index in range(self.bend_count):
                pair = self.bends[index]
                self.add_distance(pair[0], pair[1], self.bend_rest[index], 0.08)
            for index in self.seams:
                pair = self.seams[index]
                self.add_max_distance(pair[0], pair[1], self.seam_rest[index], 0.85)

        @ti.kernel
        def seam_projection_corrections(self):
            for index in self.seams:
                pair = self.seams[index]
                self.add_seam_projection(pair[0], pair[1], self.seam_rest[index])

        @ti.kernel
        def tighten_seams(self, amount: ti.f32):
            for index in self.seams:
                self.seam_rest[index] = ti.max(0.0, self.seam_rest[index] - amount)

        @ti.kernel
        def ratchet_seams(self):
            for index in self.seams:
                pair = self.seams[index]
                difference = self.x[pair[1]] - self.x[pair[0]]
                self.seam_rest[index] = ti.min(self.seam_rest[index], difference.norm())

        @ti.kernel
        def replace_seam_state(self, values: ti.types.ndarray(dtype=ti.f32, ndim=1)):
            for index in self.seams:
                self.seam_rest[index] = values[index]

        @ti.kernel
        def apply_corrections(self):
            for index in range(self.vertex_count):
                if self.locked[index] == 0 and self.count[index] > 0:
                    self.x[index] += self.delta[index] / ti.cast(self.count[index], ti.f32)

        @ti.kernel
        def apply_seam_projection(self):
            for index in range(self.vertex_count):
                if self.locked[index] == 0 and self.seam_count[index] > 0:
                    self.x[index] += self.seam_delta[index] / ti.cast(self.seam_count[index], ti.f32)

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
                    for axis in ti.static(range(3)):
                        ti.atomic_add(self.delta[vertex][axis], correction[axis])
                    ti.atomic_add(self.count[vertex], 1)

        @ti.kernel
        def self_collisions(self, candidates: ti.types.ndarray(dtype=ti.i32, ndim=2), candidate_count: ti.i32):
            for pair_index in range(candidate_count):
                vertex = candidates[pair_index, 0]
                triangle = self.faces[candidates[pair_index, 1]]
                a = self.x[triangle[0]]
                b = self.x[triangle[1]]
                c = self.x[triangle[2]]
                closest = self.closest_triangle_point(self.x[vertex], a, b, c)
                separation = self.x[vertex] - closest
                distance = separation.norm()
                if distance < CONTACT_THICKNESS_M:
                    normal = (b - a).cross(c - a).normalized(1.0e-8)
                    direction = normal
                    if distance > 1.0e-8:
                        direction = separation / distance
                    correction = direction * (CONTACT_THICKNESS_M - distance)
                    for axis in ti.static(range(3)):
                        ti.atomic_add(self.delta[vertex][axis], correction[axis])
                    ti.atomic_add(self.count[vertex], 1)

        @ti.kernel
        def update_velocities(self, dt: ti.f32):
            for index in range(self.vertex_count):
                if self.locked[index] == 0:
                    velocity = (self.x[index] - self.previous[index]) / dt
                    speed = velocity.norm()
                    if speed > MAX_SPEED_M_PER_SECOND:
                        velocity *= MAX_SPEED_M_PER_SECOND / speed
                    self.velocity[index] = velocity * ti.exp(-VELOCITY_DAMPING_PER_SECOND * dt)
                else:
                    self.velocity[index] = ti.Vector.zero(ti.f32, 3)

        def advance(self, body_candidates, self_candidates, gravity_magnitude, seam_closure):
            self.tighten_seams(seam_closure)
            self.ratchet_seams()
            for _step in range(STEPS_PER_CLICK):
                self.integrate(TIME_STEP, gravity_magnitude)
                self.ratchet_seams()
                for _iteration in range(SOLVER_ITERATIONS):
                    self.clear_corrections()
                    self.distance_corrections()
                    self.apply_corrections()
                    self.clear_corrections()
                    if len(body_candidates):
                        self.body_collisions(body_candidates, len(body_candidates))
                    if len(self_candidates):
                        self.self_collisions(self_candidates, len(self_candidates))
                    self.apply_corrections()
                    for _pass in range(SEAM_PROJECTION_PASSES):
                        self.clear_seam_projection()
                        self.seam_projection_corrections()
                        self.apply_seam_projection()
                    self.ratchet_seams()
                self.update_velocities(TIME_STEP)

        def state(self):
            return self.x.to_numpy(), self.velocity.to_numpy()

        def seam_state(self):
            return self.seam_rest.to_numpy()

    return _TaichiRuntime


class _KitsukeSession:
    def __init__(self, context, collection, body, preview):
        objects = _parts(collection)
        if len(objects) < 2:
            raise KitsukeError("Kitsuke needs at least two cloth objects.")
        ranges: list[_PartRange] = []
        position_blocks: list[np.ndarray] = []
        rest_blocks: list[np.ndarray] = []
        faces: list[tuple[int, int, int]] = []
        locked_blocks: list[np.ndarray] = []
        offset = 0
        for obj in objects:
            if any(abs(float(scale) - 1.0) > 1.0e-5 for scale in obj.scale):
                raise KitsukeError(f"Apply Scale on {obj.name} before Kitsuke; moving and rotating are supported, scaling is not.")
            block = _world_vertices(obj)
            locked = bool(obj.get(LOCKED_OBJECT_KEY, False))
            ranges.append(_PartRange(obj, offset, len(block), locked))
            position_blocks.append(block)
            rest_blocks.append(_pattern_rest_vertices(obj))
            locked_blocks.append(np.full(len(block), 1 if locked else 0, dtype=np.int32))
            faces.extend(_triangles(obj.data, offset))
            offset += len(block)
        self.collection = collection
        self.parts = ranges
        self.positions = np.concatenate(position_blocks).astype(np.float32)
        self.locked = np.concatenate(locked_blocks).astype(np.int32)
        rest_positions = np.concatenate(rest_blocks).astype(np.float32)
        self.faces = np.asarray(faces, dtype=np.int32).reshape((-1, 3))
        self.edges, self.edge_rest = _edge_constraints(ranges, rest_positions)
        self.bends, self.bend_rest = _bending_constraints(ranges, rest_positions)
        if preview is not None:
            self.seams = _seam_constraints(preview, ranges)
        elif _persisted_state_is_current(collection):
            self.seams = _read_persisted_seams(collection, len(self.positions))
        else:
            self.seams = _seam_constraints_from_parts(collection, ranges)
        persisted = None if preview is not None else _read_persisted_state(collection, ranges, len(self.seams))
        if persisted is None:
            self.revision = 0
            self.velocities = np.zeros_like(self.positions)
        else:
            self.revision, persisted_seams, self.velocities = persisted
        self.body = _body_snapshot(context, body)
        self.body_pointer = body.as_pointer()
        self.matrices = {part.obj.name: _matrix_tuple(part.obj.matrix_world) for part in ranges}
        self.preview = preview
        _ti, runtime_type = _ensure_taichi()
        self.runtime = runtime_type(
            self.positions,
            self.velocities,
            self.edges,
            self.edge_rest,
            self.bends,
            self.bend_rest,
            self.seams,
            self.faces,
            self.body,
            self.locked,
        )
        if persisted is not None:
            self.runtime.replace_seam_state(persisted_seams)
        exclusions = _self_exclusions(len(self.positions), self.faces, self.edges, self.seams)
        self.self_exclusions = exclusions

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
            if matrix != self.matrices[obj.name] or not np.allclose(
                block, self.positions[selection], rtol=0.0, atol=1.0e-6
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
            obj.hide_set(False)
            obj.hide_render = False
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

    def advance(self, context, gravity_magnitude: float, seam_closure: float):
        self._read_user_transforms()
        if _project_body_penetrations(self.body, self.positions, self.velocities, self.locked):
            self.runtime.replace_state(self.positions, self.velocities, self.locked)
        previous_positions = self.positions.copy()
        previous_velocities = self.velocities.copy()
        previous_seams = self.runtime.seam_state()
        body_candidates = _unlocked_body_collision_candidates(self.positions, self.body, self.locked)
        self_candidates = _collision_candidates(
            self.positions,
            self.positions,
            self.faces,
            self.self_exclusions,
        )
        self.runtime.advance(body_candidates, self_candidates, gravity_magnitude, seam_closure)
        positions, velocities = self.runtime.state()
        displacement = np.linalg.norm(positions - previous_positions, axis=1)
        maximum_displacement = float(displacement.max()) if len(displacement) else 0.0
        if (
            not np.all(np.isfinite(positions))
            or not np.all(np.isfinite(velocities))
            or maximum_displacement > MAX_DISPLACEMENT_PER_CLICK_M
        ):
            self.positions = previous_positions
            self.velocities = previous_velocities
            self.runtime.replace_state(self.positions, self.velocities, self.locked)
            self.runtime.replace_seam_state(previous_seams)
            raise KitsukeError(
                f"The simulation became unstable ({maximum_displacement:.3f} m maximum movement) "
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
    seam_closure: float,
) -> str:
    """Advance one fixed Kitsuke interval and restore the separate cloth objects."""
    if collection is None or collection.get("yohsai_role") != "clothes":
        raise KitsukeError("No loaded Yohsai clothes collection is selected.")
    key = collection.as_pointer()
    session = _sessions.get(key)
    preview = _sewn_preview(collection)
    if session is not None and preview is not None and session.preview is None:
        # Undoing the first Kitsuke restores the verified preview. Reconstruct
        # the transient runtime from that authoritative Blender state.
        session = None
        _sessions.pop(key, None)
    if session is None:
        session = _KitsukeSession(context, collection, body, preview)
        _sessions[key] = session
    elif body is None or body.as_pointer() != session.body_pointer:
        raise KitsukeError("The Body used by this Kitsuke session cannot be changed after its first step.")
    session.advance(context, gravity_magnitude, seam_closure)
    ti, _runtime = _ensure_taichi()
    arch = str(ti.lang.impl.current_cfg().arch).split(".")[-1]
    return (
        f"Kitsuke: {STEPS_PER_CLICK} steps on {arch}; "
        f"gravity {gravity_magnitude:.3g} m/s², seam {seam_closure * 1000.0:.3g} mm"
    )


def clear_sessions() -> None:
    _sessions.clear()


def reset_runtime_epoch() -> None:
    """Invalidate all live and saved recovery state before Blender loads a file."""
    global _RUNTIME_EPOCH
    clear_sessions()
    _RUNTIME_EPOCH = uuid4().hex


def clear_kitsuke_session(collection: bpy.types.Collection | None) -> None:
    if collection is not None:
        _sessions.pop(collection.as_pointer(), None)
        _clear_persisted_state(collection)


def has_kitsuke_session(collection: bpy.types.Collection | None) -> bool:
    return collection is not None and (
        collection.as_pointer() in _sessions or _persisted_state_is_current(collection)
    )
