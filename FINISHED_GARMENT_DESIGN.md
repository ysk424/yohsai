# Finished Garment

Status: implemented in Yohsai 0.8.0

## Purpose

`Finished Garment` is Yohsai's general-purpose output. It creates one portable
surface mesh for downstream rigging, weighting, editing, or export. It does not
target ZOZO or any other solver, character format, armature, or platform. The
current garment pose is accepted as authored; deciding whether that pose is a
rest pose belongs to the user.

## Input authority

The selected Clothes collection must have a completed GRAVITY state containing
every current part. The operation consumes the exact ordered parts and sewing
vertex pairs stored by that completed state. It never rebuilds sewing and never
welds vertices merely because they are spatially close. The Body is neither
required nor read.

## Geometry

Part vertices are transformed to world space. A disjoint-set union takes the
transitive closure of the stored sewing pairs, and each resulting seam class is
replaced by the arithmetic mean of its members. Faces are remapped to those
vertices and ordinary surface edges are rebuilt from the faces; no loose sewing
spring edges survive. Unsewn boundaries remain open.

Object transforms are baked and the output object has an identity transform.
Reflected source transforms preserve their authored front side. Shared faces
are oriented consistently without consulting the Body. A source face whose
vertices become identical solely through an exact stored seam junction is
removed and its count is reported; such a face has no remaining surface in the
welded topology. Other zero-area faces, duplicate faces, non-manifold edges or
vertices, inconsistent orientation, and non-finite coordinates cancel the
operation atomically instead of being guessed or silently repaired.

The operation does not add thickness, remesh, subdivide, simplify, resolve
self-intersection, or move cloth relative to the Body. Large seam gaps do not
block output because pose suitability belongs to the user; the maximum weld
movement is reported when it exceeds 10 mm.

## Appearance data

Polygon material assignments and source material slots are retained in stable
part order. Every existing UV map is copied by face corner, so a topological
weld does not join pattern islands. `Yohsai Pattern` is always regenerated from
the authoritative pattern-position attribute. Color attributes are converted
to face-corner values where necessary. Flat/smooth faces, surviving sharp
edges, and valid transformed custom corner normals are preserved; otherwise
normals are recalculated from the final geometry.

The output retains face-domain `panel_index` and `yohsai_source_part`
provenance. Simulation velocity, seam-rest data, ZOZO data, parents,
constraints, modifiers, vertex groups, shape keys, and animation are omitted.

## Snapshot lifetime

Each click creates a new numbered collection and object with roles
`finished_garment_output` and `finished_garment`. Source Yohsai data, the Body,
ZOZO data, and every prior Finished Garment are left unchanged. Failure leaves
no partial data, and Blender Undo removes only the new snapshot.
