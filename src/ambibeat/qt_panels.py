"""Qt slide-out visual effects and settings panels."""

import os
import re
import sys

from PySide6.QtCore import QPointF, QSettings, QSignalBlocker, QSize, QStandardPaths, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSlider,
    QStackedWidget,
    QStyle,
    QStyleOptionButton,
    QVBoxLayout,
    QWidget,
)

from .backend import (
    CUSTOM_PALETTE_LIMIT,
    SOURCE_PROMPT_SECONDS_DEFAULT,
    SOURCE_PROMPT_SECONDS_MAX,
    SOURCE_PROMPT_SECONDS_MIN,
    normalize_custom_palettes,
)

LIGHTING_COLOR_OPTIONS = (
    ("artwork", "Artwork colours"), ("rainbow", "Rainbow"),
    ("christmas", "Christmas"), ("gelato", "Gelato"),
)


def source_icon(source):
    entry = source.get("desktopEntry", "").removesuffix(".desktop")
    candidates = []
    if re.fullmatch(r"[A-Za-z0-9._-]+", entry):
        path = QStandardPaths.locate(QStandardPaths.StandardLocation.ApplicationsLocation,
                                     entry + ".desktop")
        if path:
            settings = QSettings(path, QSettings.Format.IniFormat)
            icon_name = settings.value("Desktop Entry/Icon", "", type=str)
            if settings.status() != QSettings.Status.NoError:
                print(f"Desktop entry could not be read: {path}", file=sys.stderr)
            elif icon_name:
                candidates.append(icon_name)
        candidates.append(entry)
    identity = source.get("identity", "").strip().casefold().replace(" ", "-")
    if identity:
        candidates.append(identity)
    candidates.append("multimedia-player")
    for candidate in candidates:
        icon = QIcon(candidate) if os.path.isabs(candidate) else QIcon.fromTheme(candidate)
        if not icon.isNull():
            return icon
    return QIcon()


class WheelIgnoringSlider(QSlider):
    def wheelEvent(self, event):
        event.ignore()


