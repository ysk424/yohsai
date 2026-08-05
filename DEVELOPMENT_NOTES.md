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
- Existing Lock is event-driven. Load and turning it on lock PLACED/DONE and
  unlock PENDING; turning it off unlocks non-placed parts. Select Lock is a
  button that toggles Lock on the selection. Both may be off; both must not be
  on. GRAVITY completion never changes Lock by itself.
- The product path always uses the native Square-Lattice solver. Normal GRAVITY
  uses 16 material iterations; Zero GRAVITY uses 24 (1.5x) so one press does
  more settle work relative to fixed Blender mesh round-trip cost.
- When built with CUDA (`YSC_ENABLE_CUDA`), coloured material projections can
  run on the GPU (sm_89/sm_120). Auto-select uses CUDA when edge count ≥ 20k;
  smaller meshes stay on OpenMP. Override with env `YSC_FORCE_MATERIAL=cuda|cpu`.
- Uncaptured seams close 28 mm per substep (constant kinematic drag, no momentum).
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

### Parallel solver

The native square-lattice solver parallelises material projections over a
constraint colouring; `SOLVER_DESIGN.md` owns that contract. Operational notes:

- Thread count is the usual OpenMP control: `OMP_NUM_THREADS` (and the MSVC
  runtime `vcomp140.dll` already shipped under `bin/`).
- One parallel team is kept across the colours of a family to avoid repeated
  MSVC fork/join.
- With correct colouring, different thread counts must match each other; if
  they disagree, colouring is incomplete. `tests/openmp_colour_smoke.py`
  checks that property on a small hanging lattice.
- **Body contact (auto BVH)** can run on CUDA when a device is present: the
  static Body BVH is uploaded once at solver create (`body_contact_cuda.cu`).
  Cloth vertices gather nearest faces and project the thickness shell on the
  GPU. OpenMP host BVH remains the fallback. Force material path with
  `YSC_FORCE_MATERIAL=cuda|cpu`. With CUDA contact available, material CUDA
  is preferred even below the former 20k-edge gate so positions stay device-
  resident across contact passes.

### Hardware target and measured cost

Private branch target: Ryzen 9 9950X3D (32 threads) + RTX 5070 Ti (sm_120,
16 GB, CUDA 12.9), Windows x64. Reference scene OMOTE+URA, cloth ~5.5k verts,
~11k edges, 120 seams, CC_Base_Body 225k verts, Zero GRAVITY ×15:

| Path | Steady click mean | First click |
| --- | ---: | ---: |
| Python BVH broadphase + OpenMP | ~71 ms | ~1.04 s (Blender BVH) |
| Native BVH auto + OpenMP team | ~43 ms | ~0.61 s (native BVH build) |

Material Gauss-Seidel is still ~30 ms of a steady click, so it is the next
lever when panel counts grow. Interactive target is a click that feels like a
button press rather than a progress bar.

There is no broad product test suite. Write new tests against the code as it is
when they are needed, and delete them again rather than let them drift.

`blender_manifest.toml` `[build] paths` is the authoritative file list: current
source, documentation, and the shipped DLLs under `bin/`. Wheels come from the
separate `wheels` key. Build directories, caches, temporary files, local PDFs,
and earlier ZIPs are excluded. Deleting a documentation file requires removing
its `paths` entry too, or the build fails on the missing path.
