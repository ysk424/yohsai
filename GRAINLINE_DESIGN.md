# Yohsai Grainline Design

Status: v0.5.0 grain-aligned quad-lattice milestone implemented and
integration-tested
Recorded: 2026-07-14 (Asia/Tokyo)

## 1. Authoritative garment convention

The project owner is an expert garment maker and pattern drafter. Product and
material decisions supplied by the owner are authoritative domain input, not
beginner assumptions that the software should reinterpret.

Every pattern is drafted with its grain aligned to the vertical direction of
the pattern paper. The warp direction runs from the top of the page to the
bottom. Yohsai Grainline therefore uses this fixed material convention:

- pattern-page vertical is warp;
- pattern-page horizontal is weft;
- no per-panel grain annotation or automatic grain inference is required;
- the first implementation does not treat bias cutting as a normal workflow.

The simulation lattice must preserve this authored page orientation. Object
placement in Blender changes the garment pose, not its material grain.

## 2. Fork objective

The validated Stable Cosserat v0.4.1 implementation is retained as the
behavioral and numerical baseline. The new solver will replace its internal
three-direction triangular rod network with a structured square lattice in
pattern space:

- vertical lattice edges form warp Cosserat chains;
- horizontal lattice edges form weft Cosserat chains;
- quad cells supply explicit in-plane shear and area response;
- triangulated faces remain a collision, Blender, and rendering proxy;
- proxy diagonals are not structural Cosserat segments.

The interior should remain a regular square grid. Arbitrary authored panel
boundaries may require clipped boundary cells or a narrow quad-dominant
transition, but boundary treatment must not rotate or reinterpret the material
grain.

## 3. Product invariants retained from Yohsai Cosserat

The Illustrator PDF pattern remains authoritative. `Load`, `Update`, `Sewing`,
manual Object Mode placement, Lock, incremental Kitsuke, Undo/Redo, progressive
seam closure, Body contact, and self-contact remain in scope. The first
grainline milestone changes the internal material discretization rather than
the established garment-construction workflow.

The existing `yohsai-cosserat` repository remains the trusted triangular
baseline for A/B comparison. This repository carries its complete history and
continues development under GPL-3.0-or-later.

## 4. Implemented mesh mapping

Load and Update sample interior points at integer multiples of the 5 mm pitch
in global pattern-page coordinates. They do not offset or rotate a grid for an
individual panel. The constrained Delaunay result remains the Blender surface.

After triangulation (and after RING welding), Yohsai recognizes a material cell
only when all four page-aligned corners, all four sides, and exactly one of the
two possible proxy diagonals form two valid triangles. Its corners are ordered
bottom-left, bottom-right, top-right, top-left in material space. The two faces
receive the same `yohsai_grainline_quad` integer. Mesh edges receive
`yohsai_grainline_family`:

- `0`: proxy diagonal, excluded from the material rod graph;
- `1`: vertical warp segment;
- `2`: horizontal weft segment;
- `3`: structural boundary/transition segment.

Complete square cells therefore have four structural sides and one
non-structural rendering/collision diagonal. Non-square cut-boundary triangles
remain a narrow structural transition instead of rotating the interior grain.
Self-contact exclusions use every proxy-topology edge, including the diagonal,
so adjacent triangles cannot repel one another merely because that diagonal is
not a rod.

The fixed `test2.pdf` fixture contains 16,948 vertices, 33,018 proxy triangles,
15,102 quads, 15,626 warp edges, 15,365 weft edges, 3,871 transition edges, and
448 sewing constraints. Its linear pitch is still 5 mm. The former staggered
triangular lattice had 19,454 vertices at the same pitch because its area per
sample was smaller.

## 5. Quad material model

For an ordered cell with current corners `x0..x3`, the averaged weft and warp
tangents are

```text
u = 0.5 ((x1 - x0) + (x2 - x3))
v = 0.5 ((x3 - x0) + (x2 - x1))
```

The flat pattern supplies `u0` and `v0`, independently of the non-flat RING
director frame. The dimensionless constraints are

```text
C_shear = dot(u, v) / (|u0| |v0|) - dot(u0, v0) / (|u0| |v0|)
C_area  = |cross(u, v)| / |cross(u0, v0)| - 1
```

For local corner `i`, the coefficients of `u` are
`(-0.5, 0.5, 0.5, -0.5)` and those of `v` are
`(-0.5, -0.5, 0.5, 0.5)`. Each position sweep adds the corresponding shear and
area energy gradients and positive Gauss-Newton scalar Hessians to the existing
vertex-block VBD update. Both default stiffness densities are `2.0e5`; each
cell's effective weight is multiplied by its rest area. Structural segment
stretch remains `2.0e6`, and Stable Cosserat bending remains `2.0e-4`.

Proxy diagonals contribute neither stretch rods nor Cosserat orientations. The
warp/weft/transition segments continue to use the Stable Cosserat closed-form
orientation update and mutually opposite continuation pairing. This preserves
the accepted v0.4 rod behavior while removing the unphysical third material
direction from complete interior cells.

## 6. Native and Blender state boundary

The native C ABI is version 2. Its create descriptor adds flat material rest
positions and ordered quad connectivity; configuration adds shear and area
stiffness; counts and statistics expose quad count plus shear and area energy.
The rendering faces remain triangles for Blender and collision.

