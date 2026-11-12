from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPainter

from . import io
from .model import Document, Layer, Point, Stroke
from .render import draw_document, layer_image

WALL_ALPHA = 32
HALO_PIXELS = 1
# Two stroke ends closer than this form one outline; the fill treats them as a
# single figure and bridges only the real opening.
JOIN_TOLERANCE = 10.0


@dataclass
class Figure:
    """One outline chained from strokes, with the merged polyline.

    ``points`` keeps the original points in drawing order, so filling it adds no
    geometry the user did not draw — only the straight bridge across the ends.
    """

    strokes: list[Stroke] = field(default_factory=list)
    reversed: list[bool] = field(default_factory=list)
    points: list[Point] = field(default_factory=list)
    closed: bool = False


def _ends(stroke: Stroke) -> tuple[tuple[float, float], tuple[float, float]]:
    first, last = stroke.points[0], stroke.points[-1]
    return (first.x, first.y), (last.x, last.y)


def _touching(a: tuple[float, float], b: tuple[float, float]) -> bool:
    return math.hypot(a[0] - b[0], a[1] - b[1]) <= JOIN_TOLERANCE


def _join_chain(strokes: list[Stroke], seed: Stroke) -> tuple[list[tuple[Stroke, bool]], bool]:
    """Yield the strokes joined end to end with ``seed`` and whether they close."""
    chain: list[tuple[Stroke, bool]] = [(seed, False)]
    used = {id(seed)}
    start, end = _ends(seed)
    for backwards in (False, True):
        while True:
            for stroke in strokes:
                if id(stroke) in used:
                    continue
                first, last = _ends(stroke)
                if not backwards and _touching(first, end):
                    chain.append((stroke, False))
                    end = last
                elif not backwards and _touching(last, end):
                    chain.append((stroke, True))
                    end = first
                elif backwards and _touching(last, start):
                    chain.insert(0, (stroke, False))
                    start = first
                elif backwards and _touching(first, start):
                    chain.insert(0, (stroke, True))
                    start = last
                else:
                    continue
                used.add(id(stroke))
                break
            else:
                break
    return chain, len(chain) > 1 and _touching(end, start)


def _merge(points_chain: list[tuple[Stroke, bool]]) -> list[Point]:
    merged: list[Point] = []
    for stroke, backwards in points_chain:
        source = list(reversed(stroke.points)) if backwards else list(stroke.points)
        copies = [point.copy() for point in source]
        if merged and copies:
            copies = copies[1:]  # both strokes share the joint point
        merged.extend(copies)
    return merged


def _bbox_distance(stroke: Stroke, x: float, y: float) -> float:
    """Distance from the point to the stroke's bounding box (cheap seed pick)."""
    xs = [point.x for point in stroke.points]
    ys = [point.y for point in stroke.points]
    dx = max(min(xs) - x, 0.0, x - max(xs))
    dy = max(min(ys) - y, 0.0, y - max(ys))
    return math.hypot(dx, dy)


def figure_at(strokes: list[Stroke], x: float, y: float) -> Figure | None:
    """Outline the point belongs to: the stroke nearest to it, chained with neighbours."""
    usable = [stroke for stroke in strokes if len(stroke.points) >= 2]
    if not usable:
        return None
    seed = min(usable, key=lambda stroke: _bbox_distance(stroke, x, y))
    chain, closed = _join_chain(usable, seed)
    if len(chain) == 1:
        closed = seed.closed
    return Figure(
        [stroke for stroke, _ in chain],
        [backwards for _, backwards in chain],
        _merge(chain),
        closed,
    )


# --- raster fill ---------------------------------------------------------


def _alpha_array(image: QImage) -> np.ndarray:
    alpha = image.convertToFormat(QImage.Format.Format_Alpha8)
    width = alpha.width()
    height = alpha.height()
    stride = alpha.bytesPerLine()
    data = np.frombuffer(alpha.constBits(), dtype=np.uint8, count=stride * height)
    return data.reshape(height, stride)[:, :width].copy()


