"""Qt slide-out visual effects and settings panels."""

from PySide6.QtCore import QSignalBlocker, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSlider,
    QVBoxLayout,
    QWidget,
)


class WheelIgnoringSlider(QSlider):
    def wheelEvent(self, event):
        event.ignore()


class SidePanel(QWidget):
    close_requested = Signal()

    def __init__(self, state, kind, modes, request, parent=None):
        super().__init__(parent)
        self.kind = kind
        self.request = request
        self.fields = {}
        self.committed = {}
        self.slider_outputs = {}
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
            QCheckBox::indicator { width: 18px; height: 18px; }
            QSlider::groove:horizontal { height: 6px; background: #394b73; border-radius: 3px; }
            QSlider::sub-page:horizontal { background: #60a6ff; border-radius: 3px; }
            QSlider::handle:horizontal {
                width: 16px; height: 16px; margin: -5px 0; background: #a7ceff; border-radius: 8px;
            }
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
        if kind == "visual":
            self.reset_button = QPushButton("Reset to defaults")
            self.reset_button.clicked.connect(self.reset_visual)
            footer.addWidget(self.reset_button)
        else:
            footer.addStretch()
            self.cancel_button = QPushButton("Cancel")
            self.cancel_button.clicked.connect(self.close_requested)
            self.save_button = QPushButton("Save")
            self.save_button.clicked.connect(self.save_settings)
            footer.addWidget(self.cancel_button)
            footer.addWidget(self.save_button)
        outer.addLayout(footer)

    def section(self, text):
        label = QLabel(text.upper())
        label.setObjectName("panelSection")
        self.content_layout.addWidget(label)

    def help_text(self, text):
        label = QLabel(text)
        label.setObjectName("panelHelp")
        label.setWordWrap(True)
        self.content_layout.addWidget(label)

    def combo(self, title, values, value):
        label = QLabel(title)
        combo = QComboBox()
        combo.setAccessibleName(title)
        label.setBuddy(combo)
        for item_value, item_label in values:
            combo.addItem(item_label, item_value)
        combo.setCurrentIndex(combo.findData(value))
        self.content_layout.addWidget(label)
        self.content_layout.addWidget(combo)
        return combo

    def slider(self, title, minimum, maximum, value, formatter):
        row = QHBoxLayout()
        label = QLabel(title)
        output = QLabel()
        slider = WheelIgnoringSlider(Qt.Orientation.Horizontal)
        slider.setAccessibleName(title)
        label.setBuddy(slider)
        slider.setRange(minimum, maximum)
        slider.setValue(value)
        output.setText(formatter(value))
        self.slider_outputs[slider] = (output, formatter)
        slider.valueChanged.connect(lambda value: output.setText(formatter(value)))
        row.addWidget(label)
        row.addStretch()
        row.addWidget(output)
        self.content_layout.addLayout(row)
        self.content_layout.addWidget(slider)
        return slider, output

    def bind(self, key, widget, default, path, initial):
        self.fields[key] = (widget, default, path)
        self.committed[key] = initial
        if isinstance(widget, QComboBox):
            widget.currentIndexChanged.connect(lambda _index: self.apply_visual(key))
        elif isinstance(widget, QCheckBox):
            widget.toggled.connect(lambda _checked: self.apply_visual(key))
        else:
            widget.valueChanged.connect(lambda _value: self.apply_visual(key))

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
        with QSignalBlocker(widget):
            self.set_field_value(widget, value)
        if widget in self.slider_outputs:
            output, formatter = self.slider_outputs[widget]
            output.setText(formatter(widget.value()))
        if widget is getattr(self, "lighting_mode", None):
            self.edge_swirl.setEnabled(self.lighting_mode.currentData() != "none")

    def apply_visual(self, key):
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

    def reset_visual(self):
        self.status.clear()
        self.resetting = True
        try:
            for widget, default, _path in self.fields.values():
                self.set_field_value(widget, default)
        finally:
            self.resetting = False

    def sync_visual(self, state):
        for key, (widget, _default, _path) in self.fields.items():
            if key not in state or (isinstance(widget, QSlider) and widget.isSliderDown()):
                continue
            value = state[key]
            self.committed[key] = value
            self.set_field_silently(widget, value)

    def build_visual(self, state, modes):
        self.section("Visualisation")
        self.mode = self.combo("Visualisation mode", modes, state.get("visualizerMode", 0))
        self.bind("visualizerMode", self.mode, 0, "visualizer-mode", self.mode.currentData())
        self.scope_zero_line = QCheckBox("Show scope zero line")
        self.scope_zero_line.setChecked(bool(state.get("scopeZeroLine", True)))
        self.content_layout.addWidget(self.scope_zero_line)
        self.bind("scopeZeroLine", self.scope_zero_line, True, "scope-zero-line",
                  self.scope_zero_line.isChecked())

        self.section("Artwork")
        self.artwork_mode = self.combo("Artwork", [
            ("album", "Show Album Art"), ("logo", "Show Logo"), ("none", "No artwork"),
        ], state.get("artworkMode", "album"))
        self.bind("artworkMode", self.artwork_mode, "album", "artwork-mode",
                  self.artwork_mode.currentData())
        self.album_opacity, self.album_opacity_value = self.slider(
            "Album art opacity", 0, 100, int(state.get("albumArtOpacity", 22)), lambda v: f"{v}%")
        self.bind("albumArtOpacity", self.album_opacity, 22, "album-art-opacity",
                  self.album_opacity.value())

        self.section("Lighting")
        self.lighting_mode = self.combo("Lighting", [
            ("border", "Edge lighting"), ("disco", "Lights"), ("none", "None"),
        ], state.get("lightingMode", "border"))
        self.bind("lightingMode", self.lighting_mode, "border", "lighting-mode",
                  self.lighting_mode.currentData())
        self.lighting_color_mode = self.combo("Lighting colours", [
            ("artwork", "Artwork colours"), ("rainbow", "Rainbow"),
        ], state.get("lightingColorMode", "artwork"))
        self.bind("lightingColorMode", self.lighting_color_mode, "artwork", "lighting-color-mode",
                  self.lighting_color_mode.currentData())
        self.edge_swirl = QCheckBox("Swirl edges / animate lights to music")
        self.edge_swirl.setAccessibleName("Animate lighting")
        self.edge_swirl.setChecked(bool(state.get("edgeSwirl", False)))
        self.content_layout.addWidget(self.edge_swirl)
        self.bind("edgeSwirl", self.edge_swirl, False, "edge-swirl", self.edge_swirl.isChecked())

        self.section("Lighting appearance")
        specs = (
            ("edge_intensity", "edgeColorIntensity", "Edge colour vividness", 0, 100, 100,
             "edge-color-intensity", lambda v: "Subtle" if v == 0 else "Vivid" if v == 100 else f"{v}%"),
            ("edge_fade", "edgeFade", "Edge colour fade", 0, 100, 50, "edge-fade",
             lambda v: "Sharp" if v == 0 else "Soft" if v == 100 else f"{v}%"),
            ("edge_opacity", "edgeOpacity", "Edge colour opacity", 0, 100, 70, "edge-opacity",
             lambda v: "Hidden" if v == 0 else "Opaque" if v == 100 else f"{v}%"),
            ("edge_count", "edgeColorCount", "Border colour count", 2, 8, 8, "edge-color-count",
             lambda v: f"{v} colours"),
            ("edge_depth", "edgeDepth", "Border depth", 25, 200, 100, "edge-depth", lambda v: f"{v}%"),
        )
        for name, key, title, minimum, maximum, default, path, formatter in specs:
            slider, output = self.slider(title, minimum, maximum, int(state.get(key, default)), formatter)
            setattr(self, name, slider)
            setattr(self, name + "_value", output)
            self.bind(key, slider, default, path, slider.value())

        self.section("Lights")
        for name, key, title, minimum, maximum, default, path, formatter in (
            ("disco_count", "discoLightCount", "Light count", 3, 40, 16, "disco-light-count", str),
            ("disco_size", "discoLightSize", "Light size", 40, 200, 100, "disco-light-size",
             lambda v: f"{v}%"),
        ):
            slider, output = self.slider(title, minimum, maximum, int(state.get(key, default)), formatter)
            setattr(self, name, slider)
            setattr(self, name + "_value", output)
            self.bind(key, slider, default, path, slider.value())

        self.section("Animation")
        self.edge_speed, self.edge_speed_value = self.slider(
            "Edge colour speed", 1, 20, int(state.get("edgeSwirlSlowdown", 1)),
            lambda v: "In time with music" if v == 1 else f"{v}× slower")
        self.bind("edgeSwirlSlowdown", self.edge_speed, 1, "edge-swirl-slowdown", self.edge_speed.value())
        self.edge_direction = self.combo("Border rotation direction", [
            ("clockwise", "Clockwise"), ("counter-clockwise", "Counter-clockwise"),
            ("random", "Random per track"),
        ], state.get("edgeSwirlDirection", "clockwise"))
        self.bind("edgeSwirlDirection", self.edge_direction, "clockwise", "edge-swirl-direction",
                  self.edge_direction.currentData())
        self.edge_swirl.setEnabled(self.lighting_mode.currentData() != "none")
        self.lighting_mode.currentIndexChanged.connect(
            lambda: self.edge_swirl.setEnabled(self.lighting_mode.currentData() != "none"))

    def build_settings(self, state):
        self.section("Audio")
        self.source = self.combo(
            "Playback source",
            [(item["id"], item["label"]) for item in state.get("sources", [])],
            state.get("selectedSource"),
        )
        self.saved_source = self.source.currentData()
        self.source_gains = dict(state.get("sourceGains") or {})
        self.saved_gains = self.source_gains.copy()
        self.source_gain, self.source_gain_value = self.slider(
            "Selected source volume boost", 10, 40,
            round(float(self.source_gains.get(self.saved_source, 1.0)) * 10),
            lambda v: f"{v / 10:.1f}×")
        self.source_gain.setEnabled(self.saved_source is not None)
        self.source_gain.valueChanged.connect(self.update_source_gain)
        self.source.currentIndexChanged.connect(self.update_selected_source)
        self.help_text("Boost applies to the selected source while AmbiBeat is open. High settings may distort.")
        system_volume = state.get("systemVolume")
        self.system_volume, self.system_volume_value = self.slider(
            "System volume", 0, 100, int(system_volume) if system_volume is not None else 0,
            lambda v: f"{v}%")
        self.system_volume.setEnabled(system_volume is not None)
        self.saved_system_volume = self.system_volume.value()
        self.sample_rate = self.combo("Capture sample rate", [
            (rate, f"{rate / 1000:g} kHz") for rate in (22050, 32000, 44100, 48000, 96000)
        ], int(state.get("sampleRate", 48000)))
        self.help_text("Lower rates can reduce capture load; 22.05 kHz limits the visualiser's top frequency.")
        self.saved_rate = self.sample_rate.currentData()
        self.section("Startup")
        self.show_photosensitivity_warning = QCheckBox("Show photosensitivity warning on startup")
        self.show_photosensitivity_warning.setChecked(state.get("showPhotosensitivityWarning", True))
        self.content_layout.addWidget(self.show_photosensitivity_warning)
        self.saved_show_photosensitivity_warning = self.show_photosensitivity_warning.isChecked()

    def update_source_gain(self, value):
        source = self.source.currentData()
        if source is not None:
            self.source_gains[source] = value / 10.0

    def update_selected_source(self):
        source = self.source.currentData()
        self.source_gain.setEnabled(source is not None)
        self.set_field_silently(self.source_gain, round(self.source_gains.get(source, 1.0) * 10))

    def has_unsaved_settings(self):
        return (self.show_photosensitivity_warning.isChecked() != self.saved_show_photosensitivity_warning
                or self.sample_rate.currentData() != self.saved_rate
                or self.source.currentData() != self.saved_source
                or self.system_volume.isEnabled() and self.system_volume.value() != self.saved_system_volume
                or any(value != self.saved_gains.get(source, 1.0)
                       for source, value in self.source_gains.items()))

    def apply_settings(self):
        self.status.clear()
        try:
            if self.show_photosensitivity_warning.isChecked() != self.saved_show_photosensitivity_warning:
                self.request("show-photosensitivity-warning", self.show_photosensitivity_warning.isChecked())
                self.saved_show_photosensitivity_warning = self.show_photosensitivity_warning.isChecked()
            if self.sample_rate.currentData() != self.saved_rate:
                self.request("sample-rate", self.sample_rate.currentData())
                self.saved_rate = self.sample_rate.currentData()
            source = self.source.currentData()
            if source is not None and source != self.saved_source:
                self.request("source", source)
                self.saved_source = source
            if self.system_volume.isEnabled() and self.system_volume.value() != self.saved_system_volume:
                self.request("system-volume", self.system_volume.value())
                self.saved_system_volume = self.system_volume.value()
            for source, gain in self.source_gains.items():
                if gain != self.saved_gains.get(source, 1.0):
                    self.request("source-gain", gain, source)
                    self.saved_gains[source] = gain
        except (OSError, ValueError, RuntimeError) as error:
            self.status.setText(f"Settings could not be saved: {error}")
            return False
        return True

    def save_settings(self):
        if self.apply_settings():
            self.close_requested.emit()
