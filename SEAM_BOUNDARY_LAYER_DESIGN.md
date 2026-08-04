# Seam Boundary Layer (Paving Band) Design

Status: partially implemented. Load builds the band and tags vertices; nothing
consumes the tags yet.

Related: `GRAINLINE_DESIGN.md`, `KITSUKE_DESIGN.md`.

## Purpose

ZOZO Contact Solver (ppf) needs a shell that sews with matched stitch topology,
carries a uniform seam-allowance band, and keeps the interior grain lattice
intact for material metrics. A one-layer paving strip on sewing edges, plus a
three-way vertex classification, supplies that structure without remeshing whole
panels.

Yohsai must **not** insert patch faces into gaps between sewn panels. Topology
stays multi-island panels joined by loose stitch edges.

## Geometry

For each sewing-relevant boundary chain on a panel, Load offsets the outline
inward by the band width and connects the two rows as a single strip:

```
   E0——E1——E2——E3——E4——E5     Edge vertices (outer stitch row)
   |   |   |   |   |   |
   P0——P1——P2——P3——P4——P5     Proximity vertices (band)
   |   |   |   ...
   N   N   N   ...            Normal vertices (panel interior)
```

The region inside the offset is filled with the existing grain lattice.

## Vertex kinds

Point attribute `yohsai_vertex_kind`: **0 = N**, **1 = E**, **2 = P**. P and N
stay distinct; do not merge proximity into normal. When welding merges vertices
of different kinds, E wins over P, and P over N.

| Kind | Role | Shortenable incident edges |
|------|------|----------------------------|
| **E** | Outer sewing row; 1:1 stitch partners | Outer E–E along the sewing row: **yes** |
| **P** | Inner band row; never a stitch endpoint | Width spoke E–P: **no** |
| **N** | Grain interior | Per existing material rules |

## Implemented

- `SEAM_BAND_WIDTH_M = 0.01` (10 mm) in `mesh_loader.py`;
- one-layer proximity companions on sewing-related outline vertices;
- `yohsai_vertex_kind` on points;
- `yohsai_shortenable` on the outer sewing row (E–E only; width spokes and the
  inner P–P row are not shortenable).

Stitch-count matching itself is already handled elsewhere: each GRAVITY click
recuts the shorter side of a seam so both boundaries pair 1:1. See the gather
contract in `KITSUKE_DESIGN.md`.

## Not implemented

- **Clearances A and B.** Intent: seam band (E and P) targets near-contact
  distance A from the body; interior N stands off at B with A < B, so bulk cloth
  is less likely to dig in under a crude local sim. Neither value is chosen and
  nothing enforces the ordering.
- **Ease vs stitch split on E.** On a long-to-short gather the surplus outer
  samples are not stitch partners and must not be pushed to clearance A — that
  re-creates the sleeve crumple. They belong on the air side. Until a `stitch-E`
  / `ease-E` label exists, both are stored as kind E and nothing distinguishes
  them.
- **Shortenable in GRAVITY.** The attribute is written but no solver term reads
  it, so gather is not yet absorbed on the stitch line.

## Open decisions

- Final band width (10 mm now; 5 mm was discussed as a product target).
- Final A and B in metres, and soft potential vs hard projection.
- Corner offset policy (miter vs round).
- Whether a multi-layer P chain is ever needed (default: one P row).
- Interaction with RING composite open/closed sewing alignment.
- Whether A/B applies only at hand-off or continuously in GRAVITY.

## Acceptance

1. Partner sewing chains expose the same number of stitch-role E vertices with
   stable pairing.
2. Band width is visually uniform.
3. E, P, and N are distinguishable in data.
4. On a long-to-short gather, surplus outer samples read as air-side fullness,
   not crushed into the body.
5. A reference Prepare shows fewer seam-local self-intersections than the
   pre-band mesh.

Global remeshing (Q-morph, Blossom-Quad via Gmsh) is not the product path:
neither carries a grain contract and both are too heavy to embed. The product
path is local one-layer paving.
