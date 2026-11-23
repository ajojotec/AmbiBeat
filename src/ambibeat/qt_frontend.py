"""Qt application window, visualisation renderer, and process lifecycle."""

import fcntl
import math
import os
import signal
import subprocess
import sys
import time
import urllib.parse
import shiboken6
import dbus
import dbus.mainloop.glib

from PySide6.QtCore import QEasingCurve, QEvent, QPoint, QPropertyAnimation, QRect, QRectF, QSignalBlocker, Qt, QTimer
from PySide6.QtGui import (
    QColor,
    QIcon,
    QLinearGradient,
    QConicalGradient,
    QPalette,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
    QImage,
    QRadialGradient,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QGraphicsDropShadowEffect,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QMenu,
    QToolButton,
    QSizePolicy,
    QSpacerItem,
    QStyle,
    QStyleOptionSlider,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)
from PySide6.QtOpenGLWidgets import QOpenGLWidget

from .backend import APP_ICON_PATH, SOURCE_PROMPT_SECONDS_DEFAULT, AmbiBeatBackend
from .qt_gpu import BorderShader, gpu_available
from .qt_panels import SidePanel, WheelIgnoringSlider, source_icon


QT_MODES = (
    (0, "Bars"),
    (1, "Scope"),
    (2, "Halo"),
    (4, "Time Vortex"),
    (5, "No visualisation"),
)
CHRISTMAS_COLORS = ("#e42c3e", "#36c66a", "#ffd575")


