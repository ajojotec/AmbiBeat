import copy
import json
from types import SimpleNamespace

import pytest

from ambibeat.source_volume import SourceVolume


SOURCE = "mpris:org.mpris.MediaPlayer2.player"


def stream(index, pid, value=32768):
    return {
        "index": index,
        "channel_map": "front-left,front-right",
        "volume": {"front-left": {"value": value}, "front-right": {"value": value}},
        "properties": {"application.process.id": str(pid), "object.serial": str(index)},
    }


@pytest.fixture
def audio(monkeypatch):
    streams = [stream(10, 123), stream(11, 999)]
    saved = []
    changes = []
    manager = SourceVolume({SOURCE: 2.0}, {}, lambda: saved.append(copy.deepcopy(manager.journal)),
                           lambda source: {"123"} if source == SOURCE else set())
    monkeypatch.setattr(manager, "_audio_state", lambda: ("server-cookie", streams))

    def set_volume(index, values):
        changes.append((int(index), values.copy()))
        target = next(item for item in streams if item["index"] == int(index))
        for channel, value in values.items():
            target["volume"][channel]["value"] = value
    monkeypatch.setattr(manager, "_set_volume", set_volume)
    return manager, streams, changes, saved


def test_boost_targets_selected_stream_and_restores_on_close(audio):
    manager, streams, changes, saved = audio
    manager.activate(SOURCE)
    assert changes == [(10, {"front-left": 65536, "front-right": 65536})]
    assert saved[0]["10"]["base"]["front-left"] == 32768
    assert streams[1]["volume"]["front-left"]["value"] == 32768
    manager.deactivate()
    assert changes[-1] == (10, {"front-left": 32768, "front-right": 32768})
    assert manager.journal == {}


def test_boost_change_does_not_compound_and_preserves_user_volume_changes(audio):
    manager, streams, changes, _ = audio
    manager.activate(SOURCE)
    manager.gains[SOURCE] = 3.0
    manager.apply(SOURCE)
    assert changes[-1][1]["front-left"] == 98304
    for channel in streams[0]["volume"].values():
        channel["value"] = 49152
    manager.refresh(SOURCE)
    manager.deactivate()
    assert changes[-1][1]["front-left"] == 16384


def test_recovery_restores_persisted_boost_and_does_not_touch_other_server(audio, monkeypatch):
    manager, streams, changes, saved = audio
    manager.activate(SOURCE)
    recovered = SourceVolume({}, copy.deepcopy(saved[-1]), lambda: None, lambda _: set())
    monkeypatch.setattr(recovered, "_audio_state", lambda: ("server-cookie", streams))
    monkeypatch.setattr(recovered, "_set_volume", manager._set_volume)
    recovered.restore()
    assert changes[-1][1]["front-left"] == 32768
    assert recovered.journal == {}

    old_journal = copy.deepcopy(saved[-2])
    recovered = SourceVolume({}, old_journal, lambda: None, lambda _: set())
    monkeypatch.setattr(recovered, "_audio_state", lambda: ("different-cookie", streams))
    monkeypatch.setattr(recovered, "_set_volume", manager._set_volume)
    before = len(changes)
    recovered.restore()
    assert len(changes) == before


def test_recovery_handles_crash_before_volume_change(audio, monkeypatch):
    manager, streams, changes, saved = audio
    monkeypatch.setattr(manager, "_set_volume", lambda *_: (_ for _ in ()).throw(OSError("failed")))
    with pytest.raises(OSError):
        manager.activate(SOURCE)
    recovered = SourceVolume({}, copy.deepcopy(saved[-1]), lambda: None, lambda _: set())
    monkeypatch.setattr(recovered, "_audio_state", lambda: ("server-cookie", streams))
    monkeypatch.setattr(recovered, "_set_volume", lambda *args: changes.append(args))
    recovered.restore()
    assert changes == []


def test_recovery_after_interrupted_restore_does_not_remove_boost_twice(audio, monkeypatch):
    manager, streams, changes, saved = audio
    manager.activate(SOURCE)
    for channel in streams[0]["volume"].values():
        channel["value"] = 49152
    original_set = manager._set_volume
    def interrupted(index, values):
        original_set(index, values)
        raise OSError("interrupted after volume change")
    monkeypatch.setattr(manager, "_set_volume", interrupted)
    with pytest.raises(OSError):
        manager.deactivate()
    recovered = SourceVolume({}, copy.deepcopy(saved[-1]), lambda: None, lambda _: set())
    monkeypatch.setattr(recovered, "_audio_state", lambda: ("server-cookie", streams))
    monkeypatch.setattr(recovered, "_set_volume", original_set)
    recovered.restore()
    assert streams[0]["volume"]["front-left"]["value"] == 24576


def test_missing_stream_does_not_adjust_unrelated_audio(audio):
    manager, _, changes, _ = audio
    manager.player_pids = lambda source: {"456"}
    with pytest.raises(RuntimeError, match="No audio stream"):
        manager.activate(SOURCE)
    assert changes == []


def test_failed_recovery_prevents_reapplying_boost(audio, monkeypatch):
    manager, _, _, _ = audio
    manager.activate(SOURCE)
    manager.active = False
    monkeypatch.setattr(manager, "_set_volume", lambda *_: (_ for _ in ()).throw(OSError("restore failed")))
    with pytest.raises(OSError, match="restore failed"):
        manager.activate(SOURCE)
    assert not manager.active
    assert manager.journal


def test_legacy_bluetooth_volume_journal_is_still_restored(audio):
    manager, streams, changes, _ = audio
    streams[0]["properties"] = {
        "device.api": "bluez5", "api.bluez5.profile": "a2dp-source",
        "api.bluez5.address": "00:11:22:33:44:55", "object.serial": "10",
    }
    for channel in streams[0]["volume"].values():
        channel["value"] = 49152
    manager.journal["10"] = {
        "cookie": "server-cookie", "fingerprint": {
            "object.serial": "10", "application.process.id": "", "application.name": "",
            "node.name": "", "api.bluez5.address": "00:11:22:33:44:55",
            "api.bluez5.profile": "a2dp-source",
        },
        "source": "bluetooth", "base": {"front-left": 32768, "front-right": 32768},
        "target": {"front-left": 49152, "front-right": 49152}, "gain": 1.5,
    }
    manager.restore()
    assert changes == [(10, {"front-left": 32768, "front-right": 32768})]
    assert not manager.journal


def test_audio_commands_use_server_identity_and_target_stream(monkeypatch):
    streams = [stream(10, 123)]
    calls = []
    def run(arguments, **kwargs):
        calls.append(arguments)
        if arguments[-1] == "info":
            return SimpleNamespace(stdout=json.dumps({"cookie": "server-cookie"}))
        if arguments[-1] == "sink-inputs":
            return SimpleNamespace(stdout=json.dumps(streams))
        return SimpleNamespace(stdout="")
    monkeypatch.setattr("ambibeat.source_volume.subprocess.run", run)
    manager = SourceVolume({SOURCE: 2.0}, {}, lambda: None, lambda _: {"123"})
    manager.activate(SOURCE)
    assert calls == [
        ["pactl", "-f", "json", "info"],
        ["pactl", "-f", "json", "list", "sink-inputs"],
        ["pactl", "set-sink-input-volume", "10", "100.000%", "100.000%"],
    ]