Only structural edge quaternions cross the native boundary and participate in
Undo recovery. Their values are stored in the existing edge-domain quaternion
attributes; proxy entries are written as identity values. Reconstructing an
Undo state filters the same ordered structural-edge set and reconstructs quads
from their paired face attributes and authoritative pattern coordinates.

RING construction keeps a separate non-degenerate tube geometry for director
initialization. Quad shear and area always use the flat page-space pattern,
which retains the authored warp/weft material frame even after the seam is
welded into a tube.

## 7. v0.5.0 acceptance record

The implemented milestone passed:

1. native API v2, rest-state, rigid-transform, shear-restoration,
   area-restoration, seam, lock, gravity, Body, and self-contact tests;
2. the host-Python `ctypes` state and orientation bridge tests;
3. exact `test2.pdf` density, quad pairing, proxy-diagonal exclusion, and
   warp/weft axis checks;
4. Load, Sewing, ten consecutive Stable Cosserat clicks, Object Mode placement,
   Update, and sewing-signature invalidation without rollback;
5. mirrored RING sleeve Load, Update, Sewing, and a finite native solve;
6. Undo/Redo reconstruction with zero difference in positions, velocities,
   orientations, seam targets, structural edges, all proxy edges, edge rest
   lengths, and quad connectivity.

On the development machine, the density-only Load/Sewing fixture took 4.878 s
and the broad source integration suite took 288.065 s. These are regression
observations, not cross-machine performance guarantees. The native material
solver is still single-threaded.

## 8. Deliberate limits

- Clipped boundary cells are not generalized quads; they stay in the
  grain-preserving triangular transition strip.
- The area constraint uses magnitude and is not by itself an inversion barrier.
- Contact remains the accepted discrete point-triangle implementation; this
  milestone does not claim CCD or IPC.
- Legacy Taichi remains available for recovery and comparison, but it does not
  implement the new native quad shear/area energies.
- Parallel VBD coloring, SIMD, and GPU execution are future performance work.

## 9. Triangular versus grainline cost record

The following comparison uses the same `test2.pdf`, 5 mm nominal pitch,
Blender 5.2 build, 16 iterations, and development machine. The native material
microbenchmark passes empty contact candidate arrays, zero gravity, and zero
seam closure so it measures the C++ material/seam loop rather than Python broad
phase or Blender scattering. Four advances were run; the reported value is the
median of the last three.

| Metric | v0.4.1 triangular rods | v0.5.0 grain quads | Change |
|---|---:|---:|---:|
| Material pitch | 5 mm | 5 mm | same linear resolution |
| Vertices | 19,454 | 16,948 | -12.9% |
| Render/collision triangles | 38,030 | 33,018 | -13.2% |
| Render/collision topology edges | 57,482 | 49,964 | -13.1% |
| Structural Cosserat segments | 57,482 | 34,862 | -39.4% |
| Cosserat continuation joints | 55,715 | 32,956 | -40.8% |
| Explicit shear/area cells | 0 | 15,102 | added |
| Native session construction | 0.471 s | 0.540 s | +14.7% |
| Native material advance | 0.595 s | 0.558 s | -6.3% / 1.07x |
| Broad source integration record | 388.210 s | 288.065 s | -25.8% / 1.35x |

The full integration figures are useful regression observations but are not as
controlled as the microbenchmark: they include pattern work, Python collision
candidates, Blender updates, and different development commits.

The small native advantage despite 39% fewer segments is expected. A triangular
position sweep performs 114,964 segment-endpoint visits. The grainline sweep
performs 69,724 segment-endpoint visits plus 60,408 quad-corner visits, or
130,132 local visits; each quad visit also evaluates dot and cross products for
both shear and area. The gain comes primarily from the 40% smaller orientation
and continuation graph, while quad arithmetic spends much of that saving.

At equal pitch the square lattice has fewer vertices because a staggered
triangular lattice packs samples more densely. A square pitch of approximately
4.67 mm would bring v0.5 to the v0.4.1 vertex count and provide about 7% finer
linear sampling. A simple count-proportional extrapolation predicts about
0.640 s per native advance at that density, roughly 7.6% slower than the
5 mm triangular baseline. This is an estimate, not a measured result.

The main grainline optimization opportunities are:

1. store `(quad, corner)` directly instead of searching four corners on every
   quad-vertex visit;
2. replace vector-of-vectors adjacency with compact CSR/SoA storage;
3. evaluate each cell's tangents, shear, area, and gradients once instead of
   recomputing them independently at all four corners, then use a buffered
   reduction or a validated colored sweep;
4. parallelize quad work with four cell/vertex colors, or per-thread
   accumulation and reduction (two-color checkerboarding is insufficient
   because diagonal vertices share a quad);
5. parallelize warp and weft orientation chains independently and apply SIMD;
6. move or parallelize the Python collision broad phase, which is outside the
   fast native microbenchmark and benefits directly from 13% fewer proxy
   vertices/faces.

The triangular solver also remains parallelizable and has the simpler local
constraint, so the square representation is not automatically faster at equal
vertex count. Its principal advantage is material correctness and a regular
warp/weft structure; its current speed advantage at equal 5 mm pitch is real
but modest. Keeping `yohsai-cosserat` as the unchanged triangular baseline
provides the appropriate A/B reference while this decision is evaluated.
