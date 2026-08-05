# SPDX-License-Identifier: GPL-3.0-or-later
"""Yohsai Blender N-panel."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import bpy
from bpy.app.handlers import persistent
from bpy.props import BoolProperty, PointerProperty, StringProperty
from bpy.types import Collection, Object, Operator, Panel, PropertyGroup

from .i18n import translations_dict
from .kitsuke import (
    KitsukeError,
    NORMAL_GRAVITY_M_PER_SECOND_SQUARED,
    SOLVER_ITERATIONS,
    adapt_seam_counts,
    advance_kitsuke,
    clear_kitsuke_session,
    clear_sessions,
    has_kitsuke_session,
    reset_runtime_epoch,
)
from .ppf_zero_gravity import SETTLE_FRAMES, SEWING_FRAMES, sew_zero_gravity
from .mesh_loader import (
    LOCKED_OBJECT_KEY,
    apply_auto_lock,
    create_clothes_mesh,
    create_sewn_mesh,
    mark_moved_parts_pending,
    mark_pending_parts_done,
    participating_parts,
    remove_sewn_preview,
    update_clothes_mesh,
)
from .shell_isect_bridge import library_version
from .zozo_handoff import ZOZO_MCP_PORT, ZozoHandoffError, prepare_for_zozo


_parse_process: subprocess.Popen[str] | None = None
_parse_scene_name: str | None = None
_parse_svg_path: str | None = None
_parse_action: str | None = None
_parse_collection_name: str | None = None
_loaded_pattern_json: dict | None = None
_zozo_process: subprocess.Popen[str] | None = None
_zozo_scene_name: str | None = None
_zozo_prepared_summary: str | None = None
_PARSER_FILENAME = "yohsai_svg_parser.py"
_JSON_FILENAME = "yohsai_pattern.json"
_ZOZO_CLIENT_FILENAME = "zozo_mcp_client.py"
_ZOZO_CONFIG_FILENAME = "zozo_mcp_config.json"


@persistent
def _history_change_post(_unused) -> None:
    """Discard non-undoable solver objects after Blender restores its data."""
    clear_sessions()


@persistent
def _file_load_pre(_unused) -> None:
    """Give every loaded file a new recovery epoch and no stale solver objects."""
    reset_runtime_epoch()


def _register_history_handlers() -> None:
    for handlers in (bpy.app.handlers.undo_post, bpy.app.handlers.redo_post):
        if _history_change_post not in handlers:
            handlers.append(_history_change_post)
    if _file_load_pre not in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.append(_file_load_pre)


def _unregister_history_handlers() -> None:
    for handlers in (bpy.app.handlers.undo_post, bpy.app.handlers.redo_post):
        if _history_change_post in handlers:
            handlers.remove(_history_change_post)
    if _file_load_pre in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(_file_load_pre)


def _version() -> str:
    try:
        path = os.path.join(os.path.dirname(__file__), "blender_manifest.toml")
        with open(path, "rb") as f:
            return str(tomllib.load(f).get("version", "?"))
    except Exception:
        return "?"


def _wrap_status_lines(text: str, width: int = 52) -> list[str]:
    """Split status text into panel lines (no icons; message box only)."""
    raw = (text or "").strip() or "Ready"
    lines: list[str] = []
    for paragraph in raw.replace("\r\n", "\n").split("\n"):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        while len(paragraph) > width:
            cut = paragraph.rfind(" ", 0, width)
            if cut < width // 2:
                cut = width
            lines.append(paragraph[:cut].rstrip())
            paragraph = paragraph[cut:].lstrip()
        if paragraph:
            lines.append(paragraph)
    return lines or ["Ready"]


def _draw_status_box(layout, props) -> None:
    """Large multi-line status area; text only (no alert icons)."""
    box = layout.box()
    header = box.row()
    header.label(text="Message")
    col = box.column(align=True)
    col.scale_y = 1.05
    lines = _wrap_status_lines(props.parse_status, width=46)
    # Reserve vertical space so long Prepare/shell-isect notes stay readable.
    while len(lines) < 6:
        lines.append("")
    for line in lines[:14]:
        col.label(text=line if line else " ")


def _mesh_object_poll(_properties, obj: Object) -> bool:
    """Only allow actual mesh objects in the shared Body field."""
    return obj.type == "MESH"


def _selected_mesh_objects() -> list[Object]:
    return [obj for obj in bpy.context.selected_objects if obj.type == "MESH"]


def _clothes_part_objects(collection: Collection | None) -> list[Object]:
    if collection is None:
        return []
    return [
        obj
        for obj in collection.objects
        if obj.type == "MESH" and obj.get("yohsai_role") == "part"
    ]


def _all_clothes_collections() -> list[Collection]:
    return [
        collection
        for collection in bpy.data.collections
        if collection.get("yohsai_role") == "clothes"
    ]


def _apply_auto_lock_all(properties) -> None:
    for collection in _all_clothes_collections():
        apply_auto_lock(collection, bool(properties.auto_lock))


def _update_auto_lock(properties, _context) -> None:
    # Existing Lock and Select Lock cannot both be on. Existing Lock wins here.
    if bool(properties.auto_lock) and bool(getattr(properties, "select_lock_mode", False)):
        properties.select_lock_mode = False
    _apply_auto_lock_all(properties)


def _update_select_lock_mode(properties, _context) -> None:
    # Turning Select Lock on forces Existing Lock off (both-on is forbidden).
    if bool(properties.select_lock_mode) and bool(properties.auto_lock):
        properties.auto_lock = False
        # auto_lock update already applied unlock of non-PLACED; re-assert selection
        # locks after that if the mode is still on.
    if bool(properties.select_lock_mode):
        objects = _selected_mesh_objects()
        parts = _lock_scope_parts(properties, objects)
        targets = [obj for obj in objects if obj in parts]
        for obj in targets:
            obj[LOCKED_OBJECT_KEY] = True
        if targets:
            properties.parse_status = (
                f"Select Lock on: locked {len(targets)} selected clothes part(s)."
            )
        else:
            properties.parse_status = "Select Lock on: select clothes part(s) to lock."
    else:
        properties.parse_status = "Select Lock off."


def _lock_scope_collections(properties, objects: list[Object]) -> list[Collection]:
    collections: list[Collection] = []
    seen: set[str] = set()

    def add(collection: Collection | None) -> None:
        if collection is not None and collection.get("yohsai_role") == "clothes" and collection.name not in seen:
            collections.append(collection)
            seen.add(collection.name)

    add(properties.clothes_collection)
    for obj in objects:
        collection_name = str(obj.get("yohsai_collection", ""))
        add(bpy.data.collections.get(collection_name))
    return collections


def _lock_scope_parts(properties, objects: list[Object]) -> list[Object]:
    scoped: list[Object] = []
    seen: set[str] = set()
    for collection in _lock_scope_collections(properties, objects):
        for obj in _clothes_part_objects(collection):
            if obj.name not in seen:
                scoped.append(obj)
                seen.add(obj.name)
    return scoped


def _selection_lock_targets(properties) -> list[Object]:
    objects = _selected_mesh_objects()
    parts = _lock_scope_parts(properties, objects)
    return [obj for obj in objects if obj in parts]


def _toggle_select_lock(properties) -> None:
    """Toggle Select Lock mode. Mutually exclusive with Existing Lock when on."""
    objects = _selected_mesh_objects()
    targets = _selection_lock_targets(properties)
    if not objects or not targets:
        properties.parse_status = "Select clothes part(s) before Select Lock."
        return
    if properties.select_lock_mode:
        # Mode off: unlock the current selection (same attribute as before).
        properties.select_lock_mode = False
        for obj in targets:
            obj[LOCKED_OBJECT_KEY] = False
        properties.parse_status = f"Select Lock off: unlocked {len(targets)} selected part(s)."
        return
    # Mode on: Existing Lock must be off, then lock selection.
    if properties.auto_lock:
        properties.auto_lock = False
    properties.select_lock_mode = True
    for obj in targets:
        obj[LOCKED_OBJECT_KEY] = True
    properties.parse_status = f"Select Lock on: locked {len(targets)} selected part(s)."


def _parser_data_dir() -> str:
    return bpy.utils.user_resource("DATAFILES", path="yohsai", create=True)


def _bundled_python() -> str:
    """Return Blender's bundled Python executable without external dependencies."""
    names = ["python.exe"] if os.name == "nt" else [f"python{sys.version_info.major}.{sys.version_info.minor}", "python3", "python"]
    candidates = [Path(sys.prefix) / "bin" / name for name in names]
    executable = Path(sys.executable)
    if executable.name.lower().startswith("python"):
        candidates.append(executable)
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    raise FileNotFoundError("Blender's bundled Python executable was not found.")


