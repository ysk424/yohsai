# Zero GRAVITY: sewing with the ZOZO Contact Solver

Status: in progress. The front and back panels sew into a wearable dress. Every
seam of the reference garment resolves into springs -- body, armholes, neckline
and the three RING seams. The press that attaches the sleeves was refused at
scene build; what refused it has been measured, and it was neither the sleeve
nor the solver, but this add-on replacing the garment's mesh on the way over.
It no longer does: the solve runs on the mesh Blender has, and reading the
result back is a lookup.

Zero GRAVITY closes every seam of a garment. It is the one step where Yohsai's
own square-lattice solver could not be made faster without being made worse:
reaching a seam by positional projection ties its stiffness to the iteration
count, so buying speed costs correctness. The contact solver closes a seam with
an implicit force solved inside its Newton step, so the result is the converged
one at any step count. What it costs instead is wall clock — a press is a job of
tens of seconds, not a button that answers within a frame.

That trade turned out to be worth making everywhere. As of 0.13.0 the
square-lattice solver is gone: sewing finishes inside Yohsai rather than being
exported, so the ZOZO hand-off no longer opens, welds or clears anything and
passes the garment on as it stands.

## Why it is affordable

Two promises Yohsai can make about its own state, both of which the hand-off
gave up.

**The Body never moves**, so it goes over as a static collider: no degrees of
freedom, uploaded to the device once. The hand-off instead sent the same
225,184-vertex Body as a dynamic mesh re-pinned every frame from 1.35 GB of pin
targets, and paid elastic assembly for 449,472 triangles that never move. That
is the difference between 62 ms and 12–66 seconds per advance.

**The panels are cut flat**, so the scene starts free of intersection, which is
the state the solver requires. The hand-off sent already-draped cloth and was
rejected for 588 self-intersections — the failure the 40-pass
`_resolve_self_intersections` in `zozo_handoff.py` exists to fight.

The second promise is the one this add-on has to keep on its own, and it is
kept by handing the solver the cloth Blender holds rather than a copy of it.
The rest is below.

## Shape

`ppf_zero_gravity.py` runs inside Blender: it gathers the panels, their seams,
the Body and the settings, and hands them to `ppf_driver.py` as an `.npz`.
`ppf_driver.py` runs in the ZOZO tree's own interpreter as a child process, so
the CUDA backend, its Rust cdylib and its numpy stay out of Blender: a solver
crash costs the click, not the session. `ppf_weld.py` runs there too and may
use scipy; it must never be imported from Blender.

Vertex order cannot be assumed. `Scene.build` renumbers vertices, so every
read-back goes through the per-object `local -> global` map the build returns.

## What the settings are for

**Sew, then settle.** Damping wants opposite things from the two. Air drag
strong enough to bring a garment to rest in free space also overpowers the seam
force and leaves the seams open; no damping at all closes them but leaves the
cloth drifting, so the frame the job stops on decides the answer. Six undamped
frames close the seams, five damped ones bring the result to rest.

**`stitch-length-factor`, not stiffness.** The seam force saturates at
`stitch_length_factor * l0`, and `l0` is half the contact gap — under a
millimetre. At the stock factor of 10 a pair a garment's width apart pulls no
harder than one already touching. Panels 292 mm apart closed to 211 mm in eight
frames at the stock value and to 2.1 mm in six at 100. Raising stiffness instead
slightly worsens closure, because it scales an already-saturated force, and
raising the time step does nothing because CCD sets the pace: a requested dt of
0.05 ran at 0.0058.

Measured on the reference garment: 292 mm of seam to 2.28 mm mean, last frame
moving 0.013 mm, 42 seconds, no intersections.

## RING is a seam, and a sleeve is a C

RING used to weld a sleeve's two edges together while the mesh was built, so the
panel came out a closed tube. That put the mesh builder in the business of
sewing, and it is why sleeves could not be handed to the solver: a tube is not a
simple region of its own pattern — it is a cylinder, whose boundary is the two
open ends rather than the outline the cloth was cut to — so nothing working in
pattern coordinates has a domain. Welding is also what left vertices sharing one
pattern coordinate.

Now the two chains take one sewing group and pair 1:1, and a sewing group may
occur twice on one part, meaning that part closes onto itself. The sleeve is
built as a C so an arm still goes in. Cloth does not stretch to allow that, so
the curve widens rather than the cloth opening: `radius * 2*pi - gap ==
circumference` keeps the arc exactly the pattern's width.

