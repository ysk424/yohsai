# Yohsai Grainline Mesh Design

Status: current Load mesh contract

The PDF page defines warp vertically and weft horizontally. Load samples a
square lattice per panel (`MESH_SPACING_M` = 10 mm by default in
`mesh_loader.py`), triangulates it for Blender rendering and collision, and
retains pattern coordinates and grainline attributes for material use.

Panels whose shorter pattern-page side is at most
`FINE_MESH_MAX_SHORT_SIDE_M` (5 cm) use `FINE_MESH_SPACING_M` (5 mm) so small
pieces keep enough cells. Large panels stay on 10 mm so tension still
propagates. Mixed-resolution seams equalize to the coarser side's vertex count
before 1:1 pairing (the fine boundary sparsifies; interior stays fine).

Stored attributes include `yohsai_pattern_position`,
`yohsai_grainline_family`, `yohsai_grainline_quad`, sewing membership, and fold
membership. Edge-family values remain proxy, warp, weft, and transition.

Sewing-related free edges also carry a one-layer **10 mm paving band**
(`SEAM_BAND_WIDTH_M`): outer row vertices are kind **E**, the inward companion
row is kind **P**, and the grain interior is kind **N**
(`yohsai_vertex_kind`). Outer sewing-row edges are marked
`yohsai_shortenable` for future gather absorption. See
`SEAM_BOUNDARY_LAYER_DESIGN.md`.

Kitsuke reads non-proxy edge rest lengths for warp/weft stretch, groups the two
proxy triangles back into one square for shear, and derives straight axial
triples for weak bending. The proxy diagonal itself carries no material force.
Body geometry never changes this classification or its rest values.

Update may rebuild the lattice from a revised PDF while preserving object
identity according to the Update contract.
