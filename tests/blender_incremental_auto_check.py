"""Blender-background regression check for three-state automatic Gravity stages."""

from __future__ import annotations

import importlib
import sys
import tempfile
from pathlib import Path

import bpy


repo = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo / "tests"))
for wheel in sorted((repo / "wheels").glob("*.whl")):
    sys.path.insert(0, str(wheel))

from source_package import load_source_package  # noqa: E402


SVG = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 260 100">
  <g id="CLOTHES">
    <path id="p1" d="M 20 20 L 60 20 L 60 60 L 20 60 Z"/>
    <path id="p2" d="M 80 20 L 120 20 L 120 60 L 80 60 Z"/>
    <path id="p3" d="M 140 20 L 180 20 L 180 60 L 140 60 Z"/>
    <path id="p4" d="M 200 20 L 240 20 L 240 60 L 200 60 Z"/>
    <text x="40" y="40">#P1</text>
    <text x="100" y="40">#P2</text>
    <text x="160" y="40">#P3</text>
    <text x="220" y="40">#P4</text>
    <text x="61" y="40">A</text>
    <text x="79" y="40">A</text>
    <text x="121" y="40">B</text>
    <text x="139" y="40">B</text>
    <text x="181" y="40">C</text>
    <text x="199" y="40">C</text>
    <text x="10" y="85">@S100cm</text>
    <path d="M 20 90 L 120 90"/>
  </g>
