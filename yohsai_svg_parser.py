# SPDX-License-Identifier: GPL-3.0-or-later
"""Standalone Illustrator PDF to Yohsai JSON converter.

PDF parsing uses bundled pypdf.
Blender starts this module in a separate process, so it never imports bpy or
shares Blender state.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


OUTPUT_FILENAME = "yohsai_pattern.json"
TEMP_FILENAME = OUTPUT_FILENAME + ".tmp"
SCHEMA_NAME = "yohsai-pattern"
SCHEMA_VERSION = "1.0.0"


def _add_bundled_parser_wheels() -> None:
    """Make the standalone parser independent of Blender's user site-packages."""
    wheel_directory = Path(__file__).resolve().parent / "wheels"
    for pattern in ("typing_extensions-*.whl", "pypdf-*.whl"):
        for wheel in sorted(wheel_directory.glob(pattern)):
            value = str(wheel)
            if value not in sys.path:
                sys.path.insert(0, value)


_add_bundled_parser_wheels()


class ParseError(ValueError):
    """A PDF does not satisfy the Yohsai input profile."""


@dataclass(frozen=True)
class Point:
    x: float
    y: float

    def as_json(self, scale: float) -> list[float]:
        # Internal source points use a downward Y convention; Yohsai JSON uses up.
        return [_clean_float(self.x * scale), _clean_float(-self.y * scale)]


@dataclass(frozen=True)
class Matrix:
    a: float = 1.0
    b: float = 0.0
    c: float = 0.0
    d: float = 1.0
    e: float = 0.0
    f: float = 0.0

    def apply(self, point: Point) -> Point:
        return Point(
            self.a * point.x + self.c * point.y + self.e,
            self.b * point.x + self.d * point.y + self.f,
        )

    def __matmul__(self, other: "Matrix") -> "Matrix":
        """Compose matrices so (parent @ local).apply(p) applies local first."""
        return Matrix(
            self.a * other.a + self.c * other.b,
            self.b * other.a + self.d * other.b,
            self.a * other.c + self.c * other.d,
            self.b * other.c + self.d * other.d,
            self.a * other.e + self.c * other.f + self.e,
            self.b * other.e + self.d * other.f + self.f,
        )


@dataclass
class Segment:
    kind: str
    start: Point
    end: Point
    control1: Point | None = None
    control2: Point | None = None
    sewing_group: str | None = None
    fold: bool = False
    ring: bool = False

    def transformed(self, matrix: Matrix) -> "Segment":
        return Segment(
            self.kind,
            matrix.apply(self.start),
            matrix.apply(self.end),
            matrix.apply(self.control1) if self.control1 else None,
            matrix.apply(self.control2) if self.control2 else None,
        )

    def points_for_distance(self, steps: int = 64) -> list[Point]:
        if self.kind == "line":
            return [self.start, self.end]
        assert self.control1 is not None and self.control2 is not None
        return [
            _cubic_point(self.start, self.control1, self.control2, self.end, i / steps)
            for i in range(steps + 1)
        ]

    def length(self) -> float:
        points = self.points_for_distance(steps=256)
        return sum(_distance(a, b) for a, b in zip(points, points[1:]))


@dataclass
class Subpath:
    segments: list[Segment] = field(default_factory=list)
    closed: bool = False

    def transformed(self, matrix: Matrix) -> "Subpath":
        return Subpath([segment.transformed(matrix) for segment in self.segments], self.closed)


@dataclass
class PathRecord:
    source_id: str | None
    subpaths: list[Subpath]
    document_index: int


@dataclass(frozen=True)
class Annotation:
    text: str
    position: Point


@dataclass
class Panel:
    panel_id: str
    source_path_id: str | None
    segments: list[Segment]
    update_label: str | None = None
    mirror: bool = False
    top: Point | None = None


_NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
_TOKEN_RE = re.compile(rf"[A-Za-z]|{_NUMBER}")
_NUMBER_RE = re.compile(_NUMBER)
_TRANSFORM_RE = re.compile(r"([A-Za-z]+)\s*\(([^)]*)\)")
_SCALE_RE = re.compile(r"^@s\s*(" + _NUMBER + r")\s*cm$", re.IGNORECASE)
_SEW_RE = re.compile(r"^[A-Z]$", re.IGNORECASE)
_PANEL_LABEL_RE = re.compile(r"^[A-Z0-9_-]+$", re.IGNORECASE)
_RING_MARKER = "RING"


def _clean_float(value: float) -> float:
    if not math.isfinite(value):
        raise ParseError("A calculated coordinate is not finite.")
    if abs(value) < 1.0e-15:
        return 0.0
    return value


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _numbers(value: str) -> list[float]:
    remainder = _NUMBER_RE.sub("", value).replace(",", "").strip()
    if remainder:
        raise ParseError(f"Invalid numeric list: {value!r}")
    result = [float(item) for item in _NUMBER_RE.findall(value)]
    if not all(math.isfinite(item) for item in result):
        raise ParseError("A transform contains a non-finite number.")
    return result