def _flood(walls: np.ndarray, seed_x: int, seed_y: int) -> np.ndarray:
    height, width = walls.shape
    filled = np.zeros((height, width), dtype=bool)
    if not (0 <= seed_x < width and 0 <= seed_y < height):
        return filled
    if walls[seed_y, seed_x]:
        return filled

    stack: list[tuple[int, int]] = [(seed_x, seed_y)]
    while stack:
        x, y = stack.pop()
        if filled[y, x] or walls[y, x]:
            continue
        left = x
        while left > 0 and not walls[y, left - 1] and not filled[y, left - 1]:
            left -= 1
        right = x
        while right + 1 < width and not walls[y, right + 1] and not filled[y, right + 1]:
            right += 1
        filled[y, left : right + 1] = True

        for row in (y - 1, y + 1):
            if not 0 <= row < height:
                continue
            index = left
            while index <= right:
                if not walls[row, index] and not filled[row, index]:
                    stack.append((index, row))
                    while index <= right and not walls[row, index]:
                        index += 1
                else:
                    index += 1
    return filled


def _dilate(mask: np.ndarray, pixels: int) -> np.ndarray:
    result = mask
    for _ in range(pixels):
        grown = result.copy()
        grown[1:, :] |= result[:-1, :]
        grown[:-1, :] |= result[1:, :]
        grown[:, 1:] |= result[:, :-1]
        grown[:, :-1] |= result[:, 1:]
        result = grown
    return result


def _is_wall(layer: Layer, above: bool, scope: str, source_uid: str) -> bool:
    if scope == "layer":
        return layer.uid == source_uid
    # Raster layers below the target are background, not boundaries: the canvas
    # itself closes an open region. Everything else (vector layers, groups) and
    # the rasters above the target act as walls.
    return layer.kind != "raster" or above


def region_mask(
    document: Document,
    target: Layer,
    x: float,
    y: float,
    scope: str = "image",
    source_uid: str = "",
) -> np.ndarray | None:
    """Boolean fill region reachable from the seed, or ``None`` when empty."""
    width = document.width
    height = document.height
    seed_x = int(x)
    seed_y = int(y)
    if not (0 <= seed_x < width and 0 <= seed_y < height):
        return None

    entries = [layer for layer, _depth in document.walk()]
    try:
        target_index = next(i for i, layer in enumerate(entries) if layer is target)
    except StopIteration:
        return None

    walls_of: list[Layer] = []
    for index, layer in enumerate(entries):
        if index == target_index:
            continue
        if _is_wall(layer, index > target_index, scope, source_uid):
            walls_of.append(layer)

    # The document copy keeps the masks, so a masked wall renders as on the canvas.
    reference = document.copy()
    reference.layers = walls_of
    reference_image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    reference_image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(reference_image)
    draw_document(painter, reference)
    painter.end()

    walls = _alpha_array(reference_image) > WALL_ALPHA
    region = _flood(walls, seed_x, seed_y)
    if not region.any():
        return None
    return _dilate(region, HALO_PIXELS)


def bucket_fill(
    document: Document,
    target: Layer,
    x: float,
    y: float,
    color: tuple[int, int, int],
    alpha: int,
    scope: str = "image",
    source_uid: str = "",
) -> bool:
    """Flood-fill the target raster layer using the visible lineart as walls."""
    if target.kind != "raster":
        return False
    width = document.width
    height = document.height
    region = region_mask(document, target, x, y, scope, source_uid)
    if region is None:
        return False
    seed_x = int(x)
    seed_y = int(y)
    if not (0 <= seed_x < width and 0 <= seed_y < height):
        return False

    base = layer_image(target)
    if base is None or base.width() != width or base.height() != height:
        canvas = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
        canvas.fill(Qt.GlobalColor.transparent)
        if base is not None:
            base_painter = QPainter(canvas)
            base_painter.drawImage(0, 0, base)
            base_painter.end()
        base = canvas

    rgba = np.zeros((height, width, 4), dtype=np.uint8)
    rgba[..., 0] = color[0]
    rgba[..., 1] = color[1]
    rgba[..., 2] = color[2]
    rgba[..., 3] = np.where(region, alpha, 0).astype(np.uint8)
    mask = QImage(
        rgba.data,
        width,
        height,
        width * 4,
        QImage.Format.Format_RGBA8888,
    ).copy()

    fill_painter = QPainter(base)
    fill_painter.drawImage(0, 0, mask)
    fill_painter.end()

    target.image_base64 = io.encode_image(base)
    target._cached_image = None
    return True
