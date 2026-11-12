from __future__ import annotations

import math
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QMouseEvent, QPainter
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QAbstractItemView, QApplication

from sai import fill, io, transform
from sai.app import MainWindow
from sai.canvas import Canvas
from sai.geometry import nearest_point, nearest_segment, sample_stroke
from sai.model import Document, Layer, Point, Stroke
from sai.render import draw_document, layer_image, stroke_path


def _bow_depth(stroke: Stroke) -> float:
    """Maximum distance between the sampled curve and the plain polyline."""
    samples = sample_stroke(stroke)
    return max(nearest_segment(stroke, x, y)[1] for x, y, _ in samples)


class GeometryTests(unittest.TestCase):
    def test_sharp_stroke_keeps_corners(self) -> None:
        stroke = Stroke([Point(0, 0, 2), Point(10, 0, 2), Point(10, 10, 2)])
        samples = sample_stroke(stroke)
        self.assertEqual(len(samples), 3)
        self.assertAlmostEqual(samples[0][0], 0.0)
        self.assertAlmostEqual(samples[1][0], 10.0)
        self.assertAlmostEqual(samples[-1][1], 10.0)

    def test_smooth_stroke_is_densified(self) -> None:
        stroke = Stroke(
            [
                Point(0, 0, 2, True),
                Point(50, 0, 2, True),
                Point(50, 50, 2, True),
                Point(50, 100, 2, True),
            ]
        )
        samples = sample_stroke(stroke)
        self.assertGreater(len(samples), 4)
        self.assertAlmostEqual(samples[-1][0], 50.0)
        self.assertAlmostEqual(samples[-1][1], 100.0)

    def test_width_interpolates(self) -> None:
        stroke = Stroke([Point(0, 0, 2, True), Point(40, 0, 10, True)])
        samples = sample_stroke(stroke)
        self.assertAlmostEqual(samples[0][2], 2.0)
        self.assertAlmostEqual(samples[-1][2], 10.0)
        middle = samples[len(samples) // 2][2]
        self.assertGreater(middle, 2.0)
        self.assertLess(middle, 10.0)

    def test_smooth_point_bows_the_curve(self) -> None:
        stroke = Stroke(
            [
                Point(0, 0, 2),
                Point(50, 0, 2, True),
                Point(50, 50, 2, True),
                Point(50, 100, 2),
            ]
        )
        self.assertGreater(_bow_depth(stroke), 0.1)

    def test_smoothness_scales_with_point_distance(self) -> None:
        small = Stroke(
            [
                Point(0, 0, 2),
                Point(50, 0, 2, True),
                Point(50, 50, 2, True),
                Point(50, 100, 2),
            ]
        )
        big = Stroke(
            [
                Point(0, 0, 2),
                Point(500, 0, 2, True),
                Point(500, 500, 2, True),
                Point(500, 1000, 2),
            ]
        )
        ratio = _bow_depth(big) / _bow_depth(small)
        self.assertAlmostEqual(ratio, 10.0, delta=1.0)

    def test_smooth_point_next_to_sharp_still_rounds(self) -> None:
        stroke = Stroke(
            [
                Point(0, 0, 3),
                Point(100, 0, 3, True),
                Point(100, 100, 3),
                Point(200, 100, 3),
            ]
        )
        # Segment B->C has one smooth endpoint and must still bow.
        self.assertGreater(_bow_depth(stroke), 0.5)

    def test_three_points_are_polyline(self) -> None:
        stroke = Stroke([Point(0, 0, 3), Point(50, 0, 3), Point(50, 50, 3)])
        samples = sample_stroke(stroke)
        self.assertEqual(len(samples), 3)
        self.assertEqual(samples[1][:2], (50.0, 0.0))

    def test_three_points_form_a_semicircle(self) -> None:
        stroke = Stroke([Point(0, 0, 3, True), Point(50, 0, 3, True), Point(50, 50, 3, True)])
        center = (25.0, 25.0)
        radius = math.hypot(25.0, 25.0)
        for x, y, _ in sample_stroke(stroke):
            self.assertAlmostEqual(math.hypot(x - center[0], y - center[1]), radius, delta=0.05)
        self.assertAlmostEqual(_bow_depth(stroke), radius * (1 - math.cos(math.pi / 4)), delta=0.2)

    def test_junction_between_segments_is_smooth(self) -> None:
        stroke = Stroke(
            [
                Point(0, 0, 3, True),
                Point(50, 0, 3, True),
                Point(50, 50, 3, True),
                Point(50, 100, 3, True),
            ]
        )
        samples = sample_stroke(stroke)
        junction = min(
            range(1, len(samples) - 1),
            key=lambda i: math.hypot(samples[i][0] - 50.0, samples[i][1] - 0.0),
        )
        before = (
            samples[junction][0] - samples[junction - 1][0],
            samples[junction][1] - samples[junction - 1][1],
        )
        after = (
            samples[junction + 1][0] - samples[junction][0],
            samples[junction + 1][1] - samples[junction][1],
        )
        length_before = math.hypot(*before)
        length_after = math.hypot(*after)
        cosine = (before[0] * after[0] + before[1] * after[1]) / (length_before * length_after)
        self.assertGreater(cosine, 0.99)

    def test_corner_starts_a_straight_run(self) -> None:
        stroke = Stroke([Point(0, 0, 2), Point(50, 50, 2, True), Point(100, 0, 2, True)])
        samples = sample_stroke(stroke)
        # A sharp start goes straight to the next point, rounding resumes after it.
        self.assertEqual(samples[1][:2], (50.0, 50.0))
        self.assertGreater(len(samples), 3)

    def test_straight_run_passes_through_the_smooth_point(self) -> None:
        stroke = Stroke([Point(0, 0, 2), Point(50, 0, 2, True), Point(50, 50, 2, True)])
        samples = sample_stroke(stroke)
        self.assertEqual(samples[1][:2], (50.0, 0.0))
        # The curve leaves the smooth point along the straight run: no kink.
        first, second = samples[1], samples[2]
        slope = abs(second[1] - first[1]) / max(1e-9, abs(second[0] - first[0]))
        self.assertLess(slope, 0.1)

    def test_no_kink_at_smooth_points(self) -> None:
        stroke = Stroke(
            [
                Point(0, 0, 2),
                Point(40, 0, 2, True),
                Point(80, 10, 2, True),
                Point(120, 40, 2),
                Point(150, 90, 2, True),
                Point(140, 160, 2, True),
            ]
        )
        samples = sample_stroke(stroke)
        for index in range(1, len(stroke.points) - 1):
            point = stroke.points[index]
            if not point.smooth:
                continue
            near = min(
                range(1, len(samples) - 1),
                key=lambda i: math.hypot(samples[i][0] - point.x, samples[i][1] - point.y),
            )
            before = (
                samples[near][0] - samples[near - 1][0],
                samples[near][1] - samples[near - 1][1],
            )
            after = (
                samples[near + 1][0] - samples[near][0],
                samples[near + 1][1] - samples[near][1],
            )
            length_before = math.hypot(*before)
            length_after = math.hypot(*after)
            cosine = (before[0] * after[0] + before[1] * after[1]) / (length_before * length_after)
            self.assertGreater(cosine, 0.999, f"kink at smooth point {index}")

    def test_closed_stroke_follows_its_circle(self) -> None:
        stroke = Stroke(
            [Point(0, 0, 3, True), Point(10, 0, 3, True), Point(10, 10, 3, True)],
            closed=True,
        )
        center = (5.0, 5.0)
        radius = math.hypot(5.0, 5.0)
        for x, y, _ in sample_stroke(stroke):
            self.assertAlmostEqual(math.hypot(x - center[0], y - center[1]), radius, delta=0.05)

    def test_open_stroke_does_not_draw_the_closing_segment(self) -> None:
        points = [Point(0, 0, 3, True), Point(10, 0, 3, True), Point(10, 10, 3, True)]
        open_samples = sample_stroke(Stroke(points))
        closed_samples = sample_stroke(Stroke(points, closed=True))
        self.assertGreater(len(closed_samples), len(open_samples))
        self.assertAlmostEqual(closed_samples[-1][0], 0.0, delta=0.05)
        self.assertAlmostEqual(closed_samples[-1][1], 0.0, delta=0.05)

    def test_arc_ends_exactly_on_its_point(self) -> None:
        # Nearly collinear smooth points: the sampled arc must finish exactly on
        # the point, otherwise a thick stroke shows a notch at the join.
        stroke = Stroke(
            [
                Point(0, 0, 6),
                Point(0, 30, 6, True),
                Point(0.6, 60, 6, True),
                Point(0, 90, 6),
            ]
        )
        samples = sample_stroke(stroke)
        for point in stroke.points:
            self.assertTrue(
                any(
                    abs(x - point.x) < 1e-6 and abs(y - point.y) < 1e-6 for x, y, _width in samples
                ),
                f"point {point.x},{point.y} missing from samples",
            )

    def test_nearest_point(self) -> None:
        stroke = Stroke([Point(0, 0, 1), Point(10, 0, 1)])
        index, distance = nearest_point(stroke, 9, 0)
        self.assertEqual(index, 1)
        self.assertAlmostEqual(distance, 1.0)

    def test_nearest_segment(self) -> None:
        stroke = Stroke([Point(0, 0, 1), Point(10, 0, 1)])
        index, distance = nearest_segment(stroke, 5, 2)
        self.assertEqual(index, 0)
        self.assertAlmostEqual(distance, 2.0)


class DocumentTests(unittest.TestCase):
    def _document(self) -> Document:
        document = Document(400, 300)
        document.layers[0].opacity = 0.5
        document.layers[0].strokes.append(
            Stroke([Point(10, 10, 4, True), Point(100, 100, 8, False)])
        )
        return document

    def test_json_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "drawing.json"
            io.save_document(self._document(), path)
            loaded = io.load_document(path)
        self.assertEqual(loaded.width, 400)
        self.assertEqual(loaded.height, 300)
        self.assertEqual(len(loaded.layers[0].strokes), 1)
        points = loaded.layers[0].strokes[0].points
        self.assertEqual(len(points), 2)
        self.assertTrue(points[0].smooth)
        self.assertFalse(points[1].smooth)
        self.assertAlmostEqual(loaded.layers[0].opacity, 0.5)

    def test_ensure_suffix_keeps_real_extension(self) -> None:
        self.assertEqual(io.ensure_suffix("/tmp/drawing.json", ".json"), Path("/tmp/drawing.json"))
        self.assertEqual(io.ensure_suffix("/tmp/drawing.PNG", ".png"), Path("/tmp/drawing.PNG"))

    def test_ensure_suffix_appends_when_missing(self) -> None:
        self.assertEqual(io.ensure_suffix("/tmp/drawing", ".png"), Path("/tmp/drawing.png"))

    def test_ensure_suffix_replaces_portal_placeholder(self) -> None:
        # xdg-desktop-portal-termfilechooser suggests "termfilechooser.tmp"
        self.assertEqual(
            io.ensure_suffix("/home/alex/termfilechooser.tmp", ".json"),
            Path("/home/alex/termfilechooser.json"),
        )

    def test_closed_stroke_roundtrip(self) -> None:
        document = Document(100, 100)
        document.layers[0].strokes.append(
            Stroke([Point(0, 0, 2), Point(50, 0, 4, True), Point(50, 50, 2)], closed=True)
        )
        document.layers[0].strokes.append(Stroke([Point(0, 50, 2), Point(50, 50, 2)]))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "loop.json"
            io.save_document(document, path)
            loaded = io.load_document(path)
        self.assertTrue(loaded.layers[0].strokes[0].closed)
        self.assertFalse(loaded.layers[0].strokes[1].closed)

    def test_svg_export_nests_groups(self) -> None:
        document = Document(40, 40)
        inner = Layer("inner", [Stroke([Point(10, 10, 3), Point(30, 30, 3)])])
        document.layers = [document.make_group("group", [inner])]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "group.svg"
            io.export_svg(document, path)
            text = path.read_text(encoding="utf-8")
        self.assertIn('<g id="group">', text)
        self.assertIn('  <g id="inner">', text)
        self.assertIn("</g>\n  </g>", text.replace("\r\n", "\n"))

    def test_svg_export_draws_vector_fills(self) -> None:
        document = Document(40, 40)
        document.layers[0].strokes.append(
            Stroke(
                [Point(10, 10, 0), Point(30, 10, 0), Point(30, 30, 0), Point(10, 30, 0)],
                closed=True,
                fill=(255, 0, 0),
            )
        )
        document.layers[0].strokes.append(
            Stroke(
                [Point(4, 4, 0), Point(34, 4, 0), Point(34, 34, 0), Point(4, 34, 0)],
                closed=True,
                fill=(0, 0, 255),
                fill_alpha=128,
            )
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fill.svg"
            io.export_svg(document, path)
            text = path.read_text(encoding="utf-8")
        self.assertIn("#0000ff", text)
        self.assertIn("#ff0000", text)
        self.assertIn('fill-opacity="0.502"', text)

    def test_svg_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "drawing.svg"
            io.export_svg(self._document(), path)
            text = path.read_text(encoding="utf-8")
        self.assertIn("<svg", text)
        self.assertIn("<line", text)
        self.assertIn('stroke-width="', text)
        self.assertIn('stroke-linecap="round"', text)

    def test_png_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "drawing.png"
            io.export_png(self._document(), path, scale=2.0)
            image = QImage(str(path))
        self.assertFalse(image.isNull())
        self.assertEqual(image.width(), 800)
        self.assertEqual(image.height(), 600)

    def test_png_export_background(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            transparent = Path(tmp) / "transparent.png"
            white = Path(tmp) / "white.png"
            document = self._document()
            io.export_png(document, transparent, scale=1.0)
            io.export_png(document, white, scale=1.0, background=QColor("white"))
            transparent_image = QImage(str(transparent))
            white_image = QImage(str(white))
        self.assertEqual(transparent_image.pixelColor(399, 299).alpha(), 0)
        self.assertEqual(white_image.pixelColor(399, 299).alpha(), 255)
        self.assertEqual(white_image.pixelColor(399, 299).name(), "#ffffff")

    def test_load_png_as_document_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            png_path = Path(tmp) / "image.png"
            image = QImage(4, 4, QImage.Format.Format_ARGB32_Premultiplied)
            image.fill(QColor("white"))
            image.save(str(png_path))
            path = Path(tmp) / "image.json"
            png_path.rename(path)
            with self.assertRaises(ValueError) as context:
                io.load_document(path)
        self.assertIn("Import PNG", str(context.exception))

    def test_raster_layer_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "underlay.png"
            image = QImage(32, 24, QImage.Format.Format_ARGB32_Premultiplied)
            image.fill(QColor("red"))
            image.save(str(source))

            layer = io.load_raster_layer(source, "underlay")
            self.assertEqual(layer.kind, "raster")
            self.assertIsNotNone(layer.image_base64)

            document = Document(32, 24)
            document.layers.append(layer)
            path = Path(tmp) / "doc.json"
            io.save_document(document, path)
            loaded = io.load_document(path)

        restored = loaded.layers[-1]
        self.assertEqual(restored.kind, "raster")
        decoded = layer_image(restored)
        self.assertIsNotNone(decoded)
        assert decoded is not None
        self.assertEqual(decoded.width(), 32)


class RenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _render(self, document: Document) -> QImage:
        image = QImage(document.width, document.height, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        draw_document(painter, document)
        painter.end()
        return image

    def test_stroke_fill_paints_the_closed_shape(self) -> None:
        document = Document(40, 40)
        document.layers = [
            Layer(
                "shape",
                [
                    Stroke(
                        [Point(10, 10, 0), Point(30, 10, 0), Point(30, 30, 0), Point(10, 30, 0)],
                        closed=True,
                        fill=(255, 0, 0),
                    )
                ],
            )
        ]
        image = self._render(document)
        self.assertGreater(image.pixelColor(20, 20).red(), 200)
        self.assertEqual(image.pixelColor(2, 2).alpha(), 0)

    def test_group_opacity_applies_to_children(self) -> None:
        document = Document(40, 40)
        child = Layer("child", [Stroke([Point(20, 20, 20)])])
        group = document.make_group("group", [child])
        group.opacity = 0.5
        document.layers = [group]
        image = self._render(document)
        alpha = image.pixelColor(20, 20).alpha()
        self.assertGreater(alpha, 90)
        self.assertLess(alpha, 180)

    def test_clip_uses_sibling_inside_a_group(self) -> None:
        document = Document(60, 60)
        base = Layer("base", [Stroke([Point(30, 30, 30)], (0, 0, 0))])
        clipped = Layer(
            "clipped",
            [Stroke([Point(10, 30, 10), Point(50, 30, 10)], (255, 0, 0))],
            clip="inside",
        )
        document.layers = [document.make_group("group", [base, clipped])]
        image = self._render(document)
        inside = image.pixelColor(30, 30)
        self.assertEqual((inside.red(), inside.green(), inside.blue()), (255, 0, 0))
        outside = image.pixelColor(10, 30)
        self.assertEqual((outside.red(), outside.green(), outside.blue()), (0, 0, 0))
        self.assertEqual(image.pixelColor(50, 30).alpha(), 0)

    def test_hidden_base_still_clips_inside(self) -> None:
        document = Document(60, 60)
        base = Layer("base", [Stroke([Point(30, 30, 30)], (0, 0, 0))], visible=False)
        band = Layer(
            "band",
            [Stroke([Point(10, 30, 10), Point(50, 30, 10)], (255, 0, 0))],
            clip="inside",
        )
        document.layers = [base, band]
        image = self._render(document)
        inside = image.pixelColor(30, 30)
        self.assertEqual((inside.red(), inside.green(), inside.blue()), (255, 0, 0))
        self.assertEqual(image.pixelColor(10, 30).alpha(), 0)

    def test_hidden_base_clips_outside(self) -> None:
        document = Document(60, 60)
        base = Layer("base", [Stroke([Point(30, 30, 30)], (0, 0, 0))], visible=False)
        band = Layer(
            "band",
            [Stroke([Point(10, 30, 10), Point(50, 30, 10)], (255, 0, 0))],
            clip="outside",
        )
        document.layers = [base, band]
        image = self._render(document)
        self.assertEqual(image.pixelColor(30, 30).alpha(), 0)
        outside = image.pixelColor(10, 30)
        self.assertEqual((outside.red(), outside.green(), outside.blue()), (255, 0, 0))

    def test_hidden_group_base_still_clips(self) -> None:
        document = Document(60, 60)
        group = document.make_group(
            "group", [Layer("dot", [Stroke([Point(30, 30, 30)], (0, 0, 0))])]
        )
        group.visible = False
        band = Layer(
            "band",
            [Stroke([Point(10, 30, 10), Point(50, 30, 10)], (255, 0, 0))],
            clip="inside",
        )
        document.layers = [group, band]
        image = self._render(document)
        inside = image.pixelColor(30, 30)
        self.assertEqual((inside.red(), inside.green(), inside.blue()), (255, 0, 0))
        self.assertEqual(image.pixelColor(10, 30).alpha(), 0)

    def test_group_can_be_the_clip_base(self) -> None:
        document = Document(60, 60)
        group = document.make_group(
            "group", [Layer("dot", [Stroke([Point(30, 30, 30)], (0, 0, 0))])]
        )
        clipped = Layer(
            "clipped",
            [Stroke([Point(10, 30, 10), Point(50, 30, 10)], (255, 0, 0))],
            clip="inside",
        )
        document.layers = [group, clipped]
        image = self._render(document)
        self.assertEqual(image.pixelColor(10, 30).alpha(), 0)
        dot = image.pixelColor(30, 30)
        self.assertEqual((dot.red(), dot.green(), dot.blue()), (255, 0, 0))

    def test_clip_to_layer_below(self) -> None:
        document = Document(100, 100)
        document.layers = [
            Layer("base", [Stroke([Point(20, 20, 20)])]),
            Layer(
                "clip",
                [Stroke([Point(80, 20, 20)]), Stroke([Point(21, 20, 20)])],
                clip="inside",
            ),
        ]
        image = self._render(document)
        self.assertGreater(image.pixelColor(21, 20).alpha(), 0)
        self.assertEqual(image.pixelColor(80, 20).alpha(), 0)

    def test_clip_outside_subtracts_the_layer_below(self) -> None:
        document = Document(100, 100)
        document.layers = [
            Layer("base", [Stroke([Point(20, 20, 20)], (0, 0, 0))]),
            Layer(
                "clip",
                [
                    Stroke([Point(80, 20, 20)], (255, 0, 0)),
                    Stroke([Point(21, 20, 20)], (255, 0, 0)),
                ],
                clip="outside",
            ),
        ]
        image = self._render(document)
        # inside the base only the base colour survives, outside the clip shows
        inside = image.pixelColor(21, 20)
        self.assertEqual((inside.red(), inside.green(), inside.blue()), (0, 0, 0))
        outside = image.pixelColor(80, 20)
        self.assertEqual((outside.red(), outside.green(), outside.blue()), (255, 0, 0))

    def test_clip_off_draws_everything(self) -> None:
        document = Document(100, 100)
        document.layers = [
            Layer("base", [Stroke([Point(20, 20, 20)])]),
            Layer("clip", [Stroke([Point(80, 20, 20)])], clip="off"),
        ]
        image = self._render(document)
        self.assertGreater(image.pixelColor(80, 20).alpha(), 0)

    def test_walk_lists_groups_with_depth(self) -> None:
        document = Document(32, 32)
        inner = Layer("inner")
        group = document.make_group("group", [inner])
        document.layers = [group, Layer("top")]
        self.assertEqual(
            [(layer.name, depth) for layer, depth in document.walk()],
            [("group", 0), ("inner", 1), ("top", 0)],
        )
        self.assertEqual([layer.name for layer in document.all_layers()], ["inner", "top"])

    def test_group_layer_wraps_and_ungroup_dissolves(self) -> None:
        document = Document(32, 32)
        document.layers = [Layer("a"), Layer("b")]
        group = document.group_layer(document.layers[1])
        self.assertIsNotNone(group)
        assert group is not None
        self.assertEqual(document.layers[0].name, "a")
        self.assertTrue(document.layers[1].is_group)
        self.assertIs(document.layers[1].children[0], group.children[0])
        first = document.ungroup(group)
        self.assertIsNotNone(first)
        self.assertEqual([layer.name for layer in document.layers], ["a", "b"])

    def test_move_layers_reorders_and_keeps_order(self) -> None:
        document = Document(32, 32)
        document.layers = [Layer("a"), Layer("b"), Layer("c")]
        a, b, _c = document.layers
        moved = document.move_layers([a, b], document.layers, 3)
        self.assertEqual(moved, [a, b])
        self.assertEqual([layer.name for layer in document.layers], ["c", "a", "b"])

    def test_move_layers_into_and_out_of_a_group(self) -> None:
        document = Document(32, 32)
        child = Layer("child")
        group = document.make_group("group", [child])
        outside = Layer("outside")
        document.layers = [group, outside]
        document.move_layers([outside], group.children, 0)
        self.assertEqual([layer.name for layer in group.children], ["outside", "child"])
        document.move_layers([outside], document.layers, len(document.layers))
        self.assertEqual([layer.name for layer in group.children], ["child"])
        self.assertEqual([layer.name for layer in document.layers], ["group", "outside"])

    def test_move_layers_refuses_group_into_itself(self) -> None:
        document = Document(32, 32)
        inner = Layer("inner")
        group = document.make_group("group", [inner])
        document.layers = [group]
        self.assertEqual(document.move_layers([group], group.children, 0), [])
        self.assertEqual(group.children, [inner])

    def test_group_layers_wraps_the_whole_selection(self) -> None:
        document = Document(32, 32)
        document.layers = [Layer("a"), Layer("b"), Layer("c")]
        a, b, _c = document.layers
        group = document.group_layers([a, b])
        self.assertIsNotNone(group)
        assert group is not None
        self.assertEqual([layer.name for layer in group.children], ["a", "b"])
        self.assertEqual([layer.name for layer in document.layers], ["Group 1", "c"])

    def test_move_selection_keeps_a_block_together(self) -> None:
        document = Document(32, 32)
        document.layers = [Layer("a"), Layer("b"), Layer("c")]
        a, b, _c = document.layers
        document.move_selection([a, b], 1)
        self.assertEqual([layer.name for layer in document.layers], ["c", "a", "b"])
        document.move_selection([a, b], -1)
        self.assertEqual([layer.name for layer in document.layers], ["a", "b", "c"])

    def test_insert_below_inside_a_group(self) -> None:
        document = Document(32, 32)
        child = Layer("child")
        group = document.make_group("group", [child])
        document.layers = [group]
        new_layer = Layer("new")
        index = document.insert_below(child, new_layer)
        self.assertEqual([item.name for item in group.children], ["new", "child"])
        self.assertEqual(index, document.walk_index(new_layer))

    def test_clip_roundtrip(self) -> None:
        document = Document(64, 64)
        document.layers[0].clip = "outside"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "doc.json"
            io.save_document(document, path)
            loaded = io.load_document(path)
        self.assertEqual(loaded.layers[0].clip, "outside")

    def test_legacy_clip_boolean_loads_as_inside(self) -> None:
        document = Document.from_dict(
            {
                "width": 32,
                "height": 32,
                "layers": [
                    {"name": "a", "strokes": []},
                    {"name": "b", "strokes": [], "clip": True},
                ],
            }
        )
        self.assertEqual(document.layers[1].clip, "inside")
        self.assertEqual(document.layers[0].clip, "off")


class TransformTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_rotate_vector_layer(self) -> None:
        layer = Layer(strokes=[Stroke([Point(10, 0, 2), Point(20, 0, 2)])])
        self.assertTrue(transform.rotate_layer(layer, 90.0, 100, 100))
        points = layer.strokes[0].points
        self.assertAlmostEqual(points[0].x, 15.0)
        self.assertAlmostEqual(points[0].y, -5.0)
        self.assertAlmostEqual(points[1].x, 15.0)
        self.assertAlmostEqual(points[1].y, 5.0)

    def test_scale_vector_layer(self) -> None:
        layer = Layer(strokes=[Stroke([Point(10, 0, 2), Point(20, 0, 2)])])
        self.assertTrue(transform.scale_layer(layer, 2.0, 100, 100))
        points = layer.strokes[0].points
        self.assertAlmostEqual(points[0].x, 5.0)
        self.assertAlmostEqual(points[1].x, 25.0)
        self.assertAlmostEqual(points[0].width, 4.0)

    def test_rotate_and_scale_raster_layer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "img.png"
            image = QImage(40, 20, QImage.Format.Format_ARGB32_Premultiplied)
            image.fill(QColor("red"))
            image.save(str(path))
            layer = io.load_raster_layer(path)

        self.assertTrue(transform.rotate_layer(layer, 45.0, 100, 100))
        self.assertIsNotNone(layer.image_base64)
        self.assertTrue(transform.scale_layer(layer, 0.5, 100, 100))
        decoded = layer_image(layer)
        self.assertIsNotNone(decoded)
        assert decoded is not None
        self.assertEqual(decoded.width(), 100)


class FillTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _document(self) -> Document:
        square = Stroke(
            [
                Point(10, 10, 3),
                Point(30, 10, 3),
                Point(30, 30, 3),
                Point(10, 30, 3),
                Point(10, 10, 3),
            ]
        )
        document = Document(40, 40)
        document.layers = [
            Layer("fill", kind="raster"),
            Layer("line", [square]),
        ]
        document.active_layer = 0
        return document

    def test_bucket_fill_inside_closed_line(self) -> None:
        document = self._document()
        self.assertTrue(fill.bucket_fill(document, document.layers[0], 20, 20, (255, 0, 0), 255))
        image = layer_image(document.layers[0])
        self.assertIsNotNone(image)
        assert image is not None
        inside = image.pixelColor(20, 20)
        self.assertGreater(inside.alpha(), 0)
        self.assertEqual(inside.name(), "#ff0000")
        self.assertEqual(image.pixelColor(3, 3).alpha(), 0)

    def test_bucket_fill_on_line_does_nothing(self) -> None:
        document = self._document()
        self.assertFalse(fill.bucket_fill(document, document.layers[0], 10, 10, (255, 0, 0), 255))

    def test_open_region_uses_canvas_as_boundary(self) -> None:
        document = Document(60, 40)
        underlay_image = QImage(60, 40, QImage.Format.Format_ARGB32_Premultiplied)
        underlay_image.fill(QColor("white"))
        underlay = Layer("underlay", kind="raster", image_base64=io.encode_image(underlay_image))
        line = Stroke([Point(-5, 20, 4), Point(65, 20, 4)])
        document.layers = [underlay, Layer("fill", kind="raster"), Layer("line", [line])]
        document.active_layer = 1

        self.assertTrue(fill.bucket_fill(document, document.layers[1], 30, 8, (0, 128, 255), 255))
        image = layer_image(document.layers[1])
        self.assertIsNotNone(image)
        assert image is not None
        self.assertGreater(image.pixelColor(30, 8).alpha(), 0)
        self.assertEqual(image.pixelColor(30, 32).alpha(), 0)

    def test_convert_vector_to_fill(self) -> None:
        canvas = Canvas(Document(40, 40))
        stroke = Stroke([Point(10, 10, 4), Point(30, 10, 4), Point(30, 30, 4)])
        canvas.doc.layer().strokes.append(stroke)
        canvas.color = (0, 0, 255)
        canvas.alpha = 200
        self.assertTrue(canvas.convert_to_fill())
        self.assertEqual(len(canvas.doc.layers), 2)
        fill_layer = canvas.doc.layer()
        self.assertEqual(fill_layer.kind, "vector")
        self.assertIs(fill_layer, canvas.doc.layers[0])
        converted = fill_layer.strokes[0]
        self.assertTrue(converted.closed)
        self.assertEqual(converted.fill, (0, 0, 255))
        self.assertEqual(converted.fill_alpha, 200)
        self.assertTrue(all(point.width == 0.0 for point in converted.points))
        # the source layer is untouched and sits above the fill
        self.assertIs(canvas.doc.layers[1].strokes[0], stroke)
        self.assertEqual(stroke.closed, False)

    def test_scope_image_reaches_other_layers(self) -> None:
        canvas = Canvas(Document(60, 60))
        first = Stroke([Point(10, 10, 3), Point(50, 10, 3)])
        canvas.doc.layers[0].strokes.append(first)
        canvas.doc.layers.append(Layer("top", [Stroke([Point(10, 50, 3), Point(50, 50, 3)])]))
        canvas.doc.active_layer = 1
        canvas.tool = Canvas.TOOL_EDIT
        self.assertIsNone(canvas._find_point(QPointF(10, 10))[0])
        canvas.set_scope("image")
        self.assertIs(canvas._find_point(QPointF(10, 10))[0], first)

    def test_filled_stroke_and_group_roundtrip(self) -> None:
        document = Document(60, 60)
        line = Stroke([Point(10, 10, 3), Point(50, 10, 3), Point(50, 50, 3), Point(10, 50, 3)])
        document.layers[0].strokes.append(line)
        filled = Stroke(
            [Point(10, 10, 0), Point(50, 10, 0), Point(50, 50, 0)], closed=True, fill=(0, 0, 255)
        )
        filled.fill_alpha = 200
        document.layers.insert(0, Layer("fill", strokes=[filled]))
        group = document.make_group("group", [document.layers[0], document.layers[1]])
        document.layers = [group]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "vector.json"
            io.save_document(document, path)
            loaded = io.load_document(path)
        self.assertTrue(loaded.layers[0].is_group)
        self.assertEqual(len(loaded.layers[0].children), 2)
        loaded_fill = loaded.layers[0].children[0].strokes[0]
        self.assertTrue(loaded_fill.closed)
        self.assertEqual(loaded_fill.fill, (0, 0, 255))
        self.assertEqual(loaded_fill.fill_alpha, 200)
        self.assertEqual(loaded.layers[0].children[1].uid, document.layers[0].children[1].uid)

    def test_fill_tool_on_raster_creates_fill_layer(self) -> None:
        canvas = Canvas(Document(40, 40))
        stroke = Stroke(
            [
                Point(10, 10, 3),
                Point(30, 10, 3),
                Point(30, 30, 3),
                Point(10, 30, 3),
                Point(10, 10, 3),
            ]
        )
        canvas.doc.layers.insert(0, Layer("fill", kind="raster"))
        canvas.doc.layers[1].strokes.append(stroke)
        canvas.doc.active_layer = 0
        canvas.tool = Canvas.TOOL_FILL
        canvas.color = (0, 0, 255)
        canvas._fill_click(QPointF(20, 20))
        self.assertEqual(canvas.doc.layers[0].kind, "raster")
        image = layer_image(canvas.doc.layers[0])
        self.assertIsNotNone(image)
        assert image is not None
        self.assertGreater(image.pixelColor(20, 20).alpha(), 0)

    def test_bucket_fill_inside_a_group(self) -> None:
        document = Document(40, 40)
        square = Stroke(
            [
                Point(10, 10, 3),
                Point(30, 10, 3),
                Point(30, 30, 3),
                Point(10, 30, 3),
                Point(10, 10, 3),
            ],
            closed=True,
        )
        raster = Layer("fill", kind="raster")
        document.layers = [document.make_group("group", [raster, Layer("line", [square])])]
        self.assertTrue(fill.bucket_fill(document, raster, 20, 20, (255, 0, 0), 255))
        image = layer_image(raster)
        self.assertIsNotNone(image)
        assert image is not None
        self.assertGreater(image.pixelColor(20, 20).alpha(), 0)
        self.assertEqual(image.pixelColor(2, 2).alpha(), 0)

    def test_fill_duplicates_inside_the_group(self) -> None:
        document = Document(40, 40)
        line = Stroke([Point(10, 10, 3), Point(30, 10, 3), Point(30, 30, 3), Point(10, 30, 3)])
        source = Layer("source", [line])
        group = document.make_group("group", [source])
        document.layers = [group]
        document.active_layer = document.walk_index(source)
        canvas = Canvas(document)
        canvas.tool = Canvas.TOOL_FILL
        canvas._fill_click(QPointF(20, 20))
        self.assertEqual(len(group.children), 2)
        self.assertEqual(len(document.layers), 1)
        self.assertEqual(group.children[0].name, "Fill 2")
        self.assertIs(group.children[1], source)
        self.assertIsNone(line.fill)

    def test_fill_marks_the_stroke_under_the_cursor(self) -> None:
        canvas = Canvas(Document(40, 40))
        line = Stroke([Point(10, 10, 3), Point(30, 10, 3), Point(30, 30, 3), Point(10, 30, 3)])
        canvas.doc.layer().strokes.append(line)
        canvas.tool = Canvas.TOOL_FILL
        canvas.color = (0, 0, 255)
        canvas._fill_click(QPointF(20, 20))
        self.assertEqual(len(canvas.doc.layers), 2)
        self.assertEqual(canvas.doc.active_layer, 0)
        fill_layer = canvas.doc.layers[0]
        self.assertEqual(fill_layer.name, "Fill 1")
        self.assertIs(canvas.doc.layers[1].strokes[0], line)
        # the source stays untouched, the fill lives on the duplicated layer
        self.assertIsNone(line.fill)
        self.assertFalse(line.bridge)
        copy = fill_layer.strokes[0]
        self.assertIsNot(copy, line)
        self.assertNotEqual(copy.uid, line.uid)
        self.assertEqual(copy.fill, (0, 0, 255))
        self.assertEqual(copy.fill_alpha, 255)
        self.assertTrue(copy.bridge)
        self.assertFalse(copy.closed)

    def test_fill_bridge_is_a_straight_segment(self) -> None:
        canvas = Canvas(Document(40, 40))
        line = Stroke([Point(10, 10, 3), Point(30, 10, 3), Point(30, 30, 3), Point(10, 30, 3)])
        canvas.doc.layer().strokes.append(line)
        canvas.tool = Canvas.TOOL_FILL
        canvas._fill_click(QPointF(20, 20))
        samples = sample_stroke(canvas.doc.layers[0].strokes[0])
        # the last sample returns to the first point: one straight bridge
        self.assertAlmostEqual(samples[-1][0], samples[0][0])
        self.assertAlmostEqual(samples[-1][1], samples[0][1])

    def test_fill_follows_point_edits(self) -> None:
        canvas = Canvas(Document(40, 40))
        line = Stroke([Point(10, 10, 3), Point(30, 10, 3), Point(30, 30, 3), Point(10, 30, 3)])
        canvas.doc.layer().strokes.append(line)
        canvas.tool = Canvas.TOOL_FILL
        canvas._fill_click(QPointF(20, 20))
        filled = canvas.doc.layers[0].strokes[0]
        before = stroke_path(filled).boundingRect()
        filled.points[1].x = 36.0
        after = stroke_path(filled).boundingRect()
        self.assertGreater(after.width(), before.width())
        self.assertEqual(line.points[1].x, 30.0)  # the source is not edited
        canvas.tool = Canvas.TOOL_EDIT
        self.assertIs(canvas._find_point(QPointF(36.0, 10.0))[0], filled)

    def test_fill_chains_strokes_into_one_figure(self) -> None:
        canvas = Canvas(Document(40, 40))
        top = Stroke([Point(10, 10, 3), Point(30, 10, 3), Point(30, 20, 3)])
        bottom = Stroke([Point(30, 20, 3), Point(30, 30, 3), Point(10, 30, 3), Point(10, 10, 3)])
        canvas.doc.layer().strokes.extend([top, bottom])
        canvas.tool = Canvas.TOOL_FILL
        canvas.color = (0, 0, 255)
        canvas._fill_click(QPointF(20, 20))
        self.assertEqual(len(canvas.doc.layers), 2)
        layer = canvas.doc.layers[0]
        self.assertEqual(len(layer.strokes), 1)
        self.assertEqual(len(canvas.doc.layers[1].strokes), 2)  # originals untouched
        merged = layer.strokes[0]
        self.assertTrue(merged.fill is not None)
        self.assertEqual(merged.closed, True)
        self.assertFalse(merged.bridge)
        self.assertGreaterEqual(len(merged.points), 5)
        self.assertTrue(all(point.width == 0 for point in merged.points))
        # the merged outline is the same geometry, so it contains the click point
        self.assertTrue(stroke_path(merged).contains(QPointF(20, 20)))

    def test_fill_preview_highlights_the_figure_under_the_cursor(self) -> None:
        canvas = Canvas(Document(40, 40))
        line = Stroke([Point(10, 10, 3), Point(30, 10, 3), Point(30, 30, 3), Point(10, 30, 3)])
        canvas.doc.layer().strokes.append(line)
        canvas.tool = Canvas.TOOL_FILL
        canvas.color = (0, 0, 255)
        canvas.request_fill_preview(QPointF(20, 20))
        self.assertIsNotNone(canvas._fill_preview_path)
        canvas.request_fill_preview(QPointF(2, 2))
        self.assertIsNone(canvas._fill_preview_path)
        self.assertFalse(line.closed)
        self.assertFalse(line.bridge)

    def test_fill_preview_survives_large_canvas(self) -> None:
        canvas = Canvas(Document(2000, 2000))
        line = Stroke(
            [Point(100, 100, 6), Point(900, 100, 6), Point(900, 900, 6), Point(100, 900, 6)]
        )
        canvas.doc.layer().strokes.append(line)
        canvas.tool = Canvas.TOOL_FILL
        canvas.request_fill_preview(QPointF(500, 500))
        self.assertIsNotNone(canvas._fill_preview_path)

    def test_document_change_clears_the_preview(self) -> None:
        canvas = Canvas(Document(40, 40))
        line = Stroke([Point(10, 10, 3), Point(30, 10, 3), Point(30, 30, 3), Point(10, 30, 3)])
        canvas.doc.layer().strokes.append(line)
        canvas.tool = Canvas.TOOL_FILL
        canvas.request_fill_preview(QPointF(20, 20))
        self.assertIsNotNone(canvas._fill_preview_path)
        canvas.documentChanged.emit()
        self.assertIsNone(canvas._fill_preview_path)

    def test_fill_requires_a_closed_figure(self) -> None:
        canvas = Canvas(Document(40, 40))
        line = Stroke([Point(10, 10, 3), Point(30, 10, 3), Point(30, 30, 3)])
        canvas.doc.layer().strokes.append(line)
        canvas.tool = Canvas.TOOL_FILL
        canvas._fill_click(QPointF(5, 35))
        self.assertIsNone(line.fill)
        self.assertFalse(line.bridge)


class TransformModeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _canvas(self) -> Canvas:
        canvas = Canvas(Document(100, 100))
        stroke = Stroke(
            [
                Point(10, 10, 2),
                Point(50, 10, 2),
                Point(50, 50, 2),
                Point(10, 50, 2),
            ]
        )
        canvas.doc.layer().strokes.append(stroke)
        return canvas

    def test_transform_hit_regions(self) -> None:
        canvas = self._canvas()
        self.assertTrue(canvas.begin_transform())
        self.assertEqual(canvas._transform_hit(QPointF(10, 30)), "scale-x")
        self.assertEqual(canvas._transform_hit(QPointF(30, 10)), "scale-y")
        self.assertEqual(canvas._transform_hit(QPointF(30, 30)), "move")
        self.assertEqual(canvas._transform_hit(QPointF(200, 200)), "rotate")

    def test_rotate_snaps_to_15_degrees_with_shift(self) -> None:
        canvas = self._canvas()
        self.assertTrue(canvas.begin_transform())
        pivot = canvas._tr_pivot
        canvas._tr_action = "rotate"
        canvas._tr_start = QPointF(pivot.x() + 10, pivot.y())
        canvas._tr_start_angle = 0.0
        canvas._update_transform_drag(
            QPointF(pivot.x(), pivot.y() + 10), Qt.KeyboardModifier.ShiftModifier
        )
        self.assertAlmostEqual(canvas.transform_parameters()[0], 90.0)

    def test_set_transform_values(self) -> None:
        canvas = self._canvas()
        self.assertTrue(canvas.begin_transform())
        canvas.set_transform_values(90.0, 1.0, 1.0, 0.0, 0.0)
        points = canvas.doc.layer().strokes[0].points
        # bbox centre is (30, 30); (10, 10) rotates to (50, 10).
        self.assertAlmostEqual(points[0].x, 50.0, delta=0.01)
        self.assertAlmostEqual(points[0].y, 10.0, delta=0.01)
        canvas.cancel_transform()

    def test_cancel_restores_layer(self) -> None:
        canvas = self._canvas()
        original = [(p.x, p.y) for p in canvas.doc.layer().strokes[0].points]
        self.assertTrue(canvas.begin_transform())
        canvas._tr_dx = 25.0
        canvas._apply_live_transform()
        canvas.cancel_transform()
        restored = canvas.doc.layer().strokes[0].points
        self.assertEqual([(p.x, p.y) for p in restored], original)

    def test_middle_button_pan_does_not_stick_in_transform_mode(self) -> None:
        canvas = self._canvas()
        canvas.resize(300, 300)
        self.assertTrue(canvas.begin_transform())
        QTest.mousePress(canvas, Qt.MouseButton.MiddleButton, pos=QPoint(10, 10))
        self.assertTrue(canvas._panning)
        QTest.mouseRelease(canvas, Qt.MouseButton.MiddleButton, pos=QPoint(40, 40))
        self.assertFalse(canvas._panning)
        pan = QPointF(canvas.pan)
        QTest.mouseMove(canvas, QPoint(120, 120))
        self.assertEqual(canvas.pan, pan)

    def test_commit_is_undoable(self) -> None:
        canvas = self._canvas()
        original = [(p.x, p.y) for p in canvas.doc.layer().strokes[0].points]
        self.assertTrue(canvas.begin_transform())
        canvas._tr_dx = 25.0
        canvas._apply_live_transform()
        canvas.commit_transform()
        moved = canvas.doc.layer().strokes[0].points
        self.assertNotEqual([(p.x, p.y) for p in moved], original)
        canvas.undo()
        restored = canvas.doc.layer().strokes[0].points
        self.assertEqual([(p.x, p.y) for p in restored], original)


class CanvasTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_draw_then_undo(self) -> None:
        canvas = Canvas(Document())
        canvas._draw_click(QPointF(5, 5))
        canvas._draw_click(QPointF(15, 5))
        canvas.finish_stroke()
        self.assertEqual(len(canvas.doc.layer().strokes), 1)
        self.assertEqual(len(canvas.doc.layer().strokes[0].points), 2)
        canvas.undo()
        self.assertEqual(len(canvas.doc.layer().strokes), 0)
        canvas.redo()
        self.assertEqual(len(canvas.doc.layer().strokes), 1)

    def test_wheel_width_change_is_undoable(self) -> None:
        canvas = Canvas(Document())
        stroke = Stroke([Point(0, 0, 4), Point(50, 0, 4)])
        canvas.doc.layer().strokes.append(stroke)
        canvas.tool = Canvas.TOOL_EDIT
        canvas._hover = (stroke, 0)
        canvas._adjust_point_width(2.0)
        canvas._adjust_point_width(2.0)
        self.assertAlmostEqual(stroke.points[0].width, 8.0)
        self.assertEqual(len(canvas._undo_stack), 1)
        canvas.undo()
        self.assertAlmostEqual(canvas.doc.layer().strokes[0].points[0].width, 4.0)
        canvas.redo()
        self.assertAlmostEqual(canvas.doc.layer().strokes[0].points[0].width, 8.0)

    def test_wheel_width_on_another_point_is_a_new_undo(self) -> None:
        canvas = Canvas(Document())
        stroke = Stroke([Point(0, 0, 4), Point(50, 0, 4)])
        canvas.doc.layer().strokes.append(stroke)
        canvas.tool = Canvas.TOOL_EDIT
        canvas._hover = (stroke, 0)
        canvas._adjust_point_width(1.0)
        canvas._hover = (stroke, 1)
        canvas._adjust_point_width(1.0)
        self.assertEqual(len(canvas._undo_stack), 2)
        canvas.undo()
        widths = [point.width for point in canvas.doc.layer().strokes[0].points]
        self.assertEqual(widths, [5.0, 4.0])

    def test_erase_splits_stroke(self) -> None:
        canvas = Canvas(Document())
        stroke = Stroke([Point(0, 0, 2), Point(10, 0, 2), Point(20, 0, 2)])
        canvas.doc.layer().strokes.append(stroke)
        canvas._erase_at(QPointF(10, 0))
        remaining = canvas.doc.layer().strokes
        self.assertEqual(len(remaining), 2)
        self.assertEqual([len(s.points) for s in remaining], [1, 1])

    def test_edit_click_inserts_point_on_curve(self) -> None:
        canvas = Canvas(Document())
        stroke = Stroke(
            [
                Point(0, 0, 4),
                Point(50, 0, 4, True),
                Point(50, 50, 4, True),
                Point(50, 100, 4),
            ]
        )
        canvas.doc.layer().strokes.append(stroke)
        canvas.tool = Canvas.TOOL_EDIT
        samples = sample_stroke(stroke)
        sample = max(
            samples,
            key=lambda s: min(math.hypot(s[0] - p.x, s[1] - p.y) for p in stroke.points),
        )
        canvas._edit_click(QPointF(sample[0], sample[1]))
        self.assertEqual(len(stroke.points), 5)
        inserted = min(stroke.points, key=lambda p: math.hypot(p.x - sample[0], p.y - sample[1]))
        self.assertAlmostEqual(inserted.x, sample[0], delta=0.5)
        self.assertAlmostEqual(inserted.y, sample[1], delta=0.5)
        self.assertAlmostEqual(inserted.width, 4.0, delta=0.01)

    def test_edit_click_inserts_point_on_first_segment(self) -> None:
        canvas = Canvas(Document())
        stroke = Stroke([Point(0, 0, 4, True), Point(200, 0, 4, True), Point(200, 200, 4, True)])
        canvas.doc.layer().strokes.append(stroke)
        canvas.tool = Canvas.TOOL_EDIT
        sample = sample_stroke(stroke)[3]
        canvas._edit_click(QPointF(sample[0], sample[1]))
        self.assertEqual(len(stroke.points), 4)
        inserted = min(stroke.points, key=lambda p: math.hypot(p.x - sample[0], p.y - sample[1]))
        self.assertAlmostEqual(inserted.x, sample[0], delta=0.5)
        self.assertAlmostEqual(inserted.y, sample[1], delta=0.5)

    def test_edit_click_far_away_inserts_nothing(self) -> None:
        canvas = Canvas(Document())
        stroke = Stroke([Point(0, 0, 4), Point(100, 0, 4)])
        canvas.doc.layer().strokes.append(stroke)
        canvas.tool = Canvas.TOOL_EDIT
        canvas._edit_click(QPointF(500, 500))
        self.assertEqual(len(stroke.points), 2)

    def test_preview_does_not_mutate_document(self) -> None:
        canvas = Canvas(Document())
        canvas._draw_click(QPointF(10, 10))
        canvas._cursor = QPointF(60, 10)
        preview = canvas._preview_stroke()
        self.assertIsNotNone(preview)
        assert preview is not None
        self.assertEqual(len(preview.points), 2)
        self.assertEqual(len(canvas.doc.layer().strokes[0].points), 1)

    def test_zoom_keeps_point_under_cursor(self) -> None:
        canvas = Canvas(Document())
        canvas.zoom = 1.0
        canvas.pan = QPointF(40, 40)
        position = QPointF(100, 100)
        before = canvas.to_scene(position)
        canvas._zoom_at(position, 2.0)
        after = canvas.to_scene(position)
        self.assertAlmostEqual(before.x(), after.x(), delta=1e-9)
        self.assertAlmostEqual(before.y(), after.y(), delta=1e-9)
        self.assertAlmostEqual(canvas.zoom, 2.0)

    def test_view_rotation_roundtrip(self) -> None:
        canvas = Canvas(Document(100, 100))
        canvas.resize(400, 300)
        canvas.zoom = 2.0
        canvas.set_rotation(90.0)
        scene = QPointF(30.0, 40.0)
        screen = canvas.scene_to_screen(scene)
        restored = canvas.to_scene(screen)
        self.assertAlmostEqual(restored.x(), scene.x(), delta=1e-9)
        self.assertAlmostEqual(restored.y(), scene.y(), delta=1e-9)
        # 90° clockwise: scene +x points down the screen, +y points left.
        self.assertAlmostEqual(screen.x() - canvas.pan.x(), -40.0 * 2.0, delta=1e-9)
        self.assertAlmostEqual(screen.y() - canvas.pan.y(), 30.0 * 2.0, delta=1e-9)

    def test_rotation_keeps_widget_centre_anchored(self) -> None:
        canvas = Canvas(Document(100, 100))
        canvas.resize(400, 300)
        center = QPointF(200, 150)
        before = canvas.to_scene(center)
        canvas.set_rotation(90.0)
        after = canvas.to_scene(center)
        self.assertAlmostEqual(before.x(), after.x(), delta=1e-9)
        self.assertAlmostEqual(before.y(), after.y(), delta=1e-9)

    def test_zoom_keeps_point_under_cursor_when_rotated(self) -> None:
        canvas = Canvas(Document(100, 100))
        canvas.resize(400, 300)
        canvas.zoom = 1.0
        canvas.set_rotation(90.0)
        position = QPointF(120, 90)
        before = canvas.to_scene(position)
        canvas._zoom_at(position, 2.0)
        after = canvas.to_scene(position)
        self.assertAlmostEqual(before.x(), after.x(), delta=1e-9)
        self.assertAlmostEqual(before.y(), after.y(), delta=1e-9)

    def test_view_rotation_does_not_change_export(self) -> None:
        canvas = Canvas(Document(40, 30))
        canvas.set_rotation(90.0)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.png"
            io.export_png(canvas.doc, path, scale=1.0)
            image = QImage(str(path))
        self.assertEqual((image.width(), image.height()), (40, 30))

    def _recolor_canvas(self) -> tuple[Canvas, Stroke, Stroke]:
        canvas = Canvas(Document(100, 100))
        first = Stroke([Point(10, 10, 4), Point(90, 10, 4)])
        second = Stroke([Point(10, 60, 4), Point(90, 60, 4)])
        canvas.doc.layer().strokes.extend([first, second])
        canvas.tool = Canvas.TOOL_RECOLOR
        canvas.color = (200, 10, 30)
        canvas.alpha = 90
        return canvas, first, second

    def test_recolor_tool_recolors_clicked_stroke(self) -> None:
        canvas, first, second = self._recolor_canvas()
        canvas._recolor_dirty = False
        canvas._recolor_gesture(QPointF(50, 10))
        self.assertEqual(first.color, (200, 10, 30))
        self.assertEqual(first.alpha, 90)
        self.assertEqual(second.color, (20, 20, 20))

    def test_recolor_gesture_is_a_single_undo(self) -> None:
        canvas, _first, second = self._recolor_canvas()
        canvas._recolor_dirty = False
        canvas._recolor_gesture(QPointF(50, 10))
        canvas._recolor_gesture(QPointF(50, 60))
        self.assertEqual(second.color, (200, 10, 30))
        canvas.undo()
        restored = canvas.doc.layer().strokes
        self.assertEqual([stroke.color for stroke in restored], [(20, 20, 20), (20, 20, 20)])

    def test_recolor_miss_does_not_push_undo(self) -> None:
        canvas, first, _ = self._recolor_canvas()
        canvas._recolor_dirty = False
        canvas._recolor_gesture(QPointF(80, 80))
        self.assertFalse(canvas._undo_stack)
        self.assertEqual(first.color, (20, 20, 20))

    def test_recolor_skips_hidden_layers(self) -> None:
        canvas = Canvas(Document(100, 100))
        hidden = Stroke([Point(10, 10, 4), Point(90, 10, 4)])
        document = canvas.doc
        document.layers[0].strokes.append(hidden)
        document.layers[0].visible = False
        document.layers.append(Layer("empty"))
        document.active_layer = 1
        canvas.tool = Canvas.TOOL_RECOLOR
        canvas.color = (200, 10, 30)
        canvas._recolor_dirty = False
        canvas._recolor_gesture(QPointF(50, 10))
        self.assertEqual(hidden.color, (20, 20, 20))
        self.assertFalse(canvas._undo_stack)

    def test_recolor_ignores_strokes_on_other_layers(self) -> None:
        canvas = Canvas(Document(100, 100))
        foreign = Stroke([Point(10, 10, 4), Point(90, 10, 4)])
        document = canvas.doc
        document.layers[0].strokes.append(foreign)
        document.layers.append(Layer("top"))
        document.active_layer = 1
        canvas.tool = Canvas.TOOL_RECOLOR
        canvas.color = (200, 10, 30)
        canvas._recolor_dirty = False
        canvas._recolor_gesture(QPointF(50, 10))
        self.assertEqual(foreign.color, (20, 20, 20))
        self.assertFalse(canvas._undo_stack)

    def test_recolor_hits_single_point_stroke(self) -> None:
        canvas = Canvas(Document(100, 100))
        dot = Stroke([Point(50, 50, 13)], alpha=255)
        canvas.doc.layer().strokes.append(dot)
        canvas.tool = Canvas.TOOL_RECOLOR
        canvas.color = (230, 30, 40)
        canvas._recolor_dirty = False
        # A 13 px wide dot is hit outside the 8 px cursor radius, and the dot has
        # no curve to measure against.
        canvas._recolor_gesture(QPointF(55, 50))
        self.assertEqual(dot.color, (230, 30, 40))
        self.assertIs(canvas._stroke_at(QPointF(55, 50)), dot)

    def test_edit_tracks_hovered_stroke_for_outline(self) -> None:
        canvas = Canvas(Document(100, 100))
        canvas.resize(200, 200)
        stroke = Stroke([Point(10, 10, 4), Point(90, 10, 4)])
        canvas.doc.layer().strokes.append(stroke)
        canvas.tool = Canvas.TOOL_EDIT
        position = canvas.scene_to_screen(QPointF(50, 10))
        event = QMouseEvent(
            QEvent.Type.MouseMove,
            position,
            position,
            Qt.MouseButton.NoButton,
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        )
        canvas.mouseMoveEvent(event)
        self.assertIs(canvas._hovered_stroke, stroke)

    def test_recolor_hover_uses_active_layer(self) -> None:
        canvas = Canvas(Document(100, 100))
        stroke = Stroke([Point(10, 10, 4), Point(90, 10, 4)])
        canvas.doc.layer().strokes.append(stroke)
        canvas.tool = Canvas.TOOL_RECOLOR
        self.assertIs(canvas._stroke_at(QPointF(50, 10)), stroke)
        canvas.doc.layers.append(Layer("top"))
        canvas.doc.active_layer = 1
        self.assertIsNone(canvas._stroke_at(QPointF(50, 10)))

    def test_edit_ignores_strokes_on_other_layers(self) -> None:
        canvas = Canvas(Document(100, 100))
        foreign = Stroke([Point(10, 10, 4), Point(90, 10, 4)])
        canvas.doc.layers[0].strokes.append(foreign)
        canvas.doc.layers.append(Layer("top"))
        canvas.doc.active_layer = 1
        canvas.tool = Canvas.TOOL_EDIT
        canvas._edit_click(QPointF(50, 10))
        self.assertEqual(len(foreign.points), 2)
        self.assertIsNone(canvas._find_point(QPointF(10, 10))[0])

    def test_hover_adjusts_point_width(self) -> None:
        canvas = Canvas(Document())
        stroke = Stroke([Point(0, 0, 4), Point(10, 0, 4)])
        canvas.doc.layer().strokes.append(stroke)
        canvas.tool = Canvas.TOOL_EDIT
        canvas._hover = (stroke, 1)
        self.assertTrue(canvas._adjust_point_width(2.0))
        self.assertAlmostEqual(stroke.points[1].width, 6.0)
        canvas._hover = None
        self.assertFalse(canvas._adjust_point_width(2.0))

    def test_draw_uses_active_color(self) -> None:
        canvas = Canvas(Document())
        canvas.color = (10, 120, 200)
        canvas.alpha = 128
        canvas._draw_click(QPointF(5, 5))
        canvas.finish_stroke()
        stroke = canvas.doc.layer().strokes[0]
        self.assertEqual(stroke.color, (10, 120, 200))
        self.assertEqual(stroke.alpha, 128)

    def test_zero_point_width_allowed(self) -> None:
        canvas = Canvas(Document())
        stroke = Stroke([Point(0, 0, 4), Point(10, 0, 4)])
        canvas.doc.layer().strokes.append(stroke)
        canvas.tool = Canvas.TOOL_EDIT
        canvas._hover = (stroke, 1)
        self.assertTrue(canvas._adjust_point_width(-100.0))
        self.assertEqual(stroke.points[1].width, 0.0)

    def test_zero_brush_width_places_zero_points(self) -> None:
        canvas = Canvas(Document())
        canvas.brush_width = 0.0
        canvas._draw_click(QPointF(5, 5))
        canvas.finish_stroke()
        self.assertEqual(canvas.doc.layer().strokes[0].points[0].width, 0.0)

    def test_delete_point_at_cursor(self) -> None:
        canvas = Canvas(Document())
        stroke = Stroke([Point(0, 0, 4), Point(10, 0, 4), Point(20, 0, 4)])
        canvas.doc.layer().strokes.append(stroke)
        canvas.tool = Canvas.TOOL_EDIT
        self.assertTrue(canvas._delete_point_at(QPointF(10, 0)))
        self.assertEqual([p.x for p in stroke.points], [0.0, 20.0])
        self.assertFalse(canvas._delete_point_at(QPointF(300, 300)))


class SelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _canvas(self, stroke: Stroke) -> Canvas:
        canvas = Canvas(Document(200, 200))
        canvas.doc.layer().strokes.append(stroke)
        canvas.tool = Canvas.TOOL_EDIT
        return canvas

    def test_box_selection_picks_points_inside(self) -> None:
        stroke = Stroke([Point(0, 0, 2), Point(50, 0, 2), Point(100, 0, 2)])
        canvas = self._canvas(stroke)
        count = canvas._select_in_rect(QRectF(-5, -5, 60, 10), add=False)
        self.assertEqual(count, 2)
        self.assertEqual(len(canvas.selected_points()), 2)

    def test_shift_click_toggles_selection(self) -> None:
        stroke = Stroke([Point(0, 0, 2), Point(50, 0, 2)])
        canvas = self._canvas(stroke)
        canvas._edit_press(QPointF(0, 0), True)
        self.assertEqual(len(canvas.selected_points()), 1)
        canvas._edit_press(QPointF(50, 0), True)
        self.assertEqual(len(canvas.selected_points()), 2)
        canvas._edit_press(QPointF(50, 0), True)
        self.assertEqual(len(canvas.selected_points()), 1)

    def test_plain_click_drags_without_selecting(self) -> None:
        stroke = Stroke([Point(0, 0, 2), Point(50, 0, 2)])
        canvas = self._canvas(stroke)
        canvas._edit_press(QPointF(0, 0), False)
        self.assertEqual(canvas.selected_points(), [])
        self.assertEqual(len(canvas._drag_starts), 1)
        self.assertIs(canvas._drag_starts[0][0], stroke.points[0])

    def test_group_drag_moves_all_selected_points(self) -> None:
        stroke = Stroke([Point(0, 0, 2), Point(50, 50, 2)])
        canvas = self._canvas(stroke)
        canvas._selected = [(stroke, 0), (stroke, 1)]
        canvas._drag_origin = QPointF(0, 0)
        canvas._drag_starts = [(point, point.x, point.y) for point in canvas.selected_points()]
        # Scene coordinates are pan + position; move by (25, 15) in scene space.
        position = canvas.pan + QPointF(25, 15)
        event = QMouseEvent(
            QEvent.Type.MouseMove,
            position,
            position,
            Qt.MouseButton.NoButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        canvas.mouseMoveEvent(event)
        self.assertAlmostEqual(stroke.points[0].x, 25.0)
        self.assertAlmostEqual(stroke.points[0].y, 15.0)
        self.assertAlmostEqual(stroke.points[1].x, 75.0)
        self.assertAlmostEqual(stroke.points[1].y, 65.0)

    def test_group_width_is_a_single_undo(self) -> None:
        stroke = Stroke([Point(0, 0, 2), Point(50, 0, 2), Point(100, 0, 2)])
        canvas = self._canvas(stroke)
        canvas._selected = [(stroke, 0), (stroke, 2)]
        canvas._hover = (stroke, 0)
        canvas._adjust_point_width(2.0)
        canvas._adjust_point_width(2.0)
        self.assertEqual([point.width for point in stroke.points], [6.0, 2.0, 6.0])
        self.assertEqual(len(canvas._undo_stack), 1)
        canvas.undo()
        self.assertEqual(
            [point.width for point in canvas.doc.layer().strokes[0].points], [2.0, 2.0, 2.0]
        )

    def test_delete_selected_splits_stroke(self) -> None:
        stroke = Stroke([Point(0, 0, 2), Point(10, 0, 2), Point(20, 0, 2), Point(30, 0, 2)])
        canvas = self._canvas(stroke)
        canvas._selected = [(stroke, 1), (stroke, 2)]
        self.assertTrue(canvas.delete_selected())
        parts = canvas.doc.layer().strokes
        self.assertEqual(len(parts), 2)
        self.assertEqual([len(part.points) for part in parts], [1, 1])

    def test_merge_endpoints_closes_stroke(self) -> None:
        stroke = Stroke([Point(0, 0, 2), Point(50, 0, 2, True), Point(50, 50, 2)])
        canvas = self._canvas(stroke)
        canvas._selected = [(stroke, 0), (stroke, 2)]
        self.assertTrue(canvas.merge_selected())
        self.assertTrue(stroke.closed)
        self.assertEqual(len(stroke.points), 3)

    def test_merge_interior_points_uses_centre(self) -> None:
        stroke = Stroke([Point(0, 0, 2), Point(40, 0, 4), Point(60, 0, 4), Point(100, 0, 2)])
        canvas = self._canvas(stroke)
        canvas._selected = [(stroke, 1), (stroke, 2)]
        self.assertTrue(canvas.merge_selected())
        self.assertEqual(len(stroke.points), 3)
        self.assertAlmostEqual(stroke.points[1].x, 50.0)
        self.assertAlmostEqual(stroke.points[1].width, 4.0)

    def test_merge_joins_two_strokes(self) -> None:
        first = Stroke([Point(0, 0, 2), Point(10, 0, 2)])
        second = Stroke([Point(40, 0, 2), Point(50, 0, 2)])
        canvas = self._canvas(first)
        canvas.doc.layer().strokes.append(second)
        canvas._selected = [(first, 1), (second, 0)]
        self.assertTrue(canvas.merge_selected())
        strokes = canvas.doc.layer().strokes
        self.assertEqual(len(strokes), 1)
        self.assertEqual(len(strokes[0].points), 3)
        self.assertAlmostEqual(strokes[0].points[1].x, 25.0)

    def test_close_stroke_makes_it_loop(self) -> None:
        stroke = Stroke([Point(0, 0, 2), Point(50, 0, 2), Point(50, 50, 2)])
        canvas = self._canvas(stroke)
        canvas._hovered_stroke = stroke
        self.assertTrue(canvas.close_selected_strokes())
        self.assertTrue(stroke.closed)


class LayerPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _window(self) -> MainWindow:
        return MainWindow()

    def test_clip_modes_from_the_panel(self) -> None:
        window = self._window()
        document = window.canvas.doc
        window._duplicate_layer()
        document.active_layer = 1
        window._refresh_layers()
        window._clip.setCurrentText("outside")
        self.assertEqual(document.layers[1].clip, "outside")
        window._refresh_layers()
        self.assertEqual(window._clip.currentText(), "outside")
        document.active_layer = 0
        window._refresh_layers()
        self.assertFalse(window._clip.isEnabled())

    def test_duplicate_layer_copies_content_independently(self) -> None:
        window = self._window()
        document = window.canvas.doc
        document.layer().strokes.append(Stroke([Point(1, 1, 2), Point(5, 5, 2)]))
        window._duplicate_layer()
        self.assertEqual(len(document.layers), 2)
        self.assertEqual(document.active_layer, 1)
        self.assertEqual(document.layers[1].name, "Layer 1 copy")
        document.layers[1].strokes[0].points[0].x = 99.0
        self.assertNotEqual(document.layers[0].strokes[0].points[0].x, 99.0)

    def test_rename_layer_from_list(self) -> None:
        window = self._window()
        item = window._layer_list.topLevelItem(0)
        self.assertIsNotNone(item)
        assert item is not None
        item.setText(0, "ink")
        self.assertEqual(window.canvas.doc.layers[0].name, "ink")

    def _select(self, window: MainWindow, layers: list[Layer]) -> None:
        window._layer_list.clearSelection()
        for layer in layers:
            window._layer_items[id(layer)].setSelected(True)

    def test_group_the_whole_selection_from_panel(self) -> None:
        window = self._window()
        document = window.canvas.doc
        document.layers.append(Layer("second"))
        window._refresh_layers()
        self._select(window, document.layers)
        window._group_layer()
        self.assertEqual(len(document.layers), 1)
        group = document.layers[0]
        self.assertTrue(group.is_group)
        self.assertEqual(len(group.children), 2)
        self.assertIs(document.layer(), group)

    def test_ungroup_several_groups_from_panel(self) -> None:
        window = self._window()
        document = window.canvas.doc
        document.layers = [
            document.make_group("g1", [Layer("a")]),
            document.make_group("g2", [Layer("b")]),
        ]
        window._refresh_layers()
        self._select(window, list(document.layers))
        window._ungroup_layer()
        self.assertEqual([layer.name for layer in document.layers], ["a", "b"])

    def _selected_names(self, window: MainWindow) -> list[str]:
        selected = {id(window._item_layer(item)) for item in window._layer_list.selectedItems()}
        return [layer.name for layer, _depth in window.canvas.doc.walk() if id(layer) in selected]

    def test_shift_range_starts_from_the_last_plain_click(self) -> None:
        window = self._window()
        document = window.canvas.doc
        document.layers = [Layer("a"), Layer("b"), Layer("c")]
        window._refresh_layers()
        tree = window._layer_list
        a, b, _c = document.layers
        tree.anchor = b
        tree.select_range(b, a)
        self.assertEqual(self._selected_names(window), ["a", "b"])
        self.assertEqual(document.layer(), a)

    def test_shift_click_extends_from_the_anchor(self) -> None:
        window = self._window()
        document = window.canvas.doc
        document.layers = [Layer("a"), Layer("b"), Layer("c")]
        window.resize(900, 600)
        window.show()
        window._refresh_layers()
        tree = window._layer_list
        items = {layer.name: window._layer_items[id(layer)] for layer in document.layers}

        def click(name: str, modifier: Qt.KeyboardModifier) -> None:
            rect = tree.visualItemRect(items[name])
            QTest.mouseClick(tree.viewport(), Qt.MouseButton.LeftButton, modifier, rect.center())

        click("b", Qt.KeyboardModifier.NoModifier)
        self.assertEqual(self._selected_names(window), ["b"])
        click("c", Qt.KeyboardModifier.ShiftModifier)
        self.assertEqual(self._selected_names(window), ["b", "c"])
        self.assertEqual(document.layer().name, "c")
        click("a", Qt.KeyboardModifier.ShiftModifier)
        self.assertEqual(self._selected_names(window), ["a", "b"])
        self.assertEqual(document.layer().name, "a")

    def test_shift_range_spans_groups_in_tree_order(self) -> None:
        window = self._window()
        document = window.canvas.doc
        inner_a, inner_b = Layer("inner a"), Layer("inner b")
        group = document.make_group("group", [inner_a, inner_b])
        document.layers = [Layer("a"), group, Layer("c")]
        window._refresh_layers()
        tree = window._layer_list
        tree.anchor = group
        tree.select_range(group, inner_b)
        self.assertEqual(self._selected_names(window), ["group", "inner a", "inner b"])
        self.assertEqual(document.layer(), inner_b)

    def test_selection_does_not_rebuild_the_tree(self) -> None:
        window = self._window()
        document = window.canvas.doc
        document.layers = [Layer("a"), Layer("b")]
        window._refresh_layers()
        tree = window._layer_list
        first, second = tree.topLevelItem(0), tree.topLevelItem(1)
        assert first is not None and second is not None
        tree.setCurrentItem(second)
        self.assertIs(tree.topLevelItem(0), first)
        self.assertIs(tree.topLevelItem(1), second)
        self.assertEqual(document.layer().name, "b")

    def test_drop_target_mapping(self) -> None:
        window = self._window()
        document = window.canvas.doc
        document.layers = [Layer("a"), document.make_group("g", [Layer("inner")])]
        window._refresh_layers()
        tree = window._layer_list
        group = document.layers[1]
        group_item = tree.topLevelItem(1)
        assert group_item is not None
        target = tree.drop_target(
            tree.visualItemRect(group_item).center(),
            QAbstractItemView.DropIndicatorPosition.OnItem,
        )
        self.assertEqual(target, (group.children, len(group.children)))

        a_item = tree.topLevelItem(0)
        assert a_item is not None
        point = tree.visualItemRect(a_item).center()
        self.assertEqual(
            tree.drop_target(point, QAbstractItemView.DropIndicatorPosition.AboveItem),
            (document.layers, 0),
        )
        self.assertEqual(
            tree.drop_target(point, QAbstractItemView.DropIndicatorPosition.BelowItem),
            (document.layers, 1),
        )
        self.assertEqual(tree.drop_target(QPoint(-5, -5)), (document.layers, len(document.layers)))

    def test_drop_moves_the_selection_into_a_group(self) -> None:
        window = self._window()
        document = window.canvas.doc
        document.layers = [document.make_group("group", [Layer("inner")]), Layer("outer")]
        window._refresh_layers()
        group, outer = document.layers
        self._select(window, [outer])
        self.assertTrue(window._drop_layers(group.children, 0))
        self.assertEqual([layer.name for layer in group.children], ["outer", "inner"])
        self.assertEqual([layer.name for layer in document.layers], ["group"])

    def test_drop_refuses_a_group_into_itself(self) -> None:
        window = self._window()
        document = window.canvas.doc
        group = document.make_group("group", [Layer("inner")])
        document.layers = [group]
        window._refresh_layers()
        self._select(window, [group])
        self.assertFalse(window._drop_layers(group.children, 0))
        self.assertEqual(len(group.children), 1)

    def test_delete_many_keeps_at_least_one_layer(self) -> None:
        window = self._window()
        document = window.canvas.doc
        document.layers.append(Layer("second"))
        window._refresh_layers()
        self._select(window, list(document.layers))
        window._remove_layer()
        self.assertEqual(len(document.walk()), 2)

    def test_group_and_ungroup_from_panel(self) -> None:
        window = self._window()
        document = window.canvas.doc
        document.layer().strokes.append(Stroke([Point(1, 1, 2), Point(5, 5, 2)]))
        window._group_layer()
        self.assertTrue(document.layers[0].is_group)
        self.assertEqual(document.layers[0].children[0].name, "Layer 1")
        self.assertEqual(document.layer(), document.layers[0])
        self.assertEqual(window._layer_list.topLevelItemCount(), 1)
        group_item = window._layer_list.topLevelItem(0)
        assert group_item is not None
        self.assertEqual(group_item.childCount(), 1)
        window._ungroup_layer()
        self.assertFalse(document.layers[0].is_group)
        self.assertEqual(document.layers[0].name, "Layer 1")
