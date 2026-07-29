# Yohsai Development Notes

Status: current development state

## Architecture

- Illustrator PDF is authoritative for topology and annotations.
- Load creates separate pattern-part meshes.
- GRAVITY promotes parts through PLACED -> PENDING -> DONE without reversal.
- A new pending GRAVITY stage runs Sewing automatically from positioned source
  parts; completed parts remain available as connectivity anchors. The single
  independent deformation Lock is cleared from all pending parts before Sewing.
- GRAVITY starts from source-part world vertices.
- Each GRAVITY click first equalizes every seam's two sides to matching vertex
  counts, recutting only the shorter side from the pattern stored on the
  collection. Matched sides pair 1:1 so the longer edge gathers. A recut changes
  topology and therefore forces a Sewing rebuild.
- Seam goals are fixed at zero and do not shorten per click.
- Sewing drags a pair kinematically and contributes no momentum.
- Pattern edges, square metrics, and axial triples provide cloth internal energy.
- Pattern edges hold their authored length in both directions; the lattice folds
  by bending out of plane, not by letting a span collapse.
- Body participates only through contact correction, which dissipates only.
- Self-contact and Body-relative rest-shape forces are absent.
- Zero gravity and Normal gravity select 0 or 9.81 m/s² per click in world -Z.
- Auto is event-driven rather than derived continuously from state. Load and
  switching Auto on lock PLACED/DONE and unlock PENDING; switching it off
  unlocks non-placed parts. GRAVITY completion never changes Lock.
- The product path always uses the native Square-Lattice solver. Normal GRAVITY
  uses 16 material iterations; Zero GRAVITY uses 24 (1.5x) so one press does
  more settle work relative to fixed Blender mesh round-trip cost.
- When built with CUDA (`YSC_ENABLE_CUDA`), coloured material projections can
  run on the GPU (sm_89/sm_120). Auto-select uses CUDA when edge count ≥ 20k;
  smaller meshes stay on OpenMP. Override with env `YSC_FORCE_MATERIAL=cuda|cpu`.
- Uncaptured seams close 16 mm per substep (constant kinematic drag, no momentum).
- Only a non-finite returned state causes click rollback; finite displacement is
  unrestricted.
- Update recuts meshes from stable panel labels.
- Kitsuke is dressing, not a physics showcase. Advancing the cloth is one short
  step between deliberate placements by the operator, so the solver stays the
  minimum needed to settle a panel: authored rest lengths, the square-cell
  metric, weak axial bending, and Body contact. Anything reached for from a
  general soft-body vocabulary is out of scope until dressing itself requires it.
- `i18n.py` holds the N-panel's Japanese translation dictionary, registered
  under the add-on package name. Operator button labels resolve in the
  `Operator` context and panel headings, property names, and plain labels in
  the default `*` context, so a shared string is registered under both. English
  source strings stay the identifiers; Blender's interface language selects the
  translation.

Only explicit requirements authorize behavior. Do not infer shape, fit, volume,
or Body-relative placement from names, topology, screenshots, or prior work.

## Build

The extension and native project versions are defined in
`blender_manifest.toml` and `CMakeLists.txt`.

```powershell
.\build_native.ps1 -Configuration Release
```

### OpenMP colouring (0.8.2+) and RTX auto-contact (0.9.2 / API 9)

The native square-lattice solver parallelises material projections with OpenMP.

- At create time the solver greedily colours seams, edges, quads, and bends so
  that constraints of one colour never share a vertex.
- Each colour is projected with OpenMP; one parallel team is kept across
  colours in a family to avoid repeated MSVC fork/join.
- Colours stay sequential (Gauss-Seidel across colours, independent within a
  colour). Integrate, finish, and auto contact apply are vertex-parallel.
- Thread count is the usual OpenMP control: `OMP_NUM_THREADS` (and the MSVC
  runtime `vcomp140.dll` already shipped under `bin/`).

**API 9 / `YSC_BODY_CANDIDATES_AUTO`:** Body nearest-face queries run inside
the DLL on a host AABB BVH built at create time. The Python
`bvh.find_nearest` loop and Blender `BVHTree` construction are no longer on
the product click path. Kitsuke passes `body_candidates=None` so the solver
gathers once per substep and reuses faces for later contact passes.

Colouring changes the pure serial Gauss-Seidel update order: neighbouring
constraints only see updates from earlier colours. Settled poses can differ
from pre-0.8.2 builds even at the same iteration count. With correct
colouring, different thread counts should match each other; if they disagree,
colouring is incomplete. `tests/openmp_colour_smoke.py` checks that property
on a small hanging lattice.

There is no broad product test suite. Older suites asserted values from
superseded designs — a 5 mm lattice, a Finished Garment operator, an SVG input
path — so they blocked corrections instead of catching regressions. Write new
tests against the code as it is when they are needed, and delete them again
rather than let them drift.

`blender_manifest.toml` `[build] paths` is the authoritative file list: current
source, documentation, `bin/yohsai_solver.dll`, and `bin/vcomp140.dll`.
Wheels come from the separate `wheels` key.
Build directories, caches, temporary files, local PDFs, and earlier ZIPs are
excluded. Deleting a documentation file requires removing its `paths` entry too,
or the build fails on the missing path.
