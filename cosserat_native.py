# SPDX-License-Identifier: GPL-3.0-or-later
"""ctypes bridge for the versioned native square-lattice cloth solver."""

from __future__ import annotations

import ctypes
import os
from pathlib import Path

import numpy as np


API_VERSION = 7
_ERROR_CAPACITY = 1024


class NativeCosseratError(RuntimeError):
    """The packaged native Kitsuke runtime cannot be loaded or advanced."""


FloatPointer = ctypes.POINTER(ctypes.c_float)
IntPointer = ctypes.POINTER(ctypes.c_int32)


class _Config(ctypes.Structure):
    _fields_ = [
        ("time_step", ctypes.c_float),
        ("substeps", ctypes.c_int32),
        ("iterations", ctypes.c_int32),
        ("seam_attraction_force", ctypes.c_float),
        ("seam_capture_distance", ctypes.c_float),
        ("stretch_relaxation", ctypes.c_float),
        ("shear_relaxation", ctypes.c_float),
        ("bend_relaxation", ctypes.c_float),
        ("maximum_position_correction", ctypes.c_float),
        ("contact_thickness", ctypes.c_float),
    ]


class _CreateDesc(ctypes.Structure):
    _fields_ = [
        ("vertex_count", ctypes.c_int32),
        ("positions", FloatPointer),
        ("velocities", FloatPointer),
        ("inverse_masses", FloatPointer),
        ("locked", IntPointer),
        ("seam_count", ctypes.c_int32),
        ("seams", IntPointer),
        ("edge_count", ctypes.c_int32),
        ("edges", IntPointer),
        ("edge_rest_lengths", FloatPointer),
        ("quad_count", ctypes.c_int32),
        ("quads", IntPointer),
        ("quad_rest_metrics", FloatPointer),
        ("bend_count", ctypes.c_int32),
        ("bends", IntPointer),
        ("bend_rest_lengths", FloatPointer),
        ("body_vertex_count", ctypes.c_int32),
        ("body_positions", FloatPointer),
        ("body_face_count", ctypes.c_int32),
        ("body_faces", IntPointer),
    ]


class _AdvanceDesc(ctypes.Structure):
    _fields_ = [
        ("gravity", ctypes.c_float * 3),
        ("iterations", ctypes.c_int32),
        ("body_candidate_count", ctypes.c_int32),
        ("body_candidates", IntPointer),
    ]


class _Stats(ctypes.Structure):
    _fields_ = [
        ("substeps", ctypes.c_int32),
        ("iterations", ctypes.c_int32),
        ("seam_count", ctypes.c_int32),
        ("captured_seam_count", ctypes.c_int32),
        ("edge_count", ctypes.c_int32),
        ("quad_count", ctypes.c_int32),
        ("bend_count", ctypes.c_int32),
        ("body_candidate_count", ctypes.c_int32),
        ("maximum_displacement", ctypes.c_float),
    ]


def _float_array(values, shape: tuple[int, ...], name: str) -> np.ndarray:
    result = np.ascontiguousarray(values, dtype=np.float32)
    if result.size == 0 and np.prod(shape, dtype=np.int64) == 0:
        result = np.empty(shape, dtype=np.float32)
    if result.shape != shape or not np.all(np.isfinite(result)):
        raise NativeCosseratError(f"{name} must be a finite float32 array with shape {shape}.")
    return result


def _int_array(values, columns: int, name: str) -> np.ndarray:
    result = np.ascontiguousarray(values, dtype=np.int32)
    if result.size == 0:
        return np.empty((0, columns), dtype=np.int32)
    if result.ndim != 2 or result.shape[1] != columns:
        raise NativeCosseratError(f"{name} must be an int32 array with shape (N, {columns}).")
    return result


def _float_pointer(values: np.ndarray) -> FloatPointer:
    return values.ctypes.data_as(FloatPointer)


def _int_pointer(values: np.ndarray) -> IntPointer:
    return values.ctypes.data_as(IntPointer)


def _library_candidates() -> tuple[Path, ...]:
    root = Path(__file__).resolve().parent
    explicit = os.environ.get("YOHSAI_COSSERAT_DLL", "").strip()
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    candidates.extend(
        (
            root / "bin" / "yohsai_cosserat.dll",
            root / "build" / "bin" / "Release" / "yohsai_cosserat.dll",
            root / "build" / "bin" / "Debug" / "yohsai_cosserat.dll",
        )
    )
    return tuple(candidates)


