# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared cloth state for the Yohsai solver hand-offs.

This module used to own a cloth simulation of its own: a square-lattice
solver advanced one click at a time, with a live session, persisted velocity
and seam state, and an Undo recovery epoch.  That solver is gone.  Zero
GRAVITY sews with the ZOZO Contact Solver in a single job, and the ZOZO
hand-off passes the garment on as it stands, so nothing needs cloth state to
survive between presses any more.

What is left is what both hand-offs still have to agree on: which panels take
part, where their vertices are in world space, which pairs of vertices are
sewn to each other, and what the Body looks like as a collider.  Reading any
of those two different ways is how two solvers end up sewing two different
garments, so they are read here once.
"""

from dataclasses import dataclass

import bpy
import numpy as np
from mathutils import Matrix

from .mesh_loader import (
    LOCKED_OBJECT_KEY,
    SewingError,
    build_sewing_plan,
    compute_seam_count_overrides,
    participating_parts,
    remesh_with_seam_counts,
)


class KitsukeError(RuntimeError):
    """The current Blender state cannot be handed to a solver."""


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
    bounds_minimum: np.ndarray
    bounds_maximum: np.ndarray


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


def part_ranges(collection: bpy.types.Collection, purpose: str) -> list[_PartRange]:
    """The panels a hand-off covers, with their place in the packed vertex block.

    Both hand-offs concatenate every participating panel into one array and
    index it by ``start``, so the order and the offsets have to come from one
    place; a panel numbered differently in two hand-offs is a seam sewn to the
    wrong vertex.
    """
    objects = list(participating_parts(collection))
    if len(objects) < 2:
        raise KitsukeError(f"{purpose} needs at least two pending or completed parts.")
    ranges: list[_PartRange] = []
    offset = 0
    for obj in objects:
        if any(abs(float(scale) - 1.0) > 1.0e-5 for scale in obj.scale):
            raise KitsukeError(
                f"Apply Scale on {obj.name} before {purpose}; "
                "moving and rotating are supported, scaling is not."
            )
        count = len(obj.data.vertices)
        locked = bool(obj.get(LOCKED_OBJECT_KEY, False))
        ranges.append(_PartRange(obj, offset, count, locked))
        offset += count
    return ranges


def _seam_constraints_from_parts(
    collection: bpy.types.Collection, part_ranges: list[_PartRange]
) -> np.ndarray:
    if not bool(collection.get("yohsai_sewing_verified", False)):
        raise KitsukeError("Automatic Sewing is required before a solver can run.")
    try:
        plan = build_sewing_plan(collection)
    except SewingError as exc:
        raise KitsukeError(f"Automatic Sewing failed: {exc}") from exc
    expected = [part.obj.name for part in part_ranges]
    if [part.name for part in plan.parts] != expected:
        raise KitsukeError(
            "Automatic Sewing failed: the verified panel set no longer matches the current objects."
        )
    return np.asarray([(a, b) for _label, a, b in plan.connections], dtype=np.int32).reshape((-1, 2))


def _body_snapshot(context, body: bpy.types.Object) -> _BodySnapshot:
    if body is None:
        raise KitsukeError("Select a mesh Body first.")
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
        # so Body contact cannot turn its push-out into an inward pull.
        if matrix.to_3x3().determinant() < 0.0:
            faces = faces[:, (0, 2, 1)]
    finally:
        evaluated.to_mesh_clear()
    if not len(vertices) or not len(faces):
        raise KitsukeError("Body has no triangles for collision detection.")
    return _BodySnapshot(
        vertices,
        faces,
        vertices.min(axis=0),
        vertices.max(axis=0),
    )


def adapt_seam_counts(context, collection: bpy.types.Collection | None) -> set[str]:
    """Equalize every sewing seam's two sides to matching vertex counts so they
    pair 1:1.

    Same-pitch seams keep the longer side (gather). Mixed 10 mm / 5 mm seams
    keep the coarser side. Only mismatched panels are recut; a no-op when
    already matched.
    """
    if collection is None:
        return set()
    overrides = compute_seam_count_overrides(collection)
    if not overrides:
        return set()
    return remesh_with_seam_counts(context, collection, overrides)
