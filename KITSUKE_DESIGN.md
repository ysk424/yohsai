# Kitsuke Design

Status: current Sewing, panel-state, and hand-off contract

This document used to describe a cloth solver of Yohsai's own: a square-lattice
runtime behind a Normal GRAVITY button, advanced one click at a time, with its
own seam drag, Body contact, iteration counts and undoable session state. That
solver is gone, and `SOLVER_DESIGN.md` with it. Zero GRAVITY sews with the ZOZO
Contact Solver in a separate process, described in `PPF_ZERO_GRAVITY_DESIGN.md`
and `ppf_zero_gravity.py`.

What is described here is what outlived it: how seams are decided, what a panel's
state means, and what the ZOZO hand-off may and may not do to the garment.

## Purpose

Automatic Sewing records exact cross-panel vertex pairs from where the operator
has placed the panels. It must not infer garment fit, volume, intended
Body-relative placement, or a Body-shaped rest curvature. Deciding those is the
solver's job, from the seams Sewing names.

## Gather sewing

A seam whose two sides were sampled to different vertex counts cannot pair 1:1.
The arc-length walk fans the shorter side across several of the longer side's
vertices, so the seam splays into a ladder that never closes and leaves the
armhole centimetres open. Real garments ease the longer edge (a sleeve cap into
a shorter armhole) by gathering, which needs equal vertex counts so the longer
edge bunches between its matched vertices.

At each Zero GRAVITY press, before Sewing, every seam's two sides are measured.
When they differ, the shorter-side panels are recut so both boundaries carry the
longer side's count:

- The longer side is kept; the shorter side is resampled up to match it.
- A sleeve armhole is a closed ring sewn to the composite of the body front and
  back open chains, so the ring's vertex budget is split across the body panels
  in proportion to their arc lengths.
- Only panels that actually change are recut, so the pass is idempotent: a
  second press with already-matched counts does nothing.

The recut is derived from the parsed pattern stored on the collection, not from
the current mesh, so the authored curves are never modified. The interior grain
lattice is untouched; only the seam boundary densifies and its transition band
re-triangulates. Update restores the original curves, after which the next press
re-adapts. A recut changes topology, so the current pose is transferred onto the
new mesh and Sewing is forced to rebuild on the matched boundaries.

With equal counts the two boundaries pair 1:1 by index (the closed-ring case
rotates and reflects to the best offset first), so the longer edge gathers
between its matched vertices instead of splaying into a ladder.

## The ZOZO hand-off does not touch the garment

Prepare for ZOZO copies the garment as it stands and configures ZOZO to receive
it. It does not sew, open, weld or clear anything.

It used to do all four. ZOZO's add-on pulls a seam shut with a loose stitch
edge, and a loose stitch edge needs a positive contact gap between its ends, so
the hand-off pushed each seam apart into layers first — a graph colouring over
every seam component, a spacing per layer, and a weld for the pinch points at
shoulder and underarm that the layering could not open. That was all scaffolding
for handing over a garment that was not sewn yet, and each piece of it moved
cloth the operator had positioned.

Zero GRAVITY closes the seams before this button is ever pressed, so there is
nothing left to open, and the pinch points it was written for do not survive a
press either — the contact solver resolves the two sides against the Body
instead of dragging them through each other. What ZOZO receives is therefore the
panels' current world positions, their seams as stitch edges, their pattern
coordinates as UVs, and a copy of the Body.

The parts and seams are read the same way Zero GRAVITY reads them: the
participating panels, in panel order, and the verified Sewing plan. Reading them
from a stored solver state instead would hand ZOZO whichever garment that solver
last finished, which is not necessarily the one on screen.

## ZOZO hand-off self-intersection

ppf/ZOZO rejects any shell whose triangles intersect at rest; it only detects,
it ships no resolver. Detection and repair both live in the external
`shell_isect.dll`, driven by `shell_isect_bridge.py` as CHECK 1 → FIX → CHECK 2.
`README.md` states the Prepare pipeline; the module docstring of
`shell_isect_bridge.py` owns the stage contract. Yohsai never edits topology or
body vertices to satisfy the check.

The check that matters is cloth against the Body, because that is what ZOZO
counts and cloth-only cannot see it: on the reference garment cloth × cloth is
0 and cloth × Body is 28. It used to be off by default, because
`shell_isect_check` takes one mesh and finds every self-intersection in it, so
handing it cloth and Body concatenated made it test the Body against itself —
203 s of a 207 s run, for 5943 pairs that ZOZO skips as well. The Body is now
cropped before the call to the triangles whose bounding boxes share a grid cell
with a cloth triangle's, which cannot lose a cloth–Body pair because a triangle
cannot leave its own bounding box. The whole CHECK → FIX → CHECK is 26 s, and
the result was confirmed against the uncropped 449k-triangle Body.

Zero GRAVITY has its own, separate clearing step (`ppf_clear.py`), which runs
inside the solver process on the solver's own copy of the mesh and never on
Blender's. The two are not a pipeline and do not share constants; see
`PPF_ZERO_GRAVITY_DESIGN.md` for why the solver-side one exists at all.

## Panel state, editing and Lock

Load stores each part's initial Object Mode matrix and initializes its monotonic
state as `PLACED`. At a Zero GRAVITY press, a placed part whose Object Mode
matrix has changed becomes `PENDING`. Automatic Sewing uses pending parts as the
new work, retains `DONE` parts as possible sewing anchors, and omits placed
parts. A press clears the independent deformation Lock from all pending parts
before Sewing, so each pending part is deformable. A successful solve changes
every pending participant to `DONE` and does not change its Lock.

State and Lock are separate per-part attributes. Existing Lock is an explicit
lock operation, not a continuously derived policy. Load turns it on and applies
it: placed and done parts become locked, while pending parts become unlocked.
Switching it off unlocks non-placed parts; switching it on applies the same
operation again. Select Lock directly changes the selected parts' single
deformation Lock. Placed parts remain outside the solve regardless of Lock.
Unresolvable paths remain pending. A newly moved part resolves every valid
connection available among the current participants, including one side of a
multipart label whose other side is still placed.

Scaling and vertex-count changes are rejected: a press reads world vertices
through each part's own matrix, and a scaled matrix would hand the solver a
panel whose rest shape is not the pattern's.

Because a press sews from wherever the panels currently are, there is no session
to keep and nothing to undo beyond Blender's own mesh state. Pressing again
re-sews rather than advancing.