def parse_transform(value: str | None) -> Matrix:
    if not value or not value.strip():
        return Matrix()
    matches = list(_TRANSFORM_RE.finditer(value))
    remainder = _TRANSFORM_RE.sub("", value).replace(",", "").strip()
    if remainder:
        raise ParseError(f"Invalid SVG transform: {value!r}")
    result = Matrix()
    for match in matches:
        name = match.group(1).lower()
        args = _numbers(match.group(2))
        if name == "matrix" and len(args) == 6:
            current = Matrix(*args)
        elif name == "translate" and len(args) in (1, 2):
            current = Matrix(e=args[0], f=args[1] if len(args) == 2 else 0.0)
        elif name == "scale" and len(args) in (1, 2):
            current = Matrix(a=args[0], d=args[1] if len(args) == 2 else args[0])
        elif name == "rotate" and len(args) in (1, 3):
            radians = math.radians(args[0])
            rotation = Matrix(a=math.cos(radians), b=math.sin(radians), c=-math.sin(radians), d=math.cos(radians))
            if len(args) == 3:
                cx, cy = args[1:]
                current = Matrix(e=cx, f=cy) @ rotation @ Matrix(e=-cx, f=-cy)
            else:
                current = rotation
        elif name == "skewx" and len(args) == 1:
            current = Matrix(c=math.tan(math.radians(args[0])))
        elif name == "skewy" and len(args) == 1:
            current = Matrix(b=math.tan(math.radians(args[0])))
        else:
            raise ParseError(f"Unsupported or malformed SVG transform: {match.group(0)!r}")
        result = result @ current
    return result


def _tokenize_path(data: str) -> list[str]:
    tokens = _TOKEN_RE.findall(data)
    remainder = _TOKEN_RE.sub("", data).replace(",", "").strip()
    if remainder:
        raise ParseError(f"Invalid SVG path syntax near {remainder[:30]!r}.")
    if not tokens:
        raise ParseError("An SVG path in CLOTHES has no path data.")
    return tokens


def _is_command(token: str) -> bool:
    return len(token) == 1 and token.isalpha()


def parse_path_data(data: str) -> list[Subpath]:
    tokens = _tokenize_path(data)
    index = 0
    command: str | None = None
    current = Point(0.0, 0.0)
    subpath_start: Point | None = None
    previous_cubic_control: Point | None = None
    active: Subpath | None = None
    subpaths: list[Subpath] = []

    def read(count: int) -> list[float]:
        nonlocal index
        if index + count > len(tokens) or any(_is_command(token) for token in tokens[index:index + count]):
            raise ParseError(f"SVG path command {command!r} has too few parameters.")
        values = [float(token) for token in tokens[index:index + count]]
        index += count
        if not all(math.isfinite(value) for value in values):
            raise ParseError("An SVG path contains a non-finite coordinate.")
        return values

    def point(x: float, y: float, relative: bool) -> Point:
        return Point(current.x + x, current.y + y) if relative else Point(x, y)

    def require_active() -> Subpath:
        if active is None:
            raise ParseError("An SVG path segment appears before its move command.")
        return active

    while index < len(tokens):
        if _is_command(tokens[index]):
            command = tokens[index]
            index += 1
        elif command is None:
            raise ParseError("SVG path data must start with a command.")

        assert command is not None
        upper = command.upper()
        relative = command.islower()
        if upper in {"Q", "T", "A"}:
            raise ParseError(f"Unsupported SVG path command {command!r} in CLOTHES.")
        if upper not in {"M", "L", "H", "V", "C", "S", "Z"}:
            raise ParseError(f"Unsupported SVG path command {command!r} in CLOTHES.")

        if upper == "Z":
            target = require_active()
            if subpath_start is None:
                raise ParseError("Close command has no subpath start.")
            if current != subpath_start:
                target.segments.append(Segment("line", current, subpath_start))
            target.closed = True
            current = subpath_start
            previous_cubic_control = None
            command = None
            continue

        if upper == "M":
            x, y = read(2)
            current = point(x, y, relative)
            active = Subpath()
            subpaths.append(active)
            subpath_start = current
            previous_cubic_control = None
            # Repeated coordinate pairs after moveto are implicit lineto.
            command = "l" if relative else "L"
            continue

        target = require_active()
        if upper == "L":
            x, y = read(2)
            end = point(x, y, relative)
            target.segments.append(Segment("line", current, end))
            current = end
            previous_cubic_control = None
        elif upper == "H":
            (x,) = read(1)
            end = Point(current.x + x if relative else x, current.y)
            target.segments.append(Segment("line", current, end))
            current = end
            previous_cubic_control = None
        elif upper == "V":
            (y,) = read(1)
            end = Point(current.x, current.y + y if relative else y)
            target.segments.append(Segment("line", current, end))
            current = end
            previous_cubic_control = None
        elif upper == "C":
            x1, y1, x2, y2, x, y = read(6)
            control1 = point(x1, y1, relative)
            control2 = point(x2, y2, relative)
            end = point(x, y, relative)
            target.segments.append(Segment("cubic", current, end, control1, control2))
            current = end
            previous_cubic_control = control2
        elif upper == "S":
            x2, y2, x, y = read(4)
            if previous_cubic_control is None:
                control1 = current
            else:
                control1 = Point(2 * current.x - previous_cubic_control.x, 2 * current.y - previous_cubic_control.y)
            control2 = point(x2, y2, relative)
            end = point(x, y, relative)
            target.segments.append(Segment("cubic", current, end, control1, control2))
            current = end
            previous_cubic_control = control2

    for subpath in subpaths:
        if not subpath.segments:
            raise ParseError("A path subpath in CLOTHES has no segments.")
    return subpaths


