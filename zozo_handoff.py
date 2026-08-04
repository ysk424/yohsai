# SPDX-License-Identifier: GPL-3.0-or-later
"""Create a non-destructive Yohsai hand-off for ZOZO Contact Solver."""

from __future__ import annotations

from dataclasses import dataclass
import re

import bpy
import numpy as np
from mathutils import Vector
from mathutils.bvhtree import BVHTree

from .kitsuke import KitsukeError, completed_kitsuke_handoff
from .shell_isect_bridge import ShellIsectReport, run_check_and_fix


ZOZO_MCP_PORT = 9633
ZOZO_CONTACT_GAP_M = 0.001
# ZOZO keeps a loose stitch edge open by 1.1 * (gap_a + gap_b) + 1e-5.
ZOZO_STITCH_OPENING_M = 1.1 * (2.0 * ZOZO_CONTACT_GAP_M) + 1.0e-5
MAX_HANDOFF_SEAM_DISTANCE_M = 0.01
# Body clearance used when opening stitch layers away from the collider surface.
_BODY_CLEARANCE_M = 1.1 * ZOZO_CONTACT_GAP_M
_HANDOFF_COLLECTION_ROLE = "zozo_handoff"
_HANDOFF_CLOTH_ROLE = "zozo_cloth"
_HANDOFF_BODY_ROLE = "zozo_body"


class ZozoHandoffError(RuntimeError):
    """The current Yohsai state cannot safely be handed to ZOZO."""


@dataclass(frozen=True)
class ZozoPreparation:
    collection: bpy.types.Collection
    cloth_object: bpy.types.Object
    body_object: bpy.types.Object | None
    seam_count: int
    maximum_input_seam_distance_m: float
    minimum_output_seam_distance_m: float
    cloth_group_name: str
    body_group_name: str
    project_name: str
    shell_isect: ShellIsectReport
    # Soft-stop (e.g. shell-isect NG): status box only, no body export / MCP.
    # Prefer this over raising so Blender never auto-reports as レポート:エラー.
    abort_message: str | None = None

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


def _evaluated_body_bvh(context, body: bpy.types.Object) -> tuple[BVHTree, np.ndarray]:
    depsgraph = context.evaluated_depsgraph_get()
    evaluated = body.evaluated_get(depsgraph)
    mesh = evaluated.to_mesh()
    try:
        if not mesh.vertices or not mesh.polygons:
            raise ZozoHandoffError("The selected Body needs a surface mesh for ZOZO contact.")
        matrix = evaluated.matrix_world
        vertices = [matrix @ vertex.co for vertex in mesh.vertices]
        faces = [tuple(polygon.vertices) for polygon in mesh.polygons]
        bvh = BVHTree.FromPolygons(vertices, faces, all_triangles=False)
        if bvh is None:
            raise ZozoHandoffError("The selected Body could not be converted to a contact surface.")
        centroid = np.mean(np.asarray([tuple(point) for point in vertices], dtype=np.float64), axis=0)
        return bvh, centroid
    finally:
        evaluated.to_mesh_clear()


def _component_normal(
    bvh: BVHTree,
    body_centroid: np.ndarray,
    positions: np.ndarray,
    component: list[int],
) -> tuple[np.ndarray, float]:
    center = np.mean(positions[component], axis=0)
    nearest = bvh.find_nearest(Vector(center))
    if nearest is not None:
        location, surface_normal, _face_index, _distance = nearest
        normal = np.asarray(tuple(surface_normal), dtype=np.float64)
        normal_length = float(np.linalg.norm(normal))
        if normal_length > 1.0e-10:
            normal /= normal_length
            toward_cloth = center - np.asarray(tuple(location), dtype=np.float64)
            if float(np.dot(toward_cloth, normal)) < 0.0:
                normal *= -1.0
        else:
            normal = center - body_centroid
    else:
        normal = center - body_centroid
    length = float(np.linalg.norm(normal))
    if length <= 1.0e-10:
        normal = np.asarray((0.0, 0.0, 1.0), dtype=np.float64)
    else:
        normal /= length

    minimum_distance = float("inf")
    for vertex_index in component:
        hit = bvh.find_nearest(Vector(positions[vertex_index]))
        if hit is not None:
            minimum_distance = min(minimum_distance, float(hit[3]))
    return normal, minimum_distance