A RING seam names the part it closes — `RING_SODE:LEFT`, not a pattern letter —
because two sleeves must not pair with each other. Labels are therefore whatever
follows `sewing_` on the mesh, not one character; reading only one character is
what made the sleeve seam invisible to everything downstream of the mesh
builder.

## A ring left open is still a ring

Cutting the sleeve as a C took away something the weld used to say for free.
An armhole and a cuff are rings, and the code that sews them says so through
`_SeamChain.closed`: a closed sleeve armhole tells the seam-count equaliser what
vertex budget the body armhole has to match, tells `_multipart_closed_pairs` to
align a loop against a loop, and tells the body-only step that the front and
back armholes are partners of the sleeve rather than of each other. An open
strip of cloth says none of that, so opening the sleeve broke all three at once
— including panels the sleeve is not sewn to, whose armholes got recut from a
budget that no longer existed, and the front and back armholes then sewed to
each other and collided with the shoulder and side seams.

So `_self_closing_partners` reads the part's own RING seam and matches its two
edges' ends. Any chain running between a matched pair is closed by that seam, and
is marked closed with a zero-length join — the same virtual join a composite body
loop already uses, because after sewing the two ends are one point. This asserts
nothing: the seam that closes the ring is a seam the part carries. The collar is
a RING panel too, so its neckline closes the same way.

## What the sleeve failure was

Releasing the sleeves and pressing again was refused at scene build with
`frontend._scene_.ValidationError: 343 self-intersections (343 tri-tri)`. A
sleeve at that moment is a cut panel, and a cut panel cannot intersect itself,
so the count was measured pair by pair with the same tri-tri checker the ZOZO
hand-off uses. Rebuilding the scene by hand gives 338 of them, and they are not
what the message suggests:

| | cloth × cloth | cloth × Body |
| --- | ---: | ---: |
| the cloth Blender holds | 4 | 52 |
| after `ppf_remesh` rebuilt it | 0 | **338** |

Every counted pair is cloth against the Body. 306 of the 338 are on the front
panel — which had **none** before the rebuild touched it — and 32 on the back.
Neither sleeve contributes a single pair. The press was refused for the two
panels it was not even for.

## A barrier solver only promises its own mesh

`ppf_remesh` kept a panel's outline and replaced its interior with a fresh
lattice, reading each new point off the original triangles. That reads a point
that is *on* the surface, so on a flat panel it is exact to the last bit — and a
panel is flat exactly once, when it is loaded. Sewing curves it. The new
triangles span the old ones, and across curvature a chord cuts inside the
surface it replaces; on cloth already resting against the Body, inside the
surface is inside the Body. The contact gap the solver leaves is under a
millimetre, so it takes very little chord to cross it.

That is the specific bug, but the general one outlives it: **the solver
guarantees the mesh it was given, and that is not the mesh Blender keeps.** Two
triangulations of the same vertices are two different surfaces between them, so
"no intersection in the solve" does not imply "no intersection in Blender"
whenever the two differ at all. Blender's own contribution is not in it: storing
a metre-scale coordinate as float32 and putting it back through the object
matrix moves a vertex by 0.14 um at the worst, against a contact gap 3600 times
larger, and the solver's own `vert_*.bin` is float32 already.

The obvious answer is to stop replacing the mesh and hand over Yohsai's own.
That does not work, and it is worth writing down why, because the reason is not
the one the code assumed. What makes the garment's mesh unusable looked like a
needle of 3.75e-12 m² against a 5.00e-05 m² median — a shell element's Hessian
scales with the inverse of its rest area, so that contributes a term of order
1e11 and the first PCG solve returns a NaN. Every such face is one artifact:
two vertices at the same pattern coordinate, one place on the cloth recorded
twice, with a needle stretched between them. Four vertices account for all eight
bad faces, and welding them removes the NaN outright:

| | smallest face | shortest edge | worst aspect | faces under 0.01 |
| --- | ---: | ---: | ---: | ---: |
| Yohsai's own triangulation | 3.75e-12 m² | 0.12 um | — | — |
| the same, coincident vertices welded | 2.80e-08 m² | 121 um | 2.06e-03 | 6 |
| a Delaunay over the same points | 4.75e-08 m² | 121 um | 2.06e-03 | 4 |
| the rebuilt lattice | 8.43e-07 m² | 474 um | **3.13e-02** | **0** |