def _element_text(element: ET.Element) -> str:
    return "".join(element.itertext()).strip()


def _first_coordinate(element: ET.Element, name: str) -> float:
    candidates = [element, *list(element.iter())[1:]]
    for candidate in candidates:
        value = candidate.get(name)
        if value:
            values = _numbers(value)
            if values:
                return values[0]
    return 0.0


def _text_position(element: ET.Element, inherited: Matrix) -> Point:
    # Illustrator commonly stores the entire origin in a matrix on <text>.
    own = inherited @ parse_transform(element.get("transform"))
    x = _first_coordinate(element, "x")
    y = _first_coordinate(element, "y")
    return own.apply(Point(x, y))


def _walk_layer(element: ET.Element, inherited: Matrix, paths: list[PathRecord], annotations: list[Annotation]) -> None:
    tag = _local_name(element.tag)
    own = inherited @ parse_transform(element.get("transform"))
    if tag == "g":
        for child in element:
            _walk_layer(child, own, paths, annotations)
        return
    if tag == "path":
        data = element.get("d")
        if not data:
            raise ParseError("A path in CLOTHES is missing its d attribute.")
        subpaths = [subpath.transformed(own) for subpath in parse_path_data(data)]
        paths.append(PathRecord(element.get("id"), subpaths, len(paths)))
        return
    if tag == "text":
        text = _element_text(element)
        if text:
            annotations.append(Annotation(text, _text_position(element, inherited)))
        return
    if tag in {"title", "desc", "metadata"} and not _element_text(element):
        return
    if isinstance(element.tag, str):
        raise ParseError(f"Unsupported SVG element <{tag}> inside CLOTHES.")


def _distance(a: Point, b: Point) -> float:
    return math.hypot(a.x - b.x, a.y - b.y)


def _point_segment_distance(point: Point, start: Point, end: Point) -> float:
    dx = end.x - start.x
    dy = end.y - start.y
    length_squared = dx * dx + dy * dy
    if length_squared == 0.0:
        return _distance(point, start)
    t = max(0.0, min(1.0, ((point.x - start.x) * dx + (point.y - start.y) * dy) / length_squared))
    return _distance(point, Point(start.x + t * dx, start.y + t * dy))


def _cubic_point(start: Point, control1: Point, control2: Point, end: Point, t: float) -> Point:
    inverse = 1.0 - t
    return Point(
        inverse**3 * start.x + 3 * inverse**2 * t * control1.x + 3 * inverse * t**2 * control2.x + t**3 * end.x,
        inverse**3 * start.y + 3 * inverse**2 * t * control1.y + 3 * inverse * t**2 * control2.y + t**3 * end.y,
    )


def _point_to_segment_distance(point: Point, segment: Segment) -> float:
    samples = segment.points_for_distance()
    return min(_point_segment_distance(point, a, b) for a, b in zip(samples, samples[1:]))


def _nearest_panel_segment(annotation: Annotation, panels: list[Panel]) -> tuple[Panel, int, Segment]:
    candidates: list[tuple[float, Panel, int, Segment]] = []
    for panel in panels:
        for index, segment in enumerate(panel.segments):
            candidates.append((_point_to_segment_distance(annotation.position, segment), panel, index, segment))
    if not candidates:
        raise ParseError(f"Annotation {annotation.text!r} has no panel segment to reference.")
    candidates.sort(key=lambda item: item[0])
    best = candidates[0]
    if len(candidates) > 1:
        tolerance = max(1.0e-9, best[0] * 1.0e-9)
        if abs(candidates[1][0] - best[0]) <= tolerance:
            raise ParseError(f"Annotation {annotation.text!r} is equally close to multiple panel segments.")
    return best[1], best[2], best[3]


