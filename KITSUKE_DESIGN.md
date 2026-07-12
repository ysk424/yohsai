# Kitsuke Design and Tuning Record

Status: active implementation and empirical tuning  
Recorded: 2026-07-11 (Asia/Tokyo)  
Current tested package: Yohsai 0.2.6, Windows x64, Blender 5.2 / Python 3.13,
Taichi 1.7.4

## 1. Product idea

`Kitsuke` is Yohsai's second central feature. There is no exact English
equivalent for the intended operation, so the button and operation retain the
Japanese name.

A complete physical drape from a poor initial placement often fails. Kitsuke
instead advances only a short, fixed simulation interval. The user alternates
between a physics click and manual Object Mode placement until the garment is
dressed. The current operator is a human. The same explicit operation boundary
may later allow an MCP/LLM agent to perform the placement.

The pattern is authoritative. It completely owns panel topology, vertex
identity, original dimensions, and sewing connectivity. Dressing is only a
physical realization of that data. Topology is never edited during Kitsuke; a
topology change returns to the pattern and starts a new Load.

## 2. Confirmed user workflow

1. `Load` creates one Mesh object for every pattern panel.
2. The user translates and rotates the separate panels around the character.
3. `Sewing` constructs a combined preview with loose sewing edges.
4. The user visually checks sewing correspondence and agreement with the
   pattern. If it is wrong, the user returns to the pattern rather than fixing
   topology in Blender.
5. The user selects the character's actual skin Mesh as `Body`.
6. One `Kitsuke` click constructs transient sewing and physics state, advances a
   short interval, then restores the separate panel objects.
7. The user translates and rotates any one or more objects in Object Mode.
8. Steps 6 and 7 repeat while seams close and the cloth approaches the Body.

Panels have no semantic labels such as front, back, or sleeve in the simulation.
Only the user knows what a panel represents and where it belongs. A sleeve may
contain several panels, and a dress may contain many gores. Yohsai must not infer
garment-part meaning from object identity.

## 3. Persistent and transient representations

The persistent editable representation is always the set of separate panel
objects. A permanently joined Blender Mesh cannot support independent Object
Mode translation and rotation, which are required during dressing.

For the first click, and after Undo/Redo invalidates a runtime, Kitsuke builds
the transient indexed state. Every click then performs this round trip:

1. Read current world-space vertices and transforms from all panel objects.
2. Synchronize the live runtime, or reconstruct it when none is valid.
3. Use sewing constraints from the verified Sewing preview/recovery state.
4. Advance Taichi stretch, approximate bend, sewing, gravity, Body contact, and
   self-contact constraints.
5. Scatter positions back by source object and original vertex index.
6. Delete the initial combined Sewing preview after the first click.
7. Show and select the separate panel objects again.

The initial Sewing preview is both a visual verification object and the record
from which the first session captures exact seam vertex pairs. Later clicks
reuse the live Taichi runtime while synchronizing supported Object Mode changes.
Undo and Redo discard that runtime and reconstruct it from Blender recovery
data before another click.

## 4. Transform and state rules

- Translation and rotation in Object Mode are supported.
- Scaling is rejected because it changes authoritative pattern dimensions.
- Scaling and vertex-count changes are rejected. Other direct mesh edits are
  unsupported but are not yet completely detected; topology belongs in the
  pattern.
- An untouched panel retains its per-vertex velocity between clicks.
- A translated or rotated panel has all its vertex velocities reset to zero.
- A locked mesh object remains in sewing connectivity but is excluded from
  Kitsuke deformation. Lock is object-level and literal: it does not infer
  garment type, dressing order, or whether the object is currently worn.
- The evaluated Body is captured when a live session is constructed and remains
  constant until that session is invalidated.
- Every successful click stores exact seam pairs and targets, per-vertex
  velocity, revision, and Object Mode transforms in undoable Blender data.
- `undo_post` and `redo_post` invalidate the non-undoable Taichi runtime. The
  next click rebuilds it exactly from Blender's restored same-runtime state.
