# SPDX-License-Identifier: GPL-3.0-or-later
"""Yohsai Blender N-panel."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from collections import defaultdict
from html import escape
from pathlib import Path

import bpy
from bpy.props import FloatProperty, PointerProperty, StringProperty
from bpy.types import Collection, Object, Operator, Panel, PropertyGroup
from mathutils import Vector

from .kitsuke import (
    DEFAULT_GRAVITY_M_PER_SECOND_SQUARED,
    DEFAULT_SEAM_CLOSURE_PER_CLICK_M,
    KitsukeError,
    advance_kitsuke,
    clear_sessions,
    has_kitsuke_session,
)
from .mesh_loader import create_clothes_mesh, create_sewn_mesh


_parse_process: subprocess.Popen[str] | None = None
_parse_scene_name: str | None = None
_parse_svg_path: str | None = None
_loaded_pattern_json: dict | None = None
_PARSER_FILENAME = "yohsai_svg_parser.py"
_JSON_FILENAME = "yohsai_pattern.json"


def _version() -> str:
    try:
        path = os.path.join(os.path.dirname(__file__), "blender_manifest.toml")
        with open(path, "rb") as f:
            return str(tomllib.load(f).get("version", "?"))
    except Exception:
        return "?"


def _default_output_dir() -> str:
    if bpy.data.filepath:
        return os.path.dirname(bpy.data.filepath)
    return os.path.expanduser("~")


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
        raise ValueError("Parser output belongs to a different SVG file.")
    if not isinstance(document.get("panels"), list):
        raise ValueError("Parser output has no panels array.")
    return document


def _poll_svg_parser() -> float | None:
    global _parse_process, _parse_scene_name, _parse_svg_path, _loaded_pattern_json
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
        clothes_collection = create_clothes_mesh(bpy.context, validated_document)
        scene = bpy.data.scenes.get(_parse_scene_name) if _parse_scene_name else None
        if scene is not None and hasattr(scene, "yohsai"):
            scene.yohsai.clothes_collection = clothes_collection
        _loaded_pattern_json = validated_document
        panel_count = len(validated_document["panels"])
        _set_parse_status(f"Loaded {clothes_collection.name}: {panel_count} part(s)")
    except Exception as exc:
        _set_parse_status(f"Load failed: {str(exc).strip()[:240]}")
    finally:
        _parse_process = None
        _parse_scene_name = None
        _parse_svg_path = None
    return None


def _projection_data(obj: Object, axis: str) -> tuple[list[tuple[float, float, float, float]], tuple[float, float, float, float]]:
    depsgraph = bpy.context.evaluated_depsgraph_get()
    eval_obj = obj.evaluated_get(depsgraph)
    mesh = eval_obj.to_mesh()
    try:
        mesh.calc_loop_triangles()
        matrix = eval_obj.matrix_world
        normal_matrix = matrix.to_3x3().inverted().transposed()
        view_dir = Vector((0.0, 1.0, 0.0)) if axis == "Y" else Vector((1.0, 0.0, 0.0))

        edge_faces: dict[tuple[int, int], list[float]] = defaultdict(list)
        world_vertices = [matrix @ vertex.co for vertex in mesh.vertices]

        for poly in mesh.polygons:
            normal = (normal_matrix @ poly.normal).normalized()
            facing = normal.dot(view_dir)
            for edge_key in poly.edge_keys:
                edge_faces[tuple(sorted(edge_key))].append(facing)

        segments: list[tuple[float, float, float, float]] = []
        points: list[tuple[float, float]] = []
        for edge_key, facings in edge_faces.items():
            if len(facings) == 1:
                is_silhouette = True
            else:
                is_silhouette = min(facings) <= 0.0 <= max(facings)
            if not is_silhouette:
                continue

            a = world_vertices[edge_key[0]]
            b = world_vertices[edge_key[1]]
            if axis == "Y":
                segment = (a.x, a.z, b.x, b.z)
            else:
                segment = (a.y, a.z, b.y, b.z)
            segments.append(segment)
            points.extend(((segment[0], segment[1]), (segment[2], segment[3])))

        if not points:
            raise ValueError("No silhouette edges were found.")

        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        return segments, (min(xs), min(ys), max(xs), max(ys))
    finally:
        eval_obj.to_mesh_clear()


def _mm_per_blender_unit(context) -> float:
    return max(context.scene.unit_settings.scale_length, 0.000001) * 1000.0


def _svg_path_data(segments: list[tuple[float, float, float, float]], min_x: float, max_y: float, mm_per_unit: float) -> str:
    commands = []
    for x1, y1, x2, y2 in segments:
        sx1 = (x1 - min_x) * mm_per_unit
        sy1 = (max_y - y1) * mm_per_unit
        sx2 = (x2 - min_x) * mm_per_unit
        sy2 = (max_y - y2) * mm_per_unit
        commands.append(f"M {sx1:.3f} {sy1:.3f} L {sx2:.3f} {sy2:.3f}")
    return " ".join(commands)


def _write_silhouette_svg(context, obj: Object, axis: str, filepath: str) -> None:
    segments, bounds = _projection_data(obj, axis)
    min_x, min_y, max_x, max_y = bounds
    width = max(max_x - min_x, 0.001)
    height = max(max_y - min_y, 0.001)
    mm_per_unit = _mm_per_blender_unit(context)
    padding = 10.0
    svg_width = width * mm_per_unit + padding * 2.0
    svg_height = height * mm_per_unit + padding * 2.0
    view_box = f"0 0 {svg_width:.3f} {svg_height:.3f}"
    path_data = _svg_path_data(segments, min_x, max_y, mm_per_unit)
    label = "XZ shadow" if axis == "Y" else "YZ shadow"

    content = f"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" version="1.1" width="{svg_width:.3f}mm" height="{svg_height:.3f}mm" viewBox="{view_box}">
  <title>{escape(obj.name)} {label}</title>
  <g id="{escape(obj.name)}-{axis}-silhouette" transform="translate({padding:.3f} {padding:.3f})">
    <path d="{path_data}" fill="none" stroke="#000000" stroke-width="0.25" stroke-linecap="round" stroke-linejoin="round"/>
  </g>
</svg>
"""
    with open(filepath, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)