def _panel_polygon(panel: Panel) -> list[Point]:
    points: list[Point] = []
    for segment in panel.segments:
        sampled = segment.points_for_distance(steps=64)
        points.extend(sampled if not points else sampled[1:])
    if len(points) > 1 and _distance(points[0], points[-1]) <= 1.0e-9:
        points.pop()
    return points


def _point_in_polygon(point: Point, polygon: list[Point]) -> bool:
    inside = False
    previous = polygon[-1]
    for current in polygon:
        if (current.y > point.y) != (previous.y > point.y):
            crossing_x = (previous.x - current.x) * (point.y - current.y) / (previous.y - current.y) + current.x
            if point.x < crossing_x:
                inside = not inside
        previous = current
    return inside


def _normalize_panel_label(text: str) -> str:
    compact = "".join(text.split())
    if not compact.startswith("#"):
        raise ParseError(f"Panel label must start with '#': {text!r}")
    label = compact[1:].upper()
    if not label or not _PANEL_LABEL_RE.fullmatch(label):
        raise ParseError(
            f"Invalid panel label {text!r}; use ASCII letters, digits, underscore, or hyphen after '#'."
        )
    return label


def _assign_panel_labels(annotations: list[Annotation], panels: list[Panel]) -> set[Annotation]:
    label_annotations = {
        annotation for annotation in annotations if "".join(annotation.text.split()).startswith("#")
    }
    used: dict[str, Panel] = {}
    polygons = {id(panel): _panel_polygon(panel) for panel in panels}
    for annotation in label_annotations:
        label = _normalize_panel_label(annotation.text)
        containing = [panel for panel in panels if _point_in_polygon(annotation.position, polygons[id(panel)])]
        if len(containing) != 1:
            raise ParseError(
                f"Panel label {annotation.text!r} must be inside exactly one closed panel; found {len(containing)}."
            )
        panel = containing[0]
        if panel.update_label is not None:
            raise ParseError(f"Panel {panel.panel_id!r} contains more than one # label.")
        if label in used:
            raise ParseError(f"Duplicate panel label #{label}.")
        panel.update_label = label
        panel.panel_id = label
        used[label] = panel
    final_ids = [panel.panel_id for panel in panels]
    if len(set(final_ids)) != len(final_ids):
        raise ParseError("Panel labels conflict with another panel ID.")
    return label_annotations


def _unique_panel_id(preferred: str, used: set[str]) -> str:
    candidate = preferred
    suffix = 2
    while candidate in used:
        candidate = f"{preferred}_{suffix:03d}"
        suffix += 1
    used.add(candidate)
    return candidate


def _make_panels(records: list[PathRecord]) -> tuple[list[Panel], list[Subpath]]:
    panels: list[Panel] = []
    open_subpaths: list[Subpath] = []
    used_ids: set[str] = set()
    generated_index = 1
    for record in records:
        closed = [subpath for subpath in record.subpaths if subpath.closed]
        open_subpaths.extend(subpath for subpath in record.subpaths if not subpath.closed)
        for subpath_index, subpath in enumerate(closed, start=1):
            source_id = record.source_id.strip() if record.source_id and record.source_id.strip() else None
            if source_id:
                preferred = source_id if len(closed) == 1 else f"{source_id}_{subpath_index:03d}"
            else:
                preferred = f"panel_{generated_index:03d}"
                generated_index += 1
            panel_id = _unique_panel_id(preferred, used_ids)
            panels.append(Panel(panel_id, source_id, subpath.segments))
    return panels, open_subpaths


def _serialize_segment(segment: Segment, index: int, scale: float) -> dict[str, object]:
    result: dict[str, object] = {
        "index": index,
        "type": segment.kind,
        "start": segment.start.as_json(scale),
        "end": segment.end.as_json(scale),
        "sewing_group": segment.sewing_group,
        "fold": segment.fold,
        "ring": segment.ring,
    }
    if segment.kind == "cubic":
        assert segment.control1 is not None and segment.control2 is not None
        result["control1"] = segment.control1.as_json(scale)
        result["control2"] = segment.control2.as_json(scale)
        # Keep geometric fields together in a predictable order.
        result = {
            "index": result["index"],
            "type": result["type"],
            "start": result["start"],
            "control1": result["control1"],
            "control2": result["control2"],
            "end": result["end"],
            "sewing_group": result["sewing_group"],
            "fold": result["fold"],
            "ring": result["ring"],
        }
    return result


def _panel_for_internal_command(annotation: Annotation, panels: list[Panel]) -> Panel:
    containing = [panel for panel in panels if _point_in_polygon(annotation.position, _panel_polygon(panel))]
    if len(containing) != 1:
        raise ParseError(
            f"Command {annotation.text!r} must be inside exactly one labeled panel; found {len(containing)}."
        )
    return containing[0]


