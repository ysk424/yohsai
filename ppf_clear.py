# SPDX-License-Identifier: GPL-3.0-or-later
"""Lift the rebuilt panels clear of the Body before the scene is built.

`ppf_remesh` rebuilds a panel by reading every new point off the original
triangles, so each point lands *on* the cloth.  The triangles between them do
not: they span the original ones, and across curvature a chord cuts inside
the surface it replaces.  On cloth already resting against the Body -- a
garment sewn by an earlier press -- inside the surface is inside the Body,
and the contact gap the solver leaves is under a millimetre, so it takes very
little chord to cross it.  Measured on the reference garment: rebuilding the
front panel produced 306 tri-tri pairs against a Body it had none against
before, which is what refused the press that attaches the sleeves.

A barrier solver refuses that scene outright and is right to; an
intersection-free start is what its guarantee is built on.  So the crossings
are found and undone before the scene is built.  The checker reports exactly
which triangles cross, so this is a list rather than a search: 36 of 8780
vertices on the reference garment, lifted by 1.51 mm at the worst, one pass.

What this does not do is decide anything about the garment.  It moves cloth
the rebuild moved, in the direction the Body's own surface points, far enough
to clear it and no further.  Cloth the operator placed is not touched unless
the rebuild put it inside the Body.

This module runs in the ZOZO tree's interpreter, alongside `ppf_driver`; it
must never be imported from Blender.
"""

from __future__ import annotations

import ctypes
import os

import numpy as np
from scipy.spatial import cKDTree


# How far outside the Body a lifted vertex is placed. The solver's contact
# offset is under a millimetre, so this clears it with room to spare while
# staying well inside the millimetres a seam is about to move anyway.
_CLEARANCE_M = 0.0015
# Only Body within this of the cloth can be crossed by it, and checking a
# 449,472-triangle Body costs minutes where checking the band costs seconds.
_BAND_M = 0.03
# Lifting a vertex can expose its neighbours, so the check repeats. One pass
# has been enough on every garment measured; this is the point at which the
# geometry is not what this module thinks it is, and saying so beats looping.
_MAX_PASSES = 8


class ClearError(RuntimeError):
    """The rebuilt panels could not be lifted clear of the Body."""


def _library():
    """The tri-tri checker that ships with the add-on, or None."""
    directory = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin")
    path = os.path.join(directory, "shell_isect.dll")
    if not os.path.isfile(path):
        return None
    try:
        # The checker is OpenMP; its runtime sits beside it, and Windows does
        # not search a DLL's own directory for its dependencies by default.
        if hasattr(os, "add_dll_directory"):
            os.add_dll_directory(directory)
        library = ctypes.CDLL(path)
    except OSError:
        return None
    library.shell_isect_check.argtypes = [
        ctypes.POINTER(ctypes.c_double), ctypes.c_int32,
        ctypes.POINTER(ctypes.c_int32), ctypes.c_int32,
        ctypes.POINTER(ctypes.c_int32), ctypes.c_int32,
        ctypes.POINTER(ctypes.c_int32),
    ]
    library.shell_isect_check.restype = ctypes.c_int
    return library


def _crossings(library, vertices: np.ndarray, faces: np.ndarray, limit: int) -> np.ndarray:
    """Face pairs that intersect, as the checker reports them."""
    points = np.ascontiguousarray(vertices, dtype=np.float64)
    triangles = np.ascontiguousarray(faces, dtype=np.int32)
    count = ctypes.c_int32(0)
    buffer = (ctypes.c_int32 * (limit * 2))()
    result = library.shell_isect_check(
        points.ctypes.data_as(ctypes.POINTER(ctypes.c_double)), ctypes.c_int32(len(points)),
        triangles.ctypes.data_as(ctypes.POINTER(ctypes.c_int32)), ctypes.c_int32(len(triangles)),
        buffer, ctypes.c_int32(limit), ctypes.byref(count),
    )
    if result not in (0, 2):
        raise ClearError(f"The self-intersection checker failed with code {result}.")
    written = min(int(count.value), limit)
    return np.frombuffer(buffer, dtype=np.int32, count=2 * written).reshape(-1, 2).astype(np.int64)


