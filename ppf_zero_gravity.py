# SPDX-License-Identifier: GPL-3.0-or-later
"""Zero GRAVITY: sew the panels with the ZOZO Contact Solver.

Zero GRAVITY closes every seam of a garment whose panels are still flat and
still outside the Body.  That is the whole job, so this runs it as one
solve rather than as the repeated nudges the square-lattice path uses for
Normal GRAVITY, which stays exactly as it was.

Sewing this way is not the same trade as before.  The square-lattice solver
reaches a seam by iterating a positional projection, so its stiffness is a
function of how many iterations a click can afford, and buying speed costs
correctness.  The contact solver closes a seam with an implicit force
solved inside its Newton step, so the result is the converged one at any
step count, and the answer to "make it faster" stops being "make it worse".
What it costs instead is wall clock: a press is a job of a few seconds, not
a button that answers in one frame.

Two things make that affordable, and both come from what Yohsai can promise
about its own state.  The Body never moves, so it is handed over as a
static collider: no degrees of freedom, uploaded to the device once.  The
panels start flat and outside the Body, so the scene begins free of
intersection, which is the state the solver requires and the state the
existing ZOZO hand-off cannot reach from already-draped cloth.

Because the panels are flat at the start, their placed position is also
their stress-free shape, and the solver takes the geometry it is given as
rest.  So a press always sews from flat; pressing again re-sews rather than
advancing, and never mistakes stretched cloth for the pattern.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import bpy
import numpy as np

from .kitsuke import (
    KitsukeError,
    _PartRange,
    _body_snapshot,
    _seam_constraints_from_parts,
    _transform_points,
    _world_vertices,
)
from .mesh_loader import LOCKED_OBJECT_KEY, participating_parts


# Solver settings for sewing flat panels in free space.  Young's modulus and
# bend match the ZOZO garment examples; the strain limit is what keeps a
# seam from closing by stretching the panel instead of moving it.
YOUNG_MODULUS = 100.0
BEND = 1.0
STRAIN_LIMIT = 0.05
TIME_STEP = 0.01

# The job sews first and settles second; the driver owns why.  Measured on
# the reference garment (9.6k cloth vertices, the 225k-vertex Body as a
# static collider): 60 mm of seam gap reaches 1.04 mm mean / 1.18 mm max --
# 1 mm being the contact offset, i.e. the cloth's own thickness, so the
# seam is shut -- and the last frame moves 0.004 mm, in 26 s total.
SEWING_FRAMES = 8
SETTLE_FRAMES = 5
# Drag high enough to settle the garment also overpowers the seam force, so
# it belongs only to the second phase. Anything from 2 upward settles; the
# value is not delicate.
AIR_DRAG = 5.0

_ENVIRONMENT_ROOT = "YOHSAI_PPF_ROOT"
_DRIVER = "ppf_driver.py"


def _zozo_root() -> Path:
    """Locate the ZOZO Contact Solver tree.

    The environment variable wins so a developer can point at a worktree;
    otherwise assume it sits beside Yohsai's own checkout, which is how the
    hand-off path already expects to find it.
    """
    override = os.environ.get(_ENVIRONMENT_ROOT, "").strip()
    candidates = []
    if override:
        candidates.append(Path(override))
    candidates.append(Path(__file__).resolve().parent.parent / "ppf-contact-solver")
    for candidate in candidates:
        if (candidate / "frontend" / "__init__.py").is_file():
            return candidate
    searched = ", ".join(str(candidate) for candidate in candidates)
    raise KitsukeError(
        "The ZOZO Contact Solver tree was not found "
        f"(looked in: {searched}). Set {_ENVIRONMENT_ROOT} to its path."
    )


def _zozo_python(root: Path) -> Path:
    """The interpreter that owns the ZOZO frontend's dependencies.

    Blender's own Python cannot be used: the frontend loads a Rust cdylib
    built against its tree and pulls in numpy and scipy of its own.
    """
    bundled = root / "build-win-native" / "python" / "python.exe"
    if bundled.is_file():
        return bundled
    raise KitsukeError(
        f"No ZOZO Python interpreter found under {root}. "
        "Build the native Windows distribution there first."
    )


def _part_ranges(collection: bpy.types.Collection) -> list[_PartRange]:
    objects = list(participating_parts(collection))
    if len(objects) < 2:
        raise KitsukeError("Zero GRAVITY needs at least two pending or completed parts.")
    ranges: list[_PartRange] = []
    offset = 0
    for obj in objects:
        if any(abs(float(scale) - 1.0) > 1.0e-5 for scale in obj.scale):
            raise KitsukeError(
                f"Apply Scale on {obj.name} before Zero GRAVITY; "
                "moving and rotating are supported, scaling is not."
            )
        count = len(obj.data.vertices)
        locked = bool(obj.get(LOCKED_OBJECT_KEY, False))
        ranges.append(_PartRange(obj, offset, count, locked))
        offset += count
    return ranges


def _cloth_geometry(parts: list[_PartRange]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Concatenated world vertices, triangles, and per-vertex Lock flags.

    The contact solver simulates triangles, so the panel quads are read
    through Blender's own triangulation rather than the grainline quad map
    the square-lattice solver uses for its material metric.
    """
    position_blocks: list[np.ndarray] = []
    face_blocks: list[np.ndarray] = []
    locked_blocks: list[np.ndarray] = []
    for part in parts:
        mesh = part.obj.data
        mesh.calc_loop_triangles()
        triangles = np.empty((len(mesh.loop_triangles), 3), dtype=np.int64)
        mesh.loop_triangles.foreach_get("vertices", triangles.ravel())
        if not len(triangles):
            raise KitsukeError(f"{part.obj.name} has no triangles to simulate.")
        matrix = part.obj.matrix_world
        block = _world_vertices(part.obj)
        # A reflected Object transform reverses winding once the vertices
        # reach world space; restore the authored outward side so contact
        # pushes cloth away from the Body rather than into it.
        if matrix.to_3x3().determinant() < 0.0:
            triangles = triangles[:, (0, 2, 1)]
        position_blocks.append(block)
        face_blocks.append(triangles + part.start)
        locked_blocks.append(
            np.full(part.count, 1 if part.locked else 0, dtype=np.int64)
        )
    return (
        np.concatenate(position_blocks).astype(np.float64),
        np.concatenate(face_blocks),
        np.concatenate(locked_blocks),
    )