def _expand_ring_bounded_sewing(panel: Panel) -> None:
    """Extend a seam marker across its whole boundary arc between two RING edges."""
    ring_indices = [index for index, segment in enumerate(panel.segments) if segment.ring]
    if not ring_indices:
        return
    if len(ring_indices) != 2:
        raise ParseError(f"Panel {panel.panel_id!r} must contain exactly two RING edges.")
    if panel.top is None:
        raise ParseError(f"Panel {panel.panel_id!r} with RING edges requires one @TOP command.")

    first, second = sorted(ring_indices)
    arcs = [
        list(range(first + 1, second)),
        list(range(second + 1, len(panel.segments))) + list(range(0, first)),
    ]
    for arc in arcs:
        labels = {panel.segments[index].sewing_group for index in arc if panel.segments[index].sewing_group}
        if len(labels) > 1:
            raise ParseError(f"Panel {panel.panel_id!r} has conflicting sewing markers between its RING edges.")
        if labels:
            label = next(iter(labels))
            for index in arc:
                panel.segments[index].sewing_group = label


def _collect_pdf_annotations(
    annotations: list[Annotation], panels: list[Panel], panel_label_annotations: set[Annotation]
) -> dict[str, list[dict[str, object]]]:
    sewing_groups: dict[str, list[dict[str, object]]] = {}
    for annotation in annotations:
        if annotation in panel_label_annotations:
            continue
        normalized = "".join(annotation.text.split()).upper()
        if normalized == "@M":
            panel = _panel_for_internal_command(annotation, panels)
            if panel.mirror:
                raise ParseError(f"Panel {panel.panel_id!r} contains more than one @M command.")
            panel.mirror = True
        elif normalized == "@TOP":
            panel = _panel_for_internal_command(annotation, panels)
            if panel.top is not None:
                raise ParseError(f"Panel {panel.panel_id!r} contains more than one @TOP command.")
            panel.top = annotation.position
        else:
            panel, segment_index, segment = _nearest_panel_segment(annotation, panels)
            if normalized == "@W":
                segment.fold = True
            elif normalized == _RING_MARKER:
                if segment.ring:
                    raise ParseError(f"Panel {panel.panel_id!r} segment {segment_index} has duplicate RING markers.")
                if segment.fold or segment.sewing_group:
                    raise ParseError(f"Panel {panel.panel_id!r} segment {segment_index} has conflicting markers.")
                segment.ring = True
            elif _SEW_RE.fullmatch(normalized):
                if segment.ring or (segment.sewing_group is not None and segment.sewing_group != normalized):
                    raise ParseError(
                        f"Panel {panel.panel_id!r} segment {segment_index} has conflicting sewing markers."
                    )
                segment.sewing_group = normalized
            elif normalized.startswith("@"):
                raise ParseError(f"Unsupported PDF annotation {annotation.text!r}.")

    for panel in panels:
        _expand_ring_bounded_sewing(panel)
        if panel.top is not None and not any(segment.ring for segment in panel.segments):
            raise ParseError(f"Panel {panel.panel_id!r} uses @TOP without two RING edges.")
        for segment_index, segment in enumerate(panel.segments):
            if segment.sewing_group:
                sewing_groups.setdefault(segment.sewing_group, []).append(
                    {"panel": panel.panel_id, "segment": segment_index}
                )
    return sewing_groups


def parse_svg(svg_path: str | os.PathLike[str]) -> dict[str, object]:
    raise ParseError("SVG input is no longer supported; use a PDF file.")


