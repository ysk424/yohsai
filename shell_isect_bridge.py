# SPDX-License-Identifier: GPL-3.0-or-later
"""Bridge to shell-isect (ZOZO-twin check + local-only fix).

Yohsai pins shell-isect **0.10.x**. Read shell-isect PROCEDURE.md before
changing how this module calls the DLL.

Host pipeline for Prepare for ZOZO:

  build cloth + body copies → check (cloth+body, ZOZO-twin) → fix → check
  → PASS (pairs == 0): continue ZOZO MCP
  → NG: report error kind + face-pair indices; stop (no MCP)

The Transfer-time twin includes STATIC body triangles as colliders (ppf
``fixed_scene_assemble``): cloth–cloth and cloth–body pairs count; body–body
pairs are skipped. Detection builds the same combined mesh here.

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
from mathutils import Vector
from mathutils.bvhtree import BVHTree


REQUIRED_MAJOR = 0
REQUIRED_MINOR = 10
# Cap for face-pair dump returned to the host status line.
_MAX_REPORT_PAIRS = 64
# Local cloth fix (host-side): push NG cloth verts outside the body.
# Matches ZOZO 1 mm contact gap * 1.1, plus a small pad so edge-tri clears.
_LOCAL_BODY_CLEARANCE_M = 0.0011
_LOCAL_BODY_PAD_M = 0.0005
_LOCAL_FIX_MAX_PASSES = 8
_LOCAL_CLOTH_SEPARATION_M = 0.0008


@dataclass(frozen=True)
class ShellIsectReport:
    available: bool
    version: str
    pairs_before: int
    pairs_after: int
    fix_status: str
    message: str
    # Face-index pairs from the *final* check (after fix), i < j.
    # When body is included, indices are on the combined mesh (cloth faces
    # first: 0 .. n_cloth_faces-1, then body).
    pairs: tuple[tuple[int, int], ...] = ()
    n_cloth_faces: int = 0

    @property
    def passed(self) -> bool:
        """True only when the twin-check ends with zero pairs."""
        return self.available and self.pairs_after == 0 and self.pairs_before >= 0

    def version_suffix(self) -> str:
        """Trailing token for status messages (always present when known)."""
        if self.version:
            return f"shell-isect {self.version}"
        return "shell-isect unavailable"

    def summary(self) -> str:
        if not self.available:
            return f"shell-isect: {self.message}"
        return (
            f"shell-isect {self.version}: pairs {self.pairs_before}->{self.pairs_after} "
            f"fix={self.fix_status}"
        )

    def _format_face(self, index: int) -> str:
        n = self.n_cloth_faces
        if n > 0 and index >= n:
            return f"b{index - n}"
        return f"c{index}"

    def error_report(self) -> str:
        """User-facing NG text for the status box (no internal tool names)."""
        if not self.available:
            return (
                f"ERROR: self-intersection check unavailable ({self.message}) "
                f"[{self.version_suffix()}]"
            )
        if self.pairs_before < 0 or self.pairs_after < 0:
            return (
                f"ERROR: self-intersection check failed ({self.message}) "
                f"[{self.version_suffix()}]"
            )
        # e.g. ERROR: self-intersection (tri-tri face pairs): pairs 1->1 fix=NOOP face_pairs: (c12,b3)
        text = (
            "ERROR: self-intersection (tri-tri face pairs): "
            f"pairs {self.pairs_before}->{self.pairs_after} fix={self.fix_status}"
        )
        if self.n_cloth_faces > 0:
            text += f" cloth_faces=0..{self.n_cloth_faces - 1}"
        if self.pairs:
            shown = self.pairs[:_MAX_REPORT_PAIRS]
            pair_txt = ", ".join(
                f"({self._format_face(a)},{self._format_face(b)})" for a, b in shown
            )
            if self.pairs_after > len(shown):
                pair_txt += f", ... (+{self.pairs_after - len(shown)} more)"
            text += f" face_pairs: {pair_txt}"
        text += f" [{self.version_suffix()}]"
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


def library_version() -> str:
    """Return the loaded DLL version string, or empty if unavailable."""
    lib = _load_library()
    if lib is None:
        return ""
    return lib.shell_isect_version().decode("utf-8", errors="replace")


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


def _combined_cloth_body_arrays(
    cloth: bpy.types.Object,
    body: bpy.types.Object,
) -> tuple[np.ndarray, np.ndarray, int, np.ndarray]:
    """Build PPF-style combined mesh: cloth faces then body faces.

    Returns (verts, faces, n_cloth_faces, is_collider) where is_collider is
    uint8 length n_faces (0 = dynamic cloth, 1 = static body).
    """
    c_verts, c_faces = _mesh_arrays_world(cloth)
    b_verts, b_faces = _mesh_arrays_world(body)
    n_cloth_faces = int(c_faces.shape[0])
    n_cloth_verts = int(c_verts.shape[0])
    if b_faces.shape[0] == 0:
        is_collider = np.zeros((n_cloth_faces,), dtype=np.uint8)
        return c_verts, c_faces, n_cloth_faces, is_collider

    verts = np.ascontiguousarray(np.vstack([c_verts, b_verts]), dtype=np.float64)
    shifted = b_faces + np.int32(n_cloth_verts)
    faces = np.ascontiguousarray(np.vstack([c_faces, shifted]), dtype=np.int32)
    is_collider = np.concatenate(
        [
            np.zeros((n_cloth_faces,), dtype=np.uint8),
            np.ones((b_faces.shape[0],), dtype=np.uint8),
        ]
    )
    return verts, faces, n_cloth_faces, is_collider


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


def _raw_check_pairs(
    lib,
    verts: np.ndarray,
    faces: np.ndarray,
    *,
    max_pairs: int,
) -> tuple[int, tuple[tuple[int, int], ...], int]:
    """Return (count, pairs, rc) without collider filtering."""
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


def _filter_collider_pairs(
    pairs: tuple[tuple[int, int], ...],
    is_collider: np.ndarray,
) -> tuple[tuple[int, int], ...]:
    """Drop body–body (collider × collider) pairs — same skip as ppf host check."""
    kept: list[tuple[int, int]] = []
    n = int(is_collider.shape[0])
    for i, j in pairs:
        if i < 0 or j < 0 or i >= n or j >= n:
            continue
        if is_collider[i] and is_collider[j]:
            continue
        kept.append((i, j))
    return tuple(kept)


def _check_pairs(
    lib,
    verts: np.ndarray,
    faces: np.ndarray,
    *,
    max_pairs: int = _MAX_REPORT_PAIRS,
    is_collider: np.ndarray | None = None,
) -> tuple[int, tuple[tuple[int, int], ...], int]:
    """Return (count, pairs, rc). count is total even if buffer overflowed.

    When ``is_collider`` is set (length n_faces, nonzero = STATIC body tri),
    pairs where both faces are colliders are removed so the host matches
    ppf ``fixed_scene_assemble`` (collider × collider skipped).
    """
    if is_collider is None:
        return _raw_check_pairs(lib, verts, faces, max_pairs=max_pairs)

    # Need every raw pair before filtering; body self-hits can fill a small buffer.
    total, _, rc = _raw_check_pairs(lib, verts, faces, max_pairs=0)
    if rc not in (0, 2):
        return total, (), rc
    if total <= 0:
        return 0, (), rc
    raw_total, raw_pairs, rc = _raw_check_pairs(lib, verts, faces, max_pairs=total)
    if rc not in (0, 2):
        return raw_total, (), rc
    filtered = _filter_collider_pairs(raw_pairs, is_collider)
    if max_pairs <= 0:
        return len(filtered), (), rc
    return len(filtered), filtered[:max_pairs], rc


def _body_bvh_world(body: bpy.types.Object) -> BVHTree | None:
    """World-space triangle BVH for the STATIC body copy."""
    b_verts, b_faces = _mesh_arrays_world(body)
    if b_faces.shape[0] == 0:
        return None
    points = [Vector((float(p[0]), float(p[1]), float(p[2]))) for p in b_verts]
    tris = [[int(i) for i in f] for f in b_faces]
    return BVHTree.FromPolygons(points, tris, all_triangles=True)


def _ng_cloth_faces_and_verts(
    pairs: tuple[tuple[int, int], ...],
    cloth_faces: np.ndarray,
    n_cloth_faces: int,
) -> tuple[set[int], set[int]]:
    """Cloth face / vertex indices involved in any reported pair."""
    ng_faces: set[int] = set()
    for i, j in pairs:
        if 0 <= i < n_cloth_faces:
            ng_faces.add(i)
        if 0 <= j < n_cloth_faces:
            ng_faces.add(j)
    ng_verts: set[int] = set()
    n_tris = int(cloth_faces.shape[0])
    for fi in ng_faces:
        if 0 <= fi < n_tris:
            ng_verts.update(int(v) for v in cloth_faces[fi])
    return ng_faces, ng_verts


def _grow_vert_ring(
    seeds: set[int], adjacency: list[set[int]], rings: int
) -> set[int]:
    region = set(seeds)
    for _ in range(max(0, rings)):
        grown = set(region)
        for v in region:
            grown.update(adjacency[v])
        region = grown
    return region


def _cloth_adjacency(cloth_faces: np.ndarray, n_verts: int) -> list[set[int]]:
    adj: list[set[int]] = [set() for _ in range(n_verts)]
    for f in cloth_faces:
        a, b, c = int(f[0]), int(f[1]), int(f[2])
        for u, v in ((a, b), (b, c), (c, a)):
            if 0 <= u < n_verts and 0 <= v < n_verts:
                adj[u].add(v)
                adj[v].add(u)
    return adj


def _push_verts_outside_body(
    cloth_verts: np.ndarray,
    ng_verts: set[int],
    body_bvh: BVHTree,
    clearance_m: float,
) -> tuple[np.ndarray, int]:
    """Push NG cloth verts to at least ``clearance_m`` outside the body surface."""
    trial = cloth_verts.copy()
    moved = 0
    for vi in ng_verts:
        if vi < 0 or vi >= trial.shape[0]:
            continue
        p = Vector((float(trial[vi, 0]), float(trial[vi, 1]), float(trial[vi, 2])))
        loc, normal, _face, _dist = body_bvh.find_nearest(p)
        if loc is None or normal is None:
            continue
        n = normal.normalized()
        if n.length < 1.0e-12:
            continue
        side = float((p - loc).dot(n))
        if side >= clearance_m:
            continue
        delta = clearance_m - side
        trial[vi, 0] += n.x * delta
        trial[vi, 1] += n.y * delta
        trial[vi, 2] += n.z * delta
        moved += 1
    return trial, moved


def _separate_cloth_cloth_verts(
    cloth_verts: np.ndarray,
    pairs: tuple[tuple[int, int], ...],
    cloth_faces: np.ndarray,
    n_cloth_faces: int,
    separation_m: float,
) -> tuple[np.ndarray, int]:
    """Small mutual push for cloth–cloth face pairs (topology unchanged)."""
    trial = cloth_verts.copy()
    moved_verts: set[int] = set()
    for i, j in pairs:
        if not (0 <= i < n_cloth_faces and 0 <= j < n_cloth_faces):
            continue
        fa = [int(v) for v in cloth_faces[i]]
        fb = [int(v) for v in cloth_faces[j]]
        pa = trial[fa].mean(axis=0)
        pb = trial[fb].mean(axis=0)
        d = pb - pa
        length = float(np.linalg.norm(d))
        if length < 1.0e-12:
            # Degenerate: use first triangle normal of face i.
            e1 = trial[fa[1]] - trial[fa[0]]
            e2 = trial[fa[2]] - trial[fa[0]]
            n = np.cross(e1, e2)
            ln = float(np.linalg.norm(n))
            if ln < 1.0e-12:
                continue
            direction = n / ln
        else:
            direction = d / length
        half = 0.5 * separation_m
        for vi in fa:
            trial[vi] = trial[vi] - direction * half
            moved_verts.add(vi)
        for vi in fb:
            trial[vi] = trial[vi] + direction * half
            moved_verts.add(vi)
    return trial, len(moved_verts)


def _local_fix_cloth_against_body(
    lib,
    cloth: bpy.types.Object,
    body: bpy.types.Object,
    cloth_verts: np.ndarray,
    cloth_faces: np.ndarray,
    n_cloth_faces: int,
    pairs: tuple[tuple[int, int], ...],
    pairs_before: int,
) -> tuple[np.ndarray, int, tuple[tuple[int, int], ...], str, str]:
    """Local-only NG pocket fix: push cloth verts, never body; re-check; rollback if worse.

    Returns (final_cloth_verts, pairs_after, pairs, fix_status, message).
    """
    body_bvh = _body_bvh_world(body)
    if body_bvh is None:
        return cloth_verts, pairs_before, pairs, "NOOP", "no body triangles"

    adjacency = _cloth_adjacency(cloth_faces, int(cloth_verts.shape[0]))
    working = cloth_verts.copy()
    current_pairs = pairs
    current_count = pairs_before
    clearance = _LOCAL_BODY_CLEARANCE_M + _LOCAL_BODY_PAD_M
    applied_any = False

    for pass_index in range(_LOCAL_FIX_MAX_PASSES):
        if current_count <= 0:
            break
        _ng_faces, ng_verts = _ng_cloth_faces_and_verts(
            current_pairs, cloth_faces, n_cloth_faces
        )
        if not ng_verts:
            break
        # Widen after the first pass if residuals remain.
        rings = 1 if pass_index > 0 else 0
        region = _grow_vert_ring(ng_verts, adjacency, rings)

        trial, moved_body = _push_verts_outside_body(
            working, region, body_bvh, clearance
        )
        # Cloth–cloth residual pairs: gentle mutual separation, then re-clamp.
        cloth_cloth = tuple(
            (a, b)
            for a, b in current_pairs
            if a < n_cloth_faces and b < n_cloth_faces
        )
        moved_sep = 0
        if cloth_cloth:
            trial, moved_sep = _separate_cloth_cloth_verts(
                trial,
                cloth_cloth,
                cloth_faces,
                n_cloth_faces,
                _LOCAL_CLOTH_SEPARATION_M,
            )
            # Separation can push into the body; re-assert clearance on region.
            trial, _ = _push_verts_outside_body(
                trial, region, body_bvh, clearance
            )

        if moved_body == 0 and moved_sep == 0:
            break

        # Build combined mesh from trial cloth verts (body unchanged).
        b_verts, b_faces = _mesh_arrays_world(body)
        n_cloth_verts = int(trial.shape[0])
        comb_verts = np.ascontiguousarray(np.vstack([trial, b_verts]), dtype=np.float64)
        shifted = b_faces + np.int32(n_cloth_verts)
        comb_faces = np.ascontiguousarray(
            np.vstack([cloth_faces, shifted]), dtype=np.int32
        )
        is_collider = np.concatenate(
            [
                np.zeros((n_cloth_faces,), dtype=np.uint8),
                np.ones((b_faces.shape[0],), dtype=np.uint8),
            ]
        )
        # Full filtered count is returned even when the pair buffer is capped.
        after_count, after_pairs, rc = _check_pairs(
            lib,
            comb_verts,
            comb_faces,
            max_pairs=max(int(current_count), _MAX_REPORT_PAIRS),
            is_collider=is_collider,
        )
        if rc not in (0, 2):
            return (
                working,
                current_count,
                current_pairs,
                "FAILED",
                f"re-check failed rc={rc}",
            )
        if after_count > current_count:
            # Rollback this step; do not ship a worse mesh.
            return (
                working,
                current_count,
                current_pairs,
                "APPLIED" if applied_any else "FAILED",
                "local push increased pairs; rolled back step",
            )

        working = trial
        applied_any = True
        current_count = after_count
        current_pairs = after_pairs
        if after_count == 0:
            _apply_world_verts(cloth, working)
            return working, 0, (), "CLEARED", "local body push cleared pairs"

        clearance += _LOCAL_BODY_PAD_M

    if applied_any:
        _apply_world_verts(cloth, working)
        status = "APPLIED" if current_count > 0 else "CLEARED"
        msg = (
            "pairs remain after local push"
            if current_count > 0
            else "local body push cleared pairs"
        )
        return working, current_count, current_pairs, status, msg
    return working, current_count, current_pairs, "NOOP", "local push moved nothing"


def run_check_and_fix(
    cloth: bpy.types.Object,
    body: bpy.types.Object | None = None,
) -> ShellIsectReport:
    """Run shell-isect: check → local fix → check.

    When ``body`` is provided (Prepare path), the check mesh is cloth+body
    combined with STATIC body faces marked as colliders (PPF twin). Host-side
    local fix pushes only NG cloth vertices outside the body (and gently
    separates cloth–cloth pockets); topology and body verts are never edited.
    """
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
            n_cloth_faces=0,
        )

    version = lib.shell_isect_version().decode("utf-8", errors="replace")
    cloth_verts, cloth_faces = _mesh_arrays_world(cloth)
    n_cloth_faces = int(cloth_faces.shape[0])

    if body is not None:
        verts, faces, n_cloth_faces, is_collider = _combined_cloth_body_arrays(
            cloth, body
        )
    else:
        verts, faces = cloth_verts, cloth_faces
        is_collider = None

    if faces.shape[0] < 2:
        return ShellIsectReport(
            available=True,
            version=version,
            pairs_before=0,
            pairs_after=0,
            fix_status="NOOP",
            message="fewer than 2 triangles",
            pairs=(),
            n_cloth_faces=n_cloth_faces,
        )

    # --- 1) check (twin mesh) ---
    before_count, pairs_before, rc = _check_pairs(
        lib,
        verts,
        faces,
        max_pairs=0 if is_collider is not None else _MAX_REPORT_PAIRS,
        is_collider=is_collider,
    )
    if rc not in (0, 2):
        return ShellIsectReport(
            available=True,
            version=version,
            pairs_before=-1,
            pairs_after=-1,
            fix_status="FAILED",
            message=f"check failed rc={rc}",
            pairs=(),
            n_cloth_faces=n_cloth_faces,
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
            n_cloth_faces=n_cloth_faces,
        )

    # Fetch full pair list for the local fix pocket.
    before_count, pairs_before, rc = _check_pairs(
        lib,
        verts,
        faces,
        max_pairs=max(before_count, 1),
        is_collider=is_collider,
    )
    if rc not in (0, 2):
        return ShellIsectReport(
            available=True,
            version=version,
            pairs_before=-1,
            pairs_after=-1,
            fix_status="FAILED",
            message=f"check pairs failed rc={rc}",
            pairs=(),
            n_cloth_faces=n_cloth_faces,
        )

    # --- 2) local fix (cloth verts only) ---
    fix_name = "NOOP"
    message = "pairs remain"
    after_count = before_count
    pairs_after = pairs_before

    if body is not None:
        (
            _working,
            after_count,
            pairs_after,
            fix_name,
            message,
        ) = _local_fix_cloth_against_body(
            lib,
            cloth,
            body,
            cloth_verts,
            cloth_faces,
            n_cloth_faces,
            pairs_before,
            before_count,
        )
        # Re-read report pairs for UI (cap).
        if after_count > _MAX_REPORT_PAIRS:
            pairs_after = pairs_after[:_MAX_REPORT_PAIRS]
    else:
        # Cloth-only path: keep DLL local-fix stub behaviour.
        n_verts = ctypes.c_int32(cloth_verts.shape[0])
        n_faces_cloth = ctypes.c_int32(cloth_faces.shape[0])
        v_ptr = cloth_verts.ctypes.data_as(ctypes.POINTER(ctypes.c_double))
        f_ptr = cloth_faces.ctypes.data_as(ctypes.POINTER(ctypes.c_int32))
        out = np.empty_like(cloth_verts)
        out_ptr = out.ctypes.data_as(ctypes.POINTER(ctypes.c_double))
        before2 = ctypes.c_int32(0)
        after_fix = ctypes.c_int32(0)
        status = ctypes.c_int32(3)
        rc = lib.shell_isect_fix(
            v_ptr,
            out_ptr,
            n_verts,
            f_ptr,
            n_faces_cloth,
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
                n_cloth_faces=n_cloth_faces,
            )
        fix_name = _FIX_STATUS.get(int(status.value), f"STATUS_{int(status.value)}")
        if fix_name in ("APPLIED", "CLEARED"):
            _apply_world_verts(cloth, out)
            cloth_verts = out
        after_count, pairs_after, rc = _check_pairs(
            lib, cloth_verts, cloth_faces, is_collider=None
        )
        if rc not in (0, 2):
            return ShellIsectReport(
                available=True,
                version=version,
                pairs_before=before_count,
                pairs_after=-1,
                fix_status=fix_name,
                message=f"re-check failed rc={rc}",
                pairs=(),
                n_cloth_faces=n_cloth_faces,
            )
        message = "ok" if after_count == 0 else "pairs remain after fix"

    return ShellIsectReport(
        available=True,
        version=version,
        pairs_before=before_count,
        pairs_after=after_count,
        fix_status=fix_name,
        message=message if after_count else ("ok" if fix_name == "CLEARED" else message),
        pairs=pairs_after[:_MAX_REPORT_PAIRS],
        n_cloth_faces=n_cloth_faces,
    )