def _parser_environment() -> dict[str, str]:
    environment = os.environ.copy()
    inherited_paths = [path for path in sys.path if isinstance(path, str) and path]
    existing = environment.get("PYTHONPATH")
    if existing:
        inherited_paths.append(existing)
    environment["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(inherited_paths))
    return environment


def _set_parse_status(message: str) -> None:
    if _parse_scene_name:
        scene = bpy.data.scenes.get(_parse_scene_name)
        if scene is not None and hasattr(scene, "yohsai"):
            scene.yohsai.parse_status = message


def _validate_loaded_json(document: object, svg_path: str) -> dict:
    if not isinstance(document, dict):
        raise ValueError("Parser output is not a JSON object.")
    if document.get("schema") != "yohsai-pattern" or document.get("version") != "1.0.0":
        raise ValueError("Parser output has an unsupported schema or version.")
    if document.get("units") != "m":
        raise ValueError("Parser output does not use meters.")
    source = document.get("source")
    if not isinstance(source, dict) or Path(str(source.get("svg_path", ""))).resolve() != Path(svg_path).resolve():
        raise ValueError("Parser output belongs to a different pattern file.")
    if not isinstance(document.get("panels"), list):
        raise ValueError("Parser output has no panels array.")
    return document


def _poll_svg_parser() -> float | None:
    global _parse_process, _parse_scene_name, _parse_svg_path, _parse_action, _parse_collection_name, _loaded_pattern_json
    process = _parse_process
    if process is None:
        return None
    if process.poll() is None:
        return 0.2

    stdout, stderr = process.communicate()
    svg_path = _parse_svg_path
    try:
        if process.returncode != 0:
            diagnostic = stderr.strip() or stdout.strip() or f"Parser exited with code {process.returncode}."
            raise RuntimeError(diagnostic)
        if not svg_path:
            raise RuntimeError("The parser input path was lost.")
        json_path = Path(_parser_data_dir()) / _JSON_FILENAME
        with json_path.open("r", encoding="utf-8") as handle:
            document = json.load(handle)
        validated_document = _validate_loaded_json(document, svg_path)
        scene = bpy.data.scenes.get(_parse_scene_name) if _parse_scene_name else None
        if _parse_action == "UPDATE":
            clothes_collection = bpy.data.collections.get(_parse_collection_name) if _parse_collection_name else None
            sewing_changed, vertex_count = update_clothes_mesh(bpy.context, clothes_collection, validated_document)
            clear_kitsuke_session(clothes_collection)
            message = f"Updated {clothes_collection.name}: {vertex_count} vertices"
            if sewing_changed:
                message += "; Sewing will rebuild on GRAVITY"
            _set_parse_status(message)
        else:
            clothes_collection = create_clothes_mesh(bpy.context, validated_document)
            if scene is not None and hasattr(scene, "yohsai"):
                scene.yohsai.clothes_collection = clothes_collection
                scene.yohsai.auto_lock = True
                _apply_auto_lock_all(scene.yohsai)
            part_count = sum(obj.get("yohsai_role") == "part" for obj in clothes_collection.objects)
            _set_parse_status(f"Loaded {clothes_collection.name}: {part_count} part(s); Auto lock on")
        _loaded_pattern_json = validated_document
    except Exception as exc:
        operation = "Update" if _parse_action == "UPDATE" else "Load"
        _set_parse_status(f"{operation} failed: {str(exc).strip()[:240]}")
    finally:
        _parse_process = None
        _parse_scene_name = None
        _parse_svg_path = None
        _parse_action = None
        _parse_collection_name = None
    return None


def _set_zozo_status(message: str) -> None:
    if _zozo_scene_name:
        scene = bpy.data.scenes.get(_zozo_scene_name)
        if scene is not None and hasattr(scene, "yohsai"):
            scene.yohsai.parse_status = message


def _fix_windows_mojibake(text: str) -> str:
    """Repair common Windows Japanese mojibake in status / exception strings."""
    if not text:
        return text

    def _kana_kanji(s: str) -> int:
        return sum(
            1 for c in s if "\u3040" <= c <= "\u30ff" or "\u4e00" <= c <= "\u9fff"
        )

    def _hiragana(s: str) -> int:
        return sum(1 for c in s if "\u3040" <= c <= "\u309f")

    # Prefer UTF-8 recovery first (UTF-8 bytes misread as latin-1/cp1252).
    # Avoid ranking raw CP932 remaps higher — they invent garbage CJK.
    candidates: list[tuple[int, str]] = []
    for enc_from, enc_to, weight in (
        ("latin-1", "utf-8", 100),
        ("cp1252", "utf-8", 100),
        ("latin-1", "cp932", 10),
        ("cp1252", "cp932", 10),
    ):
        try:
            fixed = text.encode(enc_from, errors="strict").decode(enc_to, errors="strict")
        except (UnicodeError, LookupError):
            continue
        if "\ufffd" in fixed or fixed == text:
            continue
        score = _kana_kanji(fixed) * weight + _hiragana(fixed) * 50
        if score > 0:
            candidates.append((score, fixed))
    if candidates:
        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1]
    # Already sensible CJK, or unrecoverable: leave as-is (except known codes).
    if _kana_kanji(text) > 0:
        return text
    if "10061" in text:
        return (
            "WinError 10061: connection refused "
            f"(nothing listening on ZOZO MCP port {ZOZO_MCP_PORT})"
        )
    return text