def _parse_legacy_svg(svg_path: str | os.PathLike[str]) -> dict[str, object]:
    source_path = Path(svg_path).expanduser().resolve()
    if not source_path.is_file():
        raise ParseError(f"SVG file does not exist: {source_path}")
    if source_path.suffix.lower() != ".svg":
        raise ParseError("Input file must have an .svg extension.")
    try:
        tree = ET.parse(source_path)
    except (ET.ParseError, OSError) as exc:
        raise ParseError(f"Cannot read SVG: {exc}") from exc

    clothes_layers = [
        element for element in tree.getroot().iter()
        if _local_name(element.tag) == "g" and element.get("id") == "CLOTHES"
    ]
    if len(clothes_layers) != 1:
        raise ParseError(f"Expected exactly one SVG group with id='CLOTHES'; found {len(clothes_layers)}.")

    # Build the transform inherited by the selected layer.
    parent_map = {child: parent for parent in tree.iter() for child in parent}
    ancestors: list[ET.Element] = []
    cursor = parent_map.get(clothes_layers[0])
    while cursor is not None:
        ancestors.append(cursor)
        cursor = parent_map.get(cursor)
    inherited = Matrix()
    for ancestor in reversed(ancestors):
        inherited = inherited @ parse_transform(ancestor.get("transform"))

    records: list[PathRecord] = []
    annotations: list[Annotation] = []
    _walk_layer(clothes_layers[0], inherited, records, annotations)
    panels, open_subpaths = _make_panels(records)
    if not panels:
        raise ParseError("CLOTHES contains no closed pattern panel.")

    scale_annotations = [annotation for annotation in annotations if _SCALE_RE.fullmatch(annotation.text)]
    if len(scale_annotations) != 1:
        raise ParseError(f"Expected exactly one @S<number>cm annotation; found {len(scale_annotations)}.")
    if len(open_subpaths) != 1:
        raise ParseError(f"Expected exactly one open scale-reference path; found {len(open_subpaths)}.")
    scale_annotation = scale_annotations[0]
    match = _SCALE_RE.fullmatch(scale_annotation.text)
    assert match is not None
    reference_centimeters = float(match.group(1))
    if not math.isfinite(reference_centimeters) or reference_centimeters <= 0.0:
        raise ParseError("Scale annotation length must be finite and greater than zero.")
    reference_svg_length = sum(segment.length() for segment in open_subpaths[0].segments)
    if not math.isfinite(reference_svg_length) or reference_svg_length <= 0.0:
        raise ParseError("Scale-reference path must have a positive length.")
    meters_per_svg_unit = reference_centimeters / 100.0 / reference_svg_length

    data_annotations = [annotation for annotation in annotations if annotation is not scale_annotation]
    panel_label_annotations = _assign_panel_labels(data_annotations, panels)
    sewing_groups: dict[str, list[dict[str, object]]] = {}
    for annotation in data_annotations:
        if annotation in panel_label_annotations:
            continue
        normalized = annotation.text.strip().upper()
        panel, segment_index, segment = _nearest_panel_segment(annotation, panels)
        if normalized == "@W":
            segment.fold = True
        elif _SEW_RE.fullmatch(normalized):
            if segment.sewing_group is not None and segment.sewing_group != normalized:
                raise ParseError(
                    f"Panel {panel.panel_id!r} segment {segment_index} has conflicting sewing markers."
                )
            segment.sewing_group = normalized
            sewing_groups.setdefault(normalized, []).append({"panel": panel.panel_id, "segment": segment_index})
        else:
            raise ParseError(f"Unsupported annotation {annotation.text!r} inside CLOTHES.")

    document: dict[str, object] = {
        "schema": SCHEMA_NAME,
        "version": SCHEMA_VERSION,
        "source": {
            "svg_path": source_path.as_posix(),
            "clothes_layer": "CLOTHES",
        },
        "units": "m",
        "scale": {
            "annotation": scale_annotation.text.strip(),
            "reference_length_m": _clean_float(reference_centimeters / 100.0),
            "reference_length_svg": _clean_float(reference_svg_length),
            "meters_per_svg_unit": _clean_float(meters_per_svg_unit),
        },
        "panels": [
            {
                "id": panel.panel_id,
                "label": panel.update_label,
                "source_path_id": panel.source_path_id,
                "closed": True,
                "segments": [
                    _serialize_segment(segment, index, meters_per_svg_unit)
                    for index, segment in enumerate(panel.segments)
                ],
            }
            for panel in panels
        ],
        "sewing_groups": sewing_groups,
    }
    # Enforce strict JSON number validity before returning to callers.
    json.dumps(document, allow_nan=False)
    return document


_PDF_METERS_PER_POINT = 0.0254 / 72.0
_PDF_PATH_PAINT = {b"S", b"s", b"f", b"F", b"f*", b"B", b"B*", b"b", b"b*"}


def _pdf_matrix(values: object) -> Matrix:
    try:
        numbers = [float(value) for value in values]
    except (TypeError, ValueError) as exc:
        raise ParseError("PDF contains an invalid transformation matrix.") from exc
    if len(numbers) != 6 or not all(math.isfinite(value) for value in numbers):
        raise ParseError("PDF contains an invalid transformation matrix.")
    return Matrix(*numbers)


def _pdf_point(ctm: Matrix, x: object, y: object) -> Point:
    try:
        transformed = ctm.apply(Point(float(x), float(y)))
    except (TypeError, ValueError) as exc:
        raise ParseError("PDF contains an invalid coordinate.") from exc
    return Point(transformed.x, -transformed.y)


def _pdf_decode_string(value: object, font: object | None) -> str:
    text = str(value)
    raw = getattr(value, "original_bytes", None)
    if font is not None and (
        str(font.get("/Encoding", "")) != "/Identity-H" or font.get("/ToUnicode") is not None
    ):
        return text
    if not isinstance(raw, bytes) or len(raw) % 2:
        return text
    # Illustrator sometimes omits a nested Form's font resource even though
    # its strings use the same ASCII CID subset as the surrounding Identity-H
    # font. The embedded CIDs are 1..95 and map to ASCII by adding 31.
    cids = [int.from_bytes(raw[index:index + 2], "big") for index in range(0, len(raw), 2)]
    if font is None and not cids:
        return text
    if any(cid < 1 or cid > 95 for cid in cids):
        return text
    result: list[str] = []
    for cid in cids:
        result.append(chr(cid + 31))
    return "".join(result)