def _configure_library(library: ctypes.CDLL) -> None:
    library.ysc_get_api_version.argtypes = []
    library.ysc_get_api_version.restype = ctypes.c_int32
    library.ysc_default_config.argtypes = [ctypes.POINTER(_Config)]
    library.ysc_default_config.restype = ctypes.c_int32
    library.ysc_create.argtypes = [
        ctypes.POINTER(_CreateDesc),
        ctypes.POINTER(_Config),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_char_p,
        ctypes.c_int32,
    ]
    library.ysc_create.restype = ctypes.c_int32
    library.ysc_destroy.argtypes = [ctypes.c_void_p]
    library.ysc_destroy.restype = None
    library.ysc_get_counts.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_int32),
        ctypes.POINTER(ctypes.c_int32),
        ctypes.c_char_p,
        ctypes.c_int32,
    ]
    library.ysc_get_counts.restype = ctypes.c_int32
    library.ysc_replace_state.argtypes = [
        ctypes.c_void_p,
        FloatPointer,
        FloatPointer,
        IntPointer,
        ctypes.c_char_p,
        ctypes.c_int32,
    ]
    library.ysc_replace_state.restype = ctypes.c_int32
    library.ysc_copy_state.argtypes = [
        ctypes.c_void_p,
        FloatPointer,
        FloatPointer,
        ctypes.c_char_p,
        ctypes.c_int32,
    ]
    library.ysc_copy_state.restype = ctypes.c_int32
    for name in ("ysc_replace_seam_state", "ysc_copy_seam_state"):
        function = getattr(library, name)
        function.argtypes = [ctypes.c_void_p, FloatPointer, ctypes.c_char_p, ctypes.c_int32]
        function.restype = ctypes.c_int32
    library.ysc_advance.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_AdvanceDesc),
        ctypes.POINTER(_Stats),
        ctypes.c_char_p,
        ctypes.c_int32,
    ]
    library.ysc_advance.restype = ctypes.c_int32


_library: ctypes.CDLL | None = None


def _load_library() -> ctypes.CDLL:
    attempted: list[str] = []
    for path in _library_candidates():
        attempted.append(str(path))
        if not path.is_file():
            continue
        try:
            library = ctypes.CDLL(str(path))
        except OSError as exc:
            raise NativeCosseratError(f"Cannot load native Kitsuke library {path}: {exc}") from exc
        _configure_library(library)
        version = int(library.ysc_get_api_version())
        if version != API_VERSION:
            raise NativeCosseratError(
                f"Native Kitsuke API {version} does not match the extension API {API_VERSION}."
            )
        return library
    raise NativeCosseratError(
        "Native Kitsuke library was not found. Build it with build_native.ps1. "
        f"Searched: {', '.join(attempted)}"
    )


def _get_library() -> ctypes.CDLL:
    global _library
    if _library is None:
        _library = _load_library()
    return _library


def native_library_available() -> bool:
    try:
        _get_library()
        return True
    except NativeCosseratError:
        return False


