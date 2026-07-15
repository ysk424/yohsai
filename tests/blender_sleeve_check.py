"""Blender-background integration check for mirrored RING sleeve construction."""

from __future__ import annotations

import os
import importlib
import sys
from pathlib import Path

import bpy
import numpy as np


installed_check = os.environ.get("YOHSAI_INSTALLED_CHECK") == "1"
if installed_check:
    from bl_ext.user_default.yohsai import kitsuke, mesh_loader, ui, yohsai_svg_parser  # noqa: E402
    for wheel in sorted((Path(mesh_loader.__file__).parent / "wheels").glob("*.whl")):
        sys.path.insert(0, str(wheel))
else:
    repo = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo / "tests"))
    for wheel in sorted((repo / "wheels").glob("*.whl")):
        sys.path.insert(0, str(wheel))
    from source_package import load_source_package  # noqa: E402

    package = load_source_package(repo)
    kitsuke = sys.modules[f"{package.__name__}.kitsuke"]
    mesh_loader = sys.modules[f"{package.__name__}.mesh_loader"]
    yohsai_svg_parser = importlib.import_module(f"{package.__name__}.yohsai_svg_parser")


source = Path.home() / "Desktop" / "test3.pdf"
if not source.is_file():
    raise RuntimeError("Missing integration input: Desktop/test3.pdf")

if installed_check:
    bpy.context.scene.yohsai.svg_path = str(source)
    assert bpy.ops.yohsai.load_svg() == {"FINISHED"}
    ui._parse_process.wait(timeout=30)
    ui._poll_svg_parser()
    assert bpy.context.scene.yohsai.parse_status.startswith("Loaded CLOTHES_001: 4 part(s)")
    document = ui._loaded_pattern_json
    collection = bpy.context.scene.yohsai.clothes_collection
else:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    document = yohsai_svg_parser.parse_pdf(source)
    collection = mesh_loader.create_clothes_mesh(bpy.context, document)
parts = sorted(
    (obj for obj in collection.objects if obj.get("yohsai_role") == "part"),
    key=lambda obj: int(obj["yohsai_panel_index"]),
)
assert len(parts) == 4
assert [obj["yohsai_panel_label"] for obj in parts] == ["OMOTE", "URA", "SODE", "SODE"]
assert [obj["yohsai_mirror_side"] for obj in parts] == ["", "", "LEFT", "RIGHT"]

sleeves = [obj for obj in parts if bool(obj["yohsai_ring_closed"])]
body = [obj for obj in parts if not bool(obj["yohsai_ring_closed"])]
assert len(sleeves) == 2 and len(body) == 2
for sleeve in sleeves:
    mesh = sleeve.data
    assert "sewing_C" in mesh.attributes
    assert "sewing_RING" not in mesh.attributes
    assert "yohsai_pattern_edge_rest" in mesh.attributes
    assert mesh_loader.GRAINLINE_EDGE_FAMILY_ATTRIBUTE in mesh.attributes
    assert mesh_loader.GRAINLINE_FACE_QUAD_ATTRIBUTE in mesh.attributes
    family = mesh.attributes[mesh_loader.GRAINLINE_EDGE_FAMILY_ATTRIBUTE]
    quad = mesh.attributes[mesh_loader.GRAINLINE_FACE_QUAD_ATTRIBUTE]
    quad_ids = {int(item.value) for item in quad.data if int(item.value) >= 0}
    proxy_count = sum(
        int(item.value) == mesh_loader.GRAINLINE_EDGE_PROXY for item in family.data
    )
    assert quad_ids and proxy_count == len(quad_ids)
    points = [sleeve.matrix_world @ vertex.co for vertex in mesh.vertices]
    assert max(point.y for point in points) - min(point.y for point in points) > 0.05
    assert max(point.z for point in points) - min(point.z for point in points) > 0.05
    assert len(mesh.vertices) - len(mesh.edges) + len(mesh.polygons) == 0

    adjacency: dict[int, set[int]] = {}
    marker = mesh.attributes["sewing_C"]
    for edge in mesh.edges:
        if not marker.data[edge.index].value:
            continue
        a, b = edge.vertices
        adjacency.setdefault(a, set()).add(b)
        adjacency.setdefault(b, set()).add(a)
    assert adjacency and all(len(neighbors) == 2 for neighbors in adjacency.values())


def seam_length(obj, label):
    mesh = obj.data
    marker = mesh.attributes[f"sewing_{label}"]
    rest = mesh.attributes["yohsai_pattern_edge_rest"]
    return sum(rest.data[edge.index].value for edge in mesh.edges if marker.data[edge.index].value)


sleeve_c = [seam_length(obj, "C") for obj in sleeves]
body_c_per_side = sum(seam_length(obj, "C") for obj in body) / 2.0
assert all(length > body_c_per_side for length in sleeve_c), (sleeve_c, body_c_per_side)

object_pointers = [obj.as_pointer() for obj in parts]
sewing_changed, vertex_count = mesh_loader.update_clothes_mesh(bpy.context, collection, document)
assert not sewing_changed
assert vertex_count == sum(len(obj.data.vertices) for obj in parts)
assert [obj.as_pointer() for obj in parts] == object_pointers
assert [obj["yohsai_panel_instance"] for obj in parts] == ["OMOTE", "URA", "SODE:LEFT", "SODE:RIGHT"]

plan = mesh_loader.build_sewing_plan(collection)
assert plan.labels == ("A", "B", "C")
c_connections = [(a, b) for label, a, b in plan.connections if label == "C"]
assert c_connections
offsets = {}
offset = 0
for obj in plan.parts:
    offsets[obj] = range(offset, offset + len(obj.data.vertices))
    offset += len(obj.data.vertices)
sleeve_ranges = [offsets[obj] for obj in sleeves]
assert all(
    any(a in sleeve_range or b in sleeve_range for sleeve_range in sleeve_ranges)
    for a, b in c_connections
)

# RING construction coordinates must remain finite under the same Body-independent
# lattice model.  Constant seam attraction is active even with gravity disabled.
preview = mesh_loader.create_sewn_mesh(bpy.context, collection)
bpy.ops.mesh.primitive_cube_add(size=1.0, location=(100.0, 100.0, 100.0))
fixed_body = bpy.context.object
session = kitsuke._KitsukeSession(
    bpy.context,
    collection,
    fixed_body,
    preview,
    kitsuke.KITSUKE_BACKEND_STABLE_COSSERAT,
)
ring_start = session.positions.copy()
try:
    session.advance(bpy.context, 0.0, 4)
    assert np.all(np.isfinite(session.positions))
    ring_drift = float(np.linalg.norm(session.positions - ring_start, axis=1).max())
    assert ring_drift < 0.25, ring_drift
finally:
    session.runtime.close()

print(
    "YOHSAI_SLEEVE_OK "
    f"parts={len(parts)} sleeve_C={sleeve_c[0]:.6f} body_C={body_c_per_side:.6f} "
    f"connections={len(c_connections)} cosserat_drift={ring_drift:.6g}"
)