The NaN goes and the solve still does not run. It stops instead in the strain
limiter, at `SL_toi: 0` — a needle four thousandths as wide as it is long
reaches any strain limit for almost any motion — three Newton steps into the
first frame of a staged press, and seven into a press from the cut layout where
the lattice ran 1107. Re-triangulating does not help either: a Delaunay over the
same points has the *same* worst aspect, because the limit is where the vertices
are, not which edges join them. Yohsai's own point set cannot be triangulated
into something this solver can advance.

So the rebuild stays, and what it does wrong is undone instead:

**The scene is cleared before it is built.** `ppf_clear` asks the same checker
which triangles the rebuild pushed into the Body and lifts those vertices back
out along the Body's own normal, to 1.5 mm. On the reference garment that is 36
of 8780 vertices, 1.51 mm at the worst, in one pass — and the scene then builds
`clean (0 self-intersections, 0 contact-offset)`.

**The read-back stays as it was**, and that is worth recording, because the
obvious improvement is worse. Interpolating solved positions writes the
resampling error into the .blend whether the solve moved anything or not: a
no-op solve comes back 0.20 mm out at the median and 2.73 mm at the worst.
Reading the *motion* instead — interpolating where the solve started as well as
where it ended, and moving the original cloth by the difference — cancels that
exactly, and a press that moves no cloth then returns the cloth unchanged bit
for bit. Measured on the same press, it is far worse:

| what Blender receives, same press | cloth × cloth | cloth × Body |
| --- | ---: | ---: |
| interpolating positions | 0 | **14** |
| carrying motion | 29 | **1239** |

The solve rests the clean surface against the Body at the contact gap, so
whatever the original mesh does differently from that surface pokes straight
through it. Interpolating positions hands back the surface the solver actually
guaranteed. Carrying motion hands back a surface nobody guaranteed anything
about, and the 0.20 mm it saves is not worth the 1239.

With the clearing, the press that attaches the sleeves runs: 11 frames in 32
seconds, sleeve seams from 55.44 mm to 3.56 mm mean, body seams held at 2.19 mm,
last frame moving 0.012 mm, and the cloth Blender receives goes from 56 tri-tri
pairs to 14.

## What is not finished

**The garment's own triangulation is still the thing that wants fixing.**
`ppf_remesh` exists because the solve cannot run on the garment's mesh, and
`ppf_clear` exists because `ppf_remesh` moves cloth. Fix the triangulation where
it is produced — a panel meshed with no needle and no coincident pair — and both
go, along with the read-back's 0.20 mm and every question about whether the mesh
the solver promises is the mesh Blender has. The measurements above say what
such a mesh has to clear: worst aspect at the 3e-02 the lattice reaches, not the
2e-03 the current vertices allow.

**The read-back still resamples.** 14 pairs is not 0, and each press hands back
a copy of the operator's cloth rather than their cloth. The clearing means a
press no longer trips over what the last one left, so it no longer compounds
into a refusal, but it is a treadmill rather than a fix.

**Clearing edits the rest shape.** The vertices `ppf_clear` lifts are lifted
before the solve, so the cloth is told it was cut with a 1.5 mm bump there. It
is small against a seam about to move tens of millimetres, and it is confined to
cloth the rebuild had already displaced, but it is not nothing and it is not
undone afterwards.

**Damage already in a .blend stays in it.** The 4 + 52 pairs the old read-back
wrote into the reference garment are the operator's cloth now. A press no longer
trips over them — the scene the solver sees is cleared every time — but nothing
repairs the .blend itself.

**A press from the cut layout does not converge for a four-panel garment.**
Sewing the reference garment from where its panels are laid out — seams 1.2 to
1.6 m apart, panels floating above the Body — builds a clean scene and then
fails inside the first frame: CCD gives up after 1107 Newton steps and 5.2
minutes with `toi: 0`. Two panels at 0.3 m closed in 42 seconds, so what breaks
is the distance, not the count. Staging a garment — sew the body, then release
the sleeves and press again with the seams already 46 to 68 mm apart — is what
the pipeline is actually good at, and it is why the mesh had to stop being
replaced on the way over.
