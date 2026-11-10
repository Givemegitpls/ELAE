from __future__ import annotations

import itertools

from PySide6.QtCore import QByteArray, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPen

from .geometry import sample_stroke
from .model import Document, Layer, Stroke


def _decode(payload: str) -> QImage | None:
    image = QImage()
    data = QByteArray.fromBase64(payload.encode("ascii"))
    if not image.loadFromData(data):
        return None
    return image


def layer_image(layer: Layer) -> QImage | None:
    """Decode and cache the raster payload of an image layer."""
    if layer.kind != "raster" or not layer.image_base64:
        return None
    cached = layer._cached_image
    if isinstance(cached, QImage):
        return cached
    image = _decode(layer.image_base64)
    layer._cached_image = image
    return image


def layer_mask(document: Document, layer: Layer) -> QImage | None:
    """Decode and cache the alpha mask referenced by a layer."""
    mask = document.mask(layer.mask_id)
    if mask is None or not mask.image_base64:
        return None
    cached = mask._cached_image
    if isinstance(cached, QImage):
        return cached
    image = _decode(mask.image_base64)
    mask._cached_image = image
    return image


def layer_has_mask(document: Document, layer: Layer) -> bool:
    mask = document.mask(layer.mask_id)
    return mask is not None and bool(mask.image_base64)


def draw_stroke(painter: QPainter, stroke: Stroke) -> None:
    samples = sample_stroke(stroke)
    if not samples:
        return

    color = QColor(stroke.color[0], stroke.color[1], stroke.color[2], stroke.alpha)

    if len(samples) == 1:
        x, y, width = samples[0]
        if width <= 0.01:
            return
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawEllipse(QPointF(x, y), max(width, 0.5) / 2, max(width, 0.5) / 2)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        return

    for (x1, y1, w1), (x2, y2, w2) in itertools.pairwise(samples):
        width = (w1 + w2) / 2
        if width <= 0.01:
            continue
        pen = QPen(
            color, width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin
        )
        painter.setPen(pen)
        painter.drawLine(QPointF(x1, y1), QPointF(x2, y2))


def _draw_layer_content(
    painter: QPainter,
    layer: Layer,
    skip_stroke: Stroke | None,
) -> None:
    image = layer_image(layer)
    if image is not None:
        painter.drawImage(QPointF(0, 0), image)
    for stroke in layer.strokes:
        if stroke is skip_stroke:
            continue
        draw_stroke(painter, stroke)


def _render_layer(document: Document, layer: Layer, skip_stroke: Stroke | None) -> QImage:
    buffer = QImage(
        max(1, document.width),
        max(1, document.height),
        QImage.Format.Format_ARGB32_Premultiplied,
    )
    buffer.fill(Qt.GlobalColor.transparent)

    painter = QPainter(buffer)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    _draw_layer_content(painter, layer, skip_stroke)

    mask = layer_mask(document, layer)
    if mask is not None:
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
        painter.drawImage(
            QRectF(0, 0, document.width, document.height),
            mask,
        )
    painter.end()
    return buffer


def _apply_composition(
    painter: QPainter,
    layer: Layer,
    background: QColor | None,
) -> None:
    painter.setOpacity(layer.opacity)
    if layer.blend == "multiply" and background is not None:
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Multiply)


def draw_document(
    painter: QPainter,
    document: Document,
    background: QColor | None = None,
    skip_stroke: Stroke | None = None,
) -> None:
    if background is not None:
        painter.fillRect(QRectF(0, 0, document.width, document.height), background)

    layers = document.layers
    index = 0
    while index < len(layers):
        layer = layers[index]
        if not layer.visible:
            index += 1
            continue

        clipped: list[Layer] = []
        cursor = index + 1
        while cursor < len(layers) and layers[cursor].clip_to_below:
            clipped.append(layers[cursor])
            cursor += 1

        if not clipped and not layer_has_mask(document, layer):
            painter.save()
            _apply_composition(painter, layer, background)
            _draw_layer_content(painter, layer, skip_stroke)
            painter.restore()
            index = cursor
            continue

        base = _render_layer(document, layer, skip_stroke)
        painter.save()
        _apply_composition(painter, layer, background)
        painter.drawImage(QPointF(0, 0), base)
        painter.restore()

        for clipped_layer in clipped:
            if not clipped_layer.visible:
                continue
            buffer = _render_layer(document, clipped_layer, skip_stroke)
            mask_painter = QPainter(buffer)
            mask_painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
            mask_painter.drawImage(QPointF(0, 0), base)
            mask_painter.end()

            painter.save()
            _apply_composition(painter, clipped_layer, background)
            painter.drawImage(QPointF(0, 0), buffer)
            painter.restore()

        index = cursor
