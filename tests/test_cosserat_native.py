# SPDX-License-Identifier: GPL-3.0-or-later
"""Host-Python tests for the square-lattice cloth native DLL bridge."""

from __future__ import annotations

from types import SimpleNamespace
import unittest

import numpy as np

import cosserat_native


@unittest.skipUnless(cosserat_native.native_library_available(), "Native solver DLL is not built")
class NativeSolverBridgeTests(unittest.TestCase):
    @staticmethod
    def empty_body():
        return SimpleNamespace(
            vertices=np.empty((0, 3), dtype=np.float32),
            faces=np.empty((0, 3), dtype=np.int32),
        )

    @staticmethod
    def empty_topology():
        return SimpleNamespace(
            edges=np.empty((0, 2), dtype=np.int32),
            edge_rest_lengths=np.empty(0, dtype=np.float32),
            quads=np.empty((0, 4), dtype=np.int32),
            quad_rest_metrics=np.empty((0, 3), dtype=np.float32),
            bends=np.empty((0, 3), dtype=np.int32),
            bend_rest_lengths=np.empty((0, 2), dtype=np.float32),
        )

    def runtime(self, positions, *, locked=None, seams=None):
        positions = np.asarray(positions, dtype=np.float32)
        count = len(positions)
        return cosserat_native.NativeCosseratRuntime(
            positions,
            np.zeros_like(positions),
            np.empty((0, 2), dtype=np.int32) if seams is None else np.asarray(seams, dtype=np.int32),
            self.empty_topology(),
            self.empty_body(),
            np.zeros(count, dtype=np.int32) if locked is None else np.asarray(locked, dtype=np.int32),
        )

    def test_state_has_no_hidden_force(self):
        authored = np.asarray(
            ((0, 0, 0), (1.3, 0.2, 0.1), (1.1, 0.4, 1.4), (-0.2, -0.1, 0.9)),
            dtype=np.float32,
        )
        runtime = self.runtime(authored)
        try:
            self.assertEqual(runtime.vertex_count, 4)
            self.assertEqual(runtime.seam_count, 0)
            runtime.advance(np.empty((0, 2), np.int32), 0.0, 64)
            positions, velocities = runtime.state()
            np.testing.assert_array_equal(positions, authored)
            np.testing.assert_array_equal(velocities, 0.0)
            self.assertEqual(int(runtime.last_stats["seam_count"]), 0)
            self.assertEqual(int(runtime.last_stats["body_candidate_count"]), 0)
        finally:
            runtime.close()

    def test_locked_vertices_can_be_unlocked_for_gravity(self):
        runtime = self.runtime(((0, 0, 0), (1, 0, 0)), locked=(1, 1))
        try:
            before, _ = runtime.state()
            runtime.replace_state(before, np.zeros_like(before), np.zeros(2, dtype=np.int32))
            runtime.advance(np.empty((0, 2), np.int32), 1.0, 2)
            after, _ = runtime.state()
            self.assertLess(float(after[:, 2].mean()), float(before[:, 2].mean()))
        finally:
            runtime.close()

    def test_seam_target_is_fixed_at_zero(self):
        runtime = self.runtime(((0, 0, 0), (2, 0, 0)), seams=((0, 1),))
        try:
            runtime.advance(np.empty((0, 2), np.int32), 0.0, 1)
            pulled, _ = runtime.state()
            self.assertLess(float(np.linalg.norm(pulled[1] - pulled[0])), 2.0)
            np.testing.assert_array_equal(runtime.seam_state(), (0.0,))
        finally:
            runtime.close()

    def test_seam_attraction_is_constant_and_near_pairs_capture(self):
        reductions = []
        for initial_distance in (0.5, 0.45):
            runtime = self.runtime(((0, 0, 0), (initial_distance, 0, 0)), seams=((0, 1),))
            try:
                runtime.advance(np.empty((0, 2), np.int32), 0.0, 1)
                solved, _ = runtime.state()
                reductions.append(
                    initial_distance - float(np.linalg.norm(solved[1] - solved[0]))
                )
            finally:
                runtime.close()
        self.assertGreater(reductions[0], 0.0)
        self.assertAlmostEqual(reductions[0], reductions[1], places=6)

        runtime = self.runtime(((0, 0, 0), (0.05, 0, 0)), seams=((0, 1),))
        try:
            runtime.advance(np.empty((0, 2), np.int32), 0.0, 1)
            solved, _ = runtime.state()
            self.assertLess(float(np.linalg.norm(solved[1] - solved[0])), 1.0e-7)
            self.assertEqual(int(runtime.last_stats["captured_seam_count"]), 1)
        finally:
            runtime.close()


if __name__ == "__main__":
    unittest.main()
