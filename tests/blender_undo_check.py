"""Blender-background regression check for Kitsuke Undo/Redo state."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import bpy
import numpy as np


installed_check = os.environ.get("YOHSAI_INSTALLED_CHECK") == "1"
if installed_check:
    from bl_ext.user_default import yohsai
    from bl_ext.user_default.yohsai import kitsuke, yohsai_svg_parser
    from bl_ext.user_default.yohsai.mesh_loader import create_clothes_mesh
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    import yohsai
    from yohsai import kitsuke, yohsai_svg_parser
    from yohsai.mesh_loader import create_clothes_mesh


source = Path.home() / "Desktop" / "test2.pdf"
if not source.is_file():
    raise RuntimeError(f"Missing Undo integration input: {source}")


def persisted_state(collection, seam_count):
    ranges = []
    offset = 0
    for obj in kitsuke._parts(collection):
        ranges.append(kitsuke._PartRange(obj, offset, len(obj.data.vertices)))
        offset += len(obj.data.vertices)
    return kitsuke._read_persisted_state(collection, ranges, seam_count)


if not installed_check:
    yohsai.register()
try:
    bpy.context.preferences.edit.use_global_undo = True
    document = yohsai_svg_parser.parse_pdf(source)
    collection = create_clothes_mesh(bpy.context, document)
    collection_name = collection.name
    bpy.context.scene.yohsai.clothes_collection = collection

    bpy.ops.mesh.primitive_cube_add(size=1.0, location=(100.0, 100.0, 100.0))
    body = bpy.context.object
    bpy.context.scene.yohsai.body_object = body

    bpy.ops.ed.undo_push(message="Yohsai Undo test initial state")
    assert bpy.ops.yohsai.sewing() == {"FINISHED"}
    bpy.ops.ed.undo_push(message="Yohsai Undo test Sewing")

    assert bpy.ops.yohsai.kitsuke() == {"FINISHED"}
    session = kitsuke._sessions[collection.as_pointer()]
    seam_one = session.runtime.seam_state().copy()
    velocity_one = session.velocities.copy()
    revision_one = session.revision
    bpy.ops.ed.undo_push(message="Yohsai Undo test click 1")

    assert bpy.ops.yohsai.kitsuke() == {"FINISHED"}
    session = kitsuke._sessions[collection.as_pointer()]
    seam_two = session.runtime.seam_state().copy()
    velocity_two = session.velocities.copy()
    revision_two = session.revision
    bpy.ops.ed.undo_push(message="Yohsai Undo test click 2")

    assert bpy.ops.ed.undo() == {"FINISHED"}
    assert not kitsuke._sessions
    collection = bpy.data.collections[collection_name]
    restored = persisted_state(collection, len(seam_one))
    assert restored is not None
    revision, seam_rest, velocities = restored
    assert revision == revision_one
    assert np.array_equal(seam_rest, seam_one)
    assert np.array_equal(velocities, velocity_one)

    assert bpy.ops.ed.redo() == {"FINISHED"}
    assert not kitsuke._sessions
    collection = bpy.data.collections[collection_name]
    restored = persisted_state(collection, len(seam_two))
    assert restored is not None
    revision, seam_rest, velocities = restored
    assert revision == revision_two
    assert np.array_equal(seam_rest, seam_two)
    assert np.array_equal(velocities, velocity_two)

    # Repeating click 2 after Undo must continue from click 1, not from the
    # stale in-memory click-2 target that originally exposed the bug.
    assert bpy.ops.ed.undo() == {"FINISHED"}
    assert not kitsuke._sessions
    assert bpy.ops.yohsai.kitsuke() == {"FINISHED"}
    collection = bpy.data.collections[collection_name]
    session = kitsuke._sessions[collection.as_pointer()]
    assert session.revision == revision_two
    assert np.array_equal(session.runtime.seam_state(), seam_two)
    print(f"YOHSAI_UNDO_REDO_OK revisions={revision_one},{revision_two} seams={len(seam_two)}")
finally:
    if not installed_check:
        yohsai.unregister()
