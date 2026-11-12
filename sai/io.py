from __future__ import annotations

import base64
import itertools
import json
import tempfile
from pathlib import Path

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath

from . import silhouette
from .geometry import sample_stroke
from .model import Document, Layer
from .render import draw_document, layer_image

FORMAT = "sai-lineart"
# Version 2 introduced shared masks (removed again), 3 adds layer groups.
VERSION = 3


def ensure_suffix(path: str | Path, suffix: str) -> Path:
    """Return ``path`` with ``suffix`` unless it already ends with a real extension.

    Terminal file choosers built on xdg-desktop-portal hand back the portal's
    placeholder name (``termfilechooser.tmp``) when the application suggests no
    file name, so ``.tmp`` counts as "no extension" here.
    """
    result = Path(path)
    current = result.suffix.lower()
    if current and current != ".tmp":
        return result
    return result.with_suffix(suffix)


def save_document(document: Document, path: str | Path) -> None:
    payload = {"format": FORMAT, "version": VERSION, **document.to_dict()}
    Path(path).write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_document(path: str | Path) -> Document:
    raw = Path(path).read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(
            "это не JSON-документ sai-lineart; картинку открывайте через File > Import PNG as Layer"
        ) from error
    data = json.loads(text)
    if data.get("format") not in (None, FORMAT):
        raise ValueError(f"unknown document format: {data.get('format')!r}")
    # Old files have no version key; every version so far is backward compatible.
    return Document.from_dict(data)


def export_png(
    document: Document,
    path: str | Path,
    scale: float = 1.0,
    background: QColor | None = None,
) -> None:
    width = max(1, round(document.width * scale))
    height = max(1, round(document.height * scale))
    image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(background if background is not None else Qt.GlobalColor.transparent)

    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.scale(scale, scale)
    draw_document(painter, document, background=background, scale=scale)
    painter.end()

    if not image.save(str(path)):
        raise OSError(f"could not write {path}")


