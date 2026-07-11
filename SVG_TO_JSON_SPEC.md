# Yohsai SVG-to-JSON Specification

Status: initial implementation specification
Version: 1.0.0

## 1. Purpose

Yohsai converts garment patterns authored in Adobe Illustrator from SVG into a
single machine-readable JSON document. The converter is a standalone process.
Blender starts it with an SVG path, waits asynchronously for it to finish, and
then reads the resulting JSON. The converter does not import Blender modules or
share memory with Blender.

The standalone conversion ends when valid JSON has been produced. Blender then
creates an initial flat garment mesh from that JSON. Darts, notches, grain lines,
and advanced sewing rules are outside this schema version. Sections 11 and 12
define Blender operations that consume the preserved sewing metadata.

## 2. Process contract

- Program name: `yohsai_svg_parser.py`.
- Runtime: the Python interpreter bundled with Blender, in a separate process.
- Dependencies: Python standard library only.
- Command line: `python yohsai_svg_parser.py <absolute-svg-path>`.
- The SVG path is the only positional argument.
- Working directory: Yohsai's private user data directory.
- Fixed result name: `yohsai_pattern.json`.
- Temporary result name: `yohsai_pattern.json.tmp`.
- A successful conversion writes the temporary file completely and atomically
  replaces the fixed result.
- A failed conversion returns a non-zero exit code and does not replace an
  existing valid result.
- Diagnostics are written to standard error.
- Exit code `0` means success; `1` means invalid input or conversion failure;
  `2` means invalid command-line usage.

Blender must not block its UI while conversion runs. It polls the child process,
loads the JSON only after exit code `0`, and reports parser errors to the user.

## 3. SVG input profile

### 3.1 Data layer

Only the SVG group whose exact `id` is `CLOTHES` contains pattern data. All
content outside that group is ignored. A missing layer, more than one matching
layer, or a misspelling such as `CLITHES` is an error.

Nested groups in `CLOTHES` are allowed. SVG transforms inherited from those
groups and transforms on supported elements are applied before measurements and
output.

### 3.2 Supported geometry

The initial profile supports SVG `path` elements composed of:

- `M`/`m` move commands;
- `L`/`l`, `H`/`h`, and `V`/`v` straight segments;
- `C`/`c` cubic Bezier segments;
- `S`/`s` smooth cubic Bezier segments;
- `Z`/`z` close commands.

Straight segments are retained as straight segments. Cubic curves are retained
as their exact endpoints and control points; they are not permanently converted
to a sampled polyline. This lets Blender choose mesh resolution later without
discarding the authored curve.

Quadratic curves, elliptical arcs, and SVG geometry elements other than `path`
are unsupported in `CLOTHES` and cause an error. An unsupported non-empty data
element must never be silently ignored.

Each closed subpath is one pattern panel. Multiple closed subpaths in one SVG
path become separate panels. Open subpaths are not panels; in version 1 they may
only serve as scale-reference lines.

### 3.3 Text annotations

Annotation matching is case-insensitive and ignores surrounding whitespace.
The initial profile recognizes:

- `@S<number>cm`: scale reference, for example `@S99cm`;
- `@W`: a fold/symmetry marker;
- a single ASCII letter `A` through `Z`: a sewing-group marker.

Text may use `text`/`tspan` nesting. Its transformed SVG text origin is the
annotation position. Empty Illustrator helper text is ignored. Any other
non-empty text inside `CLOTHES` is an error.

## 4. Scale

Exactly one scale annotation is required. The numeric value must be finite and
greater than zero.

The scale reference is the nearest open path to the annotation. Exactly one
open path must exist in the initial profile; absence or ambiguity is an error.
The complete geometric length of that open path represents the annotated length.
The scale path is metadata and is not emitted as a panel.

All JSON coordinates and lengths use meters. The conversion factor is:

`meters_per_svg_unit = annotation_centimeters / 100 / scale_path_svg_length`

## 5. Coordinates

- Output X increases to the right.
- Output Y increases upward, so SVG Y coordinates are negated after applying
  SVG transforms.
- All output coordinates are meters.
- No panel is translated or packed by the parser.
- Relative placement between panels in the SVG is preserved.