- Recovery data carries a process epoch. Reopening Blender or reloading the
  add-on ignores an older epoch; loading any `.blend` rotates the epoch through
  `load_pre`. Continuing an abandoned partially dressed session across a
  restart or file load is unsupported; restart from Load/Sewing.

## 5. Collision rules

- Body contact thickness: 0.002 m.
- Cloth self-contact thickness: 0.002 m.
- Final paired seam distance: 0 m.
- Seam closure has priority over Body and self-contact. If an undersized
  garment cannot satisfy both collision and sewing, the accepted failure is
  local cloth distortion or Body penetration, not a visible opened seam.
- Seam vertices and their immediate connected neighborhoods are excluded from
  conflicting self-contact so paired seams may close to zero.
- Body inside/outside is checked with majority parity over three non-axis-aligned
  ray directions. This tolerates small open features such as eyes better than a
  single ray.
- Initial Body penetration is projected out before dynamic integration.
- Dynamic Body contact uses only the nearest candidate Body triangle for each
  cloth vertex.
- Multiple collision corrections on a vertex are averaged, not summed.

The Body field accepts only an actual Mesh. Character roots, Empty objects, and
Armatures are not colliders. Expand the character hierarchy and select the skin
Mesh itself.

## 6. Current solver and safety constants

Implementation: `kitsuke.py`, Taichi PBD-style Jacobi constraints.

| Parameter | Current value | Notes |
| --- | ---: | --- |
| Time step | 1/240 s | Small-step stability |
| Substeps per click | 16 | About 1/15 s simulated per click |
| Constraint iterations per substep | 1 | Small Steps strategy |
| Post-contact seam projection passes | 4 | Keeps closed seams from reopening after collision |
| Default gravity magnitude | 1.0 m/s² | Interactive tuning value |
| Default seam closure | 30 mm/click | Seam maximum distance ratchets downward |
| Velocity damping rate | 4.0/s | Quasi-static dressing bias |
| Maximum velocity | 1.0 m/s | Sixteen substeps remain below the per-click displacement guard |
| Maximum constraint correction | 10 mm/substep | Allows stronger seam closure |
| Maximum accepted click displacement | 0.1 m | Larger movement rolls back |
| Collision broad-phase margin | 0.04 m | Reused during one click |

The solver rolls back without writing Blender mesh data if positions or
velocities become non-finite, or if any vertex moves more than 0.1 m in one
click. The error reports the measured maximum displacement.

Seam projection intentionally runs after Body and self-contact. This is a
product rule, not a generic cloth-simulation default: Yohsai must preserve the
pattern's sewn construction. When a garment is too small, failure should appear
as local distortion, compression, or Body penetration near the seam rather than
as the seam coming apart. Do not move Body collision back after seam projection
unless the product rule is explicitly changed.

## 7. Former temporary N-panel tuning controls

Yohsai 0.1.9 temporarily exposed:

- `Gravity` in m/s², default 1.0;
- `Seam Pull (mm/click)`, default 30.

From 0.1.12, these controls are removed from the production N-panel and their
tested values are fixed defaults. The tuning history below is retained as an
engineering record, not as a current user workflow.

Recommended tuning method:

1. Start from the same Load and approximately the same manual placement.
2. Record Gravity and Seam Pull.
3. Press Kitsuke a fixed number of times, initially five.
4. Record vertical drop, seam-distance change, collision failures, and required
   manual interventions.
5. Change only one parameter for the next comparison when possible.
6. Once a useful range is found, repeat with a second garment/placement before
   adopting constants.

Current empirical log:

| Date | Build | Gravity | Seam Pull | Clicks | Observation |
| --- | --- | ---: | ---: | ---: | --- |
| 2026-07-11 | 0.1.6 | 9.81 m/s² | 2 mm | 5 | Stable, but dropped much too quickly and seams remained visibly far apart. |
| 2026-07-11 | 0.1.7 | 1.962 m/s² | 10 mm | 26 | No explosion; panels reached the lower body and produced a recognizable partial drape. Parameter tuning remains necessary. |
| 2026-07-11 | 0.1.9 | 1.0 m/s² | 30 mm | Not recorded | First full real-character dressing result: seams close and the dress follows the torso, waist, hips, and hem. Local Body penetrations remain around the shoulders, chest, and abdomen. |

