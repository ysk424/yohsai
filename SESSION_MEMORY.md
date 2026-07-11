# Yohsai session memory

Status: authoritative handoff for the next Codex session  
Recorded: 2026-07-11 (Asia/Tokyo)  
Release prepared today: Yohsai 0.2.0

## Product viewpoint that must not drift

Yohsai is pattern-designer software. The Illustrator PDF/SVG pattern owns the
cloth pieces, dimensions, labels, sewing connectivity, and future construction
annotations. Blender meshes and simulation state are replaceable realizations.
Update means recutting revised cloth, transferring the old 3D pose only as a
placement convenience. Read `DESIGN_PHILOSOPHY.md` before changing architecture.

The production N-panel intentionally contains three inputs followed by four
actions:

1. Pattern Path, Clothes, Body;
2. Load, Update, Sewing, Kitsuke.

Silhouette export is a once-per-character Scripting utility in `UTIL/`, not a
normal Yohsai button. Gravity and seam closure are fixed internal values, not
production UI controls.

## Working workflow

1. Save the annotated Illustrator PDF (preferred) or compatible SVG.
2. Load it; Yohsai creates separate packed panel objects.
3. Translate and rotate panels in Object Mode.
4. Run Sewing and visually verify connectivity.
5. Select the character skin Mesh as Body.
6. Alternate Kitsuke clicks with manual Object Mode translation/rotation.
7. After editing and saving the same pattern, use Update; use Sewing again only
   when the authored sewing signature changed.

Current tested solver values are 16 substeps at 1/240 s, Gravity 1.0 m/s²,
Seam Pull 30 mm/click, bend stiffness 0.08, stretch stiffness 0.95, maximum
speed 1.0 m/s, and 2 mm contact thickness.

## 0.2.0 Undo/Redo design

Blender Undo restores meshes but cannot restore Python/Taichi objects. Each
successful Kitsuke click therefore writes same-runtime recovery data into
undoable Blender datablocks:

- collection runtime epoch and revision;
- exact global seam-vertex pair array;
- current seam-rest array;
- one `yohsai_kitsuke_velocity` point attribute per panel;
- the last accepted Object Mode matrix per panel.

`undo_post` and `redo_post` clear live Taichi sessions. The next Kitsuke rebuilds
from Blender's restored positions, velocities, seam targets, and transforms.
Undoing click 2 restores click 1 exactly; repeating Kitsuke performs click 2
again instead of skipping to click 3. A new Blender/add-on runtime has a new
epoch and deliberately ignores old recovery data. Cross-restart continuation
of an abandoned partially dressed session is unsupported; restart from
Load/Sewing. A persistent `load_pre` handler rotates the epoch whenever Blender
loads a `.blend`, including another file opened in the same process.

Tests:

- `tests/blender_mesh_check.py`: PDF Load, Sewing, repeated Kitsuke, Update, and
  sewing-change rejection;
- `tests/blender_undo_check.py`: exact seam/velocity Undo and Redo restoration,
  plus replay of click 2.

## Current supported input

The real integration input is `C:\Users\azoo\Desktop\test2.pdf`. It currently
produces two labeled panels (`OMOTE`, `URA`) and sewing groups A/B. PDF is
preferred because Illustrator rewrites SVG layer IDs. Bundled dependencies are
Taichi 1.7.4, pypdf 6.14.2, and their listed wheels for Blender 5.2 / CPython
3.13 on Windows x64.

## Explicitly deferred issues

These were reviewed on 2026-07-11 and intentionally not fixed in 0.2.0:

- direct vertex edits and same-vertex-count topology changes are unsupported but
  not fully detected by Kitsuke;
- an unlabeled closed PDF outline enclosing a labeled panel makes containment
  ambiguous even though unlabeled artwork is not emitted;
- PDF close-and-paint and implicit fill closure are not recognized as explicit
  closed panels;
- the ASCII label regex accidentally accepts a few Unicode case-folding
  characters;
- PDF `@S0cm` and negative `@S` metadata lack the intended positive-value
  validation.

Do not reinterpret these accidental acceptances as product requirements.

## Next engineering priority

The incremental dressing model and current parameter range are validated. The
main visible problem is Body tunneling/penetration around high-curvature areas
such as shoulders, chest, and abdomen. Collision candidates are built once per
click and contact is discrete. Refreshing contacts within the click or adding a
continuous-collision strategy is the next substantial solver task.

## Release procedure

1. Fully close interactive Blender and Blender MCP before replacing the
   extension; the Taichi native module can lock installed wheel files.
2. Build with Blender's `--command extension build` command.
3. Validate the resulting ZIP.
4. Install the ZIP with `extension install-file -r user_default -e`.
5. Run both Blender integration scripts against the installed extension.
6. Confirm `git status --short` is empty, then commit and push `main`.

The expected archive for this handoff is `dist/yohsai-0.2.0.zip`.
