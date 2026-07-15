# Yohsai Session Memory

Recorded: 2026-07-15 (Asia/Tokyo)

## Current contract

- Sewing supplies exact seam connectivity only.
- Kitsuke starts from positioned source-panel vertices.
- Seam targets are fixed at zero and never shorten per click.
- Before 2 mm capture, seam-force magnitude is independent of pair distance.
- Seam attraction is 300 force units; final material sweeps prevent seam-edge tearing.
- Pattern rest lengths connect ordinary vertices mechanically.
- Square cells use their authored 2D shear metric; proxy diagonals carry no force.
- Straight warp/weft triples provide only weak zero-curvature bending.
- No material term reads Body geometry, Body normals, or bones.
- Body geometry enters only through collision candidate lookup and contact.
- Self-contact and Body-relative shape matching are absent.
- Gravity is a per-click N-panel input: positive values act in world -Z, default
  1.0 m/s², and zero disables gravity without resetting live state.
- Finite per-click movement has no rollback threshold; only non-finite state is
  rolled back.

## Interpretation rule

Implement only explicitly requested behavior. Never infer garment shape, fit,
volume, or Body-relative placement.

## Verification

Native and Blender tests cover fixed seam targets, constant-force equality at
50 cm and 5 cm, 2 mm capture, rigid-transform/rest
invariance, edge load transmission, quad shear reduction, axial bend reduction,
Body-candidate-only contact, per-click gravity changes including 1→10 and
0→10, Undo/Redo reconstruction, and full pattern data.

The 2026-07-15 final run passed:

- native CTest and seven Python unit tests;
- the full Blender Load/Sewing/Kitsuke/Update integration check, including
  gravity `1→10→0→10` and a separate `0→10` session;
- Blender Undo/Redo reconstruction;
- the four-part sleeve regression check;
- Blender extension archive validation.

## Release

Current release: `yohsai-0.5.10.zip` (85,722,497 bytes).

SHA-256: `1E2280B8905FC77B6D6721127626B3EBA19D1E4005A00F7BC87231506B6B4E8C`.

The archive contains 40 entries and its bundled native DLL matches
`bin/yohsai_cosserat.dll`. Keep current source, manifest, dependencies, native
binaries, licenses, and current documentation. Exclude build output, caches,
temporary parser output, local PDFs, and older archives from future ZIPs.