def _seam_components(seams: np.ndarray) -> tuple[dict[int, set[int]], list[list[int]]]:
    adjacency: dict[int, set[int]] = {}
    for a_value, b_value in seams:
        a, b = int(a_value), int(b_value)
        adjacency.setdefault(a, set()).add(b)
        adjacency.setdefault(b, set()).add(a)
    components: list[list[int]] = []
    remaining = set(adjacency)
    while remaining:
        start = min(remaining)
        stack = [start]
        component: list[int] = []
        remaining.remove(start)
        while stack:
            vertex = stack.pop()
            component.append(vertex)
            for neighbor in sorted(adjacency[vertex], reverse=True):
                if neighbor in remaining:
                    remaining.remove(neighbor)
                    stack.append(neighbor)
        components.append(sorted(component))
    return adjacency, components


def _greedy_colors(adjacency: dict[int, set[int]], component: list[int]) -> dict[int, int]:
    """Color one local stitch graph so every loose edge gets a positive gap."""
    colors: dict[int, int] = {}
    # High-degree vertices first keeps unequal seam samplings at two layers in
    # the common case, while still handling an occasional three-way junction.
    for vertex in sorted(component, key=lambda item: (-len(adjacency[item]), item)):
        used = {colors[neighbor] for neighbor in adjacency[vertex] if neighbor in colors}
        color = 0
        while color in used:
            color += 1
        colors[vertex] = color
    return colors


def _open_stitches(
    positions: np.ndarray,
    seams: np.ndarray,
    bvh: BVHTree,
    body_centroid: np.ndarray,
) -> tuple[np.ndarray, float, float]:
    before = np.linalg.norm(positions[seams[:, 0]] - positions[seams[:, 1]], axis=1)
    maximum_before = float(np.max(before)) if before.size else 0.0

    # Matched 1:1 gather sewing closes every seam except a few isolated pinch
    # points (shoulder / underarm) where the body sits between the two sides and
    # holds them apart.  Those residual pairs are excluded from the ZOZO stitch
    # opening below and welded shut afterwards, so they play no part in the layer
    # spacing of their neighbours.
    residual = np.nonzero(before > MAX_HANDOFF_SEAM_DISTANCE_M)[0]
    open_seams = np.delete(seams, residual, axis=0)

    result = positions.copy()
    adjacency, components = _seam_components(open_seams)
    for component in components:
        normal, minimum_body_distance = _component_normal(
            bvh, body_centroid, result, component
        )
        colors = _greedy_colors(adjacency, component)
        component_set = set(component)
        projections = [
            abs(float(np.dot(result[int(b)] - result[int(a)], normal)))
            for a, b in open_seams
            if int(a) in component_set
        ]
        layer_spacing = ZOZO_STITCH_OPENING_M + max(projections, default=0.0)
        clearance = (
            max(0.0, _BODY_CLEARANCE_M - minimum_body_distance)
            if np.isfinite(minimum_body_distance)
            else 0.0
        )
        for vertex in component:
            result[vertex] += normal * (clearance + colors[vertex] * layer_spacing)

    # Fill each residual pinch hole by welding its pair onto whichever endpoint
    # sits farther outside the body, so the adjacent panel triangles stretch
    # across the gap without bending the panels or being driven into the body.
    # Like a real 2 mm stitch line this is not perfect, which is fine for a
    # garment.
    for index in residual:
        first, second = int(seams[index, 0]), int(seams[index, 1])
        _, _, _, distance_first = bvh.find_nearest(Vector(result[first]))
        _, _, _, distance_second = bvh.find_nearest(Vector(result[second]))
        anchor = first if (distance_first or 0.0) >= (distance_second or 0.0) else second
        result[first] = result[anchor]
        result[second] = result[anchor]

    if open_seams.size:
        after = np.linalg.norm(result[open_seams[:, 0]] - result[open_seams[:, 1]], axis=1)
        minimum_after = float(np.min(after))
        if minimum_after + 1.0e-8 < ZOZO_STITCH_OPENING_M:
            raise ZozoHandoffError(
                "Could not create a positive ZOZO contact gap at every sewing edge."
            )
    else:
        minimum_after = ZOZO_STITCH_OPENING_M
    return result, maximum_before, minimum_after


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
        cloth["yohsai_zozo_stitch_opening_m"] = ZOZO_STITCH_OPENING_M
        return cloth
    except Exception:
        _remove_object_and_owned_mesh(cloth)
        raise