class SnappingSlider(WheelIgnoringSlider):
    def __init__(self, orientation, snap_interval):
        super().__init__(orientation)
        self.snap_interval = snap_interval
        self.snap_drag = True
        self.sliderMoved.connect(self.snap_position)
        if snap_interval > 1:
            help_text = f"Drag snaps to multiples of {snap_interval}; hold Shift or use arrow keys for fine adjustment."
            self.setToolTip(help_text)
            self.setAccessibleDescription(help_text)

    def mousePressEvent(self, event):
        self.snap_drag = not bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        self.snap_drag = not bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        super().mouseMoveEvent(event)

    def snap_position(self, value):
        if not self.snap_drag or self.snap_interval <= 1:
            return
        lower = max(self.minimum(), value // self.snap_interval * self.snap_interval)
        upper = min(self.maximum(), (value + self.snap_interval - 1) // self.snap_interval * self.snap_interval)
        snapped = lower if value - lower < upper - value else upper
        if snapped != value:
            self.setSliderPosition(snapped)


class WheelIgnoringComboBox(QComboBox):
    def wheelEvent(self, event):
        event.ignore()


class PanelCheckBox(QCheckBox):
    def paintEvent(self, event):
        super().paintEvent(event)
        if not self.isChecked():
            return
        option = QStyleOptionButton()
        self.initStyleOption(option)
        rect = self.style().subElementRect(QStyle.SubElement.SE_CheckBoxIndicator, option, self)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor("white") if self.isEnabled() else QColor("#a1a9b8"))
        pen.setWidthF(2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.drawLine(QPointF(rect.left() + rect.width() * .23, rect.top() + rect.height() * .52),
                         QPointF(rect.left() + rect.width() * .43, rect.top() + rect.height() * .72))
        painter.drawLine(QPointF(rect.left() + rect.width() * .43, rect.top() + rect.height() * .72),
                         QPointF(rect.left() + rect.width() * .78, rect.top() + rect.height() * .30))


def palette_action_icon(kind):
    pixmap = QPixmap(20, 20)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor("#f5f8ff"), 2)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    if kind == "save":
        painter.drawRoundedRect(3, 3, 14, 14, 1, 1)
        painter.drawLine(6, 3, 6, 8)
        painter.drawLine(6, 8, 14, 8)
        painter.drawLine(14, 8, 14, 3)
        painter.drawLine(6, 13, 14, 13)
    elif kind == "cancel":
        painter.drawLine(5, 5, 15, 15)
        painter.drawLine(15, 5, 5, 15)
    elif kind == "choose":
        painter.drawLine(4, 16, 6, 12)
        painter.drawLine(6, 12, 12, 6)
        painter.drawLine(8, 14, 14, 8)
        painter.drawLine(6, 12, 8, 14)
        painter.drawLine(12, 6, 14, 8)
        painter.drawLine(11, 5, 15, 9)
        painter.drawLine(14, 3, 17, 6)
    elif kind == "remove":
        painter.drawLine(5, 6, 15, 6)
        painter.drawLine(8, 3, 12, 3)
        painter.drawLine(6, 8, 7, 17)
        painter.drawLine(14, 8, 13, 17)
        painter.drawLine(7, 17, 13, 17)
        painter.drawLine(10, 9, 10, 14)
    painter.end()
    return QIcon(pixmap)


class CustomPaletteDialog(QDialog):
    def __init__(self, palettes, save, capture, parent=None, artwork_colors=None):
        saved = {palette["slot"]: palette for palette in palettes}
        empty = -1
        if artwork_colors is not None:
            empty = next((index for index in range(CUSTOM_PALETTE_LIMIT) if index + 1 not in saved), -1)
            if empty < 0:
                raise ValueError("All eight palette slots are in use; clear one before saving artwork colours")
        super().__init__(parent)
        self.setWindowTitle("Custom palettes")
        self.setModal(True)
        self.setStyleSheet("""
            QDialog { background: #070e27; }
            QLineEdit { background: #070e27; color: white; border: 1px solid #718cc1;
                        border-radius: 6px; padding: 6px; }
            QLabel#paletteStatus { color: #ffb6a8; }
            QDialog QPushButton { background: #0a183a; color: #f5f8ff; min-width: 0; min-height: 0;
                border: 1px solid #718cc1; border-radius: 6px; padding: 8px 12px; }
            QDialog QPushButton:hover { background: #183567; }
        """)
        self.save_palettes = save
        self.capture_palette = capture
        self.slots = []
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Choose a palette slot"))
        self.slot_selector = WheelIgnoringComboBox()
        self.slot_selector.setAccessibleName("Palette slot")
        layout.addWidget(self.slot_selector)
        self.stack = QStackedWidget()
        layout.addWidget(self.stack, 1)
        for slot in range(1, CUSTOM_PALETTE_LIMIT + 1):
            page = QWidget()
            page_layout = QVBoxLayout(page)
            page_layout.setContentsMargins(0, 0, 0, 0)
            name = QLineEdit()
            name.setAccessibleName(f"Palette {slot} name")
            name.setPlaceholderText("Palette name")
            name_label = QLabel("Name")
            name_label.setBuddy(name)
            page_layout.addWidget(name_label)
            page_layout.addWidget(name)
            enabled = PanelCheckBox("Show in lighting colours")
            enabled.setChecked(True)
            page_layout.addWidget(enabled)
            page_layout.addWidget(QLabel("Enter 2–8 colours; leave unused fields empty."))
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            content = QWidget()
            colors_layout = QVBoxLayout(content)
            colors_layout.setContentsMargins(0, 0, 0, 0)
            rows, inputs = [], []
            for index in range(8):
                row = QWidget()
                row_layout = QHBoxLayout(row)
                row_layout.setContentsMargins(0, 0, 0, 0)
                field = QLineEdit()
                field.setAccessibleName(f"Palette {slot} colour {index + 1}")
                field.setPlaceholderText("#rrggbb")
                choose = QPushButton()
                choose.setIcon(palette_action_icon("choose"))
                choose.setIconSize(QSize(20, 20))
                choose.setToolTip("Choose colour...")
                choose.setAccessibleName(f"Choose palette {slot} colour {index + 1}")
                choose.clicked.connect(lambda _checked=False, target=field: self.choose_color(target))
                remove = QPushButton()
                remove.setIcon(palette_action_icon("remove"))
                remove.setIconSize(QSize(20, 20))
                remove.setToolTip("Remove colour")
                remove.setAccessibleName(f"Remove palette {slot} colour {index + 1}")
                remove.setEnabled(False)
                field.textChanged.connect(lambda text, button=remove: button.setEnabled(bool(text.strip())))
                remove.clicked.connect(
                    lambda _checked=False, slot_index=slot - 1, color_index=index:
                    self.remove_color(slot_index, color_index))
                row_layout.addWidget(field, 1)
                row_layout.addWidget(choose)
                row_layout.addWidget(remove)
                colors_layout.addWidget(row)
                rows.append(row)
                inputs.append(field)
            colors_layout.addStretch()
            scroll.setWidget(content)
            page_layout.addWidget(scroll, 1)
            buttons = QHBoxLayout()
            capture_button = QPushButton("Capture current palette")
            clear = QPushButton("Clear palette")
            capture_button.clicked.connect(
                lambda _checked=False, index=slot - 1: self.capture_colors(index))
            clear.clicked.connect(lambda _checked=False, index=slot - 1: self.clear_slot(index))
            buttons.addWidget(capture_button)
            buttons.addWidget(clear)
            page_layout.addLayout(buttons)
            self.stack.addWidget(page)
            self.slot_selector.addItem(f"Slot {slot}", slot)
            self.slots.append({"name": name, "enabled": enabled, "rows": rows, "inputs": inputs})
            name.textChanged.connect(lambda text, index=slot - 1: self.update_slot_label(index, text))
            if slot in saved:
                palette = saved[slot]
                name.setText(palette["name"])
                enabled.setChecked(palette["enabled"])
                self.set_colors(slot - 1, palette["colors"])
            else:
                self.set_colors(slot - 1, [])
        self.slot_selector.currentIndexChanged.connect(self.stack.setCurrentIndex)
        self.status = QLabel()
        self.status.setObjectName("paletteStatus")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setIcon(palette_action_icon("save"))
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setIcon(palette_action_icon("cancel"))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(540, 520)
        if artwork_colors is not None:
            self.slot_selector.setCurrentIndex(empty)
            self.set_colors(empty, artwork_colors)

    def update_slot_label(self, index, text):
        self.slot_selector.setItemText(index, f"Slot {index + 1}" + (f" - {text.strip()}" if text.strip() else ""))

    def set_colors(self, index, colors):
        slot = self.slots[index]
        for color_index, field in enumerate(slot["inputs"]):
            field.setText(colors[color_index] if color_index < len(colors) else "")

    def remove_color(self, index, color_index):
        slot = self.slots[index]
        colors = [field.text() for field in slot["inputs"]]
        colors.pop(color_index)
        self.set_colors(index, colors)

    def clear_slot(self, index):
        slot = self.slots[index]
        slot["name"].clear()
        slot["enabled"].setChecked(True)
        self.set_colors(index, [])
        self.status.clear()

    def capture_colors(self, index):
        try:
            if self.capture_palette is None:
                raise ValueError("Current palette capture is unavailable")
            colors = self.capture_palette()
            if not 2 <= len(colors) <= 8:
                raise ValueError("The current palette does not contain two to eight colours")
        except (OSError, ValueError, RuntimeError) as error:
            self.status.setText(f"Palette could not be captured: {error}")
            return
        self.set_colors(index, colors)
        self.status.clear()

    def choose_color(self, field):
        initial = QColor(field.text())
        chosen = QColorDialog.getColor(initial if initial.isValid() else QColor("white"), self,
                                       "Choose colour")
        if chosen.isValid():
            field.setText(chosen.name())

    def accept(self):
        palettes = []
        for index, slot in enumerate(self.slots):
            name = slot["name"].text()
            colors = [field.text().strip() for field in slot["inputs"] if field.text().strip()]
            if not name.strip() and not colors:
                continue
            palettes.append({"slot": index + 1, "name": name, "colors": colors,
                             "enabled": slot["enabled"].isChecked()})
        try:
            palettes = normalize_custom_palettes(palettes)
            self.save_palettes(palettes)
        except (OSError, ValueError, RuntimeError) as error:
            self.status.setText(f"Palettes could not be saved: {error}")
            return
        super().accept()


class SidePanel(QWidget):
    close_requested = Signal()

    def __init__(self, state, kind, modes, request, parent=None, capture_palette=None):
        super().__init__(parent)
        self.kind = kind
        self.request = request
        self.capture_palette = capture_palette
        self.custom_palettes = state.get("customPalettes", [])
        self.fields = {}
        self.committed = {}
        self.slider_outputs = {}
        self.slider_labels = {}
        self.resetting = False
        self.setObjectName("sidePanel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.setAccessibleName("Visual effects" if kind == "visual" else "Settings")
        self.setStyleSheet("""
            QWidget#sidePanel {
                background: qradialgradient(cx:0.5, cy:0.5, radius:0.75,
                    fx:0.5, fy:0.5, stop:0 #001361, stop:0.65 #000629, stop:1 #000008);
                border: 1px solid rgba(147,175,255,90);
            }
            QWidget#panelContent, QScrollArea#panelScroll, QScrollArea#panelScroll > QWidget > QWidget {
                background: transparent;
                border: 0;
            }
            QLabel { background: transparent; color: white; font-size: 13px; }
            QLabel:disabled { color: #78839a; }
            QLabel#panelTitle { font-size: 21px; font-weight: 700; }
            QLabel#panelSection { color: #a9c5ff; font-size: 13px; font-weight: 700; }
            QLabel#panelHelp { color: #c1cde4; }
            QLabel#panelStatus { color: #ffb6a8; }
            QComboBox {
                min-width: 0; min-height: 34px; padding: 0 8px;
                border: 1px solid #718cc1; border-radius: 6px;
                background: #070e27; color: white;
            }
            QComboBox QAbstractItemView {
                background: #070e27; color: white; selection-background-color: #244b9b;
            }
            QPushButton {
                min-width: 0; min-height: 0; padding: 8px 12px;
                border: 1px solid #718cc1; border-radius: 6px;
                background: #0a183a; color: white;
            }
            QPushButton:hover { background: #183567; }
            QPushButton#panelClose { border: 0; padding: 0; font-size: 24px; }
            QCheckBox { color: white; spacing: 8px; }
            QCheckBox::indicator {
                width: 18px; height: 18px; border: 1px solid #718cc1;
                border-radius: 4px; background: #070e27;
            }
            QCheckBox::indicator:checked { background: #244b9b; }
            QCheckBox::indicator:disabled { border-color: #596173; background: #313949; }
            QSlider::groove:horizontal { height: 6px; background: #394b73; border-radius: 3px; }
            QSlider::sub-page:horizontal { background: #60a6ff; border-radius: 3px; }
            QSlider::handle:horizontal {
                width: 16px; height: 16px; margin: -5px 0; background: #a7ceff; border-radius: 8px;
            }
            QSlider::groove:horizontal:disabled, QSlider::sub-page:horizontal:disabled {
                background: #313949;
            }
            QSlider::handle:horizontal:disabled { background: #78839a; }
            QScrollBar:vertical { background: #050b20; width: 10px; }
            QScrollBar::handle:vertical { background: #537bc0; min-height: 24px; border-radius: 5px; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
        """)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        header = QHBoxLayout()
        header.setContentsMargins(18, 12, 18, 8)
        title = QLabel(self.accessibleName())
        title.setObjectName("panelTitle")
        header.addWidget(title)
        header.addStretch()
        self.close_button = QPushButton("×")
        self.close_button.setObjectName("panelClose")
        self.close_button.setFixedSize(36, 36)
        self.close_button.setAccessibleName("Close " + self.accessibleName().lower())
        self.close_button.clicked.connect(self.close_requested)
        header.addWidget(self.close_button)
        outer.addLayout(header)

        self.scroll = QScrollArea()
        self.scroll.setObjectName("panelScroll")
        self.scroll.setWidgetResizable(True)
        content = QWidget()
        content.setObjectName("panelContent")
        self.scroll.setWidget(content)
        outer.addWidget(self.scroll, 1)
        self.content_layout = QVBoxLayout(content)
        self.content_layout.setContentsMargins(18, 8, 18, 12)
        self.content_layout.setSpacing(10)

        if kind == "visual":
            self.build_visual(state, modes)
        else:
            self.build_settings(state)
        self.content_layout.addStretch()

        self.status = QLabel()
        self.status.setObjectName("panelStatus")
        self.status.setWordWrap(True)
        if kind == "settings" and state.get("boostWarning"):
            self.status.setText(state["boostWarning"])
        outer.addWidget(self.status)
        footer = QHBoxLayout()
        footer.setContentsMargins(18, 8, 18, 14)
        self.reset_button = QPushButton("Reset to defaults")
        self.reset_button.clicked.connect(self.reset_fields)
        footer.addWidget(self.reset_button)
        outer.addLayout(footer)

    def section(self, text, layout=None):
        label = QLabel(text.upper())
        label.setObjectName("panelSection")
        (layout if layout is not None else self.content_layout).addWidget(label)

    def settings_group(self, parent_layout=None):
        group = QWidget()
        layout = QVBoxLayout(group)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        (parent_layout if parent_layout is not None else self.content_layout).addWidget(group)
        return group, layout

    def help_text(self, text):
        label = QLabel(text)
        label.setObjectName("panelHelp")
        label.setWordWrap(True)
        self.content_layout.addWidget(label)

    def combo(self, title, values, value, layout=None):
        label = QLabel(title)
        combo = WheelIgnoringComboBox()
        combo.setAccessibleName(title)
        label.setBuddy(combo)
        for item_value, item_label in values:
            combo.addItem(item_label, item_value)
        combo.setCurrentIndex(combo.findData(value))
        target_layout = layout if layout is not None else self.content_layout
        target_layout.addWidget(label)
        target_layout.addWidget(combo)
        return combo

    def slider(self, title, minimum, maximum, value, formatter, layout=None, snap_interval=1):
        row = QHBoxLayout()
        label = QLabel(title)
        output = QLabel()
        slider = SnappingSlider(Qt.Orientation.Horizontal, snap_interval)
        slider.setAccessibleName(title)
        label.setBuddy(slider)
        slider.setRange(minimum, maximum)
        slider.setValue(value)
        output.setText(formatter(value))
        self.slider_outputs[slider] = (output, formatter)
        self.slider_labels[slider] = label
        slider.valueChanged.connect(lambda value: output.setText(formatter(value)))
        row.addWidget(label)
        row.addStretch()
        row.addWidget(output)
        target_layout = layout if layout is not None else self.content_layout
        target_layout.addLayout(row)
        target_layout.addWidget(slider)
        return slider, output

    def bind(self, key, widget, default, path, initial):
        self.fields[key] = (widget, default, path)
        self.committed[key] = initial
        if isinstance(widget, QComboBox):
            widget.currentIndexChanged.connect(lambda _index: self.apply_field(key))
        elif isinstance(widget, QCheckBox):
            widget.toggled.connect(lambda _checked: self.apply_field(key))
        else:
            widget.valueChanged.connect(lambda _value: self.apply_field(key))

    @staticmethod
    def field_value(widget):
        if isinstance(widget, QComboBox):
            return widget.currentData()
        if isinstance(widget, QCheckBox):
            return widget.isChecked()
        return widget.value()

    @staticmethod
    def set_field_value(widget, value):
        if isinstance(widget, QComboBox):
            widget.setCurrentIndex(widget.findData(value))
        elif isinstance(widget, QCheckBox):
            widget.setChecked(value)
        else:
            widget.setValue(value)

    def set_field_silently(self, widget, value):
        previous = self.field_value(widget)
        with QSignalBlocker(widget):
            self.set_field_value(widget, value)
        if widget in self.slider_outputs:
            output, formatter = self.slider_outputs[widget]
            output.setText(formatter(widget.value()))
        if widget in (getattr(self, "lighting_mode", None), getattr(self, "lighting_color_mode", None)):
            self.update_lighting_settings_visibility()
        if widget is getattr(self, "disco_style", None):
            self.update_light_wire_visibility()
        if widget is getattr(self, "mode", None):
            self.update_line_settings_visibility()
        if (widget is getattr(self, "source", None) and hasattr(self, "source_gain")
                and previous != self.field_value(widget)):
            self.source_gain.setSliderDown(False)
            self.update_selected_source()
        if widget is getattr(self, "source_prompt_enabled", None):
            self.update_prompt_duration_enabled()

    def apply_field(self, key):
        widget, _default, path = self.fields[key]
        value = self.field_value(widget)
        if value is None:
            return
        if not self.resetting:
            self.status.clear()
        try:
            self.request(path, value)
        except (OSError, ValueError, RuntimeError) as error:
            self.status.setText(f"{widget.accessibleName()} could not be saved: {error}")
            self.set_field_silently(widget, self.committed[key])
            return
        self.committed[key] = value

    def reset_fields(self):
        self.status.clear()
        self.resetting = True
        try:
            for widget, default, _path in self.fields.values():
                if default is not None:
                    self.set_field_value(widget, default)
            if self.kind == "settings":
                for source, gain in list(self.source_gains.items()):
                    if gain != 1.0:
                        self.apply_source_gain(source, 1.0)
                self.update_selected_source()
        finally:
            self.resetting = False

    def sync_fields(self, state):
        if "customPalettes" in state:
            self.custom_palettes = state["customPalettes"]
            if self.kind == "visual":
                self.update_palette_items(state.get("lightingColorMode", "artwork"))
        for key, (widget, _default, _path) in self.fields.items():
            if key not in state or (isinstance(widget, QSlider) and widget.isSliderDown()):
                continue
            value = state[key]
            if value is None:
                continue
            self.committed[key] = value
            self.set_field_silently(widget, value)

    def build_visual(self, state, modes):
        self.section("Visualisation")
        self.mode = self.combo("Visualisation mode", modes, state.get("visualizerMode", 0))
        self.bind("visualizerMode", self.mode, 0, "visualizer-mode", self.mode.currentData())
        self.line_settings = QWidget()
        line_layout = QVBoxLayout(self.line_settings)
        line_layout.setContentsMargins(0, 0, 0, 0)
        line_layout.setSpacing(10)
        self.content_layout.addWidget(self.line_settings)
        self.scope_zero_line = PanelCheckBox("Show scope zero line")
        self.scope_zero_line.setChecked(bool(state.get("scopeZeroLine", True)))
        line_layout.addWidget(self.scope_zero_line)
        self.bind("scopeZeroLine", self.scope_zero_line, True, "scope-zero-line",
                  self.scope_zero_line.isChecked())
        self.scope_width, self.scope_width_value = self.slider(
            "Line thickness", 25, 300, int(state.get("scopeLineWidth", 100)),
            lambda value: f"{value}%", line_layout, snap_interval=10)
        self.bind("scopeLineWidth", self.scope_width, 100, "scope-line-width",
                  self.scope_width.value())
        self.mode.currentIndexChanged.connect(self.update_line_settings_visibility)
        self.update_line_settings_visibility()

        self.section("Artwork")
        self.artwork_mode = self.combo("Artwork", [
            ("album", "Show Album Art"), ("logo", "Show Logo"), ("none", "No artwork"),
        ], state.get("artworkMode", "album"))
        self.bind("artworkMode", self.artwork_mode, "album", "artwork-mode",
                  self.artwork_mode.currentData())
        self.album_opacity, self.album_opacity_value = self.slider(
            "Album art opacity", 0, 100, int(state.get("albumArtOpacity", 22)), lambda v: f"{v}%",
            snap_interval=10)
        self.bind("albumArtOpacity", self.album_opacity, 22, "album-art-opacity",
                  self.album_opacity.value())

        self.section("Lighting")
        self.lighting_mode = self.combo("Lighting", [
            ("border", "Edge lighting"), ("disco", "Lights"), ("none", "None"),
        ], state.get("lightingMode", "border"))
        self.bind("lightingMode", self.lighting_mode, "border", "lighting-mode",
                  self.lighting_mode.currentData())
        palette_options = list(LIGHTING_COLOR_OPTIONS) + [(f'custom-{palette["slot"]}', palette["name"])
             for palette in self.custom_palettes if palette["enabled"]]
        self.lighting_color_mode = self.combo("Lighting colours", palette_options,
                                              state.get("lightingColorMode", "artwork"))
        self._palette_items = palette_options
        self.bind("lightingColorMode", self.lighting_color_mode, "artwork", "lighting-color-mode",
                  self.lighting_color_mode.currentData())
        self.custom_palettes_button = QPushButton("Custom palettes...")
        self.custom_palettes_button.clicked.connect(self.open_custom_palettes)
        self.content_layout.addWidget(self.custom_palettes_button)
        self.custom_palettes_button.ensurePolished()
        self.custom_palettes_button.setMinimumHeight(self.custom_palettes_button.sizeHint().height())
        self.lighting_count_settings, count_layout = self.settings_group()
        self.edge_count, self.edge_count_value = self.slider(
            "Lighting colour count", 2, 8, int(state.get("edgeColorCount", 8)),
            lambda v: f"{v} colours", count_layout)
        self.bind("edgeColorCount", self.edge_count, 8, "edge-color-count", self.edge_count.value())
        self.edge_swirl = PanelCheckBox("Swirl edges / animate lights to music")
        self.edge_swirl.setAccessibleName("Animate lighting")
        self.edge_swirl.setChecked(bool(state.get("edgeSwirl", False)))
        self.content_layout.addWidget(self.edge_swirl)
        self.bind("edgeSwirl", self.edge_swirl, False, "edge-swirl", self.edge_swirl.isChecked())

        self.lighting_appearance, appearance_layout = self.settings_group()
        self.section("Lighting appearance", appearance_layout)
        specs = (
            ("edge_intensity", "edgeColorIntensity", "Lighting colour vividness", 0, 100, 100,
             "edge-color-intensity", lambda v: "Subtle" if v == 0 else "Vivid" if v == 100 else f"{v}%"),
            ("edge_fade", "edgeFade", "Lighting fade", 0, 100, 50, "edge-fade",
             lambda v: "Sharp" if v == 0 else "Soft" if v == 100 else f"{v}%"),
            ("edge_opacity", "edgeOpacity", "Lighting opacity", 0, 100, 70, "edge-opacity",
             lambda v: "Hidden" if v == 0 else "Opaque" if v == 100 else f"{v}%"),
        )
        for name, key, title, minimum, maximum, default, path, formatter in specs:
            slider, output = self.slider(
                title, minimum, maximum, int(state.get(key, default)), formatter, appearance_layout,
                snap_interval=10)
            setattr(self, name, slider)
            setattr(self, name + "_value", output)
            self.bind(key, slider, default, path, slider.value())

        self.light_settings, light_layout = self.settings_group()
        self.section("Lights", light_layout)
        self.disco_style = self.combo("Light style", [
            ("glow", "Glow"), ("fairy", "Fairy lights"),
            ("bulb", "Classic bulbs"), ("star", "Stars"),
        ], state.get("discoLightStyle", "glow"), light_layout)
        self.bind("discoLightStyle", self.disco_style, "glow", "disco-light-style",
                  self.disco_style.currentData())
        self.light_wire_settings, wire_layout = self.settings_group(light_layout)
        self.disco_wire_color = self.combo("Light wire", [
            ("green", "Green"), ("theme", "Lighting colours"), ("none", "Wireless"),
        ], state.get("discoWireColor", "green"), wire_layout)
        self.bind("discoWireColor", self.disco_wire_color, "green", "disco-wire-color",
                  self.disco_wire_color.currentData())
        self.update_light_wire_visibility()
        self.disco_style.currentIndexChanged.connect(self.update_light_wire_visibility)
        for name, key, title, minimum, maximum, default, path, formatter in (
            ("disco_count", "discoLightCount", "Light count", 3, 40, 16, "disco-light-count", str),
            ("disco_size", "discoLightSize", "Light size", 40, 200, 100, "disco-light-size",
             lambda v: f"{v}%"),
        ):
            slider, output = self.slider(
                title, minimum, maximum, int(state.get(key, default)), formatter, light_layout,
                snap_interval=10 if key == "discoLightSize" else 1)
            setattr(self, name, slider)
            setattr(self, name + "_value", output)
            self.bind(key, slider, default, path, slider.value())

        self.border_settings, border_layout = self.settings_group()
        self.section("Edge lighting", border_layout)
        self.edge_depth, self.edge_depth_value = self.slider(
            "Border depth", 25, 200, int(state.get("edgeDepth", 100)),
            lambda v: f"{v}%", border_layout, snap_interval=10)
        self.bind("edgeDepth", self.edge_depth, 100, "edge-depth", self.edge_depth.value())
        self.edge_speed, self.edge_speed_value = self.slider(
            "Edge colour speed", 1, 20, int(state.get("edgeSwirlSlowdown", 1)),
            lambda v: "In time with music" if v == 1 else f"{v}× slower", border_layout)
        self.bind("edgeSwirlSlowdown", self.edge_speed, 1, "edge-swirl-slowdown", self.edge_speed.value())
        self.edge_direction = self.combo("Border rotation direction", [
            ("clockwise", "Clockwise"), ("counter-clockwise", "Counter-clockwise"),
            ("random", "Random per track"),
        ], state.get("edgeSwirlDirection", "clockwise"), border_layout)
        self.bind("edgeSwirlDirection", self.edge_direction, "clockwise", "edge-swirl-direction",
                  self.edge_direction.currentData())
        self.update_lighting_settings_visibility()
        self.lighting_mode.currentIndexChanged.connect(self.update_lighting_settings_visibility)
        self.lighting_color_mode.currentIndexChanged.connect(self.update_lighting_settings_visibility)

    def update_line_settings_visibility(self):
        mode = self.mode.currentData()
        self.line_settings.setVisible(mode in (1, 2))
        self.scope_zero_line.setVisible(mode == 1)

    def update_lighting_settings_visibility(self):
        mode = self.lighting_mode.currentData()
        color_mode = self.lighting_color_mode.currentData()
        self.edge_swirl.setEnabled(mode != "none")
        self.edge_swirl.setVisible(mode in ("border", "disco"))
        self.lighting_count_settings.setVisible(
            color_mode != "gelato" and not str(color_mode).startswith("custom-")
            and (mode in ("border", "disco") or color_mode == "rainbow"))
        self.lighting_appearance.setVisible(mode in ("border", "disco"))
        self.light_settings.setVisible(mode == "disco")
        self.border_settings.setVisible(mode == "border")

    def update_light_wire_visibility(self):
        visible = self.disco_style.currentData() != "glow"
        self.light_wire_settings.setVisible(visible)
        self.disco_wire_color.setEnabled(visible)

    def build_settings(self, state):
        self.section("Audio")
        self.source = self.combo(
            "Playback source", [], None,
        )
        self.update_source_items(state.get("sources", []))
        self.source.setCurrentIndex(self.source.findData(state.get("selectedSource")))
        selected_source = self.source.currentData()
        self.source_gains = dict(state.get("sourceGains") or {})
        self.bind("selectedSource", self.source, None, "source", selected_source)
        self.source_gain, self.source_gain_value = self.slider(
            "Selected source volume boost", 10, 40,
            round(float(self.source_gains.get(selected_source, 1.0)) * 10),
            lambda v: f"{v / 10:.1f}×")
        self.source_gain.setEnabled(selected_source is not None)
        self.source_gain.valueChanged.connect(self.update_source_gain)
        self.source.currentIndexChanged.connect(self.update_selected_source)
        self.help_text("Boost applies to the selected source while AmbiBeat is open. High settings may distort.")
        system_volume = state.get("systemVolume")
        self.system_volume, self.system_volume_value = self.slider(
            "System volume", 0, 100, int(system_volume) if system_volume is not None else 0,
            lambda v: f"{v}%", snap_interval=10)
        self.system_volume.setEnabled(system_volume is not None)
        self.bind("systemVolume", self.system_volume, None, "system-volume",
                  self.system_volume.value() if system_volume is not None else None)
        self.sample_rate = self.combo("Capture sample rate", [
            (rate, f"{rate / 1000:g} kHz") for rate in (22050, 32000, 44100, 48000, 96000)
        ], int(state.get("sampleRate", 48000)))
        self.help_text("Lower rates can reduce capture load; 22.05 kHz limits the visualiser's top frequency.")
        self.bind("sampleRate", self.sample_rate, 48000, "sample-rate", self.sample_rate.currentData())
        self.source_prompt_seconds, self.source_prompt_seconds_value = self.slider(
            "Source switch prompt duration", SOURCE_PROMPT_SECONDS_MIN, SOURCE_PROMPT_SECONDS_MAX,
            int(state.get("sourcePromptSeconds", SOURCE_PROMPT_SECONDS_DEFAULT)),
            lambda value: f"{value} s", snap_interval=10)
        self.bind("sourcePromptSeconds", self.source_prompt_seconds, SOURCE_PROMPT_SECONDS_DEFAULT,
                  "source-prompt-seconds", self.source_prompt_seconds.value())
        self.source_prompt_enabled = PanelCheckBox("Show switch to new source message")
        self.source_prompt_enabled.setAccessibleName("Show switch to new source message")
        self.source_prompt_enabled.setChecked(state.get("sourcePromptEnabled", True))
        self.content_layout.addWidget(self.source_prompt_enabled)
        self.bind("sourcePromptEnabled", self.source_prompt_enabled, True, "source-prompt-enabled",
                  self.source_prompt_enabled.isChecked())
        self.source_prompt_enabled.toggled.connect(self.update_prompt_duration_enabled)
        self.update_prompt_duration_enabled()
        self.section("Startup")
        self.show_photosensitivity_warning = PanelCheckBox("Show photosensitivity warning on startup")
        self.show_photosensitivity_warning.setAccessibleName("Show photosensitivity warning on startup")
        self.show_photosensitivity_warning.setChecked(state.get("showPhotosensitivityWarning", True))
        self.content_layout.addWidget(self.show_photosensitivity_warning)
        self.bind("showPhotosensitivityWarning", self.show_photosensitivity_warning, True,
                  "show-photosensitivity-warning", self.show_photosensitivity_warning.isChecked())

    def update_palette_items(self, selected):
        options = list(LIGHTING_COLOR_OPTIONS)
        options.extend((f'custom-{palette["slot"]}', palette["name"])
                       for palette in self.custom_palettes if palette["enabled"])
        if options != self._palette_items:
            with QSignalBlocker(self.lighting_color_mode):
                self.lighting_color_mode.clear()
                for value, label in options:
                    self.lighting_color_mode.addItem(label, value)
                self.lighting_color_mode.setCurrentIndex(self.lighting_color_mode.findData(selected))
            self._palette_items = options
            self.update_lighting_settings_visibility()

    def open_custom_palettes(self):
        dialog = CustomPaletteDialog(self.custom_palettes, self.save_custom_palettes,
                                     self.capture_palette, self)
        dialog.exec()

    def save_custom_palettes(self, palettes):
        self.request("custom-palettes", palettes)
        self.custom_palettes = palettes

    def update_source_gain(self, value):
        source = self.source.currentData()
        if source is not None:
            if not self.resetting:
                self.status.clear()
            if not self.apply_source_gain(source, value / 10.0):
                self.set_field_silently(self.source_gain, round(self.source_gains.get(source, 1.0) * 10))

    def apply_source_gain(self, source, gain):
        try:
            self.request("source-gain", gain, source)
        except (OSError, ValueError, RuntimeError) as error:
            self.status.setText(f"Source volume boost could not be saved: {error}")
            return False
        self.source_gains[source] = gain
        return True

    def update_selected_source(self):
        source = self.source.currentData()
        self.source_gain.setEnabled(source is not None)
        self.set_field_silently(self.source_gain, round(self.source_gains.get(source, 1.0) * 10))

    def update_prompt_duration_enabled(self):
        enabled = self.source_prompt_enabled.isChecked()
        self.source_prompt_seconds.setEnabled(enabled)
        self.slider_labels[self.source_prompt_seconds].setEnabled(enabled)
        self.source_prompt_seconds_value.setEnabled(enabled)

    def sync_settings(self, state):
        if (self.source_gain.isSliderDown() and "selectedSource" in state
                and state["selectedSource"] != self.source.currentData()):
            self.source_gain.setSliderDown(False)
        if "sources" in state and "selectedSource" in state:
            self.update_source_items(state["sources"])
        if "sourceGains" in state and not self.source_gain.isSliderDown():
            self.source_gains = dict(state["sourceGains"] or {})
        self.sync_fields(state)
        if "systemVolume" in state:
            self.system_volume.setEnabled(state["systemVolume"] is not None)
        if not self.source_gain.isSliderDown():
            self.update_selected_source()

    def update_source_items(self, sources):
        items = [(item["id"], item["label"], item.get("desktopEntry", ""), item.get("identity", ""))
                 for item in sources]
        if items == getattr(self, "_source_items", None):
            return
        with QSignalBlocker(self.source):
            self.source.clear()
            for source in sources:
                self.source.addItem(source_icon(source), source["label"], source["id"])
        self._source_items = items
