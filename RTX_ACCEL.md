# Yohsai-RTX Acceleration Plan

Status: branch charter for private Windows x64 + RTX 5070 Ti

Base: `main` @ `aa7ef65` (Yohsai 0.9.1 square-lattice OpenMP solver)

This branch exists to make GRAVITY / Kitsuke **fast enough to use interactively**
on a Ryzen 9 9950X3D (32 threads) + **RTX 5070 Ti (sm_120, 16 GB, CUDA 12.9)**.
It is private. License policy is not a constraint. Past product restraint
(minimal solver, CPU-only, serial body candidates) is **not** binding when it
costs speed.

The generic `main` / OpenMP build remains the portable product path. This branch
may diverge hard.

## Why OpenMP (even with CUDA toolkit on the host) is not enough

Current native path (`native/src/solver.cpp`):

| Stage | Parallelism today | Why it caps out |
| --- | --- | --- |
| Constraint colouring | Once at create | Good idea; reusable on GPU |
| Seam / edge / quad / bend project | OpenMP **inside each colour** | Colours stay **serial** (GS across colours). Lattice colour diameter keeps the critical path long. |
| Integrate / finish | Per-vertex OpenMP | Cheap; not the bottleneck |
| Body contact apply | **Serial** candidate walk | Candidates can share a vertex; no atomics / no colouring |
| Body candidate gather | **Python** `bvh.find_nearest` per vertex (`kitsuke.py`) | Interpreter + per-hit Blender BVH; runs every advance, before native code |

MSVC OpenMP + coloured GS is the right *CPU* design for determinism. It will not
saturate a 5070 Ti, and it will not remove the Python broadphase or serial
contact walk. Selecting a CUDA compiler for the same OpenMP source is not a plan.

## Hardware target

- CPU: AMD Ryzen 9 9950X3D — host prep, topology build, optional hybrid fallback
- GPU: GeForce RTX 5070 Ti, compute capability **12.0**, driver 596.x
- Toolchain already on machine: **CUDA 12.9** (`nvcc`), CMake, VS 2022
- Local assets that may be reused (private): OptiX 9.1 SDK, ageha ADMM-RS CUDA
  patterns (`sm_120-real`), ppf-contact-solver stack

## Performance model (what must move)

One GRAVITY click roughly does:

```
Python: mesh/world xform → candidates (BVH loop) → ctypes advance
Native: for substep in 8:
          seam attract
          integrate
          for iter in 16:
            seams, shear, bends, edges×2, (every other) body contact
          finish
Python: write-back positions
```

To win big, **state must stay on device** for the whole click (ideally across
repeated clicks). Host↔device only for operator-visible write-back and topology
changes (recut / sewing rebuild).

Rough cost order after OpenMP colouring on a full kimono-scale lattice:

1. Edge projections (most constraints × colours × iters × substeps)
2. Python body candidate loop
3. Body contact + shear/bend
4. Integrate / seam attract

Phase work below follows that order.

## Strategy (not bound to current algorithm purity)

### Phase 0 — Baseline (same day)

- Instrument wall time: candidate gather vs `ysc_advance` vs Blender write-back
- Log vertex / edge / quad / bend / candidate counts and colour counts
- Freeze a representative scene as a non-Blender bench harness (numpy dump +
  native advance) so GPU work is not gated on Blender

### Phase 1 — CUDA coloured XPBD (algorithm-compatible)

Keep the square-lattice energy and colour schedule; move data + kernels to GPU.

- SoA device buffers: `pos`, `prev`, `vel`, `inv_mass`, `locked`
- Device constraint tables + precomputed colour offsets (CSR-like)
- Kernels per colour class for: distance edges, seam (captured), quad shear,
  axial bend, seam attract, integrate, finish
- Body contact: per-candidate Jacobi accumulate with atomics **or** colour
  candidates by cloth vertex (same idea as material colouring)
- C ABI: keep `ysc_*` surface if possible; add `ysc_create_cuda` / backend flag
- CMake: `LANGUAGES CXX CUDA`, `CUDA_ARCHITECTURES 120-real` (plus 89 if useful)
- Ship `cudart` beside the DLL for portable Blender install

Expected: large win on material iterations once colours are wide; still limited
by colour depth on long strips.

### Phase 2 — Contact off the Python critical path

