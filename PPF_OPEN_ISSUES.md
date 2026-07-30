# Open PPF hand-off issues

Status: items 1–4 addressed in 0.9.4-rtx (self-intersection gate). Further
failures still need a live Transfer rejection as ground truth.

Getting a garment that the external contact solver accepts is the hard part of
this program. A mesh exported from other garment software is normally rejected
on topology, so `Prepare for ZOZO` exists to build a shell that passes instead
of repairing one that does not.

## Fixed in 0.9.4-rtx

### 1. Self-intersection result no longer discarded

`_resolve_self_intersections` returns `SelfIntersectionResult`. The status line
includes `self-intersect: …`, and when residual vertices remain, Prepare raises
`ZozoHandoffError` after writing the best-effort cloth/body copies (for
inspection) and **does not** start ZOZO MCP setup.

### 2. Count is explicitly vertex-based

The field is named `remaining_vertices` and the status text says `verts`, so it
is not confused with intersection-pair counts.

### 3. Body clamp runs inside the resolve loop

Each smooth pass is followed by a body clamp and re-detection on the next
iteration. A post-loop clamp remains as a final safety, then residual is
measured again.

### 4. Pass count is recorded

`passes_used` / `max_passes` appear in the summary and on the cloth custom
properties `yohsai_self_intersect_passes` / `yohsai_self_intersect_remaining`.
Residual vertex indices are stored on `yohsai_self_intersect_residual` when
non-empty.

## Still true

ppf's exact Transfer-time predicates remain the final ground truth. Yohsai's
float BVH tests can clear while Transfer still rejects (or the reverse on rare
near-touches). A Transfer rejection after a clean Prepare is still a real
defect to chase with that mesh in hand.

## Not a defect, recorded to prevent a wrong assumption

Deleting the `Finished Garment` output removed manifold, duplicate-face and
orientation checks along with it. Those validated a **welded** mesh. The
hand-off shell is not welded: panels stay as separate islands joined by loose
stitch edges, so those checks do not apply to it and the hand-off path did not
lose any guarantee it previously had.