def export_svg(document: Document, path: str | Path) -> None:
    """Write the document as SVG; clip becomes ``clipPath`` (opaque) or ``mask``."""
    width = document.width
    height = document.height
    body: list[str] = []
    defs: list[str] = []
    counter = 0

    def stroke_markup(layer: Layer, depth: int, mono: str | None) -> list[str]:
        indent = "  " * depth
        lines: list[str] = []
        if layer.kind == "raster" and layer.image_base64:
            raster = layer_image(layer)
            if raster is not None:
                lines.append(
                    f'{indent}<image x="0" y="0" width="{raster.width()}" '
                    f'height="{raster.height()}" '
                    f'href="data:image/png;base64,{layer.image_base64}"/>'
                )
        for stroke in layer.strokes:
            color = mono or "#{:02x}{:02x}{:02x}".format(*stroke.color)
            alpha = 1.0 if mono is not None else stroke.alpha / 255
            samples = sample_stroke(stroke)
            if stroke.fill is not None and len(samples) > 2:
                loop = [(x, y) for x, y, _w in samples]
                fill = mono or "#{:02x}{:02x}{:02x}".format(*stroke.fill)
                fill_alpha = 1.0 if mono is not None else stroke.fill_alpha / 255
                lines.append(
                    f'{indent}<path d="{_svg_path([loop])}" fill="{fill}" '
                    f'fill-opacity="{fill_alpha:.3f}" fill-rule="evenodd"/>'
                )
            if len(samples) == 1:
                x, y, stroke_width = samples[0]
                if stroke_width <= 0.01:
                    continue
                radius = max(stroke_width, 0.5) / 2
                lines.append(
                    f'{indent}<circle cx="{x:.2f}" cy="{y:.2f}" r="{radius:.2f}" '
                    f'fill="{color}" fill-opacity="{alpha:.3f}"/>'
                )
                continue
            for (x1, y1, w1), (x2, y2, w2) in itertools.pairwise(samples):
                stroke_width = (w1 + w2) / 2
                if stroke_width <= 0.01:
                    continue
                lines.append(
                    f'{indent}<line x1="{x1:.2f}" y1="{y1:.2f}" x2="{x2:.2f}" y2="{y2:.2f}" '
                    f'stroke="{color}" stroke-opacity="{alpha:.3f}" '
                    f'stroke-width="{stroke_width:.2f}" stroke-linecap="round"/>'
                )
        return lines

    def node_markup(
        layer: Layer, depth: int, extra: str = "", mono: str | None = None
    ) -> list[str]:
        indent = "  " * depth
        style = ""
        if mono is None:
            if layer.opacity < 1.0:
                style += f' opacity="{layer.opacity:.3f}"'
            if layer.blend == "multiply":
                style += ' style="mix-blend-mode:multiply"'
        lines = [f'{indent}<g id="{_escape(layer.name)}"{style}{extra}>']
        if layer.is_group:
            lines += stack_markup(layer.children, depth + 1, mono)
        else:
            lines += stroke_markup(layer, depth + 1, mono)
        lines.append(f"{indent}</g>")
        return lines

    def mask_markup(base: Layer, invert: bool) -> str:
        nonlocal counter
        identifier = f"mask{counter}"
        counter += 1
        inner: list[str] = []
        if invert:
            inner.append(f'    <rect x="0" y="0" width="{width}" height="{height}" fill="white"/>')
            inner += node_markup(base, 2, mono="black")
            kind = "luminance"
        else:
            inner += node_markup(base, 2)
            kind = "alpha"
        defs.append(
            f'  <mask id="{identifier}" mask-type="{kind}" maskUnits="userSpaceOnUse" '
            f'x="0" y="0" width="{width}" height="{height}">\n' + "\n".join(inner) + "\n  </mask>"
        )
        return f' mask="url(#{identifier})"'

    def clip_markup(base: Layer, outside: bool) -> str:
        nonlocal counter
        identifier = f"clip{counter}"
        counter += 1
        path = silhouette.outline_path(base)
        if outside:
            page = QPainterPath()
            page.addRect(QRectF(0, 0, width, height))
            path = page.subtracted(path.simplified())
        flat = _path_data(path)
        defs.append(
            f'  <clipPath id="{identifier}" clipPathUnits="userSpaceOnUse">\n'
            f'    <path d="{flat}" clip-rule="nonzero"/>\n'
            f"  </clipPath>"
        )
        return f' clip-path="url(#{identifier})"'

    def clipped_markup(base: Layer, clipped: list[Layer], depth: int) -> list[str]:
        lines: list[str] = []
        for item in clipped:
            if not item.visible:
                continue
            outside = item.clip == "outside"
            if silhouette.is_opaque(base):
                attribute = clip_markup(base, outside)
            else:
                attribute = mask_markup(base, outside)
            lines += node_markup(item, depth, extra=attribute)
        return lines

    def stack_markup(items: list[Layer], depth: int, mono: str | None = None) -> list[str]:
        lines: list[str] = []
        index = 0
        while index < len(items):
            layer = items[index]
            clipped: list[Layer] = []
            cursor = index + 1
            while cursor < len(items) and items[cursor].clip != "off":
                clipped.append(items[cursor])
                cursor += 1
            if layer.visible:
                lines += node_markup(layer, depth, mono=mono)
            if clipped:
                lines += clipped_markup(layer, clipped, depth)
            index = cursor
        return lines

    body += stack_markup(document.layers, 1)

    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
            f'height="{height}" viewBox="0 0 {width} {height}">'
        ),
    ]
    if defs:
        parts.append("  <defs>")
        parts += defs
        parts.append("  </defs>")
    parts += body
    parts.append("</svg>")
    Path(path).write_text("\n".join(parts) + "\n", encoding="utf-8")


def _path_data(path: QPainterPath) -> str:
    """Flatten a painter path into SVG path data (polygons only)."""
    parts: list[str] = []
    for polygon in path.toSubpathPolygons():
        if polygon.size() < 3:
            continue
        points = " L ".join(
            f"{polygon.at(index).x():.2f} {polygon.at(index).y():.2f}"
            for index in range(polygon.size())
        )
        parts.append(f"M {points} Z")
    return " ".join(parts)


def _svg_path(loops: list[list[tuple[float, float]]]) -> str:
    parts: list[str] = []
    for loop in loops:
        if len(loop) < 3:
            continue
        points = " L ".join(f"{x:.2f} {y:.2f}" for x, y in loop)
        parts.append(f"M {points} Z")
    return " ".join(parts)


def encode_image(image: QImage) -> str:
    """Encode a QImage as base64 PNG for embedding in a document."""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
        temp_path = Path(handle.name)
    try:
        if not image.save(str(temp_path)):
            raise OSError("could not encode image")
        return base64.b64encode(temp_path.read_bytes()).decode("ascii")
    finally:
        temp_path.unlink(missing_ok=True)


def load_raster_layer(path: str | Path, name: str | None = None) -> Layer:
    """Load an image file into a raster layer (PNG payload inside the document)."""
    image = QImage(str(path))
    if image.isNull():
        raise OSError(f"could not read image {path}")

    return Layer(
        name=name or Path(path).stem,
        kind="raster",
        image_base64=encode_image(image),
    )


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


__all__ = [
    "FORMAT",
    "VERSION",
    "encode_image",
    "ensure_suffix",
    "export_png",
    "export_svg",
    "load_document",
    "load_raster_layer",
    "save_document",
]
