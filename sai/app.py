from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QAction, QActionGroup, QColor, QIcon, QKeySequence, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
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
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSlider,
    QSpinBox,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import io
from .canvas import Canvas
from .model import Document, Layer


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
        self._scale.setValue(2.0)
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

    def _build_layers_dock(self) -> None:
        dock = QDockWidget("Layers", self)
        dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetMovable)
        panel = QWidget(dock)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(6, 6, 6, 6)

        self._layer_list = QListWidget(panel)
        self._layer_list.currentRowChanged.connect(self._on_layer_selected)
        self._layer_list.itemChanged.connect(self._on_layer_item_changed)
        self._layer_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._layer_list.customContextMenuRequested.connect(self._layer_context_menu)
        layout.addWidget(self._layer_list, 1)

        buttons = QHBoxLayout()
        for label, slot in (
            ("+", self._add_layer),
            ("-", self._remove_layer),
            ("dup", self._duplicate_layer),
            ("^", lambda: self._move_layer(-1)),
            ("v", lambda: self._move_layer(1)),
        ):
            button = QPushButton(label, panel)
            button.setFixedWidth(36 if label == "dup" else 28)
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

        self._clip = QCheckBox("Clip to below", panel)
        self._clip.toggled.connect(self._on_clip_toggled)
        layout.addWidget(self._clip)

        mask_row = QHBoxLayout()
        mask_row.addWidget(QLabel("Mask", panel))
        self._mask_combo = QComboBox(panel)
        self._mask_combo.currentIndexChanged.connect(self._on_mask_selected)
        mask_row.addWidget(self._mask_combo, 1)
        layout.addLayout(mask_row)

        mask_buttons = QHBoxLayout()
        self._mask_button = QPushButton("Load mask...", panel)
        self._mask_button.clicked.connect(self._set_mask)
        self._clear_mask_button = QPushButton("Clear mask", panel)
        self._clear_mask_button.clicked.connect(self._clear_mask)
        mask_buttons.addWidget(self._mask_button)
        mask_buttons.addWidget(self._clear_mask_button)
        layout.addLayout(mask_buttons)

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
            "up": self._window_action("Move Up", lambda: self._move_layer(-1)),
            "down": self._window_action("Move Down", lambda: self._move_layer(1)),
            "clean_masks": self._window_action("Remove Unused Masks", self._remove_unused_masks),
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
            row = self._layer_list.row(item)
            if 0 <= row < len(self.canvas.doc.layers):
                self._layer_list.setCurrentRow(row)
            menu.addAction(self._layer_actions["duplicate"])
            menu.addAction(self._layer_actions["rename"])
            menu.addAction(self._layer_actions["delete"])
            menu.addSeparator()
            menu.addAction(self._layer_actions["up"])
            menu.addAction(self._layer_actions["down"])
            menu.addSeparator()
        menu.addAction(self._layer_actions["new"])
        menu.addAction(self._layer_actions["clean_masks"])
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

    def _refresh_layers(self) -> None:
        document = self.canvas.doc
        self._refreshing = True
        self._layer_list.clear()
        for index, layer in enumerate(document.layers):
            item = QListWidgetItem(layer.name)
            item.setFlags(
                item.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEditable
            )
            item.setCheckState(Qt.CheckState.Checked if layer.visible else Qt.CheckState.Unchecked)
            if layer.kind == "raster":
                item.setIcon(self._raster_icon)
                item.setToolTip("raster image layer")
            self._layer_list.addItem(item)
            if index == document.active_layer:
                self._layer_list.setCurrentRow(index)
        layer = document.layer()
        self._opacity.setValue(round(layer.opacity * 100))
        self._blend.setCurrentText(layer.blend)
        self._clip.setChecked(layer.clip_to_below)
        self._clip.setEnabled(document.active_layer > 0)
        self._refresh_masks(layer)
        self._clear_mask_button.setEnabled(layer.mask_id is not None)
        self._refreshing = False
        self.canvas.update()

    def _refresh_masks(self, layer: Layer) -> None:
        self._mask_combo.clear()
        self._mask_combo.addItem("(none)", None)
        for mask_id, mask in self.canvas.doc.masks.items():
            shared = sum(1 for item in self.canvas.doc.layers if item.mask_id == mask_id)
            self._mask_combo.addItem(f"{mask.name} ({shared})", mask_id)
        index = self._mask_combo.findData(layer.mask_id)
        self._mask_combo.setCurrentIndex(max(0, index))

    def _make_raster_swatch(self) -> QPixmap:
        pixmap = QPixmap(10, 10)
        pixmap.fill(QColor(120, 150, 190))
        return pixmap

    def _on_layer_selected(self, row: int) -> None:
        if self._refreshing or row < 0:
            return
        if self.canvas.transform_mode:
            self.canvas.commit_transform()
        self.canvas.doc.active_layer = row
        self.canvas.clear_hover()
        self.canvas.clear_selection()
        self._refresh_layers()

    def _on_layer_item_changed(self, item: QListWidgetItem) -> None:
        if self._refreshing:
            return
        row = self._layer_list.row(item)
        if not 0 <= row < len(self.canvas.doc.layers):
            return
        layer = self.canvas.doc.layers[row]
        visible = item.checkState() == Qt.CheckState.Checked
        if visible != layer.visible:
            layer.visible = visible
            self.canvas.clear_hover()
            self.canvas.update()
        name = item.text().strip()
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

    def _on_clip_toggled(self, checked: bool) -> None:
        if self._refreshing:
            return
        self.canvas.doc.layer().clip_to_below = checked
        self.canvas.update()

    def _set_mask(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Layer mask",
            "",
            "Images (*.png *.jpg *.jpeg *.webp *.bmp)",
        )
        if not path:
            return
        try:
            source = io.load_raster_layer(path, "mask")
        except OSError as error:
            QMessageBox.critical(self, "Mask failed", str(error))
            return
        document = self.canvas.doc
        self.canvas.push_undo()
        mask_id = document.add_mask(Path(path).stem, source.image_base64 or "")
        document.layer().mask_id = mask_id
        self._refresh_layers()

    def _on_mask_selected(self, index: int) -> None:
        if self._refreshing or index < 0:
            return
        document = self.canvas.doc
        layer = document.layer()
        mask_id = self._mask_combo.itemData(index)
        selected = mask_id if isinstance(mask_id, str) else None
        if selected == layer.mask_id:
            return
        self.canvas.push_undo()
        layer.mask_id = selected
        self._refresh_layers()

    def _clear_mask(self) -> None:
        layer = self.canvas.doc.layer()
        if not layer.mask_id:
            return
        self.canvas.push_undo()
        layer.mask_id = None
        self._refresh_layers()

    def _remove_unused_masks(self) -> None:
        document = self.canvas.doc
        used = {layer.mask_id for layer in document.layers if layer.mask_id}
        if not any(mask_id not in used for mask_id in document.masks):
            self.statusBar().showMessage("no unused masks")
            return
        self.canvas.push_undo()
        removed = document.remove_unused_masks()
        self._refresh_layers()
        self.statusBar().showMessage(f"removed {removed} unused mask(s)")

    def _add_layer(self) -> None:
        document = self.canvas.doc
        self.canvas.push_undo()
        index = document.active_layer + 1
        document.layers.insert(index, Layer(f"Layer {len(document.layers) + 1}"))
        document.active_layer = index
        self._refresh_layers()

    def _duplicate_layer(self) -> None:
        document = self.canvas.doc
        self.canvas.push_undo()
        source = document.layer()
        duplicate = source.copy()
        duplicate.name = f"{source.name} copy"
        index = document.active_layer + 1
        document.layers.insert(index, duplicate)
        document.active_layer = index
        self._refresh_layers()

    def _rename_layer(self) -> None:
        item = self._layer_list.currentItem()
        if item is not None:
            self._layer_list.editItem(item)

    def _remove_layer(self) -> None:
        document = self.canvas.doc
        if len(document.layers) <= 1:
            return
        self.canvas.push_undo()
        del document.layers[document.active_layer]
        document.active_layer = min(document.active_layer, len(document.layers) - 1)
        self._refresh_layers()

    def _move_layer(self, direction: int) -> None:
        document = self.canvas.doc
        index = document.active_layer
        target = index + direction
        if not 0 <= target < len(document.layers):
            return
        self.canvas.push_undo()
        document.layers[index], document.layers[target] = (
            document.layers[target],
            document.layers[index],
        )
        document.active_layer = target
        self._refresh_layers()

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
        index = document.active_layer + 1
        document.layers.insert(index, layer)
        document.active_layer = index
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