def _outward_normals(vertices: np.ndarray, faces: np.ndarray) -> np.ndarray:
    """Area-weighted vertex normals of the Body."""
    corners = vertices[faces]
    weighted = np.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0])
    normals = np.zeros_like(vertices)
    for column in range(3):
        np.add.at(normals, faces[:, column], weighted)
    return normals / np.maximum(np.linalg.norm(normals, axis=1, keepdims=True), 1.0e-30)


def clear_body(
    cloth_vertices: np.ndarray,
    cloth_faces: np.ndarray,
    body_vertices: np.ndarray,
    body_faces: np.ndarray,
) -> tuple[np.ndarray, dict]:
    """Return the cloth with any crossing of the Body lifted out of it."""
    library = _library()
    if library is None:
        # Without the checker there is nothing to be certain about, so the
        # cloth goes over as it is and the solver says what it thinks.
        return cloth_vertices, {"available": False, "lifted": 0, "lifted_max_mm": 0.0, "passes": 0}

    cloth = np.array(cloth_vertices, dtype=np.float64, copy=True)
    normals = _outward_normals(body_vertices, body_faces)
    body_tree = cKDTree(body_vertices)

    to_cloth, _ = cKDTree(cloth).query(body_vertices)
    inside_band = to_cloth < _BAND_M
    near = np.flatnonzero(inside_band)
    if not len(near):
        return cloth_vertices, {"available": True, "lifted": 0, "lifted_max_mm": 0.0, "passes": 0}
    renumber = np.full(len(body_vertices), -1, dtype=np.int64)
    renumber[near] = np.arange(len(near))
    band_faces = renumber[body_faces[inside_band[body_faces].all(axis=1)]]
    band_vertices = body_vertices[near]

    # The checker reports Body against Body as well, and a CC-style Body has
    # thousands of those on its own. Room for all of them, or they crowd the
    # cloth pairs out of the buffer and the clearing loops without progress.
    limit = 2 * len(cloth_faces) + 65536
    for attempt in range(_MAX_PASSES):
        pairs = _crossings(
            library,
            np.vstack([cloth, band_vertices]),
            np.vstack([cloth_faces, band_faces + len(cloth)]),
            limit,
        )
        first = pairs[:, 0] < len(cloth_faces)
        second = pairs[:, 1] < len(cloth_faces)
        # Body against Body is the collider's own business, and the scene
        # assembly skips those pairs too.
        against_body = pairs[first ^ second]
        against_cloth = pairs[first & second]
        if not len(against_body) and not len(against_cloth):
            break

        if len(against_body):
            crossing = np.where(
                against_body[:, 0] < len(cloth_faces), against_body[:, 0], against_body[:, 1]
            )
            lifted = np.unique(cloth_faces[crossing])
            _, nearest = body_tree.query(cloth[lifted])
            normal = normals[nearest]
            outside = np.einsum("ij,ij->i", cloth[lifted] - body_vertices[nearest], normal)
            cloth[lifted] += normal * np.maximum(_CLEARANCE_M - outside, 0.0)[:, None]
        for one, other in against_cloth:
            # Two rebuilt triangles crossing each other have no Body normal to
            # follow, so they are separated along the line between them.
            here, there = cloth_faces[one], cloth_faces[other]
            direction = cloth[here].mean(axis=0) - cloth[there].mean(axis=0)
            distance = float(np.linalg.norm(direction))
            if distance <= 1.0e-12:
                continue
            direction = direction / distance * (0.5 * _CLEARANCE_M)
            cloth[here] += direction
            cloth[there] -= direction
    else:
        raise ClearError(
            f"The rebuilt panels still cross the Body after {_MAX_PASSES} passes, "
            "so the scene the solver needs could not be reached."
        )

    moved = np.linalg.norm(cloth - cloth_vertices, axis=1)
    return cloth, {
        "available": True,
        "lifted": int((moved > 0.0).sum()),
        "lifted_max_mm": float(moved.max()) * 1000.0,
        "passes": attempt,
    }
