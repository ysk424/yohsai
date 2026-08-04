# Native Kitsuke Solver Design

Status: current native runtime contract

The runtime is a Body-independent square-lattice cloth solver with a version-9
C ABI (`YSC_API_VERSION` in `c_api.h`, matched by `API_VERSION` in
`native_solver.py`; a mismatch refuses to load).

## State and topology

The runtime stores particle position, previous position, velocity, inverse mass,
and Lock state. The creation descriptor also contains:

- constant-distance seam attraction with zero-length capture;
- non-proxy material edges and their authored rest lengths;
- ordered square cells and the authored 2D metric of each cell;
- straight warp/weft triples and their two segment lengths;
- a fixed Body triangle snapshot used only for collision.

All material rest data comes from the loaded pattern. Body vertices, Body
normals, bones, and the current Body silhouette never define cloth rest data.

## Material energy

Warp, weft, and boundary-transition edges preserve their authored lengths in
both directions. Every sweep aims at the rest length; only the firmness varies.
Within `stretch_limit` of rest the pull is `stretch_relaxation`, the weave's own
crimp give; outside it the pull is total. Aiming at the bound rather than at
rest would leave a span just past the reserve stretched further than one just
inside it.

Compression is resisted as firmly as extension. A yarn does not elongate, and
the centimetre between two crossings does not shorten either: cloth folds by
bending the lattice out of plane, with its cells still a centimetre across.
Letting a span collapse instead makes compression a one-way ratchet, because a
span shorter than rest would never be visited again, and the panel silently
loses the dimensions the pattern authored.

Colours already walk sequentially, so one forward pass and one reverse pass per
iteration are enough to reach the middle of a panel and keep the lattice on its
authored spacing under sewing load.

For an ordered quad `(x0, x1, x2, x3)`, the averaged material spans are

```
u = ((x1 - x0) + (x2 - x3)) / 2
v = ((x3 - x0) + (x2 - x1)) / 2
```

The shear term reduces `dot(u, v) - rest_uv`. Edge lengths supply the two axial
metric terms, so the triangulation diagonal is only a rendering proxy and does
not become an artificial spring.

For each collinear warp/weft triple `(a, b, c)`, the weak bending term reduces

```
(xa - xb) / rest_ab + (xc - xb) / rest_bc
```

This expression is zero for a straight material row under any rigid transform.
It contains no preferred Body-shaped arch.

## Substep

Each substep performs:

1. a distance-independent positional seam drag for every uncaptured pair;
2. velocity/position prediction from existing velocity and gravity;
3. seam-capture detection, then iterative captured-seam, quad-shear,
   axial-bend, and two alternating edge sweeps;
4. Body contact correction for supplied candidates on every other material
   iteration (and always the last iteration of the substep);
5. velocity reconstruction from the accepted position change.

Forward and reverse edge sweeps alternate to reduce ordering bias. One edge
sweep already propagates across the colour diameter, so a second reverse pass is
enough. Every local correction is mass weighted and bounded. The uncaptured seam
closure is a fixed distance (`seam_attraction_step`, default 28 mm per substep),
independent of how far apart the pair still is. At 2 mm or after endpoint
crossing, the pair is captured at zero distance. There is no seam-target
shortening, Body attraction, shape matching, self-contact, or speed clamp.

### Colouring and backends

Seams / edges / quads / bends are greedily coloured so each colour class is
vertex-disjoint. Projection walks colours in order (or reverse) and runs
`#pragma omp parallel for` inside a colour. Integrate and contact apply are
per-vertex parallel. Thread count is `OMP_NUM_THREADS`. Colouring is not the
pure serial Gauss-Seidel update order, but different thread counts on the same
coloured schedule must agree; `tests/openmp_colour_smoke.py` checks that.

When built with `YSC_ENABLE_CUDA`, the same colour schedule runs the material
projections on the GPU (`material_cuda.cu`, sm_89 / sm_120). The host keeps
integrate, seam attraction, capture, and Body contact. Auto-select prefers CUDA
at 20k edges or more; `YSC_FORCE_MATERIAL=cuda|cpu` overrides it. Without a
device the solver stays on OpenMP.

Body nearest-face queries run inside the DLL on an AABB BVH built at create time
(`body_bvh.hpp`), selected with `YSC_BODY_CANDIDATES_AUTO`. The advance loop
gathers once per substep and reuses those faces for later contact passes.

Sewing is an operator instruction rather than a force, so it must not become
momentum. The drag is applied ahead of the prediction, which rebases `previous`
onto the dragged position and keeps the pull itself out of the reconstructed
velocity; the endpoints of an uncaptured pair then take zero velocity for that
substep, which keeps the material's reaction to the drag out of it as well.
Admitting one and not the other would make each substep a one-way momentum
source or sink, and the pair would accelerate itself. The drag runs once per
substep, so `iterations` stays a convergence control and does not change how
fast a seam sews shut.

## Contact

Body contact is dissipative only. A contacting vertex retains
`contact_velocity_retention` of its velocity, so contact can remove kinetic
energy but never add any and Body motion cannot fling the cloth. Gravity
re-drives the span every substep, so cloth still creeps over the Body and
settles rather than sticking where it first touched. Vertices that are not
contacting keep their inertia.

## Safety

Inputs and committed state must be finite. Invalid topology and indices are
rejected. The Blender layer rolls back a click only if state becomes non-finite;
finite particle movement has no rollback threshold. Body triangles remain
collision input only.