Append future trials to this table rather than relying on memory alone.

## 8. Observations and change history

### Initial 0.1.4 prototype

The first real character test blew the cloth far away and destroyed its shape.
The cause was not merely stiffness. Body collision corrections from many nearby
triangles were accumulated, distant seam pairs immediately targeted zero, and
large positional corrections became large velocities.

### 0.1.6 stabilization

- changed 1/120 s with five iterations to 1/240 s with one iteration;
- used only the nearest Body triangle;
- averaged collision corrections;
- added per-constraint correction and velocity limits;
- introduced progressive seam closure;
- added invalid-state and excessive-displacement rollback.

The test garment then completed repeated clicks without explosion.

### 0.1.7 rate adjustment

After five real-character clicks, normal gravity caused too much vertical drop
and 2 mm/click seam closure was visibly too weak. Gravity was reduced to one
fifth and seam closure was increased fivefold to 10 mm/click. A subsequent real
test completed 26 clicks and produced a recognizable partial drape around the
lower body without the original explosion. Further empirical tuning is needed.

### 0.1.8 tuning build

Gravity and Seam Pull were exposed in the N-panel. The integration test changes
both values in the middle of an active session, verifies the next click reports
the new values, restores defaults, and continues. The 1,333-vertex,
2,446-triangle fixture remains finite and its mean seam distance decreases over
11 clicks including a translated and rotated panel.

### 0.1.9 tuning build

The next requested trial uses Gravity 1.0 m/s², Seam Pull 30 mm/click, and
sixteen substeps per click. Panel mesh spacing is reduced from 20 mm to 10 mm,
which approximately doubles linear resolution and quadruples face count. Bend
constraint stiffness is reduced from 0.18 to 0.08 while stretch stiffness stays
unchanged so the cloth bends more easily without discarding pattern dimensions.
The maximum velocity is reduced from 2.0 to 1.0 m/s because sixteen substeps at
2.0 m/s could exceed the 0.1 m per-click rollback threshold.

The refined integration fixture contains 4,821 vertices, 9,212 triangles, and
230 sewing constraints. It completes 11 clicks without rollback, including an
Object Mode translation and rotation and a temporary mid-session parameter
change. Mean seam distance decreases over the test.

The first real-character 0.1.9 result reaches a complete recognizable dress
silhouette around the torso and lower body. This validates the incremental
Kitsuke interaction model and the current order of magnitude for gravity and
seam closure. Visible local holes show that collision quality is now the primary
problem: cloth vertices can still cross the Body around high-curvature or
fast-moving regions such as the shoulder, breast, and abdomen.

### 0.2.0 Undo/Redo synchronization

Blender restores panel meshes during Undo/Redo but cannot restore Python/Taichi
objects. Each successful Kitsuke click now mirrors the minimum continuation
state into Blender: exact seam pairs, the seam-rest array, per-part velocity
point attribute, revision number, runtime epoch, and last accepted Object Mode
matrix. Undo and Redo handlers clear all live Taichi sessions. The next Kitsuke reconstructs the
runtime from the restored mesh and recovery data. Regression tests verify both
click 1/click 2 restoration and repeating click 2 after Undo without skipping a
30 mm seam-closure stage.

### 0.2.3 seam-priority projection

The 0.2.2 ratchet correctly stored seam maximum distances, including zero after
a seam had fully closed, but Body and self-contact still ran after the sewing
constraint and could visibly reopen a shoulder seam in the final displayed
state. MCP inspection of a real shoulder case showed all stored seam maximum
distances at 0 m while current distances reached about 12.7 mm, proving the
problem was enforcement order rather than ratchet state.

0.2.3 adds a dedicated post-contact seam projection pass. It is separate from
the normal averaged stretch, bend, and collision corrections, and it runs four
times after Body/self collision in every substep. The deliberate priority is:
seams must stay sewn; if the garment is too small, deformation or penetration
near the seam is preferable to an opened seam. This preserves the future path
toward welded or topologically joined sewn meshes.