def _zozo_mcp_port_from_scene(scene, default: int = ZOZO_MCP_PORT) -> int:
    try:
        if hasattr(scene, "zozo_contact_solver"):
            return int(scene.zozo_contact_solver.state.mcp_port) or default
    except Exception:
        pass
    return default


def _ensure_zozo_mcp_server(
    port: int = ZOZO_MCP_PORT, wait_s: float = 3.0
) -> tuple[int, str]:
    """Start ZOZO's MCP HTTP server if it is not already listening.

    Uses the official Extension API (``bpy.ops.mcp.start_server`` /
    ``start_mcp_server``), same as the N-panel Start button.

    Returns ``(actual_port, status_note)``.
    """
    import socket
    import time

    def _port_open(p: int) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", int(p)), timeout=0.2):
                return True
        except OSError:
            return False

    if _port_open(port):
        return int(port), f"MCP already on :{port}"

    errors: list[str] = []
    # 1) Public operator (preferred; updates ZOZO panel state / alt port).
    try:
        if hasattr(bpy.ops, "mcp") and hasattr(bpy.ops.mcp, "start_server"):
            result = bpy.ops.mcp.start_server()
            if not (result == {"FINISHED"} or "FINISHED" in str(result)):
                errors.append(f"mcp.start_server -> {result}")
        else:
            errors.append("bpy.ops.mcp.start_server unavailable")
    except Exception as exc:
        errors.append(f"ops: {_fix_windows_mojibake(str(exc))}")

    actual = _zozo_mcp_port_from_scene(bpy.context.scene, port)

    if not _port_open(actual) and not _port_open(port):
        # 2) Direct Python API (Blender 4.2+ extension module names vary).
        started = False
        for mod_name in (
            "bl_ext.user_default.ppf_contact_solver.mcp.mcp_server",
            "ppf_contact_solver.mcp.mcp_server",
        ):
            try:
                mod = __import__(
                    mod_name, fromlist=["start_mcp_server", "is_mcp_running", "get_mcp_server"]
                )
                if not mod.is_mcp_running():
                    mod.start_mcp_server(int(port))
                server = mod.get_mcp_server()
                if server is not None and getattr(server, "port", None):
                    actual = int(server.port)
                started = True
                break
            except Exception as exc:
                errors.append(f"{mod_name}: {_fix_windows_mojibake(str(exc))}")
        if not started:
            errors.append("ZOZO MCP start API not found (is the extension enabled?)")

    actual = _zozo_mcp_port_from_scene(bpy.context.scene, actual)
    deadline = time.time() + max(0.5, float(wait_s))
    while time.time() < deadline:
        if _port_open(actual):
            return actual, f"MCP started on :{actual}"
        if actual != port and _port_open(port):
            return int(port), f"MCP started on :{port}"
        time.sleep(0.1)

    detail = "; ".join(errors) if errors else "port did not open"
    raise RuntimeError(
        f"Could not start ZOZO MCP on :{port} ({detail}). "
        "Enable ZOZO Contact Solver and use MCP Start, then Prepare again."
    )


