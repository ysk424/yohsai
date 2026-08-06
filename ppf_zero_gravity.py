# SPDX-License-Identifier: GPL-3.0-or-later
"""Zero GRAVITY: sew the panels with the ZOZO Contact Solver.

Zero GRAVITY closes every seam of a garment whose panels are still flat and
still outside the Body.  That is the whole job, so this runs it as one solve
rather than as the repeated nudges Yohsai's own square-lattice solver used to
take, which is why that solver is no longer here.

Sewing this way is not the same trade.  A positional projection reaches a
seam by iterating, so its stiffness is a function of how many iterations a
click can afford, and buying speed costs correctness.  The contact solver
closes a seam with an implicit force solved inside its Newton step, so the
result is the converged one at any step count, and the answer to "make it
faster" stops being "make it worse".  What it costs instead is wall clock: a
press is a job of a few seconds, not a button that answers in one frame.

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
    part_ranges,
)


# Solver settings for sewing flat panels in free space.  Young's modulus and
# bend match the ZOZO garment examples; the strain limit is what keeps a
# seam from closing by stretching the panel instead of moving it.
YOUNG_MODULUS = 100.0
BEND = 1.0
STRAIN_LIMIT = 0.05
TIME_STEP = 0.01

# The job sews first and settles second; the driver owns why.
SEWING_FRAMES = 6
SETTLE_FRAMES = 5
# Drag high enough to settle the garment also overpowers the seam force, so
# it belongs only to the second phase. Anything from 2 upward settles; the
# value is not delicate.
AIR_DRAG = 5.0
STITCH_STIFFNESS = 1.0
# Raises the cap the seam force saturates at; the driver owns why it matters.
# The reference garment's panels start 292 mm apart, and the stock factor of
# 10 caps the pull at about 5 mm of separation, so they barely move: 8 frames
# closed 292 mm to 211 mm.  At 100 the same seam reaches 2.1 mm in 6.  Going
# further buys nothing measurable (3000 gives 2.06 mm), so this is set past
# the widest seam rather than as high as it will go.
STITCH_LENGTH_FACTOR = 100.0

_ENVIRONMENT_ROOT = "YOHSAI_PPF_ROOT"
_DRIVER = "ppf_driver.py"
_TREE_NAME = "ppf-contact-solver"


def is_zozo_tree(path: Path) -> bool:
    """Whether this directory is a usable ZOZO Contact Solver checkout."""
    return (path / "frontend" / "__init__.py").is_file()


def _configured_root() -> str:
    """The path set in Add-on Preferences, if the add-on is registered."""
    try:
        preferences = bpy.context.preferences.addons[__package__].preferences
    except (AttributeError, KeyError):
        return ""
    return bpy.path.abspath(getattr(preferences, "ppf_root", "") or "").strip()


def _candidate_roots() -> list[Path]:
    """Where to look, best evidence first.

    Installed as an extension, this module lives under Blender's own
    `bl_ext` directory, so a path relative to it finds nothing; that
    fallback is only useful when Yohsai runs from its checkout. Hence the
    explicit setting first and a search of the usual checkout homes after.
    """
    candidates: list[Path] = []
    override = os.environ.get(_ENVIRONMENT_ROOT, "").strip()
    if override:
        candidates.append(Path(override))
    configured = _configured_root()
    if configured:
        candidates.append(Path(configured))
    candidates.append(Path(__file__).resolve().parent.parent / _TREE_NAME)
    home = Path.home()
    candidates.extend(
        home / parent / _TREE_NAME
        for parent in ("git", "source/repos", "Documents", "projects", "")
    )
    return candidates


def _zozo_root() -> Path:
    candidates = _candidate_roots()
    for candidate in candidates:
        if is_zozo_tree(candidate):
            return candidate
    searched = "\n".join(f"  {candidate}" for candidate in candidates)
    raise KitsukeError(
        "The ZOZO Contact Solver tree was not found. Set its path in "
        "Preferences > Add-ons > Yohsai (or the "
        f"{_ENVIRONMENT_ROOT} environment variable). Looked in:\n{searched}"
    )


def describe_zozo_root() -> str:
    """One line for Preferences saying what the path setting resolved to.

    Reported where the path is entered, so a wrong directory is visible
    when it is typed rather than when Zero GRAVITY is first pressed.
    """
    try:
        root = _zozo_root()
    except KitsukeError:
        return "Not found. Zero GRAVITY cannot sew until this points at the checkout."
    try:
        _zozo_python(root)
    except KitsukeError as exc:
        return str(exc)
    return f"Using {root}"


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


def _cloth_geometry(parts: list[_PartRange]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Concatenated world vertices, triangles, and per-vertex Lock flags.

    The contact solver simulates triangles, so the panel quads are read
    through Blender's own triangulation rather than through the grainline
    quad map, which describes a material metric rather than a surface.
    """
    position_blocks: list[np.ndarray] = []
    face_blocks: list[np.ndarray] = []
    locked_blocks: list[np.ndarray] = []
    pattern_blocks: list[np.ndarray] = []
    for part in parts:
        mesh = part.obj.data
        pattern_blocks.append(_pattern_coordinates(part))
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
        np.concatenate(pattern_blocks).astype(np.float64),
    )


