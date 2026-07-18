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

The external free-air inputs are downward gravity and constant-magnitude seam
attraction. The cloth distributes the seam load using
three Body-independent internal terms:

1. authored rest lengths on all non-proxy material edges;
2. the authored 2D shear metric of each square cell;
3. weak zero-curvature bending along straight warp and weft triples.

The proxy diagonal used to render each square as triangles has no spring. There
is no area-to-Body, normal-to-bone, director, silhouette, or shape-matching term.

## Seam

Every explicit sewing pair has target length zero from runtime creation onward.
Clicks do not shorten, replace, or ratchet this target. Until capture, the force
magnitude is constant and only its direction follows the endpoint line; a 50 cm
and a 5 cm gap therefore receive the same force. At 2 mm, or when the endpoints
cross during a substep, the pair is captured and held at zero distance.

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

## ZOZO hand-off self-intersection resolution

ppf/ZOZO rejects any shell whose edge pierces another triangle of the same mesh
at rest ("Cannot create session: ... self-intersections"); it only detects, it
ships no resolver.  Gather sewing leaves the excess fabric folded onto itself in
the completed drape -- at the shoulders (the eased sleeve cap), the bust, and the
sides -- so the hand-off shell carries hundreds of self-intersections that are
inherent to the drape, not to the seam matching.  `prepare_for_zozo` resolves
them on the cloth object, right after it is built, before handing off.

Pushing intersecting triangles apart does not work: separating one fold cascades
into new intersections and never converges.  Instead each fold is unfolded by
smoothing its neighbourhood:

1. Detect the hits and take the union of their vertices.  ppf uses exact
   predicates (edge-vs-triangle plus a coplanar fallback), which a float ray
   test cannot mirror exactly -- a strict BVH and a 1 mm-inflated BVH do not
   even nest.  So run three tests and union them: a face-vs-face overlap on the
   inflated tree (cheap, catches the deep gather folds), and an
   **edge-vs-triangle** ray test over every mesh edge on **both** the strict and
   the inflated tree.  The edge test is the one that catches seam and
   panel-boundary edges (including the loose stitch edges) piercing a triangle
   with no face overlapping.  The 1 mm clearance stays below the >=2.2 mm ZOZO
   stitch openings, so the intentional loose stitches are never flagged, and it
   gives ppf's exact test headroom.  Edges that share a vertex with the
   triangle are excluded.
2. Expand the hit vertices by **two rings** along the mesh -- solve the region
   around each hit, not just the hit.
3. Strongly Laplacian-smooth that region (0.6 toward the neighbour average, four
   sub-iterations) to flatten the crumple.
4. Repeat until no intersection remains (capped at 40 passes).

The folds sit outside the body, so flattening them does not drive cloth inward;
as a fallback any vertex still inside the body afterwards is clamped back onto
its surface plus the body clearance.  ppf then re-sews the loose stitches and
re-drapes the smoothed region with contact, which re-forms the gathers without
re-creating the self-intersection.

The resolved mesh is handed off **already triangulated**.  The cloth still
carries grain quads, and ppf triangulates any quad with its own diagonal, which
can re-introduce an intersection the resolver already cleared on the opposite
diagonal.  Persisting the resolver's triangulation gives ppf exactly the
triangles that were verified intersection-free.

On the reference garment this took the hand-off shell from 526 self-intersections
to 0 in under a second, with no body penetration and the 1:1 seams intact.  The
smoothing is method one; the body clamp is the fallback method two for any fold
that a purely tangential smoothing could not lift clear.

## Body contact

Body is a fixed collider. Candidate lookup uses the evaluated world-space Body
mesh and a 40 mm search radius. A 5 mm contact thickness is applied only in the
contact path. Each contact correction is capped at 0.20 mm per iteration so an
initial penetration is removed gradually rather than becoming an impulse.
Self-contact is absent.

## Runtime control and fixed values

- time step: 1/240 s;
- substeps per click: 8;
- gravity per click: either 0 or 9.81 m/s² in world -Z;
- material/contact iterations: fixed at 20;
- seam attraction: 300 force units (300 m/s² at unit inverse mass);
- seam capture distance: 2 mm;
- edge stretch relaxation per sweep: 1.0, with four alternating sweeps;
- quad shear relaxation per iteration: 0.02;
- axial bend relaxation per iteration: 0.0001;
- Body candidate radius: 40 mm;
- Body contact thickness: 5 mm.

`Zero gravity` advances with no gravitational acceleration. `Normal gravity`
applies `(0, 0, -9.81)` m/s². Either button can follow the other within the same
live session without resetting positions, velocities, or seam state. The solver
is always the native CPU Square-Lattice Cloth backend.

After the coupled material iterations, additional alternating edge/seam sweeps
converge the strong sewing load into the lattice so a stitch vertex cannot run
ahead and leave a torn one-edge spike.

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

State and Lock are separate per-part attributes. Auto is an explicit lock
operation, not a continuously derived policy. Load turns Auto on and applies it:
placed and done parts become locked, while pending parts become unlocked.
Switching Auto off unlocks non-placed parts; switching it on applies the same
Auto-lock operation again. `Lock` directly changes the selected parts' single
deformation Lock. Placed parts remain outside the runtime regardless of Lock.
Unresolvable paths remain pending. A newly moved part resolves every valid
connection available among the current participants, including one side of a
multipart label whose other side is still placed.

Undoable state stores seam pairs, the fixed seam state, velocity, revision,
backend, runtime epoch, and Object Mode matrices. Recovery is limited to the
current add-on runtime. A click is rolled back only if the returned particle
state is non-finite; there is no finite-distance movement threshold.
