# Seam Boundary Layer (Paving Band) Design

Status: **draft specification — initial implementation in progress**

Implemented so far (Load / remesh mesh path in `mesh_loader.py`):

- band width **`SEAM_BAND_WIDTH_M = 0.01` (10 mm)**;
- one-layer proximity companions on sewing-related outline vertices;
- point attribute `yohsai_vertex_kind` (**0=N, 1=E, 2=P**);
- edge attribute `yohsai_shortenable` on the outer sewing row.

Not yet wired: A/B body clearances, ease-vs-stitch E split in pairing, shortenable
use in Kitsuke/GRAVITY.

Related: `GRAINLINE_DESIGN.md`, `KITSUKE_DESIGN.md`, `zozo_handoff.py` (current
hand-off path remains authoritative until this design ships).

## 1. Purpose

Yohsai’s in-editor cloth step (Kitsuke / GRAVITY) may stay comparatively simple.
ZOZO Contact Solver (ppf) must still receive a mesh that:

1. sews cleanly with **matched stitch topology**;
2. has a **uniform seam-allowance band** (equal width);
3. places gather / ease fullness so that **self-intersections are rare** at rest;
4. keeps the **interior grain lattice** intact for material metrics.

This document specifies a **one-layer paving-style boundary layer** on sewing
edges, plus a **three-way vertex classification**, so that a crude local sim can
still hand ZOZO a well-structured shell.

**Out of scope for this draft:** shipping code, DLL choice, and replacing the
existing self-intersection unfold path. Implementation proceeds only after the
authored mesh can be **visually validated**.

## 2. Problem statement

### 2.1 Gather sewing (motivating case)

Sewing a long free edge onto a short free edge (e.g. sleeve cap ~13 cm into
armhole ~5 cm) forces excess length into folds. Today, equalizing vertex counts
and closing pairs 1:1 tends to crumple the long side **into itself and into the
body**, which produces rest self-intersections that ZOZO rejects.

The excess is a property of the **long side’s fold path**, not of missing
filler triangles between panels. Yohsai must **not** insert patch faces into
gaps between sewn panels. Topology stays multi-island panels joined by loose
stitch edges (same hand-off philosophy as today).

### 2.2 What must improve before ZOZO

| Failure mode | Mitigated by this design? |
|--------------|---------------------------|
| Unmatched or uneven stitch sampling | Yes — edge vertices aligned |
| Sliver / irregular band near the seam | Yes — equal-width paving strip |
| Gather crumple against the body | Partially — A/B placement + ease |
| Deep drape self-folds far from the seam | No — still ZOZO / later resolve |

## 3. Design goals

1. **Match stitch counts** on partner sewing chains (1:1 stitch pairs).
2. **Equal band width** along each sewn edge (default discussion value: **1 cm**
   while the design is simplified; product width may later move to 5 mm).
3. Confine **length absorption (gather)** to **shortenable edges on the outer
   stitch row**, not to the interior grain metric.
4. Classify vertices so contact targets differ: seam band near the body,
   interior slightly farther.
5. Keep interior **grainline square lattice** (see `GRAINLINE_DESIGN.md`) for
   Kitsuke stretch / shear / bend.
6. Prefer a mesh that **looks correct by eye** before investing in solver
   complexity; refine after visual review.

Non-goals:

- Full-domain Q-morph or Blossom-Quad remeshing of entire panels.
- Embedding Gmsh / pygmsh in the add-on for production.
- Perfect local cloth dynamics; ZOZO owns high-quality drape after Transfer.

## 4. Geometry: one-layer paving band

### 4.1 Why paving

Paving (here: a **single regular strip** along the sewing edge) is required
because the contract needs **both**:

- equal **counts** of stitch points on the outer row, and  
- equal **width** of the allowance band.

Arc-length resampling of the outline alone does not define a band width or a
stable proximity row. A one-layer strip supplies:

- outer row = edge vertices **E**;
- inner row = proximity vertices **P**;
- everything inward of the strip = normal vertices **N** (grain lattice).

```
        sewing / free edge (outer)
   E0——E1——E2——E3——E4——E5     Edge vertices (stitch row)
   |   |   |   |   |   |
   P0——P1——P2——P3——P4——P5     Proximity vertices (band)
   |   |   |   ...
   N   N   N   ...            Normal vertices (panel interior)
```

### 4.2 Band parameters (discussion defaults)

