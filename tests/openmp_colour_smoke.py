# SPDX-License-Identifier: GPL-3.0-or-later
"""Smoke-test coloured OpenMP vs thread-count determinism on a square lattice.

Builds a small hanging sheet, advances gravity with OMP_NUM_THREADS=1 and =4,
and reports max position delta. With correct colouring the two runs should
match bit-for-bit. Serial main-branch GS order is not compared here — that
difference is intentional on the yohsai-mp branch.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class Topology:
    edges: np.ndarray
    edge_rest_lengths: np.ndarray
    quads: np.ndarray
    quad_rest_metrics: np.ndarray
    bends: np.ndarray
    bend_rest_lengths: np.ndarray


@dataclass
class Body:
    vertices: np.ndarray
    faces: np.ndarray


def make_lattice(width: int = 16, height: int = 16, spacing: float = 0.01):
    positions = []
    locked = []
    for j in range(height):
        for i in range(width):
            positions.append((i * spacing, 0.0, -j * spacing))
            locked.append(1 if j == 0 else 0)
    positions = np.asarray(positions, dtype=np.float32)
    velocities = np.zeros_like(positions)
    locked_arr = np.asarray(locked, dtype=np.int32)

    edges = []
    rest = []
    for j in range(height):
        for i in range(width - 1):
            a = j * width + i
            edges.append((a, a + 1))
            rest.append(spacing)
    for j in range(height - 1):
        for i in range(width):
            a = j * width + i
            edges.append((a, a + width))
            rest.append(spacing)

    quads = []
    metrics = []
    for j in range(height - 1):
        for i in range(width - 1):
            x0 = j * width + i
            x1 = x0 + 1
            x2 = x0 + width + 1
            x3 = x0 + width
            quads.append((x0, x1, x2, x3))
            metrics.append((spacing * spacing, spacing * spacing, 0.0))

    bends = []
    bend_rest = []
    for j in range(height):
        for i in range(width - 2):
            a = j * width + i
            bends.append((a, a + 1, a + 2))
            bend_rest.append((spacing, spacing))
    for j in range(height - 2):
        for i in range(width):
            a = j * width + i
            bends.append((a, a + width, a + 2 * width))
            bend_rest.append((spacing, spacing))

    topology = Topology(
        edges=np.asarray(edges, dtype=np.int32),
        edge_rest_lengths=np.asarray(rest, dtype=np.float32),
        quads=np.asarray(quads, dtype=np.int32),
        quad_rest_metrics=np.asarray(metrics, dtype=np.float32),
        bends=np.asarray(bends, dtype=np.int32),
        bend_rest_lengths=np.asarray(bend_rest, dtype=np.float32).reshape((-1, 2)),
    )
    body = Body(
        vertices=np.empty((0, 3), dtype=np.float32),
        faces=np.empty((0, 3), dtype=np.int32),
    )
    seams = np.empty((0, 2), dtype=np.int32)
    return positions, velocities, seams, topology, body, locked_arr


def run_with_threads(thread_count: int, clicks: int = 8, iterations: int = 20) -> np.ndarray:
    env = os.environ.copy()
    env["OMP_NUM_THREADS"] = str(thread_count)
    env["OMP_DYNAMIC"] = "FALSE"
    # Spawn a child so each thread count gets a fresh OpenMP runtime.
    script = f"""
import os, sys
from pathlib import Path
import numpy as np
ROOT = Path(r"{ROOT}")
sys.path.insert(0, str(ROOT))
os.environ["OMP_NUM_THREADS"] = "{thread_count}"
os.environ["OMP_DYNAMIC"] = "FALSE"
from tests.openmp_colour_smoke import make_lattice
from native_solver import NativeSolverRuntime

positions, velocities, seams, topology, body, locked = make_lattice()
runtime = NativeSolverRuntime(positions, velocities, seams, topology, body, locked)
first_disp = 0.0
for click in range({clicks}):
    runtime.advance(
        body_candidates=np.empty((0, 2), dtype=np.int32),
        gravity_magnitude=9.81,
        solver_iterations={iterations},
    )
    if click == 0:
        first_disp = float(runtime.last_stats.get("maximum_displacement", 0.0))
out_positions, _ = runtime.state()
np.save(sys.argv[1], out_positions)
print(
    f"threads={{os.environ['OMP_NUM_THREADS']}} "
    f"first_max_disp={{first_disp:.6f}} "
    f"final_z_min={{float(out_positions[:, 2].min()):.6f}}"
)
runtime.close()
"""
    out = Path(ROOT) / "build" / f"omp_pos_t{thread_count}.npy"
    out.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        [sys.executable, "-c", script, str(out)],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"child failed (threads={thread_count}):\n{completed.stdout}\n{completed.stderr}"
        )
    print(completed.stdout.strip())
    return np.load(out)


def main() -> int:
    one = run_with_threads(1)
    four = run_with_threads(4)
    delta = float(np.max(np.abs(one.astype(np.float64) - four.astype(np.float64))))
    print(f"max |pos(t=1) - pos(t=4)| = {delta:.9e}")
    if not np.isfinite(one).all() or not np.isfinite(four).all():
        print("FAIL: non-finite positions")
        return 1
    if delta > 0.0:
        print("WARN: thread counts disagreed — check colouring / OpenMP races")
        return 2
    print("OK: coloured OpenMP is thread-count deterministic on this lattice")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
