# Yohsai Grainline Mesh Design

Status: current Load mesh contract

The PDF page defines warp vertically and weft horizontally. Load samples a
global 5 mm square lattice, triangulates it for Blender rendering and collision,
and retains pattern coordinates and grainline attributes for material use.

Stored attributes include `yohsai_pattern_position`,
`yohsai_grainline_family`, `yohsai_grainline_quad`, sewing membership, and fold
membership. Edge-family values remain proxy, warp, weft, and transition.

Kitsuke reads non-proxy edge rest lengths for warp/weft stretch, groups the two
proxy triangles back into one square for shear, and derives straight axial
triples for weak bending. The proxy diagonal itself carries no material force.
Body geometry never changes this classification or its rest values.

Update may rebuild the lattice from a revised PDF while preserving object
identity according to the Update contract.