## 6. Annotation association

### 6.1 Sewing groups

Each single-letter marker is associated with the geometrically nearest segment
of a closed panel. Every segment bearing the same case-normalized letter belongs
to the same sewing group. A group may contain separated segments, including
future dart-like arrangements.

Version 1 records group membership only. It does not infer pairing order,
stitching direction, easing, or compatibility of segment lengths. An exact
distance tie is an ambiguity error.

### 6.2 Fold marker

`@W` is associated with the geometrically nearest segment of a closed panel.
That segment receives `fold: true`. The parser records the fold edge but does not
expand or mirror the panel. An exact distance tie is an ambiguity error.

## 7. Panel and segment identity

If an SVG path has an `id`, it is used as the panel name. When one path contains
multiple closed subpaths, suffixes `_001`, `_002`, and so on are appended. If no
usable ID exists, deterministic names `panel_001`, `panel_002`, and so on are
assigned in document order.

Segments are numbered in authored subpath order, starting at zero. Sewing and
fold references use the panel ID and segment index.

## 8. JSON document

The output is UTF-8 JSON with this top-level structure:

```json
{
  "schema": "yohsai-pattern",
  "version": "1.0.0",
  "source": {
    "svg_path": "C:/patterns/example.svg",
    "clothes_layer": "CLOTHES"
  },
  "units": "m",
  "scale": {
    "annotation": "@S99cm",
    "reference_length_m": 0.99,
    "reference_length_svg": 1234.5,
    "meters_per_svg_unit": 0.000801943
  },
  "panels": [],
  "sewing_groups": {}
}
```

Each panel has an ID, source path ID, `closed: true`, and an ordered `segments`
array. A straight segment is:

```json
{
  "index": 0,
  "type": "line",
  "start": [0.0, -0.1],
  "end": [0.2, -0.1],
  "sewing_group": "A",
  "fold": false
}
```

A cubic Bezier segment additionally preserves both controls:

```json
{
  "index": 1,
  "type": "cubic",
  "start": [0.2, -0.1],
  "control1": [0.22, -0.08],
  "control2": [0.24, -0.04],
  "end": [0.25, 0.0],
  "sewing_group": null,
  "fold": false
}
```

`sewing_groups` maps each normalized label to ordered references:

```json
{
  "A": [
    {"panel": "panel_001", "segment": 4},
    {"panel": "panel_002", "segment": 7}
  ]
}
```

Numbers must be finite JSON numbers. The output contains no NaN or Infinity.

## 9. Blender user interface

The Yohsai N-panel exposes:

- `SVG Path`: a file selector for an SVG document;
- `Load`: parse the SVG and create separate cloth-part objects;
- `Clothes`: the loaded numbered collection used by later actions;
- `Sewing`: build the combined sewn preview after manual part placement;
- `Body`: select the fixed collision mesh used by Kitsuke;
- `Kitsuke`: advance a short Taichi simulation and restore separate parts;
- a short status message.

`Load` validates the path, starts the external parser, and returns control to
Blender immediately. Repeated activation while a parse is running is rejected.
On success Blender validates and reads the fixed JSON document automatically,
then creates the mesh described in section 10. On failure it displays the parser
diagnostic and leaves the previous JSON untouched.

## 10. Initial Blender mesh

### 10.1 Fold expansion

A panel may have zero or one segment marked `fold`. More than one fold segment
in a panel is an error in this version. A panel with a fold segment is mirrored
about the infinite line through that segment. Original and mirrored halves are
welded along the fold to form one connected, symmetric piece of cloth. Sewing
attributes on the authored half are copied to their mirrored boundary segments.
The welded fold remains an internal constrained edge with a `fold` attribute.

### 10.2 Triangulation

Bezier and line boundaries are sampled at no more than approximately `0.01 m`
between boundary vertices. The interior is filled with a near-uniform constrained
triangular mesh at the same nominal spacing. Triangles form valid faces suitable
for a later Cloth modifier. `Load` does not create loose sewing-spring edges,
perform sewing, or add a Cloth modifier.

Boundary edge attributes preserve sewing membership as Boolean mesh attributes
named `sewing_<LABEL>`. Fold edges use the Boolean mesh attribute `fold`.

