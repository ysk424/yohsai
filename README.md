# Yohsai 0.1.9

Yohsai is a public, in-development Blender extension for clothing construction.
The API, data shape, and generated output are still experimental.

The planned direction is:

- JSON <=> collections of Curve objects.
- Curve collections -> garment mesh generation.
- Seam construction and stitching.
- Dressing the generated garment onto a character.

This first repository state provides a loadable Blender extension shell, an
N-panel, body selection, and silhouette export.

## Silhouette Export

In the 3D View sidebar, open `Yohsai`, pick a mesh body in the `Body` field
using the object selector or eyedropper, set `Output`, then press `Silhouette`.

The exporter writes two Adobe Illustrator-readable SVG files:

- `<Body>_shadow_xz.svg`
- `<Body>_shadow_yz.svg`

SVG dimensions are written in millimeters. Yohsai converts Blender world units
through the scene unit scale, so the path keeps real size for pattern drafting.

## Pattern SVG Load

The `SVG Path` and `Load` controls start a standalone, standard-library-only
parser in Blender's bundled Python. The Blender UI remains responsive while the
parser converts the exact `CLOTHES` layer into a fixed, atomically replaced JSON
document in Blender's private Yohsai data directory. Blender then expands `@W`
fold panels, creates an approximately 1 cm constrained triangular mesh for each
closed pattern panel, and packs the separate objects into one numbered
`CLOTHES_###` collection at Y = -1 m. Sewing labels and fold edges are retained
as mesh attributes.

After positioning the separate parts, `Sewing` infers each seam's direction from
its endpoints. It preserves the original parts as hidden source objects and
creates one combined `<collection>_SEWN` mesh with loose sewing-spring edges.
This step does not add a Cloth modifier.

## Incremental Kitsuke

After inspecting the `Sewing` preview, select a fixed mesh `Body` and press
`Kitsuke`. Yohsai temporarily reconstructs the sewing constraints and advances
sixteen 1/240-second Taichi cloth steps. Each click shortens the transient seam
targets by 30 mm. Kitsuke defaults to 1.0 m/s² downward acceleration so the user has time
to reposition parts between clicks. Yohsai then removes the combined preview and
restores every pattern panel as a separate object. Move and rotate any one or
more panels in Object Mode, press `Kitsuke` again, and repeat while the seams
close and the garment approaches the body.

Version 0.1.9 temporarily exposes `Gravity` in m/s² and `Seam Pull` in
mm/click in the N-panel for empirical tuning. Changes take effect on the next
Kitsuke click without rebuilding the session.

The pattern topology and its original edge lengths remain authoritative.
Scaling or changing topology during Kitsuke is rejected. Moving or rotating a
part clears that part's velocity; untouched parts retain theirs. The Body is
evaluated once on the first step and remains a fixed collider. Body/cloth and
cloth/cloth contact thickness is 2 mm, while paired seam points target 0 mm.

Taichi chooses an available GPU backend automatically and falls back to the CPU
only when no GPU backend initializes. Version 0.1.9 bundles the CPython 3.13
Windows x64 wheels and is packaged for Windows x64.

The input and JSON contracts are documented in `SVG_TO_JSON_SPEC.md`.
The complete Kitsuke workflow, solver invariants, tuning history, current
parameters, and resume checklist are recorded in `KITSUKE_DESIGN.md`.

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
- `README.md`
- `DEVELOPMENT_NOTES.md`
- `LICENSE`

## License

GNU General Public License v3.0 or later.
