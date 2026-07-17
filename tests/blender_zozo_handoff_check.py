"""Headless Blender check for the non-destructive ZOZO hand-off."""

from __future__ import annotations

from pathlib import Path
import sys

import bpy


REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY.parent))

from yohsai import kitsuke  # noqa: E402
from yohsai.zozo_handoff import (  # noqa: E402
    ZOZO_MCP_PORT,
    ZOZO_STITCH_OPENING_M,
    prepare_for_zozo,
)


def mesh_object(name, vertices, faces):
    mesh = bpy.data.meshes.new(f"{name}_MESH")
    mesh.from_pydata(vertices, [], faces)
    mesh.update(calc_edges=True)
    obj = bpy.data.objects.new(name, mesh)
    return obj


def add_pattern_attribute(obj):
    attribute = obj.data.attributes.new(
        name="yohsai_pattern_position", type="FLOAT_VECTOR", domain="POINT"
    )
    for item, vertex in zip(attribute.data, obj.data.vertices):
        item.vector = (vertex.co.x, vertex.co.y, 0.0)


scene = bpy.context.scene
source = bpy.data.collections.new("CLOTHES_TEST")
source["yohsai_role"] = "clothes"
scene.collection.children.link(source)

left = mesh_object(
    "LEFT",
    [(-0.5, 0.0, 1.1), (0.0, -0.2, 1.1), (0.0, 0.2, 1.1)],
    [(0, 1, 2)],
)
right = mesh_object(
    "RIGHT",
    [(0.0, -0.2, 1.1), (0.5, 0.0, 1.1), (0.0, 0.2, 1.1)],
    [(0, 1, 2)],
)
for index, obj in enumerate((left, right)):
    source.objects.link(obj)
    obj["yohsai_role"] = "part"
    obj["yohsai_panel_index"] = index
    add_pattern_attribute(obj)

body = mesh_object(
    "BODY",
    [
        (-1.0, -1.0, -1.0),
        (1.0, -1.0, -1.0),
        (1.0, 1.0, -1.0),
        (-1.0, 1.0, -1.0),
        (-1.0, -1.0, 1.0),
        (1.0, -1.0, 1.0),
        (1.0, 1.0, 1.0),
        (-1.0, 1.0, 1.0),
    ],
    [
        (0, 3, 2, 1),
        (4, 5, 6, 7),
        (0, 1, 5, 4),
        (1, 2, 6, 5),
        (2, 3, 7, 6),
        (3, 0, 4, 7),
    ],
)
scene.collection.objects.link(body)
body["_solver_uuid"] = "source-body-must-keep-this"

source[kitsuke._STATE_EPOCH_KEY] = kitsuke._RUNTIME_EPOCH
source[kitsuke._STATE_REVISION_KEY] = 1
source[kitsuke._STATE_PARTS_KEY] = [left.name, right.name]
source[kitsuke._STATE_SEAMS_KEY] = [1, 3, 2, 5]

source_before = [[tuple(vertex.co) for vertex in obj.data.vertices] for obj in (left, right)]
prepared = prepare_for_zozo(bpy.context, source, body)
cloth = prepared.cloth_object

assert len(cloth.data.vertices) == 6
assert len(cloth.data.polygons) == 2
assert cloth.data.uv_layers.active is not None
assert cloth.data.attributes.get("panel_index") is not None
assert cloth.data.attributes.get("yohsai_source_part") is not None
stitch = cloth.data.attributes.get("yohsai_zozo_stitch")
assert stitch is not None
assert sum(bool(item.value) for item in stitch.data) == 2
assert prepared.minimum_output_seam_distance_m >= ZOZO_STITCH_OPENING_M - 1.0e-8
assert prepared.body_object.get("_solver_uuid") is None
assert body.get("_solver_uuid") == "source-body-must-keep-this"
assert prepared.mcp_configuration(scene)["port"] == ZOZO_MCP_PORT
assert source_before == [
    [tuple(vertex.co) for vertex in obj.data.vertices] for obj in (left, right)
]

second = prepare_for_zozo(bpy.context, source, body)
owned = [
    obj
    for obj in second.collection.objects
    if obj.get("yohsai_role") in {"zozo_cloth", "zozo_body"}
]
assert len(owned) == 2
assert second.seam_count == 2

print(
    "ZOZO_HANDOFF_OK",
    second.seam_count,
    round(second.minimum_output_seam_distance_m * 1000.0, 4),
    second.cloth_object.data.uv_layers.active.name,
)