### 10.3 Object and collection

Each closed panel from one JSON document becomes a separate Mesh object. The
objects are placed in one newly created collection using the first available
name in the sequence `CLOTHES_001`, `CLOTHES_002`, and so on. Part objects are
named `<collection>_PART_001`, `<collection>_PART_002`, and so on. Existing
Yohsai collections are never overwritten by `Load`.

### 10.4 Initial placement

Expanded panels are packed horizontally without overlap:

- every vertex has world `Y = -1.0 m`;
- the lowest vertex has `Z = 0.01 m`;
- adjacent panel bounds have a `0.10 m` horizontal gap;
- the combined bounds are centered at world `X = 0`;
- face normals point toward world `-Y`.

The original SVG panel-to-panel offsets are not used for this initial packing.

### 10.5 Update boundary

`Load` always creates a new unused numbered collection. Updating an already
dressed garment is a separate future workflow because it must define panel
identity, topology changes, deformation transfer, sewing preservation, modifier
preservation, and panel-count changes.

## 11. Sewing

The user positions and rotates the separate part objects before pressing
`Sewing`. Object world transforms at that moment are applied to the generated
sewn mesh.

For every sewing label, the marked boundary edges are split into connected,
non-branching paths and ordered by mesh topology. A label must occur on exactly
two different part objects. Sewing a part to itself, a missing partner, a label
on more than two parts, a branched or closed sewing path, or unequal numbers of
continuous paths is an error.

When a label has multiple paths, Yohsai chooses the one-to-one path assignment
with the smallest total world-space endpoint distance. For each path pair it
compares the two possible directions and uses the direction with the smaller
endpoint-distance sum. A tied or otherwise ambiguous result is an error and the
user must move the intended seams closer together.

Vertices are matched monotonically by normalized distance along each ordered
path. This preserves ordering even when the two paths contain different vertex
counts. The resulting connections are edges that belong to no face, as required
for Blender Cloth sewing springs. They receive Boolean edge attributes named
`sewing_spring_<LABEL>`. The original marked boundaries retain
`sewing_<LABEL>`.

Blender Cloth sewing springs operate within one Mesh object. `Sewing` therefore
creates `<collection>_SEWN`, containing all positioned parts as disconnected
face islands plus the loose sewing edges. The original separate part objects are
kept in the same collection but hidden in the viewport and render. No Cloth
modifier is added in this step. Repeating `Sewing` for a collection that already
contains a sewn mesh is an error.

## 12. Kitsuke

The combined `Sewing` object is a visual verification and connectivity record,
not the persistent editing representation. On the first `Kitsuke`, Yohsai reads
its loose sewing edges, snapshots the evaluated Body, and creates a transient
Taichi simulation containing all source panels. The pattern edge lengths remain
the stretch rest lengths, paired seam vertices target zero distance, and body
and self contact use a 0.002 m thickness.

One click advances sixteen fixed 1/240-second steps and closes each transient seam
target by 0.030 m under a default 1.0 m/s² downward acceleration. This count is deliberately not exposed in the user interface.
After the calculation, positions are mapped
back by source object and vertex index, the combined preview is removed, and
the separate source objects are shown. The user may translate and rotate any
selection of those objects in Object Mode before clicking again. A transformed
part starts the next click with zero velocity; unchanged parts retain velocity.

Topology edits and object scaling are rejected during a session. Topology is
changed in the pattern and loaded as a new clothes collection. The Body is
constant after its first evaluated snapshot. Runtime velocity is not persisted
across reopening a Blender file.

Version 0.1.9 temporarily exposes gravity and seam closure in the N-panel; their
current values are read on every click without reconstructing the session.

Taichi selects an available GPU architecture automatically and uses an explicit
CPU fallback when GPU initialization fails. The 0.1.9 package supplies Windows
x64 CPython 3.13 wheels.

## 13. Future compatibility

Future versions may add darts, notches, grain lines, seam order and direction,
additional SVG primitives, error-checker interoperability, and Blender geometry
generation. Such additions require a schema-version change or backward-
compatible optional fields. They must not silently reinterpret version 1 data.
