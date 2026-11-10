from __future__ import annotations

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPainter

from . import io
from .model import Document
from .render import draw_document, layer_image

WALL_ALPHA = 32
HALO_PIXELS = 1


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


def bucket_fill(
    document: Document,
    target_index: int,
    x: float,
    y: float,
    color: tuple[int, int, int],
    alpha: int,
) -> bool:
    """Flood-fill the target raster layer using the visible lineart as walls."""
    if not 0 <= target_index < len(document.layers):
        return False
    width = document.width
    height = document.height
    seed_x = int(x)
    seed_y = int(y)
    if not (0 <= seed_x < width and 0 <= seed_y < height):
        return False

    reference = document.copy()
    reference.layers = [
        layer
        for index, layer in enumerate(document.layers)
        if index != target_index
        # Raster layers below the fill are background, not boundaries: the canvas
        # itself closes an open region. Vector layers and rasters above the fill
        # act as walls.
        and (layer.kind == "vector" or index > target_index)
    ]
    reference_image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    reference_image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(reference_image)
    draw_document(painter, reference)
    painter.end()

    walls = _alpha_array(reference_image) > WALL_ALPHA
    region = _flood(walls, seed_x, seed_y)
    if not region.any():
        return False
    region = _dilate(region, HALO_PIXELS)

    target = document.layers[target_index]
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
