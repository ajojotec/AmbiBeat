"""Manage per-source boost and journal original volumes for recovery."""

import json
import subprocess
import threading


class StreamNotFoundError(RuntimeError):
    pass


class SourceVolume:
    """Apply selected-source gain and recover safely after interruption."""

    def __init__(self, gains, journal, save, player_pids):
        self.gains = gains
        self.journal = journal
        self.save = save
        self.player_pids = player_pids
        self.active = False
        self.lock = threading.RLock()

    def _audio_state(self):
        def query(what):
            return json.loads(subprocess.run(
                ["pactl", "-f", "json", *what], check=True,
                capture_output=True, text=True,
            ).stdout)
        info = query(["info"])
        cookie = info.get("cookie")
        if not isinstance(cookie, str) or not cookie:
            raise RuntimeError("Audio server did not provide an identity")
        return cookie, query(["list", "sink-inputs"])

    @staticmethod
    def _fingerprint(stream):
        properties = stream.get("properties") or {}
        return {key: properties.get(key, "") for key in (
            "object.serial", "application.process.id", "application.name",
            "node.name", "api.bluez5.address", "api.bluez5.profile",
        )}

    @staticmethod
    def _channels(stream):
        volumes = stream.get("volume") or {}
        channel_map = stream.get("channel_map", "")
        names = channel_map.split(",") if channel_map else list(volumes)
        if not names or any(name not in volumes or "value" not in volumes[name] for name in names):
            raise RuntimeError("Audio stream has no readable channel volumes")
        return {name: int(volumes[name]["value"]) for name in names}

    @staticmethod
    def _set_volume(index, values):
        subprocess.run(
            ["pactl", "set-sink-input-volume", str(index),
             *(f"{value * 100 / 65536:.3f}%" for value in values.values())],
            check=True, capture_output=True, text=True,
        )

    @staticmethod
    def _same_volume(left, right):
        return (list(left) == list(right)
                and all(abs(left[channel] - right[channel]) <= 1 for channel in left))

    def _restore_locked(self, cookie, streams):
        streams_by_index = {str(stream.get("index")): stream for stream in streams}
        for key, entry in list(self.journal.items()):
            stream = streams_by_index.get(key)
            if (entry["cookie"] == cookie and stream is not None
                    and self._fingerprint(stream) == entry["fingerprint"]):
                current = self._channels(stream)
                if list(current) != list(entry["base"]):
                    raise RuntimeError(f"Audio channels changed for stream {key}")
                pending = entry.get("pending")
                if self._same_volume(current, entry["base"]):
                    base = entry["base"]
                elif pending and self._same_volume(current, pending["target"]):
                    base = entry["base"]
                elif not self._same_volume(current, entry["target"]):
                    base = {channel: round(value / entry["gain"])
                            for channel, value in current.items()}
                else:
                    base = entry["base"]
                if not self._same_volume(current, base):
                    entry["base"] = base
                    entry["target"] = current
                    entry["pending"] = {"gain": 1.0, "target": base}
                    self.save()
                    self._set_volume(key, base)
            del self.journal[key]
            self.save()

    def restore(self):
        with self.lock:
            if self.journal:
                self._restore_locked(*self._audio_state())

    def apply(self, source):
        with self.lock:
            gain = self.gains.get(source, 1.0)
            if (not self.active or not source or gain == 1.0) and not self.journal:
                return
            cookie, streams = self._audio_state()
            if not self.active or not source or gain == 1.0:
                self._restore_locked(cookie, streams)
                return
            pids = self.player_pids(source)
            matches = [stream for stream in streams
                       if str((stream.get("properties") or {}).get("application.process.id", "")) in pids]
            current_indices = {str(stream["index"]) for stream in matches}
            if any(key not in current_indices or entry["source"] != source
                   for key, entry in self.journal.items()):
                self._restore_locked(cookie, streams)
            for stream in matches:
                key = str(stream["index"])
                current = self._channels(stream)
                fingerprint = self._fingerprint(stream)
                entry = self.journal.get(key)
                if entry and (entry["cookie"] != cookie or entry["fingerprint"] != fingerprint):
                    del self.journal[key]
                    self.save()
                    entry = None
                if entry:
                    if list(current) != list(entry["base"]):
                        raise RuntimeError(f"Audio channels changed for stream {key}")
                    pending = entry.pop("pending", None)
                    if pending and self._same_volume(current, pending["target"]):
                        entry["gain"] = pending["gain"]
                        entry["target"] = pending["target"]
                        self.save()
                    if not self._same_volume(current, entry["target"]):
                        entry["base"] = {name: round(value / entry["gain"])
                                         for name, value in current.items()}
                else:
                    entry = {"cookie": cookie, "fingerprint": fingerprint,
                             "source": source, "base": current.copy(),
                             "gain": 1.0, "target": current.copy()}
                    self.journal[key] = entry
                target = {name: round(value * gain) for name, value in entry["base"].items()}
                if not self._same_volume(current, target):
                    entry["pending"] = {"gain": gain, "target": target}
                    self.save()
                    self._set_volume(key, target)
                    entry.pop("pending")
                if entry["gain"] != gain or entry["target"] != target:
                    entry["gain"] = gain
                    entry["target"] = target
                    self.save()
            if not matches:
                raise StreamNotFoundError("No audio stream could be identified for the selected source")

    def activate(self, source):
        with self.lock:
            self.restore()
            self.active = True
            self.apply(source)

    def deactivate(self):
        with self.lock:
            self.active = False
            self.restore()

    def refresh(self, source):
        with self.lock:
            if self.active:
                self.apply(source)
            else:
                self.restore()
