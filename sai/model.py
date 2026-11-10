from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Point:
    x: float
    y: float
    width: float
    smooth: bool = False

    def copy(self) -> Point:
        return Point(self.x, self.y, self.width, self.smooth)


@dataclass
class Stroke:
    points: list[Point] = field(default_factory=list)
    color: tuple[int, int, int] = (20, 20, 20)
    alpha: int = 255

    def copy(self) -> Stroke:
        return Stroke([p.copy() for p in self.points], self.color, self.alpha)

    def to_dict(self) -> dict[str, Any]:
        return {
            "color": list(self.color),
            "alpha": self.alpha,
            "points": [[p.x, p.y, p.width, p.smooth] for p in self.points],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Stroke:
        points = [
            Point(float(x), float(y), float(w), bool(s)) for x, y, w, s in data.get("points", [])
        ]
        red, green, blue = (int(c) for c in data.get("color", (20, 20, 20)))
        color = (red, green, blue)
        return cls(points, color, int(data.get("alpha", 255)))


@dataclass
class Mask:
    """Alpha mask shared by any number of layers."""

    name: str = "Mask"
    image_base64: str = ""
    _cached_image: object | None = field(default=None, repr=False, compare=False)

    def copy(self) -> Mask:
        return Mask(self.name, self.image_base64)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "image": self.image_base64}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Mask:
        return cls(str(data.get("name", "Mask")), str(data.get("image", "")))


@dataclass
class Layer:
    name: str = "Layer"
    strokes: list[Stroke] = field(default_factory=list)
    visible: bool = True
    opacity: float = 1.0
    blend: str = "normal"
    kind: str = "vector"
    image_base64: str | None = None
    clip_to_below: bool = False
    mask_id: str | None = None
    _cached_image: object | None = field(default=None, repr=False, compare=False)

    def copy(self) -> Layer:
        return Layer(
            name=self.name,
            strokes=[s.copy() for s in self.strokes],
            visible=self.visible,
            opacity=self.opacity,
            blend=self.blend,
            kind=self.kind,
            image_base64=self.image_base64,
            clip_to_below=self.clip_to_below,
            mask_id=self.mask_id,
        )

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "name": self.name,
            "visible": self.visible,
            "opacity": self.opacity,
            "blend": self.blend,
            "kind": self.kind,
            "strokes": [s.to_dict() for s in self.strokes],
        }
        if self.image_base64:
            data["image"] = self.image_base64
        if self.clip_to_below:
            data["clip"] = True
        if self.mask_id:
            data["mask_id"] = self.mask_id
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Layer:
        mask_id = data.get("mask_id")
        return cls(
            name=str(data.get("name", "Layer")),
            strokes=[Stroke.from_dict(s) for s in data.get("strokes", [])],
            visible=bool(data.get("visible", True)),
            opacity=float(data.get("opacity", 1.0)),
            blend=str(data.get("blend", "normal")),
            kind=str(data.get("kind", "vector")),
            image_base64=data.get("image"),
            clip_to_below=bool(data.get("clip", False)),
            mask_id=str(mask_id) if mask_id else None,
        )


@dataclass
class Document:
    width: int = 2000
    height: int = 2000
    layers: list[Layer] = field(default_factory=lambda: [Layer("Layer 1")])
    active_layer: int = 0
    masks: dict[str, Mask] = field(default_factory=dict)

    def copy(self) -> Document:
        return Document(
            width=self.width,
            height=self.height,
            layers=[layer.copy() for layer in self.layers],
            active_layer=self.active_layer,
            masks={key: mask.copy() for key, mask in self.masks.items()},
        )

    def layer(self) -> Layer:
        index = min(max(self.active_layer, 0), len(self.layers) - 1)
        return self.layers[index]

    def mask(self, mask_id: str | None) -> Mask | None:
        if not mask_id:
            return None
        return self.masks.get(mask_id)

    def add_mask(self, name: str, image_base64: str) -> str:
        """Store a new shared mask and return its id."""
        mask_id = _next_mask_id(self.masks)
        self.masks[mask_id] = Mask(name, image_base64)
        return mask_id

    def remove_unused_masks(self) -> int:
        """Drop masks no layer references; return how many were removed."""
        used = {layer.mask_id for layer in self.layers if layer.mask_id}
        stale = [key for key in self.masks if key not in used]
        for key in stale:
            del self.masks[key]
        return len(stale)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "width": self.width,
            "height": self.height,
            "active_layer": self.active_layer,
            "layers": [layer.to_dict() for layer in self.layers],
        }
        if self.masks:
            data["masks"] = [{"id": key, **mask.to_dict()} for key, mask in self.masks.items()]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Document:
        masks = {
            str(item["id"]): Mask.from_dict(item) for item in data.get("masks", []) if "id" in item
        }
        layers: list[Layer] = []
        for raw in data.get("layers", []):
            layer = Layer.from_dict(raw)
            # Version 1 stored the mask payload inside the layer; move it to the
            # shared mask store so one mask can serve many layers.
            legacy = raw.get("mask")
            if legacy:
                layer.mask_id = _next_mask_id(masks)
                masks[layer.mask_id] = Mask(f"{layer.name} mask", str(legacy))
            layers.append(layer)
        if not layers:
            layers = [Layer("Layer 1")]
        return cls(
            width=int(data.get("width", 2000)),
            height=int(data.get("height", 2000)),
            layers=layers,
            active_layer=int(data.get("active_layer", 0)),
            masks=masks,
        )


def _next_mask_id(masks: dict[str, Mask]) -> str:
    index = 1
    while f"mask-{index}" in masks:
        index += 1
    return f"mask-{index}"
