# SPDX-License-Identifier: GPL-3.0-or-later
"""Optional bridge to the shell-isect library (check + local-fix).

Yohsai pins shell-isect **0.10.x**. Read shell-isect PROCEDURE.md before
changing how this module calls the DLL.

Environment:
  SHELL_ISECT_DLL  full path to shell_isect.dll (Windows) / libshell_isect.so

If the library is missing or too old, callers get a soft result and must not
treat the mesh as Transfer-clean based on this bridge alone.
"""

from __future__ import annotations

from dataclasses import dataclass
import ctypes
import os
from pathlib import Path

import bpy
import numpy as np


# Host pin: first release that exposes check + fix stub.
REQUIRED_MAJOR = 0
REQUIRED_MINOR = 10


@dataclass(frozen=True)
class ShellIsectReport:
    available: bool
    version: str
    pairs_before: int
    pairs_after: int
    fix_status: str
    message: str

    def summary(self) -> str:
        if not self.available:
            return f"shell-isect: {self.message}"
        return (
            f"shell-isect {self.version}: pairs {self.pairs_before}->{self.pairs_after} "
            f"fix={self.fix_status}"
        )


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
    """Return (V float64 Nx3 world, F int32 Mx3) triangulating ngons as fans."""
    mesh = obj.data
    n = len(mesh.vertices)
    local = np.empty((n, 3), dtype=np.float64)
    mesh.vertices.foreach_get("co", local.ravel())
    matrix = np.asarray([tuple(row) for row in obj.matrix_world], dtype=np.float64)
    verts = np.ascontiguousarray(local @ matrix[:3, :3].T + matrix[:3, 3])
    faces: list[tuple[int, int, int]] = []
    for poly in mesh.polygons:
        idx = [int(v) for v in poly.vertices]
        if len(idx) < 3:
            continue
        for i in range(1, len(idx) - 1):
            faces.append((idx[0], idx[i], idx[i + 1]))
    if not faces:
        return verts, np.zeros((0, 3), dtype=np.int32)
    return verts, np.ascontiguousarray(faces, dtype=np.int32)


_FIX_STATUS = {
    0: "NOOP",
    1: "APPLIED",
    2: "CLEARED",
    3: "FAILED",
}


def run_check_and_fix(cloth: bpy.types.Object) -> ShellIsectReport:
    """Run shell-isect check → fix → (report). Does not write verts on NOOP stub."""
    lib = _load_library()
    if lib is None:
        return ShellIsectReport(
            available=False,
            version="",
            pairs_before=-1,
            pairs_after=-1,
            fix_status="UNAVAILABLE",
            message=_lib_error or "unavailable",
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
        )

    n_verts = ctypes.c_int32(verts.shape[0])
    n_faces = ctypes.c_int32(faces.shape[0])
    v_ptr = verts.ctypes.data_as(ctypes.POINTER(ctypes.c_double))
    f_ptr = faces.ctypes.data_as(ctypes.POINTER(ctypes.c_int32))

    before = ctypes.c_int32(0)
    rc = lib.shell_isect_check(v_ptr, n_verts, f_ptr, n_faces, None, 0, ctypes.byref(before))
    if rc not in (0, 2):
        return ShellIsectReport(
            available=True,
            version=version,
            pairs_before=-1,
            pairs_after=-1,
            fix_status="FAILED",
            message=f"check failed rc={rc}",
        )

    out = np.empty_like(verts)
    out_ptr = out.ctypes.data_as(ctypes.POINTER(ctypes.c_double))
    after = ctypes.c_int32(0)
    status = ctypes.c_int32(3)
    before2 = ctypes.c_int32(0)
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
        ctypes.byref(after),
        ctypes.byref(status),
    )
    if rc not in (0, 2):
        return ShellIsectReport(
            available=True,
            version=version,
            pairs_before=int(before.value),
            pairs_after=-1,
            fix_status="FAILED",
            message=f"fix failed rc={rc}",
        )

    fix_name = _FIX_STATUS.get(int(status.value), f"STATUS_{int(status.value)}")
    # v0.10 stub never moves; if a future fix CLEARED/APPLIED with writes, apply world→local.
    if fix_name in ("APPLIED", "CLEARED") and int(after.value) == 0:
        inverse = cloth.matrix_world.inverted_safe()
        inv = np.asarray([tuple(row) for row in inverse], dtype=np.float64)
        local = out @ inv[:3, :3].T + inv[:3, 3]
        flat = np.ascontiguousarray(local, dtype=np.float32)
        cloth.data.vertices.foreach_set("co", flat.ravel())
        cloth.data.update()

    return ShellIsectReport(
        available=True,
        version=version,
        pairs_before=int(before2.value if before2.value else before.value),
        pairs_after=int(after.value),
        fix_status=fix_name,
        message="ok",
    )
