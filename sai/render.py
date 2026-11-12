from __future__ import annotations

import itertools
import math

from PySide6.QtCore import QByteArray, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen

from .geometry import sample_stroke
from .model import Document, Layer, Stroke
from .silhouette import node_bounds


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
    scale: float,
) -> None:
    """Draw one layer or group; ``composed`` applies its own opacity/blend."""
    if composed:
        painter.save()
        _apply_composition(painter, layer, background)
    if layer.is_group:
        _draw_stack(painter, layer.children, document, background, skip_stroke, scale)
    else:
        _draw_layer_content(painter, layer, skip_stroke)
    if composed:
        painter.restore()


# One clip mask serves a whole chain of clipped siblings until the base changes.
MAX_MASK_PIXELS = 8_000_000
_MASK_CACHE: dict[tuple[object, ...], tuple[QImage, QRectF, float]] = {}
_MASK_CACHE_LIMIT = 12


def _node_signature(layer: Layer) -> tuple[object, ...]:
    """Everything that changes the rendered alpha of a node (not its own opacity)."""
    parts: list[object] = [layer.uid, layer.kind, id(layer.image_base64), layer.visible]
    for stroke in layer.strokes:
        parts.append(
            (
                stroke.uid,
                stroke.alpha,
                stroke.fill,
                stroke.fill_alpha,
                stroke.closed,
                stroke.bridge,
                tuple((p.x, p.y, p.width, p.smooth) for p in stroke.points),
            )
        )
    if layer.is_group:
        for child in layer.children:
            parts.append(_node_signature(child))
            parts.append((child.opacity, child.blend))
    return tuple(parts)


def _mask_scale(bounds: QRectF, target: float) -> float:
    """Mask resolution: at least document pixels, at most a memory budget."""
    scale = max(1.0, target)
    area = max(1.0, bounds.width() * bounds.height())
    return min(scale, math.sqrt(MAX_MASK_PIXELS / area))


def _clip_mask(
    document: Document,
    layer: Layer,
    skip_stroke: Stroke | None,
    target_scale: float,
) -> tuple[QImage, QRectF, float] | None:
    """Cropped alpha mask of a clip base, cached by geometry and resolution."""
    bounds = node_bounds(layer)
    if bounds is None:
        bounds = QRectF(0, 0, document.width, document.height)
    page = QRectF(0, 0, document.width, document.height)
    bounds = bounds.adjusted(-1.0, -1.0, 1.0, 1.0).intersected(page)
    if bounds.width() < 0.5 or bounds.height() < 0.5:
        return None
    scale = _mask_scale(bounds, target_scale)
    key: tuple[object, ...] = (
        id(document),
        layer.uid,
        _node_signature(layer),
        round(scale, 4),
        round(bounds.x(), 2),
        round(bounds.y(), 2),
        round(bounds.width(), 2),
        round(bounds.height(), 2),
        document.width,
        document.height,
    )
    if skip_stroke is None:
        cached = _MASK_CACHE.get(key)
        if cached is not None:
            return cached

    width = max(1, round(bounds.width() * scale))
    height = max(1, round(bounds.height() * scale))
    image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.scale(scale, scale)
    painter.translate(-bounds.left(), -bounds.top())
    _draw_node(painter, layer, document, None, skip_stroke, composed=False, scale=scale)
    painter.end()

    result = (image, bounds, scale)
    if skip_stroke is None:
        if len(_MASK_CACHE) >= _MASK_CACHE_LIMIT:
            _MASK_CACHE.pop(next(iter(_MASK_CACHE)))
        _MASK_CACHE[key] = result
    return result


def _render_masked(
    document: Document,
    layer: Layer,
    skip_stroke: Stroke | None,
    mask: tuple[QImage, QRectF, float],
    clip: str,
) -> tuple[QImage, QRectF] | None:
    """Render a clipped layer into a cropped buffer and apply the base mask."""
    mask_image, mask_bounds, scale = mask
    page = QRectF(0, 0, document.width, document.height)
    bounds = node_bounds(layer)
    if bounds is None:
        bounds = page
    else:
        bounds = bounds.adjusted(-1.0, -1.0, 1.0, 1.0)
    rect = mask_bounds.united(bounds).intersected(page)
    if rect.width() < 0.5 or rect.height() < 0.5:
        return None
    width = max(1, round(rect.width() * scale))
    height = max(1, round(rect.height() * scale))
    buffer = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    buffer.fill(Qt.GlobalColor.transparent)
    painter = QPainter(buffer)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.scale(scale, scale)
    painter.translate(-rect.left(), -rect.top())
    _draw_node(painter, layer, document, None, skip_stroke, composed=False, scale=scale)
    # Compose the mask in device pixels; it may sit inside the larger buffer.
    painter.resetTransform()
    offset_x = (mask_bounds.x() - rect.x()) * scale
    offset_y = (mask_bounds.y() - rect.y()) * scale
    if clip == "outside":
        # Everything outside the mask image is "outside the base" already.
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationOut)
        painter.drawImage(QPointF(offset_x, offset_y), mask_image)
    else:
        # "inside" must also clear whatever lies outside the mask rectangle.
        full = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
        full.fill(Qt.GlobalColor.transparent)
        full_painter = QPainter(full)
        full_painter.drawImage(QPointF(offset_x, offset_y), mask_image)
        full_painter.end()
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
        painter.drawImage(QPointF(0, 0), full)
    painter.end()
    return buffer, rect


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
    scale: float,
) -> None:
    """Draw siblings in order; ``clip`` refers to the previous sibling.

    A hidden node is not painted, but it still acts as the clip base for its
    clipped siblings. The base is drawn directly; only its alpha mask is
    rendered offscreen, cropped to its bounds and at the target resolution.
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
                _draw_node(
                    painter, layer, document, background, skip_stroke, composed=True, scale=scale
                )
            index = cursor
            continue

        if layer.visible:
            _draw_node(
                painter, layer, document, background, skip_stroke, composed=True, scale=scale
            )

        mask = _clip_mask(document, layer, skip_stroke, scale)
        if mask is not None:
            for clipped_layer in clipped:
                if not clipped_layer.visible:
                    continue
                masked = _render_masked(
                    document, clipped_layer, skip_stroke, mask, clipped_layer.clip
                )
                if masked is None:
                    continue
                buffer, rect = masked
                painter.save()
                _apply_composition(painter, clipped_layer, background)
                painter.drawImage(rect, buffer)
                painter.restore()

        index = cursor


def draw_document(
    painter: QPainter,
    document: Document,
    background: QColor | None = None,
    skip_stroke: Stroke | None = None,
    scale: float = 1.0,
) -> None:
    """Render the document; ``scale`` is the device scale (zoom or export scale)."""
    if background is not None:
        painter.fillRect(QRectF(0, 0, document.width, document.height), background)
    _draw_stack(painter, document.layers, document, background, skip_stroke, scale)
