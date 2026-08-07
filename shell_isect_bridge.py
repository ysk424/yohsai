# SPDX-License-Identifier: GPL-3.0-or-later
"""Bridge to shell-isect (ZOZO-twin check + local-only fix).

Yohsai pins shell-isect **0.10+** (ships **0.11.1+** with local-fix + unsliver).
Read shell-isect PROCEDURE.md before changing how this module calls the DLL.

Host pipeline for Prepare for ZOZO (strict stages):

  build cloth + body copies
  → **CHECK 1**  (shell_isect_check — detect only)
  → **FIX**      (DLL fix and/or host local push — geometry only)
  → **CHECK 2**  (shell_isect_check again — detect only; gate for MCP)
  → PASS (check2 pairs == 0): continue ZOZO MCP
  → NG: report check1 | fix | check2 + face-pair indices; stop (no MCP)

When CHECK 1 is already clean, FIX is skipped and CHECK 2 is not required
(report still records check1 = check2 = 0). When CHECK 1 finds pairs, FIX
always runs and CHECK 2 always runs afterward — even if FIX is NOOP — so
debug always sees two independent checker results around the fix.

Full ZOZO-twin mode (``include_body=True``, the default) matches Transfer-time
colliders (ppf ``fixed_scene_assemble``): cloth–cloth and cloth–body pairs
count; body–body pairs are skipped.

**Only the body under the garment is handed to the DLL.** ``shell_isect_check``
takes one mesh and finds every self-intersection in it, so concatenating cloth
and body makes it test the body against itself as well. On the reference
character that is the whole cost: the body's own self-test is 203 s of a 207 s
run, and every one of the 5943 pairs it produces is then discarded, because
ZOZO skips collider × collider too. The DLL cannot be told otherwise -- it
exports no group or mask argument -- so the body is cropped here, before the
call, to the triangles whose bounding boxes share a grid cell with a cloth
triangle's. A triangle cannot leave its own bounding box, so no cloth–body pair
can be lost; measured on the reference garment the crop keeps 12% of the body,
returns the same 28 pairs face-for-face, and takes 5.6 s instead of 207 s.

That is what makes ``include_body`` affordable enough to be the default. It was
off because it cost minutes, which meant the check that matters -- cloth against
the collider it will actually be solved against -- was the one nobody ran.

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
_LOCAL_FIX_MAX_PASSES = 12
_LOCAL_CLOTH_SEPARATION_M = 0.0008
# Cloth–body face-pair push (only cloth moves). Vertex-outward alone misses
# grazing edge–tri pierces where every cloth corner is already outside.
_LOCAL_CLOTH_BODY_SEP_M = 0.0008
_LOCAL_CLOTH_BODY_SEP_GROW_M = 0.0004
# Match shell-isect 0.11.1 absolute floors: never ship knife-edge triangles.
_LOCAL_MIN_EDGE_M = 5.0e-5
_LOCAL_MIN_AREA_M2 = 1.0e-10
_LOCAL_UNSLIVER_ITERS = 4
# Broad-phase cell for cropping the body. Only speed depends on it; the test is
# bounding-box against cell either way, so correctness does not.
_CROP_CELL_M = 0.01
_CROP_BASE = 1 << 12
_CROP_HALF = _CROP_BASE // 2
# Pair buffer asked for on the first checker call. Big enough that a real scene
# never needs the overflow retry, small enough to allocate without thinking.
_PAIR_BUDGET = 1 << 16


@dataclass(frozen=True)
class ShellIsectReport:
    available: bool
    version: str
    # CHECK 1 pair count (before FIX). -1 = checker error / unavailable.
    pairs_before: int
    # CHECK 2 pair count (after FIX). Same as pairs_before when FIX was skipped
    # because CHECK 1 was already clean.
    pairs_after: int
    fix_status: str
    message: str
    # Face-index pairs from CHECK 2 (or CHECK 1 when clean / pre-fix fail).
    # When body is included, indices are on the combined mesh (cloth faces
    # first: 0 .. n_cloth_faces-1, then body).
    pairs: tuple[tuple[int, int], ...] = ()
    n_cloth_faces: int = 0
    # False = cloth-only (default, fast). True = cloth+body ZOZO twin (slow).
    include_body: bool = False
    # How many independent checker stages ran (1 = clean early exit, 2 = full).
    checks_run: int = 0
    # True when the full check→fix→check path ran (CHECK 1 found pairs).
    fix_attempted: bool = False
    # Body triangles actually handed to the DLL, and how many the body has.
    # The gap between them is the crop, and it is the whole runtime story.
    body_faces_tested: int = 0
    body_faces_total: int = 0

    @property
    def passed(self) -> bool:
        """True only when the final checker stage ends with zero pairs."""
        return self.available and self.pairs_after == 0 and self.pairs_before >= 0

    def version_suffix(self) -> str:
        """Trailing token for status messages (always present when known)."""
        mode = "cloth+body" if self.include_body else "cloth-only"
        if self.version:
            return f"shell-isect {self.version} {mode}"
        return f"shell-isect unavailable {mode}"

    def pipeline_token(self) -> str:
        """Short stage summary for status / custom properties."""
        if not self.available:
            return "unavailable"
        if self.checks_run <= 1 and self.pairs_before == 0:
            return "check1=0 (clean; fix skipped)"
        return (
            f"check1={self.pairs_before} fix={self.fix_status} "
            f"check2={self.pairs_after}"
        )

    def summary(self) -> str:
        if not self.available:
            return f"shell-isect: {self.message}"
        mode = "cloth+body" if self.include_body else "cloth-only"
        crop = ""
        if self.include_body and self.body_faces_total:
            crop = (
                f", body {self.body_faces_tested}/{self.body_faces_total} tris"
            )
        return f"shell-isect {self.version} ({mode}{crop}): {self.pipeline_token()}"

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
        # e.g. ERROR: self-intersection check1=1 fix=NOOP check2=1 face_pairs: ...
        text = (
            "ERROR: self-intersection (tri-tri face pairs): "
            f"{self.pipeline_token()}"
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


@dataclass(frozen=True)
class _BodyProxy:
    """The part of the STATIC body that could possibly touch the cloth.

    ``face_index`` maps each proxy triangle back to its index in the whole
    body, so a reported pair still names a triangle the operator can find.
    """

    verts: np.ndarray
    faces: np.ndarray
    face_index: np.ndarray
    kept: int
    total: int


def _cell_keys(triangles: np.ndarray) -> tuple[np.ndarray, np.ndarray | None]:
    """(triangle index, packed cell) for every cell a triangle's box touches.

    Returns ``None`` for the keys when the mesh lies outside the packing range,
    which is the signal to skip cropping rather than crop wrongly.
    """
    low = np.floor(triangles.min(axis=1) / _CROP_CELL_M).astype(np.int64)
    high = np.floor(triangles.max(axis=1) / _CROP_CELL_M).astype(np.int64)
    if len(low) and max(abs(int(low.min())), abs(int(high.max()))) >= _CROP_HALF - 1:
        return np.zeros(0, dtype=np.int64), None
    span = high - low + 1
    counts = span.prod(axis=1)
    index = np.repeat(np.arange(len(triangles), dtype=np.int64), counts)
    starts = np.concatenate([np.zeros(1, dtype=np.int64), counts.cumsum()[:-1]])
    within = np.arange(int(counts.sum()), dtype=np.int64) - np.repeat(starts, counts)
    span_y, span_z = span[index, 1], span[index, 2]
    x = low[index, 0] + within // (span_y * span_z)
    y = low[index, 1] + (within // span_z) % span_y
    z = low[index, 2] + within % span_z
    return index, ((x + _CROP_HALF) * _CROP_BASE + (y + _CROP_HALF)) * _CROP_BASE + (
        z + _CROP_HALF
    )


def _body_faces_near_cloth(
    cloth_triangles: np.ndarray, body_triangles: np.ndarray
) -> np.ndarray:
    """Body triangles whose box shares a cell with some cloth triangle's box.

    Conservative: two triangles that cross each other must share a cell, so
    this can keep a triangle that turns out not to cross and can never drop one
    that does. The cloth cells are grown by one ring first, which leaves a cell
    of slack for the local fix to push cloth into without re-cropping.
    """
    total = int(body_triangles.shape[0])
    if not total or not len(cloth_triangles):
        return np.ones(total, dtype=bool)
    _cloth_index, cloth_keys = _cell_keys(cloth_triangles)
    body_index, body_keys = _cell_keys(body_triangles)
    if cloth_keys is None or body_keys is None:
        return np.ones(total, dtype=bool)
    ring = np.asarray(
        [
            (dx * _CROP_BASE + dy) * _CROP_BASE + dz
            for dx in (-1, 0, 1)
            for dy in (-1, 0, 1)
            for dz in (-1, 0, 1)
        ],
        dtype=np.int64,
    )
    wanted = np.unique((np.unique(cloth_keys)[:, None] + ring[None, :]).ravel())
    keep = np.zeros(total, dtype=bool)
    keep[body_index[np.isin(body_keys, wanted)]] = True
    return keep


def _body_proxy(
    cloth_verts: np.ndarray, cloth_faces: np.ndarray, body: bpy.types.Object
) -> _BodyProxy | None:
    body_verts, body_faces = _mesh_arrays_world(body)
    if body_faces.shape[0] == 0:
        return None
    keep = _body_faces_near_cloth(cloth_verts[cloth_faces], body_verts[body_faces])
    subset = body_faces[keep]
    used = np.unique(subset)
    remap = np.full(body_verts.shape[0], -1, dtype=np.int64)
    remap[used] = np.arange(used.shape[0], dtype=np.int64)
    return _BodyProxy(
        verts=np.ascontiguousarray(body_verts[used], dtype=np.float64),
        faces=np.ascontiguousarray(remap[subset], dtype=np.int32).reshape((-1, 3)),
        face_index=np.flatnonzero(keep).astype(np.int64),
        kept=int(keep.sum()),
        total=int(body_faces.shape[0]),
    )


def _combined_cloth_body_arrays(
    cloth_verts: np.ndarray,
    cloth_faces: np.ndarray,
    proxy: _BodyProxy | None,
) -> tuple[np.ndarray, np.ndarray, int, np.ndarray]:
    """Build the PPF-style combined mesh: cloth faces, then proxy body faces.

    Returns (verts, faces, n_cloth_faces, is_collider) where is_collider is
    uint8 length n_faces (0 = dynamic cloth, 1 = static body).
    """
    n_cloth_faces = int(cloth_faces.shape[0])
    if proxy is None or proxy.faces.shape[0] == 0:
        return (
            cloth_verts,
            cloth_faces,
            n_cloth_faces,
            np.zeros((n_cloth_faces,), dtype=np.uint8),
        )
    verts = np.ascontiguousarray(
        np.vstack([cloth_verts, proxy.verts]), dtype=np.float64
    )
    shifted = proxy.faces + np.int32(cloth_verts.shape[0])
    faces = np.ascontiguousarray(np.vstack([cloth_faces, shifted]), dtype=np.int32)
    is_collider = np.concatenate(
        [
            np.zeros((n_cloth_faces,), dtype=np.uint8),
            np.ones((proxy.faces.shape[0],), dtype=np.uint8),
        ]
    )
    return verts, faces, n_cloth_faces, is_collider


def _restore_body_faces(
    pairs: tuple[tuple[int, int], ...],
    n_cloth_faces: int,
    proxy: _BodyProxy | None,
) -> tuple[tuple[int, int], ...]:
    """Rename proxy body faces back to their index in the whole body."""
    if proxy is None or not pairs:
        return pairs

    def rename(index: int) -> int:
        local = index - n_cloth_faces
        if local < 0 or local >= proxy.face_index.shape[0]:
            return index
        return n_cloth_faces + int(proxy.face_index[local])

    return tuple((rename(a), rename(b)) for a, b in pairs)


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

    # Filtering needs the raw pairs, so ask for a buffer rather than a count and
    # a second identical run: on the twin mesh each run is the expensive part,
    # and counting first doubled it. Only a buffer that actually overflowed
    # needs asking again -- collider self-hits could otherwise crowd out the
    # cloth pairs that were the point of the call.
    budget = max(int(max_pairs), _MAX_REPORT_PAIRS, _PAIR_BUDGET)
    total, raw_pairs, rc = _raw_check_pairs(lib, verts, faces, max_pairs=budget)
    if rc not in (0, 2):
        return total, (), rc
    if total > budget:
        total, raw_pairs, rc = _raw_check_pairs(lib, verts, faces, max_pairs=total)
        if rc not in (0, 2):
            return total, (), rc
    filtered = _filter_collider_pairs(raw_pairs, is_collider)
    if max_pairs <= 0:
        return len(filtered), (), rc
    return len(filtered), filtered[:max_pairs], rc


def _proxy_bvh(proxy: _BodyProxy | None) -> BVHTree | None:
    """World-space triangle BVH for the cropped STATIC body.

    Built from the proxy rather than the whole body: the tree is only ever
    queried for the nearest surface to a cloth vertex that is already reported
    as crossing the body, and the crop keeps a cell of slack around the cloth,
    so the nearest triangle is in it. On the reference character that is 55k
    triangles instead of 449k, and this builds them through Python lists.
    """
    if proxy is None or proxy.faces.shape[0] == 0:
        return None
    points = [Vector((float(p[0]), float(p[1]), float(p[2]))) for p in proxy.verts]
    tris = [[int(i) for i in f] for f in proxy.faces]
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


def _separate_cloth_from_body_pairs(
    cloth_verts: np.ndarray,
    pairs: tuple[tuple[int, int], ...],
    cloth_faces: np.ndarray,
    n_cloth_faces: int,
    proxy: _BodyProxy,
    separation_m: float,
) -> tuple[np.ndarray, int]:
    """Push only cloth verts away from intersecting STATIC body faces.

    Needed when every cloth corner is already outside the body surface
    (BVH side > 0) but an edge still pierces a body triangle — the usual
    residual after a vertex-only outward push. Body verts never move.
    """
    trial = cloth_verts.copy()
    moved_verts: set[int] = set()
    body_faces = proxy.faces
    body_verts = proxy.verts
    n_body = int(body_faces.shape[0])
    for i, j in pairs:
        if i < n_cloth_faces and j < n_cloth_faces:
            continue
        if i < n_cloth_faces <= j:
            ci, bj = i, j - n_cloth_faces
        elif j < n_cloth_faces <= i:
            ci, bj = j, i - n_cloth_faces
        else:
            continue
        if not (0 <= ci < n_cloth_faces and 0 <= bj < n_body):
            continue
        fa = [int(v) for v in cloth_faces[ci]]
        fb = [int(v) for v in body_faces[bj]]
        pa = trial[fa].mean(axis=0)
        pb = body_verts[fb].mean(axis=0)
        # Prefer body face normal (outward-ish) so cloth rides off the surface.
        e1 = body_verts[fb[1]] - body_verts[fb[0]]
        e2 = body_verts[fb[2]] - body_verts[fb[0]]
        n = np.cross(e1, e2)
        ln = float(np.linalg.norm(n))
        if ln >= 1.0e-12:
            direction = n / ln
            # Flip so the push leaves the body (cloth centroid on outward side).
            if float(np.dot(pa - pb, direction)) < 0.0:
                direction = -direction
        else:
            d = pa - pb
            length = float(np.linalg.norm(d))
            if length < 1.0e-12:
                continue
            direction = d / length
        for vi in fa:
            trial[vi] = trial[vi] + direction * separation_m
            moved_verts.add(vi)
    return trial, len(moved_verts)


def _face_areas(verts: np.ndarray, faces: np.ndarray) -> np.ndarray:
    corners = verts[faces]
    e1 = corners[:, 1] - corners[:, 0]
    e2 = corners[:, 2] - corners[:, 0]
    return 0.5 * np.linalg.norm(np.cross(e1, e2), axis=1)


def _incident_face_indices(
    cloth_faces: np.ndarray, region: set[int]
) -> np.ndarray:
    if not region:
        return np.zeros(0, dtype=np.int32)
    mask = np.zeros(int(cloth_faces.shape[0]), dtype=bool)
    for fi, tri in enumerate(cloth_faces):
        if int(tri[0]) in region or int(tri[1]) in region or int(tri[2]) in region:
            mask[fi] = True
    return np.flatnonzero(mask).astype(np.int32)


def _areas_edges_acceptable(
    verts: np.ndarray,
    faces: np.ndarray,
    face_ids: np.ndarray,
    baseline_areas: np.ndarray,
) -> bool:
    """Reject trials that collapse a previously healthy triangle."""
    if face_ids.size == 0:
        return True
    areas = _face_areas(verts, faces[face_ids])
    for index, face_index in enumerate(face_ids):
        base = float(baseline_areas[index])
        if base < _LOCAL_MIN_AREA_M2:
            continue
        if float(areas[index]) < _LOCAL_MIN_AREA_M2:
            return False
        tri = faces[int(face_index)]
        for a, b in ((0, 1), (1, 2), (2, 0)):
            edge = float(np.linalg.norm(verts[int(tri[a])] - verts[int(tri[b])]))
            if edge < _LOCAL_MIN_EDGE_M:
                return False
    return True


def _expand_short_edges(
    cloth_verts: np.ndarray,
    cloth_faces: np.ndarray,
    region: set[int],
    min_edge_m: float = _LOCAL_MIN_EDGE_M,
) -> tuple[np.ndarray, int]:
    """Expand short edges inside ``region`` (topology unchanged)."""
    trial = cloth_verts.copy()
    moved = 0
    for _ in range(_LOCAL_UNSLIVER_ITERS):
        step_moved = 0
        for tri in cloth_faces:
            ids = [int(tri[0]), int(tri[1]), int(tri[2])]
            for e in range(3):
                ia, ib = ids[e], ids[(e + 1) % 3]
                ma, mb = ia in region, ib in region
                if not ma and not mb:
                    continue
                a = trial[ia]
                b = trial[ib]
                d = b - a
                length = float(np.linalg.norm(d))
                if length >= min_edge_m:
                    continue
                if length < 1.0e-12:
                    ic = ids[(e + 2) % 3]
                    mid = 0.5 * (a + b)
                    d = trial[ic] - mid
                    if float(np.linalg.norm(d)) < 1.0e-12:
                        d = np.array([1.0, 0.0, 0.0], dtype=np.float64)
                d = d / max(float(np.linalg.norm(d)), 1.0e-12)
                need = 0.5 * (min_edge_m - length)
                if ma and mb:
                    trial[ia] = a - d * need
                    trial[ib] = b + d * need
                elif ma:
                    trial[ia] = a - d * (2.0 * need)
                else:
                    trial[ib] = b + d * (2.0 * need)
                step_moved += 1
                moved += 1
        if step_moved == 0:
            break
    return trial, moved


def _degenerate_face_region(
    cloth_verts: np.ndarray,
    cloth_faces: np.ndarray,
    *,
    min_edge_m: float = _LOCAL_MIN_EDGE_M,
    min_area_m2: float = _LOCAL_MIN_AREA_M2,
) -> set[int]:
    """Vertex set of faces with a knife edge or near-zero area."""
    region: set[int] = set()
    if cloth_faces.size == 0:
        return region
    areas = _face_areas(cloth_verts, cloth_faces)
    for fi, tri in enumerate(cloth_faces):
        ids = [int(tri[0]), int(tri[1]), int(tri[2])]
        bad = float(areas[fi]) < min_area_m2
        if not bad:
            for a, b in ((0, 1), (1, 2), (2, 0)):
                if float(np.linalg.norm(cloth_verts[ids[a]] - cloth_verts[ids[b]])) < min_edge_m:
                    bad = True
                    break
        if bad:
            region.update(ids)
    return region


def _heal_cloth_degenerates(cloth: bpy.types.Object) -> tuple[int, int]:
    """Expand short edges / tiny faces on the whole cloth (topology unchanged).

    FIX often clears tri-tri pairs while leaving a knife-edge triangle a few
    faces away from the NG pocket — enough to fail the ZOZO rest-area gate
    (floor ~2.5e-11 m²) even when twin-check is green. Returns
    ``(faces_touched_estimate, verts_moved)``.
    """
    cloth_verts, cloth_faces = _mesh_arrays_world(cloth)
    region = _degenerate_face_region(cloth_verts, cloth_faces)
    if not region:
        return 0, 0
    n_faces = len(region)  # rough; region is verts
    healed, moved = _expand_short_edges(cloth_verts, cloth_faces, region)
    if moved <= 0:
        return 0, 0
    # Second pass after expansion can reveal new short edges in the pocket.
    region2 = _degenerate_face_region(healed, cloth_faces)
    if region2:
        healed, moved2 = _expand_short_edges(healed, cloth_faces, region2)
        moved += moved2
    if moved > 0:
        _apply_world_verts(cloth, healed)
    return n_faces, moved


def _local_fix_cloth_against_body(
    lib,
    cloth: bpy.types.Object,
    proxy: _BodyProxy | None,
    cloth_verts: np.ndarray,
    cloth_faces: np.ndarray,
    n_cloth_faces: int,
    pairs: tuple[tuple[int, int], ...],
    pairs_before: int,
) -> tuple[str, str]:
    """Local-only NG pocket fix: push cloth verts, never body.

    Multi-pass guidance may call the checker *inside* the fix loop to decide
    whether a trial step helped (algorithm detail). That is not CHECK 2 — the
    caller always runs an independent post-fix checker on the written mesh.

    Returns (fix_status, message). Writes cloth when geometry changes.
    """
    body_bvh = _proxy_bvh(proxy)
    if body_bvh is None:
        return "NOOP", "no body triangles"

    adjacency = _cloth_adjacency(cloth_faces, int(cloth_verts.shape[0]))
    working = cloth_verts.copy()
    current_pairs = pairs
    current_count = pairs_before
    clearance = _LOCAL_BODY_CLEARANCE_M + _LOCAL_BODY_PAD_M
    applied_any = False
    last_region: set[int] = set()

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
        last_region = set(region)
        face_ids = _incident_face_indices(cloth_faces, region)
        baseline = (
            _face_areas(working, cloth_faces[face_ids])
            if face_ids.size
            else np.zeros(0, dtype=np.float64)
        )

        trial, moved_body = _push_verts_outside_body(
            working, region, body_bvh, clearance
        )
        # Cloth–body residual pairs: pair-directed push (cloth only). Catches
        # edge–tri pierces that leave every corner outside the body surface.
        cloth_body = tuple(
            (a, b)
            for a, b in current_pairs
            if (a < n_cloth_faces) != (b < n_cloth_faces)
        )
        moved_cb = 0
        if cloth_body and proxy is not None:
            sep_m = (
                _LOCAL_CLOTH_BODY_SEP_M
                + _LOCAL_CLOTH_BODY_SEP_GROW_M * float(pass_index)
            )
            trial, moved_cb = _separate_cloth_from_body_pairs(
                trial,
                cloth_body,
                cloth_faces,
                n_cloth_faces,
                proxy,
                sep_m,
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
        if moved_cb or moved_sep:
            trial, _ = _push_verts_outside_body(
                trial, region, body_bvh, clearance
            )
        trial, moved_unsliver = _expand_short_edges(trial, cloth_faces, region)
        if (
            moved_body == 0
            and moved_sep == 0
            and moved_cb == 0
            and moved_unsliver == 0
        ):
            break
        if not _areas_edges_acceptable(trial, cloth_faces, face_ids, baseline):
            # Second chance: only unsliver the working mesh.
            trial, moved_unsliver = _expand_short_edges(
                working, cloth_faces, region
            )
            if moved_unsliver == 0 or not _areas_edges_acceptable(
                trial, cloth_faces, face_ids, baseline
            ):
                break

        # Guidance probe only (not the host CHECK 2 gate). The body proxy is
        # reused rather than rebuilt: it was cropped with a cell of slack and a
        # pass moves cloth by well under that, so it still covers the cloth.
        comb_verts, comb_faces, _n, is_collider = _combined_cloth_body_arrays(
            trial, cloth_faces, proxy
        )
        after_count, after_pairs, rc = _check_pairs(
            lib,
            comb_verts,
            comb_faces,
            max_pairs=max(int(current_count), _MAX_REPORT_PAIRS),
            is_collider=is_collider,
        )
        if rc not in (0, 2):
            if applied_any:
                _apply_world_verts(cloth, working)
            return (
                "FAILED" if not applied_any else "APPLIED",
                f"fix guidance re-check failed rc={rc}",
            )
        if after_count > current_count:
            # Rollback this step; do not ship a worse mesh.
            if applied_any:
                _apply_world_verts(cloth, working)
            return (
                "APPLIED" if applied_any else "FAILED",
                "local push increased pairs; rolled back step",
            )

        working = trial
        applied_any = True
        current_count = after_count
        current_pairs = after_pairs
        if after_count == 0:
            break

        clearance += _LOCAL_BODY_PAD_M

    if applied_any and last_region:
        # Final unsliver so CLEARED pockets cannot keep a 0.001 mm edge.
        face_ids = _incident_face_indices(cloth_faces, last_region)
        baseline = (
            _face_areas(cloth_verts, cloth_faces[face_ids])
            if face_ids.size
            else np.zeros(0, dtype=np.float64)
        )
        healed, moved = _expand_short_edges(working, cloth_faces, last_region)
        if moved and _areas_edges_acceptable(
            healed, cloth_faces, face_ids, baseline
        ):
            comb_verts, comb_faces, _n, is_collider = _combined_cloth_body_arrays(
                healed, cloth_faces, proxy
            )
            healed_count, healed_pairs, rc = _check_pairs(
                lib,
                comb_verts,
                comb_faces,
                max_pairs=max(int(current_count), _MAX_REPORT_PAIRS),
                is_collider=is_collider,
            )
            if rc in (0, 2) and healed_count <= current_count:
                working = healed
                current_count = healed_count
                current_pairs = healed_pairs

    if applied_any:
        _apply_world_verts(cloth, working)
        if current_count > 0:
            return "APPLIED", "pairs remain after local push"
        return "CLEARED", "local body push cleared pairs"
    return "NOOP", "local push moved nothing"


def _dll_fix_cloth_only(
    lib,
    cloth: bpy.types.Object,
    cloth_verts: np.ndarray,
    cloth_faces: np.ndarray,
) -> tuple[str, str]:
    """Geometry-only FIX stage via shell_isect_fix (no checker gate).

    Returns (fix_status, message). Writes cloth when status is APPLIED/CLEARED.
    """
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
        return "FAILED", f"fix failed rc={rc}"
    fix_name = _FIX_STATUS.get(int(status.value), f"STATUS_{int(status.value)}")
    if fix_name in ("APPLIED", "CLEARED"):
        _apply_world_verts(cloth, out)
    return fix_name, f"dll fix status={fix_name}"


def _mesh_for_check(
    cloth: bpy.types.Object,
    proxy: _BodyProxy | None,
    *,
    use_body: bool,
) -> tuple[np.ndarray, np.ndarray, int, np.ndarray | None]:
    """Build checker arrays from the current Blender cloth and the body proxy."""
    cloth_verts, cloth_faces = _mesh_arrays_world(cloth)
    if use_body and proxy is not None:
        return _combined_cloth_body_arrays(cloth_verts, cloth_faces, proxy)
    return cloth_verts, cloth_faces, int(cloth_faces.shape[0]), None


def run_check_and_fix(
    cloth: bpy.types.Object,
    body: bpy.types.Object | None = None,
    *,
    include_body: bool = False,
) -> ShellIsectReport:
    """Run shell-isect as CHECK 1 → FIX → CHECK 2.

    Parameters
    ----------
    cloth:
        ZOZO cloth copy (edited in place when a local fix applies).
    body:
        ZOZO body copy. Used only when ``include_body`` is True (and for the
        optional body-aware local fix in that mode). Always still created by
        Prepare; this flag only controls whether shell-isect *tests* against it.
    include_body:
        False (default): cloth-only pairs — practical runtime.
        True: cloth+body combined mesh with STATIC body colliders (PPF twin).
        Body–body pairs are filtered out. High-poly bodies can take many minutes.
    """
    use_body = bool(include_body and body is not None)

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
            include_body=use_body,
            checks_run=0,
            fix_attempted=False,
        )

    version = lib.shell_isect_version().decode("utf-8", errors="replace")
    # Crop the body once, from the cloth as it stands. Every stage below reuses
    # it: CHECK 1, the fix loop's probes and its BVH, and CHECK 2.
    proxy = None
    if use_body:
        cloth_verts0, cloth_faces0 = _mesh_arrays_world(cloth)
        proxy = _body_proxy(cloth_verts0, cloth_faces0, body)
    verts, faces, n_cloth_faces, is_collider = _mesh_for_check(
        cloth, proxy, use_body=use_body
    )
    body_tested = proxy.kept if proxy is not None else 0
    body_total = proxy.total if proxy is not None else 0

    if faces.shape[0] < 2:
        return ShellIsectReport(
            available=True,
            version=version,
            pairs_before=0,
            pairs_after=0,
            fix_status="SKIPPED",
            message="fewer than 2 triangles",
            pairs=(),
            n_cloth_faces=n_cloth_faces,
            include_body=use_body,
            checks_run=1,
            fix_attempted=False,
        )

    # ------------------------------------------------------------------
    # CHECK 1 — detect only (DLL shell_isect_check)
    # ------------------------------------------------------------------
    # One call: it returns the count and the pairs the FIX stage needs, and
    # `_check_pairs` asks again only if its buffer actually overflowed.
    check1_count, check1_pairs, rc = _check_pairs(
        lib,
        verts,
        faces,
        max_pairs=_PAIR_BUDGET,
        is_collider=is_collider,
    )
    if rc not in (0, 2):
        return ShellIsectReport(
            available=True,
            version=version,
            pairs_before=-1,
            pairs_after=-1,
            fix_status="SKIPPED",
            message=f"check1 failed rc={rc}",
            pairs=(),
            n_cloth_faces=n_cloth_faces,
            include_body=use_body,
            checks_run=1,
            fix_attempted=False,
            body_faces_tested=body_tested,
            body_faces_total=body_total,
        )

    # ------------------------------------------------------------------
    # FIX — geometry only (DLL fix and/or host local push)
    # ------------------------------------------------------------------
    # check1==0 still runs the empty-fix path below so post-fix unsliver
    # can heal knife edges before the host quality gate.
    cloth_verts, cloth_faces = _mesh_arrays_world(cloth)
    fix_name = "SKIPPED"
    fix_message = "check1 clean; fix skipped"
    fix_attempted = check1_count > 0

    if check1_count == 0:
        fix_name = "SKIPPED"
        fix_message = (
            "check1 clean; fix skipped"
            if use_body
            else "check1 clean; fix skipped (cloth-only; body twin skipped)"
        )
    elif use_body:
        # 1) Cloth-only DLL first (cloth–cloth + unsliver). DLL has no STATIC
        #    mask so it must not run after body-aware push — it can re-pierce.
        # 2) Body-aware host last so residual cloth–body pairs are the final
        #    geometry CHECK 2 sees. Cloth–body cannot be fixed by cloth-only
        #    shell_isect_fix (those pairs are invisible without the body).
        dll_name, dll_message = _dll_fix_cloth_only(
            lib, cloth, cloth_verts, cloth_faces
        )
        cloth_verts2, cloth_faces2 = _mesh_arrays_world(cloth)
        # Refresh pair list after DLL so the host targets what remains.
        verts_mid, faces_mid, _n_mid, is_col_mid = _mesh_for_check(
            cloth, proxy, use_body=True
        )
        mid_count, mid_pairs, mid_rc = _check_pairs(
            lib,
            verts_mid,
            faces_mid,
            max_pairs=max(check1_count, _PAIR_BUDGET),
            is_collider=is_col_mid,
        )
        if mid_rc not in (0, 2):
            fix_name = (
                dll_name if dll_name not in ("NOOP", "SKIPPED") else "FAILED"
            )
            fix_message = f"{dll_message}; mid-check failed rc={mid_rc}"
        elif mid_count <= 0:
            # All pairs gone after the cloth-only stage (or already clean mid).
            fix_name = "CLEARED" if check1_count > 0 else "SKIPPED"
            fix_message = f"{dll_message}; mid-check clean"
        else:
            host_name, host_message = _local_fix_cloth_against_body(
                lib,
                cloth,
                proxy,
                cloth_verts2,
                cloth_faces2,
                n_cloth_faces,
                mid_pairs if mid_pairs else check1_pairs,
                mid_count,
            )
            # Host status is authoritative for residual cloth–body pairs.
            fix_name = host_name
            if dll_name in ("APPLIED", "CLEARED"):
                fix_message = f"{dll_message}; {host_message}"
            else:
                fix_message = host_message
    else:
        fix_name, fix_message = _dll_fix_cloth_only(
            lib, cloth, cloth_verts, cloth_faces
        )

    # ------------------------------------------------------------------
    # UN-SLIVER — whole-cloth short-edge heal (not only the NG ring)
    # ------------------------------------------------------------------
    # Pair-clearing pushes often collapse a neighbouring triangle just outside
    # the ring. Twin-check stays green; ZOZO's rest-area gate does not.
    heal_faces, heal_moved = _heal_cloth_degenerates(cloth)
    if heal_moved > 0:
        fix_attempted = True
        if fix_name in ("SKIPPED", "NOOP"):
            fix_name = "APPLIED"
        fix_message = (
            f"{fix_message}; unsliver verts={heal_moved} pocket~{heal_faces}"
        )
        # Healing can re-pierce the body: one more body-aware pass if needed.
        if use_body and proxy is not None:
            cloth_h, faces_h = _mesh_arrays_world(cloth)
            vh, fh, nch, ich = _mesh_for_check(cloth, proxy, use_body=True)
            h_count, h_pairs, h_rc = _check_pairs(
                lib,
                vh,
                fh,
                max_pairs=max(check1_count, _PAIR_BUDGET),
                is_collider=ich,
            )
            if h_rc in (0, 2) and h_count > 0:
                host2, host2_msg = _local_fix_cloth_against_body(
                    lib,
                    cloth,
                    proxy,
                    cloth_h,
                    faces_h,
                    nch,
                    h_pairs if h_pairs else (),
                    h_count,
                )
                fix_name = host2 if host2 != "NOOP" else fix_name
                fix_message = f"{fix_message}; post-unsliver {host2_msg}"
                # Re-heal lightly: body push can re-collapse.
                _, heal_moved2 = _heal_cloth_degenerates(cloth)
                if heal_moved2 > 0:
                    fix_message = f"{fix_message}; re-unsliver={heal_moved2}"

    # ------------------------------------------------------------------
    # CHECK 2 — detect only on post-FIX / post-unsliver mesh
    # ------------------------------------------------------------------
    # Always run when we fixed or healed; also when check1 was already clean
    # but unsliver moved geometry (re-verify). If check1 clean and no heal,
    # one checker stage is enough.
    run_check2 = fix_attempted or heal_moved > 0 or check1_count > 0
    if not run_check2:
        return ShellIsectReport(
            available=True,
            version=version,
            pairs_before=0,
            pairs_after=0,
            fix_status=fix_name,
            message=fix_message,
            pairs=(),
            n_cloth_faces=n_cloth_faces,
            include_body=use_body,
            checks_run=1,
            fix_attempted=False,
            body_faces_tested=body_tested,
            body_faces_total=body_total,
        )

    verts2, faces2, n_cloth_faces2, is_collider2 = _mesh_for_check(
        cloth, proxy, use_body=use_body
    )
    check2_count, check2_pairs, rc = _check_pairs(
        lib,
        verts2,
        faces2,
        max_pairs=max(check1_count, _MAX_REPORT_PAIRS, 1),
        is_collider=is_collider2,
    )
    if rc not in (0, 2):
        return ShellIsectReport(
            available=True,
            version=version,
            pairs_before=check1_count,
            pairs_after=-1,
            fix_status=fix_name,
            message=f"check2 failed rc={rc}; fix={fix_message}",
            pairs=(),
            n_cloth_faces=n_cloth_faces2,
            include_body=use_body,
            checks_run=2,
            fix_attempted=True,
            body_faces_tested=body_tested,
            body_faces_total=body_total,
        )

    if check2_count == 0:
        if check1_count == 0 and heal_moved <= 0:
            final_message = fix_message
        else:
            final_message = (
                "check2 clean after fix"
                if use_body
                else "check2 clean after fix (cloth-only; body twin skipped)"
            )
            if heal_moved > 0:
                final_message += f"; {fix_message}"
    else:
        final_message = (
            f"check2 residual pairs; {fix_message}"
            if use_body
            else f"check2 residual pairs (cloth-only); {fix_message}"
        )

    return ShellIsectReport(
        available=True,
        version=version,
        pairs_before=check1_count,
        pairs_after=check2_count,
        fix_status=fix_name,
        message=final_message,
        # Reported pairs name body triangles by their index in the whole body,
        # not in the crop, so a face pair still points at something findable.
        pairs=_restore_body_faces(
            check2_pairs[:_MAX_REPORT_PAIRS], n_cloth_faces2, proxy
        ),
        n_cloth_faces=n_cloth_faces2,
        include_body=use_body,
        checks_run=2 if (fix_attempted or heal_moved > 0 or check1_count > 0) else 1,
        fix_attempted=bool(fix_attempted or heal_moved > 0),
        body_faces_tested=body_tested,
        body_faces_total=body_total,
    )
