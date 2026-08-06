# Yohsai Grainline Mesh Design

Status: current Load mesh contract

The PDF page defines warp vertically and weft horizontally. Load samples a
square lattice per panel (`MESH_SPACING_M` = 5 mm in `mesh_loader.py`),
triangulates it for Blender rendering and collision, and retains pattern
coordinates and grainline attributes for material use.

Every panel is cut at that one pitch. It used to be 10 mm, with 5 mm kept for
panels whose shorter pattern-page side fell under 5 cm, and the reason for
holding the large panels coarse was a solver Yohsai no longer has. The reason
for going fine is under the arm: two panels have to slide past each other there,
and a 10 mm facet stands too far off the surface it approximates for them to
pass. The standoff is the chord's sagitta and falls with the pitch squared.

On the reference pattern the change costs 8,485 vertices -> 30,826, and the
triangles come out no worse: worst aspect 3.3e-4 -> 1.1e-3, smallest rest area
88x above the ZOZO floor (it was 161x), nothing degenerate. The shortest edge
does drop, 121 um -> 22 um, from `delaunay_2d_cdt` intersection vertices rather
than from the lattice; `_lattice_minimum` records why that is left alone.

Mixed-resolution seams are still handled — a garment cut before this change
keeps its 10 mm panels until it is re-cut, and `part_spacing_m` answers with the
pitch a part actually has. Such seams equalize to the coarser side's vertex
count before 1:1 pairing (the fine boundary sparsifies; interior stays fine).
Prepare for ZOZO refuses a garment still on the old pitch rather than converting
half of it on the way out; Update then GRAVITY is the repair.

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
