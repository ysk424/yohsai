"""Blender-background integration check for Load, Sewing, Update, and Kitsuke."""

from __future__ import annotations

import copy
import importlib
import os
import sys
import tempfile
from pathlib import Path


import bpy  # noqa: E402
import numpy as np  # noqa: E402
from mathutils import Vector  # noqa: E402

installed_check = os.environ.get("YOHSAI_INSTALLED_CHECK") == "1"
if installed_check:
    from bl_ext.user_default import yohsai  # noqa: E402
    from bl_ext.user_default.yohsai import (  # noqa: E402
        kitsuke as kitsuke_module,
        mesh_loader,
        ui,
        yohsai_svg_parser,
    )
    from bl_ext.user_default.yohsai.mesh_loader import MeshLoadError  # noqa: E402
else:
    repo = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo / "tests"))
    from source_package import load_source_package  # noqa: E402

    yohsai = load_source_package(repo)
    kitsuke_module = sys.modules[f"{yohsai.__name__}.kitsuke"]
    mesh_loader = sys.modules[f"{yohsai.__name__}.mesh_loader"]
    ui = sys.modules[f"{yohsai.__name__}.ui"]
    yohsai_svg_parser = importlib.import_module(f"{yohsai.__name__}.yohsai_svg_parser")
    MeshLoadError = mesh_loader.MeshLoadError


svg_path = Path.home() / "Desktop" / "test2.pdf"
if not svg_path.is_file():
    raise RuntimeError("Missing integration input: Desktop/test2.pdf")

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
    if svg_path.suffix.lower() == ".pdf":
        labels = [obj["yohsai_panel_label"] for obj in parts]
        assert labels[0].startswith("OMOTE") and labels[1].startswith("URA"), labels
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
        assert {
            "sewing_A",
            "sewing_B",
            "fold",
            "panel_index",
            mesh_loader.GRAINLINE_EDGE_FAMILY_ATTRIBUTE,
            mesh_loader.GRAINLINE_FACE_QUAD_ATTRIBUTE,
        } <= attribute_names
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
# sources, and creates loose spring edges in one combined verification mesh.
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
    session = kitsuke_module._sessions[collection.as_pointer()]
    assert session.runtime.seam_count == len(spring_pairs) > 0
    assert not hasattr(session, "edge_rest")
    assert len(session.topology.edges) > 0
    assert len(session.topology.quads) > 0
    assert len(session.topology.bends) > 0
    np.testing.assert_array_equal(session.runtime.seam_state(), 0.0)
    material_lengths = np.linalg.norm(
        session.positions[session.topology.edges[:, 1]]
        - session.positions[session.topology.edges[:, 0]],
        axis=1,
    )
    material_ratios = material_lengths / session.topology.edge_rest_lengths
    material_p99 = float(np.quantile(material_ratios, 0.99))
    material_max = float(material_ratios.max())
    material_excess_max = float(
        np.max(np.maximum(0.0, material_lengths - session.topology.edge_rest_lengths))
    )
    worst_edge = int(np.argmax(material_ratios))
    worst_vertices = session.topology.edges[worst_edge]
    seam_vertices = set(int(value) for value in session.seams.ravel())
    seam_edge_mask = np.asarray(
        [
            int(edge[0]) in seam_vertices or int(edge[1]) in seam_vertices
            for edge in session.topology.edges
        ],
        dtype=bool,
    )
    seam_edge_excess_max = float(
        np.max(
            np.maximum(
                0.0,
                material_lengths[seam_edge_mask]
                - session.topology.edge_rest_lengths[seam_edge_mask],
            )
        )
    )
    worst_detail = (
        material_p99,
        material_max,
        float(session.topology.edge_rest_lengths[worst_edge]),
        float(material_lengths[worst_edge]),
        material_excess_max,
        seam_edge_excess_max,
        tuple(int(value) for value in worst_vertices),
        tuple(int(value) in seam_vertices for value in worst_vertices),
    )
    assert material_p99 < 1.5, worst_detail
    assert material_excess_max < 0.01, worst_detail
    assert seam_edge_excess_max < 0.005, worst_detail
    assert not any(obj.get("yohsai_role") == "sewn" for obj in collection.objects)
    assert all(not obj.hide_get() and not obj.hide_render for obj in parts)
    after = [
        tuple(obj.matrix_world @ vertex.co)
        for obj in parts
        for vertex in obj.data.vertices
    ]
    assert any((Vector(a) - Vector(b)).length > 1.0e-7 for a, b in zip(before, after))
    assert all(all(value == value and abs(value) < 1.0e6 for value in point) for point in after)

