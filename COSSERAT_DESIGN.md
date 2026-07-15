# Native Kitsuke Solver Design

Status: current native runtime contract

The DLL name retains `cosserat` for package compatibility. The active runtime is
a Body-independent square-lattice cloth solver with a version-7 C ABI.

## State and topology

The runtime stores particle position, previous position, velocity, inverse mass,
and Lock state. The creation descriptor also contains:

- constant-magnitude seam attraction with zero-length capture;
- non-proxy material edges and their authored rest lengths;
- ordered square cells and the authored 2D metric of each cell;
- straight warp/weft triples and their two segment lengths;
- a fixed Body triangle snapshot used only for collision.

All material rest data comes from the loaded pattern. Body vertices, Body
normals, bones, and the current Body silhouette never define cloth rest data.

## Material energy

Warp, weft, and boundary-transition edges preserve their authored lengths. For
an ordered quad `(x0, x1, x2, x3)`, the averaged material spans are

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

1. a distance-independent seam impulse for every uncaptured pair;
2. velocity/position prediction from existing velocity and gravity;
3. seam-capture detection, then iterative captured-seam, quad-shear,
   axial-bend, and two-way edge sweeps;
4. Body contact correction for supplied candidates;
5. velocity reconstruction from the accepted position change.

Forward and reverse edge sweeps alternate to reduce ordering bias. Every local
correction is mass weighted and bounded. Uncaptured seam-force magnitude is
independent of pair distance. At 2 mm or after endpoint crossing, the pair is
captured at zero distance. There is no seam-target shortening, Body attraction,
shape matching, self-contact, or speed clamp.

Each substep ends with extra alternating material-edge and captured-seam sweeps.
Their purpose is to distribute the strong stitch load into the cloth instead of
allowing a seam vertex to stretch one neighboring edge into a tear-like spike.

## Safety

Inputs and committed state must be finite. Invalid topology and indices are
rejected. The Blender layer rolls back a click only if state becomes non-finite;
finite particle movement has no rollback threshold. Body triangles remain
collision input only.
