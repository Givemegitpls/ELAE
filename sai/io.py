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
# Version 2 moved layer masks into the document-wide shared mask store.
VERSION = 2


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
    scale: float = 2.0,
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

    for layer in document.layers:
        if not layer.visible:
            continue
        style = ""
        if layer.opacity < 1.0:
            style = f' opacity="{layer.opacity:.3f}"'
        if layer.blend == "multiply":
            style += ' style="mix-blend-mode:multiply"'
        parts.append(f'  <g id="{_escape(layer.name)}"{style}>')
        for stroke in layer.strokes:
            color = "#{:02x}{:02x}{:02x}".format(*stroke.color)
            alpha = stroke.alpha / 255
            samples = sample_stroke(stroke)
            if len(samples) == 1:
                x, y, width = samples[0]
                if width <= 0.01:
                    continue
                radius = max(width, 0.5) / 2
                parts.append(
                    f'    <circle cx="{x:.2f}" cy="{y:.2f}" r="{radius:.2f}" '
                    f'fill="{color}" fill-opacity="{alpha:.3f}"/>'
                )
                continue
            for (x1, y1, w1), (x2, y2, w2) in itertools.pairwise(samples):
                width = (w1 + w2) / 2
                if width <= 0.01:
                    continue
                parts.append(
                    f'    <line x1="{x1:.2f}" y1="{y1:.2f}" x2="{x2:.2f}" y2="{y2:.2f}" '
                    f'stroke="{color}" stroke-opacity="{alpha:.3f}" '
                    f'stroke-width="{width:.2f}" stroke-linecap="round"/>'
                )
        parts.append("  </g>")

    parts.append("</svg>")
    Path(path).write_text("\n".join(parts) + "\n", encoding="utf-8")


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
    "export_png",
    "export_svg",
    "load_document",
    "load_raster_layer",
    "save_document",
]
