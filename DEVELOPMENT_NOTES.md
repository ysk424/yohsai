# Yohsai Development Notes

Status: public development preview.

Current version: 0.2.0.

The authoritative Kitsuke design, tuning log, known limitations, and resume
checklist are maintained in `KITSUKE_DESIGN.md`.
The product-level pattern-designer perspective and anti-drift rules are
authoritative in `DESIGN_PHILOSOPHY.md`.
The latest tested handoff, deferred issues, release procedure, and next priority
are summarized in `SESSION_MEMORY.md`.

The first real-character 0.1.9 trial produces a recognizable fitted dress from
the incremental workflow. Remaining visible holes at the shoulder, chest, and
abdomen make Body contact refinement the next primary engineering task.

## Version and Build Policy

Extension versions use `MAJOR.MINOR.PATCH`. A feature milestone may increment
MINOR and reset PATCH, as 0.2.0 does for Undo/Redo synchronization; subsequent
compatible fixes increment PATCH. Every distributed build must have a version
not previously distributed. The version in `blender_manifest.toml`, the
documented current version, and the generated archive name must agree.

Existing project documentation and specifications must remain in the repository
when a new build is produced. Amend or supersede them explicitly instead of
silently removing the current record.

This repository is public-facing but still under active development. Interfaces,
data formats, utility output details, and packaging may change before a stable
release.

## Direction

The Illustrator PDF/SVG pattern and its annotations are the source of truth.
The implemented pipeline parses that pattern to fixed JSON, cuts separate
triangular panel meshes, verifies Sewing, and incrementally dresses them with
Kitsuke. Update recuts labeled panels while transferring only a useful initial
3D placement. Future work must extend this pattern-designer workflow instead of
treating Blender mesh identity as authoritative.

## Current State

- The add-on registers a `Yohsai` N-panel in the 3D View sidebar.
- The N-panel groups Pattern Path, Clothes, and Body at the top, followed only
  by the four primary actions: Load, Update, Sewing, and Kitsuke.
- `Pattern Path` and `Load` accept PDF or SVG, run the parser in a separate
  process, and asynchronously load its fixed, atomically written JSON result.
  PDF is the preferred Illustrator interchange and uses bundled `pypdf` and
  `typing_extensions` wheels; SVG remains a compatibility input.
- Load expands fold-cut panels and creates one packed, cloth-ready triangular
  Mesh object per closed panel in a new numbered `CLOTHES_###` collection.
  Sewing and fold membership are preserved as mesh attributes.
- `#` text inside a panel provides a normalized, human-authored identity for
  Update. Whitespace is removed; ASCII letters compare case-insensitively; and
  digits, underscore, and hyphen are accepted.
- `Update` asynchronously rereads the same PDF or SVG, recuts all labeled panels,
  transfers the current 3D pose through stored flat-pattern coordinates, and
  atomically swaps new Mesh datablocks into the existing objects. New pattern
  geometry owns rest lengths and velocities reset to zero.
- Unchanged sewing signatures remain verified across Update. Changed sewing
  membership invalidates verification, and Kitsuke reports `Sewing required`
  until the independent Sewing operation succeeds again.
- `Sewing` orders each marked boundary path, infers its direction from the
  positioned world-space endpoints, and creates a combined mesh with loose
  sewing-spring edges. The separate source parts remain hidden in the same
  collection for future update work.
- `Kitsuke` treats the separate panel objects as the editable 3D realization.
  The first click constructs transient Taichi stretch, bend, sewing,
  body-contact, and self-contact state; later clicks reuse it while synchronizing
  supported Object Mode placement. Each click advances a fixed short interval
  and scatters the result back to the original objects. Translation and rotation
  clear velocity only on the moved parts.
  Sewing targets close by 30 mm per click, gravity defaults to 1.0 m/s², and
  each click uses sixteen 1/240-second substeps. Bend stiffness is reduced from
  0.18 to 0.08, and maximum velocity is 1.0 m/s. Body contact uses one nearest triangle
  per cloth vertex, collision corrections are averaged, and unstable steps are
  rolled back before Blender mesh data is changed.
  Gravity and seam closure use the tested fixed defaults and are intentionally
  absent from the production N-panel.
- Each successful Kitsuke click mirrors the non-undoable runtime's exact seam
  pairs and targets, per-vertex velocities, revision, and transforms into
  Blender data. Undo/Redo handlers discard the stale Taichi objects; the next click reconstructs them
  from the state restored by Blender. A new Blender/add-on runtime ignores the
  old recovery epoch. Cross-restart continuation of a partially dressed state
  is unsupported; the supported workflow starts again from Load/Sewing.
- PDF parsing emits explicitly closed line/cubic paths containing unique `#`
  labels. Unlabeled closed artwork still participates in containment testing,
  so it must not enclose a labeled panel in the current implementation. PDF
  page points supply physical scale;
  `@S<number>cm` remains required as pattern metadata. SVG parsing covers the
  exact `CLOTHES` layer, closed line/cubic
  paths, meter scaling through `@S<number>cm`, single-letter sewing groups, and
  `@W` fold edges. The contract is documented in `SVG_TO_JSON_SPEC.md`.
- The panel owns a mesh body pointer, with Blender's object selector/eyedropper.
- `UTIL/silhouette_export.py` is a standalone `bpy` preparation script for the
  Scripting workspace. It exports XZ and YZ orthographic silhouette SVGs and is
  normally run once per character; it is not registered as an add-on operator.
- No Blender Cloth modifier is used. The active Taichi runtime is in memory;
  Blender data contains only same-runtime Undo/Redo recovery state. Reopening
  and continuing a partially dressed session is unsupported.

## Known implementation gaps intentionally deferred

- Same-vertex-count topology edits and direct vertex edits are unsupported but
  are not yet reliably rejected. Pattern topology must be changed in Illustrator.
- PDF close-and-paint operators and implicit fill closure are not yet treated as
  explicit closed panels; current Illustrator test data uses an explicit close.
- The intended annotation contract is ASCII-only and requires positive PDF
  `@S` metadata, but validation still has known Unicode case-folding and
  non-positive PDF scale gaps. Do not depend on those accidental acceptances.