def _poll_zozo_mcp() -> float | None:
    global _zozo_process, _zozo_scene_name, _zozo_prepared_summary
    process = _zozo_process
    if process is None:
        return None
    if process.poll() is None:
        return 0.2

    stdout, stderr = process.communicate()
    summary = _zozo_prepared_summary or "Prepared the ZOZO hand-off mesh"
    try:
        lines = [line for line in stdout.splitlines() if line.strip()]
        result = json.loads(lines[-1]) if lines else {}
        if process.returncode != 0 or result.get("status") != "success":
            diagnostic = _fix_windows_mojibake(
                str(result.get("message") or stderr.strip() or "ZOZO MCP setup failed.")
            )
            _set_zozo_status(
                f"{summary}; ZOZO MCP setup failed: {diagnostic[:200]}"
            )
        else:
            capture = str(result.get("capture", "not needed"))
            connection = str(result.get("connection", "")).strip()
            conn_note = f"; {connection}" if connection else ""
            _set_zozo_status(
                f"{summary}; ZOZO MCP ready ({capture}){conn_note}. "
                "Use Transfer, then Run Simulation."
            )
    except Exception as exc:
        diagnostic = _fix_windows_mojibake(
            stderr.strip() or stdout.strip() or str(exc)
        )
        _set_zozo_status(f"{summary}; ZOZO MCP response failed: {diagnostic[:200]}")
    finally:
        _zozo_process = None
        _zozo_scene_name = None
        _zozo_prepared_summary = None
    return None


