import math
import os
import time
from unittest.mock import Mock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QAbstractAnimation, QBuffer, QEvent, QIODevice, QPoint, QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import (
    QColor, QFont, QImage, QMouseEvent, QOpenGLContext, QPaintEngine, QPainter, QPalette, QPixmap, QRegion,
    QWheelEvent,
)
from PySide6.QtOpenGL import QOpenGLFramebufferObject
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QStyle, QStyleOptionButton, QStyleOptionSlider, QWidget

from ambibeat import qt_frontend, qt_panels
from ambibeat.qt_frontend import AmbiBeatQtWindow
from ambibeat.qt_panels import SidePanel


@pytest.fixture
def window(monkeypatch):
    app = QApplication.instance() or QApplication([])
    backend = Mock()
    backend.get_state.return_value = {
        "track": {}, "selectedSource": "mpris:org.mpris.MediaPlayer2.player",
        "sources": [{"id": "mpris:org.mpris.MediaPlayer2.player", "label": "Player"}],
    }
    backend.set_source.return_value = (True, "")
    backend.update_setting.return_value = ""
    window = AmbiBeatQtWindow(backend)
    window.timer.stop()
    window.show()
    app.processEvents()
    yield window
    monkeypatch.setattr(qt_frontend.QMessageBox, "question",
                        lambda *args: qt_frontend.QMessageBox.StandardButton.Discard)
    window.close()
    app.processEvents()


def wait_for_panel_animation(window):
    animation = window.panel_animation
    deadline = time.monotonic() + 2
    while animation.state() == QAbstractAnimation.State.Running and time.monotonic() < deadline:
        QTest.qWait(20)
    assert animation.state() != QAbstractAnimation.State.Running


@pytest.mark.parametrize("acknowledged,remember", [(False, False), (False, True), (True, False)])
def test_main_starts_with_session_bus_and_stops_backend(tmp_path, monkeypatch, acknowledged, remember):
    app = Mock()
    app.exec.return_value = 0
    backend = Mock()
    backend.photosensitivity_warning_acknowledged = acknowledged
    session_bus = object()
    backend_factory = Mock(return_value=backend)
    window = Mock()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(qt_frontend, "QApplication", lambda *_: app)
    monkeypatch.setattr(qt_frontend.dbus.mainloop.glib, "DBusGMainLoop", lambda **_: None)
    monkeypatch.setattr(qt_frontend.dbus, "SessionBus", lambda: session_bus)
    monkeypatch.setattr(qt_frontend, "AmbiBeatBackend", backend_factory)
    window_factory = Mock(return_value=window)
    monkeypatch.setattr(qt_frontend, "AmbiBeatQtWindow", window_factory)
    def warning():
        window_factory.assert_not_called()
        app.setPalette.assert_not_called()
        return True, remember
    warning_dialog = Mock(side_effect=warning)
    monkeypatch.setattr(qt_frontend, "show_photosensitivity_warning", warning_dialog)
    monkeypatch.setattr(qt_frontend.signal, "signal", lambda *_: None)
    monkeypatch.setattr(qt_frontend, "QTimer", Mock)

    assert qt_frontend.main() == 0
    backend_factory.assert_called_once_with(session_bus)
    assert warning_dialog.call_count == (0 if acknowledged else 1)
    assert backend.acknowledge_photosensitivity_warning.call_count == int(not acknowledged and remember)
    app.setPalette.assert_called_once()
    window.show.assert_called_once()
    backend.stop.assert_called_once()


@pytest.mark.parametrize("result,save_fails,exit_code", [
    ((False, False), False, 0),
    ((True, True), True, 1),
])
def test_warning_must_be_acknowledged_and_saved_before_opening(
    tmp_path, monkeypatch, result, save_fails, exit_code
):
    backend = Mock()
    backend.photosensitivity_warning_acknowledged = False
    if save_fails:
        backend.acknowledge_photosensitivity_warning.side_effect = OSError("disk full")
    window_factory = Mock()
    error_dialog = Mock()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(qt_frontend, "QApplication", lambda *_: Mock())
    monkeypatch.setattr(qt_frontend.dbus.mainloop.glib, "DBusGMainLoop", lambda **_: None)
    monkeypatch.setattr(qt_frontend.dbus, "SessionBus", lambda: object())
    monkeypatch.setattr(qt_frontend, "AmbiBeatBackend", lambda _: backend)
    monkeypatch.setattr(qt_frontend, "AmbiBeatQtWindow", window_factory)
    monkeypatch.setattr(qt_frontend, "show_photosensitivity_warning", lambda: result)
    monkeypatch.setattr(qt_frontend.QMessageBox, "critical", error_dialog)
    assert qt_frontend.main() == exit_code
    window_factory.assert_not_called()
    backend.stop.assert_called_once()
    assert backend.acknowledge_photosensitivity_warning.call_count == int(save_fails)
    if save_fails:
        assert "disk full" in error_dialog.call_args.args[2]
    else:
        error_dialog.assert_not_called()


@pytest.mark.parametrize("remember,cancel", [(False, False), (True, False), (True, True)])
def test_photosensitivity_dialog_defaults_to_not_remembering(window, remember, cancel):
    observed = {}
    def respond():
        dialog = QApplication.activeModalWidget()
        checkbox = dialog.checkBox()
        observed["initially_checked"] = checkbox.isChecked()
        observed["text"] = dialog.text()
        observed["buttons"] = [button.text() for button in dialog.buttons()]
        checkbox.setChecked(remember)
        if cancel:
            dialog.button(qt_frontend.QMessageBox.StandardButton.Cancel).click()
        else:
            button = next((button for button in dialog.buttons()
                           if button.text() == "Acknowledge"), None)
            if button is None:
                dialog.reject()
            else:
                button.click()
    QTimer.singleShot(0, respond)
    acknowledged, remembered = qt_frontend.show_photosensitivity_warning()
    assert not observed["initially_checked"]
    assert "seizures" in observed["text"]
    assert "Acknowledge" in observed["buttons"]
    assert acknowledged is not cancel
    assert remembered is remember


def test_photosensitivity_dialog_uses_application_theme_and_font(window):
    app = QApplication.instance()
    original_palette, original_font = app.palette(), app.font()
    palette = QPalette(original_palette)
    palette.setColor(QPalette.ColorRole.Window, QColor("white"))
    palette.setColor(QPalette.ColorRole.WindowText, QColor("black"))
    font = QFont(original_font)
    font.setPointSize(19)
    observed = {}
    try:
        app.setPalette(palette)
        app.setFont(font)
        def respond():
            dialog = QApplication.activeModalWidget()
            observed["window"] = dialog.palette().color(QPalette.ColorRole.Window)
            observed["text"] = dialog.palette().color(QPalette.ColorRole.WindowText)
            observed["font"] = dialog.checkBox().font().pointSize()
            dialog.button(qt_frontend.QMessageBox.StandardButton.Cancel).click()
        QTimer.singleShot(0, respond)
        qt_frontend.show_photosensitivity_warning()
    finally:
        app.setPalette(original_palette)
        app.setFont(original_font)
    assert observed == {"window": QColor("white"), "text": QColor("black"), "font": 19}


@pytest.mark.parametrize(
    "widget_name",
    ["visualizer", "track_title", "source_button", "play_button"],
)
def test_mouse_movement_reveals_controls(window, widget_name):
    widget = getattr(window, widget_name)
    window.hide_controls()
    window.controls_timer.stop()
    window.setCursor(Qt.CursorShape.BlankCursor)

    QTest.mouseMove(widget, QPoint(10, 10))
    QApplication.processEvents()

    assert window.controls_effect.opacity() == 1
    assert window.controls_timer.isActive()
    assert window.cursor().shape() == Qt.CursorShape.ArrowCursor


def test_mouse_activity_resets_inactivity_timer(window):
    window.controls_timer.start(50)
    QTest.mouseMove(window.visualizer, QPoint(20, 20))
    QTest.qWait(100)

    assert window.controls_effect.opacity() == 1
    window.controls_timer.start(50)
    QTest.qWait(100)
    assert window.controls_effect.opacity() == 0


@pytest.mark.parametrize("size", [(640, 420), (1100, 720), (1920, 1080)])
def test_lower_controls_stay_above_track_text(window, size):
    window.resize(*size)
    window.show_controls()
    QApplication.processEvents()
    central = window.centralWidget()
    title_rect = window.track_title.geometry()
    artist_rect = window.track_artist.geometry()
    for button in (window.source_button, window.mode_button, window.previous_button, window.play_button,
                   window.next_button, window.volume_button, window.settings_button,
                   window.fullscreen_button):
        position = button.mapTo(central, QPoint(0, 0))
        assert position.y() + button.height() <= title_rect.top()
    assert title_rect.bottom() < artist_rect.top()
    assert artist_rect.bottom() < central.height()
    for label in (window.track_title, window.track_artist):
        assert label.height() >= label.fontMetrics().height() + 4
    shadow = window.track_artist.graphicsEffect().boundingRectFor(QRectF(window.track_artist.rect()))
    assert artist_rect.top() + shadow.bottom() < central.height()

    window.hide_controls()
    QApplication.processEvents()
    assert window.track_title.geometry() == title_rect
    assert window.track_artist.geometry() == artist_rect


