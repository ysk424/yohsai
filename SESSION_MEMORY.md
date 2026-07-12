# Yohsai session memory

Status: authoritative handoff for the next Codex session  
Recorded: 2026-07-11 (Asia/Tokyo)  
Release prepared today: Yohsai 0.2.9

## Product viewpoint that must not drift

Yohsai is pattern-designer software. The Illustrator PDF pattern owns the
cloth pieces, dimensions, labels, sewing connectivity, and future construction
annotations. Blender meshes and simulation state are replaceable realizations.
Update means recutting revised cloth, transferring the old 3D pose only as a
placement convenience. Read `DESIGN_PHILOSOPHY.md` before changing architecture.

The production N-panel intentionally contains three inputs followed by four
actions:

1. Pattern Path, Clothes, Body;
2. Lock/Auto;
3. Load, Update, Sewing, Kitsuke.

Lock is a literal object-level deformation exclusion for selected mesh objects.
It preserves sewing information and does not infer garment meaning or dressing
order. It is exclusive, not additive: checking Lock switches the locked set to
the current selection, and unchecking clears the selected Clothes lock state.
Auto is intentionally not implemented yet.

Silhouette export is a once-per-character Scripting utility in `UTIL/`, not a
normal Yohsai button. Gravity and seam closure are fixed internal values, not
production UI controls.

## Working workflow

1. Save the annotated Illustrator PDF.
2. Load it; Yohsai creates separate packed panel objects.
3. Translate and rotate panels in Object Mode.
4. Run Sewing and visually verify connectivity.
5. Select the character skin Mesh as Body.
6. Alternate Kitsuke clicks with manual Object Mode translation/rotation.
7. After editing and saving the same pattern, use Update; use Sewing again only
   when the authored sewing signature changed.

Current solver values are 8 substeps at 1/240 s, user-adjustable 1-128
constraint iterations/substep with default 16, Gravity 1.0 m/s², Seam Pull
30 mm/click, ratcheting seam
maximum-distance constraints, four post-contact seam projection passes, bend
stiffness 0.08, stretch stiffness 0.95, maximum speed 1.0 m/s, maximum
constraint correction 5 mm/iteration, and 5 mm contact thickness.
2026-07-12 user testing confirmed that higher Iterations reduce visible stretch
and make the cloth behave stiffer. Keep the Iterations UI because the developer
machine is much faster than a typical user PC; slower users need a direct way to
lower quality instead of inheriting a fixed high-cost solver setting.

Important design rule: visible seam opening is the unacceptable failure mode.
When a garment is too small or Body collision conflicts with sewing, Yohsai
prefers local cloth distortion or Body penetration over a seam coming apart.
This is intentional because future versions are expected to weld or otherwise
topologically connect sewn seams.

2026-07-12 real-character retest: the 0.2.3 seam-priority projection produced a
satisfactory shoulder result and resolved the visible seam-opening failure. Keep
this rule explicit; generic cloth-simulation instincts may otherwise move
collision resolution back after sewing and reintroduce the problem.

0.2.4 adds manual Lock. A locked mesh object stays in the sewing graph but its
vertices are not deformation targets during Kitsuke. This is a major staging
primitive for not-yet-worn parts and dressing-order control. Keep it free of
garment-semantic assumptions.

0.2.5 fixes manual Lock switching. Rechecking Lock with a different selection
must replace the old locked set; otherwise staged dressing cannot move from one
reserved part to another.

0.2.6 fixes manual Lock clearing. Unlock must clear the whole selected Clothes
scope resolved from selected objects' `yohsai_collection`, not just the current
selection or a possibly stale N-panel Clothes pointer. The expected workflow is
front locked/back deforming, then back locked/front deforming, then no lock so
both deform. Manual testing confirmed this sequence works in 0.2.6.

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
produces two labeled panels (`OMOTE`, `URA`) and sewing groups A/B. Bundled dependencies are
Taichi 1.7.4, pypdf 6.14.2, and their listed wheels for Blender 5.2 / CPython
3.13 on Windows x64.

## Explicitly deferred issues

These were reviewed on 2026-07-11 and intentionally not fixed in 0.2.6:

- direct vertex edits and same-vertex-count topology changes are unsupported but
  not fully detected by Kitsuke;
- an unlabeled closed PDF outline enclosing a labeled panel makes containment
  ambiguous even though unlabeled artwork is not emitted;
- PDF close-and-paint and implicit fill closure are not recognized as explicit
  closed panels;
- the ASCII label regex accidentally accepts a few Unicode case-folding
  characters.

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

The expected archive for this handoff is `dist/yohsai-0.2.9.zip`.