def _validate(
    parts: list[_PartRange],
    positions: np.ndarray,
    seams: np.ndarray,
    locked: np.ndarray,
) -> None:
    if not len(seams):
        raise KitsukeError("There are no seams to sew.")
    if seams.min() < 0 or seams.max() >= len(positions):
        raise KitsukeError("The sewing pairs do not match the current panel vertices.")
    if not np.all(np.isfinite(positions)):
        raise KitsukeError("The panels contain non-finite coordinates.")
    if np.all(locked == 1):
        raise KitsukeError("Every panel is Locked; unlock at least one before sewing.")


def _scatter(parts: list[_PartRange], positions: np.ndarray) -> None:
    for part in parts:
        obj = part.obj
        inverse = obj.matrix_world.inverted_safe()
        block = positions[part.start : part.start + part.count]
        local = _transform_points(block.astype(np.float32), inverse)
        obj.data.vertices.foreach_set("co", local.ravel())
        obj.data.update()
        obj.hide_set(False)
        obj.hide_render = False


def sew_zero_gravity(
    context,
    collection: bpy.types.Collection,
    body: bpy.types.Object,
    frames: int = SEWING_FRAMES,
) -> str:
    """Sew every seam of the collection and write the result back to Blender."""
    if collection is None or collection.get("yohsai_role") != "clothes":
        raise KitsukeError("No loaded Yohsai clothes collection is selected.")

    root = _zozo_root()
    interpreter = _zozo_python(root)

    parts = _part_ranges(collection)
    positions, faces, locked = _cloth_geometry(parts)
    seams = _seam_constraints_from_parts(collection, parts)
    _validate(parts, positions, seams, locked)
    body_snapshot = _body_snapshot(context, body)

    settings = {
        "session_name": f"yohsai_{collection.name}",
        "young_modulus": YOUNG_MODULUS,
        "bend": BEND,
        "strain_limit": STRAIN_LIMIT,
        "time_step": TIME_STEP,
        "sew_frames": int(frames),
        "settle_frames": SETTLE_FRAMES,
        "air_drag": AIR_DRAG,
    }

    with tempfile.TemporaryDirectory(prefix="yohsai_ppf_") as workspace:
        input_path = os.path.join(workspace, "scene.npz")
        output_path = os.path.join(workspace, "sewn.npz")
        np.savez(
            input_path,
            ppf_root=str(root),
            cloth_vertices=positions,
            cloth_faces=faces,
            seam_pairs=seams.astype(np.int64),
            body_vertices=body_snapshot.vertices.astype(np.float64),
            body_faces=body_snapshot.faces.astype(np.int64),
            locked=locked,
            settings=json.dumps(settings),
        )
        completed = subprocess.run(
            [
                str(interpreter),
                str(Path(__file__).resolve().parent / _DRIVER),
                "--input",
                input_path,
                "--output",
                output_path,
            ],
            capture_output=True,
            text=True,
            cwd=str(root),
        )
        if completed.returncode != 0 or not os.path.isfile(output_path):
            raise KitsukeError(_failure_message(completed))
        result = np.load(output_path)
        sewn = np.asarray(result["cloth_vertices"], dtype=np.float64)
        report = json.loads(str(result["report"]))

    if sewn.shape != positions.shape:
        raise KitsukeError(
            "The solver returned a different vertex count than it was given."
        )
    if not np.all(np.isfinite(sewn)):
        raise KitsukeError(
            "The solver returned a non-finite state; the cloth was left unchanged."
        )

    _scatter(parts, sewn)
    context.view_layer.update()
    return (
        f"Zero GRAVITY: sewed {len(seams)} pairs across {len(parts)} panels "
        f"in {report['frames_written']} frames ({report['solve_seconds']:.1f} s); "
        f"seam gap mean {report['seam_gap_mean_mm']:.2f} mm, "
        f"max {report['seam_gap_max_mm']:.2f} mm; "
        f"last frame moved {report['residual_motion_mm']:.3f} mm"
    )


def _failure_message(completed: subprocess.CompletedProcess) -> str:
    """Surface the solver's own rejection rather than a generic failure.

    Its messages are the only ground truth about why a shell was refused,
    so the last meaningful line is worth more here than the exit code.
    """
    for stream in (completed.stderr, completed.stdout):
        lines = [line.strip() for line in (stream or "").splitlines() if line.strip()]
        if lines:
            return f"The ZOZO solver failed: {lines[-1]}"
    return f"The ZOZO solver failed with exit code {completed.returncode}."