# Object-mode placement between clicks is accepted while the live simulation
# retains exact sewing pairs without a persistent combined preview mesh.
    assert abs(bpy.context.scene.yohsai.kitsuke_gravity - 1.0) < 1.0e-7
    parts[0].location.x += 0.01
    parts[0].rotation_euler.z += 0.02
    bpy.context.scene.yohsai.kitsuke_gravity = 10.0
    second_kitsuke = bpy.ops.yohsai.kitsuke()
    assert second_kitsuke == {"FINISHED"}, bpy.context.scene.yohsai.parse_status
    assert "gravity -Z 10" in bpy.context.scene.yohsai.parse_status
    assert not hasattr(bpy.context.scene.yohsai, "kitsuke_seam_pull_mm")
    bpy.context.scene.yohsai.kitsuke_gravity = 0.0
    assert bpy.ops.yohsai.kitsuke() == {"FINISHED"}
    assert "gravity -Z 0" in bpy.context.scene.yohsai.parse_status
    bpy.context.scene.yohsai.kitsuke_gravity = 10.0
    for _step in range(7):
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

    # The product UI is PDF-only.  This retained low-level SVG fixture exercises
    # mesh recutting directly, without pretending SVG is still a supported UI
    # input format.
    update_svg = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 220">
  <g id="CLOTHES">
    <path id="front_source" d="M 100 60 L 140 60 L 140 180 L 100 180 Z"/>
    <path id="back_source" d="M 160 60 L 200 60 L 200 180 L 160 180 Z"/>
    <text x="120" y="120"># Front 01</text>
    <text x="180" y="120">#back-01</text>
    <text x="141" y="120">A</text>
    <text x="159" y="120">A</text>
    <text x="50" y="15">@S100cm</text>
    <path d="M 0 20 L 100 20"/>
  </g>
</svg>
"""
    with tempfile.TemporaryDirectory() as update_directory:
        update_path = Path(update_directory) / "update_pattern.svg"
        update_path.write_text(update_svg, encoding="utf-8")
        update_document = yohsai_svg_parser._parse_legacy_svg(update_path)
        update_collection = mesh_loader.create_clothes_mesh(bpy.context, update_document)
        bpy.context.scene.yohsai.clothes_collection = update_collection
        assert update_collection.name == "CLOTHES_003"
        update_parts = sorted(
            (obj for obj in update_collection.objects if obj.get("yohsai_role") == "part"),
            key=lambda obj: int(obj["yohsai_panel_index"]),
        )
        assert [obj["yohsai_panel_label"] for obj in update_parts] == ["FRONT01", "BACK-01"]
        assert all("yohsai_pattern_position" in obj.data.attributes for obj in update_parts)
        assert bpy.ops.yohsai.sewing() == {"FINISHED"}
        assert bool(update_collection["yohsai_sewing_verified"])
        bpy.context.scene.yohsai.kitsuke_gravity = 0.0
        assert bpy.ops.yohsai.kitsuke() == {"FINISHED"}
        assert "gravity -Z 0" in bpy.context.scene.yohsai.parse_status
        object_pointers = [obj.as_pointer() for obj in update_parts]
        old_vertex_counts = [len(obj.data.vertices) for obj in update_parts]
        old_matrices = [obj.matrix_world.copy() for obj in update_parts]

        larger_svg = update_svg.replace("M 100 60", "M 95 55").replace(
            "L 100 180 Z", "L 95 185 Z"
        ).replace("L 200 60", "L 205 55").replace("L 200 180", "L 205 185")
        update_path.write_text(larger_svg, encoding="utf-8")
        larger_document = yohsai_svg_parser._parse_legacy_svg(update_path)
        sewing_changed, _vertex_count = mesh_loader.update_clothes_mesh(
            bpy.context, update_collection, larger_document
        )
        kitsuke_module.clear_kitsuke_session(update_collection)
        assert not sewing_changed
        assert [obj.as_pointer() for obj in update_parts] == object_pointers
        assert [obj.matrix_world for obj in update_parts] == old_matrices
        assert [len(obj.data.vertices) for obj in update_parts] != old_vertex_counts
        assert bool(update_collection["yohsai_sewing_verified"])
        assert not any(obj.get("yohsai_role") == "sewn" for obj in update_collection.objects)
        bpy.context.scene.yohsai.kitsuke_gravity = 10.0
        assert bpy.ops.yohsai.kitsuke() == {"FINISHED"}
        assert "gravity -Z 10" in bpy.context.scene.yohsai.parse_status

        changed_sewing_svg = larger_svg.replace(">A</text>", ">B</text>")
        update_path.write_text(changed_sewing_svg, encoding="utf-8")
        changed_document = yohsai_svg_parser._parse_legacy_svg(update_path)
        sewing_changed, _vertex_count = mesh_loader.update_clothes_mesh(
            bpy.context, update_collection, changed_document
        )
        kitsuke_module.clear_kitsuke_session(update_collection)
        assert sewing_changed
        assert not bool(update_collection["yohsai_sewing_verified"])
        try:
            rejected_kitsuke = bpy.ops.yohsai.kitsuke()
        except RuntimeError as exc:
            assert "Sewing required" in str(exc)
        else:
            assert rejected_kitsuke == {"CANCELLED"}
        assert "Sewing required" in bpy.context.scene.yohsai.parse_status
        assert bpy.ops.yohsai.sewing() == {"FINISHED"}
        assert bpy.ops.yohsai.kitsuke() == {"FINISHED"}

        mesh_pointers = [obj.data.as_pointer() for obj in update_parts]
        wrong_labels_svg = changed_sewing_svg.replace("#back-01", "#back-02")
        update_path.write_text(wrong_labels_svg, encoding="utf-8")
        wrong_labels_document = yohsai_svg_parser._parse_legacy_svg(update_path)
        try:
            mesh_loader.update_clothes_mesh(bpy.context, update_collection, wrong_labels_document)
        except mesh_loader.UpdateError as exc:
            assert "Panel labels changed" in str(exc)
        else:
            raise AssertionError("An update with changed panel labels was accepted")
        assert [obj.data.as_pointer() for obj in update_parts] == mesh_pointers

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