def _pattern_coordinates(part: _PartRange) -> np.ndarray:
    """The panel's authored flat pattern coordinates, per vertex.

    This is the domain the solver's mesh is rebuilt in, because it is the one
    thing about a panel that stays flat: the cloth curves as it is sewn, and a
    sleeve is a tube before anything is sewn at all.
    """
    mesh = part.obj.data
    attribute = mesh.attributes.get("yohsai_pattern_position")
    if (
        attribute is None
        or attribute.domain != "POINT"
        or attribute.data_type != "FLOAT_VECTOR"
        or len(attribute.data) != len(mesh.vertices)
    ):
        raise KitsukeError(
            f"{part.obj.name} has no valid pattern coordinates. "
            "Load it again before Zero GRAVITY."
        )
    block = np.empty((len(mesh.vertices), 3), dtype=np.float64)
    attribute.data.foreach_get("vector", block.ravel())
    if not np.all(np.isfinite(block)):
        raise KitsukeError(f"{part.obj.name} has non-finite pattern coordinates.")
    return block


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

    parts = part_ranges(collection, "Zero GRAVITY")
    positions, faces, locked, pattern = _cloth_geometry(parts)
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
        "stitch_stiffness": STITCH_STIFFNESS,
        "stitch_length_factor": STITCH_LENGTH_FACTOR,
    }

    # Cleaning up scratch files must never discard a finished solve, so the
    # directory is removed on a best-effort basis: a lingering handle here
    # would otherwise throw away half a minute of work over a temp file.
    with tempfile.TemporaryDirectory(
        prefix="yohsai_ppf_", ignore_cleanup_errors=True
    ) as workspace:
        input_path = os.path.join(workspace, "scene.npz")
        output_path = os.path.join(workspace, "sewn.npz")
        np.savez(
            input_path,
            ppf_root=str(root),
            cloth_vertices=positions,
            cloth_pattern=pattern,
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
        # np.load on an npz is lazy and holds the file open, which on
        # Windows blocks the directory from being removed. Close it here.
        with np.load(output_path) as result:
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
    # Sewing moves cloth a garment's width at most. A result that throws a
    # vertex far past the Body is not cloth, it is a rebuilt panel that failed
    # to locate one of its vertices, and writing it back would scatter the
    # garment across the scene with nothing to say why.
    body_size = float(
        np.linalg.norm(body_snapshot.bounds_maximum - body_snapshot.bounds_minimum)
    )
    travelled = np.linalg.norm(sewn - positions, axis=1)
    if travelled.max() > body_size:
        raise KitsukeError(
            f"The solver moved a vertex {travelled.max():.2f} m, further than the "
            f"whole Body ({body_size:.2f} m), so the result was discarded and the "
            "cloth left unchanged."
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
    # The solver draws progress bars on the same stream, and they arrive after
    # the traceback, so the last line is usually "build scene: 92%" and says
    # nothing. Prefer the last line that reads like a diagnosis.
    for stream in (completed.stderr, completed.stdout):
        lines = [line.strip() for line in (stream or "").splitlines() if line.strip()]
        informative = [
            line
            for line in lines
            if ("Error" in line or "error" in line or "FATAL" in line)
            and "%|" not in line
        ]
        if informative:
            return f"The ZOZO solver failed: {informative[-1]}"
    for stream in (completed.stderr, completed.stdout):
        lines = [
            line.strip()
            for line in (stream or "").splitlines()
            if line.strip() and "%|" not in line
        ]
        if lines:
            return f"The ZOZO solver failed: {lines[-1]}"
    return f"The ZOZO solver failed with exit code {completed.returncode}."
