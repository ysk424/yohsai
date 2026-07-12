# Yohsai PDF-to-JSON Specification

Status: implemented schema and Blender workflow contract
Version: 1.0.0

## 1. Purpose

Yohsai converts garment patterns authored in Adobe Illustrator from PDF into a
single machine-readable JSON document. The converter is a standalone process.
Blender starts it with a pattern path, waits asynchronously for it to finish, and
then reads the resulting JSON. The converter does not import Blender modules or
share memory with Blender.

The standalone conversion ends when valid JSON has been produced. Blender then
creates an initial flat garment mesh from that JSON. Darts, notches, grain lines,
and advanced sewing rules are outside this schema version. Sections 11 and 12
define Blender operations that consume the preserved sewing metadata.

## 2. Process contract

- Program name: `yohsai_svg_parser.py`.
- Runtime: the Python interpreter bundled with Blender, in a separate process.
- Dependencies: bundled `pypdf` and `typing_extensions` wheels for PDF.
- Command line: `python yohsai_svg_parser.py <absolute-pattern-path>`.
- The PDF path is the only positional argument.
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

## 3. Input profiles

### 3.1 PDF input profile

PDF is the Adobe Illustrator interchange format. Version 1 accepts
exactly one page and reads its standard page-content operators; Illustrator
private editing data is not required. Explicitly closed paths containing a valid
`#` text label are emitted as pattern panels. Unlabeled artwork is not emitted,
but all explicitly closed paths currently participate in label containment.
Therefore an unlabeled closed outline must not enclose a labeled panel. Open
reference silhouettes do not participate and are safe to keep on the page.

PDF line and cubic Bezier path operators are retained. The current parser
requires an explicit `h` close-path operation; close-and-paint and implicit fill
closure are not yet promoted to closed panels. Text annotations use `#`, `@W`,
and single-letter sewing syntax. The
initial Illustrator compatibility profile also decodes its embedded
Identity-H ASCII font convention when a ToUnicode map is absent.

PDF coordinates are points and convert directly to meters with
`0.0254 / 72`. `@S` scale annotations are obsolete and rejected. Page Y is
flipped into Yohsai's upward-positive pattern coordinates.

### 3.2 SVG input profile

SVG input is no longer supported. The public parser rejects `.svg` files and
direct `parse_svg` calls.

## 4. Scale

All JSON coordinates and lengths use meters. PDF scale is defined directly by
points as described in section 3.1:

`meters_per_svg_unit = 0.0254 / 72`

## 5. Coordinates

- Output X increases to the right.
- Output Y increases upward, so SVG Y coordinates are negated after applying
  SVG transforms.
- All output coordinates are meters.
- No panel is translated or packed by the parser.
- Relative placement between panels in the source document is preserved in
  JSON. Initial Blender Load deliberately repacks the panels.

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

### 6.3 Panel label

A `#` label belongs to the one closed panel containing the transformed text
origin. Zero or multiple containing panels is an error; Yohsai does not select a
nearest panel. Update requires exactly one label in every panel.

All whitespace characters are removed before parsing. The leading `#` is not
part of the stored value. ASCII letters compare case-insensitively and normalize
to uppercase. ASCII letters, digits, underscore, and hyphen are accepted. Empty,
invalid, or duplicate normalized labels are errors.

ASCII-only labels are the contract. Version 0.2.6 has a known Python
case-insensitive-regex gap that may accept a small set of Unicode case-folding
characters. Those accidental spellings are unsupported and must not be used.

## 7. Panel and segment identity

An explicit `#` label is the panel ID. Without a label, if an SVG path has an
`id`, it is used as the panel name. When one path contains
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
    "svg_path": "C:/patterns/example.pdf",
    "clothes_layer": "PDF # labeled panels",
    "input_format": "pdf"
  },
  "units": "m",
  "scale": {
    "meters_per_svg_unit": 0.00035277777777777776
  },
  "panels": [],
  "sewing_groups": {}
}
```

`source.svg_path` is a legacy field name retained for compatibility and stores
the absolute source path. PDF documents set `source.input_format` to `pdf`.

Each panel has an ID, optional normalized `label`, source path ID, `closed:
true`, and an ordered `segments` array. A labeled panel begins:

```json
{
  "id": "FRONT01",
  "label": "FRONT01",
  "source_path_id": "path123",
  "closed": true,
  "segments": []
}
```

A straight segment is:

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

The Yohsai N-panel groups all inputs first:

- `Pattern Path`: a file selector for a PDF document;
- `Clothes`: the loaded numbered collection used by later actions;
- `Body`: select the fixed collision mesh used by Kitsuke.

It then exposes only the four primary actions in workflow order:

- `Load`: parse the pattern and create separate cloth-part objects;
- `Update`: recut the selected Clothes collection from the same saved file;
- `Sewing`: build the combined sewn preview after manual part placement;
- `Kitsuke`: advance a short Taichi simulation and restore separate parts;

A short status message appears below the actions. Solver tuning and silhouette
export are intentionally absent from this production panel.

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
triangular mesh at the same nominal spacing. Triangles form the panel topology
consumed by Yohsai's Taichi solver. `Load` does not create loose sewing-preview
edges, perform Sewing, or add a Blender Cloth modifier.

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

The original PDF panel-to-panel offsets are not used for this initial packing.

### 10.5 Load versus Update

`Load` always creates a new unused numbered collection. `Update` is the
implemented recut workflow for an existing collection and is specified in
section 13. The current Update scope requires the same panel-object count and
normalized `#` label set, while triangulation and vertex counts may change.

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
counts. The resulting connectivity record uses edges that belong to no face.
They receive Boolean edge attributes named
`sewing_spring_<LABEL>`. The original marked boundaries retain
`sewing_<LABEL>`.

