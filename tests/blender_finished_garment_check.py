"""Headless Blender check for the portable Finished Garment snapshot."""

from __future__ import annotations

from pathlib import Path
import sys

import bpy


REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY.parent))

import yohsai  # noqa: E402
from yohsai import kitsuke  # noqa: E402
from yohsai.finished_garment import (  # noqa: E402
    FinishedGarmentError,
    create_finished_garment,
)


def mesh_object(name, vertices, faces):
    mesh = bpy.data.meshes.new(f"{name}_MESH")
    mesh.from_pydata(vertices, [], faces)
    mesh.update(calc_edges=True)
    return bpy.data.objects.new(name, mesh)


def add_pattern_attribute(obj, values):
    attribute = obj.data.attributes.new(
        name="yohsai_pattern_position", type="FLOAT_VECTOR", domain="POINT"
    )
    for item, value in zip(attribute.data, values):
        item.vector = (*value, 0.0)


def add_uv(obj, values):
    layer = obj.data.uv_layers.new(name="Paint", do_init=False)
    modern = getattr(layer, "uv", None)
    for loop, value in zip(obj.data.loops, values):
        if modern is not None:
            modern[loop.index].vector = value
        else:
            layer.data[loop.index].uv = value


def add_color(obj, values):
    attribute = obj.data.color_attributes.new(
        name="Tint", type="FLOAT_COLOR", domain="CORNER"
    )
    for item, value in zip(attribute.data, values):
        item.color = value


scene = bpy.context.scene
source = bpy.data.collections.new("CLOTHES_FINISHED_TEST")
source["yohsai_role"] = "clothes"
scene.collection.children.link(source)

left = mesh_object(
    "FINISHED_LEFT",
    [(-1.0, 0.0, 0.0), (0.0, -0.01, 0.0), (0.0, 1.0, 0.0)],
    [(0, 1, 2)],
)
right = mesh_object(
    "FINISHED_RIGHT",
    [(0.0, 0.01, 0.0), (-1.0, 0.0, 0.0), (0.0, 1.02, 0.0)],
    [(0, 1, 2)],
)
right.scale.x = -1.0
for index, obj in enumerate((left, right)):
    source.objects.link(obj)
    obj["yohsai_role"] = "part"
    obj["yohsai_panel_index"] = index
    add_pattern_attribute(obj, ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0)))
    add_uv(obj, ((0.0, 0.0), (0.25 + index, 0.0), (0.25 + index, 1.0)))
    add_color(obj, ((1.0 - index, 0.0, float(index), 1.0),) * 3)
    obj.data.polygons[0].use_smooth = True
    normal_z = 1.0 if index == 0 else -1.0
    obj.data.normals_split_custom_set([(0.0, 0.0, normal_z)] * len(obj.data.loops))

red = bpy.data.materials.new("FINISHED_RED")
blue = bpy.data.materials.new("FINISHED_BLUE")
left.data.materials.append(red)
right.data.materials.append(None)
right.material_slots[0].link = "OBJECT"
right.material_slots[0].material = blue
left.data.edges[[set(edge.vertices) for edge in left.data.edges].index({1, 2})].use_edge_sharp = True

source[kitsuke._STATE_EPOCH_KEY] = kitsuke._RUNTIME_EPOCH
source[kitsuke._STATE_REVISION_KEY] = 3
source[kitsuke._STATE_PARTS_KEY] = [left.name, right.name]
source[kitsuke._STATE_SEAMS_KEY] = [1, 3, 2, 5]

source_vertices = {
    obj.name: [tuple(vertex.co) for vertex in obj.data.vertices] for obj in (left, right)
}
result = create_finished_garment(bpy.context, source)
garment = result.object

assert result.vertex_count == 4
assert result.face_count == 2
assert result.seam_count == 2
assert garment.matrix_world.is_identity
assert garment.parent is None
assert not garment.modifiers
assert not garment.vertex_groups
assert garment.data.shape_keys is None
assert garment.animation_data is None
assert [material.name for material in garment.data.materials] == [red.name, blue.name]
assert [polygon.material_index for polygon in garment.data.polygons] == [0, 1]
assert all(polygon.use_smooth for polygon in garment.data.polygons)
assert garment.data.uv_layers.get("Yohsai Pattern") is not None
assert garment.data.uv_layers.get("Paint") is not None
assert garment.data.color_attributes.get("Tint") is not None
assert garment.data.has_custom_normals
assert max(vertex.co.x for vertex in garment.data.vertices) == 1.0
assert all(normal.vector.z > 0.999 for normal in garment.data.corner_normals)
assert garment.data.attributes.get("panel_index") is not None
source_part = garment.data.attributes.get("yohsai_source_part")
assert source_part is not None and source_part.domain == "FACE"
assert [item.value for item in source_part.data] == [0, 1]