| Symbol | Meaning | Discussion default |
|--------|---------|-------------------|
| \(w\) | Band width (E–P distance in pattern space) | **0.01 m (10 mm)** |
| \(p\) | Target pitch along the edge | **~0.01 m (10 mm)** |
| short chain example | Armhole-like side | **5 cm → 6 edge vertices** |
| long chain example | Sleeve-cap-like side | **13 cm → denser outer sampling (e.g. ~14)** with **6 stitch partners** |

Exact product constants are not frozen until visual trials.

### 4.3 Construction (normative intent)

For each sewing-relevant boundary chain on a panel:

1. Sample or densify the **outer** curve so pitch is approximately \(p\).
2. Build an **inward offset** curve at distance \(w\) in pattern space
   (corner policy: miter or round — choose one per implementation note after
   visual tests; must not reverse the strip).
3. Connect outer samples to offset samples as a **single strip** of quads
   (or two triangles per cell). This is the paving layer.
4. Fill the region inside the offset with the **existing grain lattice**
   (10 mm / 5 mm rules unchanged), with a transition band if needed.
5. Do **not** add faces that bridge two different panel islands.

If panel width is less than roughly \(2w\), the band must shrink or be skipped
for that edge (fallback: document in implementation notes after trials).

### 4.4 Gather on the long side

When a long chain is sewn to a short chain:

- The **short** side has \(n\) edge vertices (example: \(n = 6\)).
- The **long** side keeps enough samples to represent material length
  (example: ~14 points at ~1 cm pitch on a 13 cm edge).
- Exactly **\(n\) stitch partners** on the long side are chosen by stable
  arc-length correspondence (endpoints included when the chain is open and
  endpoints are sewn). Correspondence is **fixed** (not rewired by nearest
  neighbor each frame).
- **Outer edges between successive edge vertices on the stitch row** may
  **shorten** so the stitch polyline can meet the short side length.
- Material edges that are not marked shortenable keep their rest length
  (see §5).

Excess vertices on the long outer row that are not stitch partners represent
**ease / gather**. They must not be forced into the body; see §5.3.

## 5. Vertex classification

Vertices are partitioned into **three kinds**. **P and N remain distinct**
(do not merge proximity into normal). Visual review may later subdivide edge
vertices into stitch vs ease; that is optional refinement, not a fourth
required kind in this draft.

### 5.1 Edge vertices (E)

- Lie on the **outer** sewing row of the paving band (or on a free edge that
  participates in sewing).
- Participate in **stitch pairing** when selected as partners (1:1 with the
  other side’s E set of equal count).
- **Property — shortenable row:** edges connecting consecutive **stitch-role**
  edge vertices along the outer sewing polyline may reduce rest / target length
  so gather is absorbed **on the stitch line**, not by changing interior grain
  rest lengths.
- **Property — body distance A:** stitch-role edge vertices target clearance
  **A** from the body collider (see §6).

### 5.2 Proximity vertices (P)

- Form the **inner row** of the paving band (and, if a future multi-layer band
  is used, vertices that are **edge-connected “vertically”** toward the
  interior from E within the band).
- Are **not** stitch endpoints.
- Keep band width: edges E–P (width direction) are **not** shortenable under
  the default contract.
- **Property — body distance A:** same target clearance **A** as stitch-role E,
  so the whole allowance band can sit in the near-contact shell.

### 5.3 Normal vertices (N)

- All remaining panel vertices (grain lattice interior, non-seam free edges
  that are not part of this band, etc.).
- Material rest lengths follow the existing grainline / edge-family rules.
- **Property — body distance B** with **A < B**, so the interior stands slightly
  farther from the body than the seam band.

### 5.4 Ease on the long outer row (normative warning)

If the long outer row has more samples than stitch partners, the **non-stitch
outer samples** still sit on the edge polyline but **must not** be treated as
“force to clearance A against the body.” Doing so re-introduces the sleeve
crumple. They should be placed **away from the body** (air side / outward
along an appropriate normal) so gather folds form outside the collider.

Optional later label: `stitch-E` vs `ease-E`. Until then, implementation must
still obey this placement rule even if both are stored as kind E.

### 5.5 Summary table

| Kind | Role | Shortenable incident edges | Body clearance target |
|------|------|----------------------------|------------------------|
| **E** (stitch-role) | Stitch 1:1 partners | Outer E–E along sewing row: **yes** | **A** |
| **E** (ease / surplus) | Gather on long side | May sit on shortenable row; not a stitch partner | **Not A** — bias **outward** |
| **P** | Band / proximity row | Width E–P: **no** (default) | **A** |
| **N** | Interior / grain | Per existing material rules | **B**, with **A < B** |

## 6. Clearances A and B

