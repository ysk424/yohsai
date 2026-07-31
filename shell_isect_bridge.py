# SPDX-License-Identifier: GPL-3.0-or-later
"""Bridge to shell-isect (ZOZO-twin check + local-only fix).

Yohsai pins shell-isect **0.10.x**. Read shell-isect PROCEDURE.md before
changing how this module calls the DLL.

Host pipeline for Prepare for ZOZO:

  build cloth mesh → check → fix → check again
  → PASS (pairs == 0): continue ZOZO export / MCP
  → NG: report error kind + face-pair indices; stop (no MCP)

Environment:
  SHELL_ISECT_DLL  full path to shell_isect.dll (Windows) / libshell_isect.so
"""

from __future__ import annotations

from dataclasses import dataclass
import ctypes
import os
from pathlib import Path

import bpy
import numpy as np


REQUIRED_MAJOR = 0
REQUIRED_MINOR = 10
# Cap for face-pair dump returned to the host status line.
_MAX_REPORT_PAIRS = 64


@dataclass(frozen=True)
class ShellIsectReport:
    available: bool
    version: str
    pairs_before: int
    pairs_after: int
    fix_status: str
    message: str
    # Face-index pairs from the *final* check (after fix), i < j.
    pairs: tuple[tuple[int, int], ...] = ()

    @property
    def passed(self) -> bool:
        """True only when the twin-check ends with zero pairs."""
        return self.available and self.pairs_after == 0 and self.pairs_before >= 0

    def summary(self) -> str:
        if not self.available:
            return f"shell-isect: {self.message}"
        return (
            f"shell-isect {self.version}: pairs {self.pairs_before}->{self.pairs_after} "
            f"fix={self.fix_status}"
        )

    def error_report(self) -> str:
        """User-facing NG text for the status box (no internal tool names)."""
        if not self.available:
            return f"ERROR: self-intersection check unavailable ({self.message})"
        if self.pairs_before < 0 or self.pairs_after < 0:
            return f"ERROR: self-intersection check failed ({self.message})"
        # e.g. ERROR: self-intersection (tri-tri face pairs): pairs 1->1 fix=NOOP face_pairs: (a,b)
        text = (
            "ERROR: self-intersection (tri-tri face pairs): "
            f"pairs {self.pairs_before}->{self.pairs_after} fix={self.fix_status}"
        )
        if self.pairs:
            shown = self.pairs[:_MAX_REPORT_PAIRS]
            pair_txt = ", ".join(f"({a},{b})" for a, b in shown)
            if self.pairs_after > len(shown):
                pair_txt += f", ... (+{self.pairs_after - len(shown)} more)"
            text += f" face_pairs: {pair_txt}"
        return text


_lib = None
_lib_error: str | None = None


def _candidate_dll_paths() -> list[Path]:
    paths: list[Path] = []
    env = os.environ.get("SHELL_ISECT_DLL", "").strip()
    if env:
        paths.append(Path(env))
    here = Path(__file__).resolve().parent
    paths.extend(
        [
            here / "bin" / "shell_isect.dll",
            here / "shell_isect.dll",
            Path(r"C:\Users\azoo\git\shell-isect\build\Release\shell_isect.dll"),
            Path(r"C:\Users\azoo\git\shell-isect\build\shell_isect.dll"),
        ]
    )
    return paths


