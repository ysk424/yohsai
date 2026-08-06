# ZOZO hand-off: what the button checks before the solver ever runs

`ZOZO用準備作業` (Prepare for ZOZO) is not an export. It is the last place
Yohsai can still say why a garment will not solve, in Yohsai's own words, on
Yohsai's own vocabulary. Once the mesh is inside the ZOZO Contact Solver, the
only thing left to report is what the solver saw, and that turns out to be
much less than what Yohsai knew.

The button runs four stages, in this order:

1. **Re-cut** — `remesh_with_seam_counts` on the collection.
2. **Copy** — cloth and Body into a ZOZO-owned collection; Yohsai's own
   objects are never touched.
3. **Self-intersection** — shell-isect check → fix → check.
4. **Triangle quality** — every triangle has rest area the solver can
   integrate.

Only stage 3 existed before. Stages 1 and 4 are here because of one failure,
and the failure is worth writing down in full, because the message it produced
pointed the wrong way at every step.

## The failure

The reference garment reached ZOZO, transferred, ran, and died. The add-on
reported:

```
Solver exited abnormally: pid 35832 stopped after frame 0 without writing a
terminal outcome (segfault / OOM-kill / unrecoverable abort)
```

Every word of that is a guess except the pid and the frame. It comes from
`ppf-cts-server/src/monitor.rs`, the branch the server takes when a status
record exists, no terminal outcome was written, the liveness lock is free and
the owning process is gone. The three causes in the parentheses are a list of
things that can end a process that way; the server did not observe any of
them. On Windows the first and third are possible and the second cannot happen
at all.

It also points away from the diagnosis it names. A real CUDA out-of-memory
goes through `cuda_utils.hpp`, which sets `g_ppf_fatal_code = 2` before
`exit(1)`; the solver's atexit hook reads that and writes `Crashed{Oom}`, and
the operator sees **"Out of GPU or host memory"**. Getting the abrupt message
instead is evidence that memory was *not* the cause.

The cause was in the session directory, which the abrupt branch does not
attach to its message:

```
PPF FATAL: PCG breakdown -- p^T A p is not-a-number at iter 0
  p^T A p (recomputed in double from the same iterate) = nan
```

First Newton step of frame 0. The solver's own parameter summary named the
reason a line further on:

```
area: (max: 6.5724e-5, min: 3.5550e-12, mean: 4.1499e-6)
```

A triangle of 3.555e-12 m² in a garment whose mean is 4.1e-6, and the same
number to five figures came back from measuring the hand-off mesh in Blender.
A shell element's Hessian scales with the inverse of its rest area, so that
one triangle contributes a term around 1e14 into an fp32 device reduction, and
the assembled matrix comes back with a NaN. Every frame after that is a frame
the operator waited for and did not get.

`ppf_remesh` has said this since it was written — "a sliver contributes a term
of order 1e11, and the assembled matrix comes back with a NaN that stops the
very first PCG solve" — and it repairs exactly this. But it runs in the ZOZO
tree's interpreter beside `ppf_driver`, inside the Zero GRAVITY solve. The
hand-off never passed through it, so the one path that knew how to fix the
problem was the one path that did not run.

## Why re-cutting comes first

The sliver was not in the panels this file builds. Re-cutting the same pattern
with the current triangulation gives a smallest rest area of 2.8e-8 m² and a
worst aspect of 1.96e-3, with nothing under 1e-3 — four decades clear of the
triangle that broke the solve. The garment carried its panels from an older
cut, and a .blend outlives the code that filled it.

So the button re-cuts before it copies. What goes over is a panel the current
triangulation produced, not whichever one happened to be in the scene. Seam
counts are matched in the same pass, because a seam whose two sides disagree
on vertex count is the other thing that has to be true before any of this is
worth handing over, and `remesh_with_seam_counts` already does both.

This is a repair by replacement, not a repair in place: a re-cut panel takes
the drape the old one had by pose transfer, and a panel whose topology is
already current is left alone.

## The floor, and where it comes from

Stage 4 refuses a garment whose triangles are too small, measured on the
hand-off mesh after shell-isect (that stage can move cloth, so measuring
before it would measure something else) and triangulated with
`loop_triangles`, which is what the ZOZO encoder itself uses.

The floor is a fraction of one lattice cell, so it means the same thing at any
pitch, and each panel is judged against its own — a 5 mm panel is not held to
a 10 mm panel's cell. The fraction is 1e-6, and it was placed in a gap that
was measured rather than chosen:

| | rest area | of one 10 mm cell |
|---|---|---|
| a cleanly cut panel bottoms out at | 2.8e-8 m² | 2.8e-4 |
| **the floor** | **1e-10 m²** | **1e-6** |
| ZOZO zeroes its bending stiffness under | 1e-12 m² | 1e-8 |
| the triangle that took the solve to NaN | 3.555e-12 m² | 3.6e-8 |

A hundred times above the point where ZOZO itself stops trusting the element,
a hundred times below the worst a good panel produces. Nothing has to be
judged in the space between.

Run against the garment that failed, the gate names two faces out of 21,956 —
3.555e-12 m² and 7.728e-12 m², the two that were there — and passes the rest.

The refusal is a soft stop: the status box says what was found and no MCP
configuration happens. It never raises, because Blender surfaces an uncaught
exception as レポート:エラー and this is a finding about the garment, not a
fault in the add-on.

## What this does not do

**It does not repair a sliver.** A triangle 0.11 um across cannot be fixed by
moving a vertex — reaching the lattice's own aspect would mean moving it about
2,700 times further than it sits from the opposite edge, which is a change to
the panel, not a correction to it. The repairs that work are topological, and
topology is what Yohsai holds by index: seam pairs, pattern coordinates,
grainline attributes, the sewn result already written into those vertices. So
the gate reports and stops, and re-cutting is what actually produces a mesh
that passes.

**It does not make the panel builder better.** The builder's worst aspect is
still 1.96e-3 where the lattice reaches 3.13e-2, and
`PPF_ZERO_GRAVITY_DESIGN.md` still names that as the thing most worth fixing.
The gate sits far below it and will not fire on it. What the gate removes is
the class of failure where the operator learns about the mesh from a solver
five minutes later, in a message about memory that was never about memory.

**It does not check the Body.** The hand-off Body is a ZOZO STATIC collider,
which in ZOZO's vocabulary means it carries no degrees of freedom, not that it
holds still; an animated collider is captured per frame and driven
kinematically, and that is a supported and ordinary thing for it to be.
Nothing in stage 4 looks at it.
