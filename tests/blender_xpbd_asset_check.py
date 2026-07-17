"""Confirm the Blender 5.2 dynamics assets are scriptable as modifiers."""

from pathlib import Path

import bpy


ASSET_LIBRARY = Path(
    bpy.app.binary_path
).parent / bpy.app.version_string.split()[0] / "datafiles" / "assets" / "nodes" / "geometry_nodes_dynamics_assets.blend"
if not ASSET_LIBRARY.is_file():
    # Release builds use the major.minor data directory, not the full LTS label.
    ASSET_LIBRARY = Path(bpy.app.binary_path).parent / f"{bpy.app.version[0]}.{bpy.app.version[1]}" / "datafiles" / "assets" / "nodes" / "geometry_nodes_dynamics_assets.blend"

with bpy.data.libraries.load(str(ASSET_LIBRARY), link=False) as (source, target):
    required = {"Cloth Dynamics (Experimental)", "Collider"}
    assert required.issubset(source.node_groups)
    target.node_groups = sorted(required)

cloth_group = bpy.data.node_groups["Cloth Dynamics (Experimental)"]
collider_group = bpy.data.node_groups["Collider"]
assert cloth_group.is_modifier
assert collider_group.is_modifier

cloth_inputs = {
    item.name: item.identifier
    for item in cloth_group.interface.items_tree
    if getattr(item, "item_type", "") == "SOCKET" and item.in_out == "INPUT"
}
collider_inputs = {
    item.name: item.identifier
    for item in collider_group.interface.items_tree
    if getattr(item, "item_type", "") == "SOCKET" and item.in_out == "INPUT"
}
for required_input in (
    "Geometry",
    "Substeps",
    "Constraint Steps",
    "Collision Radius",
    "Effectors Collection",
):
    assert required_input in cloth_inputs
for required_input in ("Geometry", "Deforming", "Margin", "Friction"):
    assert required_input in collider_inputs

mesh = bpy.data.meshes.new("XPBD_TEST_MESH")
mesh.from_pydata(
    [(-0.5, -0.5, 1.0), (0.5, -0.5, 1.0), (0.5, 0.5, 1.0), (-0.5, 0.5, 1.0)],
    [],
    [(0, 1, 2, 3)],
)
cloth = bpy.data.objects.new("XPBD_TEST_CLOTH", mesh)
bpy.context.scene.collection.objects.link(cloth)
modifier = cloth.modifiers.new("Blender XPBD Cloth", "NODES")
modifier.node_group = cloth_group
getattr(modifier.properties.inputs, cloth_inputs["Substeps"]).value = 5
getattr(modifier.properties.inputs, cloth_inputs["Constraint Steps"]).value = 15
getattr(modifier.properties.inputs, cloth_inputs["Collision Radius"]).value = 0.001

body_mesh = bpy.data.meshes.new("XPBD_TEST_BODY_MESH")
body_mesh.from_pydata(
    [(-1.0, -1.0, 0.0), (1.0, -1.0, 0.0), (1.0, 1.0, 0.0), (-1.0, 1.0, 0.0)],
    [],
    [(0, 1, 2, 3)],
)
body = bpy.data.objects.new("XPBD_TEST_BODY", body_mesh)
effectors = bpy.data.collections.new("XPBD_TEST_EFFECTORS")
bpy.context.scene.collection.children.link(effectors)
effectors.objects.link(body)
collider = body.modifiers.new("Blender XPBD Collider", "NODES")
collider.node_group = collider_group
getattr(collider.properties.inputs, collider_inputs["Deforming"]).value = False
getattr(collider.properties.inputs, collider_inputs["Margin"]).value = 0.001
getattr(modifier.properties.inputs, cloth_inputs["Effectors Collection"]).value = effectors

assert modifier.node_group.name == "Cloth Dynamics (Experimental)"
assert collider.node_group.name == "Collider"
assert getattr(
    modifier.properties.inputs, cloth_inputs["Effectors Collection"]
).value == effectors
bpy.context.scene.frame_set(1)
bpy.context.view_layer.update()
evaluated = cloth.evaluated_get(bpy.context.evaluated_depsgraph_get())
evaluated_mesh = evaluated.to_mesh()
try:
    assert len(evaluated_mesh.vertices) == 4
finally:
    evaluated.to_mesh_clear()
print("XPBD_ASSET_OK", bpy.app.version_string, ASSET_LIBRARY)
