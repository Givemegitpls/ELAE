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
from PySide6.QtWidgets import QApplication

from sai import fill, io, transform
from sai.app import MainWindow
from sai.canvas import Canvas
from sai.geometry import nearest_point, nearest_segment, sample_stroke
from sai.model import Document, Layer, Point, Stroke
from sai.render import draw_document, layer_image


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
        stroke = Stroke([Point(0, 0, 3), Point(50, 0, 3, True), Point(50, 50, 3)])
        center = (25.0, 25.0)
        radius = math.hypot(25.0, 25.0)
        for x, y, _ in sample_stroke(stroke):
            self.assertAlmostEqual(math.hypot(x - center[0], y - center[1]), radius, delta=0.05)
        self.assertAlmostEqual(_bow_depth(stroke), radius * (1 - math.cos(math.pi / 4)), delta=0.2)

    def test_junction_between_segments_is_smooth(self) -> None:
        stroke = Stroke(
            [
                Point(0, 0, 3),
                Point(50, 0, 3, True),
                Point(50, 50, 3, True),
                Point(50, 100, 3),
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


class RenderMaskTests(unittest.TestCase):
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

    def test_clip_to_layer_below(self) -> None:
        document = Document(100, 100)
        document.layers = [
            Layer("base", [Stroke([Point(20, 20, 20)])]),
            Layer(
                "clip",
                [Stroke([Point(80, 20, 20)]), Stroke([Point(21, 20, 20)])],
                clip_to_below=True,
            ),
        ]
        image = self._render(document)
        self.assertGreater(image.pixelColor(21, 20).alpha(), 0)
        self.assertEqual(image.pixelColor(80, 20).alpha(), 0)

    def test_layer_mask_uses_alpha(self) -> None:
        document = Document(100, 100)
        layer = Layer("line", [Stroke([Point(20, 20, 20)]), Stroke([Point(80, 20, 20)])])
        document.layers = [layer]

        mask = QImage(100, 100, QImage.Format.Format_ARGB32_Premultiplied)
        mask.fill(Qt.GlobalColor.transparent)
        mask_painter = QPainter(mask)
        mask_painter.fillRect(QRectF(0, 0, 50, 100), QColor("white"))
        mask_painter.end()

        layer.mask_id = document.add_mask("half", io.encode_image(mask))

        image = self._render(document)
        self.assertGreater(image.pixelColor(20, 20).alpha(), 0)
        self.assertEqual(image.pixelColor(80, 20).alpha(), 0)

    def test_one_mask_serves_many_layers(self) -> None:
        document = Document(100, 100)
        left = Layer("left", [Stroke([Point(20, 20, 20)])])
        right = Layer("right", [Stroke([Point(80, 20, 20)])])
        document.layers = [left, right]

        mask = QImage(100, 100, QImage.Format.Format_ARGB32_Premultiplied)
        mask.fill(Qt.GlobalColor.transparent)
        mask_painter = QPainter(mask)
        mask_painter.fillRect(QRectF(0, 0, 50, 100), QColor("white"))
        mask_painter.end()

        mask_id = document.add_mask("left half", io.encode_image(mask))
        left.mask_id = mask_id
        right.mask_id = mask_id

        image = self._render(document)
        self.assertGreater(image.pixelColor(20, 20).alpha(), 0)
        self.assertEqual(image.pixelColor(80, 20).alpha(), 0)

    def test_clip_and_mask_roundtrip(self) -> None:
        document = Document(64, 64)
        document.layers[0].clip_to_below = True
        mask_id = document.add_mask("shared", "ZmFrZQ==")
        document.layers[0].mask_id = mask_id
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "doc.json"
            io.save_document(document, path)
            loaded = io.load_document(path)
        self.assertTrue(loaded.layers[0].clip_to_below)
        self.assertEqual(loaded.layers[0].mask_id, mask_id)
        self.assertEqual(loaded.masks[mask_id].image_base64, "ZmFrZQ==")

    def test_legacy_layer_mask_becomes_shared(self) -> None:
        document = Document.from_dict(
            {
                "width": 32,
                "height": 32,
                "layers": [{"name": "ink", "mask": "ZmFrZQ=="}],
            }
        )
        layer = document.layers[0]
        self.assertIsNotNone(layer.mask_id)
        assert layer.mask_id is not None
        self.assertEqual(document.masks[layer.mask_id].image_base64, "ZmFrZQ==")
        self.assertEqual(document.masks[layer.mask_id].name, "ink mask")

    def test_remove_unused_masks(self) -> None:
        document = Document(16, 16)
        used = document.add_mask("used", "ZmFrZQ==")
        document.add_mask("stale", "ZmFrZQ==")
        document.layers[0].mask_id = used
        self.assertEqual(document.remove_unused_masks(), 1)
        self.assertEqual(list(document.masks), [used])


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
        self.assertTrue(fill.bucket_fill(document, 0, 20, 20, (255, 0, 0), 255))
        image = layer_image(document.layers[0])
        self.assertIsNotNone(image)
        assert image is not None
        inside = image.pixelColor(20, 20)
        self.assertGreater(inside.alpha(), 0)
        self.assertEqual(inside.name(), "#ff0000")
        self.assertEqual(image.pixelColor(3, 3).alpha(), 0)

    def test_bucket_fill_on_line_does_nothing(self) -> None:
        document = self._document()
        self.assertFalse(fill.bucket_fill(document, 0, 10, 10, (255, 0, 0), 255))

    def test_open_region_uses_canvas_as_boundary(self) -> None:
        document = Document(60, 40)
        underlay_image = QImage(60, 40, QImage.Format.Format_ARGB32_Premultiplied)
        underlay_image.fill(QColor("white"))
        underlay = Layer("underlay", kind="raster", image_base64=io.encode_image(underlay_image))
        line = Stroke([Point(-5, 20, 4), Point(65, 20, 4)])
        document.layers = [underlay, Layer("fill", kind="raster"), Layer("line", [line])]
        document.active_layer = 1

        self.assertTrue(fill.bucket_fill(document, 1, 30, 8, (0, 128, 255), 255))
        image = layer_image(document.layers[1])
        self.assertIsNotNone(image)
        assert image is not None
        self.assertGreater(image.pixelColor(30, 8).alpha(), 0)
        self.assertEqual(image.pixelColor(30, 32).alpha(), 0)

    def test_fill_tool_creates_raster_layer(self) -> None:
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
        canvas.doc.layer().strokes.append(stroke)
        canvas.tool = Canvas.TOOL_FILL
        canvas.color = (0, 0, 255)
        canvas._fill_click(QPointF(20, 20))
        self.assertEqual(canvas.doc.layers[0].kind, "raster")
        image = layer_image(canvas.doc.layers[0])
        self.assertIsNotNone(image)
        assert image is not None
        self.assertGreater(image.pixelColor(20, 20).alpha(), 0)


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


class LayerPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _window(self) -> MainWindow:
        return MainWindow()

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

    def test_duplicate_keeps_shared_mask(self) -> None:
        window = self._window()
        document = window.canvas.doc
        mask_id = document.add_mask("shared", "ZmFrZQ==")
        document.layer().mask_id = mask_id
        window._duplicate_layer()
        self.assertEqual(document.layers[1].mask_id, mask_id)

    def test_rename_layer_from_list(self) -> None:
        window = self._window()
        item = window._layer_list.item(0)
        self.assertIsNotNone(item)
        assert item is not None
        item.setText("ink")
        self.assertEqual(window.canvas.doc.layers[0].name, "ink")

    def test_mask_combo_lists_shared_masks(self) -> None:
        window = self._window()
        document = window.canvas.doc
        mask_id = document.add_mask("shared", "ZmFrZQ==")
        window._refresh_layers()
        self.assertGreater(window._mask_combo.findData(mask_id), 0)
        window._mask_combo.setCurrentIndex(window._mask_combo.findData(mask_id))
        self.assertEqual(document.layer().mask_id, mask_id)


if __name__ == "__main__":
    unittest.main()
