# Yohsai Development Notes

Status: current development state

## Architecture

- Illustrator PDF is authoritative for topology and annotations.
- Load creates separate pattern-part meshes.
- Sewing records connectivity from positioned source parts and never queries Body.
- Kitsuke starts from source-part world vertices.
- Seam goals are fixed at zero and do not shorten per click.
- Pattern edges, square metrics, and axial triples provide cloth internal energy.
- Body participates only through contact correction.
- Self-contact and Body-relative rest-shape forces are absent.
- Gravity is read from the N-panel on each click and applied in world -Z.
- Only a non-finite returned state causes click rollback; finite displacement is
  unrestricted.
- Update recuts meshes from stable panel labels.

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

The release archive contains current source, documentation, bundled wheels,
`bin/yohsai_cosserat.dll`, and `bin/vcomp140.dll`. Build directories, caches,
temporary files, local PDFs, and earlier ZIPs are excluded.
