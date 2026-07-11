"""Blender-background integration check for Yohsai mesh loading."""

from __future__ import annotations

import copy
import os
import sys
from pathlib import Path


import bpy  # noqa: E402
from mathutils import Vector  # noqa: E402

installed_check = os.environ.get("YOHSAI_INSTALLED_CHECK") == "1"
if installed_check:
    from bl_ext.user_default import yohsai  # noqa: E402
    from bl_ext.user_default.yohsai import kitsuke as kitsuke_module, ui  # noqa: E402
    from bl_ext.user_default.yohsai.mesh_loader import MeshLoadError  # noqa: E402
else:
    repo_parent = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(repo_parent))
    import yohsai  # noqa: E402
    import yohsai.kitsuke as kitsuke_module  # noqa: E402
    import yohsai.ui as ui  # noqa: E402
    from yohsai.mesh_loader import MeshLoadError  # noqa: E402


svg_path = Path.home() / "Desktop" / "test2.svg"
if not svg_path.is_file():
    raise RuntimeError(f"Missing integration input: {svg_path}")

if not installed_check:
    yohsai.register()
try:
    bpy.context.scene.yohsai.svg_path = str(svg_path)
    result = bpy.ops.yohsai.load_svg()
    assert result == {"FINISHED"}, result
    assert ui._parse_process is not None
    ui._parse_process.wait(timeout=30)
    ui._poll_svg_parser()
    status = bpy.context.scene.yohsai.parse_status
    assert status.startswith("Loaded CLOTHES_001: 2 part(s)"), status

    collection = bpy.data.collections["CLOTHES_001"]
    assert bpy.context.scene.yohsai.clothes_collection == collection
    assert len(collection.objects) == 2
    parts = sorted(collection.objects, key=lambda item: item.name)
    assert [obj.name for obj in parts] == ["CLOTHES_001_PART_001", "CLOTHES_001_PART_002"]
    assert all(obj.type == "MESH" and obj.get("yohsai_role") == "part" for obj in parts)
    assert all(len(obj.modifiers) == 0 for obj in parts)
    assert sum(len(obj.data.vertices) for obj in parts) > 100
    assert sum(len(obj.data.polygons) for obj in parts) > 100
    world_vertices = [obj.matrix_world @ vertex.co for obj in parts for vertex in obj.data.vertices]
    assert all(abs(vertex.y + 1.0) < 1.0e-7 for vertex in world_vertices)
    assert abs(min(vertex.z for vertex in world_vertices) - 0.01) < 1.0e-6
    min_x = min(vertex.x for vertex in world_vertices)
    max_x = max(vertex.x for vertex in world_vertices)
    assert abs(min_x + max_x) < 1.0e-6
    assert all(polygon.normal.y < -0.999 for obj in parts for polygon in obj.data.polygons)

    for panel_index, obj in enumerate(parts):
        mesh = obj.data
        attribute_names = set(mesh.attributes.keys())
        assert {"sewing_A", "sewing_B", "fold", "panel_index"} <= attribute_names
        assert sum(item.value for item in mesh.attributes["sewing_A"].data) > 0
        assert sum(item.value for item in mesh.attributes["sewing_B"].data) > 0
        assert sum(item.value for item in mesh.attributes["fold"].data) > 0
        assert set(item.value for item in mesh.attributes["panel_index"].data) == {panel_index}

    bounds = []
    for obj in parts:
        xs = [(obj.matrix_world @ vertex.co).x for vertex in obj.data.vertices]
        bounds.append((min(xs), max(xs)))
    bounds.sort()
    assert bounds[1][0] - bounds[0][1] >= 0.099

    # Sewing uses the parts' current world transforms, keeps them as hidden
    # sources, and creates loose spring edges in one combined simulation mesh.
    parts[0].location.y += 0.03
    sewing_result = bpy.ops.yohsai.sewing()
    assert sewing_result == {"FINISHED"}, bpy.context.scene.yohsai.parse_status
    sewn = bpy.data.objects["CLOTHES_001_SEWN"]
    sewn_mesh = sewn.data
    assert len(collection.objects) == 3
    assert sewn.get("yohsai_role") == "sewn"
    assert len(sewn.modifiers) == 0
    assert all(obj.hide_get() and obj.hide_render for obj in parts)
    assert len(sewn_mesh.polygons) == sum(len(obj.data.polygons) for obj in parts)
    spring_attributes = [sewn_mesh.attributes[name] for name in ("sewing_spring_A", "sewing_spring_B")]
    assert all(sum(item.value for item in attribute.data) > 0 for attribute in spring_attributes)
    spring_edge_indices = {
        index
        for attribute in spring_attributes
        for index, item in enumerate(attribute.data)
        if item.value
    }
    assert spring_edge_indices
    spring_pairs = [tuple(sewn_mesh.edges[index].vertices) for index in spring_edge_indices]
    initial_sewn_positions = [vertex.co.copy() for vertex in sewn_mesh.vertices]
    initial_seam_distance = sum(
        (initial_sewn_positions[a] - initial_sewn_positions[b]).length
        for a, b in spring_pairs
    ) / len(spring_pairs)
    assert all(sewn_mesh.edges[index].is_loose for index in spring_edge_indices)
    assert abs(min(vertex.co.y for vertex in sewn_mesh.vertices) + 1.0) < 1.0e-6
    assert abs(max(vertex.co.y for vertex in sewn_mesh.vertices) + 0.97) < 1.0e-6
    sewn_vertex_count = len(sewn_mesh.vertices)
    sewn_face_count = len(sewn_mesh.polygons)

    try:
        duplicate_sewing = bpy.ops.yohsai.sewing()
    except RuntimeError as exc:
        assert "already has a sewn mesh" in str(exc)
    else:
        assert duplicate_sewing == {"CANCELLED"}
    assert "already has a sewn mesh" in bpy.context.scene.yohsai.parse_status

    # Kitsuke uses the verified sewn preview only as transient connectivity,
    # advances a fixed interval, then restores the separate editable parts.
    bpy.ops.mesh.primitive_cube_add(location=(0.0, -1.0, 0.1), scale=(0.1, 0.1, 0.1))
    body = bpy.context.object
    body.name = "KITSUKE_TEST_BODY"
    bpy.context.scene.yohsai.body_object = body
    body_snapshot = kitsuke_module._body_snapshot(bpy.context, body)
    assert kitsuke_module._inside_body(body_snapshot, (0.0, -1.0, 0.1))
    assert not kitsuke_module._inside_body(body_snapshot, (10.0, 10.0, 10.0))
    before = [
        tuple(obj.matrix_world @ vertex.co)
        for obj in parts
        for vertex in obj.data.vertices
    ]
    kitsuke_result = bpy.ops.yohsai.kitsuke()
    assert kitsuke_result == {"FINISHED"}, bpy.context.scene.yohsai.parse_status
    assert not any(obj.get("yohsai_role") == "sewn" for obj in collection.objects)
    assert all(not obj.hide_get() and not obj.hide_render for obj in parts)
    after = [
        tuple(obj.matrix_world @ vertex.co)
        for obj in parts
        for vertex in obj.data.vertices
    ]
    assert any((Vector(a) - Vector(b)).length > 1.0e-7 for a, b in zip(before, after))
    assert all(all(value == value and abs(value) < 1.0e6 for value in point) for point in after)

    # Object-mode placement between clicks is accepted and the simulation can
    # rebuild its transient sewing constraints without a persistent sewn mesh.
    parts[0].location.x += 0.01
    parts[0].rotation_euler.z += 0.02
    second_kitsuke = bpy.ops.yohsai.kitsuke()
    assert second_kitsuke == {"FINISHED"}, bpy.context.scene.yohsai.parse_status
    bpy.context.scene.yohsai.kitsuke_gravity = 0.5
    bpy.context.scene.yohsai.kitsuke_seam_pull_mm = 5.0
    tuned_kitsuke = bpy.ops.yohsai.kitsuke()
    assert tuned_kitsuke == {"FINISHED"}, bpy.context.scene.yohsai.parse_status
    assert "gravity 0.5" in bpy.context.scene.yohsai.parse_status
    assert "seam 5 mm" in bpy.context.scene.yohsai.parse_status
    bpy.context.scene.yohsai.kitsuke_gravity = kitsuke_module.DEFAULT_GRAVITY_M_PER_SECOND_SQUARED
    bpy.context.scene.yohsai.kitsuke_seam_pull_mm = kitsuke_module.DEFAULT_SEAM_CLOSURE_PER_CLICK_M * 1000.0
    for _step in range(8):
        repeated_kitsuke = bpy.ops.yohsai.kitsuke()
        assert repeated_kitsuke == {"FINISHED"}, bpy.context.scene.yohsai.parse_status
    repeated_positions = [
        tuple(obj.matrix_world @ vertex.co)
        for obj in parts
        for vertex in obj.data.vertices
    ]
    assert all(
        all(value == value and abs(value) < 1.0e6 for value in point)
        for point in repeated_positions
    )
    repeated_seam_distance = sum(
        (Vector(repeated_positions[a]) - Vector(repeated_positions[b])).length
        for a, b in spring_pairs
    ) / len(spring_pairs)
    assert repeated_seam_distance < initial_seam_distance

    invalid = copy.deepcopy(ui._loaded_pattern_json)
    invalid["panels"][0]["segments"][1]["fold"] = True
    try:
        ui.create_clothes_mesh(bpy.context, invalid)
    except MeshLoadError as exc:
        assert "more than one fold" in str(exc)
    else:
        raise AssertionError("A panel with multiple fold segments was accepted")

    # A second first-time import avoids name collisions instead of overwriting.
    collection2 = ui.create_clothes_mesh(bpy.context, ui._loaded_pattern_json)
    assert collection2.name == "CLOTHES_002"
    assert len(collection2.objects) == 2
    print(
        "YOHSAI_SEWING_OK",
        f"parts={len(parts)}",
        f"verts={sewn_vertex_count}",
        f"faces={sewn_face_count}",
        f"springs={len(spring_edge_indices)}",
    )
finally:
    if not installed_check:
        yohsai.unregister()
