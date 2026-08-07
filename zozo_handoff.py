# SPDX-License-Identifier: GPL-3.0-or-later
"""Hand the garment as it stands to ZOZO, and configure ZOZO to receive it.

This used to be the button that sewed.  ZOZO's add-on pulls a seam shut with
a loose stitch edge, and a loose stitch edge needs a positive contact gap, so
the hand-off pushed every seam apart into layers first -- a graph colouring
over each seam component, a spacing per layer, and a weld for the pinch
points the layering could not open.  All of that was scaffolding for handing
over cloth that was not sewn yet.

Zero GRAVITY sews before this button is ever pressed, so there is nothing
left to open.  What goes over now is the garment exactly as it stands: the
panels' current world positions, their seams as stitch edges, their pattern
coordinates as UVs, and a copy of the Body.  Moving good cloth on the way out
could only make it worse.

Yohsai's own objects are never touched -- ZOZO gets copies, in a collection
of its own.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

import bpy
import numpy as np

from .kitsuke import KitsukeError, _seam_constraints_from_parts, part_ranges
from .mesh_loader import (
    MESH_SPACING_M,
    UpdateError,
    participating_parts,
    part_spacing_m,
    remesh_with_seam_counts,
)
from .shell_isect_bridge import ShellIsectReport, run_check_and_fix


ZOZO_MCP_PORT = 9633
ZOZO_CONTACT_GAP_M = 0.001
_HANDOFF_COLLECTION_ROLE = "zozo_handoff"
_HANDOFF_CLOTH_ROLE = "zozo_cloth"
_HANDOFF_BODY_ROLE = "zozo_body"

# How small a triangle's rest area may be, as a fraction of one lattice cell.
#
# The solver builds a shell element's Hessian from the inverse of its rest
# area, so a small enough triangle contributes a term large enough to take the
# assembled matrix to NaN. That is not a hypothetical: the reference garment
# reached ZOZO carrying a triangle of 3.6e-12 m² on a 10 mm lattice, and the
# solver stopped on the first Newton step of frame 0 with `PCG breakdown --
# p^T A p is not-a-number at iter 0`, having written no frames and no cause a
# Yohsai user could read.
#
# The gap this sits in was measured, not chosen. A panel the builder cuts
# cleanly bottoms out at 2.8e-4 of a cell (2.8e-8 m² on the 10 mm lattice it was
# measured on; the fraction is what carries over, since a triangle's area and a
# cell's area both scale with the pitch squared), and the triangle that broke
# the solve sat at 3.6e-8 of a cell, four decades below it. A floor of 1e-6 of a
# cell is therefore 280 times below the worst a good panel produces, at any
# pitch.
#
# Below the floor is ZOZO's own giving-up point, and that one is absolute rather
# than relative: `energy.cu` zeroes a bending stiffness under 1e-12 m² whatever
# the cell is. The margin above it is the one thing here that moves with the
# pitch -- 100x at 10 mm, 25x at the 5 mm this build cuts. Still a margin, and
# still nothing in between that has to be judged, but it is the number to watch
# if the pitch ever halves again.
_REST_AREA_FLOOR_FRACTION = 1.0e-6
# How many bad triangles to name in the status line before summarising.
_MAX_REPORT_FACES = 8


class ZozoHandoffError(RuntimeError):
    """The current Yohsai state cannot safely be handed to ZOZO."""


@dataclass(frozen=True)
class ClothQualityReport:
    """Whether the cloth's triangles are ones an implicit solver can integrate.

    Self-intersection is not the only way a garment can be unusable, and it was
    the only thing this hand-off checked. A sliver passes every intersection
    test ever written -- it crosses nothing -- and still stops the solve dead.
    Measuring it here is what turns a crash five minutes downstream, reported
    as `segfault / OOM-kill / unrecoverable abort` by a server guessing at a
    cause it never saw, into a refusal that names the triangle.
    """

    checked_faces: int
    area_min_m2: float
    area_floor_m2: float
    edge_min_m: float
    aspect_min: float
    failing_faces: int
    worst: tuple[tuple[int, float, float], ...] = ()

    @property
    def passed(self) -> bool:
        return self.failing_faces == 0

    def summary(self) -> str:
        return (
            f"triangle quality: {self.checked_faces} faces, smallest rest area "
            f"{self.area_min_m2:.2e} m² (floor {self.area_floor_m2:.2e}), "
            f"shortest edge {self.edge_min_m * 1000.0:.3f} mm, "
            f"worst aspect {self.aspect_min:.2e}, "
            f"{self.failing_faces} under the floor"
        )

    def error_report(self) -> str:
        text = (
            f"ERROR: {self.failing_faces} triangle(s) have too little rest area "
            f"for the solver: smallest {self.area_min_m2:.2e} m² against a floor "
            f"of {self.area_floor_m2:.2e} m². A shell element's stiffness scales "
            "with 1/area, so these take the first solve to NaN and it stops "
            "after frame 0"
        )
        if self.worst:
            shown = ", ".join(
                f"(face {index}: area {area:.2e} m², shortest edge "
                f"{edge * 1000.0:.4f} mm)"
                for index, area, edge in self.worst[:_MAX_REPORT_FACES]
            )
            text += f". Worst: {shown}"
            if self.failing_faces > len(self.worst[:_MAX_REPORT_FACES]):
                remaining = self.failing_faces - len(self.worst[:_MAX_REPORT_FACES])
                text += f", ... (+{remaining} more)"
        return text


@dataclass(frozen=True)
class ZozoPreparation:
    collection: bpy.types.Collection
    cloth_object: bpy.types.Object
    body_object: bpy.types.Object | None
    seam_count: int
    # The widest seam still open on the cloth being handed over: how well sewn
    # the garment ZOZO receives actually is.
    seam_distance_max_m: float
    cloth_group_name: str
    body_group_name: str
    project_name: str
    shell_isect: ShellIsectReport
    # Soft-stop (e.g. shell-isect NG): status box only, no body export / MCP.
    # Prefer this over raising so Blender never auto-reports as レポート:エラー.
    abort_message: str | None = None
    quality: ClothQualityReport | None = None
    # Panels re-cut by the preparation pass, in name order.
    remeshed_parts: tuple[str, ...] = ()

    def mcp_configuration(self, scene: bpy.types.Scene) -> dict:
        if self.body_object is None:
            raise ZozoHandoffError("ZOZO body was not exported; cannot configure MCP.")
        frame_start, frame_count, fps = _sync_scene_timeline_for_zozo(scene)
        # Integer steps per display frame: 1/fps/N. Prefer N=8 when it fits the
        # ZOZO step_size range (0.001–0.01); fall back to 0.005.
        step_size = 1.0 / float(fps) / 8.0
        if step_size < 0.001 or step_size > 0.01:
            step_size = 0.005
        return {
            "port": ZOZO_MCP_PORT,
            "cloth_object": self.cloth_object.name,
            "body_object": self.body_object.name,
            "cloth_group": self.cloth_group_name,
            "body_group": self.body_group_name,
            "scene_parameters": {
                "step_size": float(step_size),
                "frame_start": int(frame_start),
                "frame_count": int(frame_count),
                "use_scene_frame_start": False,
                "use_scene_fps": False,
                "frame_rate": int(fps),
                "gravity": [0.0, 0.0, -9.81],
                "inactive_momentum_frames": 5,
                "project_name": self.project_name,
            },
            "cloth_properties": {
                "contact_gap": ZOZO_CONTACT_GAP_M,
                "contact_offset": 0.0,
                "deformation_damping": 0.005,
                "bending_damping": 0.002,
                "bend_rest_angle_source": "FROM_GEOMETRY",
            },
            "body_properties": {
                "contact_gap": ZOZO_CONTACT_GAP_M,
                "contact_offset": 0.0,
            },
            "capture_timeout_seconds": 300.0,
        }


def _scene_fps(scene: bpy.types.Scene) -> int:
    return max(1, int(round(float(scene.render.fps) / float(scene.render.fps_base))))


def _intended_frame_range(scene: bpy.types.Scene) -> tuple[int, int]:
    """Return inclusive (start, end) frames the user intends to simulate.

    ZOZO's Scene panel Frame Count often tracks the preview range (or a prior
    MCP write), while Blender's ``frame_end`` can lag behind. Fetch/bake then
    only writes PC2 samples up to ``scene.frame_end``, so a UI that shows 250
    frames may only produce ~22 on disk. Prefer the preview range when active,
    and never choose a range shorter than the official scene range.
    """
    start = int(scene.frame_start)
    end = int(scene.frame_end)
    if bool(getattr(scene, "use_preview_range", False)):
        p0 = int(scene.frame_preview_start)
        p1 = int(scene.frame_preview_end)
        start = min(start, p0)
        end = max(end, p1)
    # ZOZO also exposes simulation_frame_* on some Blender builds / add-on states.
    try:
        s0 = int(getattr(scene, "simulation_frame_start", start))
        s1 = int(getattr(scene, "simulation_frame_end", end))
        start = min(start, s0)
        end = max(end, s1)
    except (TypeError, ValueError):
        pass
    if end < start:
        end = start
    return start, end


def _sync_scene_timeline_for_zozo(scene: bpy.types.Scene) -> tuple[int, int, int]:
    """Align Blender timeline with ZOZO frame_count so Run/Fetch can write all frames.

    Returns ``(frame_start, frame_count, fps)``.
    """
    start, end = _intended_frame_range(scene)
    # ZOZO docs: frame_count minimum 10.
    frame_count = max(10, end - start + 1)
    end = start + frame_count - 1
    fps = _scene_fps(scene)

    scene.frame_start = int(start)
    scene.frame_end = int(end)
    # Keep preview range covering the full sim so the timeline scrub matches ZOZO.
    if hasattr(scene, "frame_preview_start"):
        scene.frame_preview_start = int(start)
    if hasattr(scene, "frame_preview_end"):
        scene.frame_preview_end = int(end)
    try:
        if hasattr(scene, "simulation_frame_start"):
            scene.simulation_frame_start = int(start)
        if hasattr(scene, "simulation_frame_end"):
            scene.simulation_frame_end = int(end)
    except (AttributeError, TypeError):
        pass
    return int(start), int(frame_count), int(fps)


def _world_vertices(obj: bpy.types.Object) -> np.ndarray:
    local = np.empty((len(obj.data.vertices), 3), dtype=np.float64)
    obj.data.vertices.foreach_get("co", local.ravel())
    matrix = np.asarray([tuple(row) for row in obj.matrix_world], dtype=np.float64)
    return np.ascontiguousarray(local @ matrix[:3, :3].T + matrix[:3, 3])


def _pattern_positions(obj: bpy.types.Object) -> list[tuple[float, float]]:
    attribute = obj.data.attributes.get("yohsai_pattern_position")
    if (
        attribute is None
        or attribute.domain != "POINT"
        or attribute.data_type != "FLOAT_VECTOR"
        or len(attribute.data) != len(obj.data.vertices)
    ):
        raise ZozoHandoffError(
            f"{obj.name} has no valid Yohsai pattern coordinates; load the pattern again."
        )
    return [(float(item.vector[0]), float(item.vector[1])) for item in attribute.data]


def _set_uv(uv_layer, loop_index: int, value: tuple[float, float]) -> None:
    modern = getattr(uv_layer, "uv", None)
    if modern is not None:
        modern[loop_index].vector = value
    else:
        uv_layer.data[loop_index].uv = value


def _remove_object_and_owned_mesh(obj: bpy.types.Object) -> None:
    mesh = obj.data if obj.type == "MESH" else None
    bpy.data.objects.remove(obj, do_unlink=True)
    if mesh is not None and mesh.users == 0:
        bpy.data.meshes.remove(mesh)


def _handoff_collection(context, source: bpy.types.Collection) -> bpy.types.Collection:
    matches = [
        collection
        for collection in bpy.data.collections
        if collection.get("yohsai_role") == _HANDOFF_COLLECTION_ROLE
        and collection.get("yohsai_source_collection") == source.name
    ]
    handoff = matches[0] if matches else bpy.data.collections.new(f"{source.name}_ZOZO")
    if not matches:
        context.scene.collection.children.link(handoff)
    handoff["yohsai_role"] = _HANDOFF_COLLECTION_ROLE
    handoff["yohsai_source_collection"] = source.name
    for collection in matches:
        for obj in list(collection.objects):
            if (
                obj.get("yohsai_source_collection") == source.name
                and obj.get("yohsai_role") in {_HANDOFF_CLOTH_ROLE, _HANDOFF_BODY_ROLE}
            ):
                _remove_object_and_owned_mesh(obj)
    return handoff


def _create_cloth_object(
    handoff: bpy.types.Collection,
    source: bpy.types.Collection,
    parts: list[bpy.types.Object],
    positions: np.ndarray,
    seams: np.ndarray,
) -> bpy.types.Object:
    vertices = [tuple(point) for point in positions]
    edges: list[tuple[int, int]] = []
    faces: list[tuple[int, ...]] = []
    face_uvs: list[tuple[tuple[float, float], ...]] = []
    face_panel_indices: list[int] = []
    face_material_indices: list[int] = []
    vertex_part_indices: list[int] = []
    materials: list[bpy.types.Material] = []
    material_slots: dict[int, int] = {}
    offset = 0
    for part_index, obj in enumerate(parts):
        mesh = obj.data
        pattern = _pattern_positions(obj)
        edges.extend(
            (int(edge.vertices[0]) + offset, int(edge.vertices[1]) + offset)
            for edge in mesh.edges
        )
        panel_index = int(obj.get("yohsai_panel_index", part_index))
        local_materials: dict[int, int] = {}
        for index, material in enumerate(mesh.materials):
            if material is None:
                continue
            pointer = int(material.as_pointer())
            if pointer not in material_slots:
                material_slots[pointer] = len(materials)
                materials.append(material)
            local_materials[index] = material_slots[pointer]
        for polygon in mesh.polygons:
            polygon_vertices = tuple(int(vertex) + offset for vertex in polygon.vertices)
            faces.append(polygon_vertices)
            face_uvs.append(tuple(pattern[int(vertex)] for vertex in polygon.vertices))
            face_panel_indices.append(panel_index)
            face_material_indices.append(local_materials.get(int(polygon.material_index), 0))
        vertex_part_indices.extend([part_index] * len(mesh.vertices))
        offset += len(mesh.vertices)

    stitch_keys = {tuple(sorted((int(a), int(b)))) for a, b in seams}
    edges.extend((int(a), int(b)) for a, b in seams)
    name = f"{source.name}_ZOZO_CLOTH"
    mesh = bpy.data.meshes.new(name)
    cloth = bpy.data.objects.new(name, mesh)
    try:
        handoff.objects.link(cloth)
        mesh.from_pydata(vertices, edges, faces)
        mesh.update(calc_edges=True, calc_edges_loose=True)
        if len(mesh.vertices) != len(vertices) or len(mesh.polygons) != len(faces):
            raise ZozoHandoffError("The ZOZO hand-off topology changed while creating the mesh.")

        for material in materials:
            mesh.materials.append(material)
        for polygon, material_index in zip(mesh.polygons, face_material_indices):
            polygon.material_index = material_index

        panel_attribute = mesh.attributes.new(name="panel_index", type="INT", domain="FACE")
        for item, value in zip(panel_attribute.data, face_panel_indices):
            item.value = value
        part_attribute = mesh.attributes.new(name="yohsai_source_part", type="INT", domain="POINT")
        for item, value in zip(part_attribute.data, vertex_part_indices):
            item.value = value
        stitch_attribute = mesh.attributes.new(name="yohsai_zozo_stitch", type="BOOLEAN", domain="EDGE")
        found_stitches = 0
        for edge in mesh.edges:
            key = tuple(sorted((int(edge.vertices[0]), int(edge.vertices[1]))))
            if key in stitch_keys:
                stitch_attribute.data[edge.index].value = True
                found_stitches += 1
        if found_stitches != len(stitch_keys):
            raise ZozoHandoffError("A loose ZOZO stitch edge was lost while creating the mesh.")

        uv_layer = mesh.uv_layers.new(name="Yohsai Pattern", do_init=False)
        for polygon, uvs in zip(mesh.polygons, face_uvs):
            for loop_index, uv in zip(polygon.loop_indices, uvs):
                _set_uv(uv_layer, int(loop_index), uv)
        mesh.uv_layers.active = uv_layer
        mesh.uv_layers.active_render = uv_layer

        cloth["yohsai_schema"] = "yohsai-pattern/1.0.0"
        cloth["yohsai_role"] = _HANDOFF_CLOTH_ROLE
        cloth["yohsai_source_collection"] = source.name
        cloth["yohsai_source_parts"] = [part.name for part in parts]
        cloth["yohsai_zozo_contact_gap_m"] = ZOZO_CONTACT_GAP_M
        return cloth
    except Exception:
        _remove_object_and_owned_mesh(cloth)
        raise


def _crop_mesh_to_world_z_slab(
    obj: bpy.types.Object, z_min_m: float, z_max_m: float
) -> tuple[int, int]:
    """Keep triangles whose world-Z AABB intersects [z_min_m, z_max_m].

    Drops faces entirely outside the slab and then orphan vertices. Returns
    ``(faces_kept, faces_before)``. Topology outside the slab is discarded so
    ZOZO only receives the collider band under the garment.
    """
    mesh = obj.data
    faces_before = len(mesh.polygons)
    if faces_before == 0:
        return 0, 0
    if z_max_m < z_min_m:
        z_min_m, z_max_m = z_max_m, z_min_m

    # Evaluate world Z without applying the matrix to the mesh data, so parent
    # armature transforms and the source object's local frame stay intact.
    matrix = obj.matrix_world
    world = np.empty((len(mesh.vertices), 3), dtype=np.float64)
    mesh.vertices.foreach_get("co", world.ravel())
    rot = np.asarray([tuple(row) for row in matrix.to_3x3()], dtype=np.float64)
    loc = np.asarray(matrix.to_translation(), dtype=np.float64)
    world = world @ rot.T + loc
    z = world[:, 2]

    keep_poly: list[int] = []
    for poly in mesh.polygons:
        zs = z[list(poly.vertices)]
        if float(zs.max()) < z_min_m or float(zs.min()) > z_max_m:
            continue
        keep_poly.append(poly.index)
    if len(keep_poly) == faces_before:
        return faces_before, faces_before
    if not keep_poly:
        # Empty collider is worse than no crop; leave the full mesh and report.
        return faces_before, faces_before

    import bmesh

    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
        bm.faces.ensure_lookup_table()
        keep_set = set(keep_poly)
        to_delete = [f for f in bm.faces if f.index not in keep_set]
        bmesh.ops.delete(bm, geom=to_delete, context="FACES")
        loose = [v for v in bm.verts if not v.link_faces]
        if loose:
            bmesh.ops.delete(bm, geom=loose, context="VERTS")
        bm.to_mesh(mesh)
        mesh.update()
    finally:
        bm.free()
    return len(mesh.polygons), faces_before


def _create_body_object(
    handoff: bpy.types.Collection,
    source: bpy.types.Collection,
    body: bpy.types.Object,
    *,
    z_min_m: float | None = None,
    z_max_m: float | None = None,
) -> bpy.types.Object:
    duplicate = body.copy()
    duplicate.data = body.data.copy()
    duplicate.name = f"{source.name}_ZOZO_BODY"
    duplicate.data.name = f"{duplicate.name}_MESH"
    # Blender object copies inherit custom properties.  ZOZO UUIDs must stay
    # unique or assigning this collider could steal the source Body's group.
    if "_solver_uuid" in duplicate:
        del duplicate["_solver_uuid"]
    handoff.objects.link(duplicate)
    # Ensure world matrix matches the source before Z crop (object.copy keeps
    # parent; reassert the evaluated world transform so the slab is correct).
    try:
        duplicate.matrix_world = body.matrix_world.copy()
    except Exception:
        pass
    duplicate["yohsai_role"] = _HANDOFF_BODY_ROLE
    duplicate["yohsai_source_collection"] = source.name
    duplicate["yohsai_source_body"] = body.name
    if z_min_m is not None and z_max_m is not None:
        kept, total = _crop_mesh_to_world_z_slab(duplicate, float(z_min_m), float(z_max_m))
        duplicate["yohsai_body_z_min_m"] = float(min(z_min_m, z_max_m))
        duplicate["yohsai_body_z_max_m"] = float(max(z_min_m, z_max_m))
        duplicate["yohsai_body_faces_kept"] = int(kept)
        duplicate["yohsai_body_faces_total"] = int(total)
    duplicate.display_type = "WIRE"
    duplicate.show_in_front = True
    duplicate.hide_render = True
    return duplicate


def _cloth_quality(
    cloth: bpy.types.Object, parts: list[bpy.types.Object]
) -> ClothQualityReport:
    """Measure the cloth exactly as ZOZO will read it.

    Read from the hand-off mesh rather than from the panels, and after the
    shell-isect stage rather than before it, because that stage may have moved
    cloth vertices: the mesh that goes over is the only one worth measuring.
    Triangles come from ``loop_triangles`` for the same reason the intersection
    check uses them -- it is what the ZOZO encoder itself triangulates with.

    Each panel keeps its own lattice pitch, so the floor is computed per face
    from the pitch of the panel that face came from; a 5 mm panel is not held
    to a 10 mm panel's cell.
    """
    mesh = cloth.data
    mesh.calc_loop_triangles()
    face_count = len(mesh.loop_triangles)
    vertex_count = len(mesh.vertices)
    if face_count == 0 or vertex_count == 0:
        return ClothQualityReport(0, 0.0, 0.0, 0.0, 0.0, 0)

    positions = np.empty((vertex_count, 3), dtype=np.float64)
    mesh.vertices.foreach_get("co", positions.ravel())
    triangles = np.empty(face_count * 3, dtype=np.int32)
    mesh.loop_triangles.foreach_get("vertices", triangles)
    triangles = triangles.reshape(face_count, 3)

    part_of_vertex = np.zeros(vertex_count, dtype=np.int32)
    attribute = mesh.attributes.get("yohsai_source_part")
    if attribute is not None and len(attribute.data) == vertex_count:
        attribute.data.foreach_get("value", part_of_vertex)
    spacings = np.asarray(
        [part_spacing_m(part) for part in parts],
        dtype=np.float64,
    )
    if not len(spacings):
        spacings = np.asarray([MESH_SPACING_M], dtype=np.float64)
    owner = np.clip(part_of_vertex[triangles[:, 0]], 0, len(spacings) - 1)
    floors = (spacings[owner] ** 2) * _REST_AREA_FLOOR_FRACTION

    corners = positions[triangles]
    first = corners[:, 1] - corners[:, 0]
    second = corners[:, 2] - corners[:, 0]
    areas = 0.5 * np.linalg.norm(np.cross(first, second), axis=1)
    lengths = np.stack(
        [
            np.linalg.norm(first, axis=1),
            np.linalg.norm(second, axis=1),
            np.linalg.norm(corners[:, 2] - corners[:, 1], axis=1),
        ],
        axis=1,
    )
    longest = lengths.max(axis=1)
    # Twice the area over the longest edge squared: 1 for equilateral, and it
    # falls with the triangle's height, which is the shape the Hessian minds.
    with np.errstate(divide="ignore", invalid="ignore"):
        aspect = np.where(longest > 0.0, 2.0 * areas / (longest * longest), 0.0)

    failing = np.flatnonzero(areas < floors)
    worst = tuple(
        (int(index), float(areas[index]), float(lengths[index].min()))
        for index in failing[np.argsort(areas[failing])][:_MAX_REPORT_FACES]
    )
    return ClothQualityReport(
        checked_faces=face_count,
        area_min_m2=float(areas.min()),
        area_floor_m2=float(floors.max()),
        edge_min_m=float(lengths.min()),
        aspect_min=float(aspect.min()),
        failing_faces=int(failing.size),
        worst=worst,
    )


def _stale_pitch_parts(
    collection: bpy.types.Collection,
) -> tuple[tuple[str, float], ...]:
    """Parts cut on a lattice pitch this build no longer cuts.

    A `.blend` outlives the code that filled it, and the pitch is the one thing
    about a panel that the hand-off cannot quietly correct. Re-cutting is how it
    would be corrected, and re-cutting after the garment is sewn resamples the
    drape onto a topology the solver never saw -- which is the whole reason the
    pitch was changed at the source rather than here.

    Left alone, the two silent outcomes are both bad and neither says anything:
    a garment whose seam counts already agree is handed over at its old pitch,
    unchanged and unmentioned; a garment with one mismatched seam has that one
    panel re-cut fine and the rest left coarse, and goes over half converted.
    Naming it is cheap, and Update followed by GRAVITY is a repair the operator
    can actually carry out.
    """
    stale: list[tuple[str, float]] = []
    for obj in participating_parts(collection):
        spacing = part_spacing_m(obj)
        if abs(spacing - MESH_SPACING_M) > 1.0e-9:
            stale.append((obj.name, spacing))
    return tuple(stale)


def _project_name(collection_name: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_-]+", "_", collection_name).strip("_")
    return f"yohsai_{value or 'clothes'}"


def prepare_for_zozo(
    context,
    collection: bpy.types.Collection | None,
    body: bpy.types.Object | None,
    *,
    shell_isect_include_body: bool = False,
    body_z_min_m: float = 0.4,
    body_z_max_m: float = 1.45,
) -> ZozoPreparation:
    """Create solver-owned cloth/body copies and leave Yohsai untouched.

    The cloth is the garment as it stands right now, read the same way Zero
    GRAVITY reads it: the participating panels, in their panel order, at their
    current world positions, with the seams the verified Sewing plan names.
    Reading it from a stored solver state instead would hand ZOZO whichever
    garment that solver last finished, which is not necessarily the one on
    screen.

    ``shell_isect_include_body`` enables the full cloth+body twin check (slow on
    high-poly characters). Default is cloth-only shell-isect for practical time;
    the body copy is still built for ZOZO MCP / Transfer.

    ``body_z_min_m`` / ``body_z_max_m`` limit the exported body copy to the
    world-Z slab that actually collides with the garment (defaults 0.4–1.45 m).
    """
    if collection is None or collection.get("yohsai_role") != "clothes":
        raise ZozoHandoffError("Select a loaded Yohsai Clothes collection first.")
    if body is None or body.type != "MESH":
        raise ZozoHandoffError("Select a mesh Body before Prepare for ZOZO.")
    context.view_layer.update()

    # Pitch before topology: the re-cut below would convert some panels to the
    # current pitch and leave the rest, and neither half of that is worth
    # handing over.
    stale = _stale_pitch_parts(collection)
    if stale:
        shown = ", ".join(
            f"{name} ({spacing * 1000.0:.0f} mm)" for name, spacing in stale[:4]
        )
        if len(stale) > 4:
            shown += f", ... (+{len(stale) - 4} more)"
        raise ZozoHandoffError(
            f"{len(stale)} panel(s) were cut on a lattice this build no longer "
            f"cuts and would go over at the wrong pitch: {shown}, against "
            f"{MESH_SPACING_M * 1000.0:.0f} mm. Press Update to re-cut the "
            "pattern, then run GRAVITY again before handing over"
        )

    # Re-cut first, so what goes over is a panel this file built rather than
    # whichever one has been sitting in the scene. A garment carried across
    # sessions keeps the topology of the version that cut it, and the sliver
    # that stopped the ZOZO solve came from exactly that: panels older than the
    # triangulation now in this file, which rebuilds them clean. Seam counts are
    # matched in the same pass because a seam whose two sides disagree is the
    # other thing that has to be true before any of this is worth handing over.
    try:
        remeshed = tuple(sorted(remesh_with_seam_counts(context, collection)))
    except UpdateError as exc:
        raise ZozoHandoffError(str(exc)) from exc
    if remeshed:
        context.view_layer.update()

    try:
        ranges = part_ranges(collection, "Prepare for ZOZO")
        seams = _seam_constraints_from_parts(collection, ranges)
    except KitsukeError as exc:
        raise ZozoHandoffError(str(exc)) from exc
    if seams.size == 0:
        raise ZozoHandoffError("The garment has no sewing edges.")

    parts = [part.obj for part in ranges]
    positions = np.concatenate([_world_vertices(part) for part in parts])
    if not np.all(np.isfinite(positions)):
        raise ZozoHandoffError("The cloth contains a non-finite vertex position.")
    if seams.min() < 0 or seams.max() >= len(positions):
        raise ZozoHandoffError("The sewing pairs do not match the current panel vertices.")
    gaps = np.linalg.norm(positions[seams[:, 0]] - positions[seams[:, 1]], axis=1)
    seam_distance_max = float(np.max(gaps))

    handoff = _handoff_collection(context, collection)
    cloth = _create_cloth_object(handoff, collection, parts, positions, seams)

    # Body copy is required before shell-isect: Transfer checks cloth+STATIC body
    # as one mesh (collider × collider skipped). Twin detection must see body.
    # Crop to the operator Z band so ZOZO does not carry the full character.
    try:
        body_copy = _create_body_object(
            handoff,
            collection,
            body,
            z_min_m=float(body_z_min_m),
            z_max_m=float(body_z_max_m),
        )
    except Exception:
        _remove_object_and_owned_mesh(cloth)
        raise

    for selected in context.selected_objects:
        selected.select_set(False)
    cloth.select_set(True)
    context.view_layer.objects.active = cloth
    context.view_layer.update()

    cloth_group_name = f"Yohsai {collection.name} Cloth"
    body_group_name = f"Yohsai {collection.name} Body"
    cloth["yohsai_zozo_group"] = cloth_group_name
    body_copy["yohsai_zozo_group"] = body_group_name

    # Triangle self-intersection: shell-isect only (not host BVH).
    # Strict stages: CHECK 1 → FIX → CHECK 2 (see shell_isect_bridge).
    # Default cloth-only (fast). Optional cloth+body twin is slow.
    # PASS (check2 pairs == 0): MCP. NG: report face pairs, keep copies, no MCP.
    shell_report = run_check_and_fix(
        cloth,
        body_copy,
        include_body=bool(shell_isect_include_body),
    )
    cloth["yohsai_shell_isect"] = shell_report.summary()
    cloth["yohsai_shell_isect_version"] = shell_report.version
    cloth["yohsai_shell_isect_include_body"] = bool(shell_report.include_body)
    cloth["yohsai_shell_isect_pipeline"] = shell_report.pipeline_token()
    cloth["yohsai_shell_isect_checks_run"] = int(shell_report.checks_run)
    cloth["yohsai_shell_isect_fix_attempted"] = bool(shell_report.fix_attempted)
    cloth["yohsai_shell_isect_pairs_before"] = int(shell_report.pairs_before)
    cloth["yohsai_shell_isect_pairs_after"] = int(shell_report.pairs_after)
    cloth["yohsai_shell_isect_fix"] = shell_report.fix_status
    cloth["yohsai_shell_isect_cloth_faces"] = int(shell_report.n_cloth_faces)
    if shell_report.pairs:
        cloth["yohsai_shell_isect_face_pairs"] = [
            f"{a},{b}" for a, b in shell_report.pairs
        ]
    elif "yohsai_shell_isect_face_pairs" in cloth:
        del cloth["yohsai_shell_isect_face_pairs"]

    if not shell_report.passed:
        # Do not raise: Blender surfaces uncaught/mismatched exceptions as
        # レポート:エラー. Soft-abort keeps the message in the status box only.
        # Body copy stays for inspection (cloth–body pairs need both meshes).
        return ZozoPreparation(
            collection=handoff,
            cloth_object=cloth,
            body_object=body_copy,
            seam_count=len(seams),
            seam_distance_max_m=seam_distance_max,
            cloth_group_name=cloth_group_name,
            body_group_name=body_group_name,
            project_name=_project_name(collection.name),
            shell_isect=shell_report,
            abort_message=shell_report.error_report(),
            remeshed_parts=remeshed,
        )

    # Triangle quality: the second way a garment can be unusable, and the one
    # nothing here used to look at. Measured after shell-isect because that
    # stage can move cloth, and before MCP because the whole point is to say so
    # here rather than let the solver find out.
    quality = _cloth_quality(cloth, parts)
    cloth["yohsai_rest_area_min_m2"] = float(quality.area_min_m2)
    cloth["yohsai_rest_area_floor_m2"] = float(quality.area_floor_m2)
    cloth["yohsai_edge_min_m"] = float(quality.edge_min_m)
    cloth["yohsai_aspect_min"] = float(quality.aspect_min)
    cloth["yohsai_quality_failing_faces"] = int(quality.failing_faces)

    return ZozoPreparation(
        collection=handoff,
        cloth_object=cloth,
        body_object=body_copy,
        seam_count=len(seams),
        seam_distance_max_m=seam_distance_max,
        cloth_group_name=cloth_group_name,
        body_group_name=body_group_name,
        project_name=_project_name(collection.name),
        shell_isect=shell_report,
        abort_message=None if quality.passed else quality.error_report(),
        quality=quality,
        remeshed_parts=remeshed,
    )