Replace `kitsuke._body_collision_candidates` per-vertex Python BVH with one of:

1. **CUDA** AABB broadphase + triangle distance (body mesh on device once)
2. **OptiX** closest-hit / GAS over body triangles (SDK already local)
3. Hybrid: host BVH only when GPU unavailable

Prefer (1) or (2) with candidates produced **inside** the native advance so
Python never walks vertices for collision.

### Phase 3 — Algorithm change if coloured GS still stalls

If colour diameter or iteration count remains the wall after Phase 1–2:

| Option | Fit | Notes |
| --- | --- | --- |
| Full Jacobi / weighted Jacobi + more iters | Easy | Same constraints; more parallel; may need param retune |
| Chebyshev / adaptive ω on projections | Medium | Faster convergence without new energy |
| **VBD** (vertex block descent) | Strong GPU | Highly parallel; different residual path |
| Ageha-style **ADMM / CPRS** CUDA | Strong | Already sm_120 patterns in-repo; heavier product change |
| External cloth (e.g. Warp, custom XPBD libs) | Optional | Private OK; evaluate only if custom path stalls |

Default preference: finish Phase 1–2 first (same look, same energy), then VBD or
Jacobi acceleration before swapping the whole energy model.

### Phase 4 — Host / product integration

- Backend enum: `cpu_openmp` | `cuda` (default `cuda` on this branch when device present)
- Fallback to OpenMP if no CUDA device
- `OMP_NUM_THREADS` still controls CPU path; CUDA path ignores it
- Manifest ships CUDA runtime (+ OptiX if used)
- Drop macOS / multi-platform concern (already Windows-only product)

## Explicit non-goals for this branch

- Preserving bit-identical poses vs OpenMP colouring order
- Shipping a license-clean redistributable for third parties
- Keeping Body-candidate generation in Python “because it worked”
- OpenMP-target offload as the primary GPU strategy

## Directory sketch (planned)

```
native/
  src/
    solver.cpp          # CPU OpenMP reference (kept)
    solver_cuda.cu      # device advance
    contact_cuda.cu     # body candidates + contact
    colouring.cpp       # shared host colouring
  include/yohsai_solver/c_api.h
tools/
  bench_advance.py      # dump-driven timing without Blender UI
RTX_ACCEL.md            # this file
```

## Acceptance (interactive)

On the operator’s usual full-pattern GRAVITY click:

- Candidate + advance + write-back wall time in a range that feels like a button
  press, not a progress bar (target: **&lt; ~200 ms** click for typical kimono
  panel counts; stretch goal **&lt; 100 ms**)
- Finite state, no blow-ups; visual dressing quality at least as usable as 0.9.1
- CPU OpenMP path still builds for A/B

Exact vertex budgets TBD after Phase 0 numbers.

## Benchmark log (OMOTE+URA, Zero GRAVITY ×15, CC_Base_Body 225k verts)

Hardware: Ryzen 9 9950X3D + RTX 5070 Ti. Cloth ~5.5k verts, ~11k edges, 120 seams.

| Build | Steady click mean | Candidates | Advance (native) | First click |
| --- | ---: | ---: | ---: | ---: |
| Baseline OpenMP + Python BVH | **~71 ms** | ~29 ms Python | ~34 ms | ~1.04 s (Blender BVH) |
| API 9 native BVH auto + OpenMP team | **~43 ms** | (inside advance) | ~35 ms total | ~0.61 s (native BVH build) |

Pillar 1 (Python BVH) is removed. Pillar 2 material GS is still ~30+ ms of the
click; CUDA projection remains the next lever when panels grow (sleeves/collar).

## Shipped in this branch (API 9)

- `native/src/body_bvh.hpp` — host AABB BVH over body triangles
- `YSC_BODY_CANDIDATES_AUTO` advance mode; Kitsuke no longer builds Blender BVH
- OpenMP parallel team kept across colour classes; fused auto contact project
- Deploy: `bin/yohsai_solver_v9b.dll` (or rebuild `yohsai_solver.dll`)

## Immediate next actions

1. CUDA colour projection for edges/quads/bends (sm_120) — residual pillar 2
2. Faster body BVH build / optional low-poly collision proxy for first click
3. Optional OptiX closest-hit when cloth counts grow past ~20k verts