def _load_library():
    global _lib, _lib_error
    if _lib is not None:
        return _lib
    if _lib_error is not None:
        return None
    last = "shell_isect library not found"
    for path in _candidate_dll_paths():
        if not path.is_file():
            continue
        try:
            lib = ctypes.CDLL(str(path))
        except OSError as exc:
            last = f"cannot load {path}: {exc}"
            continue
        lib.shell_isect_version.restype = ctypes.c_char_p
        lib.shell_isect_version_components.argtypes = [
            ctypes.POINTER(ctypes.c_int32),
            ctypes.POINTER(ctypes.c_int32),
            ctypes.POINTER(ctypes.c_int32),
        ]
        lib.shell_isect_check.argtypes = [
            ctypes.POINTER(ctypes.c_double),
            ctypes.c_int32,
            ctypes.POINTER(ctypes.c_int32),
            ctypes.c_int32,
            ctypes.POINTER(ctypes.c_int32),
            ctypes.c_int32,
            ctypes.POINTER(ctypes.c_int32),
        ]
        lib.shell_isect_check.restype = ctypes.c_int
        lib.shell_isect_fix.argtypes = [
            ctypes.POINTER(ctypes.c_double),
            ctypes.POINTER(ctypes.c_double),
            ctypes.c_int32,
            ctypes.POINTER(ctypes.c_int32),
            ctypes.c_int32,
            ctypes.POINTER(ctypes.c_int32),
            ctypes.c_int32,
            ctypes.POINTER(ctypes.c_int32),
            ctypes.POINTER(ctypes.c_int32),
            ctypes.c_int32,
            ctypes.POINTER(ctypes.c_int32),
            ctypes.POINTER(ctypes.c_int32),
        ]
        lib.shell_isect_fix.restype = ctypes.c_int

        major = ctypes.c_int32()
        minor = ctypes.c_int32()
        patch = ctypes.c_int32()
        lib.shell_isect_version_components(
            ctypes.byref(major), ctypes.byref(minor), ctypes.byref(patch)
        )
        if major.value != REQUIRED_MAJOR or minor.value < REQUIRED_MINOR:
            last = (
                f"{path} is {major.value}.{minor.value}.{patch.value}; "
                f"Yohsai requires {REQUIRED_MAJOR}.{REQUIRED_MINOR}.x"
            )
            continue
        _lib = lib
        _lib_error = None
        return _lib
    _lib_error = last
    return None


def _mesh_arrays_world(obj: bpy.types.Object) -> tuple[np.ndarray, np.ndarray]:
    """Return (V float64 Nx3 world, F int32 Mx3) for twin-check.

    Triangulation matches ZOZO/ppf encoder: Blender ``loop_triangles`` (see
    ppf-contact-solver ``numpy_mesh_utils.loop_triangle_indices``). A naive
    fan-from-first-vertex diverges on quads/N-gons and was a primary source of
    false PASS against Transfer's 9 tri-tri self-intersections.
    """
    mesh = obj.data
    n = len(mesh.vertices)
    local = np.empty((n, 3), dtype=np.float64)
    mesh.vertices.foreach_get("co", local.ravel())
    matrix = np.asarray([tuple(row) for row in obj.matrix_world], dtype=np.float64)
    verts = np.ascontiguousarray(local @ matrix[:3, :3].T + matrix[:3, 3])

    mesh.calc_loop_triangles()
    n_tri = len(mesh.loop_triangles)
    if n_tri == 0:
        return verts, np.zeros((0, 3), dtype=np.int32)
    flat = np.empty(n_tri * 3, dtype=np.int32)
    mesh.loop_triangles.foreach_get("vertices", flat)
    faces = np.ascontiguousarray(flat.reshape(n_tri, 3), dtype=np.int32)
    return verts, faces


def _apply_world_verts(cloth: bpy.types.Object, world_verts: np.ndarray) -> None:
    inverse = cloth.matrix_world.inverted_safe()
    inv = np.asarray([tuple(row) for row in inverse], dtype=np.float64)
    local = world_verts @ inv[:3, :3].T + inv[:3, 3]
    flat = np.ascontiguousarray(local, dtype=np.float32)
    cloth.data.vertices.foreach_set("co", flat.ravel())
    cloth.data.update()


_FIX_STATUS = {
    0: "NOOP",
    1: "APPLIED",
    2: "CLEARED",
    3: "FAILED",
}


def _check_pairs(
    lib,
    verts: np.ndarray,
    faces: np.ndarray,
    *,
    max_pairs: int = _MAX_REPORT_PAIRS,
) -> tuple[int, tuple[tuple[int, int], ...], int]:
    """Return (count, pairs, rc). count is total even if buffer overflowed."""
    n_verts = ctypes.c_int32(verts.shape[0])
    n_faces = ctypes.c_int32(faces.shape[0])
    v_ptr = verts.ctypes.data_as(ctypes.POINTER(ctypes.c_double))
    f_ptr = faces.ctypes.data_as(ctypes.POINTER(ctypes.c_int32))
    count = ctypes.c_int32(0)
    if max_pairs <= 0:
        rc = lib.shell_isect_check(
            v_ptr, n_verts, f_ptr, n_faces, None, 0, ctypes.byref(count)
        )
        return int(count.value), (), rc

    buf = (ctypes.c_int32 * (max_pairs * 2))()
    rc = lib.shell_isect_check(
        v_ptr, n_verts, f_ptr, n_faces, buf, max_pairs, ctypes.byref(count)
    )
    total = int(count.value)
    n_write = min(total, max_pairs)
    pairs = tuple((int(buf[2 * i]), int(buf[2 * i + 1])) for i in range(n_write))
    return total, pairs, rc