class YohsaiProperties(PropertyGroup):
    svg_path: StringProperty(
        name="SVG Path",
        description="Adobe Illustrator SVG containing the CLOTHES layer",
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
        description="Fixed body mesh used for Kitsuke collision and silhouette projection",
        type=Object,
        poll=_mesh_object_poll,
    )
    kitsuke_gravity: FloatProperty(
        name="Gravity",
        description="Downward acceleration used by each Kitsuke step",
        default=DEFAULT_GRAVITY_M_PER_SECOND_SQUARED,
        min=0.0,
        soft_max=9.81,
        max=100.0,
        precision=3,
        unit="ACCELERATION",
    )
    kitsuke_seam_pull_mm: FloatProperty(
        name="Seam Pull",
        description="Distance removed from every transient seam target per Kitsuke click",
        default=DEFAULT_SEAM_CLOSURE_PER_CLICK_M * 1000.0,
        min=0.0,
        soft_max=30.0,
        max=1000.0,
        precision=2,
    )
    output_dir: StringProperty(
        name="Output",
        description="Folder for Illustrator-readable silhouette SVG files",
        subtype="DIR_PATH",
        default="",
    )


class YOHSAI_OT_export_silhouette(Operator):
    bl_idname = "yohsai.export_silhouette"
    bl_label = "Silhouette"
    bl_description = "Export XZ and YZ body shadows as SVG files readable by Adobe Illustrator"
    bl_options = {"REGISTER"}

    def execute(self, context):
        props = context.scene.yohsai
        obj = props.body_object
        if obj is None:
            self.report({"ERROR"}, "Select a body object first.")
            return {"CANCELLED"}
        if obj.type != "MESH":
            self.report({"ERROR"}, "Body must be a mesh object.")
            return {"CANCELLED"}

        output_dir = bpy.path.abspath(props.output_dir) if props.output_dir else _default_output_dir()
        os.makedirs(output_dir, exist_ok=True)
        safe_name = bpy.path.clean_name(obj.name)
        xz_path = os.path.join(output_dir, f"{safe_name}_shadow_xz.svg")
        yz_path = os.path.join(output_dir, f"{safe_name}_shadow_yz.svg")

        try:
            _write_silhouette_svg(context, obj, "Y", xz_path)
            _write_silhouette_svg(context, obj, "X", yz_path)
        except Exception as exc:
            self.report({"ERROR"}, f"Silhouette export failed: {exc}")
            return {"CANCELLED"}

        self.report({"INFO"}, f"Exported {os.path.basename(xz_path)} and {os.path.basename(yz_path)}")
        return {"FINISHED"}


