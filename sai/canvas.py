from __future__ import annotations

import itertools
import math

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QWidget

from . import fill
from .geometry import nearest_curve_point, nearest_point, sample_stroke
from .model import Document, Layer, Point, Stroke
from .render import draw_document, draw_stroke
from .transform import apply_transform, content_bounds, map_point


class Canvas(QWidget):
    documentChanged = Signal()
    statusMessage = Signal(str)
    transformValuesRequested = Signal()

    TOOL_DRAW = "draw"
    TOOL_EDIT = "edit"
    TOOL_ERASE = "erase"
    TOOL_WIDTH = "width"
    TOOL_FILL = "fill"
    TOOL_RECOLOR = "recolor"

    def __init__(self, document: Document | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.doc = document or Document()
        self.tool = self.TOOL_DRAW
        self.brush_width = 6.0
        self.color: tuple[int, int, int] = (20, 20, 20)
        self.alpha = 255
        self.zoom = 1.0
        self.pan = QPointF(40.0, 40.0)
        # View-only rotation in degrees, clockwise on screen. The document keeps
        # its own orientation, so exports are unaffected.
        self.rotation = 0.0

        self._undo_stack: list[Document] = []
        self._redo_stack: list[Document] = []
        self._space_down = False
        self._panning = False
        self._pan_start = QPointF()
        self._pan_origin = QPointF()
        self._active_stroke: Stroke | None = None
        self._drag_point: Point | None = None
        self._cursor: QPointF | None = None
        self._width_last: QPointF | None = None
        self._preview_sharp = False
        self._hover: tuple[Stroke, int] | None = None
        self._hovered_stroke: Stroke | None = None
        self._recolor_dirty = False

        self.transform_mode = False
        self._tr_index = -1
        self._tr_original: Layer | None = None
        self._tr_undo: Document | None = None
        self._tr_bbox = QRectF()
        self._tr_pivot = QPointF()
        self._tr_angle = 0.0
        self._tr_sx = 1.0
        self._tr_sy = 1.0
        self._tr_dx = 0.0
        self._tr_dy = 0.0
        self._tr_action: str | None = None
        self._tr_start = QPointF()
        self._tr_start_params: tuple[float, float, float, float, float] = (0.0, 1.0, 1.0, 0.0, 0.0)
        self._tr_start_angle = 0.0

        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    # --- document state -------------------------------------------------
    def set_document(self, document: Document) -> None:
        if self.transform_mode:
            self._clear_transform()
        self.doc = document
        self.rotation = 0.0
        self._active_stroke = None
        self._hover = None
        self._hovered_stroke = None
        self._undo_stack.clear()
        self._redo_stack.clear()
        self.documentChanged.emit()
        self.update()

    def push_undo(self) -> None:
        self._undo_stack.append(self.doc.copy())
        if len(self._undo_stack) > 100:
            self._undo_stack.pop(0)
        self._redo_stack.clear()

    def undo(self) -> None:
        if not self._undo_stack:
            return
        if self.transform_mode:
            self._clear_transform()
        self._redo_stack.append(self.doc.copy())
        self.doc = self._undo_stack.pop()
        self._active_stroke = None
        self._hover = None
        self._hovered_stroke = None
        self.documentChanged.emit()
        self.update()

    def redo(self) -> None:
        if not self._redo_stack:
            return
        if self.transform_mode:
            self._clear_transform()
        self._undo_stack.append(self.doc.copy())
        self.doc = self._redo_stack.pop()
        self._active_stroke = None
        self._hover = None
        self._hovered_stroke = None
        self.documentChanged.emit()
        self.update()

    # --- helpers --------------------------------------------------------
    def _view_rotation(self) -> tuple[float, float]:
        radians = math.radians(self.rotation)
        return math.cos(radians), math.sin(radians)

    def scene_to_screen(self, scene: QPointF) -> QPointF:
        cosine, sine = self._view_rotation()
        return QPointF(
            self.pan.x() + (scene.x() * cosine - scene.y() * sine) * self.zoom,
            self.pan.y() + (scene.x() * sine + scene.y() * cosine) * self.zoom,
        )

    def to_scene(self, pos: QPointF) -> QPointF:
        cosine, sine = self._view_rotation()
        delta_x = (pos.x() - self.pan.x()) / self.zoom
        delta_y = (pos.y() - self.pan.y()) / self.zoom
        return QPointF(delta_x * cosine + delta_y * sine, -delta_x * sine + delta_y * cosine)

    def set_rotation(self, degrees: float) -> None:
        """Rotate the view around the widget centre, keeping it on screen."""
        center = QPointF(self.width() / 2.0, self.height() / 2.0)
        anchor = self.to_scene(center)
        self.rotation = degrees % 360.0
        cosine, sine = self._view_rotation()
        self.pan = QPointF(
            center.x() - (anchor.x() * cosine - anchor.y() * sine) * self.zoom,
            center.y() - (anchor.x() * sine + anchor.y() * cosine) * self.zoom,
        )
        self.update()

    def rotate_view(self, degrees: float) -> float:
        self.set_rotation(self.rotation + degrees)
        return self.rotation

    def reset_rotation(self) -> None:
        self.set_rotation(0.0)

    def hit_radius(self) -> float:
        return 8.0 / self.zoom

    def active_vector_layer(self) -> Layer | None:
        """The layer the editing tools may touch: active, visible and vector."""
        layer = self.doc.layer()
        if not layer.visible or layer.kind != "vector":
            return None
        return layer

    def clear_hover(self) -> None:
        self._hover = None
        self._hovered_stroke = None
        self.update()

    def finish_stroke(self) -> None:
        if self._active_stroke is not None:
            if not self._active_stroke.points:
                layer = self.doc.layer()
                layer.strokes = [
                    stroke for stroke in layer.strokes if stroke is not self._active_stroke
                ]
            self._active_stroke = None
            self.documentChanged.emit()
            self.update()

    # --- painting -------------------------------------------------------
    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(self.rect(), QColor(70, 70, 74))

        painter.save()
        painter.translate(self.pan)
        painter.rotate(self.rotation)
        painter.scale(self.zoom, self.zoom)

        page = QRectF(0, 0, self.doc.width, self.doc.height)
        painter.fillRect(page, QColor(255, 255, 255))
        preview = self._preview_stroke()
        draw_document(
            painter,
            self.doc,
            background=None,
            skip_stroke=self._active_stroke if preview is not None else None,
        )
        self._draw_overlay(painter)

        painter.restore()
        painter.end()

    def _preview_stroke(self) -> Stroke | None:
        """The active stroke plus a point under the cursor, for live preview."""
        stroke = self._active_stroke
        if stroke is None or not stroke.points or self._cursor is None:
            return None
        points = [point.copy() for point in stroke.points]
        points.append(
            Point(
                self._cursor.x(),
                self._cursor.y(),
                self.brush_width,
                not self._preview_sharp,
            )
        )
        return Stroke(points, stroke.color, stroke.alpha)

    def _draw_overlay(self, painter: QPainter) -> None:
        if self.transform_mode:
            self._draw_transform_overlay(painter)
            return
        if self._active_stroke is not None:
            preview = self._preview_stroke()
            draw_stroke(painter, preview if preview is not None else self._active_stroke)

        if self.tool in (self.TOOL_EDIT, self.TOOL_WIDTH, self.TOOL_ERASE):
            layer = self.active_vector_layer()
            if layer is not None:
                self._draw_stroke_hover(painter)
                self._draw_point_handles(painter, layer)
        elif self.tool == self.TOOL_RECOLOR:
            self._draw_recolor_overlay(painter)

    def _draw_point_handles(self, painter: QPainter, layer: Layer) -> None:
        radius = 4.0 / self.zoom
        outline = QPen(QColor(0, 110, 220), 1.0 / self.zoom)
        for stroke in layer.strokes:
            for index, point in enumerate(stroke.points):
                is_hovered = (
                    self._hover is not None and self._hover[0] is stroke and self._hover[1] == index
                )
                pen = QPen(QColor(0, 170, 255), 2.5 / self.zoom) if is_hovered else outline
                painter.setPen(pen)
                painter.setBrush(QColor(30, 30, 30) if point.smooth else QColor(255, 200, 60))
                if point.smooth:
                    painter.drawEllipse(QPointF(point.x, point.y), radius, radius)
                else:
                    painter.drawRect(
                        QRectF(point.x - radius, point.y - radius, radius * 2, radius * 2)
                    )
                painter.setBrush(Qt.BrushStyle.NoBrush)

    def _draw_stroke_hover(self, painter: QPainter) -> None:
        stroke = self._hovered_stroke
        if stroke is None:
            return
        self._draw_stroke_preview(painter, stroke, QColor(0, 170, 255, 120), 4.0 / self.zoom)

    def _draw_stroke_preview(
        self, painter: QPainter, stroke: Stroke, color: QColor, margin: float
    ) -> None:
        samples = sample_stroke(stroke)
        if not samples:
            return
        if len(samples) == 1:
            x, y, width = samples[0]
            radius = max(width, 0.5) / 2 + margin / 2
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            painter.drawEllipse(QPointF(x, y), radius, radius)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            return
        for (x1, y1, w1), (x2, y2, w2) in itertools.pairwise(samples):
            width = (w1 + w2) / 2 + margin
            if width <= 0.02:
                continue
            pen = QPen(
                color,
                width,
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
                Qt.PenJoinStyle.RoundJoin,
            )
            painter.setPen(pen)
            painter.drawLine(QPointF(x1, y1), QPointF(x2, y2))

    def _draw_recolor_overlay(self, painter: QPainter) -> None:
        stroke = self._hovered_stroke
        if stroke is not None:
            self._draw_stroke_hover(painter)
            preview = QColor(self.color[0], self.color[1], self.color[2], self.alpha)
            self._draw_stroke_preview(painter, stroke, preview, 0.0)
        layer = self.active_vector_layer()
        if layer is not None:
            self._draw_point_handles(painter, layer)

    # --- transform mode -------------------------------------------------
    def begin_transform(self) -> bool:
        if self.transform_mode:
            self.commit_transform()
        self.finish_stroke()
        layer = self.doc.layer()
        bounds = content_bounds(layer)
        if bounds is None:
            self.statusMessage.emit("nothing to transform")
            return False

        self.transform_mode = True
        self._tr_index = self.doc.active_layer
        self._tr_original = layer.copy()
        self._tr_undo = self.doc.copy()
        self._tr_bbox = bounds
        self._tr_pivot = bounds.center()
        self._tr_angle = 0.0
        self._tr_sx = 1.0
        self._tr_sy = 1.0
        self._tr_dx = 0.0
        self._tr_dy = 0.0
        self._tr_action = None
        self._hover = None
        self.update()
        self.statusMessage.emit(
            "transform: edge scales, inside moves, outside rotates. "
            "Shift snaps, T sets exact values, Enter apply, Esc cancel"
        )
        return True

    def commit_transform(self) -> None:
        if not self.transform_mode:
            return
        changed = (
            abs(self._tr_angle) > 1e-6
            or abs(self._tr_sx - 1.0) > 1e-6
            or abs(self._tr_sy - 1.0) > 1e-6
            or abs(self._tr_dx) > 1e-6
            or abs(self._tr_dy) > 1e-6
        )
        if changed and self._tr_undo is not None:
            self._undo_stack.append(self._tr_undo)
            if len(self._undo_stack) > 100:
                self._undo_stack.pop(0)
            self._redo_stack.clear()
        self._clear_transform()
        self.documentChanged.emit()
        self.statusMessage.emit("transform applied" if changed else "transform closed")

    def cancel_transform(self) -> None:
        if not self.transform_mode:
            return
        if self._tr_original is not None and 0 <= self._tr_index < len(self.doc.layers):
            self.doc.layers[self._tr_index] = self._tr_original.copy()
        self._clear_transform()
        self.documentChanged.emit()
        self.statusMessage.emit("transform cancelled")

    def _clear_transform(self) -> None:
        self.transform_mode = False
        self._tr_index = -1
        self._tr_original = None
        self._tr_undo = None
        self._tr_action = None
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.update()

    def _transformed_corners(self) -> list[QPointF]:
        bounds = self._tr_bbox
        corners = [
            QPointF(bounds.left(), bounds.top()),
            QPointF(bounds.right(), bounds.top()),
            QPointF(bounds.right(), bounds.bottom()),
            QPointF(bounds.left(), bounds.bottom()),
        ]
        pivot = self._tr_pivot
        return [
            map_point(
                corner.x(),
                corner.y(),
                self._tr_angle,
                self._tr_sx,
                self._tr_sy,
                self._tr_dx,
                self._tr_dy,
                pivot.x(),
                pivot.y(),
            )
            for corner in corners
        ]

    def _apply_live_transform(self) -> None:
        if self._tr_original is None or not 0 <= self._tr_index < len(self.doc.layers):
            return
        layer = self.doc.layers[self._tr_index]
        original = self._tr_original
        if layer.kind == "raster":
            layer.image_base64 = original.image_base64
            layer._cached_image = None
        else:
            layer.strokes = [stroke.copy() for stroke in original.strokes]
        apply_transform(
            layer,
            self._tr_angle,
            self._tr_sx,
            self._tr_sy,
            self._tr_dx,
            self._tr_dy,
            self._tr_pivot.x(),
            self._tr_pivot.y(),
            self.doc.width,
            self.doc.height,
        )
        self.update()

    def _transform_hit(self, scene: QPointF) -> str:
        corners = self._transformed_corners()
        threshold = 8.0 / self.zoom
        edges = (
            (0, 1, "scale-y"),
            (1, 2, "scale-x"),
            (2, 3, "scale-y"),
            (3, 0, "scale-x"),
        )
        for first, second, action in edges:
            if _distance_to_segment(scene, corners[first], corners[second]) <= threshold:
                return action

        polygon = QPolygonF(corners)
        if polygon.containsPoint(scene, Qt.FillRule.OddEvenFill):
            return "move"
        return "rotate"

    def _set_transform_cursor(self, action: str) -> None:
        cursors = {
            "scale-x": Qt.CursorShape.SizeHorCursor,
            "scale-y": Qt.CursorShape.SizeVerCursor,
            "move": Qt.CursorShape.SizeAllCursor,
            "rotate": Qt.CursorShape.CrossCursor,
        }
        self.setCursor(cursors.get(action, Qt.CursorShape.ArrowCursor))

    def _update_transform_drag(self, scene: QPointF, modifiers: Qt.KeyboardModifier) -> None:
        start_angle, start_sx, start_sy, start_dx, start_dy = self._tr_start_params
        pivot = self._tr_pivot
        action = self._tr_action
        snap = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
        if action == "move":
            self._tr_dx = start_dx + (scene.x() - self._tr_start.x())
            self._tr_dy = start_dy + (scene.y() - self._tr_start.y())
        elif action == "rotate":
            current = math.atan2(scene.y() - pivot.y(), scene.x() - pivot.x())
            angle = start_angle + math.degrees(current - self._tr_start_angle)
            if snap:
                angle = round(angle / 15.0) * 15.0
            self._tr_angle = angle
        elif action in ("scale-x", "scale-y"):
            radians = math.radians(start_angle)
            cosine = math.cos(radians)
            sine = math.sin(radians)

            def local(point: QPointF) -> tuple[float, float]:
                vx = point.x() - pivot.x() - start_dx
                vy = point.y() - pivot.y() - start_dy
                return vx * cosine + vy * sine, -vx * sine + vy * cosine

            mouse_local = local(scene)
            start_local = local(self._tr_start)
            scale_x = start_sx
            scale_y = start_sy
            if action == "scale-x" and abs(start_local[0]) > 1e-6:
                scale_x = mouse_local[0] / start_local[0]
            if action == "scale-y" and abs(start_local[1]) > 1e-6:
                scale_y = mouse_local[1] / start_local[1]
            if snap:
                scale_x = round(scale_x * 10.0) / 10.0
                scale_y = round(scale_y * 10.0) / 10.0
            self._tr_sx = min(20.0, max(0.02, scale_x))
            self._tr_sy = min(20.0, max(0.02, scale_y))
        self._apply_live_transform()

    def set_transform_values(
        self,
        angle: float,
        scale_x: float,
        scale_y: float,
        offset_x: float,
        offset_y: float,
    ) -> None:
        if not self.transform_mode:
            return
        self._tr_angle = angle
        self._tr_sx = min(20.0, max(0.02, scale_x))
        self._tr_sy = min(20.0, max(0.02, scale_y))
        self._tr_dx = offset_x
        self._tr_dy = offset_y
        self._apply_live_transform()

    def transform_parameters(self) -> tuple[float, float, float, float, float]:
        return (self._tr_angle, self._tr_sx, self._tr_sy, self._tr_dx, self._tr_dy)

    def _draw_transform_overlay(self, painter: QPainter) -> None:
        corners = self._transformed_corners()
        polygon = QPolygonF(corners)
        pen = QPen(QColor(0, 150, 255), 1.2 / self.zoom, Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPolygon(polygon)

        radius = 4.0 / self.zoom
        solid = QPen(QColor(0, 150, 255), 1.2 / self.zoom)
        painter.setPen(solid)
        for corner in corners:
            painter.drawRect(
                QRectF(corner.x() - radius, corner.y() - radius, radius * 2, radius * 2)
            )
        painter.drawEllipse(self._tr_pivot, radius * 0.7, radius * 0.7)

        painter.save()
        font = painter.font()
        font.setPixelSize(max(1, int(12 / self.zoom)))
        painter.setFont(font)
        painter.setPen(QPen(QColor(0, 120, 220)))
        text = (
            f"{self._tr_angle:.1f}°  "
            f"x{self._tr_sx:.2f} y{self._tr_sy:.2f}  "
            f"move {self._tr_dx:.0f},{self._tr_dy:.0f}"
        )
        painter.drawText(corners[0] + QPointF(0, -8 / self.zoom), text)
        painter.restore()

    # --- mouse ----------------------------------------------------------
    def leaveEvent(self, event) -> None:
        self._hover = None
        self._hovered_stroke = None
        self._cursor = None
        self.update()

    def mousePressEvent(self, event) -> None:
        pos = event.position()
        if event.button() == Qt.MouseButton.MiddleButton or (
            event.button() == Qt.MouseButton.LeftButton and self._space_down
        ):
            self._panning = True
            self._pan_start = pos
            self._pan_origin = QPointF(self.pan)
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            return

        if event.button() == Qt.MouseButton.RightButton:
            if self.transform_mode:
                self.commit_transform()
                return
            if self.tool == self.TOOL_EDIT and self._delete_point_at(self.to_scene(pos)):
                return
            self.finish_stroke()
            return

        if event.button() != Qt.MouseButton.LeftButton:
            return

        if self.transform_mode:
            scene = self.to_scene(pos)
            self._tr_action = self._transform_hit(scene)
            self._tr_start = QPointF(scene)
            self._tr_start_params = (
                self._tr_angle,
                self._tr_sx,
                self._tr_sy,
                self._tr_dx,
                self._tr_dy,
            )
            self._tr_start_angle = math.atan2(
                scene.y() - self._tr_pivot.y(), scene.x() - self._tr_pivot.x()
            )
            self._set_transform_cursor(self._tr_action)
            return

        scene = self.to_scene(pos)
        canvas_event = event.modifiers()

        if self.tool == self.TOOL_DRAW:
            smooth = not bool(canvas_event & Qt.KeyboardModifier.ControlModifier)
            self._draw_click(scene, smooth)
        elif self.tool == self.TOOL_EDIT:
            self._edit_click(scene)
        elif self.tool == self.TOOL_ERASE:
            self.push_undo()
            self._erase_at(scene)
        elif self.tool == self.TOOL_WIDTH:
            self.push_undo()
            self._width_last = QPointF(scene)
        elif self.tool == self.TOOL_FILL:
            self._fill_click(scene)
        elif self.tool == self.TOOL_RECOLOR:
            self._recolor_dirty = False
            self._recolor_gesture(scene)

    def _fill_click(self, scene: QPointF) -> None:
        document = self.doc
        self.push_undo()
        layer = document.layer()
        if layer.kind != "raster":
            index = document.active_layer
            document.layers.insert(index, Layer("Fill", kind="raster"))
            document.active_layer = index
        target_index = document.active_layer
        if not fill.bucket_fill(
            document, target_index, scene.x(), scene.y(), self.color, self.alpha
        ):
            self.undo()
            self.statusMessage.emit("fill: no closed region here")
            return
        self.documentChanged.emit()
        self.update()
        self.statusMessage.emit("filled")

    def _stroke_at(self, scene: QPointF) -> Stroke | None:
        """Closest stroke of the active layer, thickness taken into account."""
        layer = self.active_vector_layer()
        if layer is None:
            return None
        best: tuple[Stroke | None, float] = (None, float("inf"))
        for stroke in layer.strokes:
            if not stroke.points:
                continue
            if len(stroke.points) == 1:
                # A single-point stroke is drawn as a dot, so it has no curve to
                # measure against. Hit-test the point itself.
                point = stroke.points[0]
                distance = math.hypot(point.x - scene.x(), point.y - scene.y())
                limit = max(self.hit_radius(), point.width / 2.0)
            else:
                index, t, distance, _x, _y = nearest_curve_point(stroke, scene.x(), scene.y())
                if index < 0:
                    continue
                start = stroke.points[index]
                end = stroke.points[index + 1]
                width = start.width + (end.width - start.width) * t
                limit = max(self.hit_radius(), width / 2.0)
            if distance <= limit and distance < best[1]:
                best = (stroke, distance)
        return best[0]

    def _recolor_gesture(self, scene: QPointF) -> None:
        stroke = self._stroke_at(scene)
        if stroke is None:
            return
        color = self.color
        alpha = self.alpha
        if stroke.color == color and stroke.alpha == alpha:
            return
        if not self._recolor_dirty:
            self.push_undo()
            self._recolor_dirty = True
        stroke.color = color
        stroke.alpha = alpha
        self.statusMessage.emit(f"recolored stroke: rgb{color} alpha {alpha}")
        self.documentChanged.emit()
        self.update()

    def mouseMoveEvent(self, event) -> None:
        pos = event.position()
        scene = self.to_scene(pos)
        self._cursor = scene
        self._preview_sharp = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)

        if self._panning:
            self.pan = self._pan_origin + (pos - self._pan_start)
            self.update()
            return

        if self.transform_mode:
            scene = self.to_scene(pos)
            self._cursor = scene
            if self._tr_action is not None and event.buttons() & Qt.MouseButton.LeftButton:
                self._update_transform_drag(scene, event.modifiers())
            else:
                self._set_transform_cursor(self._transform_hit(scene))
            return

        if self._drag_point is not None:
            self._drag_point.x = scene.x()
            self._drag_point.y = scene.y()
            self.statusMessage.emit(f"point {scene.x():.0f}, {scene.y():.0f}")
            self.update()
            return

        if self._width_last is not None:
            self._adjust_width(scene)
            self.update()
            return

        if self.tool == self.TOOL_ERASE and event.buttons() & Qt.MouseButton.LeftButton:
            self._erase_at(scene)
            return

        if self.tool == self.TOOL_RECOLOR and event.buttons() & Qt.MouseButton.LeftButton:
            self._recolor_gesture(scene)
            return

        if self.tool == self.TOOL_RECOLOR:
            hovered = self._stroke_at(scene)
            if hovered is not self._hovered_stroke:
                self._hovered_stroke = hovered
                if hovered is not None:
                    self.statusMessage.emit(f"recolor: {self.doc.layer().name}")
                self.update()
            return

        if self.tool in (self.TOOL_EDIT, self.TOOL_WIDTH, self.TOOL_ERASE):
            stroke, index = self._find_point(scene)
            self._hover = (stroke, index) if stroke is not None else None
            self._hovered_stroke = self._stroke_at(scene)
            self.update()
        elif self._active_stroke is not None:
            self.update()

    def mouseReleaseEvent(self, event) -> None:
        if self._panning:
            self._panning = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
            if self.transform_mode:
                self._set_transform_cursor("rotate")
            return
        if self.transform_mode:
            self._tr_action = None
            self._set_transform_cursor("rotate")
            return
        if self._drag_point is not None:
            self._drag_point = None
            self.documentChanged.emit()
        if self._width_last is not None:
            self._width_last = None
            self.documentChanged.emit()
        self._recolor_dirty = False

    def mouseDoubleClickEvent(self, event) -> None:
        if self.tool != self.TOOL_EDIT or event.button() != Qt.MouseButton.LeftButton:
            return
        scene = self.to_scene(event.position())
        stroke, index = self._find_point(scene)
        if stroke is not None:
            self.push_undo()
            stroke.points[index].smooth = not stroke.points[index].smooth
            self._hover = (stroke, index)
            self.documentChanged.emit()
            self.update()

    def wheelEvent(self, event) -> None:
        pos = event.position()
        delta = event.angleDelta().y()
        factor = 1.15 if delta > 0 else 1 / 1.15
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self._zoom_at(pos, factor)
            return

        step = 1.0 if delta > 0 else -1.0
        if self.tool == self.TOOL_EDIT:
            if self._adjust_point_width(step):
                return
            self._zoom_at(pos, factor)
            return

        if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            self.brush_width = min(200.0, max(0.0, self.brush_width + step))
            self.statusMessage.emit(f"brush {self.brush_width:.1f}")
            self.update()
            return

        self._zoom_at(pos, factor)

    def _zoom_at(self, pos: QPointF, factor: float) -> None:
        scene = self.to_scene(pos)
        self.zoom = min(20.0, max(0.05, self.zoom * factor))
        cosine, sine = self._view_rotation()
        self.pan = QPointF(
            pos.x() - (scene.x() * cosine - scene.y() * sine) * self.zoom,
            pos.y() - (scene.x() * sine + scene.y() * cosine) * self.zoom,
        )
        self.update()

    def _adjust_point_width(self, step: float) -> bool:
        target = self._hover
        if target is None:
            return False
        stroke, index = target
        layer = self.active_vector_layer()
        if layer is None or stroke not in layer.strokes:
            self._hover = None
            return False
        if not 0 <= index < len(stroke.points):
            return False
        point = stroke.points[index]
        point.width = min(200.0, max(0.0, point.width + step))
        self.statusMessage.emit(f"width {point.width:.1f}")
        self.documentChanged.emit()
        self.update()
        return True

    def keyPressEvent(self, event) -> None:
        if self.transform_mode:
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self.commit_transform()
            elif event.key() == Qt.Key.Key_Escape:
                self.cancel_transform()
            elif event.key() == Qt.Key.Key_T:
                self.transformValuesRequested.emit()
            return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Escape):
            self.finish_stroke()
            return
        if event.key() == Qt.Key.Key_Space:
            self._space_down = True
            return
        if event.key() == Qt.Key.Key_Control:
            self._preview_sharp = True
            self.update()
            return
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            if self._cursor is not None:
                self._delete_point_at(self._cursor)
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Space:
            self._space_down = False
            return
        if event.key() == Qt.Key.Key_Control:
            self._preview_sharp = False
            self.update()
            return
        super().keyReleaseEvent(event)

    # --- tools ----------------------------------------------------------
    def _draw_click(self, scene: QPointF, smooth: bool = True) -> None:
        if self._active_stroke is None:
            self.push_undo()
            self._active_stroke = Stroke(color=self.color, alpha=self.alpha)
            self.doc.layer().strokes.append(self._active_stroke)
        self._active_stroke.points.append(Point(scene.x(), scene.y(), self.brush_width, smooth))
        self._cursor = scene
        self.update()

    def _edit_click(self, scene: QPointF) -> None:
        stroke, index = self._find_point(scene)
        if stroke is not None:
            self.push_undo()
            self._hover = (stroke, index)
            self._drag_point = stroke.points[index]
            self.update()
            return
        if not self._insert_point(scene):
            self.update()

    def _insert_point(self, scene: QPointF) -> bool:
        radius = self.hit_radius()
        layer = self.active_vector_layer()
        if layer is None:
            return False
        best: tuple[Stroke, int, float, float, float, float] | None = None
        for stroke in layer.strokes:
            index, t, distance, x, y = nearest_curve_point(stroke, scene.x(), scene.y())
            if index >= 0 and (best is None or distance < best[2]):
                best = (stroke, index, distance, t, x, y)
        if best is None or best[2] > radius:
            return False

        stroke, index, _, t, _x, _y = best
        start = stroke.points[index]
        end = stroke.points[index + 1]
        point = Point(
            scene.x(),
            scene.y(),
            start.width + (end.width - start.width) * t,
            start.smooth and end.smooth,
        )
        self.push_undo()
        stroke.points.insert(index + 1, point)
        self._hover = (stroke, index + 1)
        self._drag_point = point
        self.statusMessage.emit("point inserted")
        self.documentChanged.emit()
        self.update()
        return True

    def _find_point(self, scene: QPointF) -> tuple[Stroke | None, int]:
        radius = self.hit_radius()
        best: tuple[Stroke | None, int, float] = (None, -1, radius)
        layer = self.active_vector_layer()
        if layer is None:
            return best[0], best[1]
        for stroke in layer.strokes:
            index, distance = nearest_point(stroke, scene.x(), scene.y())
            if index >= 0 and distance < best[2]:
                best = (stroke, index, distance)
        return best[0], best[1]

    def _erase_at(self, scene: QPointF) -> None:
        radius = self.hit_radius()
        layer = self.doc.layer()
        changed = False
        new_strokes: list[Stroke] = []
        for stroke in layer.strokes:
            groups: list[list[Point]] = []
            current: list[Point] = []
            for point in stroke.points:
                if math.hypot(point.x - scene.x(), point.y - scene.y()) <= radius:
                    changed = True
                    if current:
                        groups.append(current)
                        current = []
                else:
                    current.append(point)
            if current:
                groups.append(current)
            for group in groups:
                new_strokes.append(Stroke(group, stroke.color, stroke.alpha))
        if changed:
            layer.strokes = new_strokes
            self._hover = None
            self.documentChanged.emit()
            self.update()

    def _adjust_width(self, scene: QPointF) -> None:
        if self._width_last is None:
            return
        delta = (scene.x() - self._width_last.x() + scene.y() - self._width_last.y()) * 0.5
        if abs(delta) < 1e-6:
            return
        radius = max(self.hit_radius(), self.brush_width)
        layer = self.doc.layer()
        for stroke in layer.strokes:
            for point in stroke.points:
                if math.hypot(point.x - scene.x(), point.y - scene.y()) <= radius:
                    point.width = min(200.0, max(0.0, point.width + delta))
        self._width_last = QPointF(scene)
        self.documentChanged.emit()

    def _delete_point_at(self, scene: QPointF) -> bool:
        stroke, index = self._find_point(scene)
        if stroke is None:
            return False
        self.push_undo()
        self._remove_point(stroke, index)
        return True

    def _remove_point(self, stroke: Stroke, index: int) -> None:
        if index < 0 or index >= len(stroke.points):
            self._hover = None
            return
        del stroke.points[index]
        if not stroke.points:
            for layer in self.doc.layers:
                layer.strokes = [item for item in layer.strokes if item is not stroke]
        self._hover = None
        self.documentChanged.emit()
        self.update()


def _distance_to_segment(point: QPointF, start: QPointF, end: QPointF) -> float:
    dx = end.x() - start.x()
    dy = end.y() - start.y()
    length_squared = dx * dx + dy * dy
    if length_squared < 1e-9:
        return math.hypot(point.x() - start.x(), point.y() - start.y())
    t = max(
        0.0,
        min(
            1.0,
            ((point.x() - start.x()) * dx + (point.y() - start.y()) * dy) / length_squared,
        ),
    )
    return math.hypot(
        point.x() - (start.x() + t * dx),
        point.y() - (start.y() + t * dy),
    )
