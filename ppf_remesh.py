# SPDX-License-Identifier: GPL-3.0-or-later
"""Re-triangulate flat panels for the contact solver, keeping their outline.

Yohsai's panel triangulation comes out of a constrained Delaunay pass that
leaves needle slivers in the interior: measured on the reference garment,
the shortest interior edge is 0.169 um against a 10 mm median, and 269 of
13112 triangles have under a millionth of the median area.  The
square-lattice solver never notices, because it reads its material metric
from the authored pattern and skips degenerate edges outright.  An implicit
solver cannot: a shell element's Hessian scales with the inverse of its rest
area, so a sliver contributes a term of order 1e11, and the assembled matrix
comes back with a NaN that stops the very first PCG solve.

The outline is not the problem -- boundary edges run 766 um to 1.29 mm, and
the panels are planar to within measurement -- so only the interior needs
replacing.  Keeping every boundary vertex exactly is what makes that safe:
seams live entirely on the boundary, so they survive untouched, and the
garment's cut is preserved to the last vertex.

The original mesh is never modified.  Results come back through
:func:`transfer`, which reads each original vertex out of the clean mesh it
sits in, so Blender keeps its own topology, its pattern coordinates, and its
grainline attributes, and Normal GRAVITY still works on the same panels.

This module runs in the ZOZO tree's interpreter, alongside `ppf_driver`, and
may use scipy; it must never be imported from Blender.
"""

from __future__ import annotations

from collections import defaultdict

import numpy as np
from scipy.spatial import Delaunay


# A face smaller than this fraction of one lattice cell holds no cloth, so it
# is a triangulation artifact rather than geometry. Measured on the reference
# garment the survivors sit at 1e-2 of a cell and the artifact at 1e-16, so
# there is no judgement call in the gap between them.
_AREA_FLOOR = 1.0e-9


class RemeshError(RuntimeError):
    """A panel cannot be re-triangulated from its outline."""


def _components(vertex_count: int, faces: np.ndarray) -> list[np.ndarray]:
    """Vertex indices of each connected panel, in first-appearance order."""
    import scipy.sparse as sparse
    import scipy.sparse.csgraph as csgraph

    rows = np.concatenate([faces[:, 0], faces[:, 1], faces[:, 2]])
    cols = np.concatenate([faces[:, 1], faces[:, 2], faces[:, 0]])
    graph = sparse.coo_matrix(
        (np.ones(len(rows)), (rows, cols)), shape=(vertex_count, vertex_count)
    )
    count, label = csgraph.connected_components(graph, directed=False)
    return [np.flatnonzero(label == component) for component in range(count)]


def _boundary_loops(faces: np.ndarray) -> list[list[int]]:
    """Ordered vertex loops around the panel, outer first.

    A boundary edge belongs to exactly one triangle. Walking those edges in
    the direction their triangle gives them yields consistently oriented
    loops, so the even-odd test below can treat holes as holes.
    """
    used: dict[tuple[int, int], int] = defaultdict(int)
    directed: dict[int, int] = {}
    for a, b, c in faces:
        for start, end in ((a, b), (b, c), (c, a)):
            used[(min(start, end), max(start, end))] += 1
    for a, b, c in faces:
        for start, end in ((a, b), (b, c), (c, a)):
            if used[(min(start, end), max(start, end))] == 1:
                directed[int(start)] = int(end)

    loops: list[list[int]] = []
    remaining = dict(directed)
    while remaining:
        start = next(iter(remaining))
        loop = [start]
        node = remaining.pop(start)
        while node != start:
            loop.append(node)
            if node not in remaining:
                raise RemeshError("A panel outline does not close.")
            node = remaining.pop(node)
        if len(loop) >= 3:
            loops.append(loop)
    if not loops:
        raise RemeshError("A panel has no outline.")
    return loops


