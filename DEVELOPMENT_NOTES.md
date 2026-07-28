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
- The product path always uses the native Square-Lattice solver at 20 iterations.
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

### OpenMP colouring (0.8.2+)

The native square-lattice solver parallelises material projections with OpenMP.

- At create time the solver greedily colours seams, edges, quads, and bends so
  that constraints of one colour never share a vertex.
- Each colour is projected with `#pragma omp parallel for`; colours stay
  sequential (Gauss-Seidel across colours, independent within a colour).
- Body-candidate accumulation stays serial (candidates can share a cloth
  vertex). Integrate, finish, and contact apply are vertex-parallel.
- Thread count is the usual OpenMP control: `OMP_NUM_THREADS` (and the MSVC
  runtime `vcomp140.dll` already shipped under `bin/`).

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
source, documentation, `bin/yohsai_solver.dll`, `bin/vcomp140.dll`, and
`bin/libyohsai_solver.dylib`. Wheels come from the separate `wheels` key.
Build directories, caches, temporary files, local PDFs, and earlier ZIPs are
excluded. Deleting a documentation file requires removing its `paths` entry too,
or the build fails on the missing path.