class YOHSAI_OT_load_svg(Operator):
    bl_idname = "yohsai.load_svg"
    bl_label = "Load"
    bl_description = "Parse the selected Illustrator SVG and load its Yohsai JSON"
    bl_options = {"REGISTER"}

    def execute(self, context):
        global _parse_process, _parse_scene_name, _parse_svg_path
        if _parse_process is not None and _parse_process.poll() is None:
            self.report({"WARNING"}, "An SVG is already being loaded.")
            return {"CANCELLED"}

        raw_path = context.scene.yohsai.svg_path
        if not raw_path:
            self.report({"ERROR"}, "Select an SVG file first.")
            return {"CANCELLED"}
        svg_path = str(Path(bpy.path.abspath(raw_path)).resolve())
        if not os.path.isfile(svg_path) or Path(svg_path).suffix.lower() != ".svg":
            self.report({"ERROR"}, "SVG Path must point to an existing .svg file.")
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
            )
        except Exception as exc:
            self.report({"ERROR"}, f"Could not start SVG parser: {exc}")
            return {"CANCELLED"}

        _parse_scene_name = context.scene.name
        _parse_svg_path = svg_path
        context.scene.yohsai.parse_status = "Loading..."
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
                props.kitsuke_gravity,
                props.kitsuke_seam_pull_mm / 1000.0,
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
        layout.prop(props, "svg_path")
        layout.operator(YOHSAI_OT_load_svg.bl_idname, text="Load")
        layout.prop(props, "clothes_collection")
        layout.operator(YOHSAI_OT_sewing.bl_idname, text="Sewing")
        layout.prop(props, "body_object")
        tuning = layout.box()
        tuning.label(text="Kitsuke Tuning (Temporary)")
        tuning.prop(props, "kitsuke_gravity")
        tuning.prop(props, "kitsuke_seam_pull_mm", text="Seam Pull (mm/click)")
        layout.operator(YOHSAI_OT_kitsuke.bl_idname, text="Kitsuke")
        layout.label(text=props.parse_status)
        layout.separator(factor=0.8)
        layout.label(text="Silhouette Export")
        layout.prop(props, "output_dir")
        layout.operator(YOHSAI_OT_export_silhouette.bl_idname, text="Silhouette")


_classes = (
    YohsaiProperties,
    YOHSAI_OT_export_silhouette,
    YOHSAI_OT_load_svg,
    YOHSAI_OT_sewing,
    YOHSAI_OT_kitsuke,
    YOHSAI_PT_main,
)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.yohsai = PointerProperty(type=YohsaiProperties)


def unregister():
    clear_sessions()
    if bpy.app.timers.is_registered(_poll_svg_parser):
        bpy.app.timers.unregister(_poll_svg_parser)
    del bpy.types.Scene.yohsai
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