The 2026-07-12 real-character shoulder retest produced a satisfactory result:
the visible shoulder seam no longer became the failure point. This confirms the
product priority that seams are more important than nearby cloth/body
collision quality. Future solver changes must preserve this priority unless the
construction model itself changes.

### 0.2.4 object-level deformation Lock

The N-panel adds `Lock` between Body and the action buttons. Checking it while
mesh objects are selected writes an object-level flag that removes those
objects' vertices from Kitsuke deformation while preserving sewing pairs,
boundary labels, update identity, and recovery metadata. Lock is exclusive:
checking it clears previous Locks in the selected Clothes collection and makes
the current selection the locked set; unchecking it clears the selected Clothes
lock state. The locked object still participates in the indexed sewing graph;
unlocked sewn partners may be pulled toward it, but the locked vertices do not
integrate gravity, collision, stretch, bend, or seam projection.

This is intentionally not a garment-semantic feature. Yohsai does not decide
that an object is a sleeve, bodice, already worn part, or future addition. The
user controls staging by selecting mesh objects and toggling Lock. This creates
the foundation for adding not-yet-worn parts and controlling dressing order
without introducing hidden assumptions. The adjacent `Auto` button is only a UI
placeholder in 0.2.4; automatic lock selection is deferred until manual Lock is
validated.

0.2.5 fixes the first Lock switching problem found in manual testing. The
initial implementation behaved like additive flags, so an older locked object
could remain locked when the user selected a different object and checked Lock
again. That is not the intended staging model. Manual Lock is a current
selection switch, not a cumulative selection history.

0.2.6 fixes the corresponding unlock-scope problem. Lock clearing must not rely
only on the N-panel Clothes pointer, because that pointer can be absent or stale
relative to the selected part objects. The UI now resolves the selected objects'
`yohsai_collection` back to their Clothes collection and clears every part in
that scope. The required manual sequence is: lock front, simulate back; lock
back, simulate front; uncheck Lock, then both front and back deform again.
Manual testing confirmed this sequence works in 0.2.6.

## 9. Known limitations

- This is stabilized PBD, not a complete XPBD material model.
- Bend resistance uses opposite-vertex distance constraints rather than a full
  dihedral-angle cloth bending constraint.
- Cloth collision is discrete and uses a contact set built once per click.
- The real-character 0.1.9 trial shows remaining Body tunneling/penetration at
  high-curvature regions. Contact refresh or continuous collision detection is
  required before treating collision handling as production-ready.
- Friction is not yet modeled.
- Panel mass and textile-specific stretch/bend parameters are not exposed.
- Same-vertex-count topology edits and direct vertex edits are not yet fully
  detected even though they are unsupported.
- Live Taichi objects are not serialized. Blender stores only same-runtime
  Undo/Redo recovery data; cross-restart continuation is unsupported.
- The bundled 0.2.6 distribution is Windows x64 / CPython 3.13.
- Taichi wheels make the extension archive approximately 85 MB.

XPBD is a likely later improvement because compliance reduces dependence on
time step and iteration count. It should follow empirical validation of the
current interaction model rather than block tuning of the central workflow.

## 10. Resume checklist

When work resumes:

1. Use `dist/yohsai-0.2.6.zip` unless a newer build exists.
2. Fully close Blender and Blender MCP before replacing the extension, because
   the loaded Taichi native library may lock its wheel files on Windows.
3. Start from Load/Sewing after an extension or Blender restart; abandoned
   partial Kitsuke continuation is outside the supported workflow.
4. Continue real-character trials with the fixed Gravity 1.0 m/s² and Seam Pull
   30 mm/click values; record click count and manual interventions.
5. Prioritize Body tunneling/contact refresh before adding textile controls.
6. Run both `tests/blender_mesh_check.py` and
   `tests/blender_undo_check.py` against the installed ZIP before release.
7. Read `SESSION_MEMORY.md` for the current handoff and explicitly deferred
   parser/topology issues.