</svg>
"""

yohsai = load_source_package(repo)
finished_garment = sys.modules[f"{yohsai.__name__}.finished_garment"]
kitsuke = sys.modules[f"{yohsai.__name__}.kitsuke"]
mesh_loader = sys.modules[f"{yohsai.__name__}.mesh_loader"]
parser = importlib.import_module(f"{yohsai.__name__}.yohsai_svg_parser")


yohsai.register()
try:
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "incremental.svg"
        source.write_text(SVG, encoding="utf-8")
        document = parser._parse_legacy_svg(source)
        assert sorted(document["sewing_groups"]) == ["A", "B", "C"]

        collection = mesh_loader.create_clothes_mesh(bpy.context, document)
        bpy.context.scene.yohsai.clothes_collection = collection
        parts = sorted(
            (obj for obj in collection.objects if obj.get("yohsai_role") == "part"),
            key=lambda obj: int(obj["yohsai_panel_index"]),
        )
        assert len(parts) == 4
        assert not mesh_loader.participating_parts(collection)
        assert [mesh_loader.part_gravity_state(obj) for obj in parts] == [
            mesh_loader.GRAVITY_STATE_PLACED,
        ] * 4
        assert all(bool(obj[mesh_loader.LOCKED_OBJECT_KEY]) for obj in parts)
        assert bpy.context.scene.yohsai.auto_lock

        bpy.ops.mesh.primitive_cube_add(size=1.0, location=(100.0, 100.0, 100.0))
        bpy.context.scene.yohsai.body_object = bpy.context.object

        parts[0].location.y += 0.01
        bpy.context.view_layer.update()
        try:
            rejected = bpy.ops.yohsai.kitsuke_zero_gravity()
        except RuntimeError as exc:
            assert "at least two" in str(exc)
        else:
            assert rejected == {"CANCELLED"}
        assert mesh_loader.part_gravity_state(parts[0]) == mesh_loader.GRAVITY_STATE_PENDING
        assert all(
            mesh_loader.part_gravity_state(obj) == mesh_loader.GRAVITY_STATE_PLACED
            for obj in parts[1:]
        )

        parts[1].location.y += 0.01
        bpy.context.view_layer.update()
        assert bpy.ops.yohsai.kitsuke_zero_gravity() == {"FINISHED"}
        first_session = kitsuke._sessions[collection.as_pointer()]
        assert [part.obj for part in first_session.parts] == parts[:2]
        first_plan = mesh_loader.build_sewing_plan(collection)
        assert first_plan.parts == tuple(parts[:2])
        assert first_plan.labels == ("A",)
        assert not any(obj.get("yohsai_role") == "sewn" for obj in collection.objects)
        assert [mesh_loader.part_gravity_state(obj) for obj in parts] == [
            mesh_loader.GRAVITY_STATE_DONE,
            mesh_loader.GRAVITY_STATE_DONE,
            mesh_loader.GRAVITY_STATE_PLACED,
            mesh_loader.GRAVITY_STATE_PLACED,
        ]
        assert [bool(obj.get(mesh_loader.LOCKED_OBJECT_KEY, False)) for obj in parts] == [
            False, False, True, True
        ]
        # State completion alone does not change Lock, so GRAVITY can repeat.
        assert bpy.ops.yohsai.kitsuke_zero_gravity() == {"FINISHED"}
        assert [part.locked for part in first_session.parts] == [False, False]

        # A part selected for the next pending stage must be unlocked by the
        # GRAVITY click even if its independent Lock was left on it.
        for obj in bpy.context.selected_objects:
            obj.select_set(False)
        parts[2].select_set(True)
        bpy.context.view_layer.objects.active = parts[2]
        bpy.context.scene.yohsai.lock_selection = True
        assert bool(parts[2][mesh_loader.LOCKED_OBJECT_KEY])
        parts[2].location.y += 0.01
        bpy.context.view_layer.update()
        assert mesh_loader.part_gravity_state(parts[2]) == mesh_loader.GRAVITY_STATE_PLACED
        assert bool(parts[2][mesh_loader.LOCKED_OBJECT_KEY])
        assert bpy.ops.yohsai.kitsuke_zero_gravity() == {"FINISHED"}
        second_session = kitsuke._sessions[collection.as_pointer()]
        assert [part.obj for part in second_session.parts] == parts[:3]
        assert [part.locked for part in second_session.parts] == [False, False, False]
        second_plan = mesh_loader.build_sewing_plan(collection)
        assert second_plan.parts == tuple(parts[:3])
        assert second_plan.labels == ("A", "B")
        assert [bool(obj.get(mesh_loader.LOCKED_OBJECT_KEY, False)) for obj in parts] == [
            False, False, False, True
        ]
        assert [mesh_loader.part_gravity_state(obj) for obj in parts] == [
            mesh_loader.GRAVITY_STATE_DONE,
            mesh_loader.GRAVITY_STATE_DONE,
            mesh_loader.GRAVITY_STATE_DONE,
            mesh_loader.GRAVITY_STATE_PLACED,
        ]

        # Auto off unlocks every active completed part.  The untouched fourth
        # placed part remains locked and outside the simulation.
        bpy.context.scene.yohsai.auto_lock = False
        assert [bool(obj.get(mesh_loader.LOCKED_OBJECT_KEY, False)) for obj in parts] == [
            False, False, False, True
        ]
        assert mesh_loader.participating_parts(collection) == tuple(parts[:3])
        assert bpy.ops.yohsai.kitsuke_zero_gravity() == {"FINISHED"}
        assert [part.locked for part in second_session.parts] == [False, False, False]

        bpy.context.scene.yohsai.auto_lock = True
        assert all(bool(obj.get(mesh_loader.LOCKED_OBJECT_KEY, False)) for obj in parts)

        parts[3].location.y += 0.01
        bpy.context.view_layer.update()
        assert bpy.ops.yohsai.kitsuke_zero_gravity() == {"FINISHED"}
        final_session = kitsuke._sessions[collection.as_pointer()]
        assert [part.locked for part in final_session.parts] == [True, True, True, False]
        final_plan = mesh_loader.build_sewing_plan(collection)
        assert final_plan.parts == tuple(parts)
        assert final_plan.labels == ("A", "B", "C")
        assert [mesh_loader.part_gravity_state(obj) for obj in parts] == [
            mesh_loader.GRAVITY_STATE_DONE,
        ] * 4
        assert [bool(obj.get(mesh_loader.LOCKED_OBJECT_KEY, False)) for obj in parts] == [
            True, True, True, False
        ]

        # Emergency Lock remains independent from Auto.  With Auto off, only
        # the selected three parts stay fixed and the fourth remains deformable.
        bpy.context.scene.yohsai.auto_lock = False
        for obj in bpy.context.selected_objects:
            obj.select_set(False)
        for obj in parts[:3]:
            obj.select_set(True)
        bpy.context.view_layer.objects.active = parts[0]
        bpy.context.scene.yohsai.lock_selection = True
        assert [bool(obj.get(mesh_loader.LOCKED_OBJECT_KEY, False)) for obj in parts] == [
            True, True, True, False
        ]
        assert bpy.ops.yohsai.kitsuke_zero_gravity() == {"FINISHED"}
        assert [part.locked for part in kitsuke._sessions[collection.as_pointer()].parts] == [
            True, True, True, False
        ]

        finished = finished_garment.create_finished_garment(bpy.context, collection)
        assert finished.object.get("yohsai_role") == "finished_garment"
        assert finished.face_count == sum(len(part.data.polygons) for part in parts)
        assert finished.vertex_count < sum(len(part.data.vertices) for part in parts)
        assert finished.object.parent is None
        assert finished.object.matrix_world.is_identity

        print(
            "YOHSAI_GRAVITY_STATE_LOCK_OK stages=2,3,4 repeat=ok auto=on/off "
            f"lock=ok finished={finished.vertex_count}v/{finished.face_count}f"
        )
finally:
    yohsai.unregister()