def _plane_basis(points: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Origin and 3x2 basis of the panel's own plane.

    The panels are flat by construction, so the two dominant singular
    directions carry the whole shape and projecting onto them loses nothing.
    """
    origin = points.mean(axis=0)
    _, _, directions = np.linalg.svd(points - origin, full_matrices=False)
    return origin, directions[:2].T


def _signed_area(polygon: np.ndarray) -> float:
    x, y = polygon[:, 0], polygon[:, 1]
    return 0.5 * float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))


def _inside(points: np.ndarray, loops: list[np.ndarray]) -> np.ndarray:
    """Even-odd point-in-polygon against every loop at once."""
    inside = np.zeros(len(points), dtype=bool)
    for loop in loops:
        a = loop
        b = np.roll(loop, -1, axis=0)
        straddles = (a[:, 1] > points[:, None, 1]) != (b[:, 1] > points[:, None, 1])
        with np.errstate(divide="ignore", invalid="ignore"):
            crossing = (b[:, 0] - a[:, 0]) * (points[:, None, 1] - a[:, 1]) / (
                b[:, 1] - a[:, 1]
            ) + a[:, 0]
        inside ^= np.logical_and(straddles, points[:, None, 0] < crossing).sum(axis=1) % 2 == 1
    return inside


def _interior_points(loops: list[np.ndarray], spacing: float) -> np.ndarray:
    """A triangular lattice covering the panel, clear of its outline.

    A triangular (row-offset) lattice is used rather than a square one
    because its Delaunay triangulation is already equilateral, so the result
    has no sliver to remove afterwards. Points are held half a spacing away
    from the outline so the boundary vertices, which are kept exactly, are
    not crowded into thin triangles against a lattice point.
    """
    stacked = np.concatenate(loops)
    low, high = stacked.min(axis=0), stacked.max(axis=0)
    rows = np.arange(low[1], high[1] + spacing, spacing * np.sqrt(3.0) / 2.0)
    lattice = []
    for index, y in enumerate(rows):
        offset = 0.0 if index % 2 == 0 else spacing / 2.0
        xs = np.arange(low[0] + offset, high[0] + spacing, spacing)
        lattice.append(np.stack([xs, np.full(len(xs), y)], axis=1))
    candidates = np.concatenate(lattice) if lattice else np.empty((0, 2))
    if not len(candidates):
        return candidates
    candidates = candidates[_inside(candidates, loops)]
    if not len(candidates):
        return candidates
    # Drop anything that crowds the outline.
    from scipy.spatial import cKDTree

    tree = cKDTree(stacked)
    distance, _ = tree.query(candidates)
    return candidates[distance >= spacing * 0.5]


def _triangulate(
    boundary: np.ndarray, interior: np.ndarray, loops: list[np.ndarray], spacing: float
):
    """Delaunay over outline + lattice, keeping only what is inside.

    An outline sampled along a straight run gives collinear point triples,
    and the triangulation can close one into a face of no area at all. The
    solver rejects those outright, and rightly: dropping them removes
    nothing, because a zero-area triangle covers no cloth.
    """
    points = np.concatenate([boundary, interior]) if len(interior) else boundary
    triangulation = Delaunay(points)
    simplices = triangulation.simplices
    centroids = points[simplices].mean(axis=1)
    corners = points[simplices]
    edge_a = corners[:, 1] - corners[:, 0]
    edge_b = corners[:, 2] - corners[:, 0]
    area = 0.5 * np.abs(edge_a[:, 0] * edge_b[:, 1] - edge_a[:, 1] * edge_b[:, 0])
    keep = _inside(centroids, loops) & (area > _AREA_FLOOR * spacing * spacing)
    faces = simplices[keep]
    if not len(faces):
        raise RemeshError("A panel produced no triangles inside its outline.")
    orphaned = np.setdiff1d(np.arange(len(boundary)), faces)
    if len(orphaned):
        raise RemeshError(
            f"{len(orphaned)} outline vertices ended up in no triangle."
        )
    return points, faces


def rebuild(vertices: np.ndarray, faces: np.ndarray) -> dict:
    """Replace each panel's interior, keeping its outline vertex-for-vertex.

    Returns the clean mesh, plus what :func:`transfer` needs to read the
    original vertices back out of it.
    """
    clean_vertices = [vertices[np.zeros(0, dtype=np.int64)]]
    clean_faces: list[np.ndarray] = []
    # Original index -> clean index, for the boundary vertices kept as-is.
    kept = np.full(len(vertices), -1, dtype=np.int64)
    # Per panel: what transfer() needs to locate the original vertices.
    panels: list[dict] = []
    total = 0

    for panel_vertices in _components(len(vertices), faces):
        member = np.zeros(len(vertices), dtype=bool)
        member[panel_vertices] = True
        panel_faces = faces[member[faces].all(axis=1)]
        if not len(panel_faces):
            continue

        origin, basis = _plane_basis(vertices[panel_vertices])
        loops_indices = _boundary_loops(panel_faces)
        boundary_indices = np.concatenate([np.asarray(loop) for loop in loops_indices])
        flat = (vertices - origin) @ basis
        loops = [flat[np.asarray(loop)] for loop in loops_indices]
        # Orient every loop the same way so even-odd sees holes as holes.
        loops = [loop if _signed_area(loop) > 0 else loop[::-1] for loop in loops]

        spacing = _panel_spacing(vertices, panel_faces)
        interior = _interior_points(loops, spacing)
        points, panel_clean_faces = _triangulate(
            flat[boundary_indices], interior, loops, spacing
        )

        clean_vertices.append(origin + points @ basis.T)
        clean_faces.append(panel_clean_faces + total)
        kept[boundary_indices] = np.arange(len(boundary_indices)) + total
        panels.append(
            {
                "original": panel_vertices,
                "flat": flat[panel_vertices],
                "points": points,
                "faces": panel_clean_faces + total,
            }
        )
        total += len(points)

    return {
        "vertices": np.concatenate(clean_vertices),
        "faces": np.concatenate(clean_faces),
        "kept": kept,
        "panels": panels,
    }


def _panel_spacing(vertices: np.ndarray, faces: np.ndarray) -> float:
    """The panel's own lattice pitch, read from its healthy edges.

    The median edge survives the slivers that the minimum does not, so it is
    what the replacement lattice should match: the clean mesh then carries
    the same resolution the pattern was authored at.
    """
    edges = np.concatenate([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]])
    lengths = np.linalg.norm(vertices[edges[:, 0]] - vertices[edges[:, 1]], axis=1)
    spacing = float(np.median(lengths))
    if not spacing > 0.0:
        raise RemeshError("A panel has no usable edge length.")
    return spacing


def remap_seams(seams: np.ndarray, kept: np.ndarray) -> np.ndarray:
    """Seam pairs in clean-mesh indices.

    Seams sit entirely on the outline, which is kept vertex-for-vertex, so
    this is a lookup rather than a search. A seam that misses is a real
    inconsistency and must not be silently dropped.
    """
    mapped = kept[seams]
    if (mapped < 0).any():
        missing = int((mapped < 0).any(axis=1).sum())
        raise RemeshError(
            f"{missing} seam pairs reference a vertex that is not on a panel outline."
        )
    return mapped


def transfer(rebuilt: dict, solved: np.ndarray, vertex_count: int) -> np.ndarray:
    """Read the original vertices back out of the solved clean mesh.

    Each original vertex is located once in the clean mesh's *rest* layout,
    in the panel's own plane, and then follows the triangle it sits in. So
    the original topology -- slivers included -- rides along with a solve
    that never had to represent it.
    """
    result = np.zeros((vertex_count, 3), dtype=np.float64)
    for panel in rebuilt["panels"]:
        points = panel["points"]
        faces = panel["faces"]
        triangulation = Delaunay(points)
        found = triangulation.find_simplex(panel["flat"])

        # Points outside every triangle (a vertex sitting a hair outside the
        # outline after projection) fall back to the nearest triangle, which
        # keeps them attached to the cloth instead of stranded at the origin.
        outside = found < 0
        if outside.any():
            centroids = points[triangulation.simplices].mean(axis=1)
            from scipy.spatial import cKDTree

            found[outside] = cKDTree(centroids).query(panel["flat"][outside])[1]

        simplices = triangulation.simplices[found]
        transform = triangulation.transform[found]
        offset = panel["flat"] - transform[:, 2]
        first_two = np.einsum("ijk,ik->ij", transform[:, :2], offset)
        weights = np.concatenate([first_two, 1.0 - first_two.sum(axis=1, keepdims=True)], axis=1)

        # Delaunay indexes the panel's own point block; lift to the solved mesh.
        base = int(faces.min()) if len(faces) else 0
        result[panel["original"]] = np.einsum(
            "ij,ijk->ik", weights, solved[simplices + base]
        )
    return result