edge_faces = {edge.index: 0 for edge in garment.data.edges}
for polygon in garment.data.polygons:
    for edge_index in polygon.edge_keys:
        edge = next(edge for edge in garment.data.edges if tuple(sorted(edge.vertices)) == edge_index)
        edge_faces[edge.index] += 1
shared_edges = [garment.data.edges[index] for index, count in edge_faces.items() if count == 2]
assert len(shared_edges) == 1
assert shared_edges[0].use_edge_sharp
assert all(count in {1, 2} for count in edge_faces.values())

assert source_vertices == {
    obj.name: [tuple(vertex.co) for vertex in obj.data.vertices] for obj in (left, right)
}
first_vertices = [tuple(vertex.co) for vertex in garment.data.vertices]
second = create_finished_garment(bpy.context, source)
assert second.object.name.endswith("_002")
assert garment.name in bpy.data.objects
assert first_vertices == [tuple(vertex.co) for vertex in garment.data.vertices]

yohsai.register()
try:
    scene.yohsai.clothes_collection = source
    assert bpy.ops.yohsai.finished_garment() == {"FINISHED"}
    third = bpy.context.view_layer.objects.active
    assert third is not None and third.name.endswith("_003")
    assert third.get("yohsai_role") == "finished_garment"
finally:
    yohsai.unregister()

extra = mesh_object("UNFINISHED_EXTRA", [(0.0, 0.0, 0.0)] * 3, [(0, 1, 2)])
extra["yohsai_role"] = "part"
extra["yohsai_panel_index"] = 2
source.objects.link(extra)
source[kitsuke._STATE_EPOCH_KEY] = kitsuke._RUNTIME_EPOCH
output_count = sum(
    collection.get("yohsai_role") == "finished_garment_output"
    for collection in bpy.data.collections
)
active_before_failure = bpy.context.view_layer.objects.active
try:
    create_finished_garment(bpy.context, source)
except FinishedGarmentError as exc:
    assert "Not every clothes part" in str(exc)
else:
    raise AssertionError("An unfinished extra part must cancel Finished Garment.")
assert output_count == sum(
    collection.get("yohsai_role") == "finished_garment_output"
    for collection in bpy.data.collections
)
assert bpy.context.view_layer.objects.active == active_before_failure

# A multi-part junction may map both ends of one boundary edge to the same
# welded vertex.  Its incident triangle has no remaining output surface and is
# omitted, while the surviving topology is still validated normally.
junction = bpy.data.collections.new("CLOTHES_FINISHED_JUNCTION_TEST")
junction["yohsai_role"] = "clothes"
scene.collection.children.link(junction)
junction_left = mesh_object(
    "JUNCTION_LEFT",
    [(0.0, 0.0, 0.0), (0.0, 0.1, 0.0), (-0.1, 0.0, 0.0)],
    [(0, 1, 2)],
)
junction_right = mesh_object(
    "JUNCTION_RIGHT",
    [(0.0, 0.05, 0.0), (0.1, 0.0, 0.0), (0.1, 0.1, 0.0)],
    [(0, 1, 2)],
)
for index, obj in enumerate((junction_left, junction_right)):
    junction.objects.link(obj)
    obj["yohsai_role"] = "part"
    obj["yohsai_panel_index"] = index
    add_pattern_attribute(obj, ((0.0, 0.0), (0.0, 1.0), (1.0, 0.0)))
junction[kitsuke._STATE_EPOCH_KEY] = kitsuke._RUNTIME_EPOCH
junction[kitsuke._STATE_REVISION_KEY] = 1
junction[kitsuke._STATE_PARTS_KEY] = [junction_left.name, junction_right.name]
junction[kitsuke._STATE_SEAMS_KEY] = [0, 3, 1, 3]
junction_result = create_finished_garment(bpy.context, junction)
assert junction_result.face_count == 1
assert junction_result.removed_collapsed_face_count == 1
assert any("1 face(s) collapsed" in warning for warning in junction_result.warnings)

print(
    "FINISHED_GARMENT_OK",
    result.vertex_count,
    result.face_count,
    result.seam_count,
    round(result.maximum_weld_displacement_m * 1000.0, 4),
)