def format_track_time(microseconds):
    seconds = max(0, microseconds // 1_000_000)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


class TimelineSlider(WheelIgnoringSlider):
    def set_position_from_mouse(self, event):
        option = QStyleOptionSlider()
        self.initStyleOption(option)
        groove = self.style().subControlRect(QStyle.ComplexControl.CC_Slider, option,
                                             QStyle.SubControl.SC_SliderGroove, self)
        handle = self.style().subControlRect(QStyle.ComplexControl.CC_Slider, option,
                                             QStyle.SubControl.SC_SliderHandle, self)
        span = max(1, groove.width() - handle.width())
        position = round(event.position().x() - groove.x() - handle.width() / 2)
        self.setSliderPosition(QStyle.sliderValueFromPosition(
            self.minimum(), self.maximum(), position, span, option.upsideDown
        ))

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.setSliderDown(True)
            self.set_position_from_mouse(event)
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.isSliderDown() and event.buttons() & Qt.MouseButton.LeftButton:
            self.set_position_from_mouse(event)
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.isSliderDown():
            self.set_position_from_mouse(event)
            self.setSliderDown(False)
            event.accept()
        else:
            super().mouseReleaseEvent(event)


def disco_light_positions(width, height, count):
    perimeter = width + 2 * height
    top_count = max(1, min(count - 2, int(count * width / perimeter + .5)))
    left_count = (count - top_count) // 2
    right_count = count - top_count - left_count
    return (
        [(width * (i + .5) / top_count, 0) for i in range(top_count)]
        + [(0, height * (i + .5) / left_count) for i in range(left_count)]
        + [(width, height * (i + .5) / right_count) for i in range(right_count)]
    )


def festive_strand_geometry(width, height, count, unit):
    positions = disco_light_positions(width, height, count)
    top_count = sum(y == 0 for x, y in positions)
    left_count = sum(x == 0 for x, y in positions)
    right_count = count - top_count - left_count
    top_bend = min(unit * .8, width / top_count * .16)
    left_bend = min(unit * .8, height / left_count * .16)
    right_bend = min(unit * .8, height / right_count * .16)
    path = QPainterPath()
    path.moveTo(0, 2)
    for index in range(top_count):
        path.quadTo(width * (index + .5) / top_count, 2 + 2 * top_bend,
                    width * (index + 1) / top_count, 2)
    path.moveTo(2, 0)
    for index in range(left_count):
        path.quadTo(2 + 2 * left_bend, height * (index + .5) / left_count,
                    2, height * (index + 1) / left_count)
    path.moveTo(width - 2, 0)
    for index in range(right_count):
        path.quadTo(width - 2 - 2 * right_bend, height * (index + .5) / right_count,
                    width - 2, height * (index + 1) / right_count)
    bulbs = []
    for x, y in positions:
        if y == 0:
            bulbs.append((x, 2 + top_bend, 0))
        elif x == 0:
            bulbs.append((2 + left_bend, y, -90))
        else:
            bulbs.append((width - 2 - right_bend, y, 90))
    return path, bulbs


def scope_gradient(width, active_palette):
    colors = (active_palette["colors"] if active_palette else
              [QColor("#37ff9a"), QColor("#baff8a"), QColor("#37ff9a")])
    gradient = QLinearGradient(width * .08, 0, width * .92, 0)
    for position, color in zip((0, .5, 1), (colors[0], colors[2], colors[1])):
        gradient.setColorAt(position, color)
    return gradient


def paint_scope_path(painter, path, gradient, line_width=3.5):
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setOpacity(1)
    underlay = QPen(QColor(9, 11, 18, 190))
    underlay.setWidthF(line_width * 2)
    underlay.setCapStyle(Qt.PenCapStyle.RoundCap)
    underlay.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(underlay)
    painter.drawPath(path)
    for width, opacity in ((line_width * 4, .06), (line_width * 18 / 7, .12)):
        painter.setOpacity(opacity)
        glow_pen = QPen(gradient, width)
        glow_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        glow_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(glow_pen)
        painter.drawPath(path)
    painter.setOpacity(1)
    pen = QPen(gradient, line_width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.drawPath(path)


class MovingEdgeBorder(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.angle = 0.0
        self.paused = False
        self.colors = [
            QColor("#1b5bff"),
            QColor("#b46cff"),
            QColor("#6a70ff"),
            QColor("#37ff9a"),
        ]
        self.opacity = 0.78
        self.disco_levels = []
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def set_palette(self, colors):
        if colors:
            self.colors = [QColor(color) for color in colors]

    def adjusted_colors(self, state):
        intensity = float(state.get("edgeColorIntensity", 100)) / 100
        opacity = self.opacity * (0.15 + intensity * 0.85) * 0.78
        colors = []
        for color in self.colors:
            h, s, l, _ = color.getHslF()
            adjusted = QColor.fromHslF(max(0, h), min(1, s * (0.35 + intensity * 0.95)),
                                       min(1, l * (0.55 + intensity * 0.75)))
            adjusted.setAlphaF(opacity)
            colors.append(adjusted)
        return colors

    def border_palette(self, state):
        colors = self.adjusted_colors(state)
        if (not state.get("edgeSwirl")
                and state.get("lightingColorMode") not in ("rainbow", "christmas")):
            return [colors[1 % len(colors)], colors[2 % len(colors)],
                    colors[0], colors[1 % len(colors)]]
        return colors + list(reversed(colors))

    def paintEvent(self, event):
        painter = QPainter(self)
        self.paint_border(painter, self.window().state)

    def update_disco(self, state, elapsed):
        if state.get("lightingMode", "border") != "disco":
            self.disco_levels = []
            return
        count = len(self.colors)
        if not state.get("edgeSwirl", False):
            self.disco_levels = [1.0] * count
            return
        if len(self.disco_levels) != count:
            self.disco_levels = [0.0] * count
        bars = state.get("bars") or []
        playing = (state.get("track") or {}).get("status") == "playing"
        fade = float(state.get("edgeFade", 50)) / 100
        for index in range(count):
            start = index * len(bars) // count
            end = max(start + 1, (index + 1) * len(bars) // count)
            band = bars[start:end] if playing else []
            level = sum(band) / len(band) if band else 0
            if playing:
                level = .3 + .7 * min(1, max(0, level))
            decay = (.18 + fade * .62) * (1 + index * .16)
            self.disco_levels[index] = max(
                min(1, max(0, level)),
                self.disco_levels[index] * math.exp(-max(0, min(.25, elapsed)) / decay),
            )

    def paint_disco(self, painter, state):
        if not self.disco_levels:
            return
        width, height = self.width(), self.height()
        fade = float(state.get("edgeFade", 50)) / 100
        radius = min(width, height) * .18 * float(state.get("discoLightSize", 100)) / 100
        colors = self.adjusted_colors(state)
        count = int(state.get("discoLightCount", 16))
        style = state.get("discoLightStyle", "glow")
        if style != "glow":
            self.paint_festive_lights(painter, state, colors, radius, count, style)
            return
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        for index, (x, y) in enumerate(disco_light_positions(width, height, count)):
            color_index = index % len(colors)
            level = self.disco_levels[color_index]
            if level <= .001:
                continue
            painter.save()
            painter.translate(x, y)
            painter.setOpacity(level)
            color = colors[color_index]
            glow = QRadialGradient(0, 0, radius)
            for position in (0, .45, .5, .625, .75, .875, 1):
                stop = QColor(color)
                strength = min(1, (1 - position) / .55) ** (2.4 - 1.8 * fade)
                stop.setAlphaF(color.alphaF() * strength)
                glow.setColorAt(position, stop)
            painter.setBrush(glow)
            painter.drawEllipse(QRectF(-radius, -radius, 2 * radius, 2 * radius))
            painter.restore()

    def paint_festive_lights(self, painter, state, colors, radius, count, style):
        width, height = self.width(), self.height()
        unit = radius * .14
        wire_path, positions = festive_strand_geometry(width, height, count, unit)
        fade = float(state.get("edgeFade", 50)) / 100
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        for index, (x, y, angle) in enumerate(positions):
            level = self.disco_levels[index % len(colors)]
            if level <= .001:
                continue
            painter.save()
            painter.translate(x, y)
            painter.rotate(angle)
            color = colors[index % len(colors)]
            glow = QRadialGradient(0, unit * 1.3, radius * .55)
            for position in (0, .5, 1):
                stop = QColor(color)
                stop.setAlphaF(color.alphaF() * (1 - position) ** (2.4 - 1.8 * fade) * .55)
                glow.setColorAt(position, stop)
            painter.setBrush(glow)
            painter.setOpacity(level)
            painter.drawEllipse(QRectF(-radius * .55, unit * 1.3 - radius * .55,
                                       radius * 1.1, radius * 1.1))
            painter.restore()

        wire_color_mode = state.get("discoWireColor", "green")
        if wire_color_mode != "none":
            if wire_color_mode == "theme":
                wire = QConicalGradient(width / 2, height / 2, 0)
                for index, color in enumerate(colors + [colors[0]]):
                    wire.setColorAt(index / len(colors), color)
                pen = QPen(wire, max(1.0, unit * .15))
            else:
                wire_color = QColor("#417a4c")
                wire_color.setAlphaF(self.opacity * .85)
                pen = QPen(wire_color, max(1.0, unit * .15))
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(wire_path)

        for index, (x, y, angle) in enumerate(positions):
            level = self.disco_levels[index % len(colors)]
            painter.save()
            painter.translate(x, y)
            painter.rotate(angle)
            painter.scale(unit, unit)
            painter.setPen(Qt.PenStyle.NoPen)
            if wire_color_mode != "none":
                socket = QColor("#486753")
                socket.setAlphaF(self.opacity * .9)
                painter.setBrush(socket)
                painter.drawRoundedRect(QRectF(-.35, .1, .7, .55), .1, .1)
            if level > .001:
                color = colors[index % len(colors)]
                gradient = QLinearGradient(-.7, .5, .7, 2.2)
                gradient.setColorAt(0, color.lighter(160))
                gradient.setColorAt(.5, color)
                gradient.setColorAt(1, color.darker(150))
                painter.setOpacity(level)
                painter.setBrush(gradient)
                if style == "fairy":
                    painter.drawEllipse(QRectF(-.65, .55, 1.3, 1.3))
                elif style == "bulb":
                    path = QPainterPath()
                    path.moveTo(-.45, .55)
                    path.cubicTo(-1.1, 1.1, -.8, 2.0, 0, 2.55)
                    path.cubicTo(.8, 2.0, 1.1, 1.1, .45, .55)
                    path.closeSubpath()
                    painter.drawPath(path)
                elif style == "star":
                    path = QPainterPath()
                    for point in range(10):
                        angle = -math.pi / 2 + point * math.pi / 5
                        distance = .85 if point % 2 == 0 else .38
                        px, py = distance * math.cos(angle), 1.4 + distance * math.sin(angle)
                        if point == 0:
                            path.moveTo(px, py)
                        else:
                            path.lineTo(px, py)
                    path.closeSubpath()
                    painter.drawPath(path)
            painter.restore()
        painter.restore()

    def paint_border(self, painter, state):
        mode = state.get("lightingMode", "border")
        if mode == "none":
            return
        if mode == "disco":
            self.paint_disco(painter, state)
            return
        if int(state.get("visualizerMode", 0)) == 5 and not state.get("edgeSwirl"):
            return
        fade = float(state.get("edgeFade", 50)) / 100
        depth = float(state.get("edgeDepth", 100)) / 100
        transparent = QColor(0, 0, 0, 0)
        painter.setPen(Qt.PenStyle.NoPen)
        image = QImage(self.size(), QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        layer = QPainter(image)
        angle = self.angle if state.get("edgeSwirl") else 0
        gradient = QConicalGradient(self.width() / 2, self.height() / 2, angle)
        palette = self.border_palette(state)
        for index, color in enumerate(palette + [palette[0]]):
            gradient.setColorAt(index / len(palette), color)
        layer.fillRect(self.rect(), gradient)
        layer.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
        mask = QRadialGradient(self.width() / 2, self.height() / 2,
                               max(1, min(self.width(), self.height())) / 2)
        mask.setColorAt(max(0.01, 1 - (0.32 + fade * 0.33) * depth), transparent)
        mask.setColorAt(1, QColor("white"))
        layer.fillRect(self.rect(), mask)
        layer.end()
        painter.drawImage(0, 0, image)


class VisualizerRenderer:
    def __init__(self):
        super().__init__()
        self.state = {"track": {}, "bars": [], "wave": [], "visualizerMode": 0}
        self.art_pixmap = None
        self.track_palette = None
        self.logo_palette = None
        self.background_cache = None
        self.background_palette = None
        self.background_size = None
        self.christmas_background = {
            "base": QColor("#07150e"),
            "background": [QColor("#1a3b24"), QColor("#492024"),
                           QColor("#173d28"), QColor("#3a1c1b"), QColor("#233421")],
        }
        self.halo_fade_key = None
        self.halo_fade_texture = None
        self.peaks = []
        self.smoothed_bars = []
        self.smoothed_wave = []
        self.last_frame = time.monotonic()
        self.vortex_beat = 0.0
        self.previous_bass = 0.0
        self.last_beat = 0.0
        self.beat_intervals = []
        self.vortex_pitch = 0.5
        self.frame_timer = QTimer(self)
        self.frame_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.frame_timer.setInterval(16)
        self.frame_timer.timeout.connect(self.update_scope)
        icon_path = APP_ICON_PATH
        self.fallback_pixmap = QPixmap(icon_path)
        if self.fallback_pixmap.isNull():
            self.fallback_pixmap = None
        self.setMinimumSize(640, 420)

    def set_state(self, state):
        bars = state.get("bars") or []
        now = time.monotonic()
        if (state.get("track") or {}).get("status") == "playing" and bars:
            bass = sum(bars[:4]) / min(4, len(bars))
            rise = bass - self.previous_bass
            if bass > 0.08 and rise > 0.03 and now - self.last_beat > 0.18:
                self.vortex_beat = max(self.vortex_beat, min(1, 0.3 + rise * 4))
                interval = now - self.last_beat
                if self.last_beat and .3 <= interval <= 1.5:
                    self.beat_intervals = (self.beat_intervals + [interval])[-4:]
                else:
                    self.beat_intervals = []
                self.last_beat = now
            elif now - self.last_beat > 2.5:
                self.beat_intervals = []
            self.previous_bass = bass
            total = sum(bars)
            if total:
                self.vortex_pitch = sum(i * value for i, value in enumerate(bars)) / (
                    total * max(1, len(bars) - 1)
                )
        else:
            self.previous_bass = 0
            self.vortex_beat = 0
            self.last_beat = 0
            self.beat_intervals = []
        count_changed = state.get("edgeColorCount", 8) != self.state.get("edgeColorCount", 8)
        mode_changed = state.get("artworkMode", "album") != self.state.get("artworkMode", "album")
        self.state = state
        if count_changed:
            if self.art_pixmap is not None and self.track_palette is not None:
                self.track_palette = self.sample_palette(self.art_pixmap)
            self.logo_palette = None
        if (mode_changed or count_changed) and state.get("artworkMode", "album") == "logo":
            self.update_logo_palette()
        self.update_frame_timer()
        self.update()

    def update_frame_timer(self):
        track = self.state.get("track") or {}
        if (self.isVisible() and self.state.get("visualizerMode", 0) in (1, 2)
                and track.get("status") == "playing"):
            if not self.frame_timer.isActive():
                self.frame_timer.start()
        else:
            self.frame_timer.stop()

    def update_scope(self):
        if self.state.get("visualizerMode", 0) == 2:
            self.update()
            return
        layout_bottom = self.height() - 112
        layout_height = max(180, layout_bottom - 72)
        self.update(QRect(24, 72, max(1, self.width() - 48), layout_height))

    def showEvent(self, event):
        super().showEvent(event)
        self.update_frame_timer()

    def hideEvent(self, event):
        self.frame_timer.stop()
        super().hideEvent(event)

    def set_art(self, data):
        pixmap = QPixmap()
        pixmap.loadFromData(data)
        self.art_pixmap = pixmap if not pixmap.isNull() else None
        self.track_palette = self.sample_palette(self.art_pixmap) if self.art_pixmap else None
        self.update()

    def clear_art(self):
        self.art_pixmap = self.fallback_pixmap
        self.track_palette = None
        self.update()

    def update_logo_palette(self):
        if self.logo_palette is None and self.fallback_pixmap is not None:
            self.logo_palette = self.sample_palette(self.fallback_pixmap)

    def active_palette(self):
        mode = self.state.get("artworkMode", "album")
        if mode == "none":
            return None
        if mode == "logo":
            return self.logo_palette
        return self.track_palette

    def effect_palette(self):
        mode = self.state.get("lightingColorMode", "artwork")
        if mode == "christmas":
            return {"colors": [QColor(color) for color in CHRISTMAS_COLORS]}
        if mode == "rainbow":
            count = int(self.state.get("edgeColorCount", 8))
            return {"colors": [QColor.fromHslF(index / count, 1, .5)
                               for index in (0, count // 3, 2 * count // 3)]}
        return self.active_palette()

    def sample_palette(self, pixmap):
        image = pixmap.toImage().scaled(
            24, 24, Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation
        )
        bins = {}
        totals = [0.0, 0.0, 0.0, 0.0]
        for y in range(image.height()):
            for x in range(image.width()):
                color = image.pixelColor(x, y)
                alpha = color.alphaF()
                if alpha < 0.5:
                    continue
                rgb = (color.red(), color.green(), color.blue())
                weight = alpha * max(0.08, (max(rgb) - min(rgb)) / 255)
                key = tuple(channel >> 4 for channel in rgb)
                bin_values = bins.setdefault(key, [0.0, 0.0, 0.0, 0.0])
                for index, channel in enumerate(rgb):
                    bin_values[index] += channel * weight
                    totals[index] += channel * weight
                bin_values[3] += weight
                totals[3] += weight
        if not totals[3]:
            return None
        average = QColor(*(round(value / totals[3]) for value in totals[:3]))
        hue, saturation, _, _ = average.getHslF()
        hue = max(0, hue)
        vividness = 0 if saturation < 0.08 else min(0.9, max(0.42, saturation * 1.3))
        candidates = sorted(bins.values(), key=lambda values: values[3], reverse=True)

        def distinct_colors(distance, count):
            selected = []
            for candidate in candidates:
                color = QColor(*(round(value / candidate[3]) for value in candidate[:3]))
                if all(math.dist((color.red(), color.green(), color.blue()),
                                 (other.red(), other.green(), other.blue())) >= distance
                       for other in selected):
                    selected.append(color)
                if len(selected) == count:
                    break
            return selected or [average]

        luminance = (0.2126 * average.redF() + 0.7152 * average.greenF()
                     + 0.0722 * average.blueF())
        lightness = 0.11 if luminance > 0.55 else 0.19 if luminance < 0.18 else 0.14
        return {
            "colors": [QColor.fromHslF(hue, vividness, lightness / 100)
                       for lightness in (32, 52, 72)],
            "edge": distinct_colors(70, int(self.state.get("edgeColorCount", 8))),
            "background": distinct_colors(45, 5),
            "base": QColor.fromHslF((hue + 0.5) % 1, max(0.28, vividness * 0.38), lightness),
        }

    def paint_background(self, painter, width, height):
        color_mode = self.state.get("lightingColorMode")
        palette = (None if color_mode == "rainbow" else
                   self.christmas_background if color_mode == "christmas" else
                   self.active_palette())
        size = (width, height, self.devicePixelRatioF())
        if (self.background_cache is not None and self.background_size == size
                and self.background_palette is palette):
            painter.drawPixmap(0, 0, self.background_cache)
            return
        ratio = self.devicePixelRatioF()
        background = QPixmap(math.ceil(width * ratio), math.ceil(height * ratio))
        background.setDevicePixelRatio(ratio)
        background.fill(palette["base"] if palette else QColor("black"))
        if palette:
            background_painter = QPainter(background)
            positions = ((.5, .42), (.12, .15), (.88, .18), (.18, .88), (.82, .82))
            alphas = (.72, .58, .52, .46, .42)
            for index in reversed(range(len(palette["background"]))):
                color = QColor(palette["background"][index])
                color.setAlphaF(alphas[index])
                x, y = positions[index]
                cx, cy = x * width, y * height
                if index == 0:
                    radius_x = max(cx, width - cx) * math.sqrt(2) * .58
                    radius_y = max(cy, height - cy) * math.sqrt(2) * .58
                else:
                    radius_x = radius_y = math.hypot(
                        max(cx, width - cx), max(cy, height - cy)
                    ) * .58
                background_painter.save()
                background_painter.translate(cx, cy)
                background_painter.scale(radius_x, radius_y)
                glow = QRadialGradient(0, 0, 1)
                glow.setColorAt(0, color)
                glow.setColorAt(1, QColor(0, 0, 0, 0))
                background_painter.fillRect(
                    QRectF(-cx / radius_x, -cy / radius_y,
                           width / radius_x, height / radius_y), glow
                )
                background_painter.restore()
            background_painter.end()
        else:
            background_painter = QPainter(background)
            cx, cy = width * .5, height * .42
            radius_x = max(cx, width - cx) * math.sqrt(2)
            radius_y = max(cy, height - cy) * math.sqrt(2)
            background_painter.translate(cx, cy)
            background_painter.scale(radius_x, radius_y)
            gradient = QRadialGradient(0, 0, 1)
            gradient.setColorAt(0, QColor("#00105c"))
            gradient.setColorAt(1, QColor("black"))
            background_painter.fillRect(
                QRectF(-cx / radius_x, -cy / radius_y,
                       width / radius_x, height / radius_y), gradient
            )
            background_painter.end()
        self.background_cache = background
        self.background_palette = palette
        self.background_size = size
        painter.drawPixmap(0, 0, background)

    def halo_fade_artwork(self, pixmap, source):
        key = pixmap.cacheKey()
        if self.halo_fade_key != key:
            image = QImage(512, 512, QImage.Format.Format_ARGB32_Premultiplied)
            image.fill(Qt.GlobalColor.transparent)
            painter = QPainter(image)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.drawPixmap(QRectF(0, 0, image.width(), image.height()), pixmap, source)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
            painter.translate(image.width() / 2, image.height() / 2)
            painter.scale(image.width() / 2, image.height() / 2)
            mask = QRadialGradient(0, 0, 1)
            mask.setColorAt(0, QColor(255, 255, 255, 255))
            mask.setColorAt(.6, QColor(255, 255, 255, 255))
            mask.setColorAt(.8, QColor(255, 255, 255, 210))
            mask.setColorAt(1, QColor(255, 255, 255, 0))
            painter.fillRect(QRectF(-1, -1, 2, 2), mask)
            painter.end()
            self.halo_fade_texture = QPixmap.fromImage(image)
            self.halo_fade_key = key
        return self.halo_fade_texture

    def halo_layout(self):
        width, height = self.width(), self.height()
        center_y = height / 2
        window = self.window()
        overlay = getattr(window, "track_title", None)
        if overlay is not None:
            bottom = self.mapFromGlobal(overlay.mapToGlobal(QPoint(0, 0))).y() - 8
        else:
            bottom = height - 112
        half_height = max(1, min(center_y - 72, bottom - center_y))
        return bottom, min(max(1, width - 48), 2 * half_height)

    def paint_scene(self, painter):
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        width = self.width()
        height = self.height()
        layout_top = 72
        layout_bottom = height - 112
        layout_height = max(180, layout_bottom - layout_top)
        layout_center_y = layout_top + layout_height * 0.5
        layout_left = 24
        layout_width = max(1, width - layout_left * 2)
        selected_mode = int(self.state.get("visualizerMode", 0))
        qt_mode = {0: 0, 1: 1, 2: 4, 4: 2, 5: 3}.get(selected_mode, 0)
        halo_bottom, halo_size = (
            self.halo_layout() if qt_mode == 4 else
            (layout_top + layout_height, min(layout_width, layout_height))
        )
        halo_radius = halo_size * .23

        self.paint_background(painter, width, height)
        active_palette = self.effect_palette()

        status = str((self.state.get("track") or {}).get("status", ""))
        playing = status == "playing"
        bars = list(self.state.get("bars") or []) if playing else []
        wave = list(self.state.get("wave") or []) if playing else []
        now = time.monotonic()
        elapsed = max(0, min(now - self.last_frame, .1))
        self.last_frame = now
        self.vortex_beat = max(0, self.vortex_beat - elapsed * 2.8) if playing else 0
        if len(self.smoothed_bars) != len(bars):
            self.smoothed_bars = bars.copy()
        else:
            smoothing = min(1, elapsed * 12)
            self.smoothed_bars = [current + (target - current) * smoothing
                                  for current, target in zip(self.smoothed_bars, bars)]
        if len(self.smoothed_wave) != len(wave):
            self.smoothed_wave = wave.copy()
        else:
            smoothing = min(1, elapsed * 14)
            self.smoothed_wave = [current + (target - current) * smoothing
                                  for current, target in zip(self.smoothed_wave, wave)]
        bars, wave = self.smoothed_bars, self.smoothed_wave
        halo_bass = sum(bars[:3]) / min(3, len(bars)) if bars else 0
        halo_outer_radius = min(halo_size * .48, halo_radius * (1.35 + halo_bass))
        if len(self.peaks) != len(bars):
            self.peaks = bars.copy()
        else:
            self.peaks = [max(value, peak - elapsed * .75)
                          for value, peak in zip(bars, self.peaks)]

        artwork_mode = self.state.get("artworkMode", "album")
        display_pixmap = (self.fallback_pixmap if artwork_mode == "logo" else
                          None if artwork_mode == "none" else
                          self.art_pixmap or self.fallback_pixmap)
        if display_pixmap:
            if qt_mode == 4:
                art_width = art_height = halo_radius * 2
            else:
                art_width = art_height = min(width, height) * (0.64 if qt_mode == 3 else 0.38)
            center_y = height / 2
            art_rect = QRectF((width - art_width) / 2, center_y - art_height / 2,
                              art_width, art_height)
            crop_width = min(display_pixmap.width(), display_pixmap.height() * art_width / art_height)
            crop_height = crop_width * art_height / art_width
            source = QRectF((display_pixmap.width() - crop_width) / 2,
                            (display_pixmap.height() - crop_height) / 2,
                            crop_width, crop_height)
            if qt_mode == 4 and self.state.get("albumArtOpacity", 22):
                fade_rect = QRectF(width / 2 - halo_outer_radius,
                                   center_y - halo_outer_radius,
                                   halo_outer_radius * 2, halo_outer_radius * 2)
                painter.save()
                painter.setOpacity(float(self.state.get("albumArtOpacity", 22)) / 100)
                painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
                painter.drawPixmap(fade_rect, self.halo_fade_artwork(display_pixmap, source),
                                   QRectF(0, 0, 512, 512))
                painter.restore()
            if qt_mode != 4:
                painter.save()
                clip = QPainterPath()
                clip.addRoundedRect(art_rect, art_width * 0.14, art_width * 0.14)
                painter.setClipPath(clip)
                painter.setOpacity(
                    1 if qt_mode == 3 and (self.art_pixmap or artwork_mode == "logo") else
                    float(self.state.get("albumArtOpacity", 22)) / 100
                )
                painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
                painter.drawPixmap(art_rect, display_pixmap, source)
                painter.restore()

        if qt_mode == 3:
            painter.end()
            return

        painter.save()
        clip_height = max(1, halo_bottom - layout_top) if qt_mode == 4 else layout_height
        painter.setClipRect(QRectF(layout_left, layout_top, layout_width, clip_height))
        if qt_mode == 0:
            if bars:
                gap = max(2, width * 0.004)
                bar_width = (width * .84 - gap * (len(bars) - 1)) / len(bars)
                palette = (
                    active_palette["colors"]
                    if active_palette
                    else [QColor("#1b5bff"), QColor("#6a70ff"), QColor("#b46cff")]
                )
                gradient = QLinearGradient(0, height, 0, 0)
                for position, color in zip((0, .55, 1), palette):
                    gradient.setColorAt(position, color)
                for index, value in enumerate(bars):
                    bar_height = max(2, float(value) * layout_height * 0.58)
                    x = width * .08 + index * (bar_width + gap)
                    painter.setOpacity(.72)
                    painter.setBrush(gradient)
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.drawRect(QRectF(x, layout_bottom - bar_height, bar_width, bar_height))
                    painter.setOpacity(1)
                    peak_color = (palette[-1] if self.state.get("lightingColorMode")
                                  in ("rainbow", "christmas") else QColor("#80d8ff"))
                    painter.fillRect(QRectF(x, layout_bottom - self.peaks[index] * layout_height * .58 - 1,
                                            bar_width, 2), peak_color)

        elif qt_mode == 1:
            gradient = scope_gradient(width, active_palette)
            if self.state.get("scopeZeroLine", True):
                painter.setOpacity(.78)
                painter.setPen(QPen(gradient, 1.25))
                painter.drawLine(int(width * .08), int(layout_center_y),
                                 int(width * .92), int(layout_center_y))
            if len(wave) > 1:
                scale = 1 if isinstance(self, QOpenGLWidget) else min(1.0, 960 / width)
                if scale < 1:
                    image = QImage(math.ceil(width * scale), math.ceil(height * scale),
                                   QImage.Format.Format_ARGB32_Premultiplied)
                    image.fill(Qt.GlobalColor.transparent)
                    scope_painter = QPainter(image)
                    scope_painter.scale(scale, scale)
                else:
                    scope_painter = painter
                # Qt's antialiased path rasterizer is slow for self-intersecting waves.
                scope_painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
                path = QPainterPath()
                for index, value in enumerate(wave):
                    x = width * .08 + (index / (len(wave) - 1)) * width * .84
                    y = layout_center_y - float(value) * layout_height * 0.46
                    if index == 0:
                        path.moveTo(x, y)
                    else:
                        path.lineTo(x, y)
                paint_scope_path(scope_painter, path, gradient,
                                 3.5 * self.state.get("scopeLineWidth", 100) / 100)
                if scale < 1:
                    scope_painter.end()
                    painter.setOpacity(1)
                    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
                    painter.drawImage(QRectF(0, 0, width, height), image,
                                      QRectF(0, 0, image.width(), image.height()))

        elif qt_mode == 4:
            cx, cy = width / 2, height / 2
            base_radius = halo_radius
            if not display_pixmap:
                glow = QRadialGradient(cx, cy, halo_outer_radius)
                if active_palette:
                    glow.setColorAt(0, active_palette["colors"][2])
                    glow.setColorAt(.6, active_palette["colors"][0])
                else:
                    glow.setColorAt(0, QColor(0, 176, 255, 217))
                    glow.setColorAt(.6, QColor(124, 77, 255, 89))
                glow.setColorAt(1, QColor(0, 0, 0, 0))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(glow)
                painter.drawEllipse(QRectF(cx - halo_outer_radius, cy - halo_outer_radius,
                                           halo_outer_radius * 2, halo_outer_radius * 2))
            if bars:
                path = QPainterPath()
                for index, value in enumerate(bars):
                    angle = index / len(bars) * math.tau - math.pi / 2
                    ring_radius = base_radius + halo_size * (value * .18 + halo_bass * .04)
                    x = cx + math.cos(angle) * ring_radius
                    y = cy + math.sin(angle) * ring_radius
                    if index == 0:
                        path.moveTo(x, y)
                    else:
                        path.lineTo(x, y)
                path.closeSubpath()
                paint_scope_path(painter, path, scope_gradient(width, active_palette),
                                 3.5 * self.state.get("scopeLineWidth", 100) / 100)

        elif qt_mode == 2:
            palette = (
                active_palette["colors"]
                if active_palette
                else [QColor("#1b5bff"), QColor("#6a70ff"), QColor("#b46cff")]
            )
            energy = sum(bars) / len(bars) if bars else 0
            sound_pulse = min(1, energy * 3.5 + self.vortex_beat * .55)
            pulse = 1 + sound_pulse * .28
            size = min(width, height)
            pitch_phase = now * .24 + (self.vortex_pitch - .5) * math.tau
            cx = (width / 2 + math.cos(pitch_phase) * width * (.055 + self.vortex_pitch * .045)
                  + math.cos(now * 3.4) * width * .045 * self.vortex_beat)
            cy = (layout_center_y + math.sin(pitch_phase) * height * (.035 + (1 - self.vortex_pitch) * .065)
                  + math.sin(now * 3.4) * height * .05 * self.vortex_beat)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Screen)
            for ray in range(12):
                angle = ray / 12 * math.tau + now * .035
                end_x = cx + math.cos(angle) * size * .72
                end_y = cy + math.sin(angle) * size * .72
                gradient = QLinearGradient(cx, cy, end_x, end_y)
                gradient.setColorAt(0, palette[ray % len(palette)])
                gradient.setColorAt(1, QColor(0, 0, 0, 0))
                painter.setOpacity(.08 + sound_pulse * .08)
                painter.setPen(QPen(gradient, 1 + sound_pulse * 1.5))
                painter.drawLine(QRectF(cx, cy, end_x - cx, end_y - cy).topLeft(),
                                 QRectF(cx, cy, end_x - cx, end_y - cy).bottomRight())
            painter.setBrush(Qt.BrushStyle.NoBrush)
            for ring in range(24):
                depth = (ring / 24 + now * .12) % 1
                radius = size * (.025 + depth * .49) * pulse
                twist = now * .18 + depth * math.pi * 1.7
                path = QPainterPath()
                for segment in range(97):
                    angle = segment / 96 * math.tau
                    ripple = (1 + math.sin(angle * 4 + now * 1.1 + depth * 9)
                              * (.018 + sound_pulse * .04)
                              + math.sin(angle * 7 - now * .65 + depth * 4) * .012)
                    rotated = angle + twist + math.sin(angle * 2 + now + depth) * .045
                    x = cx + math.cos(rotated) * radius * 1.28 * ripple
                    y = cy + math.sin(rotated) * radius * .82 * ripple
                    if segment == 0:
                        path.moveTo(x, y)
                    else:
                        path.lineTo(x, y)
                path.closeSubpath()
                fade = (1 - depth) ** 1.45
                painter.setOpacity((.48 + sound_pulse * .3) * fade)
                painter.setPen(QPen(palette[(ring + int(now / 1.2)) % len(palette)],
                                    (.7 + sound_pulse * 1.8 + (1 - depth)) * (.3 + fade * .7)))
                painter.drawPath(path)
            core_radius = size * (.025 + sound_pulse * .035) * 3
            glow = QRadialGradient(cx, cy, core_radius)
            glow.setColorAt(0, palette[2])
            glow.setColorAt(1, QColor(0, 0, 0, 0))
            painter.setOpacity(.2 + sound_pulse * .35)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(glow)
            painter.drawEllipse(QRectF(cx - core_radius, cy - core_radius,
                                       core_radius * 2, core_radius * 2))
        painter.restore()
        painter.end()


class VisualizerWidget(VisualizerRenderer, QWidget):
    def paintEvent(self, event):
        self.paint_scene(QPainter(self))


class GPUVisualizerWidget(VisualizerRenderer, QOpenGLWidget):
    def __init__(self):
        super().__init__()
        self.border_shader = None
        self.initialized = False

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(2000, self.check_context)

    def check_context(self):
        if self.isVisible() and not self.initialized:
            self.window().fallback_visualizer("OpenGL widget failed to initialize")

    def paintEvent(self, event):
        QOpenGLWidget.paintEvent(self, event)

    def initializeGL(self):
        try:
            self.border_shader = BorderShader()
        except RuntimeError as error:
            QTimer.singleShot(0, lambda message=str(error): self.window().fallback_visualizer(message))
            return
        self.context().aboutToBeDestroyed.connect(self.cleanup_gl)
        self.initialized = True

    def cleanup_gl(self):
        self.makeCurrent()
        self.border_shader = None
        self.doneCurrent()
        self.initialized = False

    def paintGL(self):
        self.paint_scene(QPainter(self))
        window = self.window()
        if not self.initialized or not hasattr(window, "edge_border"):
            return
        border = window.edge_border
        state = window.state
        if (state.get("lightingMode", "border") == "border"
                and (state.get("edgeSwirl") or int(state.get("visualizerMode", 0)) != 5)):
            try:
                self.border_shader.draw(
                    self.width(), self.height(), self.devicePixelRatioF(), border, state
                )
            except RuntimeError as error:
                QTimer.singleShot(
                    0, lambda message=str(error): self.window().fallback_visualizer(message)
                )
        else:
            painter = QPainter(self)
            border.paint_border(painter, state)
            painter.end()


class AmbiBeatQtWindow(QMainWindow):
    """Present backend state and route user actions to the backend."""

    def line_icon(self, kind):
        pixmap = QPixmap(32, 32)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor("#f5f8ff"))
        pen.setWidthF(2.2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(QColor("#f5f8ff"))

        if kind == "previous":
            painter.drawLine(9, 7, 9, 25)
            painter.drawPolygon(QPolygonF([
                QRectF(12, 16, 0, 0).topLeft(),
                QRectF(23, 7, 0, 0).topLeft(),
                QRectF(23, 25, 0, 0).topLeft(),
            ]))
        elif kind == "next":
            painter.drawLine(23, 7, 23, 25)
            painter.drawPolygon(QPolygonF([
                QRectF(9, 7, 0, 0).topLeft(),
                QRectF(9, 25, 0, 0).topLeft(),
                QRectF(20, 16, 0, 0).topLeft(),
            ]))
        elif kind == "play":
            painter.drawPolygon(QPolygonF([
                QRectF(11, 7, 0, 0).topLeft(),
                QRectF(11, 25, 0, 0).topLeft(),
                QRectF(24, 16, 0, 0).topLeft(),
            ]))
        elif kind == "pause":
            painter.drawRoundedRect(8, 7, 6, 18, 1, 1)
            painter.drawRoundedRect(18, 7, 6, 18, 1, 1)
        elif kind == "volume":
            painter.drawPolygon(QPolygonF([
                QRectF(7, 13, 0, 0).topLeft(),
                QRectF(12, 13, 0, 0).topLeft(),
                QRectF(18, 8, 0, 0).topLeft(),
                QRectF(18, 24, 0, 0).topLeft(),
                QRectF(12, 19, 0, 0).topLeft(),
                QRectF(7, 19, 0, 0).topLeft(),
            ]))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawArc(19, 10, 8, 12, -45 * 16, 90 * 16)
        elif kind == "settings":
            painter.scale(4 / 3, 4 / 3)
            cog = QPainterPath()
            cog.moveTo(8.94, 4.61)
            for x, y in (
                (9.41, 2.34), (14.59, 2.34), (15.06, 4.61), (17, 3.34),
                (20.66, 7), (19.39, 8.94), (21.66, 9.41), (21.66, 14.59),
                (19.39, 15.06), (20.66, 17), (17, 20.66), (15.06, 19.39),
                (14.59, 21.66), (9.41, 21.66), (8.94, 19.39), (7, 20.66),
                (3.34, 17), (4.61, 15.06), (2.34, 14.59), (2.34, 9.41),
                (4.61, 8.94), (3.34, 7), (7, 3.34),
            ):
                cog.lineTo(x, y)
            cog.closeSubpath()
            cog.addEllipse(QRectF(8.8, 8.8, 6.4, 6.4))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawPath(cog)
        elif kind == "mode":
            painter.setBrush(QColor("#f5f8ff"))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(5, 12, 4, 12, 1, 1)
            painter.drawRoundedRect(11, 7, 4, 17, 1, 1)
            painter.drawRoundedRect(17, 15, 4, 9, 1, 1)
            painter.drawRoundedRect(23, 9, 4, 15, 1, 1)
        elif kind == "source":
            painter.setBrush(QColor("#f5f8ff"))
            painter.drawLine(13, 9, 13, 23)
            painter.drawLine(13, 9, 23, 6)
            painter.drawLine(23, 6, 23, 20)
            painter.drawEllipse(QRectF(8, 21, 5, 4))
            painter.drawEllipse(QRectF(18, 18, 5, 4))
        elif kind == "fullscreen":
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawLine(7, 12, 7, 7)
            painter.drawLine(7, 7, 12, 7)
            painter.drawLine(20, 7, 25, 7)
            painter.drawLine(25, 7, 25, 12)
            painter.drawLine(25, 20, 25, 25)
            painter.drawLine(25, 25, 20, 25)
            painter.drawLine(12, 25, 7, 25)
            painter.drawLine(7, 25, 7, 20)
        painter.end()
        return QIcon(pixmap)

    def __init__(self, backend):
        super().__init__()
        self.backend = backend
        self.setWindowTitle("AmbiBeat")
        self.resize(1100, 720)
        icon_path = APP_ICON_PATH
        if os.path.isfile(icon_path):
            self.setWindowIcon(QIcon(icon_path))

        self.setStyleSheet(
            """
            QMainWindow {
                background: #090b12;
            }
            QWidget {
                color: #ffffff;
                font-family: system-ui, sans-serif;
            }
            QToolTip {
                background: #111522;
                color: #ffffff;
                border: 1px solid rgba(255,255,255,70);
                border-radius: 6px;
                padding: 6px;
            }
            QComboBox {
                min-width: 258px;
                min-height: 42px;
                border: 1px solid rgba(255,255,255,90);
                border-radius: 8px;
                padding: 0 10px;
                background: rgba(9,11,18,220);
                color: #ffffff;
            }
            QPushButton, QToolButton {
                min-width: 46px;
                min-height: 46px;
                qproperty-iconSize: 20px 20px;
                border: 1px solid rgba(255,255,255,102);
                border-radius: 24px;
                background: rgba(9,11,18,187);
                color: #ffffff;
                font-size: 13px;
            }
            QPushButton:hover, QToolButton:hover {
                background: rgba(255,255,255,18);
            }
            """
        )
        self.control_style = self.styleSheet()
        self.control_border_color = None

        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        available, reason = gpu_available()
        if not available:
            print(f"Qt GPU rendering unavailable ({reason}); using CPU rendering.", file=sys.stderr)
        self.visualizer = GPUVisualizerWidget() if available else VisualizerWidget()
        self.visualizer.setParent(central)
        self.visualizer.lower()
        root.addStretch()

        self.track_title = QLabel("Waiting for music...")
        self.track_artist = QLabel("")
        self.track_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.track_artist.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.track_title.setStyleSheet("background: transparent; font-size: 28px; font-weight: 700;")
        self.track_artist.setStyleSheet(
            "background: transparent; color: rgba(255,255,255,102); font-size: 18px;"
        )
        artist_shadow = QGraphicsDropShadowEffect(self.track_artist)
        artist_shadow.setColor(QColor(0, 0, 0, 230))
        artist_shadow.setBlurRadius(6)
        artist_shadow.setOffset(0, 1)
        self.track_artist.setGraphicsEffect(artist_shadow)
        self.track_title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.track_artist.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.track_title.setParent(central)
        self.track_artist.setParent(central)
        self.track_title.raise_()
        self.track_artist.raise_()

        controls = QHBoxLayout()
        controls.setContentsMargins(16, 0, 16, 16)
        controls.setSpacing(12)
        controls.addStretch()

        self.source_button = QToolButton()
        self.source_button.setAutoRaise(True)
        self.source_button.setIcon(self.line_icon("source"))
        self.source_button.setToolTip("Choose playback source")
        self.source_button.setAccessibleName("Choose playback source")
        self.source_button.clicked.connect(self.open_source_menu)

        self.mode_button = QToolButton()
        self.mode_button.setAutoRaise(True)
        self.mode_button.setIcon(self.line_icon("mode"))
        self.mode_button.setToolTip("Visual effects")
        self.mode_button.setAccessibleName("Visual effects")
        self.mode_button.clicked.connect(self.open_mode_menu)

        self.previous_button = QToolButton()
        self.previous_button.setAutoRaise(True)
        self.previous_button.setIcon(self.line_icon("previous"))
        self.previous_button.setToolTip("Previous track")
        self.previous_button.setAccessibleName("Previous track")

        self.play_button = QToolButton()
        self.play_button.setAutoRaise(True)
        self.play_button.setIcon(self.line_icon("pause"))
        self.play_button.setToolTip("Pause playback")
        self.play_button.setAccessibleName("Pause playback")

        self.next_button = QToolButton()
        self.next_button.setAutoRaise(True)
        self.next_button.setIcon(self.line_icon("next"))
        self.next_button.setToolTip("Next track")
        self.next_button.setAccessibleName("Next track")

        self.volume_button = QToolButton()
        self.volume_button.setAutoRaise(True)
        self.volume_button.setIcon(self.line_icon("volume"))
        self.volume_button.setToolTip("System volume")
        self.volume_button.setAccessibleName("System volume")

        self.settings_button = QToolButton()
        self.settings_button.setAutoRaise(True)
        self.settings_button.setIcon(self.line_icon("settings"))
        self.settings_button.setToolTip("Open settings")
        self.settings_button.setAccessibleName("Open settings")

        self.fullscreen_button = QToolButton()
        self.fullscreen_button.setAutoRaise(True)
        self.fullscreen_button.setIcon(self.line_icon("fullscreen"))
        self.fullscreen_button.setToolTip("Enter full screen")
        self.fullscreen_button.setAccessibleName("Enter full screen")

        controls.addWidget(self.source_button)
        controls.addWidget(self.mode_button)
        controls.addWidget(self.previous_button)
        controls.addWidget(self.play_button)
        controls.addWidget(self.next_button)
        controls.addWidget(self.volume_button)
        controls.addWidget(self.settings_button)
        controls.addWidget(self.fullscreen_button)
        controls.addStretch()
        controls_widget = QWidget()
        self.controls_container = controls_widget
        controls_widget.setObjectName("controlsContainer")
        controls_widget.setStyleSheet("QWidget#controlsContainer { background: transparent; }")
        controls_layout = QVBoxLayout(controls_widget)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.setSpacing(2)
        self.timeline_container = QWidget()
        timeline = QHBoxLayout(self.timeline_container)
        timeline.setContentsMargins(16, 0, 16, 0)
        timeline.addStretch()
        self.timeline_elapsed = QLabel("--:--")
        self.timeline_remaining = QLabel("--:--")
        self.timeline_slider = TimelineSlider(Qt.Orientation.Horizontal)
        self.timeline_slider.setAccessibleName("Track position")
        self.timeline_slider.setRange(0, 1)
        self.timeline_slider.setMaximumWidth(460)
        self.timeline_slider.setMinimumWidth(120)
        self._timeline_theme = None
        timeline.addWidget(self.timeline_elapsed)
        timeline.addWidget(self.timeline_slider, 1)
        timeline.addWidget(self.timeline_remaining)
        timeline.addStretch()
        controls_layout.addWidget(self.timeline_container)
        controls_layout.addLayout(controls)
        controls_widget.setContentsMargins(0, 0, 0, 0)
        self.controls_effect = QGraphicsOpacityEffect(controls_widget)
        controls_widget.setGraphicsEffect(self.controls_effect)
        root.addWidget(controls_widget)
        self.metadata_spacer = QSpacerItem(
            0, 0, QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed
        )
        root.addItem(self.metadata_spacer)

        self.edge_border = MovingEdgeBorder(self.visualizer)
        if isinstance(self.visualizer, GPUVisualizerWidget):
            self.edge_border.hide()
        self.edge_border.raise_()
        self.setCentralWidget(central)
        self.edge_border.setGeometry(self.visualizer.rect())

        self.source_prompt = QWidget(central)
        self.source_prompt.setObjectName("sourcePrompt")
        self.source_prompt.setAccessibleName("Playback source switch prompt")
        self.source_prompt.setStyleSheet("""
            QWidget#sourcePrompt {
                background: #101a35; border: 1px solid #718cc1; border-radius: 8px;
            }
            QLabel { background: transparent; color: white; font-size: 13px; border: 0; }
            QPushButton {
                min-width: 0; min-height: 0; padding: 8px 12px;
                border: 1px solid #718cc1; border-radius: 6px; background: #1b305a;
            }
            QPushButton:hover { background: #315590; }
        """)
        prompt_layout = QHBoxLayout(self.source_prompt)
        prompt_layout.setContentsMargins(12, 8, 12, 8)
        self.source_prompt_label = QLabel()
        self.source_prompt_label.setTextFormat(Qt.TextFormat.PlainText)
        self.source_prompt_label.setWordWrap(True)
        self.source_prompt_switch = QPushButton("Switch")
        self.source_prompt_dismiss = QPushButton("Keep current")
        prompt_layout.addWidget(self.source_prompt_label, 1)
        prompt_layout.addWidget(self.source_prompt_switch)
        prompt_layout.addWidget(self.source_prompt_dismiss)
        self.source_prompt.hide()
        self.source_prompt_timer = QTimer(self)
        self.source_prompt_timer.setSingleShot(True)
        self.source_prompt_timer.timeout.connect(self.dismiss_source_prompt)
        self.source_prompt_switch.clicked.connect(self.accept_source_prompt)
        self.source_prompt_dismiss.clicked.connect(self.dismiss_source_prompt)
        self._source_statuses = None
        self._source_prompt_id = None
        self._pending_source_prompts = []

        self.state = {}
        self._volume_slider = None
        self.update_timeline_theme(None)
        self._timeline_drag = None
        self._last_refresh_error = ""
        self.side_panel = None
        self.panel_animation = None
        self.panel_closing = False
        self.prefer_playing_source_on_start = True
        self.edge_last_update = time.monotonic()
        self.play_button.clicked.connect(
            lambda: self.media_control(
                "Play"
                if (self.state.get("track") or {}).get("status") != "playing"
                else "Pause"
            )
        )
        self.previous_button.clicked.connect(
            lambda: self.media_control("Previous")
        )
        self.next_button.clicked.connect(lambda: self.media_control("Next"))
        self.settings_button.clicked.connect(self.open_settings)
        self.fullscreen_button.clicked.connect(self.toggle_fullscreen)
        self.volume_button.clicked.connect(self.open_volume_menu)
        self.timeline_slider.sliderPressed.connect(self.start_timeline_drag)
        self.timeline_slider.sliderReleased.connect(self.finish_timeline_drag)
        self.timeline_slider.valueChanged.connect(self.timeline_value_changed)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_state)
        self.timer.start(50)
        self.refresh_state()
        self.backend.activate()

        self.cursor_timer = QTimer(self)
        self.cursor_timer.setSingleShot(True)
        self.cursor_timer.timeout.connect(self.hide_fullscreen_cursor)
        self.setMouseTracking(True)

        self.controls_timer = QTimer(self)
        self.controls_timer.setSingleShot(True)
        self.controls_timer.timeout.connect(self.hide_controls)
        self.show_controls()

        for widget in central.findChildren(QWidget) + [central]:
            widget.setMouseTracking(True)
            widget.installEventFilter(self)

    def fallback_visualizer(self, reason):
        old = self.visualizer
        if not isinstance(old, GPUVisualizerWidget):
            return
        print(f"Qt GPU rendering failed ({reason}); using CPU rendering.", file=sys.stderr)
        replacement = VisualizerWidget()
        for name in (
            "state", "art_pixmap", "track_palette", "logo_palette", "peaks",
            "smoothed_bars", "smoothed_wave", "last_frame", "vortex_beat",
            "previous_bass", "last_beat", "beat_intervals", "vortex_pitch",
        ):
            setattr(replacement, name, getattr(old, name))
        old.frame_timer.stop()
        replacement.setParent(self.centralWidget())
        replacement.setGeometry(old.geometry())
        replacement.setMouseTracking(True)
        replacement.installEventFilter(self)
        self.visualizer = replacement
        self.edge_border.setParent(replacement)
        self.edge_border.setGeometry(replacement.rect())
        self.edge_border.show()
        old.hide()
        old.deleteLater()
        replacement.lower()
        replacement.show()

    def eventFilter(self, watched, event):
        background = watched in (self.visualizer, self.centralWidget(), self.controls_container)
        in_source_prompt = isinstance(watched, QWidget) and (
            watched is self.source_prompt or self.source_prompt.isAncestorOf(watched)
        )
        if (event.type() == QEvent.Type.MouseButtonPress
                and self.side_panel is not None
                and not in_source_prompt
                and watched is not (self.mode_button if self.side_panel.kind == "visual"
                                    else self.settings_button)
                and isinstance(watched, QWidget)
                and not self.side_panel.rect().contains(
                    self.side_panel.mapFromGlobal(event.globalPosition().toPoint())
                )):
            if not self.close_side_panel():
                return True
            if background:
                return True
        if (watched in (self.timeline_container, self.source_prompt)
                and event.type() in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick)):
            return True
        if (event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Escape
                and self.side_panel is not None):
            self.close_side_panel()
            return True
        if (background and event.type() in (QEvent.Type.MouseButtonPress,
                                            QEvent.Type.MouseButtonDblClick)
                and event.button() == Qt.MouseButton.LeftButton):
            if self.side_panel is None:
                central = self.centralWidget()
                x = central.mapFromGlobal(event.globalPosition().toPoint()).x()
                if event.type() == QEvent.Type.MouseButtonPress:
                    if x * 3 < central.width():
                        self.open_mode_menu()
                    elif x * 3 >= central.width() * 2:
                        self.open_settings()
                elif central.width() <= x * 3 < central.width() * 2:
                    self.next_visualizer_mode()
            return True
        if event.type() == QEvent.Type.MouseMove:
            self.show_controls()
            self.show_fullscreen_cursor()
        return super().eventFilter(watched, event)

    def open_source_menu(self):
        menu = QMenu(self)
        selected = self.state.get("selectedSource")
        for source in self.state.get("sources") or []:
            action = menu.addAction(source_icon(source), source["label"])
            action.setCheckable(True)
            action.setChecked(source["id"] == selected)
            action.triggered.connect(
                lambda checked=False, source_id=source["id"]: self.change_source(source_id)
            )
        if not menu.actions():
            action = menu.addAction("No sources available")
            action.setEnabled(False)
        self.controls_timer.stop()
        self.cursor_timer.stop()
        try:
            menu.exec(self.source_button.mapToGlobal(QPoint(0, -menu.sizeHint().height())))
        finally:
            self.show_controls()
            self.show_fullscreen_cursor()

    def open_mode_menu(self):
        self.open_side_panel("visual")

    def open_volume_menu(self):
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu { background: #101a35; border: 1px solid #718cc1; padding: 8px; }
        """)
        slider = WheelIgnoringSlider(Qt.Orientation.Vertical)
        slider.setAccessibleName("System volume")
        slider.setRange(0, 100)
        slider.setFixedSize(32, 140)
        volume = self.state.get("systemVolume")
        slider.setEnabled(volume is not None)
        slider.setValue(int(volume) if volume is not None else 0)
        slider.setStyleSheet(self.themed_slider_style(self._timeline_theme, vertical=True))
        self._volume_slider = slider
        action = QWidgetAction(menu)
        action.setDefaultWidget(slider)
        menu.addAction(action)
        def change_volume(value):
            previous = self.state.get("systemVolume")
            if not self.change_system_volume(value) and previous is not None:
                with QSignalBlocker(slider):
                    slider.setValue(previous)
        slider.valueChanged.connect(change_volume)
        self.controls_timer.stop()
        self.cursor_timer.stop()
        try:
            size = menu.sizeHint()
            offset = QPoint((self.volume_button.width() - size.width()) // 2, -size.height())
            menu.exec(self.volume_button.mapToGlobal(offset))
        finally:
            self._volume_slider = None
            self.show_controls()
            self.show_fullscreen_cursor()

    def show_controls(self):
        self.controls_effect.setOpacity(1)
        if self.side_panel is None:
            self.controls_timer.start(2500)
        else:
            self.controls_timer.stop()
        self.position_metadata()

    def hide_controls(self):
        if self.side_panel is not None:
            return
        if self.timeline_slider.isSliderDown():
            self.controls_timer.start(2500)
            return
        self.controls_effect.setOpacity(0)
        self.position_metadata()

    def hide_fullscreen_cursor(self):
        if self.isFullScreen():
            self.setCursor(Qt.CursorShape.BlankCursor)

    def show_fullscreen_cursor(self):
        self.setCursor(Qt.CursorShape.ArrowCursor)
        if self.isFullScreen() and self.side_panel is None:
            self.cursor_timer.start(2500)

    def mouseMoveEvent(self, event):
        self.show_controls()
        self.show_fullscreen_cursor()
        super().mouseMoveEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.position_metadata()
        if hasattr(self, "source_prompt"):
            self.position_source_prompt()
        if hasattr(self, "visualizer") and shiboken6.isValid(self.visualizer):
            central = self.centralWidget()
            if central is not None:
                self.visualizer.setGeometry(central.rect())
                self.visualizer.lower()
        if hasattr(self, "edge_border") and shiboken6.isValid(self.edge_border):
            central = self.centralWidget()
            if central is not None:
                self.edge_border.setGeometry(self.visualizer.rect())
            self.edge_border.raise_()
        if hasattr(self, "side_panel") and self.side_panel is not None:
            self.panel_animation.stop()
            if self.panel_closing:
                self.finish_closing_panel(self.side_panel)
            else:
                width = min(400, int(self.centralWidget().width() * .75))
                x = 0 if self.side_panel.kind == "visual" else self.centralWidget().width() - width
                self.side_panel.setGeometry(x, 0, width, self.centralWidget().height())

    def position_metadata(self):
        if not hasattr(self, "track_title") or self.centralWidget() is None:
            return
        central = self.centralWidget()
        width, height = central.width(), central.height()
        title_width = int(width * .92)
        bottom = height * .05
        artist_size = max(14, min(24, width * .016))
        title_size = max(22, min(44, width * .03))
        self.track_title.setStyleSheet(
            f"background: transparent; font-size: {title_size}px; font-weight: 700;"
        )
        self.track_artist.setStyleSheet(
            f"background: transparent; color: {self.control_border_color or 'rgba(255,255,255,102)'};"
            f" font-size: {artist_size}px;"
        )
        artist_height = max(25, self.track_artist.fontMetrics().height() + 4)
        title_height = max(math.ceil(title_size * 1.3), self.track_title.fontMetrics().height() + 4)
        artist_top = int(height - bottom - artist_height)
        self.track_artist.setGeometry(int(width * .04), artist_top, title_width, artist_height)
        self.track_title.setGeometry(int(width * .04), artist_top - title_height,
                                     title_width, title_height)
        spacer_height = height - self.track_title.y()
        if self.metadata_spacer.sizeHint().height() != spacer_height:
            self.metadata_spacer.changeSize(
                0, spacer_height, QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed
            )
            central.layout().invalidate()

    def position_source_prompt(self):
        width = min(620, max(1, self.centralWidget().width() - 24))
        self.source_prompt.setFixedWidth(width)
        self.source_prompt.adjustSize()
        self.source_prompt.move((self.centralWidget().width() - width) // 2, 12)

    def update_source_prompts(self, sources, selected_source, suppress_new=False):
        statuses = {source["id"]: source.get("status", "") for source in sources}
        previous = self._source_statuses
        self._source_statuses = statuses
        if not self.state.get("sourcePromptEnabled", True):
            self.source_prompt_timer.stop()
            self.source_prompt.hide()
            self._source_prompt_id = None
            self._pending_source_prompts.clear()
            return
        if self._source_prompt_id and (
            self._source_prompt_id == selected_source
            or statuses.get(self._source_prompt_id) != "playing"
        ):
            self.source_prompt_timer.stop()
            self.source_prompt.hide()
            self._source_prompt_id = None
        self._pending_source_prompts = [
            source_id for source_id in self._pending_source_prompts
            if source_id != selected_source and statuses.get(source_id) == "playing"
        ]
        if previous is not None and not suppress_new and selected_source:
            for source in sources:
                source_id = source["id"]
                if (source_id != selected_source and source.get("status") == "playing"
                        and previous.get(source_id) != "playing"
                        and source_id != self._source_prompt_id
                        and source_id not in self._pending_source_prompts):
                    self._pending_source_prompts.append(source_id)
        self.show_next_source_prompt()

    def show_next_source_prompt(self):
        while not self._source_prompt_id and self._pending_source_prompts:
            source_id = self._pending_source_prompts.pop(0)
            source = next((item for item in self.state.get("sources", [])
                           if item["id"] == source_id), None)
            if source is None or source.get("status") != "playing" or source_id == self.state.get("selectedSource"):
                continue
            self._source_prompt_id = source_id
            self.source_prompt_label.setText(f"{source['label']} started playing. Switch source?")
            self.position_source_prompt()
            self.source_prompt.show()
            self.source_prompt.raise_()
            self.source_prompt_timer.start(
                int(self.state.get("sourcePromptSeconds", SOURCE_PROMPT_SECONDS_DEFAULT)) * 1000
            )

    def dismiss_source_prompt(self):
        self.source_prompt_timer.stop()
        self.source_prompt.hide()
        self._source_prompt_id = None
        self.show_next_source_prompt()

    def accept_source_prompt(self):
        source_id = self._source_prompt_id
        if not source_id:
            return
        remaining = self.source_prompt_timer.remainingTime()
        self.source_prompt_timer.stop()
        if self.change_source(source_id):
            self._pending_source_prompts.clear()
            if self._source_prompt_id == source_id:
                self.dismiss_source_prompt()
        elif self._source_prompt_id == source_id:
            self.source_prompt_timer.start(max(1, remaining))

    def next_visualizer_mode(self):
        modes = [mode for mode, _label in QT_MODES]
        current = self.state.get("visualizerMode", 0)
        next_mode = modes[(modes.index(current) + 1) % len(modes)]
        try:
            self.panel_request("visualizer-mode", next_mode)
        except (OSError, ValueError, RuntimeError) as error:
            QMessageBox.warning(self, "Visualisation", f"Visualisation could not be changed: {error}")

    def keyPressEvent(self, event):
        self.show_controls()
        self.show_fullscreen_cursor()
        key = event.key()
        if key == Qt.Key.Key_Escape and self.side_panel is not None:
            self.close_side_panel()
        elif key in (Qt.Key.Key_Left,):
            self.media_control("Previous")
        elif key in (Qt.Key.Key_Right,):
            self.media_control("Next")
        elif key in (Qt.Key.Key_Space,):
            status = (self.state.get("track") or {}).get("status")
            self.media_control("Pause" if status == "playing" else "Play")
        elif key == Qt.Key.Key_F11:
            self.toggle_fullscreen()
        elif key == Qt.Key.Key_Escape and self.isFullScreen():
            self.toggle_fullscreen()
        elif key == Qt.Key.Key_MediaPrevious:
            self.media_control("Previous")
        elif key == Qt.Key.Key_MediaNext:
            self.media_control("Next")
        elif key == Qt.Key.Key_MediaPlay:
            self.media_control("Play")
        elif key == Qt.Key.Key_MediaPause:
            self.media_control("Pause")
        elif key == Qt.Key.Key_MediaTogglePlayPause:
            status = (self.state.get("track") or {}).get("status")
            self.media_control("Pause" if status == "playing" else "Play")
        else:
            super().keyPressEvent(event)

    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
            self.cursor_timer.stop()
            self.setCursor(Qt.CursorShape.ArrowCursor)
            self.fullscreen_button.setIcon(
                self.line_icon("fullscreen")
            )
            self.fullscreen_button.setToolTip("Enter full screen")
            self.fullscreen_button.setAccessibleName("Enter full screen")
        else:
            self.showFullScreen()
            self.cursor_timer.start(2500)
            self.fullscreen_button.setIcon(
                self.line_icon("fullscreen")
            )
            self.fullscreen_button.setToolTip("Exit full screen")
            self.fullscreen_button.setAccessibleName("Exit full screen")

    def refresh_state(self):
        try:
            state = self.backend.get_state()
            previous_track = self.state.get("track") or {}
            previous_art = tuple(previous_track.get(key) for key in ("title", "artist", "album", "art"))
            self.state = state
            if self.side_panel is not None:
                if self.side_panel.kind == "visual":
                    self.side_panel.sync_fields(state)
                else:
                    self.side_panel.sync_settings(state)
            track = state.get("track") or {}
            self.update_timeline(track)
            self.visualizer.set_state(state)
            self.edge_border.opacity = float(state.get("edgeOpacity", 70)) / 100
            self.edge_border.paused = (
                track.get("status") != "playing"
                or not bool(state.get("edgeSwirl", False))
            )
            now = time.monotonic()
            elapsed = min(.25, now - self.edge_last_update)
            self.edge_last_update = now
            if not self.edge_border.paused:
                slowdown = int(state.get("edgeSwirlSlowdown", 1))
                intervals = sorted(self.visualizer.beat_intervals)
                duration = (intervals[len(intervals) // 2] * 2 if intervals else 6)
                direction = state.get(
                    "edgeSwirlResolvedDirection",
                    state.get("edgeSwirlDirection", "clockwise"),
                )
                sign = 1 if direction == "counter-clockwise" else -1
                self.edge_border.angle = (
                    self.edge_border.angle + sign * 360 * elapsed / (duration * max(1, slowdown))
                ) % 360
            self.play_button.setText(
                "Pause" if track.get("status") == "playing" else "Play"
            )
            playing = track.get("status") == "playing"
            self.play_button.setIcon(
                self.line_icon("pause" if playing else "play")
            )
            self.play_button.setToolTip("Pause playback" if playing else "Start playback")
            self.play_button.setAccessibleName(
                "Pause playback" if playing else "Start playback"
            )
            self.track_title.setText(track.get("title") or "Waiting for music...")
            artist_album = " - ".join(
                value for value in (track.get("artist"), track.get("album")) if value
            )
            self.track_artist.setText(artist_album)
            sources = state.get("sources") or []
            selected = next((source for source in sources
                             if source["id"] == state.get("selectedSource")), None)
            source_label = f"Playback source: {selected['label']}" if selected else "Choose playback source"
            self.source_button.setToolTip(source_label)
            self.source_button.setAccessibleName(source_label)
            startup_selection = self.prefer_playing_source_on_start and bool(sources)
            if startup_selection:
                self.prefer_playing_source_on_start = False
                active = next((source for source in sources
                               if source["id"].startswith("mpris:")
                               and source.get("status") == "playing"), None)
                if active and active.get("application"):
                    active = next((source for source in sources
                                   if source.get("application") == active["application"]
                                   and source.get("lastSelected")), active)
                if active and active["id"] != state.get("selectedSource"):
                    self.change_source(active["id"])
            self.update_source_prompts(sources, state.get("selectedSource"), startup_selection)

            current_art = track.get("art")
            art_key = tuple(track.get(key) for key in ("title", "artist", "album", "art"))
            if current_art and art_key != previous_art:
                self.load_art()
            elif not current_art:
                if track.get("artStatus") in ("failed", "fallback"):
                    self.visualizer.clear_art()
            palette = self.visualizer.active_palette()
            self.update_control_borders(palette)
            if state.get("lightingColorMode") == "rainbow":
                count = int(state.get("edgeColorCount", 8))
                self.edge_border.set_palette([
                    QColor.fromHslF(index / count, 1, .5).name() for index in range(count)
                ])
            elif state.get("lightingColorMode") == "christmas":
                count = int(state.get("edgeColorCount", 8))
                self.edge_border.set_palette([
                    CHRISTMAS_COLORS[index % len(CHRISTMAS_COLORS)] for index in range(count)
                ])
            elif palette:
                self.edge_border.set_palette(
                    [color.name() for color in palette["edge"]]
                )
            else:
                self.edge_border.set_palette(["#1b5bff", "#b46cff", "#6a70ff", "#37ff9a"])
            self.update_timeline_theme(palette)
            self.edge_border.update_disco(state, elapsed)
            self.edge_border.update()
            self._last_refresh_error = ""
        except (OSError, ValueError, RuntimeError) as error:
            message = str(error)
            if message != self._last_refresh_error:
                print(f"Visualiser state could not be refreshed: {error}", file=sys.stderr)
                self._last_refresh_error = message

    def update_control_borders(self, palette):
        color = ("#ffffff" if self.state.get("lightingColorMode") == "rainbow" else
                 CHRISTMAS_COLORS[2] if self.state.get("lightingColorMode") == "christmas"
                 else palette["colors"][-1].name() if palette else None)
        if color == self.control_border_color:
            return
        self.control_border_color = color
        style = self.control_style
        if color:
            style += f"\nQComboBox, QPushButton, QToolButton {{ border-color: {color}; }}"
        self.setStyleSheet(style)
        self.position_metadata()

    def update_timeline_theme(self, palette):
        if self.state.get("lightingColorMode") == "rainbow":
            colors = self.edge_border.colors + self.edge_border.colors[:1]
            handle = "#ffffff"
        elif self.state.get("lightingColorMode") == "christmas":
            colors = self.edge_border.colors + self.edge_border.colors[:1]
            handle = CHRISTMAS_COLORS[2]
        elif palette:
            colors = palette["colors"]
            handle = colors[-1].name()
        else:
            colors = [QColor("#1b5bff"), QColor("#6a70ff"), QColor("#b46cff")]
            handle = colors[-1].name()
        names = tuple(color.name() for color in colors)
        theme = (names, handle)
        if theme == self._timeline_theme:
            return
        self._timeline_theme = theme
        self.timeline_slider.setStyleSheet(self.themed_slider_style(theme))
        if self._volume_slider is not None:
            self._volume_slider.setStyleSheet(self.themed_slider_style(theme, vertical=True))

    @staticmethod
    def themed_slider_style(theme, vertical=False):
        names, handle = theme
        stops = ", ".join(f"stop: {index / (len(names) - 1):.4f} {color}"
                          for index, color in enumerate(names))
        groove = QColor(names[0]).darker(250).name()
        if vertical:
            return f"""
            QSlider::groove:vertical {{ width: 6px; background: {groove}; border-radius: 3px; }}
            QSlider::add-page:vertical {{
                background: qlineargradient(x1:0, y1:1, x2:0, y2:0, {stops}); border-radius: 3px;
            }}
            QSlider::handle:vertical {{
                height: 12px; margin: 0 -4px; background: {handle}; border-radius: 6px;
            }}
            """
        return f"""
            QSlider::groove:horizontal {{ height: 4px; background: {groove}; border-radius: 2px; }}
            QSlider::sub-page:horizontal {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, {stops}); border-radius: 2px;
            }}
            QSlider::handle:horizontal {{
                width: 12px; margin: -4px 0; background: {handle}; border-radius: 6px;
            }}
        """

    def load_art(self):
        art = (self.state.get("track") or {}).get("art", "")
        try:
            if not art.startswith("file://"):
                raise ValueError("Artwork is not a local file")
            with open(urllib.parse.unquote(art[7:]), "rb") as image:
                self.visualizer.set_art(image.read())
        except (OSError, ValueError) as error:
            print(f"Artwork could not be loaded: {error}", file=sys.stderr)
            self.visualizer.clear_art()

    def media_control(self, action):
        try:
            self.backend.media_control(action)
        except (dbus.DBusException, OSError, RuntimeError) as error:
            QMessageBox.warning(self, "Playback", f"Playback control failed: {error}")

    def update_timeline(self, track):
        duration, position = track.get("duration"), track.get("position")
        available = isinstance(duration, int) and duration > 0 and isinstance(position, int)
        self.timeline_slider.setEnabled(available and bool(track.get("canSeek")))
        if self.timeline_slider.isSliderDown():
            return
        with QSignalBlocker(self.timeline_slider):
            maximum = min((duration + 999) // 1000, 2_147_483_647) if available else 1
            self.timeline_slider.setMaximum(maximum)
            self.timeline_slider.setSingleStep(max(1, maximum * 5_000_000 // duration) if available else 1)
            self.timeline_slider.setPageStep(max(1, maximum * 30_000_000 // duration) if available else 1)
            self.timeline_slider.setValue(min(maximum, max(0, position * maximum // duration)) if available else 0)
        self.timeline_elapsed.setText(format_track_time(position) if available else "--:--")
        self.timeline_remaining.setText(format_track_time(duration) if available else "--:--")

    def start_timeline_drag(self):
        track = self.state.get("track") or {}
        self._timeline_drag = (self.state.get("selectedSource"), track.get("trackId"), track.get("duration"))
        self.controls_timer.stop()

    def finish_timeline_drag(self):
        drag = self._timeline_drag
        self._timeline_drag = None
        if drag:
            self.seek_timeline(*drag)
        self.show_controls()

    def timeline_value_changed(self, value):
        if self.timeline_slider.isSliderDown():
            if self._timeline_drag and self._timeline_drag[2] is not None:
                self.timeline_elapsed.setText(format_track_time(
                    self._timeline_drag[2] * value // self.timeline_slider.maximum()
                ))
            return
        track = self.state.get("track") or {}
        self.seek_timeline(self.state.get("selectedSource"), track.get("trackId"), track.get("duration"))

    def seek_timeline(self, source_id, track_id, duration):
        try:
            if not isinstance(duration, int) or duration <= 0 or not source_id or not track_id:
                raise ValueError("Track is no longer available for seeking")
            position = duration * self.timeline_slider.value() // self.timeline_slider.maximum()
            self.backend.seek_track(source_id, track_id, position)
        except (dbus.DBusException, OSError, ValueError, RuntimeError) as error:
            QMessageBox.warning(self, "Playback", f"Track position could not be changed: {error}")
            self.update_timeline(self.state.get("track") or {})

    def change_system_volume(self, value):
        try:
            self.panel_request("system-volume", value)
        except (OSError, ValueError, RuntimeError) as error:
            QMessageBox.warning(self, "System volume", f"Volume could not be changed: {error}")
            return False
        self.state = {**self.state, "systemVolume": value}
        return True

    def change_source(self, source_id):
        if source_id == self.state.get("selectedSource"):
            return True
        try:
            success, warning = self.backend.set_source(source_id)
            if not success:
                raise ValueError(warning or "Source is no longer available")
            if warning:
                QMessageBox.warning(self, "Playback source", warning)
            return True
        except (dbus.DBusException, OSError, ValueError, RuntimeError) as error:
            QMessageBox.warning(self, "Playback source", f"Source could not be changed: {error}")
            return False

    def open_settings(self):
        self.open_side_panel("settings")

    def panel_request(self, name, value, source_id=None):
        try:
            warning = self.backend.update_setting(name, value, source_id)
        except (dbus.DBusException, subprocess.SubprocessError) as error:
            raise RuntimeError(str(error)) from error
        if name == "visualizer-mode":
            self.state = {**self.state, "visualizerMode": value}
            self.visualizer.set_state(self.state)
        elif name == "show-photosensitivity-warning":
            self.state = {**self.state, "showPhotosensitivityWarning": value}
        elif name == "source-prompt-enabled":
            self.state = {**self.state, "sourcePromptEnabled": value}
            self.update_source_prompts(self.state.get("sources") or [], self.state.get("selectedSource"))
        if warning:
            QMessageBox.warning(self, "Playback source", warning)

    def closeEvent(self, event):
        if not self.close_side_panel():
            event.ignore()
            return
        try:
            self.backend.stop()
        except (OSError, subprocess.SubprocessError, ValueError, RuntimeError) as error:
            print(f"Source volume could not be restored on close: {error}", file=sys.stderr)
            QMessageBox.warning(
                self, "Source volume",
                f"Source volume could not be restored immediately: {error}\n"
                "AmbiBeat will retry when it next starts.",
            )
        super().closeEvent(event)

    def open_side_panel(self, kind):
        if self.side_panel is not None:
            if self.side_panel.kind == kind and not self.panel_closing:
                self.close_side_panel()
                return
            if not self.panel_closing and not self.close_side_panel():
                return
            self.panel_animation.stop()
            self.side_panel.hide()
            self.side_panel.deleteLater()
        if self.panel_animation is not None:
            self.panel_animation.deleteLater()
        self.panel_closing = False
        central = self.centralWidget()
        panel = SidePanel(self.state, kind, QT_MODES, self.panel_request, central)
        self.side_panel = panel
        panel.close_requested.connect(self.close_side_panel)
        for widget in [panel] + panel.findChildren(QWidget):
            widget.setMouseTracking(True)
            widget.installEventFilter(self)
        width = min(400, int(central.width() * .75))
        end_x = 0 if kind == "visual" else central.width() - width
        start_x = -width if kind == "visual" else central.width()
        panel.setGeometry(start_x, 0, width, central.height())
        panel.show()
        panel.raise_()
        if self._source_prompt_id:
            self.source_prompt.raise_()
        self.controls_timer.stop()
        self.cursor_timer.stop()
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.panel_animation = QPropertyAnimation(panel, b"pos", self)
        self.panel_animation.setDuration(220)
        self.panel_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.panel_animation.setStartValue(QPoint(start_x, 0))
        self.panel_animation.setEndValue(QPoint(end_x, 0))
        self.panel_animation.start()

    def close_side_panel(self):
        panel = self.side_panel
        if panel is None or self.panel_closing:
            return True
        self.panel_closing = True
        self.panel_animation.stop()
        end_x = -panel.width() if panel.kind == "visual" else self.centralWidget().width()
        self.panel_animation.setStartValue(panel.pos())
        self.panel_animation.setEndValue(QPoint(end_x, 0))

        self.panel_animation.finished.connect(lambda: self.finish_closing_panel(panel))
        self.panel_animation.start()
        return True

    def finish_closing_panel(self, panel):
        if self.side_panel is panel:
            panel.hide()
            panel.deleteLater()
            self.side_panel = None
            self.panel_closing = False
            self.show_controls()
            self.show_fullscreen_cursor()


def show_photosensitivity_warning():
    dialog = QMessageBox()
    dialog.setIcon(QMessageBox.Icon.Warning)
    dialog.setWindowTitle("Photosensitivity warning")
    dialog.setText(
        "AmbiBeat's visual effects can flash or change rapidly. "
        "They may trigger seizures or discomfort in people sensitive to flashing lights."
    )
    remember = QCheckBox("Remember my acknowledgement")
    dialog.setCheckBox(remember)
    acknowledge = dialog.addButton("Acknowledge", QMessageBox.ButtonRole.AcceptRole)
    dialog.addButton(QMessageBox.StandardButton.Cancel)
    dialog.setDefaultButton(acknowledge)
    dialog.exec()
    return dialog.clickedButton() is acknowledge, remember.isChecked()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("AmbiBeat")
    app.setOrganizationName("AmbiBeat")
    app.setApplicationDisplayName("AmbiBeat")
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor("#090b12"))
    palette.setColor(QPalette.ColorRole.WindowText, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.Base, QColor("#111522"))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#161824"))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor("#111522"))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.Text, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.Button, QColor("#090b12"))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.Highlight, QColor("#8d82d8"))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.BrightText, QColor("#d8e7ff"))
    settings_dir = os.path.join(
        os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config"),
        "ambibeat",
    )
    lock_file = None
    backend = None
    try:
        os.makedirs(settings_dir, mode=0o700, exist_ok=True)
        lock_file = open(os.path.join(settings_dir, "instance.lock"), "a", encoding="utf-8")
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            QMessageBox.warning(None, "AmbiBeat", "AmbiBeat is already running.")
            return 1
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        session_bus = dbus.SessionBus()
        backend = AmbiBeatBackend(session_bus)
    except (OSError, dbus.DBusException, RuntimeError) as error:
        QMessageBox.critical(None, "AmbiBeat", f"Could not start AmbiBeat: {error}")
        return 1
    finally:
        if lock_file is not None and backend is None:
            lock_file.close()
    try:
        if not backend.photosensitivity_warning_acknowledged:
            acknowledged, remember = show_photosensitivity_warning()
            if not acknowledged:
                return 0
            if remember:
                try:
                    backend.acknowledge_photosensitivity_warning()
                except OSError as error:
                    QMessageBox.critical(
                        None, "AmbiBeat", f"Could not save the warning acknowledgement: {error}",
                    )
                    return 1
        app.setPalette(palette)
        window = AmbiBeatQtWindow(backend)
        window.show()
        signal.signal(signal.SIGINT, lambda *_args: app.quit())
        signal.signal(signal.SIGTERM, lambda *_args: app.quit())
        signal_timer = QTimer()
        signal_timer.timeout.connect(lambda: None)
        signal_timer.start(250)
        return app.exec()
    finally:
        try:
            backend.stop()
        except (OSError, subprocess.SubprocessError, ValueError, RuntimeError) as error:
            print(f"AmbiBeat shutdown failed: {error}", file=sys.stderr)
        lock_file.close()