class YohsaiProperties(PropertyGroup):
    svg_path: StringProperty(
        name="Pattern Path",
        description="Adobe Illustrator PDF pattern file",
        subtype="FILE_PATH",
        default="",
    )
    parse_status: StringProperty(
        name="Status",
        description="Status and warnings (panel message area only; not operator error icons)",
        default="Ready",
        options={"TEXTEDIT_UPDATE"},
    )
    clothes_collection: PointerProperty(
        name="Clothes",
        description="Loaded Yohsai clothes collection used by GRAVITY",
        type=Collection,
    )
    body_object: PointerProperty(
        name="Body",
        description="Fixed body mesh used for GRAVITY collision",
        type=Object,
        poll=_mesh_object_poll,
    )
    # Mode flag for the Select Lock button (pressed look). Independent of the
    # per-part LOCKED_OBJECT_KEY; both-on with auto_lock is forbidden.
    select_lock_mode: BoolProperty(
        name="Select Lock",
        description="Lock selected clothes parts; cannot be on together with Existing Lock",
        default=False,
        update=_update_select_lock_mode,
    )
    auto_lock: BoolProperty(
        name="Existing Lock",
        description="Lock PLACED and DONE parts; cannot be on together with Select Lock",
        default=True,
        update=_update_auto_lock,
    )
    shell_isect_include_body: BoolProperty(
        name="Shell-isect vs Body",
        description=(
            "When on, Prepare runs the full cloth+body shell-isect twin (slow on "
            "high-poly bodies, can take many minutes). When off (default), only "
            "cloth–cloth pairs are checked so Prepare stays practical. Body copy "
            "is still built for ZOZO either way"
        ),
        default=False,
    )