def _create_body_object(
    handoff: bpy.types.Collection,
    source: bpy.types.Collection,
    body: bpy.types.Object,
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
    duplicate["yohsai_role"] = _HANDOFF_BODY_ROLE
    duplicate["yohsai_source_collection"] = source.name
    duplicate["yohsai_source_body"] = body.name
    duplicate.display_type = "WIRE"
    duplicate.show_in_front = True
    duplicate.hide_render = True
    return duplicate


def _project_name(collection_name: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_-]+", "_", collection_name).strip("_")
    return f"yohsai_{value or 'clothes'}"


def prepare_for_zozo(
    context,
    collection: bpy.types.Collection | None,
    body: bpy.types.Object | None,
    *,
    shell_isect_include_body: bool = False,
) -> ZozoPreparation:
    """Create solver-owned cloth/body copies and leave Yohsai untouched.

    ``shell_isect_include_body`` enables the full cloth+body twin check (slow on
    high-poly characters). Default is cloth-only shell-isect for practical time;
    the body copy is still built for ZOZO MCP / Transfer.
    """
    if collection is None or collection.get("yohsai_role") != "clothes":
        raise ZozoHandoffError("Select a loaded Yohsai Clothes collection first.")
    if body is None or body.type != "MESH":
        raise ZozoHandoffError("Select a mesh Body before Prepare for ZOZO.")
    try:
        parts, seams = completed_kitsuke_handoff(collection)
    except KitsukeError as exc:
        raise ZozoHandoffError(str(exc)) from exc
    if seams.size == 0:
        raise ZozoHandoffError("The completed GRAVITY state has no sewing edges.")

    context.view_layer.update()
    positions = np.concatenate([_world_vertices(part) for part in parts])
    if not np.all(np.isfinite(positions)):
        raise ZozoHandoffError("The completed cloth contains a non-finite vertex position.")
    bvh, body_centroid = _evaluated_body_bvh(context, body)
    positions, maximum_before, minimum_after = _open_stitches(
        positions, seams, bvh, body_centroid
    )

    handoff = _handoff_collection(context, collection)
    cloth = _create_cloth_object(handoff, collection, parts, positions, seams)

    # Body copy is required before shell-isect: Transfer checks cloth+STATIC body
    # as one mesh (collider × collider skipped). Twin detection must see body.
    try:
        body_copy = _create_body_object(handoff, collection, body)
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
            maximum_input_seam_distance_m=maximum_before,
            minimum_output_seam_distance_m=minimum_after,
            cloth_group_name=cloth_group_name,
            body_group_name=body_group_name,
            project_name=_project_name(collection.name),
            shell_isect=shell_report,
            abort_message=shell_report.error_report(),
        )

    return ZozoPreparation(
        collection=handoff,
        cloth_object=cloth,
        body_object=body_copy,
        seam_count=len(seams),
        maximum_input_seam_distance_m=maximum_before,
        minimum_output_seam_distance_m=minimum_after,
        cloth_group_name=cloth_group_name,
        body_group_name=body_group_name,
        project_name=_project_name(collection.name),
        shell_isect=shell_report,
    )
