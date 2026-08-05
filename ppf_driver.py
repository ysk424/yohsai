# SPDX-License-Identifier: GPL-3.0-or-later
"""Sew Yohsai panels with the ZOZO Contact Solver, outside Blender.

This module never runs inside Blender.  It is executed by the ZOZO tree's
own Python interpreter as a child process of :mod:`ppf_zero_gravity`, so
the CUDA backend, its Rust cdylib, and its numpy stay entirely out of
Blender's address space: a solver crash costs the click, not the session.

The contract is two ``.npz`` files whose layout ``ppf_zero_gravity`` owns.
Input carries flat cloth panels, their triangles, the 1:1 seam pairs, the
static Body, and the solver settings; output carries the sewn cloth in the
caller's own vertex order plus what the run measured.

Vertex order is the one thing that cannot be assumed.  ``Scene.build``
renumbers vertices, so every read-back goes through the per-object
``local -> global`` map the build returns.  Reading ``vert_*.bin``
positionally would silently scramble the panels.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import numpy as np

# The ZOZO tree ships an embedded interpreter, whose `._pth` suppresses the
# usual script-directory entry, so a sibling import needs saying explicitly.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ppf_clear  # noqa: E402
import ppf_remesh  # noqa: E402


def _load_frontend(ppf_root: str):
    """Import the ZOZO frontend from its own tree.

    The package resolves its Rust cdylib relative to its own location, so
    the tree root is the only thing that has to be right.
    """
    if not os.path.isdir(ppf_root):
        raise SystemExit(f"ZOZO tree not found: {ppf_root}")
    if ppf_root not in sys.path:
        sys.path.insert(0, ppf_root)
    try:
        from frontend import App  # type: ignore[import-not-found]
    except ImportError as exc:
        raise SystemExit(
            f"Could not import the ZOZO frontend from {ppf_root}: {exc}. "
            "Build it there with `cargo build --release` first."
        ) from exc
    return App


def _stitch_rows(pairs: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Turn 1:1 seam pairs into the asset's CIPC stitch wire form.

    The asset layer takes 4-wide rows ``Ind = [i0, i1, i2, i2]`` with
    ``W = [1, 1-w, w, 0]``: a source vertex sewn to the point at parameter
    ``w`` along the edge ``(i1, i2)``.  A Yohsai seam pairs two vertices,
    which is the degenerate case ``i1 = i2`` and ``w = 0``.

    Scene assembly pads these to the solver's 6-wide barycentric pairs, so
    a future non-1:1 seam only has to fill in real ``(i1, i2, w)`` here.
    """
    source, target = pairs[:, 0], pairs[:, 1]
    index = np.stack([source, target, target, target], axis=1).astype(np.int64)
    weight = np.tile(np.asarray([1.0, 1.0, 0.0, 0.0]), (len(pairs), 1))
    return index, weight


