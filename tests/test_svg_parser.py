# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

import yohsai_svg_parser as parser


class SvgParserTests(unittest.TestCase):
    @staticmethod
    def _square_panel(panel_id: str, x: float) -> parser.Panel:
        points = [
            parser.Point(x, 0.0),
            parser.Point(x + 1.0, 0.0),
            parser.Point(x + 1.0, 1.0),
            parser.Point(x, 1.0),
        ]
        return parser.Panel(
            panel_id,
            None,
            [
                parser.Segment("line", points[index], points[(index + 1) % len(points)])
                for index in range(len(points))
            ],
            update_label=panel_id,
        )

    def test_svg_input_is_rejected(self) -> None:
        with self.assertRaisesRegex(parser.ParseError, "must be a .pdf"):
            parser.parse_pattern("input.svg")
        with self.assertRaisesRegex(parser.ParseError, "no longer supported"):
            parser.parse_svg("input.svg")

    @unittest.skipUnless(importlib.util.find_spec("pypdf"), "pypdf is not installed in this test interpreter")
    def test_pdf_comments_and_layer_markers_are_ignored(self) -> None:
        class FakeContentStream:
            def __init__(self, _contents, _reader):
                self.operations = [
                    (["/OC", "/CLOTHES"], b"BDC"),
                    ([], b"BT"),
                    (["//Pattern:"], b"Tj"),
                    (["A"], b"Tj"),
                    ([], b"ET"),
                    ([], b"EMC"),
                    (["/OC", "/ANY_LAYER_NAME"], b"BDC"),
                    ([], b"BT"),
                    (["#PANEL"], b"Tj"),
                    ([], b"ET"),
                    ([], b"EMC"),
                ]

        owner = {"/Type": "/Page", "/Contents": object(), "/Resources": {}}
        paths: list[parser.Subpath] = []
        annotations: list[parser.Annotation] = []
        with patch("pypdf.generic.ContentStream", FakeContentStream):
            parser._pdf_collect_content(
                owner,
                object(),
                parser.Matrix(),
                paths,
                annotations,
            )
        self.assertEqual([annotation.text for annotation in annotations], ["#PANEL"])

    @unittest.skipUnless(importlib.util.find_spec("pypdf"), "pypdf is not installed in this test interpreter")
    def test_supplied_illustrator_pdf(self) -> None:
        source = Path.home() / "Desktop" / "test2.pdf"
        if not source.is_file():
            self.skipTest("The user-supplied Desktop/test2.pdf is not available.")
        document = parser.parse_pdf(source)
        self.assertEqual(document["source"]["input_format"], "pdf")
        self.assertNotIn("clothes_layer", document["source"])
        self.assertEqual(
            [panel["label"] for panel in document["panels"]],
            ["OMOTE_LAWN60", "URA_LAWN60"],
        )
        self.assertNotIn("annotation", document["scale"])
        self.assertNotIn("reference_length_m", document["scale"])
        self.assertNotIn("reference_length_svg", document["scale"])
        self.assertAlmostEqual(document["scale"]["meters_per_svg_unit"], 0.0254 / 72.0)
        self.assertEqual(len(document["sewing_groups"]["A"]), 2)
        self.assertEqual(len(document["sewing_groups"]["B"]), 4)
        self.assertEqual(
            sum(segment["fold"] for panel in document["panels"] for segment in panel["segments"]),
            2,
        )

    @unittest.skipUnless(importlib.util.find_spec("pypdf"), "pypdf is not installed in this test interpreter")
    def test_supplied_ring_sleeve_pdf(self) -> None:
        source = Path.home() / "Desktop" / "test3.pdf"
        if not source.is_file():
            self.skipTest("The user-supplied Desktop/test3.pdf is not available.")
        document = parser.parse_pdf(source)
        self.assertNotIn("clothes_layer", document["source"])
        self.assertEqual([panel["label"] for panel in document["panels"]], ["OMOTE", "URA", "SODE"])
        sleeve = document["panels"][2]
        self.assertTrue(sleeve["mirror"])
        self.assertIsNotNone(sleeve["top"])
        self.assertEqual(sum(segment["ring"] for segment in sleeve["segments"]), 2)
        self.assertEqual(
            [segment["index"] for segment in sleeve["segments"] if segment["sewing_group"] == "C"],
            [3, 4],
        )
        self.assertEqual(len(document["sewing_groups"]["C"]), 4)


if __name__ == "__main__":
    unittest.main()
