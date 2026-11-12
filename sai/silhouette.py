from __future__ import annotations

import itertools
import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QPainterPath

from .geometry import sample_stroke
from .model import Layer, Stroke


def stroke_outline(stroke: Stroke) -> QPainterPath:
    """Silhouette of one stroke: filled centreline plus the drawn ribbon.

    The ribbon is built from per-pair quads and a circle at every sample, with
    the nonzero winding rule, so overlapping pieces union without ``united``.
    """
    path = QPainterPath()
    path.setFillRule(Qt.FillRule.WindingFill)
    samples = sample_stroke(stroke)
    if not samples:
        return path

    if stroke.fill is not None and len(samples) >= 3:
        path.moveTo(samples[0][0], samples[0][1])
        for x, y, _width in samples[1:]:
            path.lineTo(x, y)
        path.closeSubpath()

    for (x1, y1, w1), (x2, y2, w2) in itertools.pairwise(samples):
        width = (w1 + w2) / 2
        if width <= 0.01:
            continue
        dx = x2 - x1
        dy = y2 - y1
        length = math.hypot(dx, dy)
        if length <= 1e-9:
            continue
        offset_x = -dy / length * width / 2
        offset_y = dx / length * width / 2
        path.moveTo(x1 + offset_x, y1 + offset_y)
        path.lineTo(x2 + offset_x, y2 + offset_y)
        path.lineTo(x2 - offset_x, y2 - offset_y)
        path.lineTo(x1 - offset_x, y1 - offset_y)
        path.closeSubpath()

    for x, y, width in samples:
        if width > 0.01:
            path.addEllipse(QPointF(x, y), width / 2, width / 2)
    return path


def outline_path(layer: Layer) -> QPainterPath:
    """Vector silhouette of a layer or group (empty for raster content)."""
    path = QPainterPath()
    path.setFillRule(Qt.FillRule.WindingFill)
    _collect(layer, path)
    return path


def _collect(layer: Layer, path: QPainterPath) -> None:
    if layer.is_group:
        for child in layer.children:
            if child.visible:
                _collect(child, path)
        return
    if layer.kind == "raster":
        return
    for stroke in layer.strokes:
        path.addPath(stroke_outline(stroke))


def node_bounds(layer: Layer) -> QRectF | None:
    """Content bounds of a node, or ``None`` when it is not knowable (raster).

    The node's own ``visible`` is ignored: a hidden clip base still supplies its
    alpha. Hidden children of a group do not render, so they are skipped.
    """
    if layer.is_group:
        bounds = QRectF()
        for child in layer.children:
            if not child.visible:
                continue
            child_bounds = node_bounds(child)
            if child_bounds is None:
                return None
            bounds = child_bounds if bounds.isNull() else bounds.united(child_bounds)
        return bounds
    if layer.kind == "raster":
        return None
    bounds = QRectF()
    for stroke in layer.strokes:
        for x, y, width in sample_stroke(stroke):
            half = max(width, 0.0) / 2
            piece = QRectF(x - half, y - half, half * 2, half * 2)
            bounds = piece if bounds.isNull() else bounds.united(piece)
    return bounds


def is_opaque(layer: Layer) -> bool:
    """True when the node's alpha is binary inside a geometric silhouette."""
    if layer.opacity < 1.0 or layer.blend != "normal":
        return False
    if layer.is_group:
        return all(is_opaque(child) for child in layer.children if child.visible)
    if layer.kind == "raster":
        return False
    for stroke in layer.strokes:
        if stroke.alpha < 255:
            return False
        if stroke.fill is not None and stroke.fill_alpha < 255:
            return False
    return True
