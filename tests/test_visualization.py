import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch
import urllib.error

import numpy as np
import pytest


MODULE_PATH = Path(__file__).parents[1] / "src" / "ambibeat" / "backend.py"
SPEC = importlib.util.spec_from_file_location("ambibeat_main", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_calculate_visualization_returns_expected_shapes():
    raw = (np.sin(np.linspace(0, 20, 1024)) * 10000).astype("<i2").tobytes()

    wave, bars, peak = MODULE.calculate_visualization(raw)

    assert len(wave) == 64
    assert len(bars) == 24
    assert 0.0 <= peak <= 1.0


def test_calculate_visualization_boosts_quiet_samples():
    raw = (np.sin(np.linspace(0, 20, 1024)) * 1000).astype("<i2").tobytes()

    normal_wave, _, _ = MODULE.calculate_visualization(raw, agc_peak=0.1, gain=1.0)
    boosted_wave, _, _ = MODULE.calculate_visualization(raw, agc_peak=0.1, gain=2.0)

    assert max(map(abs, boosted_wave)) > max(map(abs, normal_wave))


@pytest.mark.parametrize("rate", MODULE.SAMPLE_RATES)
def test_spectrum_bars_cover_broadband_audio_and_decay(rate):
    rng = np.random.default_rng(42)
    samples = rng.normal(0, 4000, 1024).clip(-32768, 32767).astype("<i2")
    _, bars, peak = MODULE.calculate_visualization(samples.tobytes(), rate=rate)
    assert len(bars) == 24
    assert all(0 < value <= 1 for value in bars)

    silence = np.zeros(1024, dtype="<i2").tobytes()
    _, fading, _ = MODULE.calculate_visualization(
        silence, rate=rate, previous_bars=bars, agc_peak=peak,
    )
    assert all(0 <= after < before for before, after in zip(bars, fading))


def test_spectrum_bars_respond_to_different_frequency_tones():
    rate = 48000
    def tone(frequency):
        samples = np.sin(np.arange(1024) * 2 * np.pi * frequency / rate) * 5000
        return MODULE.calculate_visualization(samples.astype("<i2").tobytes(), rate=rate)[1]

    low, high = tone(100), tone(8000)
    assert max(low[:6]) > .5 and max(low[20:]) < .02
    assert max(high[20:]) > .5 and max(high[:6]) < .02


def test_source_gain_accepts_only_supported_range():
    backend = MODULE.AmbiBeatBackend.__new__(MODULE.AmbiBeatBackend)
    backend.selected_source = "mpris:org.mpris.MediaPlayer2.player"
    backend.source_volume = SimpleNamespace(
        gains={}, lock=threading.RLock(), apply=lambda source: None,
    )
    backend._save_settings = lambda: None

    assert backend.set_source_gain(backend.selected_source, 2.4)
    assert backend.set_source_gain(backend.selected_source, 4.0)
    assert not backend.set_source_gain(backend.selected_source, 4.1)
    assert not backend.set_source_gain("bluetooth", 2.0)
    assert backend.source_volume.gains[backend.selected_source] == 4.0


def test_source_gain_is_remembered_if_stream_is_not_yet_available():
    backend = MODULE.AmbiBeatBackend.__new__(MODULE.AmbiBeatBackend)
    backend.selected_source = "mpris:org.mpris.MediaPlayer2.player"
    def missing_stream(source):
        raise MODULE.StreamNotFoundError("no stream")
    backend.source_volume = SimpleNamespace(gains={}, lock=threading.RLock(), apply=missing_stream)
    backend._save_settings = lambda: None
    assert backend.set_source_gain(backend.selected_source, 2.0)
    assert backend.source_volume.gains[backend.selected_source] == 2.0
    assert "no stream" in backend.boost_warning


def test_app_settings_are_saved_and_loaded_with_validated_values():
    settings = {
        "sample_rate": 96000,
        "source_prompt_seconds": 18,
        "source_prompt_enabled": False,
        "photosensitivity_warning_acknowledged": True,
        "source_gains": {"mpris:org.mpris.MediaPlayer2.player": 2.4},
        "selected_source": "mpris:org.mpris.MediaPlayer2.player",
        "last_source_instances": {"player": "mpris:org.mpris.MediaPlayer2.player"},
        "visualizer_mode": 5,
        "scope_zero_line": False,
        "scope_line_width": 175,
        "edge_swirl": True,
        "lighting_mode": "disco",
        "lighting_color_mode": "rainbow",
        "disco_light_style": "bulb",
        "disco_wire_color": "theme",
        "disco_light_count": 12,
        "disco_light_size": 150,
        "edge_swirl_direction": "random",
        "edge_swirl_slowdown": 20,
        "edge_color_intensity": 67,
        "edge_fade": 35,
        "edge_depth": 150,
        "edge_opacity": 60,
        "edge_color_count": 6,
        "album_art_opacity": 35,
        "artwork_mode": "logo",
    }

    with tempfile.TemporaryDirectory() as directory:
        settings_path = Path(directory) / "ambibeat" / "settings.json"
        with patch.object(MODULE, "APP_SETTINGS_PATH", str(settings_path)):
            MODULE._write_app_settings(settings)
            assert MODULE._load_app_settings() == settings


def test_settings_are_isolated_by_user_config_directory(tmp_path):
    code = (
        "import json, sys; from ambibeat.backend import _load_app_settings, _write_app_settings; "
        "value = int(sys.argv[1]); "
        "_write_app_settings({'sample_rate': value, 'photosensitivity_warning_acknowledged': True}) if value else None; "
        "print(json.dumps(_load_app_settings()))"
    )
    def run(config, value):
        env = {**os.environ, "XDG_CONFIG_HOME": str(config)}
        result = subprocess.run([sys.executable, "-c", code, str(value)], env=env,
                                check=True, capture_output=True, text=True)
        return json.loads(result.stdout)

    alice, bob = tmp_path / "alice", tmp_path / "bob"
    assert run(alice, 44100)["sample_rate"] == 44100
    assert run(alice, 0)["photosensitivity_warning_acknowledged"] is True
    assert run(bob, 0) == {}
    assert run(bob, 96000)["sample_rate"] == 96000
    assert run(alice, 0)["sample_rate"] == 44100


def test_photosensitivity_warning_acknowledgement_is_saved_and_validated(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    monkeypatch.setattr(MODULE, "APP_SETTINGS_PATH", str(path))
    monkeypatch.setattr(MODULE, "LEGACY_APP_SETTINGS_PATH", str(tmp_path / "legacy.json"))
    monkeypatch.setattr(MODULE, "ART_CACHE_DIR", str(tmp_path / "art"))
    monkeypatch.setattr(MODULE.AmbiBeatBackend, "_refresh_system_volume", lambda self: True)
    bus = SimpleNamespace(list_names=lambda: [])
    backend = MODULE.AmbiBeatBackend(bus)
    try:
        assert not backend.photosensitivity_warning_acknowledged
        assert backend.get_state()["showPhotosensitivityWarning"]
        backend.acknowledge_photosensitivity_warning()
        assert MODULE._load_app_settings()["photosensitivity_warning_acknowledged"] is True
        assert not backend.get_state()["showPhotosensitivityWarning"]
        backend.update_setting("show-photosensitivity-warning", True)
        assert MODULE._load_app_settings()["photosensitivity_warning_acknowledged"] is False
        assert backend.get_state()["showPhotosensitivityWarning"]
        with pytest.raises(ValueError):
            backend.update_setting("show-photosensitivity-warning", "yes")
        backend.update_setting("show-photosensitivity-warning", False)
    finally:
        backend.stop()
    backend = MODULE.AmbiBeatBackend(bus)
    try:
        assert backend.photosensitivity_warning_acknowledged
    finally:
        backend.stop()
    path.write_text(json.dumps({"photosensitivity_warning_acknowledged": 1}), encoding="utf-8")
    assert MODULE._load_app_settings() == {}


def test_photosensitivity_warning_acknowledgement_rolls_back_on_save_failure():
    backend = MODULE.AmbiBeatBackend.__new__(MODULE.AmbiBeatBackend)
    backend.photosensitivity_warning_acknowledged = False
    backend.source_volume = SimpleNamespace(lock=threading.RLock())
    def fail_save():
        raise OSError("disk full")
    backend._save_settings = fail_save
    with pytest.raises(OSError, match="disk full"):
        backend.acknowledge_photosensitivity_warning()
    assert not backend.photosensitivity_warning_acknowledged


def test_backend_stop_retries_failed_volume_restoration(monkeypatch):
    backend = MODULE.AmbiBeatBackend.__new__(MODULE.AmbiBeatBackend)
    backend._stopped = False
    backend.running = True
    backend._timer_ids = [42]
    removed = []
    monkeypatch.setattr(MODULE.GLib, "source_remove", removed.append)
    backend._glib_loop = SimpleNamespace(quit=lambda: None)
    backend._glib_thread = SimpleNamespace(join=lambda **kwargs: None, is_alive=lambda: False)
    backend.audio_thread = SimpleNamespace(join=lambda **kwargs: None, is_alive=lambda: False)
    backend._capture_lock = threading.Lock()
    backend._capture_proc = None
    backend._idle_inhibitor = None
    attempts = []
    def deactivate():
        attempts.append(True)
        if len(attempts) == 1:
            raise OSError("audio server unavailable")
    backend.source_volume = SimpleNamespace(deactivate=deactivate)
    with pytest.raises(RuntimeError, match="audio server unavailable"):
        backend.stop()
    assert not backend._stopped
    backend.stop()
    backend.stop()
    assert backend._stopped
    assert len(attempts) == 2
    assert removed == [42]


def test_backend_starts_and_stops(tmp_path, monkeypatch):
    monkeypatch.setattr(MODULE, "ART_CACHE_DIR", str(tmp_path / "art"))
    monkeypatch.setattr(MODULE, "_load_app_settings", lambda: {})
    monkeypatch.setattr(MODULE.AmbiBeatBackend, "_refresh_system_volume", lambda self: True)
    bus = SimpleNamespace(list_names=lambda: [])
    backend = MODULE.AmbiBeatBackend(bus)
    try:
        backend.activate()
        assert backend.source_volume.active
        assert backend._glib_thread.is_alive()
        assert backend.audio_thread.is_alive()
        assert backend.get_state()["selectedSource"] == ""
        assert backend.get_state()["scopeZeroLine"] is True
        assert backend.get_state()["scopeLineWidth"] == 100
    finally:
        backend.stop()
    assert not backend.source_volume.active
    assert not backend._glib_thread.is_alive()
    assert not backend.audio_thread.is_alive()


def test_scope_zero_line_persists_and_rejects_invalid_values(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    monkeypatch.setattr(MODULE, "APP_SETTINGS_PATH", str(path))
    monkeypatch.setattr(MODULE, "LEGACY_APP_SETTINGS_PATH", str(tmp_path / "legacy.json"))
    monkeypatch.setattr(MODULE, "ART_CACHE_DIR", str(tmp_path / "art"))
    monkeypatch.setattr(MODULE.AmbiBeatBackend, "_refresh_system_volume", lambda self: True)
    backend = MODULE.AmbiBeatBackend(SimpleNamespace(list_names=lambda: []))
    assert backend.update_setting("scope-zero-line", False) == ""
    assert backend.get_state()["scopeZeroLine"] is False
    assert MODULE._load_app_settings()["scope_zero_line"] is False
    with pytest.raises(ValueError):
        backend.update_setting("scope-zero-line", "false")
    def fail_write(_settings):
        raise OSError("disk full")
    monkeypatch.setattr(MODULE, "_write_app_settings", fail_write)
    with pytest.raises(OSError, match="disk full"):
        backend.update_setting("scope-zero-line", True)
    assert backend.scope_zero_line is False
    path.write_text(json.dumps({"scope_zero_line": "false"}), encoding="utf-8")
    assert MODULE._load_app_settings() == {}


def test_scope_line_width_validates_persists_and_rolls_back(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    monkeypatch.setattr(MODULE, "APP_SETTINGS_PATH", str(path))
    monkeypatch.setattr(MODULE, "LEGACY_APP_SETTINGS_PATH", str(tmp_path / "legacy.json"))
    monkeypatch.setattr(MODULE, "ART_CACHE_DIR", str(tmp_path / "art"))
    monkeypatch.setattr(MODULE.AmbiBeatBackend, "_refresh_system_volume", lambda self: True)
    backend = MODULE.AmbiBeatBackend(SimpleNamespace(list_names=lambda: []))
    try:
        for value in (25, 300):
            backend.update_setting("scope-line-width", value)
            assert MODULE._load_app_settings()["scope_line_width"] == value
            assert backend.get_state()["scopeLineWidth"] == value
        for value in (24, 301, True, 1.5):
            with pytest.raises(ValueError):
                backend.update_setting("scope-line-width", value)
        def fail_write(_settings):
            raise OSError("disk full")
        monkeypatch.setattr(MODULE, "_write_app_settings", fail_write)
        with pytest.raises(OSError, match="disk full"):
            backend.update_setting("scope-line-width", 100)
        assert backend.scope_line_width == 300
    finally:
        backend.stop()
    path.write_text(json.dumps({"scope_line_width": 301}), encoding="utf-8")
    assert MODULE._load_app_settings() == {}


def test_source_prompt_duration_persists_validates_and_rolls_back(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    monkeypatch.setattr(MODULE, "APP_SETTINGS_PATH", str(path))
    monkeypatch.setattr(MODULE, "LEGACY_APP_SETTINGS_PATH", str(tmp_path / "legacy.json"))
    monkeypatch.setattr(MODULE, "ART_CACHE_DIR", str(tmp_path / "art"))
    monkeypatch.setattr(MODULE.AmbiBeatBackend, "_refresh_system_volume", lambda self: True)
    backend = MODULE.AmbiBeatBackend(SimpleNamespace(list_names=lambda: []))
    try:
        assert backend.get_state()["sourcePromptSeconds"] == 10
        for value in (3, 60):
            backend.update_setting("source-prompt-seconds", value)
            assert MODULE._load_app_settings()["source_prompt_seconds"] == value
            assert backend.get_state()["sourcePromptSeconds"] == value
        for value in (2, 61, True, 2.5):
            with pytest.raises(ValueError):
                backend.update_setting("source-prompt-seconds", value)
        def fail_write(_settings):
            raise OSError("disk full")
        monkeypatch.setattr(MODULE, "_write_app_settings", fail_write)
        with pytest.raises(OSError, match="disk full"):
            backend.update_setting("source-prompt-seconds", 10)
        assert backend.source_prompt_seconds == 60
    finally:
        backend.stop()
    path.write_text(json.dumps({"source_prompt_seconds": 61}), encoding="utf-8")
    assert MODULE._load_app_settings() == {}


def test_source_prompt_enabled_persists_validates_and_rolls_back(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    monkeypatch.setattr(MODULE, "APP_SETTINGS_PATH", str(path))
    monkeypatch.setattr(MODULE, "LEGACY_APP_SETTINGS_PATH", str(tmp_path / "legacy.json"))
    monkeypatch.setattr(MODULE, "ART_CACHE_DIR", str(tmp_path / "art"))
    monkeypatch.setattr(MODULE.AmbiBeatBackend, "_refresh_system_volume", lambda self: True)
    backend = MODULE.AmbiBeatBackend(SimpleNamespace(list_names=lambda: []))
    try:
        assert backend.get_state()["sourcePromptEnabled"] is True
        backend.update_setting("source-prompt-enabled", False)
        assert MODULE._load_app_settings()["source_prompt_enabled"] is False
        assert backend.get_state()["sourcePromptEnabled"] is False
        with pytest.raises(ValueError):
            backend.update_setting("source-prompt-enabled", 1)
        def fail_write(_settings):
            raise OSError("disk full")
        monkeypatch.setattr(MODULE, "_write_app_settings", fail_write)
        with pytest.raises(OSError, match="disk full"):
            backend.update_setting("source-prompt-enabled", True)
        assert backend.source_prompt_enabled is False
    finally:
        backend.stop()
    path.write_text(json.dumps({"source_prompt_enabled": 1}), encoding="utf-8")
    assert MODULE._load_app_settings() == {}


def test_legacy_app_settings_are_migrated_to_ambibeat_directory():
    settings = {"sample_rate": 44100, "edge_swirl": True}

    with tempfile.TemporaryDirectory() as directory:
        legacy_path = Path(directory) / "bt-audio-companion" / "settings.json"
        new_path = Path(directory) / "ambibeat" / "settings.json"
        legacy_path.parent.mkdir()
        legacy_path.write_text(json.dumps(settings), encoding="utf-8")
        with (
            patch.object(MODULE, "APP_SETTINGS_PATH", str(new_path)),
            patch.object(MODULE, "LEGACY_APP_SETTINGS_PATH", str(legacy_path)),
        ):
            assert MODULE._load_app_settings() == settings

        assert json.loads(new_path.read_text(encoding="utf-8")) == {
            "version": 1,
            **settings,
        }
        assert legacy_path.exists()


def test_legacy_bluetooth_source_and_settings_are_removed(tmp_path):
    settings_path = tmp_path / "settings.json"
    player = "mpris:org.mpris.MediaPlayer2.player"
    settings_path.write_text(json.dumps({
        "bluetooth_gain": 2.4, "bluetooth_enabled": True, "selected_source": "bluetooth",
        "source_gains": {"bluetooth": 2.0, player: 1.5},
    }), encoding="utf-8")
    with patch.object(MODULE, "APP_SETTINGS_PATH", str(settings_path)):
        assert MODULE._load_app_settings() == {"selected_source": "", "source_gains": {player: 1.5}}


@pytest.mark.parametrize("saved,expected", [
    (0, 0), (1, 1), (2, 2), (3, 0), (4, 4), (5, 5), (6, 4), (7, 0), (8, 5),
])
def test_saved_visualizer_modes_migrate_to_supported_qt_modes(saved, expected):
    with tempfile.TemporaryDirectory() as directory:
        settings_path = Path(directory) / "settings.json"
        settings_path.write_text(json.dumps({"visualizer_mode": saved}), encoding="utf-8")
        with patch.object(MODULE, "APP_SETTINGS_PATH", str(settings_path)):
            assert MODULE._load_app_settings()["visualizer_mode"] == expected


def test_app_settings_ignore_out_of_range_values():
    invalid_settings = {
        "sample_rate": 12345,
        "bluetooth_gain": 10.0,
        "selected_source": "not-a-player",
        "visualizer_mode": 9,
        "edge_swirl": "true",
    }

    with tempfile.TemporaryDirectory() as directory:
        settings_path = Path(directory) / "settings.json"
        settings_path.write_text(json.dumps(invalid_settings), encoding="utf-8")
        with patch.object(MODULE, "APP_SETTINGS_PATH", str(settings_path)):
            assert MODULE._load_app_settings() == {}


def test_app_settings_ignore_invalid_edge_swirl_slowdown():
    with tempfile.TemporaryDirectory() as directory:
        settings_path = Path(directory) / "settings.json"
        with patch.object(MODULE, "APP_SETTINGS_PATH", str(settings_path)):
            for invalid_slowdown in (0, 21, True, 1.5):
                settings_path.write_text(
                    json.dumps({"edge_swirl_slowdown": invalid_slowdown}),
                    encoding="utf-8",
                )
                assert MODULE._load_app_settings() == {}


def test_app_settings_ignore_invalid_edge_color_intensity():
    with tempfile.TemporaryDirectory() as directory:
        settings_path = Path(directory) / "settings.json"
        with patch.object(MODULE, "APP_SETTINGS_PATH", str(settings_path)):
            for intensity in (-1, 101, True, 50.5):
                settings_path.write_text(
                    json.dumps({"edge_color_intensity": intensity}),
                    encoding="utf-8",
                )
                assert MODULE._load_app_settings() == {}


def test_app_settings_ignore_invalid_edge_fade():
    with tempfile.TemporaryDirectory() as directory:
        settings_path = Path(directory) / "settings.json"
        with patch.object(MODULE, "APP_SETTINGS_PATH", str(settings_path)):
            for fade in (-1, 101, True, 50.5):
                settings_path.write_text(
                    json.dumps({"edge_fade": fade}),
                    encoding="utf-8",
                )
                assert MODULE._load_app_settings() == {}


def test_app_settings_ignore_invalid_edge_depth():
    with tempfile.TemporaryDirectory() as directory:
        settings_path = Path(directory) / "settings.json"
        with patch.object(MODULE, "APP_SETTINGS_PATH", str(settings_path)):
            for depth in (24, 201, True, 50.5):
                settings_path.write_text(json.dumps({"edge_depth": depth}), encoding="utf-8")
                assert MODULE._load_app_settings() == {}


def test_app_settings_ignore_invalid_edge_opacity():
    with tempfile.TemporaryDirectory() as directory:
        settings_path = Path(directory) / "settings.json"
        with patch.object(MODULE, "APP_SETTINGS_PATH", str(settings_path)):
            for opacity in (-1, 101, True, 50.5):
                settings_path.write_text(
                    json.dumps({"edge_opacity": opacity}),
                    encoding="utf-8",
                )
                assert MODULE._load_app_settings() == {}


def test_app_settings_ignore_invalid_album_art_opacity():
    with tempfile.TemporaryDirectory() as directory:
        settings_path = Path(directory) / "settings.json"
        with patch.object(MODULE, "APP_SETTINGS_PATH", str(settings_path)):
            for opacity in (-1, 101, True, 50.5):
                settings_path.write_text(
                    json.dumps({"album_art_opacity": opacity}),
                    encoding="utf-8",
                )
                assert MODULE._load_app_settings() == {}


def test_sample_rate_and_visualizer_mode_changes_are_saved():
    backend = MODULE.AmbiBeatBackend.__new__(MODULE.AmbiBeatBackend)
    backend.sample_rate = 48000
    backend.visualizer_mode = 0
    saved_settings = []
    backend._save_settings = lambda: saved_settings.append(
        (backend.sample_rate, backend.visualizer_mode)
    )

    assert backend.set_sample_rate(22050)
    assert backend.set_sample_rate(32000)
    assert backend.set_sample_rate(96000)
    assert not backend.set_visualizer_mode(3)
    assert backend.set_visualizer_mode(4)
    assert backend.set_visualizer_mode(5)
    assert not backend.set_visualizer_mode(6)

    assert saved_settings == [
        (22050, 0), (32000, 0), (96000, 0),
        (96000, 4), (96000, 5)
    ]


def test_edge_swirl_setting_is_validated_and_saved():
    backend = MODULE.AmbiBeatBackend.__new__(MODULE.AmbiBeatBackend)
    backend.edge_swirl = False
    saved_settings = []
    backend._save_settings = lambda: saved_settings.append(backend.edge_swirl)

    assert backend.set_edge_swirl(True)
    assert backend.set_edge_swirl(False)
    assert not backend.set_edge_swirl("true")

    assert saved_settings == [True, False]


def test_edge_swirl_direction_is_validated_saved_and_rolled_back():
    backend = MODULE.AmbiBeatBackend.__new__(MODULE.AmbiBeatBackend)
    backend.edge_swirl_direction = "clockwise"
    saved = []
    backend._save_settings = lambda: saved.append(backend.edge_swirl_direction)

    for direction in ("counter-clockwise", "random", "clockwise"):
        assert backend.set_edge_swirl_direction(direction)
    assert not backend.set_edge_swirl_direction("sideways")
    assert saved == ["counter-clockwise", "random", "clockwise"]

    def fail_save():
        raise OSError("disk full")

    backend._save_settings = fail_save
    try:
        backend.set_edge_swirl_direction("random")
    except OSError:
        pass
    else:
        assert False, "settings failure must be surfaced"
    assert backend.edge_swirl_direction == "clockwise"


def test_random_edge_direction_is_stable_per_track_and_shared():
    backend = MODULE.AmbiBeatBackend.__new__(MODULE.AmbiBeatBackend)
    backend.edge_swirl_direction = "random"
    backend._edge_direction_lock = threading.Lock()
    backend._edge_direction_track = None
    backend._edge_random_direction = None
    track = {"title": "First", "artist": "Artist", "album": "Album", "status": "playing"}
    with patch.object(MODULE.random, "choice", side_effect=["clockwise", "counter-clockwise"]) as choose:
        assert backend._resolved_edge_swirl_direction(track, "bluetooth") == "clockwise"
        assert backend._resolved_edge_swirl_direction({**track, "status": "paused"}, "bluetooth") == "clockwise"
        assert backend._resolved_edge_swirl_direction({**track, "title": "Next"}, "bluetooth") == "counter-clockwise"
        assert choose.call_count == 2
        backend.edge_swirl_direction = "clockwise"
        assert backend._resolved_edge_swirl_direction(track, "bluetooth") == "clockwise"


def test_invalid_saved_edge_swirl_direction_is_ignored():
    with tempfile.TemporaryDirectory() as directory:
        settings_path = Path(directory) / "settings.json"
        settings_path.write_text(json.dumps({"edge_swirl_direction": "sideways"}), encoding="utf-8")
        with patch.object(MODULE, "APP_SETTINGS_PATH", str(settings_path)):
            assert MODULE._load_app_settings() == {}


def test_invalid_saved_lighting_color_mode_is_ignored():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "settings.json"
        path.write_text(json.dumps({"lighting_color_mode": "invalid"}), encoding="utf-8")
        with patch.object(MODULE, "APP_SETTINGS_PATH", str(path)):
            assert MODULE._load_app_settings() == {}


@pytest.mark.parametrize("mode", ["christmas", "gelato"])
def test_builtin_lighting_mode_is_saved_loaded_and_rolled_back(mode, tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    monkeypatch.setattr(MODULE, "APP_SETTINGS_PATH", str(path))
    monkeypatch.setattr(MODULE, "LEGACY_APP_SETTINGS_PATH", str(tmp_path / "legacy.json"))
    backend = MODULE.AmbiBeatBackend.__new__(MODULE.AmbiBeatBackend)
    backend.lighting_color_mode = "artwork"
    backend.custom_palettes = []
    backend._save_settings = lambda: MODULE._write_app_settings(
        {"lighting_color_mode": backend.lighting_color_mode})
    backend.update_setting("lighting-color-mode", mode)
    assert MODULE._load_app_settings()["lighting_color_mode"] == mode
    with pytest.raises(ValueError):
        backend.update_setting("lighting-color-mode", "invalid")

    def fail_save():
        raise OSError("disk full")
    backend._save_settings = fail_save
    with pytest.raises(OSError, match="disk full"):
        backend.update_setting("lighting-color-mode", "rainbow")
    assert backend.lighting_color_mode == mode


@pytest.mark.parametrize("setting,valid", [
    ("disco_light_style", ("glow", "fairy", "bulb", "star")),
    ("disco_wire_color", ("green", "theme", "none")),
])
def test_festive_light_settings_validate_persist_and_roll_back(setting, valid, tmp_path, monkeypatch, capsys):
    backend = MODULE.AmbiBeatBackend.__new__(MODULE.AmbiBeatBackend)
    setattr(backend, setting, valid[0])
    saved = []
    backend._save_settings = lambda: saved.append(getattr(backend, setting))
    for value in valid:
        backend.update_setting(setting.replace("_", "-"), value)
    assert saved == list(valid)
    with pytest.raises(ValueError):
        backend.update_setting(setting.replace("_", "-"), "invalid")
    assert getattr(backend, setting) == valid[-1]

    def fail_save():
        raise OSError("disk full")
    backend._save_settings = fail_save
    with pytest.raises(OSError, match="disk full"):
        backend.update_setting(setting.replace("_", "-"), valid[0])
    assert getattr(backend, setting) == valid[-1]

    path = tmp_path / "settings.json"
    monkeypatch.setattr(MODULE, "APP_SETTINGS_PATH", str(path))
    monkeypatch.setattr(MODULE, "LEGACY_APP_SETTINGS_PATH", str(tmp_path / "legacy.json"))
    for value in valid:
        MODULE._write_app_settings({setting: value})
        assert MODULE._load_app_settings()[setting] == value
    path.write_text(json.dumps({setting: "invalid"}), encoding="utf-8")
    assert MODULE._load_app_settings() == {}
    assert "Ignoring invalid saved" in capsys.readouterr().out


@pytest.mark.parametrize("setting,minimum,maximum", [
    ("disco_light_count", 3, 40), ("disco_light_size", 40, 200),
])
def test_disco_settings_validate_persist_and_roll_back(setting, minimum, maximum):
    backend = MODULE.AmbiBeatBackend.__new__(MODULE.AmbiBeatBackend)
    setattr(backend, setting, minimum)
    saved = []
    backend._save_settings = lambda: saved.append(getattr(backend, setting))
    setter = getattr(backend, "set_" + setting)
    assert setter(maximum)
    assert saved == [maximum]
    for invalid in (minimum - 1, maximum + 1, True, 1.5):
        assert not setter(invalid)
    def fail_save():
        raise OSError("disk full")
    backend._save_settings = fail_save
    with pytest.raises(OSError):
        setter(minimum)
    assert getattr(backend, setting) == maximum
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "settings.json"
        path.write_text(json.dumps({setting: maximum + 1}), encoding="utf-8")
        with patch.object(MODULE, "APP_SETTINGS_PATH", str(path)):
            assert MODULE._load_app_settings() == {}


def test_invalid_saved_lighting_mode_is_ignored():
    with tempfile.TemporaryDirectory() as directory:
        settings_path = Path(directory) / "settings.json"
        settings_path.write_text(json.dumps({"lighting_mode": "invalid"}), encoding="utf-8")
        with patch.object(MODULE, "APP_SETTINGS_PATH", str(settings_path)):
            assert MODULE._load_app_settings() == {}


def test_invalid_saved_artwork_mode_is_ignored():
    with tempfile.TemporaryDirectory() as directory:
        settings_path = Path(directory) / "settings.json"
        settings_path.write_text(json.dumps({"artwork_mode": "invalid"}), encoding="utf-8")
        with patch.object(MODULE, "APP_SETTINGS_PATH", str(settings_path)):
            assert MODULE._load_app_settings() == {}


def test_state_exposes_selected_and_resolved_rotation_direction():
    backend = MODULE.AmbiBeatBackend.__new__(MODULE.AmbiBeatBackend)
    backend.track_info = {"title": "Song", "artist": "Artist", "album": "Album"}
    backend.selected_source = "mpris:org.mpris.MediaPlayer2.player"
    backend.edge_swirl_direction = "random"
    backend._edge_direction_lock = threading.Lock()
    backend._edge_direction_track = None
    backend._edge_random_direction = None
    backend._mpris_players = {
        "player": {"id": "mpris:org.mpris.MediaPlayer2.player", "label": "Player",
                   "track": {"status": "playing"}},
    }
    backend.bars, backend.wave = [], []
    backend.sample_rate = 48000
    backend.source_prompt_seconds = 10
    backend.source_prompt_enabled = True
    backend.source_volume = SimpleNamespace(gains={})
    backend.last_source_instances = {}
    backend.boost_warning = ""
    backend.photosensitivity_warning_acknowledged = False
    backend.visualizer_mode = 1
    backend.scope_zero_line = True
    backend.scope_line_width = 100
    backend.edge_swirl = True
    backend.lighting_mode = "disco"
    backend.lighting_color_mode = "rainbow"
    backend.custom_palettes = []
    backend.disco_light_style = "bulb"
    backend.disco_wire_color = "theme"
    backend.disco_light_count = 12
    backend.disco_light_size = 150
    backend.edge_swirl_slowdown = 1
    backend.edge_color_intensity = 100
    backend.edge_fade = 50
    backend.edge_depth = 150
    backend.edge_opacity = 70
    backend.edge_color_count = 8
    backend.album_art_opacity = 22
    backend.artwork_mode = "logo"
    backend.system_volume = 50

    with patch.object(MODULE.random, "choice", return_value="counter-clockwise"):
        state = backend.get_state()
    assert state["edgeSwirlDirection"] == "random"
    assert state["edgeSwirlResolvedDirection"] == "counter-clockwise"
    assert state["artworkMode"] == "logo"
    assert state["lightingMode"] == "disco"
    assert state["lightingColorMode"] == "rainbow"
    assert state["discoLightStyle"] == "bulb"
    assert state["discoWireColor"] == "theme"
    assert state["edgeDepth"] == 150
    assert state["discoLightCount"] == 12
    assert state["discoLightSize"] == 150
    assert state["sources"] == [
        {"id": "mpris:org.mpris.MediaPlayer2.player", "label": "Player", "status": "playing",
         "identity": "", "application": "", "desktopEntry": "", "lastSelected": False},
    ]


def test_edge_swirl_slowdown_is_validated_and_saved():
    backend = MODULE.AmbiBeatBackend.__new__(MODULE.AmbiBeatBackend)
    backend.edge_swirl_slowdown = 1
    saved_settings = []
    backend._save_settings = lambda: saved_settings.append(backend.edge_swirl_slowdown)

    assert backend.set_edge_swirl_slowdown(20)
    assert backend.set_edge_swirl_slowdown(1)
    assert not backend.set_edge_swirl_slowdown(0)
    assert not backend.set_edge_swirl_slowdown(21)
    assert not backend.set_edge_swirl_slowdown(True)
    assert saved_settings == [20, 1]


def test_edge_color_intensity_is_validated_and_saved():
    backend = MODULE.AmbiBeatBackend.__new__(MODULE.AmbiBeatBackend)
    backend.edge_color_intensity = 100
    saved_settings = []
    backend._save_settings = lambda: saved_settings.append(backend.edge_color_intensity)

    assert backend.set_edge_color_intensity(0)
    assert backend.set_edge_color_intensity(67)
    assert backend.set_edge_color_intensity(100)
    assert not backend.set_edge_color_intensity(-1)
    assert not backend.set_edge_color_intensity(101)
    assert not backend.set_edge_color_intensity(True)
    assert not backend.set_edge_color_intensity(50.5)
    assert saved_settings == [0, 67, 100]


def test_edge_fade_is_validated_and_saved():
    backend = MODULE.AmbiBeatBackend.__new__(MODULE.AmbiBeatBackend)
    backend.edge_fade = 50
    saved_settings = []
    backend._save_settings = lambda: saved_settings.append(backend.edge_fade)

    assert backend.set_edge_fade(0)
    assert backend.set_edge_fade(35)
    assert backend.set_edge_fade(100)
    assert not backend.set_edge_fade(-1)
    assert not backend.set_edge_fade(101)
    assert not backend.set_edge_fade(True)
    assert not backend.set_edge_fade(50.5)
    assert saved_settings == [0, 35, 100]


def test_edge_depth_is_validated_saved_and_rolled_back():
    backend = MODULE.AmbiBeatBackend.__new__(MODULE.AmbiBeatBackend)
    backend.edge_depth = 100
    saved = []
    backend._save_settings = lambda: saved.append(backend.edge_depth)
    for value in (25, 100, 200):
        assert backend.set_edge_depth(value)
    for value in (24, 201, True, 50.5):
        assert not backend.set_edge_depth(value)
    assert saved == [25, 100, 200]

    def fail_save():
        raise OSError("disk full")

    backend._save_settings = fail_save
    with pytest.raises(OSError):
        backend.set_edge_depth(100)
    assert backend.edge_depth == 200


def test_edge_opacity_is_validated_and_saved():
    backend = MODULE.AmbiBeatBackend.__new__(MODULE.AmbiBeatBackend)
    backend.edge_opacity = 70
    saved_settings = []
    backend._save_settings = lambda: saved_settings.append(backend.edge_opacity)

    assert backend.set_edge_opacity(0)
    assert backend.set_edge_opacity(60)
    assert backend.set_edge_opacity(100)
    assert not backend.set_edge_opacity(-1)
    assert not backend.set_edge_opacity(101)
    assert not backend.set_edge_opacity(True)
    assert not backend.set_edge_opacity(50.5)
    assert saved_settings == [0, 60, 100]


def test_edge_color_count_is_validated_and_saved():
    backend = MODULE.AmbiBeatBackend.__new__(MODULE.AmbiBeatBackend)
    backend.edge_color_count = 8
    saved_settings = []
    backend._save_settings = lambda: saved_settings.append(backend.edge_color_count)

    assert backend.set_edge_color_count(2)
    assert backend.set_edge_color_count(6)
    assert backend.set_edge_color_count(8)
    assert not backend.set_edge_color_count(1)
    assert not backend.set_edge_color_count(9)
    assert not backend.set_edge_color_count(True)
    assert not backend.set_edge_color_count(6.5)
    assert saved_settings == [2, 6, 8]


def test_album_art_opacity_is_validated_and_saved():
    backend = MODULE.AmbiBeatBackend.__new__(MODULE.AmbiBeatBackend)
    backend.album_art_opacity = 22
    saved_settings = []
    backend._save_settings = lambda: saved_settings.append(backend.album_art_opacity)

    assert backend.set_album_art_opacity(0)
    assert backend.set_album_art_opacity(35)
    assert backend.set_album_art_opacity(100)
    assert not backend.set_album_art_opacity(-1)
    assert not backend.set_album_art_opacity(101)
    assert not backend.set_album_art_opacity(True)
    assert not backend.set_album_art_opacity(35.5)
    assert saved_settings == [0, 35, 100]


def test_system_volume_control_sets_and_validates_default_output_level():
    backend = MODULE.AmbiBeatBackend.__new__(MODULE.AmbiBeatBackend)
    backend.system_volume = 100
    backend._last_system_volume_error = ""
    calls = []

    with patch.object(MODULE.subprocess, "run", side_effect=lambda args, **_kwargs: calls.append(args)):
        assert backend.set_system_volume(42)
        assert not backend.set_system_volume(101)

    assert backend.system_volume == 42
    assert calls == [["pactl", "set-sink-volume", "@DEFAULT_SINK@", "42%"]]


def test_switching_mpris_sources_pauses_other_players_and_plays_selected():
    backend = MODULE.AmbiBeatBackend.__new__(MODULE.AmbiBeatBackend)
    old_source = "mpris:org.mpris.MediaPlayer2.old"
    other_source = "mpris:org.mpris.MediaPlayer2.other"
    target_source = "mpris:org.mpris.MediaPlayer2.target"
    backend.selected_source = old_source
    backend.last_source_instances = {}
    backend._mpris_players = {
        source.removeprefix("mpris:"): {
            "id": source,
            "identity": source,
            "application": source,
            "track": {"title": "Track", "artist": "Artist", "album": "", "status": status},
            "art_url": "",
        }
        for source, status in (
            (old_source, "playing"),
            (other_source, "playing"),
            (target_source, "paused"),
        )
    }
    backend._publish_track = lambda *_args: None
    backend._save_settings = lambda: None
    actions = []
    backend._mpris_control_warning = (
        lambda source, action: actions.append((source, action)) or ""
    )
    backend.source_volume = SimpleNamespace(
        lock=threading.RLock(), restore=lambda: None, apply=lambda source: None,
    )

    switched, warning = backend.set_source(target_source)

    assert switched
    assert warning == ""
    assert actions == [
        (old_source, "Pause"),
        (other_source, "Pause"),
        (target_source, "Play"),
    ]
    assert backend.set_source("bluetooth") == (False, "")
    assert backend.selected_source == target_source
    assert backend.last_source_instances[target_source] == target_source


def test_source_refresh_prefers_last_selected_instance_and_preserves_current(monkeypatch):
    first = "org.mpris.MediaPlayer2.example.instance1"
    last = "org.mpris.MediaPlayer2.example.instance2"
    available = [first, last]
    class Properties:
        def __init__(self, name):
            self.name = name

        def GetAll(self, interface):
            if interface == "org.mpris.MediaPlayer2":
                return {"Identity": "Example Web Browser" if self.name == first else "Example",
                        "DesktopEntry": "example-app"}
            return {"PlaybackStatus": "Playing" if self.name == first else "Paused", "Metadata": {}}

    backend = MODULE.AmbiBeatBackend.__new__(MODULE.AmbiBeatBackend)
    backend.session_bus = SimpleNamespace(list_names=lambda: available,
                                          get_object=lambda name, *_args, **_kwargs: name)
    monkeypatch.setattr(MODULE.dbus, "Interface", lambda name, _interface: Properties(name))
    backend._mpris_errors = set()
    backend.selected_source = "mpris:org.mpris.MediaPlayer2.missing"
    backend.last_source_instances = {"example": "mpris:" + last}
    backend.source_volume = SimpleNamespace(lock=threading.RLock(), restore=Mock())
    backend._publish_track = Mock()
    backend._sync_idle_inhibition = Mock()
    backend._refresh_mpris_players()
    assert backend.selected_source == "mpris:" + last
    assert backend._mpris_players[last]["desktop_entry"] == "example-app"

    backend.selected_source = "mpris:" + first
    backend._refresh_mpris_players()
    assert backend.selected_source == "mpris:" + first
    backend.selected_source = "mpris:org.mpris.MediaPlayer2.missing"
    available.remove(last)
    backend._refresh_mpris_players()
    assert backend.selected_source == "mpris:" + first


def test_last_selected_instances_survive_backend_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(MODULE, "APP_SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setattr(MODULE, "LEGACY_APP_SETTINGS_PATH", str(tmp_path / "legacy.json"))
    monkeypatch.setattr(MODULE, "ART_CACHE_DIR", str(tmp_path / "art"))
    monkeypatch.setattr(MODULE.AmbiBeatBackend, "_refresh_system_volume", lambda self: True)
    monkeypatch.setattr(MODULE.AmbiBeatBackend, "_publish_track", lambda *_args: None)
    first = "org.mpris.MediaPlayer2.example.instance1"
    last = "org.mpris.MediaPlayer2.example.instance2"
    bus = SimpleNamespace(list_names=lambda: [])
    backend = MODULE.AmbiBeatBackend(bus)
    try:
        backend._mpris_players = {
            last: {"id": "mpris:" + last, "identity": "Example", "application": "example",
                   "track": {"status": "paused"}, "art_url": ""},
        }
        monkeypatch.setattr(backend.source_volume, "apply", lambda _source: None)
        monkeypatch.setattr(backend, "_mpris_control_warning", lambda *_args: "")
        assert backend.set_source("mpris:" + last)[0]
        assert MODULE._load_app_settings()["last_source_instances"] == {"example": "mpris:" + last}
    finally:
        backend.stop()

    class Properties:
        def __init__(self, name):
            self.name = name

        def GetAll(self, interface):
            if interface == "org.mpris.MediaPlayer2":
                return {"Identity": "Example", "DesktopEntry": "example-app"}
            return {"PlaybackStatus": "Playing" if self.name == first else "Paused", "Metadata": {}}

    bus = SimpleNamespace(list_names=lambda: [first, last],
                          get_object=lambda name, *_args, **_kwargs: name)
    monkeypatch.setattr(MODULE.dbus, "Interface", lambda name, _interface: Properties(name))
    backend = MODULE.AmbiBeatBackend(bus)
    try:
        assert backend.selected_source == "mpris:" + last
        sources = backend.get_state()["sources"]
        assert next(item for item in sources if item["id"] == "mpris:" + last)["lastSelected"]
    finally:
        backend.stop()


def test_saved_source_instances_reject_invalid_entries(tmp_path, monkeypatch, capsys):
    path = tmp_path / "settings.json"
    monkeypatch.setattr(MODULE, "APP_SETTINGS_PATH", str(path))
    monkeypatch.setattr(MODULE, "LEGACY_APP_SETTINGS_PATH", str(tmp_path / "legacy.json"))
    path.write_text(json.dumps({"last_source_instances": {
        "Example": "mpris:org.mpris.MediaPlayer2.example",
        "Invalid": "bluetooth", "": "mpris:org.mpris.MediaPlayer2.other",
    }}), encoding="utf-8")
    assert MODULE._load_app_settings()["last_source_instances"] == {
        "Example": "mpris:org.mpris.MediaPlayer2.example"}
    assert "Ignoring invalid saved playback instance" in capsys.readouterr().out


def test_find_matching_album_cover_skips_unrelated_first_result():
    hits = [
        {
            "title": "Just the Way You Are",
            "artist": {"name": "Another Artist"},
            "album": {"title": "Just the Way You Are", "cover_big": "wrong-artist-cover"},
        },
        {
            "title": "Just the Way You Are",
            "artist": {"name": "Milky"},
            "album": {"title": "Just the Way You Are", "cover_big": "matching-cover"},
        },
    ]

    cover = MODULE._find_matching_album_cover(
        hits, "Milky", "Just The Way You Are", "Just The Way You Are"
    )

    assert cover == "matching-cover"


def test_find_matching_album_cover_rejects_album_mismatch():
    hits = [
        {
            "title": "Just the Way You Are",
            "artist": {"name": "Milky"},
            "album": {"title": "Different Album", "cover_big": "wrong-cover"},
        }
    ]

    cover = MODULE._find_matching_album_cover(
        hits, "Milky", "Just The Way You Are", "Just The Way You Are"
    )

    assert cover == ""


def test_find_matching_track_cover_allows_same_artist_and_title_on_another_album():
    hits = [
        {
            "title": "Wild Long Lie",
            "artist": {"name": "Sam Fender"},
            "album": {"title": "Wild Long Lie", "cover_big": "matching-track-cover"},
        }
    ]

    cover = MODULE._find_matching_track_cover(hits, "Sam Fender", "Wild Long Lie")

    assert cover == "matching-track-cover"


def test_catalog_collaboration_matches_a_credited_artist_only_for_exact_title():
    hits = [
        {"title": "Another Song", "artist": {"name": "Artist One"},
         "album": {"cover_big": "wrong-title"}},
        {"title": "Song", "artist": {"name": "Artist Three"},
         "album": {"cover_big": "wrong-artist"}},
        {"title": "Song", "artist": {"name": "Artist One"},
         "album": {"cover_big": "right-cover"}},
    ]
    assert MODULE._find_matching_track_cover(hits, "Artist One & Artist Two", "Song") == "right-cover"
    assert MODULE._find_matching_track_cover(hits[:2], "Artist One & Artist Two", "Song") == ""
    itunes = [{"trackName": "Song", "artistName": "Artist Two", "artworkUrl100": "cover"}]
    assert MODULE._find_matching_itunes_cover(itunes, "Artist One & Artist Two", "Song", "") == "cover"
    similar_title = [{"title": "Example Titles", "artist": {"name": "Artist One"},
                      "album": {"cover_big": "wrong-title"}}]
    assert MODULE._find_matching_track_cover(
        similar_title, "Artist One & Artist Two", "Example Title"
    ) == ""
    hits.append({"title": "Song", "artist": {"name": "Artist One & Artist Two"},
                 "album": {"cover_big": "full-credit"}})
    assert MODULE._find_matching_track_cover(hits, "Artist One & Artist Two", "Song") == "full-credit"


def test_featured_device_title_matches_backend_track_title():
    deezer_hits = [
        {
            "title": "Got A Little Crush On You",
            "artist": {"name": "Wildflowers"},
            "album": {"title": "Buttercup", "cover_big": "matching-featured-cover"},
        }
    ]
    itunes_hits = [
        {
            "trackName": "Got a Little Crush on You (feat. Emmi)",
            "artistName": "Wildflowers",
            "collectionName": "Buttercup (feat. Emmi) - EP",
            "artworkUrl100": "https://example.com/100x100bb.jpg",
        }
    ]

    assert MODULE._find_matching_track_cover(
        deezer_hits, "Wildflowers", "Got A Little Crush On You (feat. Emmi)"
    ) == "matching-featured-cover"
    assert MODULE._find_matching_itunes_cover(
        itunes_hits,
        "Wildflowers",
        "Got A Little Crush On You (feat. Emmi)",
        "",
    ) == "https://example.com/600x600bb.jpg"


def test_find_matching_itunes_cover_matches_artist_album_and_title():
    hits = [
        {
            "trackName": "Wrong Track",
            "artistName": "Other Artist",
            "collectionName": "Wrong Album",
            "artworkUrl100": "https://example.com/wrong.jpg",
        },
        {
            "trackName": "Just the Way You Are",
            "artistName": "Milky",
            "collectionName": "Just the Way You Are",
            "artworkUrl100": "https://example.com/100x100bb.jpg",
        },
    ]

    cover = MODULE._find_matching_itunes_cover(
        hits, "Milky", "Just The Way You Are", "Just The Way You Are"
    )

    assert cover == "https://example.com/600x600bb.jpg"


def test_itunes_cover_matches_youtube_music_track_metadata():
    hits = [
        {
            "trackName": "The Human Heart",
            "artistName": "John Pattern & Ram Dass",
            "collectionName": "Rainbow Body",
            "artworkUrl100": "https://example.com/100x100bb.jpg",
        }
    ]

    cover = MODULE._find_matching_itunes_cover(
        hits,
        "John Pattern & Ram Dass",
        "The Human Heart",
        "Rainbow Body",
    )

    assert cover == "https://example.com/600x600bb.jpg"


class _FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self.payload


def test_remote_album_art_falls_back_to_itunes_when_deezer_is_unavailable():
    def open_url(request, timeout):
        url = request.full_url
        if "api.deezer.com" in url:
            raise urllib.error.URLError("offline")
        if "itunes.apple.com/search" in url:
            payload = json.dumps({
                "results": [{
                    "trackName": "Wild Long Lie",
                    "artistName": "Sam Fender",
                    "collectionName": "Wild Long Lie",
                    "artworkUrl100": "https://example.com/100x100bb.jpg",
                }]
            }).encode("utf-8")
            return _FakeResponse(payload)
        if "example.com" in url:
            return _FakeResponse(b"\x89PNG\r\n\x1a\nfallback")
        raise AssertionError(f"Unexpected URL: {url}")

    with patch.object(MODULE.urllib.request, "urlopen", side_effect=open_url):
        image_data, source = MODULE._download_remote_album_art(
            "Sam Fender", "Wild Long Lie", "Wild Long Lie"
        )

    assert image_data.startswith(b"\x89PNG")
    assert source == "iTunes"


def test_remote_album_art_returns_error_when_both_services_are_unavailable():
    def open_url(_request, timeout):
        raise urllib.error.URLError("offline")

    with patch.object(MODULE.urllib.request, "urlopen", side_effect=open_url):
        image_data, error = MODULE._download_remote_album_art(
            "Artist", "Title", "Album"
        )

    assert image_data is None
    assert "Deezer" in error
    assert "iTunes" in error


def test_remote_art_retries_individual_credits_when_full_search_has_no_match():
    requests = []
    def open_url(request, timeout):
        url = request.full_url
        requests.append(url)
        if "api.deezer.com" in url:
            query = MODULE.urllib.parse.parse_qs(MODULE.urllib.parse.urlparse(url).query)["q"][0]
            hits = []
            if query == "Artist One Song":
                hits = [{"title": "Song", "artist": {"name": "Artist One"},
                         "album": {"cover_big": "https://example.com/cover.jpg"}}]
            return _FakeResponse(json.dumps({"data": hits}).encode())
        if "itunes.apple.com/search" in url:
            return _FakeResponse(b'{"results": []}')
        if url == "https://example.com/cover.jpg":
            return _FakeResponse(b"cover")
        raise AssertionError(f"Unexpected URL: {url}")

    with patch.object(MODULE.urllib.request, "urlopen", side_effect=open_url):
        image, service = MODULE._download_remote_album_art("Artist One & Artist Two", "Song", "")
    assert (image, service) == (b"cover", "Deezer")
    assert len(requests) == 5


def test_catalog_search_cleans_video_title_before_query_and_matching():
    queries = []
    def open_url(request, timeout):
        url = request.full_url
        if "api.deezer.com" in url:
            query = MODULE.urllib.parse.parse_qs(MODULE.urllib.parse.urlparse(url).query)["q"][0]
            queries.append(query)
            payload = {"data": [{
                "title": "The Song", "artist": {"name": "Artist One"},
                "album": {"cover_big": "https://example.com/cover.jpg"},
            }]}
            return _FakeResponse(json.dumps(payload).encode())
        if "itunes.apple.com/search" in url:
            query = MODULE.urllib.parse.parse_qs(MODULE.urllib.parse.urlparse(url).query)["term"][0]
            queries.append(query)
            return _FakeResponse(b'{"results": []}')
        if url == "https://example.com/cover.jpg":
            return _FakeResponse(b"cover")
        raise AssertionError(f"Unexpected URL: {url}")

    with patch.object(MODULE.urllib.request, "urlopen", side_effect=open_url):
        image, service = MODULE._download_remote_album_art(
            "Artist One & Artist Two",
            "Artist One & Artist Two - The Song (Official Audio) (feat. Guest)", "",
        )
    assert (image, service) == (b"cover", "Deezer")
    assert queries == ["Artist One & Artist Two The Song"] * 2


def test_catalog_title_cleanup_preserves_distinct_titles_and_versions():
    assert MODULE._catalog_track_title("Artist", "Other Artist - Song (Live)") == "Other Artist - Song (Live)"
    assert MODULE._catalog_track_title("Artist", "Artist - Song (Live)") == "Song (Live)"
    assert MODULE._catalog_track_title("Artist", "Artist - Song [Official Video]") == "Song"


def test_http_player_art_is_primary_and_matching_mpris_art_is_not_used():
    backend = MODULE.AmbiBeatBackend.__new__(MODULE.AmbiBeatBackend)
    backend.track_info = {"art": ""}
    backend._mpris_players = {
        "other": {
            "track": {"title": "Title", "artist": "Artist", "album": "Album"},
            "art_url": "file:///tmp/mpris-cover.jpg",
        }
    }
    fetched = []
    backend._fetch_album_art = lambda *args: fetched.append(args)
    backend._sync_idle_inhibition = lambda: None
    backend._last_art_key = ""

    backend._publish_track(
        {
            "title": "Title",
            "artist": "Artist",
            "album": "Album",
            "status": "playing",
        },
        "mpris:org.mpris.MediaPlayer2.player",
        "https://example.com/device-art.jpg",
    )

    assert len(fetched) == 1
    assert fetched[0][3] == "https://example.com/device-art.jpg"
    assert fetched[0][5] == ""


def test_local_player_art_is_used_before_remote_services():
    backend = MODULE.AmbiBeatBackend.__new__(MODULE.AmbiBeatBackend)
    backend.track_info = {"art": ""}
    backend._last_art_key = ""

    with tempfile.NamedTemporaryFile(suffix=".jpg") as device_art:
        art_url = f"file://{device_art.name}"
        backend._fetch_album_art("Artist", "Title", "Album", art_url, "mpris:org.mpris.MediaPlayer2.player")

    assert backend.track_info["art"] == art_url


def test_normalize_device_art_url_accepts_absolute_path():
    assert MODULE._normalize_device_art_url("/tmp/device-cover.jpg") == (
        "file:///tmp/device-cover.jpg"
    )


def test_publishing_unchanged_track_preserves_loaded_artwork():
    backend = MODULE.AmbiBeatBackend.__new__(MODULE.AmbiBeatBackend)
    backend.running = False
    source_id = "mpris:org.mpris.MediaPlayer2.player"
    backend.track_info = {"art": "file:///tmp/cover.jpg"}
    backend._mpris_players = {}
    backend._last_art_key = MODULE._track_art_key(
        "Artist", "Track", "Album", "", source_id, ""
    )

    backend._publish_track(
        {
            "title": "Track",
            "artist": "Artist",
            "album": "Album",
            "status": "playing",
            "device": "Player",
            "art": "",
        },
        source_id,
        "",
    )

    assert backend.track_info["art"] == "file:///tmp/cover.jpg"


def test_find_matching_album_cover_recognizes_lady_antebellum_rebrand():
    hits = [
        {
            "title": "Need You Now",
            "artist": {"name": "Lady A"},
            "album": {"title": "Need You Now", "cover_big": "matching-cover"},
        }
    ]

    cover = MODULE._find_matching_album_cover(
        hits, "Lady Antebellum", "Need You Now", "Need You Now"
    )

    assert cover == "matching-cover"


def test_find_matching_mpris_artwork_requires_matching_track():
    players = [
        {
            "track": {"title": "Same Title", "artist": "Different Artist", "album": "Album"},
            "art_url": "file:///wrong.jpg",
        },
        {
            "track": {"title": "Same Title", "artist": "Right Artist", "album": "Album"},
            "art_url": "file:///right.jpg",
        },
    ]

    artwork = MODULE._find_matching_mpris_artwork(
        players, "Right Artist", "Same Title", "Album"
    )

    assert artwork == "file:///right.jpg"


def test_find_matching_mpris_artwork_uses_same_browser_process_art():
    players = [
        {
            "id": "mpris:org.mpris.MediaPlayer2.plasma-browser-integration",
            "bus_name": "org.mpris.MediaPlayer2.plasma-browser-integration",
            "pid": "297587",
            "track": {
                "title": "The Way It Was",
                "artist": "The Killers",
                "album": "Battle Born",
                "device": "Chromium",
            },
            "art_url": "",
        },
        {
            "id": "mpris:org.mpris.MediaPlayer2.chromium.instance297587",
            "bus_name": "org.mpris.MediaPlayer2.chromium.instance297587",
            "pid": "",
            "track": {
                "title": "The Way It Was | YouTube Music",
                "artist": "Chromium",
                "album": "",
                "device": "Chromium",
            },
            "art_url": "file:///tmp/player-art.png",
        },
    ]

    artwork = MODULE._find_matching_mpris_artwork(
        players,
        "The Killers",
        "The Way It Was",
        "Battle Born",
        players[0]["id"],
    )

    assert artwork == "file:///tmp/player-art.png"


def test_image_content_type_detects_png_without_filename_extension():
    assert MODULE._image_content_type(
        b"\x89PNG\r\n\x1a\n", "/tmp/media-player-art"
    ) == "image/png"


def test_track_art_key_changes_when_artist_changes_for_same_title():
    first = MODULE._track_art_key("Artist One", "Same Title", "Album", "")
    second = MODULE._track_art_key("Artist Two", "Same Title", "Album", "")

    assert first != second


def test_track_art_key_changes_when_source_changes():
    bluetooth = MODULE._track_art_key("Artist", "Title", "Album", "", "bluetooth")
    player = MODULE._track_art_key(
        "Artist", "Title", "Album", "", "mpris:org.mpris.MediaPlayer2.player"
    )

    assert bluetooth != player


def test_visualization_analysis_updates_twice_as_fast():
    assert MODULE.ANALYSIS_INTERVAL == 0.05


def test_installer_creates_native_app_launcher():
    install_source = (MODULE_PATH.parents[2] / "scripts" / "install.sh").read_text(
        encoding="utf-8"
    )

    assert "ambibeat-native" in install_source
    assert 'exec "$BIN_DIR/ambibeat-qt" "\\$@"' in install_source
    assert "Exec=$BIN_DIR/ambibeat-qt" in install_source
    assert "chromium" not in install_source
    assert "StartupWMClass=AmbiBeat" in install_source
    assert "Icon=$ASSET_DIR/ambibeat-icon.png" in install_source


def test_installer_migrates_browser_launcher_to_qt(tmp_path):
    root = MODULE_PATH.parents[2]
    project = tmp_path / "project"
    scripts = project / "scripts"
    assets = project / "src" / "ambibeat" / "assets"
    venv_bin = project / ".venv" / "bin"
    tools = tmp_path / "tools"
    home = tmp_path / "home"
    for path in (scripts, assets, venv_bin, tools, home):
        path.mkdir(parents=True)
    shutil.copyfile(root / "scripts" / "install.sh", scripts / "install.sh")
    for name in ("ambibeat-icon.png", "ambibeat-icon-32.png", "ambibeat-icon-192.png"):
        shutil.copyfile(root / "src" / "ambibeat" / "assets" / name, assets / name)
    (project / ".venv" / "pyvenv.cfg").write_text("include-system-site-packages = true\n")

    def executable(path, text):
        path.write_text("#!/bin/sh\n" + text)
        path.chmod(0o755)

    executable(tools / "python3", "exit 0\n")
    executable(tools / "systemctl",
               'echo "$*" >> "$HOME/systemctl.log"\nexit 1\n')
    executable(tools / "gtk-update-icon-cache", "exit 0\n")
    executable(venv_bin / "python", 'if [ "$3" = show ]; then exit 1; fi\nexit 0\n')
    executable(venv_bin / "ambibeat", "exit 0\n")
    executable(venv_bin / "ambibeat-qt", 'printf "%s\\n" "$@"\n')
    bin_dir = home / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "ambibeat-native").write_text("obsolete browser launcher\n")
    env = {**os.environ, "HOME": str(home), "PATH": f"{tools}:{os.environ['PATH']}",
           "XDG_BIN_HOME": str(bin_dir), "XDG_DATA_HOME": str(home / ".local" / "share"),
           "XDG_CONFIG_HOME": str(home / ".config")}
    subprocess.run(["bash", str(scripts / "install.sh")], env=env, check=True,
                   capture_output=True, text=True)
    desktop = (home / ".local" / "share" / "applications" / "ambibeat.desktop").read_text()
    assert f"Exec={bin_dir}/ambibeat-qt" in desktop
    result = subprocess.run([str(bin_dir / "ambibeat-native"), "--test", "two words"],
                            env=env, check=True, capture_output=True, text=True)
    assert result.stdout.splitlines() == ["--test", "two words"]
    assert not (home / "systemctl.log").exists()


def test_qt_frontend_excludes_removed_modes():
    project_root = MODULE_PATH.parents[2]
    qt_source = (project_root / "src" / "ambibeat" / "qt_frontend.py").read_text(
        encoding="utf-8"
    )
    pyproject = (project_root / "pyproject.toml").read_text(encoding="utf-8")
    install_source = (project_root / "scripts" / "install.sh").read_text(
        encoding="utf-8"
    )

    assert "ambibeat-qt" in pyproject
    assert "ambibeat-qt.desktop" in install_source
    assert "(0, \"Bars\")" in qt_source
    assert "(1, \"Scope\")" in qt_source
    assert "(4, \"Time Vortex\")" in qt_source
    assert "(5, \"No visualisation\")" in qt_source
    assert "Christmas" not in qt_source
    assert "Album Halo" not in qt_source
    assert "Effortless Swirl" not in qt_source
    assert "Modern" not in qt_source


def test_qt_window_does_not_reference_removed_network_manager():
    project_root = MODULE_PATH.parents[2]
    qt_source = (
        project_root / "src" / "ambibeat" / "qt_frontend.py"
    ).read_text(encoding="utf-8")

    assert "QNetworkAccessManager" not in qt_source
    assert "AmbiBeatQtWindow" in qt_source


def test_qt_window_has_playback_controls():
    project_root = MODULE_PATH.parents[2]
    qt_source = (
        project_root / "src" / "ambibeat" / "qt_frontend.py"
    ).read_text(encoding="utf-8")

    for text in (
        'self.media_control("Previous")',
        'self.media_control("Next")',
        'self.media_control("Play")',
        'self.media_control("Pause")',
        'self.panel_request("system-volume", value)',
        "SidePanel",
        "def keyPressEvent",
        "MovingEdgeBorder",
        "self.source_button = QToolButton",
        "open_volume_menu",
        "open_mode_menu",
    ):
        assert text in qt_source


def test_qt_icons_are_custom_and_controls_hide():
    project_root = MODULE_PATH.parents[2]
    qt_source = (
        project_root / "src" / "ambibeat" / "qt_frontend.py"
    ).read_text(encoding="utf-8")

    assert "def line_icon(self, kind)" in qt_source
    assert 'kind == "previous"' in qt_source
    assert 'kind == "pause"' in qt_source
    assert 'kind == "settings"' in qt_source
    assert "QPolygonF" in qt_source
    assert "border: 1px solid rgba(255,255,255,102)" in qt_source
    assert "def show_controls(self)" in qt_source
    assert "def hide_controls(self)" in qt_source
    assert "2500" in qt_source
    assert "controls_effect" in qt_source


def test_qt_and_backend_use_application_icon_fallback():
    project_root = MODULE_PATH.parents[2]
    qt_source = (project_root / "src" / "ambibeat" / "qt_frontend.py").read_text(
        encoding="utf-8"
    )

    assert "self.fallback_pixmap = QPixmap(icon_path)" in qt_source
    assert "self.art_pixmap or self.fallback_pixmap" in qt_source
    assert "APP_ICON_PATH" in qt_source
    assert hasattr(MODULE, "APP_ICON_PATH")
    assert MODULE.APP_ICON_PATH.endswith("assets/ambibeat-icon.png")
    assert hasattr(MODULE.AmbiBeatBackend, "_set_app_icon_fallback")


def test_capture_loop_skips_work_when_paused():
    source = MODULE_PATH.read_text(encoding="utf-8")

    assert 'if self.track_info.get("status") != "playing":' in source
    assert "time.sleep(0.1)" in source


def test_mpris_track_info_reads_artist_album_status_and_art_url():
    track, art_url, pid = MODULE._mpris_track_info(
        "Music Player",
        {
            "PlaybackStatus": "Playing",
            "Metadata": {
                "kde:pid": 1234,
                "xesam:title": "Current Track",
                "xesam:artist": ["Artist One", "Artist Two"],
                "xesam:album": "Current Album",
                "mpris:artUrl": "file:///tmp/cover.jpg",
            },
        },
    )

    assert track["title"] == "Current Track"
    assert track["artist"] == "Artist One, Artist Two"
    assert track["album"] == "Current Album"
    assert track["status"] == "playing"
    assert art_url == "file:///tmp/cover.jpg"
    assert pid == "1234"


def test_mpris_timeline_progress_and_seek(tmp_path, monkeypatch):
    monkeypatch.setattr(MODULE, "APP_SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setattr(MODULE, "LEGACY_APP_SETTINGS_PATH", str(tmp_path / "legacy.json"))
    monkeypatch.setattr(MODULE, "ART_CACHE_DIR", str(tmp_path / "art"))
    monkeypatch.setattr(MODULE.AmbiBeatBackend, "_refresh_system_volume", lambda self: True)
    bus = Mock()
    bus.list_names.return_value = []
    backend = MODULE.AmbiBeatBackend(bus)
    try:
        monkeypatch.setattr(MODULE.time, "monotonic", lambda: 10.0)
        track, _, _ = MODULE._mpris_track_info("Player", {
            "PlaybackStatus": "Playing", "Position": 20_000_000, "Rate": 2.0, "CanSeek": True,
            "Metadata": {"mpris:trackid": "/track/1", "mpris:length": 100_000_000},
        })
        backend.track_info = track
        backend.selected_source = "mpris:org.mpris.MediaPlayer2.test"
        backend._mpris_players = {"org.mpris.MediaPlayer2.test": {
            "id": backend.selected_source, "label": "Player", "track": track,
        }}
        monkeypatch.setattr(MODULE.time, "monotonic", lambda: 15.0)
        assert backend.get_state()["track"]["position"] == 30_000_000
        track["status"] = "paused"
        assert backend.get_state()["track"]["position"] == 20_000_000
        player = Mock()
        monkeypatch.setattr(MODULE.dbus, "Interface", lambda *_args: player)
        backend.seek_track(backend.selected_source, "/track/1", 50_000_000)
        player.SetPosition.assert_called_once_with("/track/1", 50_000_000)
        assert backend.get_state()["track"]["position"] == 50_000_000
        for source, track_id, position in (
            ("mpris:other", "/track/1", 1), (backend.selected_source, "/track/2", 1),
            (backend.selected_source, "/track/1", 100_000_001),
        ):
            with pytest.raises(ValueError):
                backend.seek_track(source, track_id, position)
        assert player.SetPosition.call_count == 1
    finally:
        backend.stop()


@pytest.mark.parametrize("properties", [
    {"Position": 0, "CanSeek": False, "Metadata": {"mpris:trackid": "/track/1", "mpris:length": 1}},
    {"Position": 0, "CanSeek": True, "Metadata": {"mpris:trackid": "/track/1"}},
    {"CanSeek": True, "Metadata": {"mpris:trackid": "/track/1", "mpris:length": 1}},
    {"Position": 0, "CanSeek": True, "Metadata": {"mpris:length": 1}},
])
def test_mpris_timeline_unavailable_without_required_metadata(properties):
    track, _, _ = MODULE._mpris_track_info("Player", properties)
    assert not track["canSeek"]