- **A**: near-contact distance for the seam band (stitch-role E and P). Intended
  to sit in the same regime as ZOZO contact gap thinking (order of millimetres;
  exact value chosen with visual + hand-off tests).
- **B**: interior standoff, **strictly greater than A**, so bulk cloth is less
  likely to dig into the body under a crude local sim.
- “Quasi-penetration” in design discussion means: **no true intersection**, but
  band vertices may sit at near-contact (clearance ≈ A). Cloth–cloth
  self-intersection remains **forbidden** for ZOZO Transfer.

Signed distance is measured from the body surface (outward positive). Targets
may be soft (potential) or hard (projection); the contract is the **ordering**
A < B and the **assignment** of kinds, not a specific solver method.

## 7. Material and stitch contract

1. **Stitch topology:** only pairs of stitch-role **E** vertices across panels
   (or across composite open chains for RING-style sewing). Counts match; pairing
   index is stable.
2. **Shortenable:** outer sewing-row edges between consecutive stitch-role E
   (and the gather mechanism that lets a long polyline meet a short one). All
   other rest lengths follow grainline rules unless an existing transition
   edge type already allows adaptation.
3. **Band width:** E–P edges target length ≈ \(w\) (equal width).
4. **No gap-filling faces** between panels; islands remain separate; ZOZO loose
   stitches open as in the current hand-off philosophy unless superseded later.
5. **Interior grain:** N-region quads / proxy triangles continue to feed Kitsuke
   as today (`yohsai_grainline_*` attributes).

## 8. Pipeline placement (when this applies)

| Stage | Responsibility under this design |
|-------|----------------------------------|
| Load / remesh | Build paving band; tag E / P / N; preserve grain interior |
| Sewing plan | Match stitch-role E counts; fix pairing; mark shortenable row edges |
| Local pose / GRAVITY (optional) | Respect A/B and shortenable row; need not be a high-end cloth sim |
| Visual check | **Gate for implementation quality** — inspect band, gather, clearances |
| Prepare for ZOZO | Export triangulated shell; stitches on E; keep non-intersection |
| ZOZO Transfer / Run | High-quality contact and drape |

Existing residual-pinch welding and self-intersection smoothing remain possible
fallbacks until this mesh structure proves sufficient by eye and by ZOZO.

## 9. Implementation strategy (deferred)

**Do not implement in the same change as this document.** After the spec is
accepted:

1. Prototype band construction (likely Python in `mesh_loader` or a dedicated
   module) with attributes for vertex kind.
2. **Visual validation** of width, pairing, and gather placement on reference
   garments (sleeve ease first).
3. Only then wire shortenable edges and A/B into local sim or pure kinematic
   placement.
4. External C++ DLL / process / Gmsh are **not required** for the band itself;
   reconsider only if robust offset or large-scale reconnection becomes the
   bottleneck.

Research notes (non-normative): full Q-morph is a poor product fit (endpoint /
singularity cost, no grain contract). Blossom-Quad via Gmsh is fine for offline
A/B comparisons but too heavy to embed. The product path is **local one-layer
paving**, not global remeshing.

## 10. Acceptance criteria (before calling the feature done)

1. Partner sewing chains expose the **same number** of stitch-role E vertices
   with stable pairing.
2. Along each band, width is **visually uniform** (~ \(w\)).
3. E, P, and N are **distinguishable** in data (attribute or equivalent).
4. On a long-to-short gather, surplus outer samples are **not** crushed into the
   body; fullness reads as outside / air-side.
5. Interior grain lattice away from the band remains usable for Kitsuke metrics.
6. A reference Prepare → ZOZO path shows **fewer** seam-local self-intersections
   than the pre-band mesh (quantitative bar set during trials).

## 11. Open decisions (resolve during visual trials)

- Final \(w\) (1 cm discussion default vs 5 mm product target).
- Final A, B in metres and soft vs hard enforcement.
- Corner offset policy (miter vs round).
- Whether multi-layer vertical P chains are ever needed (default: **one** P row).
- Whether to store `stitch-E` / `ease-E` as separate labels.
- Interaction with RING composite open/closed sewing alignment.
- Whether A/B is applied only at hand-off, or continuously in GRAVITY.

## 12. Document history

| Date | Note |
|------|------|
| 2026-08-03 | Initial draft from design discussion: paving band, E/P/N, shortenable outer row, clearances A < B, gather ease placement; implementation deferred pending visual review. |
| 2026-08-03 | Product band width fixed at **10 mm**. Mesh load writes `yohsai_vertex_kind` and `yohsai_shortenable`; A/B and ease pairing still open. |
