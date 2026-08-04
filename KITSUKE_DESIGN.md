# Kitsuke Design

Status: current square-lattice cloth contract

## Purpose

GRAVITY advances the separately positioned pattern parts after its automatic
Sewing phase records exact cross-panel vertex pairs. It must not infer garment fit, volume, intended
Body-relative placement, or a Body-shaped rest curvature.

## Initial state

The first click of a pending stage starts from the current world-space vertices
of the source part objects. The transient Sewing preview supplies connectivity only. Pattern attributes
supply the intrinsic material metric. Body geometry defines neither initial
positions nor material rest state.

## Free-air dynamics

The external free-air inputs are downward gravity and the constant-distance seam
drag. The cloth distributes the seam load using
three Body-independent internal terms:

1. authored rest lengths on all non-proxy material edges;
2. the authored 2D shear metric of each square cell;
3. weak zero-curvature bending along straight warp and weft triples.

The proxy diagonal used to render each square as triangles has no spring. There
is no area-to-Body, normal-to-bone, director, silhouette, or shape-matching term.

## Seam

Every explicit sewing pair has target length zero from runtime creation onward.
Clicks do not shorten, replace, or ratchet this target.

Sewing is not a force. Until capture it is a positional drag applied once per
substep, ahead of the prediction, so it contributes no momentum; see
`SOLVER_DESIGN.md`. The closure is a constant distance and only its direction
follows the endpoint line, so a 50 cm and a 5 cm gap close at the same rate. At
2 mm, or when the endpoints cross during a substep, the pair is captured and
held at zero distance.

## Gather sewing

A seam whose two sides were sampled to different vertex counts cannot pair 1:1.
The arc-length walk fans the shorter side across several of the longer side's
vertices, so the seam splays into a ladder that never closes and leaves the
armhole centimetres open. Real garments ease the longer edge (a sleeve cap into
a shorter armhole) by gathering, which needs equal vertex counts so the longer
edge bunches between its matched vertices.

At each GRAVITY click, before Sewing, every seam's two sides are measured. When
they differ, the shorter-side panels are recut so both boundaries carry the
longer side's count:

- The longer side is kept; the shorter side is resampled up to match it.
- A sleeve armhole is a closed ring sewn to the composite of the body front and
  back open chains, so the ring's vertex budget is split across the body panels
  in proportion to their arc lengths.
- Only panels that actually change are recut, so the pass is idempotent: a
  second click with already-matched counts does nothing.

The recut is derived from the parsed pattern stored on the collection, not from
the current mesh, so the authored curves are never modified. The interior grain
lattice is untouched; only the seam boundary densifies and its transition band
re-triangulates. Update restores the original curves, after which the next click
re-adapts. A recut changes topology, so the current pose is transferred onto the
new mesh and the stale session and persisted state are dropped, which forces a
Sewing rebuild on the matched boundaries.

With equal counts the two boundaries pair 1:1 by index (the closed-ring case
rotates and reflects to the best offset first), so the longer edge gathers
between its matched vertices instead of splaying into a ladder.

## Residual pinch holes

Matched 1:1 gather closes every seam except a few isolated points where the body
sits between the two sides (shoulder, underarm) and holds them apart; these
settle roughly a centimetre open and do not close with more clicks because the
body, not penetration, is the obstacle. At the ZOZO hand-off each residual pair
is excluded from the stitch opening and welded onto whichever endpoint sits
farther outside the body, so the adjacent panel triangles stretch across the
hole without bending the panels or entering the body. Like a real 2 mm stitch
line this is a small, deliberate imperfection, accepted as part of the garment.

## ZOZO hand-off self-intersection

ppf/ZOZO rejects any shell whose triangles intersect at rest; it only detects,
it ships no resolver. Gather sewing leaves excess fabric folded onto itself in
the completed drape, so the hand-off shell can carry intersections that are
inherent to the drape rather than to the seam matching.

