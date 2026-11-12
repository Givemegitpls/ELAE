from __future__ import annotations

import base64
import itertools
import json
import tempfile
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage, QPainter

from .geometry import sample_stroke
from .model import Document, Layer
from .render import draw_document

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
    draw_document(painter, document, background=background)
    painter.end()

    if not image.save(str(path)):
        raise OSError(f"could not write {path}")


def export_svg(document: Document, path: str | Path) -> None:
    parts: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{document.width}" '
            f'height="{document.height}" viewBox="0 0 {document.width} {document.height}">'
        ),
    ]

    def emit(items: list[Layer], depth: int) -> None:
        indent = "  " * depth
        for layer in items:
            if not layer.visible:
                continue
            style = ""
            if layer.opacity < 1.0:
                style = f' opacity="{layer.opacity:.3f}"'
            if layer.blend == "multiply":
                style += ' style="mix-blend-mode:multiply"'
            parts.append(f'{indent}<g id="{_escape(layer.name)}"{style}>')
            if layer.is_group:
                emit(layer.children, depth + 1)
            for stroke in layer.strokes:
                color = "#{:02x}{:02x}{:02x}".format(*stroke.color)
                alpha = stroke.alpha / 255
                samples = sample_stroke(stroke)
                if stroke.fill is not None and len(samples) > 2:
                    loop = [(x, y) for x, y, _width in samples]
                    parts.append(
                        f'{indent}  <path d="{_svg_path([loop])}" '
                        f'fill="#{stroke.fill[0]:02x}{stroke.fill[1]:02x}{stroke.fill[2]:02x}" '
                        f'fill-opacity="{stroke.fill_alpha / 255:.3f}"/>'
                    )
                if len(samples) == 1:
                    x, y, width = samples[0]
                    if width <= 0.01:
                        continue
                    radius = max(width, 0.5) / 2
                    parts.append(
                        f'{indent}  <circle cx="{x:.2f}" cy="{y:.2f}" r="{radius:.2f}" '
                        f'fill="{color}" fill-opacity="{alpha:.3f}"/>'
                    )
                    continue
                for (x1, y1, w1), (x2, y2, w2) in itertools.pairwise(samples):
                    width = (w1 + w2) / 2
                    if width <= 0.01:
                        continue
                    parts.append(
                        f'{indent}  <line x1="{x1:.2f}" y1="{y1:.2f}" x2="{x2:.2f}" y2="{y2:.2f}" '
                        f'stroke="{color}" stroke-opacity="{alpha:.3f}" '
                        f'stroke-width="{width:.2f}" stroke-linecap="round"/>'
                    )
            parts.append(f"{indent}</g>")

    emit(document.layers, 1)

    parts.append("</svg>")
    Path(path).write_text("\n".join(parts) + "\n", encoding="utf-8")


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