def _pdf_collect_content(
    owner: object,
    reader: object,
    inherited: Matrix,
    paths: list[Subpath],
    annotations: list[Annotation],
) -> None:
    try:
        from pypdf.generic import ContentStream
    except ImportError as exc:
        raise ParseError("PDF input requires the bundled pypdf package.") from exc

    resources = owner.get("/Resources", {})
    contents = owner.get("/Contents") if str(owner.get("/Type", "")) == "/Page" else owner
    if contents is None:
        return
    stream = ContentStream(contents, reader)
    ctm = inherited
    graphics_stack: list[Matrix] = []
    pending: list[Subpath] = []
    current: Subpath | None = None
    current_point: Point | None = None
    start_point: Point | None = None
    text_matrix = Matrix()
    text_line_matrix = Matrix()
    current_font = None

    def finish_open_subpath() -> None:
        nonlocal current, current_point, start_point
        if current is not None and current.segments:
            pending.append(current)
        current = None
        current_point = None
        start_point = None

    def begin(point: Point) -> None:
        nonlocal current, current_point, start_point
        finish_open_subpath()
        current = Subpath([], False)
        current_point = point
        start_point = point

    def line_to(point: Point) -> None:
        nonlocal current_point
        if current is None or current_point is None:
            raise ParseError("PDF path draws a line before a move operation.")
        current.segments.append(Segment("line", current_point, point))
        current_point = point

    def cubic_to(control1: Point, control2: Point, point: Point) -> None:
        nonlocal current_point
        if current is None or current_point is None:
            raise ParseError("PDF path draws a curve before a move operation.")
        current.segments.append(Segment("cubic", current_point, point, control1, control2))
        current_point = point

    def close_current() -> None:
        nonlocal current, current_point
        if current is None or current_point is None or start_point is None:
            raise ParseError("PDF closes a path before a move operation.")
        if _distance(current_point, start_point) > 1.0e-9:
            current.segments.append(Segment("line", current_point, start_point))
        current.closed = True
        pending.append(current)
        current = None
        current_point = None

    def emit_text(operands: object) -> None:
        pieces: list[str] = []
        values = operands[0] if operands else []
        if not isinstance(values, list):
            values = [values]
        for value in values:
            if isinstance(value, (int, float)):
                continue
            pieces.append(_pdf_decode_string(value, current_font))
        text = "".join(pieces).strip()
        if text:
            origin_matrix = ctm @ text_matrix
            annotations.append(Annotation(text, _pdf_point(origin_matrix, 0.0, 0.0)))

    for operands, operator in stream.operations:
        if operator == b"q":
            graphics_stack.append(ctm)
        elif operator == b"Q":
            if not graphics_stack:
                raise ParseError("PDF graphics-state stack is unbalanced.")
            ctm = graphics_stack.pop()
        elif operator == b"cm":
            ctm = ctm @ _pdf_matrix(operands)
        elif operator == b"m":
            begin(_pdf_point(ctm, operands[0], operands[1]))
        elif operator == b"l":
            line_to(_pdf_point(ctm, operands[0], operands[1]))
        elif operator == b"c":
            cubic_to(
                _pdf_point(ctm, operands[0], operands[1]),
                _pdf_point(ctm, operands[2], operands[3]),
                _pdf_point(ctm, operands[4], operands[5]),
            )
        elif operator == b"v":
            if current_point is None:
                raise ParseError("PDF path draws a curve before a move operation.")
            cubic_to(current_point, _pdf_point(ctm, operands[0], operands[1]), _pdf_point(ctm, operands[2], operands[3]))
        elif operator == b"y":
            endpoint = _pdf_point(ctm, operands[2], operands[3])
            cubic_to(_pdf_point(ctm, operands[0], operands[1]), endpoint, endpoint)
        elif operator == b"h":
            close_current()
        elif operator == b"re":
            x, y, width, height = (float(value) for value in operands)
            begin(_pdf_point(ctm, x, y))
            line_to(_pdf_point(ctm, x + width, y))
            line_to(_pdf_point(ctm, x + width, y + height))
            line_to(_pdf_point(ctm, x, y + height))
            close_current()
        elif operator in _PDF_PATH_PAINT:
            finish_open_subpath()
            paths.extend(pending)
            pending.clear()
        elif operator == b"n":
            current = None
            current_point = None
            start_point = None
            pending.clear()
        elif operator == b"BT":
            text_matrix = Matrix()
            text_line_matrix = Matrix()
            current_font = None
        elif operator == b"Tf":
            font_reference = resources.get("/Font", {}).get(operands[0])
            current_font = font_reference.get_object() if font_reference is not None else None
        elif operator == b"Tm":
            text_matrix = _pdf_matrix(operands)
            text_line_matrix = text_matrix
        elif operator in {b"Td", b"TD"}:
            text_line_matrix = text_line_matrix @ Matrix(e=float(operands[0]), f=float(operands[1]))
            text_matrix = text_line_matrix
        elif operator == b"T*":
            text_matrix = text_line_matrix
        elif operator in {b"Tj", b"TJ"}:
            emit_text(operands)
        elif operator == b"Do":
            reference = resources.get("/XObject", {}).get(operands[0])
            if reference is None:
                continue
            form = reference.get_object()
            if str(form.get("/Subtype", "")) == "/Form":
                form_matrix = _pdf_matrix(form.get("/Matrix", [1, 0, 0, 1, 0, 0]))
                _pdf_collect_content(form, reader, ctm @ form_matrix, paths, annotations)