@pytest.mark.parametrize("size", [(640, 420), (1100, 720), (1920, 1080)])
@pytest.mark.parametrize("label_name", ["track_title", "track_artist"])
def test_track_text_is_not_clipped_vertically(window, size, label_name):
    window.resize(*size)
    window.show_controls()
    QApplication.processEvents()
    label = getattr(window, label_name)
    label.setText("gypj Qy")
    effect = label.graphicsEffect()
    if effect:
        effect.setEnabled(False)

    def ink():
        image = QImage(label.size(), QImage.Format.Format_RGBA8888_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        label.render(painter, QPoint(), QRegion(), QWidget.RenderFlag.DrawChildren)
        painter.end()
        return sum(bytes(image.bits())[3::4])

    original_size = label.size()
    try:
        actual = ink()
        label.resize(label.width(), label.height() + 40)
        assert actual == ink()
    finally:
        label.resize(original_size)
        if effect:
            effect.setEnabled(True)


def test_timeline_displays_progress_and_seeks_by_click_and_keyboard(window):
    source = "mpris:org.mpris.MediaPlayer2.player"
    state = {
        "selectedSource": source,
        "track": {"status": "paused", "trackId": "/track/1", "canSeek": True,
                  "position": 30_000_000, "duration": 120_000_000},
    }
    window.backend.get_state.return_value = state
    window.refresh_state()
    slider = window.timeline_slider
    assert slider.isEnabled()
    assert slider.value() == 30_000
    assert window.timeline_elapsed.text() == "0:30"
    assert window.timeline_remaining.text() == "2:00"
    assert slider.mapTo(window.centralWidget(), QPoint(0, 0)).y() < window.play_button.mapTo(
        window.centralWidget(), QPoint(0, 0)).y()
    QTest.mouseClick(slider, Qt.MouseButton.LeftButton, pos=QPoint(slider.width() * 3 // 4, slider.height() // 2))
    args = window.backend.seek_track.call_args.args
    assert args[:2] == (source, "/track/1")
    assert args[2] == pytest.approx(90_000_000, abs=2_000_000)
    window.backend.seek_track.reset_mock()
    slider.setFocus()
    QTest.keyClick(slider, Qt.Key.Key_Left)
    window.backend.seek_track.assert_called_once()


def test_timeline_disables_without_seek_support_and_rejects_stale_drag(window, monkeypatch):
    source = "mpris:org.mpris.MediaPlayer2.player"
    track = {"status": "playing", "trackId": "/track/1", "canSeek": True,
             "position": 10_000_000, "duration": 60_000_000}
    window.backend.get_state.return_value = {"selectedSource": source, "track": track}
    window.refresh_state()
    slider = window.timeline_slider
    QTest.mousePress(slider, Qt.MouseButton.LeftButton, pos=slider.rect().center())
    window.hide_controls()
    assert window.controls_effect.opacity() == 1
    window.backend.get_state.return_value = {"selectedSource": source, "track": {**track, "trackId": "/track/2"}}
    window.refresh_state()
    window.backend.seek_track.side_effect = ValueError("Track changed")
    warnings = []
    monkeypatch.setattr(qt_frontend.QMessageBox, "warning", lambda *args: warnings.append(args[-1]))
    QTest.mouseRelease(slider, Qt.MouseButton.LeftButton, pos=slider.rect().center())
    assert window.backend.seek_track.call_args.args[:2] == (source, "/track/1")
    assert warnings and "Track changed" in warnings[0]
    window.backend.get_state.return_value = {"track": {}}
    window.refresh_state()
    assert not slider.isEnabled()
    assert window.timeline_elapsed.text() == "--:--"


def test_timeline_waits_for_drag_release_and_preserves_preview(window):
    source = "mpris:org.mpris.MediaPlayer2.player"
    track = {"status": "playing", "trackId": "/track/1", "canSeek": True,
             "position": 10_000_000, "duration": 60_000_000}
    window.backend.get_state.return_value = {"selectedSource": source, "track": track}
    window.refresh_state()
    slider = window.timeline_slider
    point = QPoint(slider.width() * 3 // 4, slider.height() // 2)
    QTest.mousePress(slider, Qt.MouseButton.LeftButton, pos=point)
    preview = slider.value()
    assert preview > 40_000
    window.backend.seek_track.assert_not_called()
    window.refresh_state()
    assert slider.value() == preview
    QTest.mouseRelease(slider, Qt.MouseButton.LeftButton, pos=point)
    window.backend.seek_track.assert_called_once()


def test_timeline_clicks_do_not_activate_background_controls(window, monkeypatch):
    source = "mpris:org.mpris.MediaPlayer2.player"
    track = {"status": "paused", "trackId": "/track/1", "canSeek": True,
             "position": 10_000_000, "duration": 60_000_000}
    window.backend.get_state.return_value = {"selectedSource": source, "track": track}
    window.refresh_state()
    calls = []
    monkeypatch.setattr(window, "open_mode_menu", lambda: calls.append("effects"))
    monkeypatch.setattr(window, "open_settings", lambda: calls.append("settings"))
    monkeypatch.setattr(window, "next_visualizer_mode", lambda: calls.append("next effect"))
    slider = window.timeline_slider
    for x in (4, slider.width() - 4):
        QTest.mouseClick(slider, Qt.MouseButton.LeftButton, pos=QPoint(x, slider.height() // 2))
    QTest.mouseDClick(slider, Qt.MouseButton.LeftButton, pos=slider.rect().center())
    QTest.mouseClick(window.timeline_elapsed, Qt.MouseButton.LeftButton)
    window.backend.get_state.return_value = {"track": {}}
    window.refresh_state()
    QTest.mouseClick(slider, Qt.MouseButton.LeftButton, pos=slider.rect().center())
    assert calls == []


def test_timeline_follows_artwork_rainbow_and_default_colours(window):
    track = {"status": "paused", "trackId": "/track/1", "canSeek": True,
             "position": 30_000_000, "duration": 60_000_000}
    state = {"track": track, "artworkMode": "album"}
    window.backend.get_state.return_value = state
    visualizer = window.visualizer

    def timeline_pixel():
        image = window.timeline_slider.grab().toImage()
        return image.pixelColor(image.width() // 4, image.height() // 2)

    colors = []
    for color in ("red", "blue"):
        pixmap = QPixmap(32, 32)
        pixmap.fill(QColor(color))
        visualizer.track_palette = visualizer.sample_palette(pixmap)
        window.refresh_state()
        colors.append(timeline_pixel())
    assert colors[0] != colors[1]
    state["lightingColorMode"] = "rainbow"
    state["edgeColorCount"] = 3
    window.refresh_state()
    style = window.timeline_slider.styleSheet()
    assert all(color in style for color in ("#ff0000", "#00ff00", "#0000ff"))
    assert timeline_pixel() not in colors
    state["edgeColorCount"] = 4
    window.refresh_state()
    assert window.timeline_slider.styleSheet() != style
    state["lightingColorMode"] = "artwork"
    state["artworkMode"] = "none"
    window.refresh_state()
    assert "#1b5bff" in window.timeline_slider.styleSheet()
    assert timeline_pixel() != colors[0]


def test_christmas_theme_changes_lighting_background_and_controls(window):
    visualizer = window.visualizer
    artwork = {
        "base": QColor("blue"), "background": [QColor("blue")],
        "colors": [QColor("blue")] * 3, "edge": [QColor("blue")],
    }
    visualizer.track_palette = artwork
    state = {"track": {"status": "paused"}, "artworkMode": "album",
             "lightingColorMode": "christmas", "edgeColorCount": 3}
    window.backend.get_state.return_value = state
    window.refresh_state()
    assert [color.name() for color in window.edge_border.colors] == list(qt_frontend.CHRISTMAS_COLORS)
    assert window.control_border_color == qt_frontend.CHRISTMAS_COLORS[2]
    assert all(color in window.timeline_slider.styleSheet() for color in qt_frontend.CHRISTMAS_COLORS)
    assert visualizer.active_palette() is artwork
    assert [color.name() for color in visualizer.effect_palette()["colors"]] == list(
        qt_frontend.CHRISTMAS_COLORS)

    def background():
        image = QImage(300, 200, QImage.Format.Format_ARGB32_Premultiplied)
        painter = QPainter(image)
        visualizer.paint_background(painter, 300, 200)
        painter.end()
        return image.pixelColor(150, 84)

    festive = background()
    assert festive.green() > festive.blue()
    window.backend.get_state.return_value = {**state, "lightingColorMode": "artwork"}
    window.refresh_state()
    assert background() != festive
    assert window.control_border_color == "#0000ff"
    window.backend.get_state.return_value = state
    window.refresh_state()
    assert background() == festive


def test_gelato_matches_milky_palette_across_visuals_and_controls(window):
    milky = ["#fec601", "#02a8fe", "#fe0288", "#d701fd",
             "#fd6903", "#aae603", "#f959f5", "#fe1536"]
    state = {"track": {"status": "playing"}, "visualizerMode": 1, "lightingMode": "border",
             "lightingColorMode": "custom-1", "edgeColorCount": 2,
             "customPalettes": [{"slot": 1, "name": "Milky", "enabled": True, "colors": milky}],
             "bars": [.7] * 24, "wave": [math.sin(i / 5) * .5 for i in range(64)]}

    def snapshot(state):
        window.backend.get_state.return_value = state
        window.refresh_state()
        visualizer = window.visualizer
        background = QImage(300, 200, QImage.Format.Format_ARGB32_Premultiplied)
        painter = QPainter(background)
        visualizer.paint_background(painter, 300, 200)
        painter.end()
        return (
            [color.name() for color in window.edge_border.colors],
            [color.name() for color in visualizer.effect_palette()["colors"]],
            [color.name() for color in window.edge_border.border_palette(state)],
            background,
            window.control_border_color,
            window.timeline_slider.styleSheet(),
            window.capture_current_palette(),
        )

    custom = snapshot(state)
    builtin = snapshot({**state, "lightingColorMode": "gelato", "customPalettes": []})
    assert builtin == custom
    assert builtin[0] == milky
    assert len(builtin[2]) == 16

    panel = SidePanel({"lightingColorMode": "gelato"}, "visual", qt_frontend.QT_MODES, Mock())
    assert panel.lighting_color_mode.currentText() == "Gelato"
    assert panel.lighting_count_settings.isHidden()
    panel.sync_fields({"customPalettes": state["customPalettes"], "lightingColorMode": "gelato"})
    assert panel.lighting_color_mode.currentText() == "Gelato"
    panel.deleteLater()


def test_gelato_renders_on_gpu(window):
    visualizer = window.visualizer
    if not isinstance(visualizer, qt_frontend.GPUVisualizerWidget):
        pytest.skip("hardware OpenGL widget unavailable")
    state = {"track": {"status": "playing"}, "visualizerMode": 0, "lightingMode": "border",
             "lightingColorMode": "gelato", "bars": [.7] * 24}
    window.backend.get_state.return_value = state
    window.refresh_state()
    assert visualizer.initialized
    assert not visualizer.grabFramebuffer().isNull()
    assert len(window.edge_border.border_palette(state)) == 16
    assert isinstance(window.visualizer, qt_frontend.GPUVisualizerWidget)


def test_custom_palette_editor_capture_edit_clear_and_errors(window, monkeypatch):
    capture = Mock(return_value=["#ff0000", "#00ff00", "#0000ff"])
    save = Mock()
    dialog = qt_panels.CustomPaletteDialog([], save, capture, window)
    dialog.show()
    slot = dialog.slots[0]
    slot["name"].setText("  Captured  ")
    button = next(button for button in dialog.stack.widget(0).findChildren(qt_panels.QPushButton)
                  if button.text() == "Capture current palette")
    QTest.mouseClick(button, Qt.MouseButton.LeftButton)
    capture.assert_called_once()
    assert [field.text() for field in slot["inputs"][:3]] == capture.return_value
    slot["inputs"][3].setText("not a colour")
    dialog.accept()
    save.assert_not_called()
    assert "HTML hex" in dialog.status.text()
    slot["inputs"][3].setText("#abc")
    monkeypatch.setattr(qt_panels.QColorDialog, "getColor", lambda *_: QColor("#123456"))
    dialog.choose_color(slot["inputs"][1])
    slot["enabled"].setChecked(False)
    dialog.accept()
    save.assert_called_once_with([{
        "slot": 1, "name": "Captured", "enabled": False,
        "colors": ["#ff0000", "#123456", "#0000ff", "#aabbcc"],
    }])

    dialog.clear_slot(0)
    assert not slot["name"].text()
    assert [field.text() for field in slot["inputs"][:2]] == ["", ""]
    assert all(not field.text() for field in slot["inputs"])
    dialog.deleteLater()


def test_custom_palette_editor_save_failure_and_cancel_do_not_save(window):
    save = Mock(side_effect=OSError("disk full"))
    dialog = qt_panels.CustomPaletteDialog([], save, lambda: ["#000000", "#ffffff"], window)
    dialog.slots[0]["name"].setText("Test")
    dialog.capture_colors(0)
    dialog.accept()
    assert "disk full" in dialog.status.text()
    assert dialog.result() == 0
    dialog.reject()
    assert save.call_count == 1
    dialog.deleteLater()


def test_palette_editor_buttons_have_visible_icons_and_tooltips(window, monkeypatch):
    dialog = qt_panels.CustomPaletteDialog([], Mock(), Mock(), window)
    dialog.show()
    QApplication.processEvents()
    buttons = dialog.findChild(qt_panels.QDialogButtonBox)
    save = buttons.button(qt_panels.QDialogButtonBox.StandardButton.Save)
    cancel = buttons.button(qt_panels.QDialogButtonBox.StandardButton.Cancel)
    row = dialog.slots[0]["rows"][0]
    choose = next(button for button in row.findChildren(qt_panels.QPushButton)
                  if button.toolTip() == "Choose colour...")
    remove = next(button for button in row.findChildren(qt_panels.QPushButton)
                  if button.toolTip() == "Remove colour")
    assert not choose.text() and not remove.text()
    for button in (save, cancel, choose, remove):
        image = button.icon().pixmap(20, 20).toImage()
        assert any(image.pixelColor(x, y).alpha() > 200
                   and image.pixelColor(x, y).red() > 220
                   for x in range(image.width()) for y in range(image.height()))
    assert save.palette().color(QPalette.ColorRole.ButtonText).lightness() > 200
    monkeypatch.setattr(qt_panels.QColorDialog, "getColor", lambda *_: QColor("#123456"))
    QTest.mouseClick(choose, Qt.MouseButton.LeftButton)
    assert dialog.slots[0]["inputs"][0].text() == "#123456"
    dialog.slots[0]["inputs"][1].setText("#abcdef")
    dialog.slots[0]["inputs"][2].setText("#ffffff")
    QTest.mouseClick(remove, Qt.MouseButton.LeftButton)
    assert [field.text() for field in dialog.slots[0]["inputs"][:2]] == ["#abcdef", "#ffffff"]
    dialog.reject()
    dialog.deleteLater()


def test_palette_editor_accepts_two_to_eight_optional_fields(window):
    save = Mock()
    dialog = qt_panels.CustomPaletteDialog([], save, Mock(), window)
    dialog.show()
    QApplication.processEvents()
    slot = dialog.slots[0]
    assert all(not row.isHidden() for row in slot["rows"])
    assert not any(button.text() == "Add colour"
                   for button in dialog.findChildren(qt_panels.QPushButton))
    slot["name"].setText("Eight")
    colors = [f"#{index:06x}" for index in range(8)]
    for field, color in zip(slot["inputs"], colors):
        field.setText(color)
    dialog.accept()
    assert save.call_args.args[0][0]["colors"] == colors
    dialog.deleteLater()


def test_palette_editor_skips_unused_fields_but_requires_two_colours(window):
    save = Mock()
    dialog = qt_panels.CustomPaletteDialog([], save, Mock(), window)
    slot = dialog.slots[0]
    slot["name"].setText("Sparse")
    slot["inputs"][0].setText("#ff0000")
    dialog.accept()
    save.assert_not_called()
    assert "two to eight" in dialog.status.text()
    slot["inputs"][7].setText("#0000ff")
    dialog.accept()
    assert save.call_args.args[0][0]["colors"] == ["#ff0000", "#0000ff"]
    dialog.deleteLater()


def test_custom_palette_dropdown_tracks_enabled_edits_and_reset(window):
    palettes = [{"slot": 2, "name": "Sunset", "enabled": True, "colors": ["#ff0000", "#0000ff"]},
                {"slot": 4, "name": "Hidden", "enabled": False, "colors": ["#000000", "#ffffff"]}]
    requests = []
    panel = SidePanel({"customPalettes": palettes, "lightingColorMode": "custom-2"}, "visual",
                      qt_frontend.QT_MODES, lambda *args: requests.append(args))
    assert panel.lighting_color_mode.currentData() == "custom-2"
    assert panel.lighting_color_mode.findData("custom-4") == -1
    changed = [{**palettes[0], "name": "Sunrise"}, {**palettes[1], "enabled": True}]
    panel.sync_fields({"customPalettes": changed, "lightingColorMode": "custom-2"})
    assert panel.lighting_color_mode.currentText() == "Sunrise"
    assert panel.lighting_color_mode.findData("custom-4") >= 0
    assert requests == []
    panel.reset_fields()
    assert panel.lighting_color_mode.currentData() == "artwork"
    assert panel.custom_palettes == changed
    assert not any(request[0] == "custom-palettes" for request in requests)
    panel.deleteLater()


def test_custom_palettes_button_is_in_visual_lighting_section(window):
    visual = SidePanel({}, "visual", qt_frontend.QT_MODES, Mock())
    settings = SidePanel({}, "settings", qt_frontend.QT_MODES, Mock())
    layout = visual.content_layout
    assert layout.indexOf(visual.custom_palettes_button) == layout.indexOf(visual.lighting_color_mode) + 1
    assert not any(button.text() == "Custom palettes..."
                   for button in settings.findChildren(qt_panels.QPushButton))
    visual.deleteLater()
    settings.deleteLater()


def test_custom_palettes_button_label_is_rendered_in_short_panel(window):
    window.open_mode_menu()
    panel = window.side_panel
    panel.resize(400, 420)
    QApplication.processEvents()
    button = panel.custom_palettes_button
    panel.scroll.ensureWidgetVisible(button)
    QApplication.processEvents()
    assert button.height() >= button.sizeHint().height()
    labeled = button.grab().toImage()
    button.setText("")
    QApplication.processEvents()
    unlabeled = button.grab().toImage()
    assert labeled != unlabeled


def test_visual_panel_opens_palette_editor_and_captures_current_colours(window):
    requests = []
    panel = SidePanel({}, "visual", qt_frontend.QT_MODES, lambda *args: requests.append(args),
                      capture_palette=lambda: ["#ff0000", "#00ff00"])
    panel.show()
    def save_palette():
        dialog = QApplication.activeModalWidget()
        try:
            dialog.slots[0]["name"].setText("Captured")
            dialog.capture_colors(0)
            dialog.accept()
        finally:
            dialog.reject()
    QTimer.singleShot(0, save_palette)
    QTest.mouseClick(panel.custom_palettes_button, Qt.MouseButton.LeftButton)
    assert requests == [("custom-palettes", [{
        "slot": 1, "name": "Captured", "enabled": True, "colors": ["#ff0000", "#00ff00"],
    }])]
    panel.close()
    panel.deleteLater()


@pytest.mark.parametrize("count", [2, 8])
@pytest.mark.parametrize("mode", [0, 1, 2, 4])
def test_custom_palettes_render_all_visualizations(window, count, mode):
    colors = ["#ff0000", "#00ff00", "#0000ff", "#ffff00",
              "#ff00ff", "#00ffff", "#ff8800", "#8888ff"][:count]
    palette = {"slot": 1, "name": "Test", "enabled": True, "colors": colors}
    state = {"track": {"status": "playing"}, "visualizerMode": mode,
             "lightingColorMode": "custom-1", "customPalettes": [palette],
             "bars": [.7] * 24, "wave": [math.sin(i / 5) * .5 for i in range(64)]}
    window.backend.get_state.return_value = state
    window.refresh_state()
    visualizer = window.visualizer
    assert [color.name() for color in window.edge_border.colors] == colors
    assert [color.name() for color in visualizer.effect_palette()["colors"]] == colors
    assert [color.name() for _, color in qt_frontend.scope_gradient(
        500, visualizer.effect_palette()).stops()] == colors
    assert len(window.edge_border.border_palette(state)) == count * 2
    assert all(color in window.timeline_slider.styleSheet() for color in colors)
    image = QImage(visualizer.width(), visualizer.height(), QImage.Format.Format_ARGB32_Premultiplied)
    visualizer.paint_scene(QPainter(image))
    assert not image.isNull()

    before = visualizer.custom_background
    palette["colors"] = ["#111111", "#eeeeee"]
    visualizer.paint_scene(QPainter(image))
    assert visualizer.custom_background is not before


def test_artwork_right_click_offers_palette_only_on_effect(window, monkeypatch):
    offered = []
    monkeypatch.setattr(window, "open_artwork_palette_menu", lambda point: offered.append(point))
    window.state = {**window.state, "lightingColorMode": "artwork", "artworkMode": "album"}
    QTest.mouseClick(window.visualizer, Qt.MouseButton.RightButton, pos=QPoint(320, 100))
    assert len(offered) == 1
    window.state["lightingColorMode"] = "rainbow"
    QTest.mouseClick(window.visualizer, Qt.MouseButton.RightButton, pos=QPoint(320, 100))
    assert len(offered) == 1
    window.state["lightingColorMode"] = "artwork"
    QTest.mouseClick(window.timeline_container, Qt.MouseButton.RightButton)
    assert len(offered) == 1
    window.visualizer.track_palette = {"edge": [QColor("red"), QColor("red")]}
    with pytest.raises(ValueError, match="two distinct"):
        window.artwork_palette_colors()
    window.visualizer.track_palette = {"edge": [QColor("red"), QColor("blue")]}
    assert window.artwork_palette_colors() == ["#ff0000", "#0000ff"]


def test_artwork_palette_editor_uses_empty_slot_and_rejects_full(window):
    palettes = [{"slot": 1, "name": "First", "enabled": True, "colors": ["#000", "#fff"]}]
    dialog = qt_panels.CustomPaletteDialog(palettes, Mock(), Mock(), window,
                                            artwork_colors=["#ff0000", "#0000ff"])
    assert dialog.slot_selector.currentData() == 2
    assert [field.text() for field in dialog.slots[1]["inputs"][:2]] == ["#ff0000", "#0000ff"]
    dialog.deleteLater()
    palettes = [{**palettes[0], "slot": slot, "name": str(slot)} for slot in range(1, 9)]
    with pytest.raises(ValueError, match="All eight"):
        qt_panels.CustomPaletteDialog(palettes, Mock(), Mock(), window,
                                     artwork_colors=["#ff0000", "#0000ff"])


def test_artwork_palette_menu_opens_editor_with_sampled_colours(window, monkeypatch):
    window.visualizer.track_palette = {"edge": [QColor("red"), QColor("blue")]}
    open_editor = Mock()
    monkeypatch.setattr(window, "open_artwork_palette_dialog", open_editor)
    window.open_artwork_palette_menu(window.mapToGlobal(QPoint(320, 100)))
    menu = next(menu for menu in window.findChildren(qt_frontend.QMenu)
                if menu.actions() and menu.actions()[0].text() == "Save artwork palette...")
    menu.actions()[0].trigger()
    open_editor.assert_called_once_with(["#ff0000", "#0000ff"])
    menu.close()


@pytest.mark.parametrize("count", [2, 8])
def test_gpu_renders_custom_palette_with_shader(window, count):
    visualizer = window.visualizer
    if not isinstance(visualizer, qt_frontend.GPUVisualizerWidget):
        pytest.skip("hardware OpenGL widget unavailable")
    colors = ["#ff0000", "#00ff00", "#0000ff", "#ffff00",
              "#ff00ff", "#00ffff", "#ff8800", "#8888ff"][:count]
    palette = {"slot": 1, "name": "GPU", "enabled": True, "colors": colors}
    state = {"track": {"status": "playing"}, "visualizerMode": 0, "lightingMode": "border",
             "lightingColorMode": "custom-1", "customPalettes": [palette], "bars": [.7] * 24}
    window.backend.get_state.return_value = state
    window.refresh_state()
    assert visualizer.initialized
    image = visualizer.grabFramebuffer()
    assert not image.isNull()
    assert len(window.edge_border.border_palette(state)) == count * 2
    assert isinstance(window.visualizer, qt_frontend.GPUVisualizerWidget)


def test_fullscreen_button_in_lower_controls_toggles_fullscreen(window):
    button = window.fullscreen_button
    assert button.parentWidget() is window.settings_button.parentWidget()
    assert button.x() > window.settings_button.x()
    assert button.size() == window.settings_button.size()

    QTest.mouseClick(button, Qt.MouseButton.LeftButton)
    QApplication.processEvents()
    assert window.isFullScreen()
    assert button.accessibleName() == "Exit full screen"

    window.show_controls()
    QTest.mouseClick(button, Qt.MouseButton.LeftButton)
    QApplication.processEvents()
    assert not window.isFullScreen()
    assert button.accessibleName() == "Enter full screen"


def test_mouse_movement_restores_fullscreen_cursor(window):
    window.showFullScreen()
    window.cursor_timer.stop()
    window.setCursor(Qt.CursorShape.BlankCursor)
    QTest.mouseMove(window.visualizer, QPoint(30, 30))

    assert window.cursor().shape() == Qt.CursorShape.ArrowCursor
    assert window.cursor_timer.isActive()


def test_mouse_tracking_does_not_consume_button_clicks(window, monkeypatch):
    actions = []
    monkeypatch.setattr(window.backend, "media_control", actions.append)

    QTest.mouseMove(window.next_button, QPoint(10, 10))
    QTest.mouseClick(window.next_button, Qt.MouseButton.LeftButton)

    assert actions == ["Next"]


def test_visualizer_fills_window_and_art_is_rounded(window):
    window.resize(716, 744)
    window.visualizer.fallback_pixmap = None
    art = QPixmap(100, 100)
    art.fill(QColor("red"))
    window.visualizer.art_pixmap = art
    window.visualizer.set_state({
        "track": {"status": "paused"},
        "visualizerMode": 5,
        "albumArtOpacity": 0,
    })
    window.edge_border.opacity = 0
    QApplication.processEvents()

    assert window.visualizer.geometry() == window.centralWidget().rect()
    image = window.visualizer.grab().toImage()
    assert image.pixelColor(358, 372).red() > 240
    assert image.pixelColor(130, 70).red() < 100


def test_album_art_opacity_applies_to_visualization_modes(window):
    art = QPixmap(100, 100)
    art.fill(QColor("red"))
    window.visualizer.art_pixmap = art
    window.visualizer.track_palette = None
    window.visualizer.set_state({
        "track": {"status": "paused"},
        "visualizerMode": 0,
        "albumArtOpacity": 0,
    })
    window.edge_border.opacity = 0
    image = window.visualizer.grab().toImage()
    center = image.pixelColor(window.width() // 2, window.height() // 2)
    assert center.red() < 50


@pytest.mark.parametrize("size", [(640, 420), (1100, 720)])
@pytest.mark.parametrize("controls_visible", [True, False])
@pytest.mark.parametrize("visualizer_mode", [0, 1, 2, 4, 5])
@pytest.mark.parametrize("artwork_mode", ["album", "logo"])
def test_artwork_is_screen_centered(window, monkeypatch, size, controls_visible, visualizer_mode, artwork_mode):
    window.resize(*size)
    if controls_visible:
        window.show_controls()
    else:
        window.hide_controls()
    window.controls_timer.stop()
    QApplication.processEvents()
    visualizer = window.visualizer
    art = QPixmap(100, 100)
    art.fill(QColor("red"))
    visualizer.art_pixmap = art
    visualizer.fallback_pixmap = art
    visualizer.track_palette = None
    window.edge_border.opacity = 0
    drawn = []
    draw_pixmap = QPainter.drawPixmap

    def record_pixmap(painter, *args):
        if len(args) == 3 and isinstance(args[0], QRectF) and isinstance(args[1], QPixmap):
            drawn.append((args[0], args[1].cacheKey()))
        return draw_pixmap(painter, *args)

    monkeypatch.setattr(QPainter, "drawPixmap", record_pixmap)
    visualizer.set_state({
        "track": {"status": "paused"}, "visualizerMode": visualizer_mode,
        "artworkMode": artwork_mode, "albumArtOpacity": 100,
    })
    if isinstance(visualizer, qt_frontend.GPUVisualizerWidget):
        visualizer.grabFramebuffer()
    else:
        visualizer.grab()
    if visualizer_mode == 2:
        key = visualizer.halo_fade_texture.cacheKey()
        expected_size = visualizer.halo_layout()[1] * .23 * 1.35 * 2
    else:
        key = art.cacheKey()
        expected_size = min(visualizer.width(), visualizer.height()) * (
            .64 if visualizer_mode == 5 else .38
        )
    rects = [rect for rect, pixmap_key in drawn if pixmap_key == key]
    assert rects
    rect = rects[-1]
    assert rect.center().x() == pytest.approx(visualizer.width() / 2)
    assert rect.center().y() == pytest.approx(visualizer.height() / 2)
    assert rect.width() == pytest.approx(expected_size)
    assert rect.height() == pytest.approx(expected_size)


@pytest.mark.parametrize("mode,expected", [
    ("album", QColor("red")), ("logo", QColor("lime")), ("none", None),
])
def test_artwork_mode_selects_image(window, mode, expected):
    visualizer = window.visualizer
    album = QPixmap(100, 100)
    album.fill(QColor("red"))
    logo = QPixmap(100, 100)
    logo.fill(QColor("lime"))
    visualizer.art_pixmap = album
    visualizer.fallback_pixmap = logo
    visualizer.track_palette = None
    window.edge_border.opacity = 0
    visualizer.set_state({
        "track": {"status": "paused"}, "visualizerMode": 5, "artworkMode": mode,
    })
    image = visualizer.grab().toImage()
    center = image.pixelColor(visualizer.width() // 2, visualizer.height() // 2)
    if expected is not None:
        assert center == expected
    else:
        assert center.blue() > center.red()
        assert center.blue() > center.green()
    assert visualizer.art_pixmap is album


def test_artwork_mode_selects_palette_and_border(window, monkeypatch):
    visualizer = window.visualizer
    album = QPixmap(100, 100)
    album.fill(QColor("red"))
    logo = QPixmap(100, 100)
    logo.fill(QColor("lime"))
    visualizer.art_pixmap = album
    visualizer.fallback_pixmap = logo
    visualizer.track_palette = visualizer.sample_palette(album)
    base = {"track": {"art": "album", "status": "paused"}, "visualizerMode": 0}
    window.state = base

    for mode, color in (("album", "#ff0000"), ("logo", "#00ff00"),
                        ("none", "#1b5bff"), ("album", "#ff0000")):
        state = {**base, "artworkMode": mode}
        monkeypatch.setattr(window.backend, "get_state", lambda: state)
        window.refresh_state()
        palette = visualizer.active_palette()
        assert (palette is None) == (mode == "none")
        if palette:
            assert palette["edge"][0].name() == color
        assert window.edge_border.colors[0].name() == color

    visualizer.set_state({**base, "artworkMode": "logo", "edgeColorCount": 2})
    assert visualizer.active_palette()["edge"][0].name() == "#00ff00"
    visualizer.clear_art()
    assert visualizer.active_palette()["edge"][0].name() == "#00ff00"


def test_control_borders_follow_artwork_palette_and_restore_default(window, monkeypatch, tmp_path):
    def art_data(color):
        image = QPixmap(32, 32)
        image.fill(QColor(color))
        buffer = QBuffer()
        assert buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        assert image.save(buffer, "PNG")
        return bytes(buffer.data())

    def border_colors():
        QApplication.processEvents()
        colors = []
        for widget in (window.source_button, window.mode_button, window.fullscreen_button,
                       window.settings_button):
            image = widget.grab().toImage()
            colors.append(image.pixelColor(image.width() // 2, 0).name())
        return colors

    default_colors = border_colors()
    red_art = tmp_path / "red.png"
    blue_art = tmp_path / "blue.png"
    red_art.write_bytes(art_data("red"))
    blue_art.write_bytes(art_data("blue"))
    track = {"title": "Red", "art": red_art.as_uri(), "status": "paused"}
    state = {"track": track, "artworkMode": "album"}
    monkeypatch.setattr(window.backend, "get_state", lambda: state)
    window.refresh_state()
    red = window.visualizer.track_palette["colors"][-1].name()
    assert border_colors() == [red] * 4
    assert window.track_artist.palette().color(QPalette.ColorRole.WindowText).name() == red

    state = {"track": {**track, "title": "Blue", "art": blue_art.as_uri()}, "artworkMode": "album"}
    window.refresh_state()
    blue = window.visualizer.track_palette["colors"][-1].name()
    assert blue != red
    assert border_colors() == [blue] * 4
    assert window.track_artist.palette().color(QPalette.ColorRole.WindowText).name() == blue
    window.resize(900, 600)
    window.hide_controls()
    assert window.track_artist.palette().color(QPalette.ColorRole.WindowText).name() == blue

    state = {**state, "artworkMode": "none"}
    window.refresh_state()
    assert border_colors() == default_colors
    assert window.track_artist.palette().color(QPalette.ColorRole.WindowText).alpha() == 102

    logo = QPixmap(32, 32)
    logo.fill(QColor("lime"))
    window.visualizer.fallback_pixmap = logo
    state = {**state, "artworkMode": "logo"}
    window.refresh_state()
    logo_color = window.visualizer.logo_palette["colors"][-1].name()
    assert border_colors() == [logo_color] * 4

    state = {**state, "lightingColorMode": "rainbow", "edgeColorCount": 3}
    window.refresh_state()
    assert border_colors() == ["#ffffff"] * 4
    assert window.track_artist.palette().color(QPalette.ColorRole.WindowText).name() == "#ffffff"
    assert [color.name() for color in window.edge_border.colors] == [
        "#ff0000", "#00ff00", "#0000ff",
    ]
    assert window.visualizer.active_palette()["colors"][-1].name() == logo_color

    state = {**state, "lightingColorMode": "artwork"}
    window.refresh_state()
    assert border_colors() == [logo_color] * 4

    state = {"track": {"status": "paused", "art": "", "artStatus": "failed"},
             "artworkMode": "album"}
    window.refresh_state()
    assert border_colors() == default_colors


def test_artist_text_has_visible_shadow(window):
    window.track_artist.setText("Artist - Album")
    effect = window.track_artist.graphicsEffect()
    assert isinstance(effect, qt_frontend.QGraphicsDropShadowEffect)
    effect.setEnabled(False)
    without_shadow = window.track_artist.grab().toImage()
    effect.setEnabled(True)
    with_shadow = window.track_artist.grab().toImage()
    def on_white(color):
        return (color.lightness() * color.alpha() + 255 * (255 - color.alpha())) // 255
    assert any(on_white(without_shadow.pixelColor(x, y)) - on_white(with_shadow.pixelColor(x, y)) >= 20
               for y in range(with_shadow.height()) for x in range(with_shadow.width()))


@pytest.mark.parametrize("art_color", ["red", "blue", "black", "white"])
def test_control_border_contrasts_with_dark_background(window, art_color):
    art = QPixmap(32, 32)
    art.fill(QColor(art_color))
    color = window.visualizer.sample_palette(art)["colors"][-1]

    def luminance(value):
        channels = (value.redF(), value.greenF(), value.blueF())
        linear = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4
                  for v in channels]
        return .2126 * linear[0] + .7152 * linear[1] + .0722 * linear[2]

    assert (luminance(color) + .05) / (luminance(QColor("#090b12")) + .05) >= 3


def test_default_background_is_blue_in_center_and_black_at_edges(window):
    visualizer = window.visualizer
    visualizer.fallback_pixmap = None
    visualizer.track_palette = None
    window.edge_border.opacity = 0
    visualizer.set_state({"track": {}, "visualizerMode": 5, "artworkMode": "none"})
    image = visualizer.grab().toImage()
    center = image.pixelColor(visualizer.width() // 2, int(visualizer.height() * .42))
    corner = image.pixelColor(0, visualizer.height() - 1)
    assert center.blue() > 80 and center.blue() > center.green()
    assert corner.blue() < 3 and corner.red() < 3 and corner.green() < 3


@pytest.mark.parametrize("swirl,rainbow", [(False, False), (True, False), (False, True)])
def test_border_depth_changes_rendered_extent(window, swirl, rainbow):
    border = window.edge_border
    border.resize(400, 300)
    border.opacity = 1
    border.set_palette(["#ff0000", "#00ff00", "#0000ff"])
    state = {
        "lightingMode": "border", "edgeSwirl": swirl,
        "lightingColorMode": "rainbow" if rainbow else "artwork",
        "edgeFade": 50,
    }

    def render(extra):
        image = QImage(400, 300, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        border.paint_border(painter, {**state, **extra})
        painter.end()
        return image

    shallow, normal, deep = (render({"edgeDepth": value}) for value in (25, 100, 200))
    assert render({}) == normal
    alpha = [image.pixelColor(80, 150).alpha() for image in (shallow, normal, deep)]
    assert alpha[0] == 0
    assert 0 < alpha[1] < alpha[2]


@pytest.mark.parametrize("size", [(800, 400), (400, 800)])
@pytest.mark.parametrize("swirl,rainbow", [(False, False), (True, False), (False, True)])
def test_border_has_circular_centre(window, size, swirl, rainbow):
    border = window.edge_border
    border.resize(*size)
    border.opacity = 1
    border.set_palette(["#ff0000"])
    state = {"edgeSwirl": swirl, "lightingColorMode": "rainbow" if rainbow else "artwork",
             "edgeFade": 50, "edgeDepth": 100}
    image = QImage(*size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    border.paint_border(painter, state)
    painter.end()
    cx, cy = size[0] // 2, size[1] // 2
    assert image.pixelColor(cx, cy).alpha() == 0
    for distance in (120, 160, 190):
        samples = [image.pixelColor(cx + distance, cy).alpha(),
                   image.pixelColor(cx - distance, cy).alpha(),
                   image.pixelColor(cx, cy + distance).alpha(),
                   image.pixelColor(cx, cy - distance).alpha()]
        assert min(samples) > 0
        assert max(samples) - min(samples) <= 3


def test_disco_lights_respond_to_music_and_fade_independently(window):
    border = window.edge_border
    border.set_palette(["#ff0000", "#00ff00", "#0000ff"])
    state = {"lightingMode": "disco", "edgeFade": 50, "edgeSwirl": True,
             "track": {"status": "playing"}, "bars": [1.0] * 6}
    border.update_disco(state, .05)
    assert border.disco_levels == [1, 1, 1]
    border.update_disco({**state, "bars": [0.0] * 6}, .2)
    assert 0 < border.disco_levels[0] < border.disco_levels[1] < border.disco_levels[2] < 1
    slow = border.disco_levels[0]
    border.disco_levels = [1.0] * 3
    border.update_disco({**state, "edgeFade": 0, "track": {"status": "paused"}}, .2)
    assert border.disco_levels[0] < slow
    border.update_disco({**state, "lightingMode": "none"}, .2)
    assert border.disco_levels == []


def test_disco_lights_are_steady_when_swirl_is_off(window):
    border = window.edge_border
    border.resize(780, 520)
    border.opacity = 1
    border.set_palette(["#ff0000", "#00ff00", "#0000ff"])
    state = {"lightingMode": "disco", "edgeSwirl": False,
             "discoLightCount": 10, "track": {"status": "paused"}, "bars": []}
    frames = []
    for track, bars in (({"status": "paused"}, []), ({"status": "playing"}, [0, .5, 1])):
        border.update_disco({**state, "track": track, "bars": bars}, .2)
        assert border.disco_levels == [1, 1, 1]
        image = QImage(780, 520, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        border.paint_border(painter, state)
        painter.end()
        frames.append(image)
    assert frames[0] == frames[1]
    assert frames[0].pixelColor(98, 5).alpha() > 0
    assert frames[0].pixelColor(5, 87).alpha() > 0
    border.update_disco({**state, "lightingMode": "none"}, .2)
    assert border.disco_levels == []


def test_animated_lights_remain_visible_in_quiet_bands_and_fade_when_paused(window):
    border = window.edge_border
    border.resize(780, 520)
    border.opacity = 1
    border.set_palette(["#ff0000", "#00ff00", "#0000ff"])
    state = {"lightingMode": "disco", "discoLightStyle": "bulb", "discoLightCount": 3,
             "discoWireColor": "none", "edgeSwirl": True,
             "track": {"status": "playing"}, "bars": [1, 0, 0]}
    border.update_disco(state, .05)
    assert border.disco_levels == pytest.approx([1, .3, .3])

    def render():
        image = QImage(780, 520, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        border.paint_border(painter, state)
        painter.end()
        return image

    quiet = render()
    assert quiet.pixelColor(30, 260).green() > 0
    assert quiet.pixelColor(750, 260).blue() > 0
    border.update_disco({**state, "bars": [1, 1, 1]}, .05)
    loud = render()
    assert loud.pixelColor(30, 260).alpha() > quiet.pixelColor(30, 260).alpha()
    for _ in range(100):
        border.update_disco({**state, "track": {"status": "paused"}}, .25)
    assert max(border.disco_levels) < .001


def test_disco_swirl_toggle_is_available_in_settings(window):
    dialog = SidePanel({"lightingMode": "disco"}, "visual", qt_frontend.QT_MODES,
                       lambda path: None, window)
    assert dialog.lighting_mode.currentText() == "Lights"
    assert dialog.edge_swirl.isEnabled()
    dialog.lighting_mode.setCurrentIndex(dialog.lighting_mode.findData("none"))
    assert not dialog.edge_swirl.isEnabled()
    dialog.lighting_mode.setCurrentIndex(dialog.lighting_mode.findData("border"))
    assert dialog.edge_swirl.isEnabled()
    dialog.close()


def test_disco_lights_render_on_top_and_sides_without_border(window):
    border = window.edge_border
    border.resize(780, 520)
    border.opacity = 1
    border.set_palette(["#ff0000", "#00ff00", "#0000ff"])
    state = {"lightingMode": "disco", "edgeFade": 50, "edgeSwirl": True,
             "discoLightCount": 10, "visualizerMode": 5,
             "track": {"status": "playing"}, "bars": [1] * 6}
    border.update_disco(state, .05)

    def render(mode):
        image = QImage(780, 520, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        border.paint_border(painter, {**state, "lightingMode": mode})
        painter.end()
        return image

    image = render("disco")
    assert image.pixelColor(98, 5).red() > image.pixelColor(98, 5).green()
    assert image.pixelColor(293, 5).green() > image.pixelColor(293, 5).red()
    assert image.pixelColor(5, 87).alpha() > 0
    assert image.pixelColor(775, 87).alpha() > 0
    assert image.pixelColor(390, 519).alpha() == 0
    assert render("none").pixelColor(65, 5).alpha() == 0


@pytest.mark.parametrize("count", [3, 10, 40])
def test_disco_light_count_and_spacing(count):
    positions = qt_frontend.disco_light_positions(780, 520, count)
    top = [x for x, y in positions if y == 0]
    left = [y for x, y in positions if x == 0]
    right = [y for x, y in positions if x == 780]
    assert len(positions) == count
    assert top and left and right
    for group in (top, left, right):
        gaps = [b - a for a, b in zip(group, group[1:])]
        assert not gaps or max(gaps) - min(gaps) < 1e-9


def test_disco_light_size_changes_radius(window):
    border = window.edge_border
    border.resize(780, 520)
    border.set_palette(["#ff0000"])
    state = {"lightingMode": "disco", "edgeFade": 50, "discoLightCount": 6,
             "track": {"status": "playing"}, "bars": [1]}
    border.update_disco(state, .05)

    def sample(size):
        image = QImage(780, 520, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        border.paint_border(painter, {**state, "discoLightSize": size})
        painter.end()
        return image.pixelColor(390, 120).alpha()

    assert sample(100) == 0
    assert sample(200) > 0


def test_disco_light_has_solid_core_and_adjustable_ambient_fade(window):
    border = window.edge_border
    border.resize(780, 520)
    border.opacity = 1
    border.set_palette(["#ff0000"])
    state = {"lightingMode": "disco", "discoLightCount": 3,
             "track": {"status": "playing"}, "bars": [1]}
    border.update_disco(state, .05)

    def samples(fade):
        image = QImage(780, 520, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        border.paint_border(painter, {**state, "edgeFade": fade})
        painter.end()
        return [image.pixelColor(390, y).alpha() for y in (10, 30, 65, 100)]

    sharp = samples(0)
    soft = samples(100)
    assert abs(sharp[0] - sharp[1]) <= 1
    assert abs(soft[0] - soft[1]) <= 1
    assert sharp[0] == soft[0]
    assert 0 < sharp[2] < soft[2] < soft[0]
    assert sharp[3] == soft[3] == 0


def test_border_depth_does_not_change_disco_lights(window):
    border = window.edge_border
    border.resize(400, 300)
    border.set_palette(["#ff0000"])
    state = {"lightingMode": "disco", "discoLightCount": 3,
             "track": {"status": "playing"}, "bars": [1]}
    border.update_disco(state, .05)

    def render(depth):
        image = QImage(400, 300, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        border.paint_border(painter, {**state, "edgeDepth": depth})
        painter.end()
        return image

    assert render(25) == render(200)


def test_festive_light_styles_render_on_all_sides_and_honor_wire_colour(window):
    border = window.edge_border
    border.resize(780, 520)
    border.opacity = 1
    border.set_palette(["#ff0000"])
    state = {"lightingMode": "disco", "discoLightCount": 3, "edgeSwirl": False}
    border.update_disco(state, .05)

    def render(style, wire="green"):
        image = QImage(780, 520, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        border.paint_border(painter, {**state, "discoLightStyle": style, "discoWireColor": wire})
        painter.end()
        return image

    images = [render(style) for style in ("fairy", "bulb", "star")]
    assert len({bytes(image.bits()) for image in images}) == 3
    for image in images:
        assert image.pixelColor(390, 16).red() > 0
        assert image.pixelColor(16, 260).red() > 0
        assert image.pixelColor(763, 260).red() > 0
        assert image.pixelColor(390, 519).alpha() == 0
    green = images[0].pixelColor(195, 10)
    themed = render("fairy", "theme").pixelColor(195, 10)
    assert green.green() > green.red()
    assert themed.red() > themed.green()
    border.opacity = 0
    assert render("bulb").pixelColor(195, 10).alpha() == 0
    assert render("bulb").pixelColor(390, 16).alpha() == 0


def test_festive_light_wire_control_tracks_style_and_sync(window):
    actions = []
    panel = SidePanel({}, "visual", qt_frontend.QT_MODES,
                      lambda name, value: actions.append((name, value)), window)
    assert not panel.disco_wire_color.isEnabled()
    assert panel.light_wire_settings.isHidden()
    panel.disco_style.setCurrentIndex(panel.disco_style.findData("fairy"))
    assert panel.disco_wire_color.isEnabled()
    assert not panel.light_wire_settings.isHidden()
    panel.disco_wire_color.setCurrentIndex(panel.disco_wire_color.findData("theme"))
    assert actions == [("disco-light-style", "fairy"), ("disco-wire-color", "theme")]
    panel.disco_wire_color.setCurrentIndex(panel.disco_wire_color.findData("none"))
    assert actions[-1] == ("disco-wire-color", "none")
    panel.sync_fields({"discoLightStyle": "glow"})
    assert not panel.disco_wire_color.isEnabled()
    assert panel.light_wire_settings.isHidden()
    assert len(actions) == 3
    panel.reset_button.click()
    assert panel.disco_wire_color.currentData() == "green"


def test_visual_controls_follow_lighting_mode_and_keep_hidden_values(window):
    actions = []
    panel = SidePanel({}, "visual", qt_frontend.QT_MODES,
                      lambda name, value: actions.append((name, value)), window)

    def visibility():
        return tuple(not widget.isHidden() for widget in (
            panel.lighting_count_settings, panel.lighting_appearance,
            panel.border_settings, panel.light_settings, panel.edge_swirl))

    assert visibility() == (True, True, True, False, True)
    panel.disco_style.setCurrentIndex(panel.disco_style.findData("star"))
    panel.lighting_mode.setCurrentIndex(panel.lighting_mode.findData("disco"))
    assert visibility() == (True, True, False, True, True)
    panel.sync_fields({"lightingMode": "none"})
    assert visibility() == (False, False, False, False, False)
    assert panel.disco_style.currentData() == "star"
    panel.lighting_color_mode.setCurrentIndex(panel.lighting_color_mode.findData("rainbow"))
    assert visibility() == (True, False, False, False, False)
    panel.reset_button.click()
    assert visibility() == (True, True, True, False, True)
    assert panel.disco_style.currentData() == "glow"


def test_festive_wire_bends_between_lights_and_wireless_hides_it(window):
    border = window.edge_border
    border.resize(780, 520)
    border.opacity = 1
    border.set_palette(["#ff0000"])
    state = {"lightingMode": "disco", "discoLightStyle": "bulb", "discoLightCount": 3,
             "edgeSwirl": True, "track": {"status": "paused"}, "bars": [0]}
    border.update_disco(state, .05)

    def render(wire):
        image = QImage(780, 520, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        border.paint_border(painter, {**state, "discoWireColor": wire})
        painter.end()
        return image

    wired = render("green")
    wireless = render("none")
    assert wired.pixelColor(195, 10).alpha() > 0
    assert wired.pixelColor(195, 2).alpha() == 0
    assert wireless.pixelColor(195, 10).alpha() == 0
    for x in (10, 769):
        assert wired.pixelColor(x, 130).alpha() > 0
        assert wireless.pixelColor(x, 130).alpha() == 0
    assert wireless.pixelColor(390, 16).alpha() == 0
    border.update_disco({**state, "track": {"status": "playing"}, "bars": [1]}, .05)
    assert render("none").pixelColor(390, 30).alpha() > 0


@pytest.mark.parametrize("style", ["fairy", "bulb", "star"])
def test_festive_lights_react_to_audio_and_follow_size(window, style):
    border = window.edge_border
    border.resize(780, 520)
    border.set_palette(["#ff0000"])
    state = {"lightingMode": "disco", "discoLightStyle": style, "discoLightCount": 3,
             "edgeSwirl": True, "track": {"status": "paused"}, "bars": [0]}

    def render(size=100):
        image = QImage(780, 520, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        border.paint_border(painter, {**state, "discoLightSize": size})
        painter.end()
        return image

    border.update_disco(state, .05)
    assert render().pixelColor(390, 30).alpha() == 0
    border.update_disco({**state, "track": {"status": "playing"}, "bars": [1]}, .05)
    normal = render()
    large = render(200)
    assert normal.pixelColor(390, 30).alpha() > 0
    assert large.pixelColor(390, 65).alpha() > normal.pixelColor(390, 65).alpha()


def test_vortex_renders_distinct_from_empty_scene(window, monkeypatch):
    window.visualizer.fallback_pixmap = None
    window.visualizer.art_pixmap = None
    window.visualizer.set_state({
        "track": {"status": "playing"},
        "visualizerMode": 4,
        "bars": [.4] * 24,
    })
    window.edge_border.opacity = 0
    now = qt_frontend.time.monotonic()
    monkeypatch.setattr(qt_frontend.time, "monotonic", lambda: now)
    first = window.visualizer.grab().toImage()
    monkeypatch.setattr(qt_frontend.time, "monotonic", lambda: now + 1)
    window.visualizer.update()
    second = window.visualizer.grab().toImage()

    assert first != second
    assert any(first.pixelColor(x, y) != QColor("#090b12")
               for x in range(100, window.width() - 100, 20)
               for y in range(80, 450, 20))


@pytest.mark.parametrize("mode", [0, 1, 2, 4])
def test_visualizations_follow_lighting_theme_and_restore_artwork(window, monkeypatch, mode):
    visualizer = window.visualizer
    visualizer.fallback_pixmap = None
    visualizer.art_pixmap = None
    visualizer.track_palette = {
        "base": QColor("black"), "background": [],
        "colors": [QColor("blue")] * 3, "edge": [QColor("blue")],
    }
    monkeypatch.setattr(visualizer, "paint_background",
                        lambda painter, width, height: painter.fillRect(0, 0, width, height,
                                                                       QColor("black")))
    now = qt_frontend.time.monotonic()
    monkeypatch.setattr(qt_frontend.time, "monotonic", lambda: now)
    state = {"track": {"status": "playing"}, "visualizerMode": mode,
             "artworkMode": "album", "edgeColorCount": 3,
             "bars": [.7] * 24, "wave": [math.sin(i / 5) * .5 for i in range(64)]}

    def render(theme):
        visualizer.set_state({**state, "lightingColorMode": theme})
        visualizer.last_frame = now
        visualizer.vortex_beat = 0
        image = QImage(visualizer.width(), visualizer.height(), QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        visualizer.paint_scene(QPainter(image))
        return image

    artwork = render("artwork")
    rainbow = render("rainbow")
    christmas = render("christmas")
    assert artwork != rainbow
    assert artwork != christmas
    assert rainbow != christmas
    assert render("artwork") == artwork


def test_halo_renders_audio_and_selected_palette(window):
    visualizer = window.visualizer
    visualizer.fallback_pixmap = None
    visualizer.art_pixmap = None
    window.edge_border.opacity = 0
    visualizer.track_palette = {
        "base": QColor("black"), "background": [],
        "colors": [QColor("red")] * 3, "edge": [QColor("red")],
    }
    state = {"track": {"status": "paused"}, "visualizerMode": 2,
             "artworkMode": "album", "lightingColorMode": "artwork"}
    def image():
        if isinstance(visualizer, qt_frontend.GPUVisualizerWidget):
            return visualizer.grabFramebuffer()
        return visualizer.grab().toImage()

    visualizer.set_state(state)
    paused = image()
    x = round(visualizer.width() / 2 * paused.devicePixelRatio())
    cy = visualizer.height() / 2
    y = round(cy * paused.devicePixelRatio())
    assert paused.pixelColor(x, y).red() > paused.pixelColor(x, y).blue()

    visualizer.set_state({**state, "track": {"status": "playing"}, "bars": [.8] * 24})
    playing = image()
    assert playing != paused
    assert visualizer.frame_timer.isActive()
    visualizer.set_state({**state, "artworkMode": "none"})
    assert not visualizer.frame_timer.isActive()
    default = image()
    assert default.pixelColor(x, y).blue() > default.pixelColor(x, y).red()


def test_halo_bass_pulses_quiet_sections_without_exceeding_layout(window, monkeypatch):
    visualizer = window.visualizer
    visualizer.fallback_pixmap = None
    visualizer.art_pixmap = None
    paths = []
    monkeypatch.setattr(qt_frontend, "paint_scope_path",
                        lambda painter, path, gradient, line_width: paths.append(path))
    now = qt_frontend.time.monotonic()
    monkeypatch.setattr(qt_frontend.time, "monotonic", lambda: now)
    state = {"track": {"status": "playing"}, "visualizerMode": 2, "artworkMode": "none"}

    def radii(bars):
        visualizer.set_state({**state, "bars": bars})
        visualizer.last_frame = now - .1
        image = QImage(visualizer.width(), visualizer.height(), QImage.Format.Format_ARGB32_Premultiplied)
        visualizer.paint_scene(QPainter(image))
        path = paths[-1]
        cx, cy = visualizer.width() / 2, visualizer.height() / 2
        return [math.hypot(path.elementAt(i).x - cx, path.elementAt(i).y - cy)
                for i in range(len(bars))]

    quiet = radii([.2] * 3 + [.1] * 21)
    loud = radii([.8] * 3 + [.1] * 21)
    assert all(after > before for before, after in zip(quiet, loud))
    assert loud[6] - quiet[6] == pytest.approx(visualizer.halo_layout()[1] * .04 * .6)
    assert max(radii([1] * 24)) == pytest.approx(visualizer.halo_layout()[1] * .45)


@pytest.mark.parametrize("artwork_mode", ["album", "logo"])
def test_halo_artwork_fades_outward_and_responds_to_bass(window, artwork_mode):
    visualizer = window.visualizer
    window.edge_border.opacity = 0
    visualizer.track_palette = None
    red = QPixmap(300, 100)
    red.fill(QColor("red"))
    green = QPixmap(300, 100)
    green.fill(QColor("lime"))
    attribute = "art_pixmap" if artwork_mode == "album" else "fallback_pixmap"
    setattr(visualizer, attribute, red)
    state = {"track": {"status": "paused"}, "visualizerMode": 2,
             "artworkMode": artwork_mode, "albumArtOpacity": 100,
             "lightingColorMode": "rainbow"}
    visualizer.set_state(state)

    def image():
        if isinstance(visualizer, qt_frontend.GPUVisualizerWidget):
            return visualizer.grabFramebuffer()
        return visualizer.grab().toImage()

    red_image = image()
    cached_key = visualizer.halo_fade_texture.cacheKey()
    image()
    assert visualizer.halo_fade_texture.cacheKey() == cached_key
    setattr(visualizer, attribute, green)
    green_image = image()
    assert visualizer.halo_fade_texture.cacheKey() != cached_key
    ratio = red_image.devicePixelRatio()
    width, height = visualizer.width(), visualizer.height()
    cx, cy = width / 2, height / 2
    radius = visualizer.halo_layout()[1] * .23

    def pixel(frame, x, y):
        return frame.pixelColor(round(x * ratio), round(y * ratio))

    assert pixel(red_image, cx, cy) == QColor("red")
    assert pixel(green_image, cx, cy) == QColor("lime")
    near = pixel(red_image, cx + radius * 1.05, cy).red()
    far = pixel(red_image, cx + radius * 1.25, cy).red()
    outside = pixel(red_image, cx + radius * 1.5, cy).red()
    assert 255 > near > 180 and near > far > outside
    assert pixel(red_image, cx + radius * 1.5, cy) == pixel(green_image, cx + radius * 1.5, cy)
    corner = pixel(red_image, cx + radius * .9, cy + radius * .72)
    assert 0 < corner.red() < 255

    visualizer.set_state({**state, "track": {"status": "playing"}, "bars": [1.0] * 24})
    playing = image()
    assert pixel(playing, cx + radius * 1.5, cy).green() > (
        pixel(green_image, cx + radius * 1.5, cy).green() + 5
    )

    visualizer.set_state({**state, "albumArtOpacity": 0})
    transparent = image()
    assert pixel(transparent, cx, cy) != QColor("lime")


def test_halo_artwork_centre_moves_with_audio(window):
    visualizer = window.visualizer
    window.edge_border.opacity = 0
    art = QPixmap(200, 160)
    art.fill(QColor("lime"))
    painter = QPainter(art)
    painter.fillRect(140, 0, 60, 160, QColor("red"))
    painter.end()
    visualizer.art_pixmap = art
    visualizer.track_palette = None
    state = {"track": {"status": "paused"}, "visualizerMode": 2,
             "artworkMode": "album", "albumArtOpacity": 100,
             "lightingColorMode": "rainbow"}

    def image():
        if isinstance(visualizer, qt_frontend.GPUVisualizerWidget):
            return visualizer.grabFramebuffer()
        return visualizer.grab().toImage()

    visualizer.set_state(state)
    quiet = image()
    width, height = visualizer.width(), visualizer.height()
    radius = visualizer.halo_layout()[1] * .23
    ratio = quiet.devicePixelRatio()
    x = round((width / 2 + radius * .7) * ratio)
    y = round(height / 2 * ratio)
    visualizer.set_state({**state, "track": {"status": "playing"}, "bars": [1.0] * 24})
    playing = image()
    assert quiet.pixelColor(x, y).red() > quiet.pixelColor(x, y).green()
    assert playing.pixelColor(x, y).green() > playing.pixelColor(x, y).red()


def test_halo_artwork_fade_is_circular(window, monkeypatch):
    visualizer = window.visualizer
    window.edge_border.opacity = 0
    art = QPixmap(300, 100)
    art.fill(QColor("red"))
    visualizer.art_pixmap = art
    monkeypatch.setattr(visualizer, "paint_background",
                        lambda painter, width, height: painter.fillRect(0, 0, width, height,
                                                                       QColor("black")))
    visualizer.set_state({
        "track": {"status": "paused"}, "visualizerMode": 2,
        "artworkMode": "album", "albumArtOpacity": 100,
    })
    image = (visualizer.grabFramebuffer()
             if isinstance(visualizer, qt_frontend.GPUVisualizerWidget)
             else visualizer.grab().toImage())
    ratio = image.devicePixelRatio()
    radius = visualizer.halo_layout()[1] * .23 * 1.35
    cx, cy = visualizer.width() / 2, visualizer.height() / 2
    values = [
        image.pixelColor(round((cx + radius * .9) * ratio), round(cy * ratio)).red(),
        image.pixelColor(round(cx * ratio), round((cy + radius * .9) * ratio)).red(),
    ]
    assert 0 < min(values) < 255
    assert max(values) - min(values) <= 4


@pytest.mark.parametrize("size", [(640, 420), (800, 600), (1400, 800)])
@pytest.mark.parametrize("controls_visible", [True, False])
def test_halo_scales_to_available_space_and_uses_scope_style(window, monkeypatch, size, controls_visible):
    window.resize(*size)
    if controls_visible:
        window.show_controls()
    else:
        window.hide_controls()
    window.controls_timer.stop()
    QApplication.processEvents()
    visualizer = window.visualizer
    visualizer.fallback_pixmap = None
    window.edge_border.opacity = 0
    strokes = []
    paint_scope_path = qt_frontend.paint_scope_path

    def record_stroke(painter, path, gradient, line_width=3.5):
        strokes.append((path.boundingRect(), gradient.stops()))
        paint_scope_path(painter, path, gradient, line_width)

    monkeypatch.setattr(qt_frontend, "paint_scope_path", record_stroke)
    state = {"track": {"status": "playing"}, "visualizerMode": 2,
             "artworkMode": "none", "bars": [1.0] * 24}
    visualizer.set_state(state)
    if isinstance(visualizer, qt_frontend.GPUVisualizerWidget):
        visualizer.grabFramebuffer()
    else:
        visualizer.grab()
    bounds, halo_colors = strokes[-1]
    bottom, available = visualizer.halo_layout()
    overlay = window.track_title
    assert bottom == visualizer.mapFromGlobal(overlay.mapToGlobal(QPoint(0, 0))).y() - 8
    assert bounds.width() == pytest.approx(available * .9)
    assert bounds.height() == pytest.approx(available * .9)
    assert bounds.center().x() == pytest.approx(visualizer.width() / 2)
    assert bounds.center().y() == pytest.approx(visualizer.height() / 2)
    assert bounds.bottom() < bottom

    visualizer.set_state({**state, "visualizerMode": 1, "wave": [.2] * 64})
    if isinstance(visualizer, qt_frontend.GPUVisualizerWidget):
        visualizer.grabFramebuffer()
    else:
        visualizer.grab()
    assert strokes[-1][1] == halo_colors


@pytest.mark.parametrize("size", [(640, 420), (1100, 720)])
@pytest.mark.parametrize("mode", [0, 1, 2, 4, 5])
def test_effects_keep_their_layout_when_controls_toggle(window, monkeypatch, size, mode):
    window.resize(*size)
    window.hide_controls()
    window.controls_timer.stop()
    QApplication.processEvents()
    visualizer = window.visualizer
    window.edge_border.opacity = 0
    now = qt_frontend.time.monotonic()
    monkeypatch.setattr(qt_frontend.time, "monotonic", lambda: now)
    visualizer.set_state({
        "track": {"status": "playing"}, "visualizerMode": mode,
        "artworkMode": "logo", "bars": [.4] * 24,
        "wave": [math.sin(index * .2) * .5 for index in range(64)],
    })
    visualizer.frame_timer.stop()

    def image():
        if isinstance(visualizer, qt_frontend.GPUVisualizerWidget):
            return visualizer.grabFramebuffer()
        return visualizer.grab().toImage()

    hidden = image()
    window.show_controls()
    window.controls_timer.stop()
    QApplication.processEvents()
    assert image() == hidden


def test_halo_mode_is_available_in_visual_effects(window, monkeypatch):
    assert (2, "Halo") in qt_frontend.QT_MODES
    actions = []
    monkeypatch.setattr(window.backend, "update_setting",
                        lambda name, value, source_id=None: actions.append((name, value)))
    window.open_mode_menu()
    panel = window.side_panel
    panel.mode.setCurrentIndex(panel.mode.findData(2))
    assert ("visualizer-mode", 2) in actions


def test_spectrum_bars_respond_to_audio_and_clear_when_paused(window, monkeypatch):
    visualizer = window.visualizer
    visualizer.fallback_pixmap = None
    window.edge_border.opacity = 0
    state = {"track": {"status": "playing"}, "visualizerMode": 0,
             "artworkMode": "none", "bars": [1.0] * 24}
    now = qt_frontend.time.monotonic()
    monkeypatch.setattr(qt_frontend.time, "monotonic", lambda: now)
    def image():
        if isinstance(visualizer, qt_frontend.GPUVisualizerWidget):
            return visualizer.grabFramebuffer()
        return visualizer.grab().toImage()

    visualizer.set_state(state)
    playing = image()
    width, height = visualizer.width(), visualizer.height()
    x = int(width * .08 + width * .84 / 48)
    bottom = height - 112
    y = int(bottom - max(180, bottom - 72) * .3)
    assert visualizer.peaks[0] == 1

    monkeypatch.setattr(qt_frontend.time, "monotonic", lambda: now + .1)
    visualizer.set_state({**state, "bars": [0.0] * 24})
    quiet = image()
    ratio = playing.devicePixelRatio()
    assert playing.pixelColor(round(x * ratio), round(y * ratio)) != quiet.pixelColor(
        round(x * ratio), round(y * ratio)
    )
    assert 0 < visualizer.peaks[0] < 1

    visualizer.set_state({**state, "track": {"status": "paused"}})
    image()
    assert visualizer.smoothed_bars == []
    assert visualizer.peaks == []


@pytest.mark.parametrize("width", [716, 1920])
def test_scope_renders_waveform_and_clears_it_when_paused(window, width):
    window.resize(width, 720)
    QApplication.processEvents()
    window.visualizer.fallback_pixmap = None
    window.edge_border.opacity = 0
    state = {
        "track": {"status": "playing"},
        "visualizerMode": 1,
        "wave": [math.sin(index * 2.7) * .7 for index in range(64)],
    }
    window.visualizer.set_state(state)
    playing = window.visualizer.grab().toImage()

    window.visualizer.set_state({**state, "track": {"status": "paused"}})
    paused = window.visualizer.grab().toImage()

    assert playing != paused
    assert window.visualizer.smoothed_wave == []


def test_scope_zero_line_can_be_hidden_without_hiding_waveform(window):
    visualizer = window.visualizer
    visualizer.fallback_pixmap = None
    state = {"track": {"status": "paused"}, "visualizerMode": 1, "artworkMode": "none"}
    visualizer.set_state(state)
    with_line = visualizer.grab().toImage()
    visualizer.set_state({**state, "scopeZeroLine": False})
    without_line = visualizer.grab().toImage()
    assert with_line != without_line

    visualizer.set_state({**state, "scopeZeroLine": False,
                          "track": {"status": "playing"}, "wave": [.5] * 64})
    assert visualizer.grab().toImage() != without_line
    visualizer.set_state({**state, "scopeZeroLine": True})
    assert visualizer.grab().toImage() == with_line


def test_scope_zero_line_visual_setting_applies_and_resets(window, monkeypatch):
    actions = []
    monkeypatch.setattr(window.backend, "update_setting",
                        lambda name, value, source_id=None: actions.append((name, value)))
    window.open_mode_menu()
    panel = window.side_panel
    assert panel.scope_zero_line.isChecked()
    panel.scope_zero_line.setChecked(False)
    assert actions == [("scope-zero-line", False)]
    panel.reset_button.click()
    assert actions[-1] == ("scope-zero-line", True)


def test_line_settings_follow_selected_mode_and_sync(window, monkeypatch):
    actions = []
    monkeypatch.setattr(window.backend, "update_setting",
                        lambda name, value, source_id=None: actions.append((name, value)))
    window.open_mode_menu()
    panel = window.side_panel
    assert panel.line_settings.isHidden()
    panel.mode.setCurrentIndex(panel.mode.findData(1))
    assert not panel.line_settings.isHidden()
    assert not panel.scope_zero_line.isHidden()
    panel.scope_width.setValue(200)
    assert panel.scope_width_value.text() == "200%"
    assert actions[-1] == ("scope-line-width", 200)
    panel.sync_fields({"visualizerMode": 2})
    assert not panel.line_settings.isHidden()
    assert panel.scope_zero_line.isHidden()
    panel.sync_fields({"visualizerMode": 1})
    assert not panel.line_settings.isHidden()
    assert not panel.scope_zero_line.isHidden()
    panel.reset_button.click()
    assert panel.line_settings.isHidden()
    assert panel.scope_width.value() == 100
    assert actions[-1] == ("scope-line-width", 100)


@pytest.mark.parametrize("percent,expected", [(25, .875), (100, 3.5), (300, 10.5)])
def test_line_thickness_changes_scope_and_halo_rendered_pens(window, monkeypatch, percent, expected):
    visualizer = window.visualizer
    visualizer.fallback_pixmap = None
    window.edge_border.opacity = 0
    strokes = []
    original = QPainter.drawPath

    def record(painter, path):
        strokes.append(painter.pen().widthF())
        original(painter, path)

    monkeypatch.setattr(QPainter, "drawPath", record)
    state = {"track": {"status": "playing"}, "visualizerMode": 1, "artworkMode": "none",
             "scopeZeroLine": False, "scopeLineWidth": percent, "wave": [.5] * 64}
    visualizer.set_state(state)
    if isinstance(visualizer, qt_frontend.GPUVisualizerWidget):
        visualizer.grabFramebuffer()
    else:
        visualizer.grab()
    assert strokes[-4:] == pytest.approx([expected * 2, expected * 4, expected * 18 / 7, expected])
    strokes.clear()
    visualizer.set_state({**state, "visualizerMode": 2, "bars": [.5] * 24})
    if isinstance(visualizer, qt_frontend.GPUVisualizerWidget):
        visualizer.grabFramebuffer()
    else:
        visualizer.grab()
    assert strokes[-4:] == pytest.approx([expected * 2, expected * 4, expected * 18 / 7, expected])


def test_scope_interpolates_between_audio_updates_and_stops_when_paused(window):
    state = {"track": {"status": "playing"}, "visualizerMode": 1, "wave": [0.0] * 64}
    window.visualizer.set_state(state)
    window.visualizer.grab()
    window.visualizer.set_state({**state, "wave": [1.0] * 64})
    window.visualizer.grab()
    QApplication.processEvents()
    initial = window.visualizer.smoothed_wave[0]

    QTest.qWait(120)
    assert window.visualizer.smoothed_wave[0] > initial

    window.visualizer.hide()
    assert not window.visualizer.frame_timer.isActive()
    window.visualizer.show()
    assert window.visualizer.frame_timer.isActive()
    window.visualizer.set_state({**state, "track": {"status": "paused"}})
    assert not window.visualizer.frame_timer.isActive()
    window.visualizer.set_state({**state, "visualizerMode": 0})
    assert not window.visualizer.frame_timer.isActive()


def test_visualizer_background_updates_after_palette_and_size_changes(window):
    visualizer = window.visualizer
    visualizer.fallback_pixmap = None
    window.edge_border.opacity = 0
    visualizer.track_palette = {
        "base": QColor("red"), "background": [], "colors": [QColor("red")] * 3,
    }
    first = visualizer.grab().toImage()
    visualizer.track_palette = {
        "base": QColor("blue"), "background": [], "colors": [QColor("blue")] * 3,
    }
    second = visualizer.grab().toImage()
    assert first.pixelColor(20, 20) != second.pixelColor(20, 20)
    window.resize(900, 600)
    QApplication.processEvents()
    resized = visualizer.grab().toImage()
    assert resized.pixelColor(20, 20) == second.pixelColor(20, 20)


def test_rainbow_background_restores_default_without_changing_visualizer_palette(window):
    visualizer = window.visualizer
    visualizer.track_palette = {
        "base": QColor("red"), "background": [QColor("red"), QColor("red")],
        "colors": [QColor("red")] * 3, "edge": [QColor("red")] * 3,
    }

    def background():
        image = QImage(300, 200, QImage.Format.Format_ARGB32_Premultiplied)
        painter = QPainter(image)
        visualizer.paint_background(painter, 300, 200)
        painter.end()
        return image.pixelColor(150, 84)

    visualizer.set_state({"artworkMode": "album", "lightingColorMode": "artwork"})
    artwork_color = background()
    visualizer.set_state({"artworkMode": "album", "lightingColorMode": "rainbow"})
    default_color = background()
    assert default_color.blue() > default_color.red()
    assert default_color != artwork_color
    assert visualizer.active_palette()["colors"][0] == QColor("red")
    visualizer.set_state({"artworkMode": "album", "lightingColorMode": "artwork"})
    assert background() == artwork_color


@pytest.mark.parametrize("size", [(800, 600), (700, 900)])
def test_background_radial_gradient_has_expected_geometry(window, size):
    window.resize(*size)
    window.visualizer.fallback_pixmap = None
    window.edge_border.opacity = 0
    window.visualizer.track_palette = {
        "base": QColor("black"),
        "background": [QColor("black"), QColor("red")],
        "colors": [QColor("red")] * 3,
    }
    QApplication.processEvents()
    image = window.visualizer.grab().toImage()
    width, height = window.visualizer.width(), window.visualizer.height()
    for x, y in ((int(width * .12), int(height * .15)),
                 (int(width * .5), int(height * .42)),
                 (int(width * .75), int(height * .8))):
        cx, cy = width * .12, height * .15
        radius = math.hypot(max(cx, width - cx), max(cy, height - cy)) * .58
        circle = max(0, 1 - math.hypot(x + .5 - cx, y + .5 - cy) / radius)
        cx, cy = width * .5, height * .42
        rx = max(cx, width - cx) * math.sqrt(2) * .58
        ry = max(cy, height - cy) * math.sqrt(2) * .58
        ellipse = max(0, 1 - math.hypot((x + .5 - cx) / rx, (y + .5 - cy) / ry))
        expected_red = 255 * .58 * circle * (1 - .72 * ellipse)
        assert abs(image.pixelColor(x, y).red() - expected_red) < 4


def test_f11_and_escape_toggle_fullscreen(window):
    QTest.keyClick(window, Qt.Key.Key_F)
    assert not window.isFullScreen()
    QTest.keyClick(window, Qt.Key.Key_F11)
    assert window.isFullScreen()
    QTest.keyClick(window, Qt.Key.Key_Escape)
    assert not window.isFullScreen()
    QTest.keyClick(window, Qt.Key.Key_F11)
    QTest.keyClick(window, Qt.Key.Key_F11)
    assert not window.isFullScreen()


@pytest.mark.parametrize("fraction,kind", [(0, "visual"), (1, "visual"),
                                          (2, "settings"), (3, "settings")])
def test_screen_side_click_opens_panel(window, fraction, kind):
    width = window.visualizer.width()
    x = (1, (width - 1) // 3, (width * 2 + 2) // 3, width - 1)[fraction]
    QTest.mouseClick(window.visualizer, Qt.MouseButton.LeftButton, pos=QPoint(x, 100))
    assert window.side_panel.kind == kind


def test_middle_double_click_cycles_and_wraps_modes(window, monkeypatch):
    actions = []
    monkeypatch.setattr(window.backend, "update_setting",
                        lambda name, value, source_id=None: actions.append((name, value)))
    position = QPoint(window.visualizer.width() // 2, 100)
    QTest.mouseClick(window.visualizer, Qt.MouseButton.LeftButton, pos=position)
    assert window.side_panel is None and not actions
    for _ in qt_frontend.QT_MODES:
        QTest.mouseDClick(window.visualizer, Qt.MouseButton.LeftButton, pos=position)
    assert actions == [("visualizer-mode", mode) for mode in (1, 2, 4, 5, 0)]
    assert window.state["visualizerMode"] == 0
    assert not window.isFullScreen()


def test_middle_double_click_follows_a_panel_mode_selection(window, monkeypatch):
    actions = []
    monkeypatch.setattr(window.backend, "update_setting",
                        lambda name, value, source_id=None: actions.append((name, value)))
    window.open_mode_menu()
    panel = window.side_panel
    panel.mode.setCurrentIndex(panel.mode.findData(2))
    panel.close_button.click()
    wait_for_panel_animation(window)
    QTest.mouseDClick(window.visualizer, Qt.MouseButton.LeftButton,
                      pos=QPoint(window.visualizer.width() // 2, 100))
    assert actions == [("visualizer-mode", 2), ("visualizer-mode", 4)]


def test_side_double_click_does_not_toggle_fullscreen(window):
    position = QPoint(10, 100)
    QTest.mouseClick(window.visualizer, Qt.MouseButton.LeftButton, pos=position)
    QTest.mouseDClick(window.visualizer, Qt.MouseButton.LeftButton, pos=position)
    assert window.side_panel.kind == "visual"
    assert not window.isFullScreen()


def test_screen_click_in_empty_controls_container_opens_panel(window):
    container = window.controls_container
    QTest.mouseClick(container, Qt.MouseButton.LeftButton, pos=QPoint(1, 5))
    assert window.side_panel.kind == "visual"


def test_middle_double_click_reports_failed_mode_change(window, monkeypatch):
    def fail(*_args):
        raise OSError("disk full")
    warnings = []
    monkeypatch.setattr(window.backend, "update_setting", fail)
    monkeypatch.setattr(qt_frontend.QMessageBox, "warning",
                        lambda *args: warnings.append(args))
    QTest.mouseDClick(window.visualizer, Qt.MouseButton.LeftButton,
                      pos=QPoint(window.visualizer.width() // 2, 100))
    assert "disk full" in warnings[0][2]
    assert window.state.get("visualizerMode", 0) == 0


def test_right_click_does_not_open_a_panel(window):
    QTest.mouseClick(window.visualizer, Qt.MouseButton.RightButton, pos=QPoint(10, 100))
    assert window.side_panel is None


def test_state_refresh_updates_selected_source_without_sending_action(window, monkeypatch):
    state = {
        "track": {},
        "sources": [{"id": "mpris:org.mpris.MediaPlayer2.first", "label": "First"},
                    {"id": "other", "label": "Other"}],
        "selectedSource": "mpris:org.mpris.MediaPlayer2.first",
    }
    actions = []
    monkeypatch.setattr(window.backend, "get_state", lambda: state)
    monkeypatch.setattr(window.backend, "update_setting",
                        lambda name, value, source_id=None: actions.append((name, value, source_id)))

    qt_frontend.AmbiBeatQtWindow.refresh_state(window)
    state["selectedSource"] = "other"
    qt_frontend.AmbiBeatQtWindow.refresh_state(window)

    assert window.state["selectedSource"] == "other"
    assert window.source_button.toolTip() == "Playback source: Other"
    assert actions == []


def test_source_menu_shows_selected_source_and_switches(window, monkeypatch):
    window.state = {
        "sources": [{"id": "mpris:org.mpris.MediaPlayer2.first", "label": "First"},
                    {"id": "mpris:org.mpris.MediaPlayer2.player", "label": "Player"}],
        "selectedSource": "mpris:org.mpris.MediaPlayer2.first",
    }
    calls = []
    monkeypatch.setattr(window.backend, "set_source", lambda source: (calls.append(source) or True, ""))

    class TestMenu(qt_frontend.QMenu):
        def exec(self, position):
            assert position == window.source_button.mapToGlobal(QPoint(0, -self.sizeHint().height()))
            actions = self.actions()
            assert [(action.text(), action.isChecked()) for action in actions] == [
                ("First", True), ("Player", False),
            ]
            actions[1].trigger()

    monkeypatch.setattr(qt_frontend, "QMenu", TestMenu)
    window.open_source_menu()
    assert calls == ["mpris:org.mpris.MediaPlayer2.player"]
    assert window.controls_effect.opacity() == 1
    assert window.source_button.parentWidget() is window.settings_button.parentWidget()


def test_source_icons_appear_in_menu_and_settings_and_update(window, monkeypatch, tmp_path):
    icons = {}
    for entry, color in (("app-one", "red"), ("app-two", "blue")):
        image = QPixmap(16, 16)
        image.fill(QColor(color))
        icon_path = tmp_path / f"{entry}.png"
        assert image.save(str(icon_path))
        desktop_path = tmp_path / f"{entry}.desktop"
        desktop_path.write_text(f"[Desktop Entry]\nIcon={icon_path}\n", encoding="utf-8")
        icons[entry + ".desktop"] = str(desktop_path)
    monkeypatch.setattr(qt_panels.QStandardPaths, "locate",
                        lambda _location, name: icons.get(name, ""))
    source = {"id": "mpris:org.mpris.MediaPlayer2.test", "label": "Player",
              "identity": "Player", "desktopEntry": "app-one"}
    window.state = {"sources": [source], "selectedSource": source["id"]}

    def color(icon):
        return icon.pixmap(16, 16).toImage().pixelColor(8, 8).name()

    class TestMenu(qt_frontend.QMenu):
        def exec(self, position):
            assert color(self.actions()[0].icon()) == "#ff0000"

    monkeypatch.setattr(qt_frontend, "QMenu", TestMenu)
    window.open_source_menu()
    window.open_settings()
    panel = window.side_panel
    assert color(panel.source.itemIcon(0)) == "#ff0000"
    panel.sync_settings({**window.state, "sources": [{**source, "desktopEntry": "app-two"}]})
    assert color(panel.source.itemIcon(0)) == "#0000ff"
    assert panel.source.currentData() == source["id"]


@pytest.mark.parametrize("selected,status,expected", [
    ("mpris:org.mpris.MediaPlayer2.first", "playing", True),
    ("mpris:org.mpris.MediaPlayer2.first", "paused", False),
    ("mpris:org.mpris.MediaPlayer2.player", "playing", False),
])
def test_qt_startup_prefers_playing_computer_app_once(window, monkeypatch, selected, status, expected):
    state = {
        "track": {},
        "sources": [{"id": "mpris:org.mpris.MediaPlayer2.first", "label": "First", "status": "paused"},
                    {"id": "mpris:org.mpris.MediaPlayer2.player",
                     "label": "Player", "status": status}],
        "selectedSource": selected,
    }
    calls = []

    monkeypatch.setattr(window.backend, "get_state", lambda: state)
    monkeypatch.setattr(window.backend, "set_source", lambda source: (calls.append(source) or True, ""))
    window.prefer_playing_source_on_start = True
    window.refresh_state()
    window.refresh_state()
    assert calls == (["mpris:org.mpris.MediaPlayer2.player"] if expected else [])
    assert window.source_prompt.isHidden()


@pytest.mark.parametrize("selected,expected", [
    ("mpris:org.mpris.MediaPlayer2.example.instance2", []),
    ("mpris:org.mpris.MediaPlayer2.other", ["mpris:org.mpris.MediaPlayer2.example.instance2"]),
])
def test_startup_prefers_last_selected_instance_of_playing_app(window, monkeypatch, selected, expected):
    sources = [
        {"id": "mpris:org.mpris.MediaPlayer2.example.instance1",
         "label": "Example 1", "identity": "Example Web Browser", "application": "example",
         "status": "playing"},
        {"id": "mpris:org.mpris.MediaPlayer2.example.instance2",
         "label": "Example 2", "identity": "Example", "application": "example",
         "status": "paused", "lastSelected": True},
        {"id": "mpris:org.mpris.MediaPlayer2.other",
         "label": "Other", "identity": "Other", "application": "other", "status": "paused"},
    ]
    state = {"track": {}, "sources": sources, "selectedSource": selected}
    calls = []
    monkeypatch.setattr(window.backend, "get_state", lambda: state)
    monkeypatch.setattr(window.backend, "set_source", lambda source: (calls.append(source) or True, ""))
    window.prefer_playing_source_on_start = True
    window.refresh_state()
    window.refresh_state()
    assert calls == expected


def test_source_selection_reports_failure(window, monkeypatch):
    warnings = []
    def fail(path):
        raise OSError("unavailable")

    monkeypatch.setattr(window.backend, "set_source", fail)
    monkeypatch.setattr(qt_frontend.QMessageBox, "warning",
                        lambda parent, title, message: warnings.append(message))
    window.change_source("mpris:org.mpris.MediaPlayer2.other")
    assert warnings == ["Source could not be changed: unavailable"]


def test_source_selection_reports_backend_warning(window, monkeypatch):
    warnings = []
    monkeypatch.setattr(window.backend, "set_source", lambda source: (True, "Unable to pause"))
    monkeypatch.setattr(qt_frontend.QMessageBox, "warning",
                        lambda parent, title, message: warnings.append(message))
    window.change_source("mpris:org.mpris.MediaPlayer2.other")
    assert warnings == ["Unable to pause"]


def test_source_prompt_appears_on_new_playback_and_switches(window):
    first = "mpris:org.mpris.MediaPlayer2.first"
    other = "mpris:org.mpris.MediaPlayer2.other"
    state = {"selectedSource": first, "sourcePromptSeconds": 17, "track": {}, "sources": [
        {"id": first, "label": "First", "status": "playing"},
        {"id": other, "label": "Other", "status": "paused"},
    ]}
    window.backend.get_state.return_value = state
    window.refresh_state()
    assert window.source_prompt.isHidden()
    state["sources"][1]["status"] = "playing"
    window.refresh_state()
    assert not window.source_prompt.isHidden()
    assert window.source_prompt_label.text() == "Other started playing. Switch source?"
    assert window.source_prompt_timer.interval() == 17000
    assert window.source_prompt.x() == (window.centralWidget().width() - window.source_prompt.width()) // 2
    assert window.source_prompt.y() == 12
    window.resize(800, 600)
    QApplication.processEvents()
    assert window.source_prompt.x() == (window.centralWidget().width() - window.source_prompt.width()) // 2
    window.open_settings()
    QTest.mouseClick(window.source_prompt_switch, Qt.MouseButton.LeftButton)
    window.backend.set_source.assert_called_once_with(other)
    assert window.source_prompt.isHidden()
    assert window.side_panel is not None
    window.refresh_state()
    assert window.source_prompt.isHidden()


def test_source_prompt_timeout_and_dismiss_only_reappear_on_new_playback(window):
    first = "mpris:org.mpris.MediaPlayer2.first"
    other = "mpris:org.mpris.MediaPlayer2.other"
    state = {"selectedSource": first, "track": {}, "sources": [
        {"id": first, "label": "First", "status": "playing"},
        {"id": other, "label": "Other", "status": "paused"},
    ]}
    window.backend.get_state.return_value = state
    window.refresh_state()
    state["sources"][1]["status"] = "playing"
    window.refresh_state()
    window.source_prompt_timer.start(20)
    QTest.qWait(60)
    assert window.source_prompt.isHidden()
    window.refresh_state()
    assert window.source_prompt.isHidden()
    state["sources"][1]["status"] = "paused"
    window.refresh_state()
    state["sources"][1]["status"] = "playing"
    window.refresh_state()
    assert not window.source_prompt.isHidden()
    QTest.mouseClick(window.source_prompt_dismiss, Qt.MouseButton.LeftButton)
    assert window.source_prompt.isHidden()
    window.backend.set_source.assert_not_called()


def test_source_prompts_queue_and_discard_stale_sources(window):
    first = "mpris:org.mpris.MediaPlayer2.first"
    second = "mpris:org.mpris.MediaPlayer2.second"
    third = "mpris:org.mpris.MediaPlayer2.third"
    state = {"selectedSource": first, "track": {}, "sources": [
        {"id": first, "label": "First", "status": "playing"},
        {"id": second, "label": "Second", "status": "paused"},
        {"id": third, "label": "Third", "status": "paused"},
    ]}
    window.backend.get_state.return_value = state
    window.refresh_state()
    state["sources"][1]["status"] = "playing"
    state["sources"][2]["status"] = "playing"
    window.refresh_state()
    assert window._source_prompt_id == second
    state["sources"][1]["status"] = "paused"
    window.refresh_state()
    assert window._source_prompt_id == third
    state["sources"] = state["sources"][:2]
    window.refresh_state()
    assert window.source_prompt.isHidden()
    window.backend.set_source.assert_not_called()


def test_source_prompts_show_queued_playing_sources_in_order(window):
    first = "mpris:org.mpris.MediaPlayer2.first"
    state = {"selectedSource": first, "track": {}, "sources": [
        {"id": first, "label": "First", "status": "playing"},
        {"id": "second", "label": "Second", "status": "paused"},
        {"id": "third", "label": "Third", "status": "paused"},
    ]}
    window.backend.get_state.return_value = state
    window.refresh_state()
    state["sources"][1]["status"] = state["sources"][2]["status"] = "playing"
    window.refresh_state()
    assert window._source_prompt_id == "second"
    QTest.mouseClick(window.source_prompt_dismiss, Qt.MouseButton.LeftButton)
    assert window._source_prompt_id == "third"
    assert not window.source_prompt.isHidden()


def test_source_prompt_tracks_new_and_manually_selected_sources(window):
    first = "mpris:org.mpris.MediaPlayer2.first"
    other = "mpris:org.mpris.MediaPlayer2.other"
    state = {"selectedSource": first, "track": {}, "sources": [
        {"id": first, "label": "First", "status": "playing"},
    ]}
    window.backend.get_state.return_value = state
    window.refresh_state()
    state["sources"].append({"id": other, "label": "Other", "status": "playing"})
    window.refresh_state()
    assert window._source_prompt_id == other
    state["selectedSource"] = other
    window.refresh_state()
    assert window.source_prompt.isHidden()
    assert not window.source_prompt_timer.isActive()


def test_source_prompt_does_not_trigger_background_gestures(window, monkeypatch):
    first = "mpris:org.mpris.MediaPlayer2.first"
    other = "mpris:org.mpris.MediaPlayer2.other"
    state = {"selectedSource": first, "track": {}, "sources": [
        {"id": first, "label": "First", "status": "playing"},
        {"id": other, "label": "Other", "status": "paused"},
    ]}
    window.backend.get_state.return_value = state
    window.refresh_state()
    state["sources"][1]["status"] = "playing"
    window.refresh_state()
    actions = []
    monkeypatch.setattr(window, "open_mode_menu", lambda: actions.append("effects"))
    monkeypatch.setattr(window, "open_settings", lambda: actions.append("settings"))
    monkeypatch.setattr(window, "next_visualizer_mode", lambda: actions.append("next"))
    QTest.mouseClick(window.source_prompt_label, Qt.MouseButton.LeftButton, pos=QPoint(2, 2))
    QTest.mouseDClick(window.source_prompt, Qt.MouseButton.LeftButton, pos=window.source_prompt.rect().center())
    assert actions == []
    assert not window.source_prompt.isHidden()


def test_source_prompt_reports_failed_switch_and_remains_available(window, monkeypatch):
    first = "mpris:org.mpris.MediaPlayer2.first"
    other = "mpris:org.mpris.MediaPlayer2.other"
    state = {"selectedSource": first, "track": {}, "sources": [
        {"id": first, "label": "First", "status": "playing"},
        {"id": other, "label": "Other", "status": "paused"},
    ]}
    window.backend.get_state.return_value = state
    window.refresh_state()
    state["sources"][1]["status"] = "playing"
    window.refresh_state()
    window.backend.set_source.side_effect = OSError("unavailable")
    warnings = []
    monkeypatch.setattr(qt_frontend.QMessageBox, "warning",
                        lambda _parent, _title, message: warnings.append(message))
    QTest.mouseClick(window.source_prompt_switch, Qt.MouseButton.LeftButton)
    assert warnings == ["Source could not be changed: unavailable"]
    assert not window.source_prompt.isHidden()
    assert window.source_prompt_timer.isActive()


def test_source_prompt_duration_settings_apply_and_reset(window):
    window.open_settings()
    panel = window.side_panel
    assert panel.source_prompt_seconds.value() == 10
    panel.source_prompt_seconds.setValue(20)
    window.backend.update_setting.assert_called_once_with("source-prompt-seconds", 20, None)
    panel.reset_button.click()
    assert panel.source_prompt_seconds.value() == 10
    assert window.backend.update_setting.call_args.args == ("source-prompt-seconds", 10, None)


def test_source_prompt_duration_setting_reports_save_failure(window):
    window.open_settings()
    panel = window.side_panel
    panel.source_prompt_seconds.setValue(20)
    window.backend.update_setting.side_effect = OSError("disk full")
    panel.source_prompt_seconds.setValue(25)
    assert "disk full" in panel.status.text()
    assert panel.source_prompt_seconds.value() == 20


def test_source_prompt_toggle_disables_duration_and_dismisses_prompt(window, monkeypatch):
    first = "mpris:org.mpris.MediaPlayer2.first"
    other = "mpris:org.mpris.MediaPlayer2.other"
    state = {"selectedSource": first, "track": {}, "sourcePromptEnabled": True, "sources": [
        {"id": first, "label": "First", "status": "playing"},
        {"id": other, "label": "Other", "status": "paused"},
    ]}
    window.backend.get_state.return_value = state
    def update(name, value, source_id=None):
        if name == "source-prompt-enabled":
            state["sourcePromptEnabled"] = value
        return ""
    monkeypatch.setattr(window.backend, "update_setting", update)
    window.refresh_state()
    state["sources"][1]["status"] = "playing"
    window.refresh_state()
    assert not window.source_prompt.isHidden()
    window.open_settings()
    panel = window.side_panel
    assert panel.source_prompt_seconds.isEnabled()
    panel.source_prompt_enabled.setChecked(False)
    assert not panel.source_prompt_seconds.isEnabled()
    assert not panel.source_prompt_seconds_value.isEnabled()
    assert not panel.slider_labels[panel.source_prompt_seconds].isEnabled()
    QApplication.processEvents()
    assert panel.source_prompt_seconds_value.palette().color(
        QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText).name() == "#78839a"
    assert window.source_prompt.isHidden()
    window.refresh_state()
    panel.source_prompt_enabled.setChecked(True)
    assert panel.source_prompt_seconds.isEnabled()
    window.refresh_state()
    assert window.source_prompt.isHidden()
    state["sources"][1]["status"] = "paused"
    window.refresh_state()
    state["sources"][1]["status"] = "playing"
    window.refresh_state()
    assert not window.source_prompt.isHidden()


def test_source_prompt_toggle_failure_restores_duration_enabled_state(window, monkeypatch):
    def fail(*args):
        raise OSError("disk full")
    monkeypatch.setattr(window.backend, "update_setting", fail)
    window.open_settings()
    panel = window.side_panel
    panel.source_prompt_enabled.setChecked(False)
    assert panel.source_prompt_enabled.isChecked()
    assert panel.source_prompt_seconds.isEnabled()
    assert "disk full" in panel.status.text()


def test_settings_reset_defaults_keeps_source_and_system_volume(window, monkeypatch):
    first = "mpris:org.mpris.MediaPlayer2.first"
    second = "mpris:org.mpris.MediaPlayer2.second"
    window.state = {
        "track": {}, "selectedSource": first, "systemVolume": 65, "sampleRate": 96000,
        "sourcePromptSeconds": 25, "sourcePromptEnabled": False, "showPhotosensitivityWarning": False,
        "sourceGains": {first: 2.0, second: 3.0},
        "sources": [{"id": first, "label": "First"}, {"id": second, "label": "Second"}],
    }
    actions = []
    monkeypatch.setattr(window.backend, "update_setting",
                        lambda name, value, source_id=None: actions.append((name, value, source_id)))
    window.open_settings()
    panel = window.side_panel
    assert not panel.source_prompt_seconds.isEnabled()
    panel.reset_button.click()
    assert panel.source.currentData() == first
    assert panel.system_volume.value() == 65
    assert panel.sample_rate.currentData() == 48000
    assert panel.source_prompt_seconds.value() == 10
    assert panel.source_prompt_seconds.isEnabled()
    assert panel.source_prompt_enabled.isChecked()
    assert panel.show_photosensitivity_warning.isChecked()
    assert panel.source_gain.value() == 10
    assert set(actions) == {
        ("sample-rate", 48000, None), ("source-prompt-seconds", 10, None),
        ("source-prompt-enabled", True, None), ("show-photosensitivity-warning", True, None),
        ("source-gain", 1.0, first), ("source-gain", 1.0, second),
    }


def test_settings_reset_reports_failed_gain_and_keeps_saved_value(window, monkeypatch):
    first = "mpris:org.mpris.MediaPlayer2.first"
    second = "mpris:org.mpris.MediaPlayer2.second"
    window.state = {
        "track": {}, "selectedSource": first, "sourceGains": {first: 1.5, second: 2.0},
        "sources": [{"id": first, "label": "First"}, {"id": second, "label": "Second"}],
    }
    def update(name, value, source_id=None):
        if name == "source-gain" and source_id == first:
            raise OSError("disk full")
        return ""
    monkeypatch.setattr(window.backend, "update_setting", update)
    window.open_settings()
    panel = window.side_panel
    panel.reset_button.click()
    assert panel.source_gain.value() == 15
    assert panel.source_gains == {first: 1.5, second: 1.0}
    assert "disk full" in panel.status.text()


def test_settings_sync_external_values_without_overwriting_drag(window):
    first = "mpris:org.mpris.MediaPlayer2.first"
    second = "mpris:org.mpris.MediaPlayer2.second"
    state = {"selectedSource": first, "sourceGains": {first: 1.5, second: 2.0},
             "systemVolume": 50, "sources": [{"id": first, "label": "First"}]}
    window.state = state
    window.open_settings()
    panel = window.side_panel
    panel.source_gain.setSliderDown(True)
    panel.sync_settings({**state, "sourceGains": {first: 1.1}})
    assert panel.source_gain.value() == 15
    panel.source_gain.setSliderDown(False)
    panel.sync_settings({**state, "selectedSource": second, "systemVolume": 70,
                         "sourcePromptEnabled": False,
                         "sources": state["sources"] + [{"id": second, "label": "Second"}]})
    assert panel.source.currentData() == second
    assert panel.source_gain.value() == 20
    assert panel.system_volume.value() == 70
    assert not panel.source_prompt_seconds.isEnabled()
    panel.source_gain.setSliderDown(True)
    panel.sync_settings({**state, "selectedSource": first, "sourceGains": {first: 1.5},
                         "sources": [{"id": first, "label": "First"}]})
    assert not panel.source_gain.isSliderDown()
    assert panel.source_gain.value() == 15


def test_edge_glow_obeys_opacity_and_no_visualisation_mode(window):
    window.visualizer.fallback_pixmap = None
    window.visualizer.set_state({"visualizerMode": 1})
    window.state = {"visualizerMode": 1, "edgeFade": 50}
    window.edge_border.set_palette(["#ff0000", "#ff0000"])
    window.edge_border.opacity = 0
    no_glow = window.visualizer.grab().toImage()
    window.edge_border.opacity = 1
    with_glow = window.visualizer.grab().toImage()
    y = window.visualizer.height() // 2
    assert with_glow.pixelColor(0, y).red() > no_glow.pixelColor(0, y).red()
    assert with_glow.pixelColor(window.visualizer.width() // 2, y) == no_glow.pixelColor(
        window.visualizer.width() // 2, y
    )

    window.state["visualizerMode"] = 5
    hidden_glow = window.visualizer.grab().toImage()
    assert hidden_glow.pixelColor(0, y) == no_glow.pixelColor(0, y)


def test_edge_swirl_pauses_when_playback_stops(window, monkeypatch):
    state = {"track": {"status": "playing"}, "edgeSwirl": True}
    now = qt_frontend.time.monotonic()
    monkeypatch.setattr(window.backend, "get_state", lambda: state)
    monkeypatch.setattr(qt_frontend.time, "monotonic", lambda: now + .1)
    window.refresh_state()
    angle = window.edge_border.angle
    assert angle > 0

    state["track"]["status"] = "paused"
    monkeypatch.setattr(qt_frontend.time, "monotonic", lambda: now + .2)
    window.refresh_state()
    assert window.edge_border.angle == angle


@pytest.mark.parametrize(
    "direction, expected_sign",
    [("clockwise", -1), ("counter-clockwise", 1)],
)
def test_edge_swirl_uses_resolved_direction(window, monkeypatch, direction, expected_sign):
    state = {
        "track": {"status": "playing"},
        "edgeSwirl": True,
        "edgeSwirlDirection": "random",
        "edgeSwirlResolvedDirection": direction,
    }
    now = qt_frontend.time.monotonic()
    window.edge_border.angle = 180
    window.edge_last_update = now
    monkeypatch.setattr(window.backend, "get_state", lambda: state)
    monkeypatch.setattr(qt_frontend.time, "monotonic", lambda: now + .1)
    window.refresh_state()
    assert (window.edge_border.angle - 180) * expected_sign > 0


def test_settings_icon_has_cog_teeth_and_hollow_center(window):
    image = window.line_icon("settings").pixmap(32, 32).toImage()
    assert image.pixelColor(16, 16).alpha() == 0
    assert image.pixelColor(16, 4).alpha() > 0
    assert image.pixelColor(16, 8).alpha() > 0
    assert image.pixelColor(4, 16).alpha() > 0


def test_visual_options_apply_immediately_and_reset(window, monkeypatch):
    actions = []
    monkeypatch.setattr(window.backend, "update_setting", lambda name, value, source_id=None: actions.append((name, value, source_id)))
    window.state = {"visualizerMode": 3}
    window.open_mode_menu()
    panel = window.side_panel
    assert panel.mode.currentData() is None
    panel.mode.setCurrentIndex(panel.mode.findData(2))
    panel.artwork_mode.setCurrentIndex(panel.artwork_mode.findData("logo"))
    panel.lighting_mode.setCurrentIndex(panel.lighting_mode.findData("disco"))
    panel.edge_swirl.setChecked(True)
    panel.edge_depth.setValue(150)
    assert actions == [("visualizer-mode", 2, None), ("artwork-mode", "logo", None),
                       ("lighting-mode", "disco", None), ("edge-swirl", True, None),
                       ("edge-depth", 150, None)]
    panel.reset_button.click()
    assert actions[-5:] == [("visualizer-mode", 0, None), ("artwork-mode", "album", None),
                            ("lighting-mode", "border", None), ("edge-swirl", False, None),
                            ("edge-depth", 100, None)]


@pytest.mark.parametrize("key,value,path", [
    ("visualizerMode", 2, "visualizer-mode/2"),
    ("artworkMode", "logo", "artwork-mode/logo"),
    ("albumArtOpacity", 60, "album-art-opacity/60"),
    ("lightingMode", "disco", "lighting-mode/disco"),
    ("lightingColorMode", "rainbow", "lighting-color-mode/rainbow"),
    ("lightingColorMode", "christmas", "lighting-color-mode/christmas"),
    ("edgeSwirl", True, "edge-swirl/true"),
    ("edgeColorIntensity", 40, "edge-color-intensity/40"),
    ("edgeFade", 20, "edge-fade/20"),
    ("edgeOpacity", 80, "edge-opacity/80"),
    ("edgeColorCount", 3, "edge-color-count/3"),
    ("edgeDepth", 150, "edge-depth/150"),
    ("discoLightCount", 20, "disco-light-count/20"),
    ("discoLightSize", 150, "disco-light-size/150"),
    ("discoLightStyle", "star", "disco-light-style/star"),
    ("discoWireColor", "theme", "disco-wire-color/theme"),
    ("discoWireColor", "none", "disco-wire-color/none"),
    ("edgeSwirlSlowdown", 5, "edge-swirl-slowdown/5"),
    ("edgeSwirlDirection", "random", "edge-swirl-direction/random"),
])
def test_each_visual_control_applies_and_resets(window, key, value, path):
    actions = []
    panel = SidePanel({}, "visual", qt_frontend.QT_MODES,
                      lambda name, value: actions.append((name, value)), window)
    widget, default, _ = panel.fields[key]
    panel.set_field_value(widget, value)
    assert actions == [(path.split("/", 1)[0], value)]
    panel.reset_button.click()
    assert actions[-1] == (panel.fields[key][2], default)
    assert panel.field_value(widget) == default


def test_visual_option_failure_reverts_and_reports(window, monkeypatch):
    def fail(*args):
        raise OSError("connection failed")
    monkeypatch.setattr(window.backend, "update_setting", fail)
    window.open_mode_menu()
    panel = window.side_panel
    panel.edge_depth.setValue(150)
    assert panel.edge_depth.value() == 100
    assert panel.edge_depth_value.text() == "100%"
    assert "connection failed" in panel.status.text()


def test_visual_panel_sync_does_not_send_requests(window, monkeypatch):
    actions = []
    monkeypatch.setattr(window.backend, "update_setting", lambda name, value, source_id=None: actions.append((name, value, source_id)))
    window.open_mode_menu()
    panel = window.side_panel
    panel.sync_fields({"edgeDepth": 150, "lightingMode": "none"})
    assert panel.edge_depth.value() == 150
    assert panel.edge_depth_value.text() == "150%"
    assert not panel.edge_swirl.isEnabled()
    assert actions == []


def test_reset_restores_all_visual_settings(window):
    state = {
        "visualizerMode": 2, "scopeZeroLine": False, "scopeLineWidth": 200,
        "artworkMode": "logo", "albumArtOpacity": 60,
        "lightingMode": "disco", "lightingColorMode": "rainbow", "edgeSwirl": True,
        "edgeColorIntensity": 40, "edgeFade": 20, "edgeOpacity": 80,
        "edgeColorCount": 3, "edgeDepth": 150, "discoLightCount": 20,
        "discoLightSize": 150, "edgeSwirlSlowdown": 5, "edgeSwirlDirection": "random",
        "discoLightStyle": "fairy", "discoWireColor": "theme",
    }
    actions = []
    panel = SidePanel(state, "visual", qt_frontend.QT_MODES,
                      lambda name, value: actions.append((name, value)), window)
    panel.reset_button.click()
    assert len(actions) == len(state)
    for widget, default, path in panel.fields.values():
        assert panel.field_value(widget) == default
        assert (path, default) in actions


def test_settings_apply_immediately_and_restore_failed_change(window, monkeypatch):
    actions = []
    monkeypatch.setattr(window.backend, "update_setting", lambda name, value, source_id=None: actions.append((name, value, source_id)))
    window.open_settings()
    panel = window.side_panel
    panel.sample_rate.setCurrentIndex(panel.sample_rate.findData(44100))
    panel.source_gain.setValue(25)
    assert actions == [("sample-rate", 44100, None),
                       ("source-gain", 2.5, "mpris:org.mpris.MediaPlayer2.player")]
    panel.close_button.click()
    wait_for_panel_animation(window)
    assert window.side_panel is None

    def fail(*args):
        raise OSError("connection failed")
    monkeypatch.setattr(window.backend, "update_setting", fail)
    window.open_settings()
    panel = window.side_panel
    panel.sample_rate.setCurrentIndex(panel.sample_rate.findData(44100))
    assert window.side_panel is panel
    assert panel.sample_rate.currentData() == 48000
    assert "connection failed" in panel.status.text()


@pytest.mark.parametrize("initial", [True, False])
def test_startup_warning_setting_applies_and_is_last(window, monkeypatch, initial):
    window.state["showPhotosensitivityWarning"] = initial
    actions = []
    monkeypatch.setattr(window.backend, "update_setting",
                        lambda name, value, source_id=None: actions.append((name, value)))
    window.open_settings()
    panel = window.side_panel
    assert panel.content_layout.itemAt(panel.content_layout.count() - 2).widget() is panel.show_photosensitivity_warning
    assert panel.show_photosensitivity_warning.isChecked() is initial
    panel.show_photosensitivity_warning.setChecked(not initial)
    assert actions == [("show-photosensitivity-warning", not initial)]
    assert window.state["showPhotosensitivityWarning"] is not initial
    panel.reset_button.click()
    assert panel.show_photosensitivity_warning.isChecked()


def test_startup_warning_setting_reports_save_failure(window, monkeypatch):
    def fail(*args):
        raise OSError("disk full")
    monkeypatch.setattr(window.backend, "update_setting", fail)
    window.open_settings()
    panel = window.side_panel
    panel.show_photosensitivity_warning.setChecked(False)
    assert window.side_panel is panel
    assert panel.show_photosensitivity_warning.isChecked()
    assert "disk full" in panel.status.text()


@pytest.mark.parametrize("kind,name", [
    ("settings", "show_photosensitivity_warning"),
    ("settings", "source_prompt_enabled"),
    ("visual", "edge_swirl"),
])
def test_panel_checkboxes_show_outline_and_checkmark(window, kind, name):
    window.open_side_panel(kind)
    checkbox = getattr(window.side_panel, name)
    window.side_panel.scroll.ensureWidgetVisible(checkbox)
    option = QStyleOptionButton()
    checkbox.initStyleOption(option)
    rect = checkbox.style().subElementRect(QStyle.SubElement.SE_CheckBoxIndicator, option, checkbox)
    checkbox.setChecked(False)
    unchecked = checkbox.grab().toImage()
    checkbox.setChecked(True)
    checked = checkbox.grab().toImage()
    ratio = checked.devicePixelRatio()
    def pixel(image, x, y):
        return image.pixelColor(round(x * ratio), round(y * ratio))
    assert pixel(unchecked, rect.left(), rect.center().y()).name() == "#718cc1"
    assert pixel(checked, rect.left(), rect.center().y()).name() == "#718cc1"
    assert any(pixel(checked, x, y).lightness() > 200 and pixel(unchecked, x, y).lightness() < 100
               for x in range(rect.left() + 2, rect.right() - 1)
               for y in range(rect.top() + 2, rect.bottom() - 1))


def test_settings_apply_source_system_volume_and_per_source_boost(window, monkeypatch):
    first = "mpris:org.mpris.MediaPlayer2.first"
    second = "mpris:org.mpris.MediaPlayer2.second"
    window.state = {
        "selectedSource": first, "sourceGains": {first: 1.5, second: 2.0},
        "sources": [{"id": first, "label": "First"}, {"id": second, "label": "Second"}],
        "systemVolume": 50,
    }
    actions = []
    monkeypatch.setattr(window.backend, "update_setting", lambda name, value, source_id=None: actions.append((name, value, source_id)))
    window.open_settings()
    panel = window.side_panel
    assert panel.source_gain.value() == 15
    panel.source_gain.setValue(20)
    panel.source.setCurrentIndex(panel.source.findData(second))
    assert panel.source_gain.value() == 20
    panel.source_gain.setValue(30)
    panel.system_volume.setValue(65)
    assert actions == [
        ("source-gain", 2.0, first), ("source", second, None),
        ("source-gain", 3.0, second),
        ("system-volume", 65, None),
    ]


def test_settings_source_failure_restores_source_and_gain(window, monkeypatch):
    first = "mpris:org.mpris.MediaPlayer2.first"
    second = "mpris:org.mpris.MediaPlayer2.second"
    window.state = {
        "selectedSource": first, "sourceGains": {first: 1.5, second: 2.0},
        "sources": [{"id": first, "label": "First"}, {"id": second, "label": "Second"}],
    }
    def fail(*args):
        raise OSError("unavailable")
    monkeypatch.setattr(window.backend, "update_setting", fail)
    window.open_settings()
    panel = window.side_panel
    panel.source.setCurrentIndex(panel.source.findData(second))
    assert panel.source.currentData() == first
    assert panel.source_gain.value() == 15
    assert "unavailable" in panel.status.text()


def test_closing_qt_stops_backend(window):
    window.close()
    window.backend.stop.assert_called_once()


def test_closing_settings_keeps_applied_source_and_system_volume(window, monkeypatch):
    first = "mpris:org.mpris.MediaPlayer2.first"
    second = "mpris:org.mpris.MediaPlayer2.second"
    window.state = {
        "selectedSource": first, "systemVolume": 50,
        "sources": [{"id": first, "label": "First"}, {"id": second, "label": "Second"}],
    }
    actions = []
    monkeypatch.setattr(window.backend, "update_setting", lambda name, value, source_id=None: actions.append((name, value, source_id)))
    window.open_settings()
    panel = window.side_panel
    panel.source.setCurrentIndex(panel.source.findData(second))
    panel.system_volume.setValue(75)
    panel.close_button.click()
    wait_for_panel_animation(window)
    assert actions == [("source", second, None), ("system-volume", 75, None)]
    assert window.side_panel is None


@pytest.mark.parametrize("kind", ["visual", "settings"])
def test_side_panel_slides_from_edge_and_keeps_controls_visible(window, kind):
    window.resize(640, 420)
    window.open_side_panel(kind)
    panel = window.side_panel
    wait_for_panel_animation(window)
    assert panel.x() == (0 if kind == "visual" else window.centralWidget().width() - panel.width())
    assert panel.height() == window.centralWidget().height()
    assert panel.isVisible()
    window.hide_controls()
    assert window.controls_effect.opacity() == 1
    panel.close_button.click()
    wait_for_panel_animation(window)
    assert window.side_panel is None
    assert window.controls_timer.isActive()


def test_switching_panels_keeps_immediately_applied_settings(window, monkeypatch):
    actions = []
    monkeypatch.setattr(window.backend, "update_setting", lambda name, value, source_id=None: actions.append((name, value, source_id)))
    window.open_settings()
    panel = window.side_panel
    panel.source_gain.setValue(25)
    window.open_mode_menu()
    assert window.side_panel.kind == "visual"
    assert actions == [("source-gain", 2.5, "mpris:org.mpris.MediaPlayer2.player")]


def test_clicking_outside_closes_visual_panel(window):
    window.open_mode_menu()
    wait_for_panel_animation(window)
    panel = window.side_panel
    QTest.mouseClick(panel, Qt.MouseButton.LeftButton, pos=QPoint(10, 10))
    assert window.side_panel is panel and not window.panel_closing
    QTest.mouseClick(window.visualizer, Qt.MouseButton.LeftButton,
                     pos=QPoint(window.visualizer.width() - 10, 100))
    wait_for_panel_animation(window)
    assert window.side_panel is None


def test_clicking_settings_outside_visual_panel_opens_settings(window):
    window.open_mode_menu()
    wait_for_panel_animation(window)
    QTest.mouseClick(window.settings_button, Qt.MouseButton.LeftButton)
    assert window.side_panel.kind == "settings"


def test_clicking_outside_settings_keeps_immediately_applied_changes(window, monkeypatch):
    actions = []
    prompts = []
    monkeypatch.setattr(window.backend, "update_setting", lambda name, value, source_id=None: actions.append((name, value, source_id)))
    monkeypatch.setattr(qt_frontend.QMessageBox, "question", lambda *args: prompts.append(args))
    window.open_settings()
    wait_for_panel_animation(window)
    panel = window.side_panel
    panel.source_gain.setValue(25)
    QTest.mouseClick(window.visualizer, Qt.MouseButton.LeftButton, pos=QPoint(10, 100))
    wait_for_panel_animation(window)
    assert window.side_panel is None
    assert not prompts
    assert actions == [("source-gain", 2.5, "mpris:org.mpris.MediaPlayer2.player")]


def test_clicking_outside_unchanged_settings_closes_without_prompt(window, monkeypatch):
    prompts = []
    monkeypatch.setattr(qt_frontend.QMessageBox, "question", lambda *args: prompts.append(args))
    window.open_settings()
    wait_for_panel_animation(window)
    QTest.mouseClick(window.visualizer, Qt.MouseButton.LeftButton, pos=QPoint(10, 100))
    wait_for_panel_animation(window)
    assert window.side_panel is None
    assert not prompts


def test_clicking_visual_button_keeps_applied_settings(window, monkeypatch):
    actions = []
    monkeypatch.setattr(window.backend, "update_setting", lambda name, value, source_id=None: actions.append((name, value, source_id)))
    window.open_settings()
    wait_for_panel_animation(window)
    window.side_panel.source_gain.setValue(25)
    QTest.mouseClick(window.mode_button, Qt.MouseButton.LeftButton)
    assert window.side_panel.kind == "visual"
    assert actions == [("source-gain", 2.5, "mpris:org.mpris.MediaPlayer2.player")]


@pytest.mark.parametrize("close_method", ["close", "escape", "toggle"])
def test_settings_close_paths_do_not_prompt(window, monkeypatch, close_method):
    prompts = []
    monkeypatch.setattr(qt_frontend.QMessageBox, "question", lambda *args: prompts.append(args))
    window.open_settings()
    wait_for_panel_animation(window)
    panel = window.side_panel
    panel.sample_rate.setCurrentIndex(panel.sample_rate.findData(44100))
    if close_method == "close":
        panel.close_button.click()
    elif close_method == "escape":
        QTest.keyClick(panel.sample_rate, Qt.Key.Key_Escape)
    else:
        QTest.mouseClick(window.settings_button, Qt.MouseButton.LeftButton)
    wait_for_panel_animation(window)
    assert not prompts
    assert window.side_panel is None


def test_escape_closes_panel_without_exiting_fullscreen(window):
    window.showFullScreen()
    window.open_mode_menu()
    panel = window.side_panel
    panel.mode.setFocus()
    QTest.keyClick(panel.mode, Qt.Key.Key_Escape)
    wait_for_panel_animation(window)
    assert window.side_panel is None
    assert window.isFullScreen()


def test_f11_works_with_focused_panel_control(window):
    window.open_mode_menu()
    panel = window.side_panel
    panel.mode.setFocus()
    QTest.keyClick(panel.mode, Qt.Key.Key_F11)
    assert window.isFullScreen()


@pytest.mark.parametrize("kind", ["visual", "settings"])
def test_side_panel_uses_blue_to_black_gradient(window, kind):
    window.open_side_panel(kind)
    wait_for_panel_animation(window)
    panel = window.side_panel
    panel.scroll.hide()
    image = panel.grab().toImage()
    center = image.pixelColor(panel.width() // 2, panel.height() // 2)
    edge = image.pixelColor(10, panel.height() // 2)
    assert center.blue() > center.red()
    assert center.blue() > edge.blue()


@pytest.mark.parametrize("kind", ["visual", "settings"])
def test_panel_sliders_ignore_wheel(window, kind):
    window.open_side_panel(kind)
    panel = window.side_panel
    slider = panel.edge_depth if kind == "visual" else panel.source_gain
    before = slider.value()
    point = QPointF(slider.rect().center())
    event = QWheelEvent(point, point, QPoint(0, 0), QPoint(0, 120), Qt.MouseButton.NoButton,
                        Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.ScrollUpdate, False)
    QApplication.sendEvent(slider, event)
    assert slider.value() == before


@pytest.mark.parametrize("kind,name,target", [
    ("visual", "edge_depth", 97),
    ("visual", "edge_depth", 146),
    ("settings", "source_prompt_seconds", 26),
])
def test_panel_slider_snaps_drag_but_shift_and_keys_allow_fine_values(window, monkeypatch, kind, name, target):
    actions = []
    monkeypatch.setattr(window.backend, "update_setting",
                        lambda name, value, source_id=None: actions.append((name, value)))
    window.open_side_panel(kind)
    panel = window.side_panel
    slider = getattr(panel, name)
    panel.scroll.ensureWidgetVisible(slider)
    QApplication.processEvents()

    def drag(value, modifier=Qt.KeyboardModifier.NoModifier):
        option = QStyleOptionSlider()
        slider.initStyleOption(option)
        groove = slider.style().subControlRect(QStyle.ComplexControl.CC_Slider, option,
                                               QStyle.SubControl.SC_SliderGroove, slider)
        handle = slider.style().subControlRect(QStyle.ComplexControl.CC_Slider, option,
                                               QStyle.SubControl.SC_SliderHandle, slider)
        span = groove.width() - handle.width()
        x = groove.x() + handle.width() // 2 + QStyle.sliderPositionFromValue(
            slider.minimum(), slider.maximum(), value, span, option.upsideDown)
        QTest.mousePress(slider, Qt.MouseButton.LeftButton, modifier, handle.center())
        point = QPoint(x, handle.center().y())
        event = QMouseEvent(QEvent.Type.MouseMove, QPointF(point), QPointF(slider.mapToGlobal(point)),
                            Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton, modifier)
        QApplication.sendEvent(slider, event)
        QTest.mouseRelease(slider, Qt.MouseButton.LeftButton, modifier, point)

    drag(target)
    assert slider.value() == round(target / 10) * 10
    if kind == "visual" and target == 146:
        assert actions == [("edge-depth", 150)]
    drag(target, Qt.KeyboardModifier.ShiftModifier)
    assert slider.value() == pytest.approx(target, abs=1)
    value = slider.value()
    QTest.keyClick(slider, Qt.Key.Key_Right)
    assert slider.value() == value + 1
    assert "Shift" in slider.accessibleDescription()


def test_slider_snapping_preserves_endpoints_and_programmatic_values(window):
    window.open_mode_menu()
    slider = window.side_panel.edge_depth
    slider.setValue(27)
    assert slider.value() == 27
    slider.setSliderDown(True)
    slider.setSliderPosition(26)
    assert slider.value() == 25
    slider.setSliderPosition(199)
    assert slider.value() == 200
    slider.setSliderDown(False)
    count = window.side_panel.edge_count
    count.setSliderDown(True)
    count.setSliderPosition(3)
    assert count.value() == 3
    count.setSliderDown(False)


def test_snapped_slider_restores_committed_value_when_save_fails(window, monkeypatch):
    actions = []
    def fail(name, value, source_id=None):
        actions.append((name, value))
        raise OSError("disk full")
    monkeypatch.setattr(window.backend, "update_setting", fail)
    window.open_mode_menu()
    panel = window.side_panel
    panel.edge_depth.setSliderDown(True)
    panel.edge_depth.setSliderPosition(146)
    panel.edge_depth.setSliderDown(False)
    assert actions == [("edge-depth", 150)]
    assert panel.edge_depth.value() == 100
    assert panel.edge_depth_value.text() == "100%"
    assert "disk full" in panel.status.text()


@pytest.mark.parametrize("kind,names", [
    ("visual", ("mode", "artwork_mode", "lighting_mode", "lighting_color_mode", "edge_direction")),
    ("settings", ("sample_rate", "source")),
])
def test_panel_dropdowns_ignore_wheel_but_allow_keyboard_selection(window, kind, names):
    window.open_side_panel(kind)
    panel = window.side_panel
    for name in names:
        combo = getattr(panel, name)
        combo.setCurrentIndex(0)
        combo.setFocus()
        before = combo.currentIndex()
        point = QPointF(combo.rect().center())
        event = QWheelEvent(point, point, QPoint(0, 0), QPoint(0, -120),
                            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                            Qt.ScrollPhase.ScrollUpdate, False)
        QApplication.sendEvent(combo, event)
        assert combo.currentIndex() == before
        if combo.count() > 1:
            QTest.keyClick(combo, Qt.Key.Key_Down)
            assert combo.currentIndex() == before + 1


def test_volume_popup_slider_ignores_wheel(window, monkeypatch):
    actions = []
    monkeypatch.setattr(window.backend, "update_setting", lambda name, value, source_id=None: actions.append((name, value, source_id)))

    class TestMenu(qt_frontend.QMenu):
        def exec(self, position):
            slider = self.actions()[0].defaultWidget()
            before = slider.value()
            point = QPointF(slider.rect().center())
            event = QWheelEvent(point, point, QPoint(0, 0), QPoint(0, 120),
                                Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                                Qt.ScrollPhase.ScrollUpdate, False)
            QApplication.sendEvent(slider, event)
            assert slider.value() == before

    monkeypatch.setattr(qt_frontend, "QMenu", TestMenu)
    window.open_volume_menu()
    assert actions == []


def test_volume_popup_is_centered_themed_and_unsnapped(window, monkeypatch):
    window.state = {**window.state, "systemVolume": 50, "lightingColorMode": "artwork"}
    colors = [QColor("red"), QColor("lime"), QColor("blue")]
    window.update_timeline_theme({"colors": colors})
    actions = []
    monkeypatch.setattr(window.backend, "update_setting",
                        lambda name, value, source_id=None: actions.append((name, value)))

    class TestMenu(qt_frontend.QMenu):
        def exec(self, position):
            slider = self.actions()[0].defaultWidget()
            expected = QPoint((window.volume_button.width() - self.sizeHint().width()) // 2,
                              -self.sizeHint().height())
            assert position == window.volume_button.mapToGlobal(expected)
            assert all(color.name() in slider.styleSheet() for color in colors)
            assert slider.value() == 50 and slider.isEnabled()
            QTest.keyClick(slider, Qt.Key.Key_Up)
            assert slider.value() == 51
            replacement = [QColor("yellow"), QColor("cyan"), QColor("magenta")]
            window.update_timeline_theme({"colors": replacement})
            assert all(color.name() in slider.styleSheet() for color in replacement)

    monkeypatch.setattr(qt_frontend, "QMenu", TestMenu)
    window.open_volume_menu()
    assert actions == [("system-volume", 51)]


def test_volume_popup_disables_unavailable_volume_and_rolls_back_failure(window, monkeypatch):
    def fail(*args):
        raise OSError("unavailable")
    monkeypatch.setattr(window.backend, "update_setting", fail)
    warnings = []
    monkeypatch.setattr(qt_frontend.QMessageBox, "warning",
                        lambda _parent, _title, message: warnings.append(message))

    class TestMenu(qt_frontend.QMenu):
        def exec(self, position):
            slider = self.actions()[0].defaultWidget()
            if window.state.get("systemVolume") is None:
                assert not slider.isEnabled()
            else:
                slider.setValue(70)
                assert slider.value() == 50

    monkeypatch.setattr(qt_frontend, "QMenu", TestMenu)
    window.state["systemVolume"] = None
    window.open_volume_menu()
    window.state["systemVolume"] = 50
    window.open_volume_menu()
    assert warnings == ["Volume could not be changed: unavailable"]


def test_unavailable_gpu_selects_cpu_renderer(monkeypatch, capsys):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(qt_frontend, "gpu_available", lambda: (False, "test failure"))
    backend = Mock()
    backend.get_state.return_value = {"track": {}}
    window = AmbiBeatQtWindow(backend)
    assert isinstance(window.visualizer, qt_frontend.VisualizerWidget)
    assert "test failure" in capsys.readouterr().err
    window.close()
    app.processEvents()


def test_gpu_surface_renders_scope_and_preserves_controls(window, monkeypatch):
    visualizer = window.visualizer
    if not isinstance(visualizer, qt_frontend.GPUVisualizerWidget):
        pytest.skip("hardware OpenGL widget unavailable")
    assert visualizer.isValid()
    assert visualizer.initialized
    paint_engines = []
    paint_scene = visualizer.paint_scene

    def record_engine(painter):
        paint_engines.append(painter.paintEngine().type())
        paint_scene(painter)

    monkeypatch.setattr(visualizer, "paint_scene", record_engine)
    visualizer.fallback_pixmap = None
    window.edge_border.opacity = 0
    state = {
        "track": {"status": "playing"}, "visualizerMode": 1,
        "wave": [math.sin(index * 2.7) * .7 for index in range(64)],
    }
    visualizer.set_state(state)
    playing = visualizer.grabFramebuffer()
    visualizer.set_state({**state, "track": {"status": "paused"}})
    paused = visualizer.grabFramebuffer()
    assert playing != paused
    assert paint_engines and all(engine == QPaintEngine.Type.OpenGL2 for engine in paint_engines)
    assert visualizer.smoothed_wave == []

    window.hide_controls()
    event = QMouseEvent(
        QEvent.Type.MouseMove, QPointF(20, 20), QPointF(20, 20), Qt.MouseButton.NoButton,
        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(visualizer, event)
    assert window.controls_effect.opacity() == 1
    actions = []
    monkeypatch.setattr(window.backend, "media_control", actions.append)
    QTest.mouseClick(window.next_button, Qt.MouseButton.LeftButton)
    assert actions == ["Next"]


def test_gpu_surface_renders_halo_with_opengl_painter(window, monkeypatch):
    visualizer = window.visualizer
    if not isinstance(visualizer, qt_frontend.GPUVisualizerWidget):
        pytest.skip("hardware OpenGL widget unavailable")
    engines = []
    stroke_engines = []
    paint_scene = visualizer.paint_scene
    paint_scope_path = qt_frontend.paint_scope_path

    def record_engine(painter):
        engines.append(painter.paintEngine().type())
        paint_scene(painter)

    monkeypatch.setattr(visualizer, "paint_scene", record_engine)
    def record_stroke(painter, path, gradient, line_width=3.5):
        stroke_engines.append(painter.paintEngine().type())
        paint_scope_path(painter, path, gradient, line_width)

    monkeypatch.setattr(qt_frontend, "paint_scope_path", record_stroke)
    art = QPixmap(100, 100)
    art.fill(QColor("red"))
    visualizer.art_pixmap = art
    visualizer.track_palette = None
    visualizer.fallback_pixmap = None
    window.edge_border.opacity = 0
    state = {"visualizerMode": 2, "artworkMode": "album", "albumArtOpacity": 100,
             "track": {"status": "paused"}}
    visualizer.set_state(state)
    quiet = visualizer.grabFramebuffer()
    visualizer.set_state({**state, "track": {"status": "playing"}, "bars": [.8] * 24})
    playing = visualizer.grabFramebuffer()
    assert quiet != playing
    assert visualizer.halo_fade_texture is not None
    assert engines and all(engine == QPaintEngine.Type.OpenGL2 for engine in engines)
    assert stroke_engines and all(engine == QPaintEngine.Type.OpenGL2 for engine in stroke_engines)


@pytest.mark.parametrize("swirl", [False, True])
@pytest.mark.parametrize("style,wire", [
    ("glow", "green"), ("bulb", "green"), ("star", "theme"), ("fairy", "none"),
])
def test_gpu_surface_renders_disco_with_opengl_painter(window, monkeypatch, swirl, style, wire):
    visualizer = window.visualizer
    if not isinstance(visualizer, qt_frontend.GPUVisualizerWidget):
        pytest.skip("hardware OpenGL widget unavailable")
    border = window.edge_border
    state = {"lightingMode": "disco", "edgeSwirl": swirl,
             "discoLightStyle": style, "discoWireColor": wire,
             "track": {"status": "playing" if swirl else "paused"},
             "bars": [1.0] * 24 if swirl else []}
    border.opacity = 1
    window.state = {**state, "lightingMode": "none"}
    visualizer.set_state(state)
    baseline = visualizer.grabFramebuffer()
    window.state = state
    border.update_disco(state, .05)
    paint_engines = []
    paint_disco = border.paint_disco

    def record_engine(painter, state):
        paint_engines.append(painter.paintEngine().type())
        paint_disco(painter, state)

    monkeypatch.setattr(border, "paint_disco", record_engine)
    image = visualizer.grabFramebuffer()
    assert paint_engines and all(engine == QPaintEngine.Type.OpenGL2 for engine in paint_engines)
    assert all(level == 1 for level in border.disco_levels)
    x, _ = qt_frontend.disco_light_positions(visualizer.width(), visualizer.height(), 16)[0]
    ratio = image.devicePixelRatio()
    assert image.pixelColor(round(x * ratio), round(5 * ratio)) != baseline.pixelColor(
        round(x * ratio), round(5 * ratio)
    )


def test_gpu_surface_keeps_controls_visible_and_survives_fullscreen(window):
    visualizer = window.visualizer
    if not isinstance(visualizer, qt_frontend.GPUVisualizerWidget):
        pytest.skip("hardware OpenGL widget unavailable")
    window.state = {"visualizerMode": 5}
    visualizer.set_state(window.state)
    window.hide_controls()
    QTest.qWait(50)
    hidden = window.grab().toImage()
    window.show_controls()
    QTest.qWait(50)
    visible = window.grab().toImage()
    ratio = visible.devicePixelRatio()
    for button in (window.next_button, window.fullscreen_button):
        position = button.mapTo(window, QPoint(0, 0))
        assert any(
            hidden.pixel(x, y) != visible.pixel(x, y)
            for x in range(int(position.x() * ratio), int((position.x() + button.width()) * ratio))
            for y in range(int(position.y() * ratio), int((position.y() + button.height()) * ratio))
        )
    window.showFullScreen()
    QTest.qWait(50)
    assert visualizer.isValid() and visualizer.initialized
    window.showNormal()
    QTest.qWait(50)
    assert visualizer.isValid() and visualizer.initialized


@pytest.mark.parametrize("angle", [0, 18, 36, 130])
@pytest.mark.parametrize("color_count", [1, 2, 3, 4, 5, 6, 7, 8])
@pytest.mark.parametrize("style", ["swirl", "rainbow", "static"])
def test_gpu_border_matches_raster(window, angle, color_count, style):
    visualizer = window.visualizer
    if not isinstance(visualizer, qt_frontend.GPUVisualizerWidget):
        pytest.skip("hardware OpenGL widget unavailable")
    border = window.edge_border
    border.set_palette([
        "#ff0000", "#00ff00", "#0000ff", "#ffff00",
        "#ff00ff", "#00ffff", "#ff8000", "#808080",
    ][:color_count])
    border.angle = angle
    border.opacity = .7
    state = {
        "edgeSwirl": style == "swirl",
        "lightingColorMode": "rainbow" if style == "rainbow" else "artwork",
        "edgeFade": 50, "edgeColorIntensity": 100,
        "edgeDepth": {0: 100, 18: 25, 36: 200, 130: 100}[angle],
    }
    width, height = visualizer.width(), visualizer.height()
    raster = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    raster.fill(QColor("#090b12"))
    painter = QPainter(raster)
    border.paint_border(painter, state)
    painter.end()

    visualizer.makeCurrent()
    framebuffer = QOpenGLFramebufferObject(width, height)
    assert framebuffer.isValid() and framebuffer.bind()
    functions = QOpenGLContext.currentContext().functions()
    functions.glViewport(0, 0, width, height)
    functions.glDisable(0x0C11)
    functions.glColorMask(True, True, True, True)
    functions.glClearColor(9 / 255, 11 / 255, 18 / 255, 1)
    functions.glClear(0x4000)
    visualizer.border_shader.draw(width, height, 1, border, state)
    gpu = framebuffer.toImage()
    framebuffer.release()
    visualizer.doneCurrent()

    for x, y in ((0, height // 2), (width - 1, height // 2),
                 (width // 2, 0), (width // 2, height - 1),
                 (20, 30), (width // 2, height // 2)):
        expected, actual = raster.pixelColor(x, y), gpu.pixelColor(x, y)
        assert all(abs(a - b) <= 6 for a, b in zip(expected.getRgb(), actual.getRgb()))


@pytest.mark.parametrize("color_mode", ["artwork", "rainbow"])
def test_gpu_static_border_uses_shader(window, monkeypatch, color_mode):
    visualizer = window.visualizer
    if not isinstance(visualizer, qt_frontend.GPUVisualizerWidget):
        pytest.skip("hardware OpenGL widget unavailable")
    calls = []
    draw = visualizer.border_shader.draw

    def record_draw(*args):
        calls.append(args[-1])
        return draw(*args)

    monkeypatch.setattr(visualizer.border_shader, "draw", record_draw)
    window.state = {
        "visualizerMode": 0, "lightingMode": "border",
        "lightingColorMode": color_mode, "edgeSwirl": False,
    }
    visualizer.grabFramebuffer()
    assert calls and calls[-1]["lightingColorMode"] == color_mode


@pytest.mark.parametrize("size", [(800, 400), (400, 800)])
def test_gpu_border_centre_is_circular(window, size):
    visualizer = window.visualizer
    if not isinstance(visualizer, qt_frontend.GPUVisualizerWidget):
        pytest.skip("hardware OpenGL widget unavailable")
    border = window.edge_border
    border.resize(*size)
    border.opacity = 1
    border.set_palette(["#ff0000"])
    visualizer.makeCurrent()
    framebuffer = QOpenGLFramebufferObject(*size)
    assert framebuffer.isValid() and framebuffer.bind()
    functions = QOpenGLContext.currentContext().functions()
    functions.glViewport(0, 0, *size)
    functions.glDisable(0x0C11)
    functions.glColorMask(True, True, True, True)
    functions.glClearColor(0, 0, 0, 1)
    functions.glClear(0x4000)
    visualizer.border_shader.draw(*size, 1, border, {"edgeFade": 50})
    image = framebuffer.toImage()
    framebuffer.release()
    visualizer.doneCurrent()

    cx, cy = size[0] // 2, size[1] // 2
    for distance in (120, 160, 190):
        samples = [image.pixelColor(cx + distance, cy).red(),
                   image.pixelColor(cx - distance, cy).red(),
                   image.pixelColor(cx, cy + distance).red(),
                   image.pixelColor(cx, cy - distance).red()]
        assert min(samples) > 0
        assert max(samples) - min(samples) <= 3


def test_gpu_failure_restores_cpu_visualizer(window, capsys):
    old = window.visualizer
    if not isinstance(old, qt_frontend.GPUVisualizerWidget):
        pytest.skip("hardware OpenGL widget unavailable")
    old.set_state({"visualizerMode": 1, "track": {"status": "playing"}, "wave": [0.2, 0.4]})
    window.edge_border.angle = 40
    window.fallback_visualizer("shader failure")
    QApplication.processEvents()
    assert isinstance(window.visualizer, qt_frontend.VisualizerWidget)
    assert window.visualizer.state["wave"] == [0.2, 0.4]
    assert window.visualizer.frame_timer.isActive()
    assert window.edge_border.parent() is window.visualizer
    assert window.edge_border.isVisible()
    assert window.edge_border.angle == 40
    assert "shader failure" in capsys.readouterr().err


def test_shader_initialization_failure_automatically_falls_back(window, monkeypatch, capsys):
    if not isinstance(window.visualizer, qt_frontend.GPUVisualizerWidget):
        pytest.skip("hardware OpenGL widget unavailable")

    def fail_shader():
        raise RuntimeError("shader initialization failed")

    monkeypatch.setattr(qt_frontend, "BorderShader", fail_shader)
    replacement_window = AmbiBeatQtWindow(window.backend)
    replacement_window.timer.stop()
    replacement_window.show()
    QTest.qWait(100)
    assert isinstance(replacement_window.visualizer, qt_frontend.VisualizerWidget)
    assert replacement_window.edge_border.isVisible()
    assert "shader initialization failed" in capsys.readouterr().err
    replacement_window.close()
