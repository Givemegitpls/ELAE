from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QImage, QPainter, QTransform

from . import io
from .model import Layer
from .render import layer_image


def content_bounds(layer: Layer) -> QRectF | None:
    """Bounding box of the layer content in canvas coordinates."""
    if layer.kind == "raster":
        image = layer_image(layer)
        if image is None:
            return None
        return QRectF(0, 0, image.width(), image.height())

    points = [point for stroke in layer.strokes for point in stroke.points]
    if not points:
        return None
    minimum_x = min(point.x for point in points)
    maximum_x = max(point.x for point in points)
    minimum_y = min(point.y for point in points)
    maximum_y = max(point.y for point in points)
    return QRectF(minimum_x, minimum_y, maximum_x - minimum_x, maximum_y - minimum_y)


def map_point(
    x: float,
    y: float,
    angle: float,
    scale_x: float,
    scale_y: float,
    offset_x: float,
    offset_y: float,
    pivot_x: float,
    pivot_y: float,
) -> QPointF:
    radians = math.radians(angle)
    cosine = math.cos(radians)
    sine = math.sin(radians)
    local_x = (x - pivot_x) * scale_x
    local_y = (y - pivot_y) * scale_y
    return QPointF(
        pivot_x + local_x * cosine - local_y * sine + offset_x,
        pivot_y + local_x * sine + local_y * cosine + offset_y,
    )


def apply_transform(
    layer: Layer,
    angle: float,
    scale_x: float,
    scale_y: float,
    offset_x: float,
    offset_y: float,
    pivot_x: float,
    pivot_y: float,
    canvas_width: int,
    canvas_height: int,
) -> bool:
    """Apply a rotate/scale/translate around ``pivot`` to the layer content."""
    if layer.kind == "raster":
        image = layer_image(layer)
        if image is None:
            return False
        target = QImage(
            max(1, canvas_width),
            max(1, canvas_height),
            QImage.Format.Format_ARGB32_Premultiplied,
        )
        target.fill(Qt.GlobalColor.transparent)

        transform = QTransform()
        transform.translate(pivot_x + offset_x, pivot_y + offset_y)
        transform.rotate(angle)
        transform.scale(scale_x, scale_y)
        transform.translate(-pivot_x, -pivot_y)

        painter = QPainter(target)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.setTransform(transform)
        painter.drawImage(0, 0, image)
        painter.end()

        layer.image_base64 = io.encode_image(target)
        layer._cached_image = None
        return True

    points = [point for stroke in layer.strokes for point in stroke.points]
    if not points:
        return False

    width_scale = math.sqrt(abs(scale_x * scale_y))
    for point in points:
        moved = map_point(
            point.x,
            point.y,
            angle,
            scale_x,
            scale_y,
            offset_x,
            offset_y,
            pivot_x,
            pivot_y,
        )
        point.x = moved.x()
        point.y = moved.y()
        point.width *= width_scale
    return True


def rotate_layer(layer: Layer, angle_degrees: float, canvas_width: int, canvas_height: int) -> bool:
    bounds = content_bounds(layer)
    if bounds is None:
        return False
    center = bounds.center()
    return apply_transform(
        layer,
        angle_degrees,
        1.0,
        1.0,
        0.0,
        0.0,
        center.x(),
        center.y(),
        canvas_width,
        canvas_height,
    )


def scale_layer(layer: Layer, factor: float, canvas_width: int, canvas_height: int) -> bool:
    if factor <= 0.0:
        return False
    bounds = content_bounds(layer)
    if bounds is None:
        return False
    center = bounds.center()
    return apply_transform(
        layer,
        0.0,
        factor,
        factor,
        0.0,
        0.0,
        center.x(),
        center.y(),
        canvas_width,
        canvas_height,
    )