class NativeCosseratRuntime:
    """Own one native cloth solver and expose Kitsuke state operations."""

    def __init__(self, positions, velocities, seams, topology, body, locked):
        self._library = _get_library()
        self._handle = ctypes.c_void_p()
        self.vertex_count = int(len(positions))
        self.seam_count = int(len(seams))

        positions_array = _float_array(positions, (self.vertex_count, 3), "positions")
        velocities_array = _float_array(velocities, (self.vertex_count, 3), "velocities")
        seams_array = _int_array(seams, 2, "seams")
        edges = _int_array(topology.edges, 2, "material edges")
        edge_rest_lengths = _float_array(
            topology.edge_rest_lengths, (len(edges),), "material edge rest lengths"
        )
        quads = _int_array(topology.quads, 4, "material quads")
        quad_rest_metrics = _float_array(
            topology.quad_rest_metrics, (len(quads), 3), "material quad rest metrics"
        )
        bends = _int_array(topology.bends, 3, "material bends")
        bend_rest_lengths = _float_array(
            topology.bend_rest_lengths, (len(bends), 2), "material bend rest lengths"
        )
        body_vertices = _float_array(body.vertices, (len(body.vertices), 3), "Body vertices")
        body_faces = _int_array(body.faces, 3, "Body faces")
        locked_array = np.ascontiguousarray(locked, dtype=np.int32)
        if locked_array.shape != (self.vertex_count,):
            raise NativeCosseratError(f"locked must have shape ({self.vertex_count},).")
        inverse_masses = np.ones(self.vertex_count, dtype=np.float32)

        config = _Config()
        if self._library.ysc_default_config(ctypes.byref(config)) != 0:
            raise NativeCosseratError("Native solver did not provide a default configuration.")

        desc = _CreateDesc(
            self.vertex_count,
            _float_pointer(positions_array),
            _float_pointer(velocities_array),
            _float_pointer(inverse_masses),
            _int_pointer(locked_array),
            self.seam_count,
            _int_pointer(seams_array),
            len(edges),
            _int_pointer(edges),
            _float_pointer(edge_rest_lengths),
            len(quads),
            _int_pointer(quads),
            _float_pointer(quad_rest_metrics),
            len(bends),
            _int_pointer(bends),
            _float_pointer(bend_rest_lengths),
            len(body_vertices),
            _float_pointer(body_vertices),
            len(body_faces),
            _int_pointer(body_faces),
        )
        self._call("ysc_create", ctypes.byref(desc), ctypes.byref(config), ctypes.byref(self._handle))
        if not self._handle:
            raise NativeCosseratError("Native solver returned no handle.")

        vertex_count = ctypes.c_int32()
        seam_count = ctypes.c_int32()
        self._call(
            "ysc_get_counts",
            self._handle,
            ctypes.byref(vertex_count),
            ctypes.byref(seam_count),
        )
        if vertex_count.value != self.vertex_count or seam_count.value != self.seam_count:
            self.close()
            raise NativeCosseratError("Native solver count validation failed.")
        self.last_stats: dict[str, float | int] = {}

    def _call(self, function_name: str, *arguments) -> None:
        if function_name != "ysc_create" and not self._handle:
            raise NativeCosseratError("Native Kitsuke runtime is closed.")
        error = ctypes.create_string_buffer(_ERROR_CAPACITY)
        status = int(getattr(self._library, function_name)(*arguments, error, _ERROR_CAPACITY))
        if status != 0:
            message = error.value.decode("utf-8", errors="replace").strip()
            raise NativeCosseratError(message or f"{function_name} failed with native status {status}.")

    def close(self) -> None:
        if getattr(self, "_handle", None):
            self._library.ysc_destroy(self._handle)
            self._handle = ctypes.c_void_p()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def replace_state(self, positions, velocities, locked) -> None:
        positions_array = _float_array(positions, (self.vertex_count, 3), "positions")
        velocities_array = _float_array(velocities, (self.vertex_count, 3), "velocities")
        locked_array = np.ascontiguousarray(locked, dtype=np.int32)
        if locked_array.shape != (self.vertex_count,):
            raise NativeCosseratError(f"locked must have shape ({self.vertex_count},).")
        self._call(
            "ysc_replace_state",
            self._handle,
            _float_pointer(positions_array),
            _float_pointer(velocities_array),
            _int_pointer(locked_array),
        )

    def state(self) -> tuple[np.ndarray, np.ndarray]:
        positions = np.empty((self.vertex_count, 3), dtype=np.float32)
        velocities = np.empty_like(positions)
        self._call(
            "ysc_copy_state",
            self._handle,
            _float_pointer(positions),
            _float_pointer(velocities),
        )
        return positions, velocities

    def seam_state(self) -> np.ndarray:
        values = np.empty(self.seam_count, dtype=np.float32)
        self._call("ysc_copy_seam_state", self._handle, _float_pointer(values))
        return values

    def replace_seam_state(self, values) -> None:
        state = _float_array(values, (self.seam_count,), "seam state")
        self._call("ysc_replace_seam_state", self._handle, _float_pointer(state))

    def advance(
        self,
        body_candidates,
        gravity_magnitude: float,
        solver_iterations: int,
    ) -> None:
        body = _int_array(body_candidates, 2, "Body candidates")
        desc = _AdvanceDesc(
            (ctypes.c_float * 3)(0.0, 0.0, -float(gravity_magnitude)),
            int(solver_iterations),
            len(body),
            _int_pointer(body),
        )
        stats = _Stats()
        self._call("ysc_advance", self._handle, ctypes.byref(desc), ctypes.byref(stats))
        self.last_stats = {name: getattr(stats, name) for name, _ctype in stats._fields_}
