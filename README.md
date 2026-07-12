# Yohsai 0.2.6

Yohsai is a public, in-development Blender extension for clothing construction.
The API, data shape, and generated output are still experimental.

The Illustrator pattern is authoritative; Blender meshes are replaceable
physical realizations of that pattern. The normal Yohsai workflow is
intentionally concentrated into four operations:
`Load`, `Update`, `Sewing`, and `Kitsuke`.

## N-panel workflow

The top of the Yohsai N-panel contains all three inputs:

- `Pattern Path`: the current Illustrator PDF;
- `Clothes`: the loaded Yohsai clothes collection;
- `Body`: the fixed collision mesh used by Kitsuke.

Below Body, `Lock` marks the currently selected mesh object(s) as excluded from
Kitsuke deformation. The adjacent `Auto` button is reserved for a later
automatic selection workflow. Below them are `Load`, `Update`, `Sewing`, and
`Kitsuke`, in workflow order.
Gravity and seam pull use the tested Yohsai defaults and are no longer exposed
as N-panel debugging controls.

## Silhouette preparation utility

Silhouette export is character preparation, normally run only once per
character, so it is not part of the Yohsai N-panel. Select the character mesh
and run `UTIL/silhouette_export.py` from Blender's Scripting workspace. It
writes `<Body>_shadow_xz.svg` and `<Body>_shadow_yz.svg` for Illustrator.

SVG dimensions are written in millimeters. Yohsai converts Blender world units
through the scene unit scale, so the path keeps real size for pattern drafting.
Complete instructions are in `UTIL/README.md`.

## Pattern PDF Load

The `Pattern Path` and `Load` controls accept Adobe Illustrator PDF. Yohsai
emits closed paths that contain a unique `#` panel label as panels. Unlabeled
artwork is not emitted, subject to the containment limitation recorded in
`SVG_TO_JSON_SPEC.md`. SVG input is no longer supported.

The standalone parser runs asynchronously with Blender's bundled Python and
writes a fixed, atomically replaced JSON document in Blender's private Yohsai
data directory. PDF parsing uses the bundled `pypdf` and `typing_extensions`
wheels. Blender then expands `@W`
fold panels, creates an approximately 1 cm constrained triangular mesh for each
closed pattern panel, and packs the separate objects into one numbered
`CLOTHES_###` collection at Y = -1 m. Sewing labels and fold edges are retained
as mesh attributes.

After positioning the separate parts, `Sewing` infers each seam's direction from
its endpoints. It preserves the original parts as hidden source objects and
creates one combined `<collection>_SEWN` mesh with loose sewing-spring edges.
This step does not add a Cloth modifier.

## Pattern Update

Place one unique `#` text label inside every closed pattern panel, for example
`#FRONT01` or `#BACK-BODICE`. Label whitespace is removed, ASCII letter case is
ignored, and digits, underscore, and hyphen are supported. After changing and
saving the same Illustrator PDF, press `Update` instead of Load.

Update recuts every labeled panel into new mesh data and transfers the old
garment's current 3D pose as an initial placement. Existing panel objects,
materials, transforms, and collection ownership remain. New flat-pattern
coordinates become the authoritative stretch and bend rest state, and velocity
is reset. The operation is atomic: label, panel-count, parsing, triangulation,
or transfer failures leave the existing garment unchanged.

If authored sewing membership is unchanged, press Kitsuke directly. If it
changed, Kitsuke reports `Sewing required`; inspect a new Sewing preview before
continuing.

## Incremental Kitsuke

After inspecting the `Sewing` preview, select a fixed mesh `Body` and press
`Kitsuke`. Yohsai constructs a transient Taichi session and advances sixteen
1/240-second cloth steps. Each click shortens the transient seam maximum
distance by 30 mm, and every simulation cycle ratchets that maximum down to the
current seam distance when the seam gets shorter. Seam projection runs after
Body and self-contact so conflict is resolved by cloth distortion or Body
penetration before seam separation. Kitsuke defaults to 1.0 m/s² downward acceleration so the user has time
to reposition parts between clicks. Yohsai then removes the combined preview and
restores every pattern panel as a separate object. Move and rotate any one or
more panels in Object Mode, press `Kitsuke` again, and repeat while the seams
close and the garment approaches the body.

Selecting mesh object(s) and enabling `Lock` keeps those objects in the sewing
graph but removes their vertices from Kitsuke deformation. Lock is exclusive:
checking it replaces the previous locked set with the current selection, while
unchecking it clears the selected Clothes lock state. Locked parts can therefore
anchor or reserve sewn pieces while other parts continue dressing.

The pattern topology and its original edge lengths remain authoritative.
Scaling and vertex-count changes during Kitsuke are rejected. Other direct mesh
edits are unsupported but are not yet completely detected; edit topology only
in the pattern. Moving or rotating a part clears that part's velocity; untouched
parts retain theirs. The Body is evaluated once for a live session and remains
a fixed collider. Body/cloth and cloth/cloth contact thickness is 2 mm, while
paired seam points progressively approach 0 mm.

Kitsuke supports Blender Undo and Redo. Each successful click stores its exact
seam vertex pairs and targets, per-vertex velocities, revision, and Object Mode
transforms in undoable Blender data. After Undo or Redo, the non-undoable Taichi runtime is
discarded and rebuilt from the restored Blender state before the next click.
Opening the file in a new Blender/add-on runtime intentionally ignores that
recovery state. Continuing an abandoned, partially dressed session across a
restart is not supported; begin again from Load/Sewing when required.

Taichi chooses an available GPU backend automatically and falls back to the CPU
only when no GPU backend initializes. Version 0.2.6 bundles the CPython 3.13
Windows x64 wheels and is packaged for Windows x64.

The input and JSON contracts are documented in `SVG_TO_JSON_SPEC.md`.
The complete Kitsuke workflow, solver invariants, tuning history, current
parameters, and resume checklist are recorded in `KITSUKE_DESIGN.md`.
The pattern-designer viewpoint that governs Update, Sewing, Kitsuke, annotation
design, and future automation is recorded in `DESIGN_PHILOSOPHY.md`.
The current resume handoff and deliberately deferred issues are recorded in
`SESSION_MEMORY.md`.

## Package

The extension manifest is `blender_manifest.toml`. The source package contains:

- `__init__.py`
- `ui.py`
- `kitsuke.py`
- `mesh_loader.py`
- `yohsai_svg_parser.py`
- `yohsai_defaults.json`
- `SVG_TO_JSON_SPEC.md`
- `KITSUKE_DESIGN.md`
- `DESIGN_PHILOSOPHY.md`
- `README.md`
- `DEVELOPMENT_NOTES.md`
- `SESSION_MEMORY.md`
- `UTIL/silhouette_export.py`
- `UTIL/README.md`
- `LICENSE`

## License

GNU General Public License v3.0 or later.
