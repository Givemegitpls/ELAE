from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QAction, QActionGroup, QColor, QIcon, QKeySequence, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDockWidget,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSlider,
    QSpinBox,
    QToolBar,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import io
from .canvas import Canvas
from .model import Document, Layer


class LayerTree(QTreeWidget):
    """Layer tree; drops are delegated to the window, the model stays the source of truth."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setHeaderHidden(True)
        self.setRootIsDecorated(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.document: Document | None = None
        self.on_drop: Callable[[list[Layer], int], bool] | None = None
        # Shift-range anchor: the layer of the last plain/Ctrl click.
        self.anchor: Layer | None = None

    def item_layer(self, item: QTreeWidgetItem | None) -> Layer | None:
        if item is None:
            return None
        layer = item.data(0, Qt.ItemDataRole.UserRole)
        return layer if isinstance(layer, Layer) else None

    def ordered_items(self) -> list[QTreeWidgetItem]:
        """Items in draw order (same walk as Document.walk)."""
        items: list[QTreeWidgetItem] = []

        def visit(parent: QTreeWidgetItem) -> None:
            for index in range(parent.childCount()):
                child = parent.child(index)
                if child is None:
                    continue
                items.append(child)
                visit(child)

        for index in range(self.topLevelItemCount()):
            item = self.topLevelItem(index)
            if item is None:
                continue
            items.append(item)
            visit(item)
        return items

    def select_range(self, start: Layer, end: Layer) -> None:
        """Select every item between two layers in tree order, end becomes current."""
        items = self.ordered_items()
        layers = [self.item_layer(item) for item in items]
        try:
            first = next(index for index, layer in enumerate(layers) if layer is start)
            last = next(index for index, layer in enumerate(layers) if layer is end)
        except StopIteration:
            return
        end_item = next(item for item, layer in zip(items, layers) if layer is end)
        if first > last:
            first, last = last, first
        # Current first: it drives the active layer (and must be the clicked end).
        self.setCurrentItem(end_item)
        self.clearSelection()
        for item in items[first : last + 1]:
            item.setSelected(True)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            layer = self.item_layer(self.itemAt(event.position().toPoint()))
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier and layer is not None:
                self.select_range(self.anchor or layer, layer)
                event.accept()
                return
            if layer is not None:
                self.anchor = layer
        super().mousePressEvent(event)

    def drop_target(
        self,
        position: QPoint,
        indicator: QAbstractItemView.DropIndicatorPosition | None = None,
    ) -> tuple[list[Layer], int] | None:
        """Sibling list and position a drop at ``position`` means."""
        document = self.document
        if document is None:
            return None
        item = self.itemAt(position)
        if indicator is None:
            indicator = self.dropIndicatorPosition()
        if item is None:
            return document.layers, len(document.layers)
        layer = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(layer, Layer):
            return None
        if layer.is_group and indicator == QAbstractItemView.DropIndicatorPosition.OnItem:
            return layer.children, len(layer.children)
        located = document.locate(layer)
        if located is None:
            return None
        siblings, index = located
        if indicator == QAbstractItemView.DropIndicatorPosition.AboveItem:
            return siblings, index
        return siblings, index + 1

    def dropEvent(self, event) -> None:
        target = self.drop_target(event.position().toPoint())
        handler = self.on_drop
        if target is None or handler is None:
            event.ignore()
            return
        if handler(*target):
            event.accept()
        else:
            event.ignore()


class CanvasSizeDialog(QDialog):
    def __init__(self, width: int, height: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Canvas size")
        self._width = QSpinBox(self)
        self._width.setRange(16, 20000)
        self._width.setValue(width)
        self._height = QSpinBox(self)
        self._height.setRange(16, 20000)
        self._height.setValue(height)

        form = QFormLayout(self)
        form.addRow("Width", self._width)
        form.addRow("Height", self._height)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def canvas_size(self) -> tuple[int, int]:
        return self._width.value(), self._height.value()


class PngExportDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Export PNG")
        self._scale = QDoubleSpinBox(self)
        self._scale.setRange(0.5, 8.0)
        self._scale.setSingleStep(0.5)
        self._scale.setValue(1.0)
        self._background = QComboBox(self)
        self._background.addItems(["transparent", "white"])

        form = QFormLayout(self)
        form.addRow("Scale", self._scale)
        form.addRow("Background", self._background)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def options(self) -> tuple[float, QColor | None]:
        background = None if self._background.currentIndex() == 0 else QColor("white")
        return self._scale.value(), background


class TransformValuesDialog(QDialog):
    def __init__(
        self,
        angle: float,
        scale_x: float,
        scale_y: float,
        offset_x: float,
        offset_y: float,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Transform values")

        self._angle = QDoubleSpinBox(self)
        self._angle.setRange(-360.0, 360.0)
        self._angle.setDecimals(1)
        self._angle.setValue(angle)

        self._scale_x = QDoubleSpinBox(self)
        self._scale_x.setRange(1.0, 2000.0)
        self._scale_x.setSuffix(" %")
        self._scale_x.setValue(scale_x * 100.0)

        self._scale_y = QDoubleSpinBox(self)
        self._scale_y.setRange(1.0, 2000.0)
        self._scale_y.setSuffix(" %")
        self._scale_y.setValue(scale_y * 100.0)

        self._offset_x = QDoubleSpinBox(self)
        self._offset_x.setRange(-10000.0, 10000.0)
        self._offset_x.setValue(offset_x)

        self._offset_y = QDoubleSpinBox(self)
        self._offset_y.setRange(-10000.0, 10000.0)
        self._offset_y.setValue(offset_y)

        form = QFormLayout(self)
        form.addRow("Angle", self._angle)
        form.addRow("Scale X", self._scale_x)
        form.addRow("Scale Y", self._scale_y)
        form.addRow("Offset X", self._offset_x)
        form.addRow("Offset Y", self._offset_y)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def values(self) -> tuple[float, float, float, float, float]:
        return (
            self._angle.value(),
            self._scale_x.value() / 100.0,
            self._scale_y.value() / 100.0,
            self._offset_x.value(),
            self._offset_y.value(),
        )


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("sai-lineart")
        self.canvas = Canvas()
        self.setCentralWidget(self.canvas)
        self._path: Path | None = None
        self._refreshing = False

        self._build_toolbar()
        self._build_layers_dock()
        self._layer_actions = self._build_layer_actions()
        self._build_menus()

        self.canvas.documentChanged.connect(self._on_document_changed)
        self.canvas.statusMessage.connect(self.statusBar().showMessage)
        self.canvas.transformValuesRequested.connect(self._edit_transform_values)
        self.statusBar().showMessage(
            "Click = smooth point, Ctrl+Click = sharp corner. "
            "Right click / Enter finishes. Ctrl+wheel zooms."
        )
        self._refresh_layers()

    # --- ui -------------------------------------------------------------
    def _build_toolbar(self) -> None:
        bar = QToolBar("Tools", self)
        bar.setMovable(False)
        self.addToolBar(bar)

        group = QActionGroup(self)
        group.setExclusive(True)
        tools = [
            ("Draw", Canvas.TOOL_DRAW, "1"),
            ("Edit", Canvas.TOOL_EDIT, "2"),
            ("Erase", Canvas.TOOL_ERASE, "3"),
            ("Width", Canvas.TOOL_WIDTH, "4"),
            ("Fill", Canvas.TOOL_FILL, "5"),
            ("Recolor", Canvas.TOOL_RECOLOR, "6"),
        ]
        self._tool_actions: dict[str, QAction] = {}
        for name, tool, shortcut in tools:
            action = QAction(name, self)
            action.setCheckable(True)
            action.setShortcut(QKeySequence(shortcut))
            action.triggered.connect(lambda _=False, t=tool: self._set_tool(t))
            group.addAction(action)
            bar.addAction(action)
            self._tool_actions[tool] = action
        self._tool_actions[Canvas.TOOL_DRAW].setChecked(True)

        bar.addSeparator()
        bar.addWidget(QLabel(" width "))
        self._width_spin = QDoubleSpinBox(self)
        self._width_spin.setRange(0.0, 200.0)
        self._width_spin.setSingleStep(1.0)
        self._width_spin.setValue(self.canvas.brush_width)
        self._width_spin.valueChanged.connect(self._set_brush_width)
        bar.addWidget(self._width_spin)

        bar.addWidget(QLabel(" color "))
        self._color_button = QToolButton(self)
        self._color_button.setFixedSize(24, 24)
        self._color_button.clicked.connect(self._pick_color)
        bar.addWidget(self._color_button)
        self._update_color_swatch()

        bar.addWidget(QLabel(" alpha "))
        self._alpha_slider = QSlider(Qt.Orientation.Horizontal, self)
        self._alpha_slider.setRange(0, 255)
        self._alpha_slider.setValue(self.canvas.alpha)
        self._alpha_slider.setFixedWidth(110)
        self._alpha_slider.valueChanged.connect(self._set_alpha)
        bar.addWidget(self._alpha_slider)

        bar.addSeparator()
        self._scope_action = QAction("Scope: layer", self)
        self._scope_action.setCheckable(True)
        self._scope_action.setToolTip("Edit and fill: active layer or the whole image")
        self._scope_action.setShortcut(QKeySequence("Shift+S"))
        self._scope_action.toggled.connect(self._on_scope_toggled)
        bar.addAction(self._scope_action)

    def _build_layers_dock(self) -> None:
        dock = QDockWidget("Layers", self)
        dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetMovable)
        panel = QWidget(dock)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(6, 6, 6, 6)

        self._layer_list = LayerTree(panel)
        self._layer_list.on_drop = self._drop_layers
        self._layer_list.currentItemChanged.connect(self._on_layer_selected)
        self._layer_list.itemChanged.connect(self._on_layer_item_changed)
        self._layer_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._layer_list.customContextMenuRequested.connect(self._layer_context_menu)
        layout.addWidget(self._layer_list, 1)

        buttons = QHBoxLayout()
        for label, slot in (
            ("+", self._add_layer),
            ("-", self._remove_layer),
            ("dup", self._duplicate_layer),
            ("grp", self._group_layer),
            ("ung", self._ungroup_layer),
            ("^", lambda: self._move_layer(-1)),
            ("v", lambda: self._move_layer(1)),
        ):
            button = QPushButton(label, panel)
            button.setFixedWidth(36 if label in ("dup", "grp", "ung") else 28)
            button.clicked.connect(slot)
            buttons.addWidget(button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        opacity_row = QHBoxLayout()
        opacity_row.addWidget(QLabel("Opacity", panel))
        self._opacity = QSlider(Qt.Orientation.Horizontal, panel)
        self._opacity.setRange(0, 100)
        self._opacity.setValue(100)
        self._opacity.valueChanged.connect(self._on_opacity_changed)
        opacity_row.addWidget(self._opacity, 1)
        layout.addLayout(opacity_row)

        blend_row = QHBoxLayout()
        blend_row.addWidget(QLabel("Blend", panel))
        self._blend = QComboBox(panel)
        self._blend.addItems(["normal", "multiply"])
        self._blend.currentTextChanged.connect(self._on_blend_changed)
        blend_row.addWidget(self._blend, 1)
        layout.addLayout(blend_row)

        clip_row = QHBoxLayout()
        clip_row.addWidget(QLabel("Clip to below", panel))
        self._clip = QComboBox(panel)
        self._clip.addItems(["off", "inside", "outside"])
        self._clip.currentTextChanged.connect(self._on_clip_changed)
        clip_row.addWidget(self._clip, 1)
        layout.addLayout(clip_row)

        self._raster_icon = QIcon(self._make_raster_swatch())

        dock.setWidget(panel)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)

    def _build_menus(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        self._add_action(file_menu, "New", self._new_document, QKeySequence.StandardKey.New)
        self._add_action(file_menu, "Open...", self._open_document, QKeySequence.StandardKey.Open)
        self._add_action(file_menu, "Save", self._save_document, QKeySequence.StandardKey.Save)
        self._add_action(
            file_menu, "Save As...", self._save_document_as, QKeySequence.StandardKey.SaveAs
        )
        file_menu.addSeparator()
        self._add_action(file_menu, "Import PNG as Layer...", self._import_raster)
        self._add_action(file_menu, "Export PNG...", self._export_png)
        self._add_action(file_menu, "Export SVG...", self._export_svg)
        file_menu.addSeparator()
        self._add_action(file_menu, "Canvas Size...", self._canvas_size)
        self._add_action(
            file_menu,
            "Transform Layer...",
            self._transform_layer,
            QKeySequence("Ctrl+T"),
        )
        file_menu.addSeparator()
        self._add_action(file_menu, "Quit", self.close, QKeySequence.StandardKey.Quit)

        edit_menu = self.menuBar().addMenu("&Edit")
        self._add_action(edit_menu, "Undo", self.canvas.undo, QKeySequence.StandardKey.Undo)
        self._add_action(edit_menu, "Redo", self.canvas.redo, QKeySequence.StandardKey.Redo)
        edit_menu.addSeparator()
        self._add_action(
            edit_menu, "Merge Selected Points", self._merge_selected, QKeySequence("M")
        )
        self._add_action(edit_menu, "Close Stroke", self._close_stroke, QKeySequence("Shift+M"))
        self._add_action(edit_menu, "Delete Selected Points", self._delete_selected)
        self._add_action(
            edit_menu, "Deselect All", self._deselect_all, QKeySequence("Ctrl+Shift+A")
        )
        edit_menu.addSeparator()
        self._add_action(edit_menu, "Convert Vector to Fill", self._convert_to_fill)

        view_menu = self.menuBar().addMenu("&View")
        self._add_action(
            view_menu,
            "Rotate View Left 90°",
            lambda: self._rotate_view(-90.0),
            QKeySequence("Alt+["),
        )
        self._add_action(
            view_menu,
            "Rotate View Right 90°",
            lambda: self._rotate_view(90.0),
            QKeySequence("Alt+]"),
        )
        self._add_action(
            view_menu, "Reset View Rotation", self._reset_view_rotation, QKeySequence("Alt+0")
        )

    def _build_layer_actions(self) -> dict[str, QAction]:
        """Layer actions live in the list's context menu, so they go on the window."""
        return {
            "new": self._window_action("New Layer", self._add_layer),
            "duplicate": self._window_action(
                "Duplicate Layer", self._duplicate_layer, QKeySequence("Ctrl+J")
            ),
            "rename": self._window_action(
                "Rename Layer...", self._rename_layer, QKeySequence("F2")
            ),
            "delete": self._window_action("Delete Layer", self._remove_layer),
            "group": self._window_action("Group Layer", self._group_layer, QKeySequence("Ctrl+G")),
            "ungroup": self._window_action(
                "Ungroup", self._ungroup_layer, QKeySequence("Ctrl+Shift+G")
            ),
            "up": self._window_action("Move Up", lambda: self._move_layer(-1)),
            "down": self._window_action("Move Down", lambda: self._move_layer(1)),
        }

    def _window_action(
        self,
        text: str,
        slot,
        shortcut: QKeySequence | QKeySequence.StandardKey | None = None,
    ) -> QAction:
        action = QAction(text, self)
        if shortcut is not None:
            action.setShortcut(shortcut)
        action.triggered.connect(slot)
        self.addAction(action)
        return action

    def _layer_context_menu(self, position: QPoint) -> None:
        item = self._layer_list.itemAt(position)
        menu = QMenu(self._layer_list)
        if item is not None:
            if not item.isSelected():
                self._layer_list.clearSelection()
                item.setSelected(True)
            self._layer_list.setCurrentItem(item)
            menu.addAction(self._layer_actions["duplicate"])
            menu.addAction(self._layer_actions["rename"])
            menu.addAction(self._layer_actions["delete"])
            menu.addSeparator()
            menu.addAction(self._layer_actions["group"])
            menu.addAction(self._layer_actions["ungroup"])
            menu.addAction(self._layer_actions["up"])
            menu.addAction(self._layer_actions["down"])
            menu.addSeparator()
        menu.addAction(self._layer_actions["new"])
        menu.exec(self._layer_list.viewport().mapToGlobal(position))

    def _add_action(
        self,
        menu,
        text: str,
        slot,
        shortcut: QKeySequence | QKeySequence.StandardKey | None = None,
    ) -> QAction:
        action = QAction(text, self)
        if shortcut is not None:
            action.setShortcut(shortcut)
        action.triggered.connect(slot)
        menu.addAction(action)
        return action

    # --- state ----------------------------------------------------------
    def _set_tool(self, tool: str) -> None:
        self.canvas.commit_transform()
        self.canvas.tool = tool
        self.canvas.update()
        self.canvas.setFocus()
        self.statusBar().showMessage(f"tool: {tool}")

    def _set_brush_width(self, value: float) -> None:
        self.canvas.brush_width = value

    def _pick_color(self) -> None:
        current = QColor(*self.canvas.color)
        current.setAlpha(self.canvas.alpha)
        chosen = QColorDialog.getColor(
            current,
            self,
            "Stroke color",
            QColorDialog.ColorDialogOption.ShowAlphaChannel,
        )
        if not chosen.isValid():
            return
        self.canvas.color = (chosen.red(), chosen.green(), chosen.blue())
        self.canvas.alpha = chosen.alpha()
        self._alpha_slider.setValue(chosen.alpha())
        self._update_color_swatch()

    def _set_alpha(self, value: int) -> None:
        self.canvas.alpha = value
        self._update_color_swatch()

    def _update_color_swatch(self) -> None:
        red, green, blue = self.canvas.color
        self._color_button.setStyleSheet(
            f"background-color: rgb({red},{green},{blue}); border: 1px solid #555;"
        )
        self._color_button.setToolTip(f"#{red:02x}{green:02x}{blue:02x}, alpha {self.canvas.alpha}")

    def _on_document_changed(self) -> None:
        self._refresh_layers()

    @staticmethod
    def _item_layer(item: QTreeWidgetItem | None) -> Layer | None:
        if item is None:
            return None
        layer = item.data(0, Qt.ItemDataRole.UserRole)
        return layer if isinstance(layer, Layer) else None

    def _selected_layers(self) -> list[Layer]:
        layers = [
            layer
            for layer in (self._item_layer(item) for item in self._layer_list.selectedItems())
            if layer is not None
        ]
        return self.canvas.doc.sort_layers(layers)

    def _refresh_layers(self, selection: list[Layer] | None = None) -> None:
        document = self.canvas.doc
        tree = self._layer_list
        tree.document = document
        if selection is None:
            selection = self._selected_layers()
        self._refreshing = True
        tree.clear()
        self._layer_items: dict[int, QTreeWidgetItem] = {}

        def add(items: list[Layer], parent: QTreeWidgetItem | None) -> None:
            for layer in items:
                item = QTreeWidgetItem([layer.name])
                item.setFlags(
                    item.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEditable
                )
                item.setCheckState(
                    0, Qt.CheckState.Checked if layer.visible else Qt.CheckState.Unchecked
                )
                item.setData(0, Qt.ItemDataRole.UserRole, layer)
                if layer.kind == "raster":
                    item.setIcon(0, self._raster_icon)
                    item.setToolTip(0, "raster image layer")
                elif layer.is_group:
                    item.setToolTip(0, "group")
                if parent is None:
                    self._layer_list.addTopLevelItem(item)
                else:
                    parent.addChild(item)
                self._layer_items[id(layer)] = item
                if layer.is_group:
                    add(layer.children, item)
                    item.setExpanded(True)

        add(document.layers, None)
        tree.expandAll()

        for layer in selection:
            item = self._layer_items.get(id(layer))
            if item is not None:
                item.setSelected(True)
        active = document.layer()
        item = self._layer_items.get(id(active))
        if item is not None:
            tree.setCurrentItem(item)
        if tree.anchor is None or id(tree.anchor) not in self._layer_items:
            tree.anchor = active
        self._refresh_layer_properties()
        self._refreshing = False
        self.canvas.update()

    def _refresh_layer_properties(self) -> None:
        """Opacity/blend/clip rows for the active layer, without rebuilding the tree."""
        document = self.canvas.doc
        active = document.layer()
        was_refreshing = self._refreshing
        self._refreshing = True
        self._opacity.setValue(round(active.opacity * 100))
        self._blend.setCurrentText(active.blend)
        self._clip.setCurrentText(active.clip)
        located = document.locate(active)
        self._clip.setEnabled(located is not None and located[1] > 0)
        self._refreshing = was_refreshing

    def _make_raster_swatch(self) -> QPixmap:
        pixmap = QPixmap(10, 10)
        pixmap.fill(QColor(120, 150, 190))
        return pixmap

    def _on_layer_selected(
        self, current: QTreeWidgetItem | None, _previous: QTreeWidgetItem | None = None
    ) -> None:
        if self._refreshing or current is None:
            return
        layer = current.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(layer, Layer):
            return
        index = self.canvas.doc.walk_index(layer)
        if index < 0:
            return
        if self.canvas.transform_mode:
            self.canvas.commit_transform()
        self.canvas.doc.active_layer = index
        self.canvas.clear_hover()
        self.canvas.clear_selection()
        self._refresh_layer_properties()

    def _on_layer_item_changed(self, item: QTreeWidgetItem, _column: int) -> None:
        if self._refreshing:
            return
        layer = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(layer, Layer):
            return
        visible = item.checkState(0) == Qt.CheckState.Checked
        if visible != layer.visible:
            layer.visible = visible
            self.canvas.clear_hover()
            self.canvas.update()
        name = item.text(0).strip()
        if name and name != layer.name:
            self.canvas.push_undo()
            layer.name = name
            self._refresh_layers()

    def _on_opacity_changed(self, value: int) -> None:
        if self._refreshing:
            return
        self.canvas.doc.layer().opacity = value / 100.0
        self.canvas.update()

    def _on_blend_changed(self, value: str) -> None:
        if self._refreshing:
            return
        self.canvas.doc.layer().blend = value
        self.canvas.update()

    def _on_clip_changed(self, value: str) -> None:
        if self._refreshing:
            return
        self.canvas.doc.layer().clip = value
        self.canvas.update()

    def _add_layer(self) -> None:
        document = self.canvas.doc
        self.canvas.push_undo()
        layer = Layer(f"Layer {len(document.walk()) + 1}")
        document.active_layer = document.insert_above(document.layer(), layer)
        self._refresh_layers()

    def _duplicate_layer(self) -> None:
        document = self.canvas.doc
        layers = self._selected_layers()
        if not layers:
            return
        self.canvas.push_undo()
        copies: list[Layer] = []
        for layer in layers:
            duplicate = layer.duplicate()
            duplicate.name = f"{layer.name} copy"
            document.insert_above(layer, duplicate)
            copies.append(duplicate)
        document.active_layer = document.walk_index(copies[0])
        self._refresh_layers(copies)

    def _group_layer(self) -> None:
        document = self.canvas.doc
        layers = self._selected_layers()
        if not layers:
            self.statusBar().showMessage("select layers to group")
            return
        self.canvas.push_undo()
        group = document.group_layers(layers)
        if group is not None:
            document.active_layer = document.walk_index(group)
        self._refresh_layers([group] if group is not None else [])

    def _ungroup_layer(self) -> None:
        document = self.canvas.doc
        groups = [layer for layer in self._selected_layers() if layer.is_group]
        if not groups:
            self.statusBar().showMessage("select a group to ungroup")
            return
        self.canvas.push_undo()
        released: list[Layer] = []
        for group in groups:
            members = list(group.children)
            document.ungroup(group)
            released.extend(members)
        if released:
            document.active_layer = document.walk_index(released[0])
        self._refresh_layers(released)

    def _drop_layers(self, siblings: list[Layer], index: int) -> bool:
        """Drag&drop from the tree: move the selection into ``siblings`` at ``index``."""
        document = self.canvas.doc
        layers = self._selected_layers()
        if not layers:
            return False
        active = document.layer()
        snapshot = document.copy()
        moved = document.move_layers(layers, siblings, index)
        if not moved:
            return False
        self.canvas.push_undo_snapshot(snapshot)
        document.active_layer = document.walk_index(active)
        self.canvas.clear_hover()
        self._refresh_layers(moved)
        self.statusBar().showMessage(f"moved {len(moved)} layer(s)")
        return True

    def _rename_layer(self) -> None:
        item = self._layer_list.currentItem()
        if item is not None:
            self._layer_list.editItem(item)

    def _remove_layer(self) -> None:
        document = self.canvas.doc
        layers = self._selected_layers()
        if not layers:
            return
        removable = sum(document.node_count(layer) for layer in layers)
        if len(document.walk()) - removable <= 0:
            self.statusBar().showMessage("a document needs at least one layer")
            return
        self.canvas.push_undo()
        for layer in layers:
            document.remove_layer(layer)
        document.active_layer = min(document.active_layer, len(document.walk()) - 1)
        self._refresh_layers([])

    def _move_layer(self, direction: int) -> None:
        document = self.canvas.doc
        layers = self._selected_layers()
        if not layers:
            return
        active = document.layer()
        snapshot = document.copy()
        if not document.move_selection(layers, direction):
            return
        self.canvas.push_undo_snapshot(snapshot)
        document.active_layer = document.walk_index(active)
        self._refresh_layers(layers)

    def _rotate_view(self, degrees: float) -> None:
        angle = self.canvas.rotate_view(degrees)
        self.statusBar().showMessage(f"view rotation {angle:.0f}° (export keeps its orientation)")

    def _reset_view_rotation(self) -> None:
        self.canvas.reset_rotation()
        self.statusBar().showMessage("view rotation reset")

    # --- files ----------------------------------------------------------
    def _suggested_save_path(self, suffix: str) -> str:
        """Default file name for save/export dialogs.

        Qt forwards it to the portal as ``current_name``; without it the
        portal suggests ``termfilechooser.tmp`` and that is what lands on disk.
        """
        if self._path is not None:
            return str(self._path.with_suffix(suffix))
        return str(Path.home() / f"drawing{suffix}")

    def _new_document(self) -> None:
        self.canvas.set_document(Document())
        self._path = None
        self._refresh_layers()

    def _open_document(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open", "", "sai-lineart (*.json)")
        if not path:
            return
        try:
            document = io.load_document(path)
        except (OSError, ValueError) as error:
            QMessageBox.critical(
                self,
                "Open failed",
                f"{error}\n\nДля картинок: File > Import PNG as Layer.",
            )
            return
        self.canvas.set_document(document)
        self._path = Path(path)
        self._refresh_layers()

    def _save_document(self) -> None:
        if self._path is None:
            self._save_document_as()
            return
        self._write_document(self._path)

    def _save_document_as(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Save", self._suggested_save_path(".json"), "sai-lineart (*.json)"
        )
        if not path:
            return
        self._path = io.ensure_suffix(path, ".json")
        self._write_document(self._path)

    def _write_document(self, path: Path) -> None:
        try:
            io.save_document(self.canvas.doc, path)
        except OSError as error:
            QMessageBox.critical(self, "Save failed", str(error))
            return
        self.statusBar().showMessage(f"saved {path}")

    def _export_png(self) -> None:
        dialog = PngExportDialog(self)
        if not dialog.exec():
            return
        scale, background = dialog.options()
        path, _ = QFileDialog.getSaveFileName(
            self, "Export PNG", self._suggested_save_path(".png"), "PNG (*.png)"
        )
        if not path:
            return
        target = io.ensure_suffix(path, ".png")
        try:
            io.export_png(self.canvas.doc, target, scale=scale, background=background)
        except OSError as error:
            QMessageBox.critical(self, "Export failed", str(error))
            return
        self.statusBar().showMessage(f"exported {target}")

    def _import_raster(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Import image",
            "",
            "Images (*.png *.jpg *.jpeg *.webp *.bmp)",
        )
        if not path:
            return
        try:
            layer = io.load_raster_layer(path)
        except OSError as error:
            QMessageBox.critical(self, "Import failed", str(error))
            return
        document = self.canvas.doc
        self.canvas.push_undo()
        document.active_layer = document.insert_above(document.layer(), layer)
        self.canvas.documentChanged.emit()
        self.canvas.update()

    def _canvas_size(self) -> None:
        dialog = CanvasSizeDialog(self.canvas.doc.width, self.canvas.doc.height, self)
        if not dialog.exec():
            return
        width, height = dialog.canvas_size()
        self.canvas.push_undo()
        self.canvas.doc.width = width
        self.canvas.doc.height = height
        self.canvas.update()

    # --- edit -----------------------------------------------------------
    def _merge_selected(self) -> None:
        self.canvas.setFocus()
        self.canvas.merge_selected()

    def _close_stroke(self) -> None:
        self.canvas.setFocus()
        self.canvas.close_selected_strokes()

    def _delete_selected(self) -> None:
        self.canvas.setFocus()
        self.canvas.delete_selected()

    def _deselect_all(self) -> None:
        self.canvas.setFocus()
        self.canvas.clear_selection()

    def _on_scope_toggled(self, checked: bool) -> None:
        self.canvas.set_scope("image" if checked else "layer")
        self._scope_action.setText("Scope: image" if checked else "Scope: layer")

    def _convert_to_fill(self) -> None:
        self.canvas.setFocus()
        if self.canvas.convert_to_fill():
            self._refresh_layers()

    def _transform_layer(self) -> None:
        self.canvas.begin_transform()
        self.canvas.setFocus()

    def _edit_transform_values(self) -> None:
        if not self.canvas.transform_mode:
            return
        angle, scale_x, scale_y, offset_x, offset_y = self.canvas.transform_parameters()
        dialog = TransformValuesDialog(angle, scale_x, scale_y, offset_x, offset_y, self)
        if dialog.exec():
            self.canvas.set_transform_values(*dialog.values())

    def _export_svg(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Export SVG", self._suggested_save_path(".svg"), "SVG (*.svg)"
        )
        if not path:
            return
        target = io.ensure_suffix(path, ".svg")
        try:
            io.export_svg(self.canvas.doc, target)
        except OSError as error:
            QMessageBox.critical(self, "Export failed", str(error))
            return
        self.statusBar().showMessage(f"exported {target}")
