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
- `Body`: the fixed collision mesh used by Kitsuke;
- `Gravity (-Z m/s²)`: downward acceleration for the next Kitsuke click.

The normal operation order is:

1. `Load` creates one Mesh object per pattern panel instance.
2. Translate and rotate the separate parts in Object Mode.
3. `Sewing` validates sewing paths and creates a connectivity preview.
4. Select the Body and press `Kitsuke` to advance a short simulation.
5. Continue alternating Object Mode placement and Kitsuke clicks.
6. Use `Update` after editing the same Illustrator PDF.

`Lock` excludes selected part objects from Kitsuke deformation while retaining
their sewing connectivity.

## Pattern input

The parser accepts a one-page Illustrator PDF. Each closed panel must contain a
unique `#` label. Page vertical is warp and page horizontal is weft.

Supported annotations are:

- a single letter for a sewing group;
- `@W` for a fold edge;
- `@M` for authored-left and mirrored-right instances;
- two `RING` edges plus `@TOP` for a welded ring construction.

No undocumented annotation has implied behavior.

Load samples a 5 mm square material lattice in pattern-page coordinates and
uses triangles as the Blender and collision proxy. Pattern coordinates retain
the material rest state.

## Sewing

Sewing uses the world-space positions of the separate source parts. It orders
marked boundary paths, matches them by normalized authored distance, and stores
cross-panel pairs as loose preview edges.

The preview is a visual connectivity record. It does not define a replacement
initial cloth shape. Body geometry is not used by Sewing.

## Kitsuke

Kitsuke starts from the positioned source-panel vertices. The default backend is
the native CPU square-lattice cloth solver.

Each click applies:

- existing velocity and downward gravity;
- distance-independent attraction and zero-distance capture on explicit seam pairs;
- authored material-edge stretch, square-cell shear, and weak axial bending;
- Body contact correction.

Pattern attributes define every material rest value. The render-triangulation
diagonal carries no spring, and no material term reads Body shape, normals, or
bones. The Body may influence particles only through collision contact.

The default values are an eight-substep click, a 1/240-second substep,
16 material/contact iterations, 1.0 m/s² gravity, constant-magnitude seam
attraction with 2 mm capture, and 5 mm contact thickness. Gravity is read from
the N-panel on every click: a positive value acts in world -Z, and zero disables
gravity. It may be changed without resetting the live simulation.

After a click, positions are scattered back to the separate part objects. Object
translation and rotation are supported between clicks; scaling and vertex-count
changes are rejected. Finite movement is not capped or rolled back; rollback is
reserved for a non-finite solver state.

Undo and Redo store the solver state needed to reconstruct the live session
inside the same add-on runtime. Continuing a partially dressed session after
restarting Blender is unsupported.

## Update

Update rereads the same PDF and recuts the selected Clothes collection. Stable
`#` labels and mirror instances identify corresponding parts. Existing object
identity, transforms, materials, and collection ownership remain.

If sewing membership changes, Sewing must be run again. Pattern topology and
material rest dimensions always come from the revised PDF.

## Silhouette utility

Character silhouettes are exported separately with
`UTIL/silhouette_export.py`. See `UTIL/README.md`.

## Documentation

- `SVG_TO_JSON_SPEC.md`: input, JSON, Load, Sewing, Kitsuke, and Update contract;
- `DESIGN_PHILOSOPHY.md`: product-level interpretation rules;
- `KITSUKE_DESIGN.md`: current simulation workflow and invariants;
- `COSSERAT_DESIGN.md`: native particle solver and compatibility boundary;
- `GRAINLINE_DESIGN.md`: grain-aligned mesh and material mapping;
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
