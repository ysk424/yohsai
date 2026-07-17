# Yohsai

Yohsai is an experimental Blender extension for constructing and incrementally
dressing pattern-based clothing. The Illustrator pattern is authoritative;
Blender meshes are replaceable physical realizations of that pattern.

Only explicit commands and documented data have meaning. Yohsai must not infer
garment-part semantics, intended shape, or Body-relative placement from panel
names, seam layout, or visual similarity.

## Workflow

The N-panel contains these inputs:

- `Pattern Path`: the Illustrator PDF;
- `Clothes`: the loaded Yohsai collection;
- `Body`: the fixed collision mesh used by GRAVITY.

The normal operation order is:

1. `Load` creates one Mesh object per pattern panel instance and turns `Auto` on.
2. Translate and rotate the separate parts in Object Mode.
3. Select the Body, then press `Zero GRAVITY` or `Normal GRAVITY`. Yohsai runs
   Sewing automatically immediately before the simulation.
4. Continue placement and use either GRAVITY button in any order.
5. Use `Update` after editing the same Illustrator PDF.
6. After the garment is sewn, start the ZOZO MCP server on port 9633 and use
   `Prepare for ZOZO` to create an animation hand-off.

Every part has two independent attributes: the monotonic `PLACED` -> `PENDING`
-> `DONE` state and one deformation Lock. Moving a `PLACED` part makes it
`PENDING` at the next GRAVITY click and unlocks it; a successful click changes
its state to `DONE` without changing that Lock. It therefore remains deformable
for repeated GRAVITY clicks.

Load turns `Auto` on and performs one Auto-lock operation: `PLACED` and existing
`DONE` parts are locked, while pending parts are unlocked. Turning Auto off
immediately unlocks every non-placed part; turning it on performs the Auto-lock
operation again. A `PLACED` part remains outside the simulation. `Lock` directly
changes the same independent deformation attribute on selected clothes parts.
The Auto control is a colored toggle whose pressed state shows that it is on.

## Pattern input

The parser accepts a one-page Illustrator PDF. Each closed panel must contain a
unique `#` label. Page vertical is warp and page horizontal is weft.
Illustrator layer and sublayer names are ignored; standard PDF page content is
read as one flattened drawing. A PDF text object whose first non-whitespace
characters are `//` is a comment and is ignored in full.

Supported annotations are:

- a single letter for a sewing group;
- `@W` for a fold edge;
- `@M` for authored-left and mirrored-right instances;
- two `RING` edges plus `@TOP` for a welded ring construction.

No undocumented annotation has implied behavior.

Load samples a 5 mm square material lattice in pattern-page coordinates and
uses triangles as the Blender and collision proxy. Pattern coordinates retain
the material rest state.

## Automatic Sewing

The GRAVITY buttons run Sewing from the world-space positions of the separate
source parts before a new pending stage. Sewing orders
marked boundary paths, matches them by normalized authored distance, and stores
cross-panel pairs in a transient preview.

Load records every part's initial Object Mode transform. Automatic Sewing ignores
parts still in `PLACED`, includes `PENDING` parts as the new work, and retains
`DONE` parts as connectivity anchors. Unresolvable paths stay pending; when a
moved part completes one side of a multipart sewing group, that side is sewn
without waiting for later parts.

The preview is a visual connectivity record. It does not define a replacement
initial cloth shape. Body geometry is not used by Sewing.

## GRAVITY

GRAVITY starts from the positioned source-panel vertices. The solver is always
the native CPU Square-Lattice Cloth solver; no solver or iteration setup is
required.

Each click applies:

- existing velocity and downward gravity;
- distance-independent attraction and zero-distance capture on explicit seam pairs;
- authored material-edge stretch, square-cell shear, and weak axial bending;
- Body contact correction.

Pattern attributes define every material rest value. The render-triangulation
diagonal carries no spring, and no material term reads Body shape, normals, or
bones. The Body may influence particles only through collision contact.