def run_check_and_fix(cloth: bpy.types.Object) -> ShellIsectReport:
    """Run shell-isect: check → fix → check. Apply verts when fix moves them."""
    lib = _load_library()
    if lib is None:
        return ShellIsectReport(
            available=False,
            version="",
            pairs_before=-1,
            pairs_after=-1,
            fix_status="UNAVAILABLE",
            message=_lib_error or "unavailable",
            pairs=(),
        )

    version = lib.shell_isect_version().decode("utf-8", errors="replace")
    verts, faces = _mesh_arrays_world(cloth)
    if faces.shape[0] < 2:
        return ShellIsectReport(
            available=True,
            version=version,
            pairs_before=0,
            pairs_after=0,
            fix_status="NOOP",
            message="fewer than 2 triangles",
            pairs=(),
        )

    # --- 1) check ---
    before_count, _pairs_before, rc = _check_pairs(lib, verts, faces)
    if rc not in (0, 2):
        return ShellIsectReport(
            available=True,
            version=version,
            pairs_before=-1,
            pairs_after=-1,
            fix_status="FAILED",
            message=f"check failed rc={rc}",
            pairs=(),
        )

    if before_count == 0:
        return ShellIsectReport(
            available=True,
            version=version,
            pairs_before=0,
            pairs_after=0,
            fix_status="NOOP",
            message="clean",
            pairs=(),
        )

    # --- 2) fix ---
    n_verts = ctypes.c_int32(verts.shape[0])
    n_faces = ctypes.c_int32(faces.shape[0])
    v_ptr = verts.ctypes.data_as(ctypes.POINTER(ctypes.c_double))
    f_ptr = faces.ctypes.data_as(ctypes.POINTER(ctypes.c_int32))
    out = np.empty_like(verts)
    out_ptr = out.ctypes.data_as(ctypes.POINTER(ctypes.c_double))
    before2 = ctypes.c_int32(0)
    after_fix = ctypes.c_int32(0)
    status = ctypes.c_int32(3)
    rc = lib.shell_isect_fix(
        v_ptr,
        out_ptr,
        n_verts,
        f_ptr,
        n_faces,
        None,
        0,
        ctypes.byref(before2),
        None,
        0,
        ctypes.byref(after_fix),
        ctypes.byref(status),
    )
    if rc not in (0, 2):
        return ShellIsectReport(
            available=True,
            version=version,
            pairs_before=before_count,
            pairs_after=-1,
            fix_status="FAILED",
            message=f"fix failed rc={rc}",
            pairs=(),
        )

    fix_name = _FIX_STATUS.get(int(status.value), f"STATUS_{int(status.value)}")
    working = verts
    if fix_name in ("APPLIED", "CLEARED"):
        # Local-fix wrote a new buffer; always apply when status claims a write.
        _apply_world_verts(cloth, out)
        working = out

    # --- 3) check again (on the mesh that will be handed to ZOZO) ---
    after_count, pairs_after, rc = _check_pairs(lib, working, faces)
    if rc not in (0, 2):
        return ShellIsectReport(
            available=True,
            version=version,
            pairs_before=before_count,
            pairs_after=-1,
            fix_status=fix_name,
            message=f"re-check failed rc={rc}",
            pairs=(),
        )

    return ShellIsectReport(
        available=True,
        version=version,
        pairs_before=int(before2.value) if before2.value else before_count,
        pairs_after=after_count,
        fix_status=fix_name,
        message="ok" if after_count == 0 else "pairs remain after fix",
        pairs=pairs_after,
    )
