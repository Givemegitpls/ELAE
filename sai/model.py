from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4


def _new_uid() -> str:
    return uuid4().hex


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
    closed: bool = False
    fill: tuple[int, int, int] | None = None
    fill_alpha: int = 255
    # Close last -> first with a straight bridge instead of a sampled curve.
    bridge: bool = False
    uid: str = field(default_factory=_new_uid)

    def copy(self) -> Stroke:
        return Stroke(
            [p.copy() for p in self.points],
            self.color,
            self.alpha,
            self.closed,
            self.fill,
            self.fill_alpha,
            self.bridge,
            self.uid,
        )

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "color": list(self.color),
            "alpha": self.alpha,
            "points": [[p.x, p.y, p.width, p.smooth] for p in self.points],
            "uid": self.uid,
        }
        if self.closed:
            data["closed"] = True
        if self.fill is not None:
            data["fill"] = list(self.fill)
            data["fill_alpha"] = self.fill_alpha
        if self.bridge:
            data["bridge"] = True
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Stroke:
        points = [
            Point(float(x), float(y), float(w), bool(s)) for x, y, w, s in data.get("points", [])
        ]
        red, green, blue = (int(c) for c in data.get("color", (20, 20, 20)))
        color = (red, green, blue)
        fill = None
        raw_fill = data.get("fill")
        if raw_fill is not None:
            fill = (int(raw_fill[0]), int(raw_fill[1]), int(raw_fill[2]))
        return cls(
            points,
            color,
            int(data.get("alpha", 255)),
            bool(data.get("closed", False)),
            fill,
            int(data.get("fill_alpha", 255)),
            bool(data.get("bridge", False)),
            str(data.get("uid") or _new_uid()),
        )


def _clip_mode(value: object) -> str:
    """``"clip": true`` in older files means the intersection mode."""
    if value is True:
        return "inside"
    text = str(value or "off").lower()
    return text if text in ("inside", "outside") else "off"


@dataclass
class Layer:
    """A layer, or a group of layers when ``kind == "group"``."""

    name: str = "Layer"
    strokes: list[Stroke] = field(default_factory=list)
    # Children of a group, in draw order. Empty for ordinary layers.
    children: list[Layer] = field(default_factory=list)
    visible: bool = True
    opacity: float = 1.0
    blend: str = "normal"
    kind: str = "vector"  # "vector" | "raster" | "group"
    image_base64: str | None = None
    # "off" | "inside" (intersection with the sibling below) | "outside" (subtract it)
    clip: str = "off"
    uid: str = field(default_factory=_new_uid)
    _cached_image: object | None = field(default=None, repr=False, compare=False)

    @property
    def is_group(self) -> bool:
        return self.kind == "group"

    def copy(self) -> Layer:
        return Layer(
            name=self.name,
            strokes=[s.copy() for s in self.strokes],
            children=[child.copy() for child in self.children],
            visible=self.visible,
            opacity=self.opacity,
            blend=self.blend,
            kind=self.kind,
            image_base64=self.image_base64,
            clip=self.clip,
            uid=self.uid,
        )

    def duplicate(self) -> Layer:
        """Deep copy for a second layer in the same document: fresh uids."""
        copy = self.copy()
        copy.uid = _new_uid()
        for stroke in copy.strokes:
            stroke.uid = _new_uid()
        for child in copy.children:
            child.uid = _new_uid()
            for stroke in child.strokes:
                stroke.uid = _new_uid()
        return copy

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "name": self.name,
            "visible": self.visible,
            "opacity": self.opacity,
            "blend": self.blend,
            "kind": self.kind,
            "strokes": [s.to_dict() for s in self.strokes],
            "uid": self.uid,
        }
        if self.children:
            data["children"] = [child.to_dict() for child in self.children]
        if self.image_base64:
            data["image"] = self.image_base64
        if self.clip != "off":
            data["clip"] = self.clip
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Layer:
        return cls(
            name=str(data.get("name", "Layer")),
            strokes=[Stroke.from_dict(s) for s in data.get("strokes", [])],
            children=[Layer.from_dict(child) for child in data.get("children", [])],
            visible=bool(data.get("visible", True)),
            opacity=float(data.get("opacity", 1.0)),
            blend=str(data.get("blend", "normal")),
            kind=str(data.get("kind", "vector")),
            image_base64=data.get("image"),
            clip=_clip_mode(data.get("clip", "off")),
            uid=str(data.get("uid") or _new_uid()),
        )


