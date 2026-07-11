# Yohsai Development Notes

Status: public development preview.

Current version: 0.1.9.

The authoritative Kitsuke design, tuning log, known limitations, and resume
checklist are maintained in `KITSUKE_DESIGN.md`.

The first real-character 0.1.9 trial produces a recognizable fitted dress from
the incremental workflow. Remaining visible holes at the shoulder, chest, and
abdomen make Body contact refinement the next primary engineering task.

## Version and Build Policy

The final numeric component of the extension version is the build number. It
must be incremented before every extension package build. The version in
`blender_manifest.toml`, the documented current version, and the generated
archive name must agree.

Existing project documentation and specifications must remain in the repository
when a new build is produced. Amend or supersede them explicitly instead of
silently removing the current record.

This repository is public-facing but still under active development. Interfaces,
data formats, silhouette output details, and packaging may change before a stable
release.

## Direction

Yohsai will own clothing creation. The intended pipeline is:

1. Describe garment data in JSON.
2. Read and write Curve object collections from that JSON.
3. Convert Curve collections into mesh panels.
4. Stitch mesh panels into garments.
5. Fit or dress the garment onto a character.

The first month should stay focused on data shape, coordinate conventions,
Curve object ownership, and a minimal reproducible panel-to-mesh path before
adding sewing or fitting behavior.

## Current State

- The add-on registers a `Yohsai` N-panel in the 3D View sidebar.
- `SVG Path` and `Load` run a standard-library-only SVG parser in a separate
  process and asynchronously load its fixed, atomically written JSON result.
- Load expands fold-cut panels and creates one packed, cloth-ready triangular
  Mesh object per closed panel in a new numbered `CLOTHES_###` collection.
  Sewing and fold membership are preserved as mesh attributes.
- `Sewing` orders each marked boundary path, infers its direction from the
  positioned world-space endpoints, and creates a combined mesh with loose
  sewing-spring edges. The separate source parts remain hidden in the same
  collection for future update work.
- `Kitsuke` treats the separate panel objects as the editable source of truth.
  Each click reconstructs transient Taichi stretch, bend, sewing, body-contact,
  and self-contact constraints, advances a fixed short interval, and scatters
  the result back to the original objects. Object-mode translation and rotation
  are accepted between clicks and clear velocity only on the moved parts.
  Sewing targets close by 30 mm per click, gravity defaults to 1.0 m/s², and
  each click uses sixteen 1/240-second substeps. Bend stiffness is reduced from
  0.18 to 0.08, and maximum velocity is 1.0 m/s. Body contact uses one nearest triangle
  per cloth vertex, collision corrections are averaged, and unstable steps are
  rolled back before Blender mesh data is changed.
  Gravity and seam closure are temporarily exposed in the N-panel for repeated
  empirical tuning and are read again on every click.
- SVG parsing currently covers the exact `CLOTHES` layer, closed line/cubic
  paths, meter scaling through `@S<number>cm`, single-letter sewing groups, and
  `@W` fold edges. The contract is documented in `SVG_TO_JSON_SPEC.md`.
- The panel owns a mesh body pointer, with Blender's object selector/eyedropper.
- `Silhouette` exports XZ and YZ orthographic silhouette shadows as SVG files
  readable by Adobe Illustrator.
- No Blender Cloth modifier is used. Kitsuke sessions are intentionally
  in-memory; reopening a partially dressed file restarts with zero velocity.
