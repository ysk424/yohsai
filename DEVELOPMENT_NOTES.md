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
- Finished Garment is the general-purpose, Body-, rig-, and platform-independent
  output. It creates a new world-space snapshot, welds only the exact completed
  sewing classes, and preserves material, UV, color, smoothing, sharp-edge, and
  valid custom-normal data without modifying Yohsai source parts.
- A face collapsed completely by an exact multi-part seam junction is omitted
  and reported. Every remaining face must still pass the zero-area, duplicate,
  manifold, and orientation checks before the snapshot is committed.

Only explicit requirements authorize behavior. Do not infer shape, fit, volume,
or Body-relative placement from names, topology, screenshots, or prior work.

## Build

The extension and native project versions are defined in
`blender_manifest.toml` and `CMakeLists.txt`.

```powershell
.\build_native.ps1 -Configuration Release
python -m unittest discover -s tests -p "test_*.py"
ctest --test-dir build -C Release --output-on-failure
```

The release archive contains current source, documentation, the bundled PDF wheel,
`bin/yohsai_cosserat.dll`, and `bin/vcomp140.dll`. Build directories, caches,
temporary files, local PDFs, and earlier ZIPs are excluded.