@dataclass
class Document:
    width: int = 2000
    height: int = 2000
    layers: list[Layer] = field(default_factory=lambda: [Layer("Layer 1")])
    # Index into ``walk()``: groups are entries too, in draw order.
    active_layer: int = 0

    def copy(self) -> Document:
        return Document(
            width=self.width,
            height=self.height,
            layers=[layer.copy() for layer in self.layers],
            active_layer=self.active_layer,
        )

    # --- tree -----------------------------------------------------------
    def walk(self) -> list[tuple[Layer, int]]:
        """Layers in draw order with their nesting depth; groups included."""
        entries: list[tuple[Layer, int]] = []

        def visit(items: list[Layer], depth: int) -> None:
            for item in items:
                entries.append((item, depth))
                if item.is_group:
                    visit(item.children, depth + 1)

        visit(self.layers, 0)
        return entries

    def all_layers(self) -> Iterator[Layer]:
        """Every layer except groups, in draw order."""
        for layer, _depth in self.walk():
            if not layer.is_group:
                yield layer

    def walk_index(self, layer: Layer) -> int:
        for index, (item, _depth) in enumerate(self.walk()):
            if item is layer:
                return index
        return -1

    def locate(self, layer: Layer) -> tuple[list[Layer], int] | None:
        """Sibling list and position of ``layer`` (top-level or inside a group)."""
        stack: list[list[Layer]] = [self.layers]
        while stack:
            siblings = stack.pop()
            for index, item in enumerate(siblings):
                if item is layer:
                    return siblings, index
                if item.is_group:
                    stack.append(item.children)
        return None

    def layer_at(self, index: int) -> Layer:
        entries = self.walk()
        if not entries:
            return Layer()
        return entries[min(max(index, 0), len(entries) - 1)][0]

    def layer(self) -> Layer:
        return self.layer_at(self.active_layer)

    def parent(self, layer: Layer) -> Layer | None:
        for candidate, _depth in self.walk():
            if candidate.is_group and any(child is layer for child in candidate.children):
                return candidate
        return None

    def sort_layers(self, layers: list[Layer]) -> list[Layer]:
        """Given layers in draw order (walk index ascending), duplicates removed."""
        order = {id(layer): index for index, (layer, _depth) in enumerate(self.walk())}
        seen: set[int] = set()
        ordered: list[Layer] = []
        for layer in sorted(layers, key=lambda item: order.get(id(item), 0)):
            if id(layer) in seen:
                continue
            seen.add(id(layer))
            ordered.append(layer)
        return ordered

    def selection_roots(self, layers: list[Layer]) -> list[Layer]:
        """Selected nodes in draw order, without descendants of selected groups.

        Selecting a group and one of its children acts on the group only: the
        child is already part of it.
        """
        ordered = self.sort_layers(layers)
        return [
            layer
            for layer in ordered
            if not any(other is not layer and self.is_ancestor(other, layer) for other in ordered)
        ]

    def group_owning(self, children: list[Layer]) -> Layer | None:
        """Group whose ``children`` list this is, or ``None`` for the top level."""
        for layer, _depth in self.walk():
            if layer.is_group and layer.children is children:
                return layer
        return None

    def is_ancestor(self, ancestor: Layer, layer: Layer | None) -> bool:
        if layer is None or not ancestor.is_group:
            return False
        stack = list(ancestor.children)
        while stack:
            item = stack.pop()
            if item is layer:
                return True
            if item.is_group:
                stack.extend(item.children)
        return False

    def node_count(self, layer: Layer) -> int:
        return 1 + sum(self.node_count(child) for child in layer.children)

    def move_layers(self, layers: list[Layer], target: list[Layer], index: int) -> list[Layer]:
        """Move nodes into ``target`` at ``index``; order kept, cycles refused."""
        parent = self.group_owning(target)
        moving: list[Layer] = []
        for layer in layers:
            if any(layer is item for item in moving):
                continue
            # never drop a group into itself or one of its own descendants
            if parent is not None and (layer is parent or self.is_ancestor(layer, parent)):
                continue
            moving.append(layer)
        if not moving:
            return []
        for layer in moving:
            located = self.locate(layer)
            if located is None:
                continue
            siblings, position = located
            if siblings is target and position < index:
                index -= 1
            del siblings[position]
        index = min(max(index, 0), len(target))
        target[index:index] = moving
        return moving

    def move_selection(self, layers: list[Layer], direction: int) -> list[Layer]:
        """Step the selected nodes one place; keeps a contiguous block together."""
        ordered = self.sort_layers(layers)
        if direction > 0:
            ordered = list(reversed(ordered))
        moved: list[Layer] = []
        for layer in ordered:
            if self.swap_with_sibling(layer, direction):
                moved.append(layer)
        return moved

    def insert_below(self, target: Layer, layer: Layer) -> int:
        """Insert ``layer`` right under ``target`` (same parent); return its walk index."""
        located = self.locate(target)
        if located is None:
            self.layers.append(layer)
        else:
            siblings, index = located
            siblings.insert(index, layer)
        return self.walk_index(layer)

    def insert_above(self, target: Layer, layer: Layer) -> int:
        """Insert ``layer`` right above ``target`` (same parent); return its walk index."""
        located = self.locate(target)
        if located is None:
            self.layers.append(layer)
        else:
            siblings, index = located
            siblings.insert(index + 1, layer)
        return self.walk_index(layer)

    def remove_layer(self, layer: Layer) -> bool:
        located = self.locate(layer)
        if located is None:
            return False
        siblings, index = located
        del siblings[index]
        return True

    def swap_with_sibling(self, layer: Layer, direction: int) -> bool:
        located = self.locate(layer)
        if located is None:
            return False
        siblings, index = located
        target = index + direction
        if not 0 <= target < len(siblings):
            return False
        siblings[index], siblings[target] = siblings[target], siblings[index]
        return True

    def make_group(self, name: str, children: list[Layer] | None = None) -> Layer:
        return Layer(name=name, kind="group", children=children or [])

    def group_layer(self, layer: Layer) -> Layer | None:
        """Wrap ``layer`` in a new group at its position."""
        located = self.locate(layer)
        if located is None:
            return None
        siblings, index = located
        group = self.make_group(f"Group {index + 1}", [layer])
        siblings[index] = group
        return group

    def group_layers(self, layers: list[Layer], name: str | None = None) -> Layer | None:
        """Wrap several layers in one group at the position of the topmost one."""
        ordered = self.sort_layers(layers)
        if not ordered:
            return None
        located = self.locate(ordered[0])
        if located is None:
            return None
        siblings, index = located
        group = self.make_group(name or f"Group {index + 1}")
        siblings.insert(index, group)
        self.move_layers(ordered, group.children, 0)
        return group

    def ungroup(self, group: Layer) -> Layer | None:
        """Dissolve a group: its children take its place; returns the first child."""
        located = self.locate(group)
        if located is None or not group.is_group:
            return None
        siblings, index = located
        siblings[index : index + 1] = group.children
        return group.children[0] if group.children else None

    def replace_layers(self, layers: list[Layer], replacement: Layer) -> int:
        """Remove nodes and put ``replacement`` where the first one was."""
        roots = self.selection_roots(layers)
        if not roots:
            return -1
        located = self.locate(roots[0])
        if located is None:
            return -1
        siblings, index = located
        insert_at = sum(1 for item in siblings[:index] if not any(item is root for root in roots))
        for layer in roots:
            self.remove_layer(layer)
        siblings.insert(insert_at, replacement)
        return self.walk_index(replacement)

    def _flatten_strokes(self, layer: Layer) -> list[Stroke] | None:
        """Strokes in draw order if the node can be flattened without rasterising."""
        if not layer.visible:
            return []
        if layer.opacity != 1.0 or layer.blend != "normal" or layer.clip != "off":
            return None
        if layer.is_group:
            strokes: list[Stroke] = []
            for child in layer.children:
                part = self._flatten_strokes(child)
                if part is None:
                    return None
                strokes.extend(part)
            return strokes
        if layer.kind != "vector":
            return None
        return list(layer.strokes)

    def merge_layers(self, layers: list[Layer], name: str | None = None) -> Layer | None:
        """Flatten selected nodes into one vector layer.

        Returns ``None`` when opacity, blending, clipping or raster content means
        the result has to be rendered to pixels instead.
        """
        roots = self.selection_roots(layers)
        if not roots or (len(roots) == 1 and not roots[0].is_group):
            return None
        strokes: list[Stroke] = []
        for layer in roots:
            part = self._flatten_strokes(layer)
            if part is None:
                return None
            strokes.extend(part)
        merged = Layer(name=name or roots[0].name, strokes=strokes)
        self.replace_layers(roots, merged)
        return merged

    def layer_by_uid(self, uid: str) -> Layer | None:
        for layer, _depth in self.walk():
            if layer.uid == uid:
                return layer
        return None

    def stroke_by_uid(self, uid: str) -> Stroke | None:
        for layer in self.all_layers():
            for stroke in layer.strokes:
                if stroke.uid == uid:
                    return stroke
        return None

    # --- json -----------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "width": self.width,
            "height": self.height,
            "active_layer": self.active_layer,
            "layers": [layer.to_dict() for layer in self.layers],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Document:
        layers = [Layer.from_dict(raw) for raw in data.get("layers", [])]
        if not layers:
            layers = [Layer("Layer 1")]
        return cls(
            width=int(data.get("width", 2000)),
            height=int(data.get("height", 2000)),
            layers=layers,
            active_layer=int(data.get("active_layer", 0)),
        )
