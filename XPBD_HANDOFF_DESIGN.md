# Blender 5.2 XPBD hand-off design

## Decision

Blender XPBD needs a separate `Prepare for Blender XPBD` button. It may share
Yohsai's completed-GRAVITY extraction and neutral combined-mesh builder, but it
must not reuse the ZOZO-ready mesh.

The two targets require opposite seam initialization:

- ZOZO needs every loose stitch edge opened beyond the combined shell contact
  gaps; Yohsai currently uses at least 2.21 mm.
- Blender's stock `Cloth Dynamics (Experimental)` asset records the current
  length of every mesh edge as its `rest_length`. A loose stitch edge therefore
  needs an explicit zero rest length; feeding it the ZOZO gap would preserve the
  open seam instead of sewing it.

## Verified Blender 5.2 surface

This design was checked with Blender 5.2.0 LTS, commit `fbe6228777e7`, built
2026-07-14. XPBD is not a new Cloth modifier. The low-level implementation is
the `GeometryNodeXPBDSolver` node, labelled `XPBD Solver (Experimental)`.
Blender ships the usable modifier assets in:

`5.2/datafiles/assets/nodes/geometry_nodes_dynamics_assets.blend`

The relevant marked assets are:

- `Cloth Dynamics (Experimental)`, a Geometry Nodes modifier asset;
- `Collider`, a Geometry Nodes modifier asset.

The cloth asset exposes Geometry, Pin Group, Stretchiness, Bendiness, Substeps,
Constraint Steps, Mass, Friction, Collision Radius, Linear Damping, Gravity,
tearing, and an Effectors Collection. Its shipped solver defaults are five
substeps and fifteen constraint steps. The Collider asset exposes Deforming,
Boundary, Margin, Friction, Softness, and filtering.

Blender 5.2 also changed scripted modifier inputs. Values are assigned through
`modifier.properties.inputs.<socket_identifier>.value`; the legacy
`modifier["Socket_N"]` ID-property form is not supported by this build. Code
must discover and validate identifiers from the asset interface instead of
assuming older Geometry Nodes modifier behavior.

## Proposed button contract

`Prepare for Blender XPBD` will:

1. Require Blender 5.2 or later, the dynamics asset library, a completed Yohsai
   GRAVITY state, and a mesh Body.
2. Create target-owned cloth and Body copies without modifying the Yohsai
   source parts.
3. Preserve the active pattern UV and source-panel attributes for inspection,
   although the stock XPBD asset is isotropic and does not consume Yohsai's
   warp/weft material metric.
4. Keep loose seam edges, mark them with a dedicated Boolean edge attribute,
   and use a Yohsai-owned copy/wrapper of the cloth node group that writes
   `rest_length = 0` on only those edges after Blender's `Setup Structural Rest
   Data` stage. Surface structural edges keep their captured pattern length.
5. Append the official `Cloth Dynamics (Experimental)` and `Collider` assets,
   add the cloth modifier to the garment copy, add the collider modifier last
   on the Body copy, and connect a dedicated effectors collection.
6. Set Collider `Deforming` when the Body has an Armature, Lattice, Mesh Deform,
   animated shape keys, or vertex-driving animation. No separate ZOZO-style
   deformation capture is required because Geometry Nodes evaluates the
   animated collider on Blender's timeline.
7. Start with a 1 mm cloth collision radius and 1 mm collider margin rather
   than the asset's general-purpose 10 mm cloth radius. Substeps, constraint
   steps, stretchiness, bendiness, damping, and friction remain visible for
   user tuning.
8. Leave playback and simulation-cache baking to Blender. The button prepares
   the modifier graph but does not advance the timeline or bake automatically.

The button is hidden or disabled on Blender 5.1. Experimental asset names,
interfaces, and internal node layout must be validated each time the button is
pressed; a changed Blender asset should produce a clear compatibility error,
not a partly wired simulation.

## Acceptance checks for implementation

- Source Yohsai objects are bit-for-bit unchanged after preparation.
- The generated cloth has one zero-rest loose constraint per stored Yohsai seam
  pair, including unequal seam samplings and multipart rings.
- Structural mesh edges retain their nonzero captured rest lengths.
- An animated Body evaluates through the Collider modifier for every frame.
- Frame-one evaluation has no modifier warnings and no non-finite positions.
- Repeating Prepare replaces only Yohsai's previous XPBD copies and node group;
  user-created Geometry Nodes objects and caches are left alone.