def run(input_path: str, output_path: str) -> dict:
    data = np.load(input_path)
    ppf_root = str(data["ppf_root"])
    App = _load_frontend(ppf_root)

    cloth_vertices = np.ascontiguousarray(data["cloth_vertices"], dtype=np.float64)
    cloth_faces = np.ascontiguousarray(data["cloth_faces"], dtype=np.int64)
    seam_pairs = np.ascontiguousarray(data["seam_pairs"], dtype=np.int64)
    body_vertices = np.ascontiguousarray(data["body_vertices"], dtype=np.float64)
    body_faces = np.ascontiguousarray(data["body_faces"], dtype=np.int64)
    locked = np.ascontiguousarray(data["locked"], dtype=np.int64)
    settings = json.loads(str(data["settings"]))

    # Yohsai's own triangulation carries interior slivers an implicit solver
    # cannot assemble; ppf_remesh replaces each panel's interior while keeping
    # its outline, so the seams and the cut are untouched.
    cloth_pattern = np.ascontiguousarray(data["cloth_pattern"], dtype=np.float64)
    rebuilt = ppf_remesh.rebuild(cloth_vertices, cloth_faces, cloth_pattern)
    solve_faces = rebuilt["faces"]
    # Rebuilding curved cloth chords inside its own surface, and against a Body
    # the cloth already rests on that is inside the Body. An intersection-free
    # scene is what the solver's guarantee is built on, so what the rebuild
    # pushed in is put back before the scene is.
    solve_vertices, clearance = ppf_clear.clear_body(
        rebuilt["vertices"], solve_faces, body_vertices, body_faces
    )
    solve_seams = ppf_remesh.remap_seams(seam_pairs, rebuilt["kept"])
    solve_locked = np.zeros(len(solve_vertices), dtype=np.int64)
    locked_clean = rebuilt["kept"][np.flatnonzero(locked)]
    solve_locked[locked_clean[locked_clean >= 0]] = 1

    app = App.create(settings["session_name"])
    app.asset.add.tri("cloth", solve_vertices, solve_faces)
    app.asset.add.stitch("seams", _stitch_rows(solve_seams))
    app.asset.add.tri("body", body_vertices, body_faces)

    scene = app.scene.create()
    cloth = scene.add("cloth")
    cloth.param.set("young-mod", settings["young_modulus"])
    cloth.param.set("bend", settings["bend"])
    cloth.param.set("strain-limit", settings["strain_limit"])
    cloth.param.set("stitch-stiffness", settings["stitch_stiffness"])
    cloth.stitch("seams")
    # A Locked panel is the operator holding cloth in place, so it is a
    # prescribed boundary rather than something the solve may move.
    locked_indices = np.flatnonzero(solve_locked).tolist()
    if locked_indices:
        cloth.pin(locked_indices)
    # Pinning every Body vertex with no motion attached is what makes the
    # Body a static collider: it carries no degrees of freedom and is
    # uploaded to the device once instead of being solved each step.
    scene.add("body").pin()

    build_started = time.time()
    scene = scene.build()
    build_seconds = time.time() - build_started

    # The job runs in two phases, because sewing and settling want
    # opposite things from damping. Air drag strong enough to bring a
    # garment to rest in free space also overpowers the seam force, which
    # leaves the seams open; no damping at all closes them but leaves the
    # cloth drifting, so the frame the job happens to stop on decides the
    # answer. So: close the seams undamped, then switch drag on and let
    # the sewn shape come to rest.
    sew_frames = int(settings["sew_frames"])
    settle_frames = int(settings["settle_frames"])
    frames = sew_frames + settle_frames
    time_step = float(settings["time_step"])

    session = app.session.create(scene)
    param = session.param
    param.set("gravity", [0.0, 0.0, 0.0])
    param.set("isotropic-air-friction", 0.0)
    # The seam force saturates at `stitch_length_factor * l0`, and l0 is half
    # the contact gap -- under a millimetre. At the stock factor of 10 a pair
    # a garment's width apart therefore pulls no harder than one already
    # touching, which is why panels laid out for cutting creep instead of
    # closing. Raising the cap past the widest seam restores a force that
    # actually reflects how far the two sides still have to travel.
    param.set("stitch-length-factor", float(settings["stitch_length_factor"]))
    param.set("dt", time_step)
    param.set("frames", frames)
    if settle_frames:
        param.dyn("isotropic-air-friction").time(sew_frames * time_step).hold().change(
            float(settings["air_drag"])
        )
    session = session.build()

    solve_started = time.time()
    session.start(blocking=True)
    solve_seconds = time.time() - solve_started

    # local -> global from the build; invert it to restore the caller's order.
    to_global = np.asarray(scene._map_by_name["cloth"], dtype=np.int64)
    output_dir = os.path.join(session.info.path, "output")
    _require_progress(session.info.path, output_dir)
    frame = _last_written_frame(output_dir, frames)
    positions = np.fromfile(
        os.path.join(output_dir, f"vert_{frame}.bin"), dtype=np.float32
    ).reshape(-1, 3)
    # The solve ran on the clean mesh; put the caller's own vertices back.
    #
    # Reading the *motion* back instead -- interpolating where the solve
    # started as well as where it ended, and moving the original cloth by the
    # difference -- looks better and measures far worse: 1268 tri-tri pairs
    # against the Body where reading positions gives 14. The solve rests the
    # clean surface against the Body at the contact gap, and the original
    # mesh's own departure from that surface then pokes straight through it.
    # Interpolating positions hands back the surface the solver actually
    # guaranteed, which is the one that clears the Body.
    solved = positions[to_global].astype(np.float64)
    sewn = ppf_remesh.transfer(rebuilt, solved, len(cloth_vertices))

    global_pairs = to_global[solve_seams]
    gaps = np.linalg.norm(
        positions[global_pairs[:, 0]] - positions[global_pairs[:, 1]], axis=1
    )
    # How far the last frame still moved: the measure of whether the sewn
    # shape actually settled, which is what makes the result repeatable.
    residual_mm = 0.0
    if frame > 0:
        previous = np.fromfile(
            os.path.join(output_dir, f"vert_{frame - 1}.bin"), dtype=np.float32
        ).reshape(-1, 3)
        residual_mm = float(np.linalg.norm(positions - previous, axis=1).max()) * 1000.0

    report = {
        "cloth_vertices_in": int(len(cloth_vertices)),
        "cloth_vertices_solved": int(len(solve_vertices)),
        "cloth_faces_solved": int(len(solve_faces)),
        # What the rebuild had to be lifted out of the Body by, which is a
        # measure of how far the rebuilt surface strayed from the cloth.
        "cleared_vertices": clearance["lifted"],
        "cleared_max_mm": clearance["lifted_max_mm"],
        "sew_frames": sew_frames,
        "settle_frames": settle_frames,
        "frames_requested": frames,
        "frames_written": frame,
        "finished": bool(session.finished()),
        "build_seconds": build_seconds,
        "solve_seconds": solve_seconds,
        "seam_gap_mean_mm": float(gaps.mean()) * 1000.0,
        "seam_gap_max_mm": float(gaps.max()) * 1000.0,
        "residual_motion_mm": residual_mm,
    }
    np.savez(output_path, cloth_vertices=sewn, report=json.dumps(report))
    return report