Detection and repair both live in the external `shell_isect.dll`, driven by
`shell_isect_bridge.py` as CHECK 1 → FIX → CHECK 2. `README.md` states the
Prepare pipeline; the module docstring of `shell_isect_bridge.py` owns the stage
contract. Yohsai never edits topology or body vertices to satisfy the check.

## Body contact

Body is a fixed collider. Candidate lookup uses the evaluated world-space Body
mesh and a 40 mm search radius. A 5 mm contact thickness is applied only in the
contact path. A penetrating vertex is projected fully onto the thickness shell;
a vertex outside the body but inside the shell is corrected by at most the
thickness per contact pass. Self-contact is absent.

## Runtime control and fixed values

- time step: 1/240 s;
- substeps per click: 8;
- gravity per click: either 0 or 9.81 m/s² in world -Z;
- material/contact iterations: 16 for Normal GRAVITY, 24 (1.5x) for Zero GRAVITY;
- seam closure: 28 mm per substep, applied once as a position change;
- seam capture distance: 2 mm;
- edge stretch relaxation per sweep: 1.0, with two alternating sweeps;
- crimp reserve before an edge becomes a hard wall: 5% of rest length;
- quad shear relaxation per iteration: 0.02;
- axial bend relaxation per iteration: 0.02;
- maximum position correction per projection: 5 mm;
- Body candidate radius: 40 mm;
- Body contact thickness: 5 mm;
- Body contact on penetration: full projection to the thickness shell; outside
  but inside the shell: up to the thickness per contact pass;
- Body contact frequency: every other material iteration, and always the last.

Every value above is `ysc_default_config()` in `native/src/solver.cpp`, except
the iteration count, which the Blender layer passes per click
(`SOLVER_ITERATIONS` / `ZERO_GRAVITY_SOLVER_ITERATIONS` in `kitsuke.py`), and the candidate radius, which is
Blender-side broad phase (`COLLISION_SEARCH_M`).

`Zero gravity` advances with no gravitational acceleration. `Normal gravity`
applies `(0, 0, -9.81)` m/s². Either button can follow the other within the same
live session without resetting positions, velocities, or seam state. The solver
is always the native Square-Lattice Cloth library; there is no solver selection.

There is no sweep phase after the iteration loop. Each iteration runs seam
capture, captured seams, quad shear, axial bend, and two alternating edge
sweeps; Body contact runs on even iterations and the final iteration. Edge
sweeps run last among the material terms so the strong sewing load converges
into the lattice and a stitch vertex cannot run ahead and leave a torn
one-edge spike. OpenMP colouring propagates corrections across the colour
diameter of the lattice, so two alternating sweeps suffice.

## Editing and recovery

Moving or rotating a part between clicks replaces that part's positions and
clears its velocity. Scaling and vertex-count changes are rejected. Lock keeps
seam and material connectivity but prevents the locked vertices from moving.

Load stores each part's initial Object Mode matrix and initializes its monotonic
state as `PLACED`. At a GRAVITY click, a placed part whose Object Mode matrix has
changed becomes `PENDING`. Automatic Sewing uses pending parts as the new work,
retains `DONE` parts as possible sewing anchors, and omits placed parts. A
GRAVITY click clears the independent deformation Lock from all pending parts
before Sewing, so each pending part is deformable. A successful simulation
changes every pending participant to `DONE` and does not change its Lock.

State and Lock are separate per-part attributes. Existing Lock is an explicit
lock operation, not a continuously derived policy. Load turns it on and applies
it: placed and done parts become locked, while pending parts become unlocked.
Switching it off unlocks non-placed parts; switching it on applies the same
operation again. Select Lock directly changes the selected parts' single
deformation Lock. Placed parts remain outside the runtime regardless of Lock.
Unresolvable paths remain pending. A newly moved part resolves every valid
connection available among the current participants, including one side of a
multipart label whose other side is still placed.

Undoable state stores seam pairs, the fixed seam state, velocity, revision,
runtime epoch, and Object Mode matrices. Recovery is limited to the
current add-on runtime. A click is rolled back only if the returned particle
state is non-finite; there is no finite-distance movement threshold.
