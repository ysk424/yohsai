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
from bpy.props import PointerProperty, StringProperty
from bpy.types import Collection, Object, Operator, Panel, PropertyGroup

from .kitsuke import (
    DEFAULT_GRAVITY_M_PER_SECOND_SQUARED,
    DEFAULT_SEAM_CLOSURE_PER_CLICK_M,
    KitsukeError,
    advance_kitsuke,
    clear_kitsuke_session,
    clear_sessions,
    has_kitsuke_session,
    reset_runtime_epoch,
)
from .mesh_loader import create_clothes_mesh, create_sewn_mesh, update_clothes_mesh


_parse_process: subprocess.Popen[str] | None = None
_parse_scene_name: str | None = None
_parse_svg_path: str | None = None
_parse_action: str | None = None
_parse_collection_name: str | None = None
_loaded_pattern_json: dict | None = None
_PARSER_FILENAME = "yohsai_svg_parser.py"
_JSON_FILENAME = "yohsai_pattern.json"


@persistent
def _history_change_post(_unused) -> None:
    """Discard non-undoable Taichi objects after Blender restores its data."""
    clear_sessions()


@persistent
def _file_load_pre(_unused) -> None:
    """Give every loaded file a new recovery epoch and no stale Taichi objects."""
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


def _mesh_object_poll(_properties, obj: Object) -> bool:
    """Only allow actual mesh objects in the shared Body field."""
    return obj.type == "MESH"


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
                message += "; Sewing required"
            _set_parse_status(message)
        else:
            clothes_collection = create_clothes_mesh(bpy.context, validated_document)
            if scene is not None and hasattr(scene, "yohsai"):
                scene.yohsai.clothes_collection = clothes_collection
            panel_count = len(validated_document["panels"])
            _set_parse_status(f"Loaded {clothes_collection.name}: {panel_count} part(s)")
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


class YohsaiProperties(PropertyGroup):
    svg_path: StringProperty(
        name="Pattern Path",
        description="Adobe Illustrator PDF or SVG pattern file",
        subtype="FILE_PATH",
        default="",
    )
    parse_status: StringProperty(
        name="Status",
        default="Ready",
    )
    clothes_collection: PointerProperty(
        name="Clothes",
        description="Loaded Yohsai clothes collection used by Sewing",
        type=Collection,
    )
    body_object: PointerProperty(
        name="Body",
        description="Fixed body mesh used for Kitsuke collision",
        type=Object,
        poll=_mesh_object_poll,
    )


class YOHSAI_OT_load_svg(Operator):
    bl_idname = "yohsai.load_svg"
    bl_label = "Load"
    bl_description = "Parse the selected Illustrator PDF or SVG and load its Yohsai JSON"
    bl_options = {"REGISTER"}

    def execute(self, context):
        global _parse_process, _parse_scene_name, _parse_svg_path, _parse_action, _parse_collection_name
        if _parse_process is not None and _parse_process.poll() is None:
            self.report({"WARNING"}, "A pattern is already being loaded.")
            return {"CANCELLED"}

        raw_path = context.scene.yohsai.svg_path
        if not raw_path:
            self.report({"ERROR"}, "Select a PDF or SVG pattern file first.")
            return {"CANCELLED"}
        svg_path = str(Path(bpy.path.abspath(raw_path)).resolve())
        if not os.path.isfile(svg_path) or Path(svg_path).suffix.lower() not in {".svg", ".pdf"}:
            self.report({"ERROR"}, "Pattern Path must point to an existing .pdf or .svg file.")
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
    bl_description = "Recut the selected Clothes collection from the saved PDF or SVG and transfer its current 3D placement"
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
            self.report({"ERROR"}, "Select the original PDF or SVG file first.")
            return {"CANCELLED"}
        svg_path = str(Path(bpy.path.abspath(raw_path)).resolve())
        if not os.path.isfile(svg_path) or Path(svg_path).suffix.lower() not in {".svg", ".pdf"}:
            self.report({"ERROR"}, "Pattern Path must point to the existing source .pdf or .svg file.")
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


class YOHSAI_OT_sewing(Operator):
    bl_idname = "yohsai.sewing"
    bl_label = "Sewing"
    bl_description = "Combine the positioned cloth parts and create ordered loose sewing edges"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        props = context.scene.yohsai
        collection = props.clothes_collection
        if has_kitsuke_session(collection):
            message = "Kitsuke has already started. Reload the pattern before creating a new Sewing preview."
            props.parse_status = f"Sewing failed: {message}"
            self.report({"ERROR"}, message)
            return {"CANCELLED"}
        try:
            sewn_object = create_sewn_mesh(context, collection)
        except Exception as exc:
            message = str(exc).strip() or type(exc).__name__
            props.parse_status = f"Sewing failed: {message[:240]}"
            self.report({"ERROR"}, message)
            return {"CANCELLED"}
        props.parse_status = f"Sewn {sewn_object.name}"
        self.report({"INFO"}, f"Created {sewn_object.name}")
        return {"FINISHED"}


class YOHSAI_OT_kitsuke(Operator):
    bl_idname = "yohsai.kitsuke"
    bl_label = "Kitsuke"
    bl_description = "Advance a short cloth simulation, then restore the separate parts for manual placement"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return context.mode == "OBJECT"

    def execute(self, context):
        props = context.scene.yohsai
        try:
            message = advance_kitsuke(
                context,
                props.clothes_collection,
                props.body_object,
                DEFAULT_GRAVITY_M_PER_SECOND_SQUARED,
                DEFAULT_SEAM_CLOSURE_PER_CLICK_M,
            )
        except KitsukeError as exc:
            message = str(exc).strip() or type(exc).__name__
            props.parse_status = f"Kitsuke failed: {message[:240]}"
            self.report({"ERROR"}, message)
            return {"CANCELLED"}
        except Exception as exc:
            message = str(exc).strip() or type(exc).__name__
            props.parse_status = f"Kitsuke failed: {message[:240]}"
            self.report({"ERROR"}, message)
            return {"CANCELLED"}
        props.parse_status = message
        self.report({"INFO"}, message)
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
        layout.separator(factor=0.4)
        actions = layout.column(align=True)
        actions.operator(YOHSAI_OT_load_svg.bl_idname, text="Load")
        actions.operator(YOHSAI_OT_update_svg.bl_idname, text="Update")
        actions.operator(YOHSAI_OT_sewing.bl_idname, text="Sewing")
        actions.operator(YOHSAI_OT_kitsuke.bl_idname, text="Kitsuke")
        layout.label(text=props.parse_status)


_classes = (
    YohsaiProperties,
    YOHSAI_OT_load_svg,
    YOHSAI_OT_update_svg,
    YOHSAI_OT_sewing,
    YOHSAI_OT_kitsuke,
    YOHSAI_PT_main,
)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.yohsai = PointerProperty(type=YohsaiProperties)
    _register_history_handlers()


def unregister():
    _unregister_history_handlers()
    reset_runtime_epoch()
    if bpy.app.timers.is_registered(_poll_svg_parser):
        bpy.app.timers.unregister(_poll_svg_parser)
    del bpy.types.Scene.yohsai
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