`Sewing` creates `<collection>_SEWN`, containing all positioned parts as
disconnected face islands plus the loose sewing edges so Yohsai can verify and
capture cross-panel pairs. The original separate part objects are
kept in the same collection but hidden in the viewport and render. No Cloth
modifier is added in this step. Repeating `Sewing` for a collection that already
contains a sewn mesh is an error.

## 12. Kitsuke

The combined `Sewing` object is a visual verification and connectivity record,
not the persistent editing representation. On the first `Kitsuke`, Yohsai reads
its loose sewing edges, snapshots the evaluated Body, and creates a transient
Taichi simulation containing all source panels. Later clicks reuse that live
runtime. The pattern edge lengths remain the stretch rest lengths, paired seam
vertices progressively approach zero distance, and body and self contact use a
0.002 m thickness.

One click advances sixteen fixed 1/240-second steps and closes each transient
seam target by 0.030 m under a default 1.0 m/s² downward acceleration. This
count is deliberately not exposed in the user interface.
After the calculation, positions are mapped
back by source object and vertex index, the combined preview is removed, and
the separate source objects are shown. The user may translate and rotate any
selection of those objects in Object Mode before clicking again. A transformed
part starts the next click with zero velocity; unchanged parts retain velocity.

Object scaling and vertex-count changes are rejected during a session. Direct
vertex edits and same-vertex-count topology edits are unsupported but are not
yet completely detected; topology must be changed in the pattern. The Body is
constant within one live runtime.

Version 0.2.0 mirrors exact seam pairs and targets, per-vertex velocity,
revision, runtime epoch, and accepted Object Mode matrices into undoable Blender
data after every successful click. Blender `undo_post` and `redo_post` handlers discard the
non-undoable Taichi objects. The next Kitsuke reconstructs them from the state
restored by Blender, preventing a 30 mm seam stage from being skipped. Recovery
data is valid only for the current add-on runtime. Continuing an abandoned
partially dressed session after reopening Blender or reloading the add-on is
unsupported.

Version 0.2.0 uses the tested gravity and seam-closure defaults internally.
They are solver constants rather than pattern data and do not alter the JSON
contract.

Taichi selects an available GPU architecture automatically and uses an explicit
CPU fallback when GPU initialization fails. The 0.2.9 package supplies Windows
x64 CPython 3.13 wheels.

## 13. Update

Update rereads the same absolute PDF path that created the selected Clothes
collection. The number and normalized set of `#`-labeled panel objects must be
unchanged. The operation generates entirely new panel meshes; vertex and face
counts may differ.

Load stores each vertex's authoritative flat-pattern position as the Point
attribute `yohsai_pattern_position`. Update normalizes the revised and previous
panel bounds, locates the corresponding old flat triangle, and barycentrically
interpolates its current world-space deformation onto each new vertex. This is
an initial-placement convenience, not a claim that the old and new cloth are
physically identical.

Existing objects, transforms, materials, collection membership, names, and
panel indices remain. Revised flat coordinates define new stretch and bend rest
lengths. Runtime velocity and the previous Kitsuke session are discarded.

Update prepares all meshes before changing Blender data. Any missing, duplicate,
unexpected, or ambiguous label, panel-count change, parse error, triangulation
error, or transfer failure cancels the whole operation without modifying the
existing garment.

The sewing signature contains normalized sewing labels and their panel/segment
membership, but not geometry coordinates. An unchanged signature preserves the
verified Sewing state and permits direct Kitsuke. A changed signature clears
verification; Kitsuke refuses with `Sewing required` until Sewing succeeds.

## 14. Future compatibility

Future versions may add darts, notches, grain lines, seam order and direction,
additional PDF primitives, error-checker interoperability, and richer
Blender geometry generation. Such additions require a schema-version change or
backward-compatible optional fields. They must not silently reinterpret version
1 data.