def _require_progress(session_dir: str, output_dir: str) -> None:
    """Fail loudly when the solver stopped before simulating anything.

    `vert_0.bin` is the scene as handed over, not a result. Treating it as
    one returns the input unchanged and reports success, which is the worst
    possible outcome: the caller sees cloth that did not move and no reason
    why. The solver's own `error.log` says what went wrong, so raise it.
    """
    if os.path.isfile(os.path.join(output_dir, "vert_1.bin")):
        return
    detail = ""
    try:
        with open(os.path.join(session_dir, "error.log")) as log:
            lines = [line.strip() for line in log if line.strip()]
        detail = lines[0] if lines else ""
    except OSError:
        pass
    raise SystemExit(
        "The solver stopped before completing a single frame"
        + (f": {detail}" if detail else ".")
    )


def _last_written_frame(output_dir: str, requested: int) -> int:
    """The newest frame the solver actually wrote.

    A run stopped part-way still leaves usable cloth, so prefer the last
    real frame over failing on the requested one. Frame 0 is excluded by
    `_require_progress`, which runs first.
    """
    for frame in range(requested, 0, -1):
        if os.path.isfile(os.path.join(output_dir, f"vert_{frame}.bin")):
            return frame
    raise SystemExit(f"The solver wrote no simulated frames to {output_dir}.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    arguments = parser.parse_args(argv)
    report = run(arguments.input, arguments.output)
    # The parent reads the report from the output file; this is for the log.
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
