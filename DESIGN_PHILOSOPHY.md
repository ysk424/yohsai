# Yohsai Design Philosophy

Status: authoritative product perspective  
Recorded: 2026-07-11 (Asia/Tokyo)

## 1. Yohsai is pattern-designer software

Yohsai must be designed from the perspective of a pattern designer, not from
the conventional perspective of a 3D modeler.

This distinction is not cosmetic. It decides which data is authoritative, what
an update means, which failures are acceptable, and how the interface should
feel. During implementation it is easy to drift toward familiar 3D concepts
such as preserving mesh identity, editing topology in place, or treating a
garment as one persistent deformed surface. Yohsai must actively resist that
drift.

When a 3D convention conflicts with the way a pattern designer understands the
work, the pattern-designer interpretation wins.

## 2. The pattern is the source of truth

The Illustrator pattern owns:

- the pieces of cloth;
- their authored outlines and internal construction information;
- stable panel labels;
- sewing annotations and future construction annotations;
- the intended rest dimensions of the material.

The Blender meshes are realizations of the pattern. They are not the master
data. Mesh topology, triangulation, object data, simulation state, and the
current drape may all be regenerated or discarded when the pattern changes.

Topology is changed in the pattern, never repaired as an ad-hoc mesh edit during
dressing.

## 3. Update means recutting and resewing

A pattern designer who changes a pattern does not think, "I am deforming the
old 3D mesh." The mental model is closer to this:

1. discard the previously cut cloth;
2. cut new cloth from the revised pattern;
3. sew the new pieces;
4. dress and settle the new garment again.

`Update` must follow this model. It parses the saved PDF or SVG again and constructs
new mesh data from the revised pattern. The new pattern dimensions become the
new rest state. Physical velocity is reset.

Transferring the old garment's deformed 3D pose to the new mesh is only a
placement convenience. It gives the newly cut cloth a useful starting location.
It does not mean the old and new meshes are the same physical cloth, and it must
not constrain future pattern changes.

The first Update implementation may transfer a moderately resized pattern by
mapping new flat-pattern vertices through the old flat triangulation and
interpolating the old 3D pose. This is intentionally provisional. It will not
cover every future edit, including:

- adding or removing a dart;
- changing a straight collar into a curved collar;
- adding or removing internal construction lines;
- changing panel count;
- changing sewing topology.

When such changes cannot be transferred safely, Yohsai should require a fresh
construction or manual placement rather than inventing an unreliable 3D
interpretation. Recutting is a valid outcome, not a failure of the product
model.

## 4. Stable labels identify pattern pieces

Human-authored `#` text labels identify corresponding pattern panels across
PDF/SVG saves and Updates. The text origin is placed inside its closed Bezier panel.
If it is not inside exactly one panel, parsing fails rather than guessing.

Label normalization rules:

- remove whitespace characters from the label text;
- compare ASCII letters case-insensitively;
- allow underscore `_` and ASCII hyphen `-`;
- require uniqueness after normalization.

For example, `#Front Bodice`, `#frontbodice`, and `#FRONTBODICE` identify the
same normalized label. Labels identify pattern pieces, not persistent Blender
Mesh datablocks.

An Update is atomic. Missing, duplicated, unexpected, or ambiguous panel labels
are errors and leave all existing Blender data unchanged.

## 5. Update workflow

The intended rapid iteration loop is:

1. watch the Blender viewport;
2. adjust pattern size or shape in Illustrator;
3. press `Ctrl+S` in Illustrator;
4. press `Update` in Blender;
5. press `Kitsuke` to settle the newly cut cloth;
6. repeat.

The pattern path does not change. `Update` rereads the same PDF or SVG selected
by `Pattern Path` and updates the currently selected `Clothes` collection.

For the initial Update scope:

- the number of panel objects is unchanged;
- every panel has a unique `#` label;
- panel dimensions may change;
- triangulation and vertex count may change;
- object names, collection ownership, materials, visibility, and Object Mode
  transforms are preserved;
- the current 3D pose is transferred as an initial placement when possible;
- new pattern dimensions replace all old stretch and bend rest data;
- all velocities reset to zero;
- the operation is all-or-nothing.