Every click uses eight 1/240-second substeps, 20 material/contact iterations,
constant-magnitude seam attraction with 2 mm capture, and 5 mm contact
thickness. `Zero gravity` applies 0 m/s² and `Normal gravity` applies 9.81 m/s²
in world -Z. The two buttons may be alternated without resetting the live
simulation.

After a click, positions are scattered back to the separate part objects. Object
translation and rotation are supported between clicks; scaling and vertex-count
changes are rejected. Finite movement is not capped or rolled back; rollback is
reserved for a non-finite solver state.

On a successful click, pending parts become `DONE` without being relocked, so
GRAVITY may repeat immediately. Moving another placed part starts a new
automatic Sewing stage. A later Load or switching Auto on performs the explicit
Auto-lock operation and locks done parts while retaining seam connectivity.

Undo and Redo store the solver state needed to reconstruct the live session
inside the same add-on runtime. Continuing a partially dressed session after
restarting Blender is unsupported.

## Prepare for ZOZO

`Prepare for ZOZO` is available after at least one completed GRAVITY step. It
does not modify, join, rename, hide, or move the source Yohsai parts. It creates
a dedicated cloth copy with one active `Yohsai Pattern` UV map, loose stitch
edges, and a dedicated Body collider copy that retains the Body's modifiers,
parent, shape keys, and animation links.

ZOZO cannot start with two contact surfaces exactly coincident. On the copy
only, each loose stitch is opened to at least 2.21 mm: 1.1 times the two 1 mm
contact gaps plus 0.01 mm. A seam still more than 10 mm open is rejected; use
Zero GRAVITY to close it first. The original completed garment remains the
authoritative Yohsai state.

The button configures ZOZO Contact Solver through its localhost MCP endpoint at
`http://localhost:9633/mcp`. It replaces only the two groups named for the
selected Yohsai collection, creates a SHELL and STATIC group, uses absolute 1
mm contact gaps, preserves the initial fitted shape as the bending rest shape,
and sets conservative damping and five inactive-momentum frames. If the Body
copy deforms through an Armature, Lattice, Mesh Deform, shape keys, or drivers,
the MCP client also records its deformation cache.

MCP setup runs outside Blender's main thread so ZOZO can safely execute its
queued Blender operations. If the MCP server is stopped, the hand-off copies
remain valid: start ZOZO MCP on port 9633 and press Prepare again. Yohsai never
starts Transfer or the simulation automatically; inspect the groups, then use
ZOZO's `Transfer` and `Run Simulation` controls.

## Update

Update rereads the same PDF and recuts the selected Clothes collection. Stable
`#` labels and mirror instances identify corresponding parts. Existing object
identity, transforms, materials, and collection ownership remain.

If sewing membership changes, the next eligible GRAVITY click rebuilds Sewing
automatically. Pattern topology and material rest dimensions always come from
the revised PDF.

## Silhouette utility

Character silhouettes are exported separately with
`UTIL/silhouette_export.py`. See `UTIL/README.md`.

## Documentation

- `SVG_TO_JSON_SPEC.md`: input, JSON, Load, automatic Sewing, GRAVITY, and Update contract;
- `DESIGN_PHILOSOPHY.md`: product-level interpretation rules;
- `KITSUKE_DESIGN.md`: current simulation workflow and invariants;
- `COSSERAT_DESIGN.md`: native particle solver and compatibility boundary;
- `GRAINLINE_DESIGN.md`: grain-aligned mesh and material mapping;
- `XPBD_HANDOFF_DESIGN.md`: Blender 5.2 experimental cloth compatibility plan;
- `SESSION_MEMORY.md`: concise current handoff.

## Native development

Visual Studio 2022 Build Tools, CMake, and the standard OpenMP runtime are
sufficient for the Windows CPU backend:

```powershell
.\build_native.ps1 -Configuration Release
```

The script builds the DLL and native tests, runs CTest, and installs the runtime
files into `bin/`.

## License

Yohsai is licensed under GNU GPL v3.0 or later. Third-party boundaries and
attribution are listed in `THIRD_PARTY_NOTICES.md`.