class YOHSAI_OT_load_svg(Operator):
    bl_idname = "yohsai.load_svg"
    bl_label = "Load"
    bl_description = "Parse the selected Illustrator PDF and load its Yohsai JSON"
    bl_options = {"REGISTER"}

    def execute(self, context):
        global _parse_process, _parse_scene_name, _parse_svg_path, _parse_action, _parse_collection_name
        if _parse_process is not None and _parse_process.poll() is None:
            self.report({"WARNING"}, "A pattern is already being loaded.")
            return {"CANCELLED"}

        raw_path = context.scene.yohsai.svg_path
        if not raw_path:
            self.report({"ERROR"}, "Select a PDF pattern file first.")
            return {"CANCELLED"}
        svg_path = str(Path(bpy.path.abspath(raw_path)).resolve())
        if not os.path.isfile(svg_path) or Path(svg_path).suffix.lower() != ".pdf":
            self.report({"ERROR"}, "Pattern Path must point to an existing .pdf file.")
            return {"CANCELLED"}

        parser_path = Path(__file__).with_name(_PARSER_FILENAME)
        if not parser_path.is_file():
            self.report({"ERROR"}, f"Parser program is missing: {_PARSER_FILENAME}")
            return {"CANCELLED"}
        try:
            python_path = _bundled_python()
            creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            _parse_process = subprocess.Popen(
                [python_path, str(parser_path), svg_path],
                cwd=_parser_data_dir(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=creationflags,
                env=_parser_environment(),
            )
        except Exception as exc:
            self.report({"ERROR"}, f"Could not start pattern parser: {exc}")
            return {"CANCELLED"}

        _parse_scene_name = context.scene.name
        _parse_svg_path = svg_path
        _parse_action = "LOAD"
        _parse_collection_name = None
        context.scene.yohsai.parse_status = "Loading..."
        if not bpy.app.timers.is_registered(_poll_svg_parser):
            bpy.app.timers.register(_poll_svg_parser, first_interval=0.2)
        return {"FINISHED"}


class YOHSAI_OT_update_svg(Operator):
    bl_idname = "yohsai.update_svg"
    bl_label = "Update"
    bl_description = "Recut the selected Clothes collection from the saved PDF and transfer its current 3D placement"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        global _parse_process, _parse_scene_name, _parse_svg_path, _parse_action, _parse_collection_name
        if _parse_process is not None and _parse_process.poll() is None:
            self.report({"WARNING"}, "A pattern is already being processed.")
            return {"CANCELLED"}
        props = context.scene.yohsai
        collection = props.clothes_collection
        if collection is None or collection.get("yohsai_role") != "clothes":
            self.report({"ERROR"}, "Select a loaded Clothes collection before Update.")
            return {"CANCELLED"}
        raw_path = props.svg_path
        if not raw_path:
            self.report({"ERROR"}, "Select the original PDF file first.")
            return {"CANCELLED"}
        svg_path = str(Path(bpy.path.abspath(raw_path)).resolve())
        if not os.path.isfile(svg_path) or Path(svg_path).suffix.lower() != ".pdf":
            self.report({"ERROR"}, "Pattern Path must point to the existing source .pdf file.")
            return {"CANCELLED"}
        source_path = str(Path(str(collection.get("yohsai_source_svg", ""))).resolve())
        if os.path.normcase(svg_path) != os.path.normcase(source_path):
            self.report({"ERROR"}, "Update must use the same pattern file that created the selected Clothes collection.")
            return {"CANCELLED"}
        parser_path = Path(__file__).with_name(_PARSER_FILENAME)
        try:
            python_path = _bundled_python()
            creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            _parse_process = subprocess.Popen(
                [python_path, str(parser_path), svg_path],
                cwd=_parser_data_dir(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=creationflags,
                env=_parser_environment(),
            )
        except Exception as exc:
            self.report({"ERROR"}, f"Could not start pattern parser: {exc}")
            return {"CANCELLED"}
        _parse_scene_name = context.scene.name
        _parse_svg_path = svg_path
        _parse_action = "UPDATE"
        _parse_collection_name = collection.name
        props.parse_status = "Updating..."
        if not bpy.app.timers.is_registered(_poll_svg_parser):
            bpy.app.timers.register(_poll_svg_parser, first_interval=0.2)
        return {"FINISHED"}


def _prepare_gravity(context, collection) -> tuple[Object, ...]:
    """Bring seams and the Sewing plan up to date before a solver runs.

    Both GRAVITY buttons need the same starting state, so this is shared;
    only the solve that follows differs.
    """
    # Gather seams: recut any sewing seam whose two sides carry unequal
    # vertex counts so they can pair 1:1.  This changes topology on the
    # affected panels, so force a sewing rebuild below.
    if adapt_seam_counts(context, collection):
        collection["yohsai_sewing_verified"] = False

    pending_parts = mark_moved_parts_pending(collection)
    sewing_required = bool(pending_parts) or (
        not has_kitsuke_session(collection)
        and not bool(collection.get("yohsai_sewing_verified", False))
    )
    if sewing_required:
        if not pending_parts and len(participating_parts(collection)) < 2:
            raise KitsukeError("Move at least two connected pattern parts before pressing GRAVITY.")
        # Each new pending stage gets sewing connections from its current
        # Object Mode placement.  Completed parts remain in the plan as
        # locked anchors when Auto is on.  A changed Update signature also
        # rebuilds from completed participants so the hidden Sewing action
        # is never required for recovery.
        clear_kitsuke_session(collection)
        remove_sewn_preview(collection, reveal_parts=True)
        collection["yohsai_sewing_verified"] = False
        create_sewn_mesh(context, collection)
    return pending_parts


def _run_gravity(
    operator: Operator,
    context,
    gravity_magnitude: float,
    solver_iterations: int = SOLVER_ITERATIONS,
):
    props = context.scene.yohsai
    collection = props.clothes_collection
    pending_parts: tuple[Object, ...] = ()
    try:
        if collection is None or collection.get("yohsai_role") != "clothes":
            raise KitsukeError("No loaded Yohsai clothes collection is selected.")
        if props.body_object is None:
            raise KitsukeError("Select a mesh Body before pressing GRAVITY.")

        pending_parts = _prepare_gravity(context, collection)
        message = advance_kitsuke(
            context,
            collection,
            props.body_object,
            gravity_magnitude,
            solver_iterations,
        )
        mark_pending_parts_done(pending_parts)
    except Exception as exc:
        message = str(exc).strip() or type(exc).__name__
        props.parse_status = f"GRAVITY failed: {message[:240]}"
        operator.report({"ERROR"}, message)
        return {"CANCELLED"}
    props.parse_status = message
    operator.report({"INFO"}, message)
    return {"FINISHED"}


def _run_zero_gravity(operator: Operator, context):
    """Sew every seam in one contact-solver job.

    The square-lattice session is dropped first: it holds cloth state this
    solve replaces wholesale, and keeping a stale one would let the next
    Normal GRAVITY press continue from cloth it never simulated.
    """
    props = context.scene.yohsai
    collection = props.clothes_collection
    pending_parts: tuple[Object, ...] = ()
    try:
        if collection is None or collection.get("yohsai_role") != "clothes":
            raise KitsukeError("No loaded Yohsai clothes collection is selected.")
        if props.body_object is None:
            raise KitsukeError("Select a mesh Body before pressing Zero GRAVITY.")

        pending_parts = _prepare_gravity(context, collection)
        clear_kitsuke_session(collection)
        remove_sewn_preview(collection, reveal_parts=True)
        message = sew_zero_gravity(context, collection, props.body_object)
        mark_pending_parts_done(pending_parts)
    except Exception as exc:
        message = str(exc).strip() or type(exc).__name__
        props.parse_status = f"Zero GRAVITY failed: {message[:240]}"
        operator.report({"ERROR"}, message)
        return {"CANCELLED"}
    props.parse_status = message
    operator.report({"INFO"}, message)
    return {"FINISHED"}


class YOHSAI_OT_kitsuke_zero_gravity(Operator):
    bl_idname = "yohsai.kitsuke_zero_gravity"
    bl_label = "Zero GRAVITY"
    bl_description = (
        "Run automatic Sewing, then close every seam with the ZOZO Contact "
        f"Solver in one job ({SEWING_FRAMES} frames sewing, {SETTLE_FRAMES} "
        "settling; no gravity, static Body). Takes seconds, not a frame. "
        "Sews from the flat panels, so pressing it again re-sews"
    )
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return context.mode == "OBJECT"

    def execute(self, context):
        return _run_zero_gravity(self, context)


class YOHSAI_OT_kitsuke(Operator):
    bl_idname = "yohsai.kitsuke"
    bl_label = "Normal GRAVITY"
    bl_description = "Run automatic Sewing, then advance with normal gravity (9.81 m/s²)"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return context.mode == "OBJECT"

    def execute(self, context):
        return _run_gravity(self, context, NORMAL_GRAVITY_M_PER_SECOND_SQUARED, SOLVER_ITERATIONS)


class YOHSAI_OT_prepare_zozo(Operator):
    bl_idname = "yohsai.prepare_zozo"
    bl_label = "Prepare for ZOZO"
    bl_description = (
        "Build ZOZO cloth/body copies, run shell-isect check→fix→check "
        "(cloth-only by default; enable Shell-isect vs Body for full twin); "
        f"on PASS start ZOZO MCP if needed and configure on port {ZOZO_MCP_PORT}. "
        "On NG, stop and report"
    )
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        return context.mode == "OBJECT"

    def execute(self, context):
        global _zozo_process, _zozo_scene_name, _zozo_prepared_summary
        props = context.scene.yohsai
        if _zozo_process is not None and _zozo_process.poll() is None:
            self.report({"WARNING"}, "ZOZO MCP configuration is already running.")
            return {"CANCELLED"}
        try:
            prepared = prepare_for_zozo(
                context,
                props.clothes_collection,
                props.body_object,
                shell_isect_include_body=bool(props.shell_isect_include_body),
            )
        except ZozoHandoffError as exc:
            message = _fix_windows_mojibake(str(exc).strip() or type(exc).__name__)
            # Status box only — never self.report ERROR (avoids レポート:エラー).
            ver = library_version()
            suffix = f" [shell-isect {ver}]" if ver else " [shell-isect unavailable]"
            props.parse_status = f"Prepare for ZOZO stopped: {message}{suffix}"
            return {"CANCELLED"}
        except Exception as exc:
            message = _fix_windows_mojibake(str(exc).strip() or type(exc).__name__)
            ver = library_version()
            suffix = f" [shell-isect {ver}]" if ver else " [shell-isect unavailable]"
            props.parse_status = f"Prepare for ZOZO failed: {message}{suffix}"
            return {"CANCELLED"}

        shell_suffix = f" [{prepared.shell_isect.version_suffix()}]"

        # shell-isect NG and other soft stops: status box only, no MCP / no report.
        if prepared.abort_message:
            # error_report already ends with [shell-isect x.y.z]
            props.parse_status = f"Prepare for ZOZO stopped: {prepared.abort_message}"
            return {"CANCELLED"}

        # Triangle self-intersection already gated by shell-isect; MCP only.
        summary = (
            f"Prepared {prepared.seam_count} ZOZO stitches "
            f"(minimum {prepared.minimum_output_seam_distance_m * 1000.0:.2f} mm)"
            + shell_suffix
        )
        try:
            mcp_port, mcp_note = _ensure_zozo_mcp_server(ZOZO_MCP_PORT)
            config = prepared.mcp_configuration(context.scene)
            config["port"] = int(mcp_port)
            config_path = Path(_parser_data_dir()) / _ZOZO_CONFIG_FILENAME
            config_path.write_text(
                json.dumps(config, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            client_path = Path(__file__).with_name(_ZOZO_CLIENT_FILENAME)
            creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            _zozo_process = subprocess.Popen(
                [_bundled_python(), str(client_path), str(config_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=creationflags,
                env=_parser_environment(),
            )
            _zozo_scene_name = context.scene.name
            _zozo_prepared_summary = summary
            # Status box only — no operator report icon for prepare success/warnings.
            props.parse_status = (
                f"{summary}; {mcp_note}; configuring ZOZO MCP on :{mcp_port}..."
            )
            if not bpy.app.timers.is_registered(_poll_zozo_mcp):
                bpy.app.timers.register(_poll_zozo_mcp, first_interval=0.2)
        except Exception as exc:
            message = _fix_windows_mojibake(str(exc).strip() or type(exc).__name__)
            props.parse_status = (
                f"{summary}; copies are ready, but MCP could not start: {message[:240]}"
            )
        return {"FINISHED"}


class YOHSAI_PT_main(Panel):
    bl_idname = "YOHSAI_PT_main"
    bl_label = "Yohsai"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Yohsai"

    def draw(self, context):
        layout = self.layout
        props = context.scene.yohsai
        layout.label(text=f"Yohsai v{_version()}")
        layout.separator(factor=0.4)
        inputs = layout.column(align=True)
        inputs.label(text="Inputs")
        inputs.prop(props, "svg_path")
        inputs.prop(props, "clothes_collection")
        inputs.prop(props, "body_object")
        lock_row = inputs.row(align=True)
        # Select Lock: operator button (toggle look via depress).
        lock_row.operator(
            YOHSAI_OT_lock_selection.bl_idname,
            text="Select Lock",
            depress=bool(props.select_lock_mode),
        )
        lock_row.prop(props, "auto_lock", text="Existing Lock", toggle=True)
        layout.separator(factor=0.4)
        actions = layout.column(align=True)
        actions.operator(YOHSAI_OT_load_svg.bl_idname, text="Load")
        actions.operator(YOHSAI_OT_update_svg.bl_idname, text="Update")
        gravity_actions = actions.row(align=True)
        gravity_actions.operator(YOHSAI_OT_kitsuke_zero_gravity.bl_idname, text="Zero GRAVITY")
        gravity_actions.operator(YOHSAI_OT_kitsuke.bl_idname, text="Normal GRAVITY")
        actions.operator(YOHSAI_OT_prepare_zozo.bl_idname, text="Prepare for ZOZO")
        actions.prop(props, "shell_isect_include_body", text="Shell-isect vs Body")
        layout.separator(factor=0.5)
        _draw_status_box(layout, props)


class YOHSAI_OT_lock_selection(Operator):
    bl_idname = "yohsai.lock_selection"
    bl_label = "Select Lock"
    bl_description = (
        "Lock or unlock selected clothes parts. "
        "Cannot be on together with Existing Lock"
    )
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return context.mode == "OBJECT"

    def execute(self, context):
        props = context.scene.yohsai
        _toggle_select_lock(props)
        self.report({"INFO"}, props.parse_status)
        return {"FINISHED"}


_classes = (
    YohsaiProperties,
    YOHSAI_OT_load_svg,
    YOHSAI_OT_update_svg,
    YOHSAI_OT_lock_selection,
    YOHSAI_OT_kitsuke_zero_gravity,
    YOHSAI_OT_kitsuke,
    YOHSAI_OT_prepare_zozo,
    YOHSAI_PT_main,
)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.yohsai = PointerProperty(type=YohsaiProperties)
    _register_history_handlers()
    bpy.app.translations.register(__package__, translations_dict)


def unregister():
    global _zozo_process, _zozo_scene_name, _zozo_prepared_summary
    bpy.app.translations.unregister(__package__)
    _unregister_history_handlers()
    reset_runtime_epoch()
    if bpy.app.timers.is_registered(_poll_svg_parser):
        bpy.app.timers.unregister(_poll_svg_parser)
    if bpy.app.timers.is_registered(_poll_zozo_mcp):
        bpy.app.timers.unregister(_poll_zozo_mcp)
    if _zozo_process is not None and _zozo_process.poll() is None:
        _zozo_process.terminate()
    _zozo_process = None
    _zozo_scene_name = None
    _zozo_prepared_summary = None
    del bpy.types.Scene.yohsai
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
