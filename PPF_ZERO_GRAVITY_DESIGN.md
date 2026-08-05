# Zero GRAVITY: sewing with the ZOZO Contact Solver

Status: in progress. Every seam of the reference garment now resolves into
springs -- body, armholes, neckline and the three RING seams. Reading the solved
cloth back is what is still wrong.

Zero GRAVITY closes every seam of a garment. It is the one step where the
square-lattice solver could not be made faster without being made worse:
reaching a seam by positional projection ties its stiffness to the iteration
count, so buying speed costs correctness. The contact solver closes a seam with
an implicit force solved inside its Newton step, so the result is the converged
one at any step count. What it costs instead is wall clock — a press is a job of
tens of seconds, not a button that answers within a frame — so Normal GRAVITY
and Kitsuke keep the square-lattice solver untouched.

Succeeding here removes the ZOZO hand-off entirely: sewing finishes inside
Yohsai instead of being exported, and the intersection-clearing that the
hand-off fights goes with it.

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

## Shape

`ppf_zero_gravity.py` runs inside Blender: it gathers the panels, their seams,
the Body and the settings, and hands them to `ppf_driver.py` as an `.npz`.
`ppf_driver.py` runs in the ZOZO tree's own interpreter as a child process, so
the CUDA backend, its Rust cdylib and its numpy stay out of Blender: a solver
crash costs the click, not the session. `ppf_remesh.py` runs there too and may
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

## What is not finished

**`ppf_remesh.transfer` is wrong.** Locating an original vertex in a rebuilt
panel returns 0.156 mm of error at the median and kilometres at the worst.
Welded tubes were the suspected cause and the weld is now gone, but that is
unproven. Until it is, `ppf_zero_gravity` discards any result that moves cloth
further than the Body and leaves the cloth untouched.

`ppf_remesh` exists because Yohsai's panel triangulation leaves needle slivers
in the interior — the shortest interior edge measures 0.169 um against a 10 mm
median — and a shell element's Hessian scales with the inverse of its rest area,
so a sliver contributes a term of order 1e11 and the first PCG solve returns
NaN. The square-lattice solver never notices, because it reads its metric from
the authored pattern and skips degenerate edges. If the triangulation is fixed
where it is produced, `ppf_remesh` should be deleted rather than repaired.
