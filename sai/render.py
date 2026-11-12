from __future__ import annotations

import itertools

from PySide6.QtCore import QByteArray, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen

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


def stroke_path(stroke: Stroke) -> QPainterPath:
    """Closed painter path along the stroke centreline, for vector fills.

    Always closed: an open stroke is filled up to its ends, joined by a straight
    bridge (the same bridge `Stroke.bridge` draws).
    """
    samples = sample_stroke(stroke)
    path = QPainterPath()
    if not samples:
        return path
    path.moveTo(samples[0][0], samples[0][1])
    for x, y, _width in samples[1:]:
        path.lineTo(x, y)
    if len(samples) >= 3:
        path.closeSubpath()
    return path


def draw_stroke(painter: QPainter, stroke: Stroke, color: QColor | None = None) -> None:
    samples = sample_stroke(stroke)
    if not samples:
        return

    if stroke.fill is not None and len(samples) > 2:
        fill = QColor(stroke.fill[0], stroke.fill[1], stroke.fill[2], stroke.fill_alpha)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.fillPath(stroke_path(stroke), fill)
        painter.setPen(Qt.PenStyle.SolidLine)

    pen_color = (
        color
        if color is not None
        else QColor(stroke.color[0], stroke.color[1], stroke.color[2], stroke.alpha)
    )

    if len(samples) == 1:
        x, y, width = samples[0]
        if width <= 0.01:
            return
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(pen_color)
        painter.drawEllipse(QPointF(x, y), max(width, 0.5) / 2, max(width, 0.5) / 2)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        return

    for (x1, y1, w1), (x2, y2, w2) in itertools.pairwise(samples):
        width = (w1 + w2) / 2
        if width <= 0.01:
            continue
        pen = QPen(
            pen_color,
            width,
            Qt.PenStyle.SolidLine,
            Qt.PenCapStyle.RoundCap,
            Qt.PenJoinStyle.RoundJoin,
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


def _apply_composition(
    painter: QPainter,
    layer: Layer,
    background: QColor | None,
) -> None:
    # Multiply, not assign: a group's opacity must reach its children.
    painter.setOpacity(painter.opacity() * layer.opacity)
    if layer.blend == "multiply" and background is not None:
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Multiply)


def _draw_node(
    painter: QPainter,
    layer: Layer,
    document: Document,
    background: QColor | None,
    skip_stroke: Stroke | None,
    composed: bool,
) -> None:
    """Draw one layer or group; ``composed`` applies its own opacity/blend."""
    if composed:
        painter.save()
        _apply_composition(painter, layer, background)
    if layer.is_group:
        _draw_stack(painter, layer.children, document, background, skip_stroke)
    else:
        _draw_layer_content(painter, layer, skip_stroke)
    if composed:
        painter.restore()


def _render_node(document: Document, layer: Layer, skip_stroke: Stroke | None) -> QImage:
    """Layer or group content without its own opacity, for clip masking."""
    buffer = QImage(
        max(1, document.width),
        max(1, document.height),
        QImage.Format.Format_ARGB32_Premultiplied,
    )
    buffer.fill(Qt.GlobalColor.transparent)
    painter = QPainter(buffer)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    _draw_node(painter, layer, document, None, skip_stroke, composed=False)
    painter.end()
    return buffer


def _draw_image(
    painter: QPainter,
    image: QImage,
    layer: Layer,
    background: QColor | None,
) -> None:
    painter.save()
    _apply_composition(painter, layer, background)
    painter.drawImage(QPointF(0, 0), image)
    painter.restore()


def _draw_stack(
    painter: QPainter,
    layers: list[Layer],
    document: Document,
    background: QColor | None,
    skip_stroke: Stroke | None,
) -> None:
    """Draw siblings in order; ``clip`` refers to the previous sibling.

    A hidden node is not painted, but it still acts as the clip base for its
    clipped siblings (and its own hidden children feed the stencil). That is how
    one hides a mask layer without losing the masking.
    """
    index = 0
    while index < len(layers):
        layer = layers[index]

        clipped: list[Layer] = []
        cursor = index + 1
        while cursor < len(layers) and layers[cursor].clip != "off":
            clipped.append(layers[cursor])
            cursor += 1

        if not clipped:
            if layer.visible:
                _draw_node(painter, layer, document, background, skip_stroke, composed=True)
            index = cursor
            continue

        base = _render_node(document, layer, skip_stroke)
        if layer.visible:
            _draw_image(painter, base, layer, background)

        for clipped_layer in clipped:
            if not clipped_layer.visible:
                continue
            buffer = _render_node(document, clipped_layer, skip_stroke)
            mask_painter = QPainter(buffer)
            mode = (
                QPainter.CompositionMode.CompositionMode_DestinationOut
                if clipped_layer.clip == "outside"
                else QPainter.CompositionMode.CompositionMode_DestinationIn
            )
            mask_painter.setCompositionMode(mode)
            mask_painter.drawImage(QPointF(0, 0), base)
            mask_painter.end()
            _draw_image(painter, buffer, clipped_layer, background)

        index = cursor


def draw_document(
    painter: QPainter,
    document: Document,
    background: QColor | None = None,
    skip_stroke: Stroke | None = None,
) -> None:
    if background is not None:
        painter.fillRect(QRectF(0, 0, document.width, document.height), background)
    _draw_stack(painter, document.layers, document, background, skip_stroke)
