# Open PPF hand-off issues

Status: known defects, none fixed

Getting a garment that the external contact solver accepts is the hard part of
this program. A mesh exported from other garment software is normally rejected
on topology, so `Prepare for ZOZO` exists to build a shell that passes instead
of repairing one that does not.

The defects below were found by reading the code, not by running the solver.
**Do not fix them from the code alone.** The solver is sensitive, its rejection
messages are the only ground truth, and a change that looks correct here can
move the failure somewhere else. Fix each one while an actual pattern is being
handed off and the solver is reporting what it thinks of the result.

Every item names the file and line as of the commit that added this document.

## 1. The self-intersection result is computed and thrown away

`_resolve_self_intersections` smooths the shell until no intersection remains,
capped at 40 passes, and returns how much is still unresolved
(`zozo_handoff.py:346`). The caller ignores the return value:

```python
# zozo_handoff.py:552
_resolve_self_intersections(cloth, bvh)
```

`ZozoPreparation` has no field for it and the status line reports only the
stitch count and the minimum opening:

```
Prepared 532 ZOZO stitches (minimum 2.21 mm)
```

So when 40 passes are not enough, Yohsai reports success and hands over a shell
the solver will reject. The failure then surfaces as a session-creation error
with no indication that Yohsai already knew.

This is the most important item. It is not that the resolver is wrong — it is
that its verdict is discarded.

Deciding between a warning and a hard failure needs a real run. A warning keeps
the option of finishing the last few folds by hand; a hard failure never hands
over a shell that is known to be inadmissible. Pick one while watching what the
solver actually does with a marginal shell.

## 2. The returned number is not what the docstring says

The docstring promises "the number of intersections still present"
(`zozo_handoff.py:254`), but the value returned is a count of vertices involved
in intersections:

```python
# zozo_handoff.py:339
remaining = len(involved_vertices(coords))
```

One intersection contributes several vertices, so the number is larger than the
docstring implies. Whatever item 1 decides to display must not repeat this
confusion. Note that the 526 quoted in `KITSUKE_DESIGN.md` came from this same
measurement, so it is a vertex count as well.

## 3. The body clamp runs after the loop and nothing re-solves it

The convergence loop ends at `zozo_handoff.py:326`. The fallback that pushes any
vertex still inside the body back onto its surface runs afterwards
(`zozo_handoff.py:328-332`), and the smoothing never runs again.

`remaining` is measured after the clamp (`zozo_handoff.py:339`), so an
intersection the clamp introduces is at least counted. Nothing resolves it. On
the reference garment this has not been observed to matter; on a garment that
sits closer to the body it could be the difference between passing and failing.

Whether this needs its own repair pass, or whether the clamp should simply run
inside the loop, should be decided from a case that actually exhibits it.

## 4. Nothing records how close the resolver came to its cap

The loop runs up to 40 passes and breaks early on success
(`zozo_handoff.py:311-314`), but the number used is not recorded. A garment that
converges on pass 39 looks exactly like one that converges on pass 3, and the
first is one pattern edit away from item 1.

Reporting the pass count alongside the result of item 1 would make the margin
visible. This is a diagnostic, not a defect, and is only worth doing if items 1
to 3 leave room for it.

## Not a defect, recorded to prevent a wrong assumption

Deleting the `Finished Garment` output removed manifold, duplicate-face and
orientation checks along with it. Those validated a **welded** mesh. The
hand-off shell is not welded: panels stay as separate islands joined by loose
stitch edges, so those checks do not apply to it and the hand-off path did not
lose any guarantee it previously had.