## 6. Sewing is independent and reusable

`Sewing` is not an implementation detail of Load or Update. It is an independent
construction and verification operation that can be run again whenever sewing
connectivity changes or the user discovers a sewing mistake after dressing.

If Update preserves the verified panel labels, sewing labels, and their authored
segment membership, the user may proceed directly to Kitsuke. If the sewing
definition changed and the user presses Kitsuke without rebuilding Sewing,
Yohsai must refuse and display a clear `Sewing required` warning.

The reusable sewing engine and the user-visible Sewing verification action may
share code, but their responsibilities must remain explicit. No component should
silently guess or approve changed sewing connectivity.

## 7. Kitsuke is incremental dressing

Kitsuke does not attempt to solve the entire dressing problem in one simulation.
It alternates a short physical step with deliberate placement by an operator.
The operator currently means a human.

Separate panel objects remain the editable representation because only the
operator knows what each piece represents and where it belongs. A sleeve may be
made from several pieces, and a dress may contain many gores. Yohsai must not
infer garment semantics merely from the number or shape of panels.

After every simulation step, the panels return to separate Blender objects so
the operator can translate and rotate them before the next transient sewing and
simulation step.

Undo/Redo recovery does not make simulation state authoritative pattern data.
Yohsai may mirror seam targets and velocity into Blender data so Blender can
restore one interactive Kitsuke history step correctly, but that data is scoped
to the current runtime and may be discarded on restart, Update, or recutting.
The PDF/SVG pattern remains the only durable construction authority.

## 8. Illustrator interaction should stay minimal

Marvelous Designer and conventional 3D applications frequently require the
designer to remember and switch tools for each operation. Yohsai should reduce
that cognitive cost.

The core Illustrator authoring experience uses only:

- the Bezier/Pen tool for geometry;
- the Text tool for labels, sewing markers, and commands.

The designer should not have to remember where a specialized tool is located.
Geometry plus compact text annotations should express garment construction.
This simplicity is a primary product advantage, not merely an implementation
shortcut.

## 9. Annotation roadmap

`@` is reserved for commands and construction metadata. Plain sewing markers
continue to represent sewing groups. `#` identifies panels.

Deferred commands include:

- `@M`: create one left/right mirrored set; this is common for sleeves and
  symmetric garment pieces;
- `@N<number>`: create repeated identical pieces, such as five or ten frill
  panels; this is deferred because replicated sewing labels and partner
  selection require a complete rule;
- `@B<bone hint>`: store an armature bone-name hint for future automatic
  placement.

Future construction metadata will also need to represent elastic, gathers,
internal lines, stay tape, and other garment-making concepts. These features
must be added as pattern concepts rather than borrowed uncritically from a 3D
tool taxonomy.

The Blender interface follows the same restraint. The normal N-panel groups its
few inputs first and presents the construction sequence as `Load`, `Update`,
`Sewing`, and `Kitsuke`. One-time character preparation and engineering tuning
do not belong among those daily actions. Occasional preparation such as
silhouette export lives in documented Scripting utilities instead.

## 10. V2 automatic operator

The long-term V2.0.0 concept replaces or assists the human placement operator
with a small language model, currently envisioned as a GPT-5.4-nano-class API
workflow:

1. provide textual dressing instructions and current garment metadata;
2. receive JSON calls describing placement operations;
3. execute constrained Blender placement functions;
4. send a viewport image back to the model;
5. repeat placement and Kitsuke until dressed.

Bone hints such as `@Bupper arm` can assist this process, but they are deferred
until the manual construction, Update, Sewing, and Kitsuke workflows are
complete and reliable.

## 11. Anti-drift checklist

Before implementing a garment feature, ask:

1. Is this information owned by the pattern or by its temporary 3D realization?
2. Would a pattern designer describe this as editing old cloth, or cutting new
   cloth?
3. Are we preserving a Blender object merely because 3D software usually does?
4. Can the operation be expressed with Bezier geometry and a short annotation?
5. Are ambiguous or unsupported construction changes rejected clearly instead
   of guessed?
6. Does the design keep Sewing, Update, and Kitsuke independently reusable?

If the answers begin from mesh identity rather than pattern intent, reconsider
the design before writing code.
