# Yohsai Session Memory

Recorded: 2026-07-16 (Asia/Tokyo)

## Current contract

- Sewing supplies exact seam connectivity only.
- Kitsuke starts from positioned source-panel vertices.
- Seam targets are fixed at zero and never shorten per click.
- Before 2 mm capture, seam closure is a fixed distance per substep, independent
  of how far apart the pair still is.
- Sewing is an operator instruction, not a force. The drag runs once per substep
  ahead of the prediction, and the endpoints of an uncaptured pair take zero
  velocity for that substep, so neither the drag nor the material's reaction to
  it becomes momentum.
- Pattern rest lengths connect ordinary vertices mechanically, in both
  directions: a span resists compression exactly as firmly as extension.
- Square cells use their authored 2D shear metric; proxy diagonals carry no force.
- Warp and weft are stiff while shear is soft. That is what makes grain and bias
  behave differently, and it is why a split yoke can read as a split yoke.
- Straight warp/weft triples provide zero-curvature bending. With compression
  resisted, bending is what sets the fold scale, so `bend_relaxation` is a
  material knob and not a stability tweak.
- Body contact is dissipative only: a contacting vertex keeps
  `contact_velocity_retention` of its velocity, so contact can remove kinetic
  energy but never add any. Non-contacting cloth keeps its inertia.
- No material term reads Body geometry, Body normals, or bones.
- Body geometry enters only through collision candidate lookup and contact.
- Self-contact and Body-relative shape matching are absent.
- Gravity is a per-click N-panel input: positive values act in world -Z, default
  1.0 m/s², and zero disables gravity without resetting live state.
- Finite per-click movement has no rollback threshold; only non-finite state is
  rolled back.

## Interpretation rule

Implement only explicitly requested behavior. Never infer garment shape, fit,
volume, or Body-relative placement.

## What 0.5.11 got wrong, and how it was found

0.5.11 fixed a real defect — sewing injected a velocity impulse of thirty times
gravity — and introduced two regressions while doing it. Neither test suite
caught either one. Both were found only by measuring the live scene.

- **Compression was left unresisted**, on the reasoning that cloth buckles into
  wrinkles rather than resisting in-plane. That is true of the sheet and false of
  a span: the centimetre between two crossings does not shorten, because cloth
  folds by bending the lattice out of plane with its cells still a centimetre
  across. Because the edge projection skipped any span shorter than rest,
  compression became a one-way ratchet no later pass could undo, and spans
  reached -99% of their authored length.
- **The repeated edge sweeps were removed** as a workaround for the old impulse.
  They were not a workaround. A Gauss-Seidel pass carries a length correction
  only about one span further into the sheet, so one pass per iteration never
  reached the middle of a panel — the part furthest from any anchor — and the
  lattice grew under load instead of settling.

Together these made the solver diverge: a quarter of all material spans sat
outside the crimp reserve, the worst at twice its rest length, and each further
click made it worse rather than better.

The lesson worth keeping: a suite that asserts the shipped behaviour cannot see a
regression in the shipped behaviour. Measuring strain against the authored rest
length in the real scene is the check that works, and the mesh already carries
everything needed — `yohsai_pattern_edge_rest` per edge, and
`yohsai_grainline_family` to separate warp, weft, and transition spans from
rendering proxies. Edge length alone cannot do it: a sheared cell's diagonal
lands in the same range as a warp span.

## Verification

Native and Blender tests cover fixed seam targets, distance-independent seam
closure at 50 cm and 5 cm, 2 mm capture, rigid-transform/rest invariance, edge
load transmission, quad shear reduction, axial bend reduction,
Body-candidate-only contact, per-click gravity changes including 1→10 and 0→10,
Undo/Redo reconstruction, and full pattern data.

Convergence is covered by none of them and must be measured. A 24x24 lattice of
1 cm cells hung from its top row, at the shipped 16 iterations, holds every span
inside the crimp reserve, peaks at +0.47%, and is flat from the third click
onward. That lattice has no seams and no Body contact, so it bounds the material
terms only; the garment scene remains the real check.

## Release

Current release: `yohsai-0.5.12.zip` (85,724,666 bytes).

SHA-256: `6F298B843524B1819A1BC5D00B8E706383C7358FEE8F33CE54430F28AE5C6AF8`.

The archive contains 40 entries and its bundled native DLL matches
`bin/yohsai_cosserat.dll`. Keep current source, manifest, dependencies, native
binaries, licenses, and current documentation. Exclude build output, caches,
temporary parser output, local PDFs, and older archives from future ZIPs.

Roughly 83 of the 86 MB is the bundled Taichi wheel, which exists only for the
`TAICHI` Kitsuke backend. That backend still carries the pre-0.5.11 seam impulse
and the old `BEND_RELAXATION = 0.0001`, so selecting it restores the behaviour
0.5.11 and 0.5.12 removed. It is a second solver to keep in step, and 97% of the
download.