def parse_pdf(path: str | os.PathLike[str]) -> dict[str, object]:
    source_path = Path(path).resolve()
    if not source_path.is_file():
        raise ParseError(f"PDF file does not exist: {source_path}")
    if source_path.suffix.lower() != ".pdf":
        raise ParseError("Input file must have a .pdf extension.")
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ParseError("PDF input requires the bundled pypdf package.") from exc
    try:
        reader = PdfReader(source_path)
    except Exception as exc:
        raise ParseError(f"Cannot read PDF: {exc}") from exc
    if len(reader.pages) != 1:
        raise ParseError(f"Yohsai PDF input must contain exactly one page; found {len(reader.pages)}.")

    paths: list[Subpath] = []
    annotations: list[Annotation] = []
    _pdf_collect_content(reader.pages[0], reader, Matrix(), paths, annotations)
    records = [
        PathRecord(None, [subpath], index)
        for index, subpath in enumerate(paths)
        if subpath.closed
    ]
    all_panels, _open = _make_panels(records)
    command_annotations = [
        annotation
        for annotation in annotations
        if (normalized := "".join(annotation.text.split()))
        and (
            normalized.startswith("#")
            or normalized.startswith("@")
            or normalized.upper() == _RING_MARKER
            or _SEW_RE.fullmatch(normalized)
        )
    ]
    panel_label_annotations = _assign_panel_labels(command_annotations, all_panels)
    panels = [panel for panel in all_panels if panel.update_label is not None]
    if not panels:
        raise ParseError("PDF contains no closed panel with an internal # label.")

    sewing_groups = _collect_pdf_annotations(command_annotations, panels, panel_label_annotations)

    document: dict[str, object] = {
        "schema": SCHEMA_NAME,
        "version": SCHEMA_VERSION,
        "source": {
            "svg_path": source_path.as_posix(),
            "clothes_layer": "PDF # labeled panels",
            "input_format": "pdf",
        },
        "units": "m",
        "scale": {
            "meters_per_svg_unit": _clean_float(_PDF_METERS_PER_POINT),
        },
        "panels": [
            {
                "id": panel.panel_id,
                "label": panel.update_label,
                "source_path_id": None,
                "closed": True,
                "mirror": panel.mirror,
                "top": panel.top.as_json(_PDF_METERS_PER_POINT) if panel.top else None,
                "segments": [
                    _serialize_segment(segment, index, _PDF_METERS_PER_POINT)
                    for index, segment in enumerate(panel.segments)
                ],
            }
            for panel in panels
        ],
        "sewing_groups": sewing_groups,
    }
    json.dumps(document, allow_nan=False)
    return document


def parse_pattern(path: str | os.PathLike[str]) -> dict[str, object]:
    suffix = Path(path).suffix.lower()
    if suffix == ".pdf":
        return parse_pdf(path)
    raise ParseError("Pattern input must be a .pdf file.")


def write_fixed_output(document: dict[str, object], directory: str | os.PathLike[str] = ".") -> Path:
    output_directory = Path(directory).resolve()
    output_directory.mkdir(parents=True, exist_ok=True)
    output_path = output_directory / OUTPUT_FILENAME
    temporary_path = output_directory / TEMP_FILENAME
    try:
        with temporary_path.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, output_path)
    except Exception:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return output_path


def main(argv: Iterable[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 1:
        print("Usage: yohsai_svg_parser.py <absolute-pattern.pdf>", file=sys.stderr)
        return 2
    try:
        document = parse_pattern(arguments[0])
        output_path = write_fixed_output(document)
    except (ParseError, OSError, ValueError) as exc:
        print(f"Yohsai pattern parse failed: {exc}", file=sys.stderr)
        return 1
    print(output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
