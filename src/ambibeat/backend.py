"""Audio capture, playback sources, artwork lookup, and application state."""

from difflib import SequenceMatcher
import hashlib
import json
import math
import mimetypes
import os
import random
import re
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
import numpy as np
import dbus
import dbus.mainloop.glib
from gi.repository import GLib
from ambibeat.audio_analysis import calculate_visualization
from ambibeat.source_volume import SourceVolume, StreamNotFoundError

ART_CACHE_DIR = "/tmp/ambibeat-art"
APP_ICON_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "assets",
    "ambibeat-icon.png",
)
SAMPLE_RATES = (22050, 32000, 44100, 48000, 96000)
EDGE_SWIRL_SLOWDOWN_MIN = 1
EDGE_SWIRL_SLOWDOWN_MAX = 20
EDGE_SWIRL_DIRECTIONS = ("clockwise", "counter-clockwise", "random")
EDGE_COLOR_INTENSITY_MIN = 0
EDGE_COLOR_INTENSITY_MAX = 100
EDGE_COLOR_INTENSITY_DEFAULT = 100
EDGE_FADE_MIN = 0
EDGE_FADE_MAX = 100
EDGE_FADE_DEFAULT = 50
EDGE_DEPTH_MIN = 25
EDGE_DEPTH_MAX = 200
EDGE_DEPTH_DEFAULT = 100
EDGE_OPACITY_MIN = 0
EDGE_OPACITY_MAX = 100
EDGE_OPACITY_DEFAULT = 70
EDGE_COLOR_COUNT_MIN = 2
EDGE_COLOR_COUNT_MAX = 8
EDGE_COLOR_COUNT_DEFAULT = 8
ALBUM_ART_OPACITY_MIN = 0
ALBUM_ART_OPACITY_MAX = 100
ALBUM_ART_OPACITY_DEFAULT = 22
ARTWORK_MODES = ("album", "logo", "none")
SCOPE_LINE_WIDTH_MIN = 25
SCOPE_LINE_WIDTH_MAX = 300
SCOPE_LINE_WIDTH_DEFAULT = 100
SOURCE_PROMPT_SECONDS_MIN = 3
SOURCE_PROMPT_SECONDS_MAX = 60
SOURCE_PROMPT_SECONDS_DEFAULT = 10
LIGHTING_MODES = ("border", "disco", "none")
LIGHTING_COLOR_MODES = ("artwork", "rainbow", "christmas", "gelato")
CUSTOM_PALETTE_LIMIT = 8
DISCO_LIGHT_STYLES = ("glow", "fairy", "bulb", "star")
DISCO_WIRE_COLORS = ("green", "theme", "none")
DISCO_LIGHT_COUNT_MIN = 3
DISCO_LIGHT_COUNT_MAX = 40
DISCO_LIGHT_COUNT_DEFAULT = 16
DISCO_LIGHT_SIZE_MIN = 40
DISCO_LIGHT_SIZE_MAX = 200
DISCO_LIGHT_SIZE_DEFAULT = 100
ANALYSIS_INTERVAL = 0.05
MPRIS_PREFIX = "org.mpris.MediaPlayer2."
MPRIS_OBJECT_PATH = "/org/mpris/MediaPlayer2"
MPRIS_PLAYER_INTERFACE = "org.mpris.MediaPlayer2.Player"
CONFIG_HOME = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
    os.path.expanduser("~"), ".config"
)
APP_SETTINGS_PATH = os.path.join(
    CONFIG_HOME, "ambibeat", "settings.json"
)
LEGACY_APP_SETTINGS_PATH = os.path.join(
    CONFIG_HOME, "bt-audio-companion", "settings.json"
)
_VERSION_SUFFIXES = {
    "acoustic", "anniversary", "bonus", "clean", "deluxe", "edition",
    "edit", "explicit", "extended", "live", "mix", "mono", "radio",
    "remaster", "remastered", "single", "stereo", "version",
}
_ARTIST_ALIAS_GROUPS = (
    frozenset({"lady antebellum", "lady a"}),
)


def normalize_custom_palettes(palettes):
    if not isinstance(palettes, list) or len(palettes) > CUSTOM_PALETTE_LIMIT:
        raise ValueError("A maximum of eight custom palettes is allowed")
    result = []
    slots, names = set(), set()
    for palette in palettes:
        if not isinstance(palette, dict):
            raise ValueError("Invalid custom palette")
        slot, name = palette.get("slot"), palette.get("name")
        colors, enabled = palette.get("colors"), palette.get("enabled")
        if (not isinstance(slot, int) or isinstance(slot, bool) or not 1 <= slot <= CUSTOM_PALETTE_LIMIT
                or slot in slots):
            raise ValueError("Invalid or duplicate palette slot")
        if not isinstance(name, str) or not name.strip() or name.strip().casefold() in names:
            raise ValueError("Palette names must be non-empty and unique")
        if not isinstance(enabled, bool):
            raise ValueError("Invalid palette enabled setting")
        if not isinstance(colors, list) or not 2 <= len(colors) <= 8:
            raise ValueError("A palette must contain two to eight colours")
        normalized = []
        for color in colors:
            if not isinstance(color, str) or not re.fullmatch(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})", color):
                raise ValueError("Colours must be HTML hex codes such as #ff8800")
            value = color[1:].lower()
            normalized.append("#" + ("".join(char * 2 for char in value) if len(value) == 3 else value))
        name = name.strip()
        slots.add(slot)
        names.add(name.casefold())
        result.append({"slot": slot, "name": name, "colors": normalized, "enabled": enabled})
    return sorted(result, key=lambda palette: palette["slot"])


def custom_palette_for_mode(palettes, mode):
    return next((palette for palette in palettes
                 if palette["enabled"] and mode == f'custom-{palette["slot"]}'), None)


def _load_app_settings():
    loaded_from_legacy_path = False
    for settings_path in (APP_SETTINGS_PATH, LEGACY_APP_SETTINGS_PATH):
        try:
            with open(settings_path, encoding="utf-8") as settings_file:
                data = json.load(settings_file)
        except FileNotFoundError:
            continue
        except (OSError, json.JSONDecodeError) as error:
            print(f"Application settings could not be loaded: {error}", flush=True)
            return {}
        loaded_from_legacy_path = settings_path == LEGACY_APP_SETTINGS_PATH
        break
    else:
        return {}

    if not isinstance(data, dict) or data.get("version", 1) != 1:
        print("Application settings have an unsupported format; using defaults", flush=True)
        return {}

    settings = {}
    warning_acknowledged = data.get("photosensitivity_warning_acknowledged")
    if isinstance(warning_acknowledged, bool):
        settings["photosensitivity_warning_acknowledged"] = warning_acknowledged
    elif "photosensitivity_warning_acknowledged" in data:
        print("Ignoring invalid photosensitivity warning acknowledgement", flush=True)

    sample_rate = data.get("sample_rate")
    if sample_rate in SAMPLE_RATES and isinstance(sample_rate, int):
        settings["sample_rate"] = sample_rate
    elif "sample_rate" in data:
        print("Ignoring invalid saved sample rate", flush=True)

    prompt_seconds = data.get("source_prompt_seconds")
    if (isinstance(prompt_seconds, int) and not isinstance(prompt_seconds, bool)
            and SOURCE_PROMPT_SECONDS_MIN <= prompt_seconds <= SOURCE_PROMPT_SECONDS_MAX):
        settings["source_prompt_seconds"] = prompt_seconds
    elif "source_prompt_seconds" in data:
        print("Ignoring invalid saved source prompt duration", flush=True)

    prompt_enabled = data.get("source_prompt_enabled")
    if isinstance(prompt_enabled, bool):
        settings["source_prompt_enabled"] = prompt_enabled
    elif "source_prompt_enabled" in data:
        print("Ignoring invalid saved source prompt setting", flush=True)

    raw_gains = data.get("source_gains", {})
    gains = {}
    if isinstance(raw_gains, dict):
        for source_id, raw_gain in raw_gains.items():
            if source_id == "bluetooth":
                continue
            if (isinstance(source_id, str) and source_id
                    and isinstance(raw_gain, (int, float)) and not isinstance(raw_gain, bool)
                    and math.isfinite(raw_gain) and 1.0 <= raw_gain <= 4.0):
                gains[source_id] = float(raw_gain)
            else:
                print("Ignoring invalid saved source boost", flush=True)
    else:
        print("Ignoring invalid saved source boosts", flush=True)
    if gains or "source_gains" in data:
        settings["source_gains"] = gains

    journal = data.get("volume_journal", {})
    if isinstance(journal, dict):
        valid = {}
        for key, entry in journal.items():
            def valid_channels(channels):
                return (isinstance(channels, dict) and bool(channels)
                        and all(isinstance(k, str) and isinstance(v, int)
                                and not isinstance(v, bool) and v >= 0
                                for k, v in channels.items()))
            if (isinstance(key, str) and key.isdecimal() and isinstance(entry, dict)
                    and isinstance(entry.get("cookie"), str) and entry["cookie"]
                    and isinstance(entry.get("fingerprint"), dict)
                    and isinstance(entry.get("source"), str)
                    and valid_channels(entry.get("base"))
                    and valid_channels(entry.get("target"))
                    and list(entry["base"]) == list(entry["target"])
                    and isinstance(entry.get("gain"), (int, float))
                    and not isinstance(entry["gain"], bool)
                    and math.isfinite(entry["gain"]) and 1 <= entry["gain"] <= 4
                    and ("pending" not in entry or (
                        isinstance(entry["pending"], dict)
                        and valid_channels(entry["pending"].get("target"))
                        and list(entry["base"]) == list(entry["pending"]["target"])
                        and isinstance(entry["pending"].get("gain"), (int, float))
                        and not isinstance(entry["pending"]["gain"], bool)
                        and math.isfinite(entry["pending"]["gain"])
                        and 1 <= entry["pending"]["gain"] <= 4))):
                valid[key] = entry
            else:
                print("Ignoring invalid saved volume recovery entry", flush=True)
        if valid or "volume_journal" in data:
            settings["volume_journal"] = valid
    else:
        print("Ignoring invalid saved volume recovery journal", flush=True)

    source = data.get("selected_source")
    if source == "bluetooth":
        settings["selected_source"] = ""
    elif (
        source == ""
        or (
            isinstance(source, str)
            and source.startswith(f"mpris:{MPRIS_PREFIX}")
            and len(source) > len(f"mpris:{MPRIS_PREFIX}")
        )
    ):
        settings["selected_source"] = source
    elif "selected_source" in data:
        print("Ignoring invalid saved playback source", flush=True)

    last_instances = data.get("last_source_instances", {})
    if isinstance(last_instances, dict):
        valid_instances = {}
        for identity, source_id in last_instances.items():
            if (isinstance(identity, str) and identity and isinstance(source_id, str)
                    and source_id.startswith(f"mpris:{MPRIS_PREFIX}")
                    and len(source_id) > len(f"mpris:{MPRIS_PREFIX}")):
                valid_instances[identity] = source_id
            else:
                print("Ignoring invalid saved playback instance", flush=True)
        if valid_instances or "last_source_instances" in data:
            settings["last_source_instances"] = valid_instances
    else:
        print("Ignoring invalid saved playback instances", flush=True)

    visualizer_mode = data.get("visualizer_mode")
    if (
        isinstance(visualizer_mode, int)
        and not isinstance(visualizer_mode, bool)
        and 0 <= visualizer_mode < 9
    ):
        if visualizer_mode in (3, 7):
            settings["visualizer_mode"] = 0
        elif visualizer_mode == 6:
            settings["visualizer_mode"] = 4
        elif visualizer_mode == 8:
            settings["visualizer_mode"] = 5
        else:
            settings["visualizer_mode"] = visualizer_mode
    elif "visualizer_mode" in data:
        print("Ignoring invalid saved visualizer mode", flush=True)

    scope_zero_line = data.get("scope_zero_line")
    if isinstance(scope_zero_line, bool):
        settings["scope_zero_line"] = scope_zero_line
    elif "scope_zero_line" in data:
        print("Ignoring invalid saved scope zero line setting", flush=True)

    scope_line_width = data.get("scope_line_width")
    if (isinstance(scope_line_width, int) and not isinstance(scope_line_width, bool)
            and SCOPE_LINE_WIDTH_MIN <= scope_line_width <= SCOPE_LINE_WIDTH_MAX):
        settings["scope_line_width"] = scope_line_width
    elif "scope_line_width" in data:
        print("Ignoring invalid saved scope line width", flush=True)

    edge_swirl = data.get("edge_swirl")
    if isinstance(edge_swirl, bool):
        settings["edge_swirl"] = edge_swirl
    elif "edge_swirl" in data:
        print("Ignoring invalid saved edge swirl setting", flush=True)

    lighting_mode = data.get("lighting_mode")
    if lighting_mode in LIGHTING_MODES:
        settings["lighting_mode"] = lighting_mode
    elif "lighting_mode" in data:
        print("Ignoring invalid saved lighting mode", flush=True)

    custom_palettes = []
    if "custom_palettes" in data:
        try:
            custom_palettes = normalize_custom_palettes(data["custom_palettes"])
            settings["custom_palettes"] = custom_palettes
        except ValueError as error:
            print(f"Ignoring invalid saved custom palettes: {error}", flush=True)

    lighting_color_mode = data.get("lighting_color_mode")
    if (lighting_color_mode in LIGHTING_COLOR_MODES
            or custom_palette_for_mode(custom_palettes, lighting_color_mode)):
        settings["lighting_color_mode"] = lighting_color_mode
    elif "lighting_color_mode" in data:
        print("Ignoring invalid saved lighting colour mode", flush=True)

    disco_light_style = data.get("disco_light_style")
    if disco_light_style in DISCO_LIGHT_STYLES:
        settings["disco_light_style"] = disco_light_style
    elif "disco_light_style" in data:
        print("Ignoring invalid saved light style", flush=True)

    disco_wire_color = data.get("disco_wire_color")
    if disco_wire_color in DISCO_WIRE_COLORS:
        settings["disco_wire_color"] = disco_wire_color
    elif "disco_wire_color" in data:
        print("Ignoring invalid saved light wire colour", flush=True)

    for key, minimum, maximum in (
        ("disco_light_count", DISCO_LIGHT_COUNT_MIN, DISCO_LIGHT_COUNT_MAX),
        ("disco_light_size", DISCO_LIGHT_SIZE_MIN, DISCO_LIGHT_SIZE_MAX),
    ):
        value = data.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and minimum <= value <= maximum:
            settings[key] = value
        elif key in data:
            print(f"Ignoring invalid saved {key.replace('_', ' ')}", flush=True)

    edge_swirl_direction = data.get("edge_swirl_direction")
    if edge_swirl_direction in EDGE_SWIRL_DIRECTIONS:
        settings["edge_swirl_direction"] = edge_swirl_direction
    elif "edge_swirl_direction" in data:
        print("Ignoring invalid saved edge swirl direction", flush=True)

    edge_swirl_slowdown = data.get("edge_swirl_slowdown")
    if (
        isinstance(edge_swirl_slowdown, int)
        and not isinstance(edge_swirl_slowdown, bool)
        and EDGE_SWIRL_SLOWDOWN_MIN <= edge_swirl_slowdown <= EDGE_SWIRL_SLOWDOWN_MAX
    ):
        settings["edge_swirl_slowdown"] = edge_swirl_slowdown
    elif "edge_swirl_slowdown" in data:
        print("Ignoring invalid saved edge swirl speed", flush=True)

    edge_color_intensity = data.get("edge_color_intensity")
    if (
        isinstance(edge_color_intensity, int)
        and not isinstance(edge_color_intensity, bool)
        and EDGE_COLOR_INTENSITY_MIN <= edge_color_intensity <= EDGE_COLOR_INTENSITY_MAX
    ):
        settings["edge_color_intensity"] = edge_color_intensity
    elif "edge_color_intensity" in data:
        print("Ignoring invalid saved edge colour intensity", flush=True)

    edge_fade = data.get("edge_fade")
    if (
        isinstance(edge_fade, int)
        and not isinstance(edge_fade, bool)
        and EDGE_FADE_MIN <= edge_fade <= EDGE_FADE_MAX
    ):
        settings["edge_fade"] = edge_fade
    elif "edge_fade" in data:
        print("Ignoring invalid saved edge colour fade", flush=True)

    edge_depth = data.get("edge_depth")
    if (
        isinstance(edge_depth, int)
        and not isinstance(edge_depth, bool)
        and EDGE_DEPTH_MIN <= edge_depth <= EDGE_DEPTH_MAX
    ):
        settings["edge_depth"] = edge_depth
    elif "edge_depth" in data:
        print("Ignoring invalid saved border depth", flush=True)

    edge_opacity = data.get("edge_opacity")
    if (
        isinstance(edge_opacity, int)
        and not isinstance(edge_opacity, bool)
        and EDGE_OPACITY_MIN <= edge_opacity <= EDGE_OPACITY_MAX
    ):
        settings["edge_opacity"] = edge_opacity
    elif "edge_opacity" in data:
        print("Ignoring invalid saved edge colour opacity", flush=True)

    edge_color_count = data.get("edge_color_count")
    if (
        isinstance(edge_color_count, int)
        and not isinstance(edge_color_count, bool)
        and EDGE_COLOR_COUNT_MIN <= edge_color_count <= EDGE_COLOR_COUNT_MAX
    ):
        settings["edge_color_count"] = edge_color_count
    elif "edge_color_count" in data:
        print("Ignoring invalid saved edge colour count", flush=True)

    album_art_opacity = data.get("album_art_opacity")
    if (
        isinstance(album_art_opacity, int)
        and not isinstance(album_art_opacity, bool)
        and ALBUM_ART_OPACITY_MIN <= album_art_opacity <= ALBUM_ART_OPACITY_MAX
    ):
        settings["album_art_opacity"] = album_art_opacity
    elif "album_art_opacity" in data:
        print("Ignoring invalid saved album art opacity", flush=True)

    artwork_mode = data.get("artwork_mode")
    if artwork_mode in ARTWORK_MODES:
        settings["artwork_mode"] = artwork_mode
    elif "artwork_mode" in data:
        print("Ignoring invalid saved artwork mode", flush=True)

    if loaded_from_legacy_path:
        try:
            _write_app_settings(settings)
        except OSError as error:
            print(f"Application settings could not be migrated: {error}", flush=True)
    return settings


def _write_app_settings(settings):
    settings_dir = os.path.dirname(APP_SETTINGS_PATH)
    os.makedirs(settings_dir, mode=0o700, exist_ok=True)
    file_descriptor, temporary_path = tempfile.mkstemp(
        prefix=".settings-", dir=settings_dir
    )
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as settings_file:
            json.dump({"version": 1, **settings}, settings_file, indent=2)
            settings_file.write("\n")
            settings_file.flush()
            os.fsync(settings_file.fileno())
        os.replace(temporary_path, APP_SETTINGS_PATH)
    finally:
        if os.path.exists(temporary_path):
            os.unlink(temporary_path)


def _normalize_metadata(value):
    normalized = unicodedata.normalize("NFKD", str(value))
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii").casefold()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", ascii_text).split())


def _source_application_key(identity):
    name = " ".join(identity.casefold().split())
    return re.sub(r"\s+(?:web browser|media player)$", "", name) or name


def _normalize_track_title(value):
    normalized = _normalize_metadata(value)
    for separator in (" feat ", " featuring ", " ft ", " with "):
        if separator in normalized:
            normalized = normalized.split(separator, 1)[0].strip()
    return normalized


def _track_match_score(expected, candidate):
    expected_text = _normalize_track_title(expected)
    candidate_text = _normalize_track_title(candidate)
    if not expected_text or not candidate_text:
        return 0.0
    if expected_text == candidate_text:
        return 1.0
    return SequenceMatcher(None, expected_text, candidate_text).ratio()


def _metadata_match_score(expected, candidate):
    expected_text = _normalize_metadata(expected)
    candidate_text = _normalize_metadata(candidate)
    if not expected_text or not candidate_text:
        return 0.0
    if expected_text == candidate_text:
        return 1.0

    expected_words = expected_text.split()
    candidate_words = candidate_text.split()
    if candidate_words[:len(expected_words)] == expected_words:
        suffix = candidate_words[len(expected_words):]
        if len(suffix) <= 3 and all(word in _VERSION_SUFFIXES or word.isdigit() for word in suffix):
            return 0.96
    return SequenceMatcher(None, expected_text, candidate_text).ratio()


def _artist_match_score(expected, candidate):
    expected_text = _normalize_metadata(expected)
    candidate_text = _normalize_metadata(candidate)
    if any(
        expected_text in group and candidate_text in group
        for group in _ARTIST_ALIAS_GROUPS
    ):
        return 1.0
    return _metadata_match_score(expected, candidate)


def _artist_credits(artist):
    return [part.strip() for part in re.split(
        r"\s+(?:&|and|feat\.?|ft\.?|featuring|with)\s+|,\s*",
        str(artist), flags=re.IGNORECASE,
    ) if part.strip()]


def _catalog_artist_match_score(expected, candidate, title_score):
    score = _artist_match_score(expected, candidate)
    if score >= 0.9 or title_score != 1.0:
        return score
    credits = _artist_credits(expected)
    normalized_candidate = _normalize_metadata(candidate)
    if normalized_candidate and len(credits) > 1 and normalized_candidate in {
        _normalize_metadata(credit) for credit in credits
    }:
        return 0.9
    return score


def _catalog_track_title(artist, title):
    title = str(title).strip()
    parts = re.split(r"\s+[-–—]\s+", title, maxsplit=1)
    if len(parts) == 2 and _artist_match_score(artist, parts[0]) >= 0.9:
        title = parts[1]
    title = re.sub(
        r"\s*[\(\[]\s*(?:official\s+(?:audio|video|music video)|"
        r"(?:official\s+)?lyric(?:s| video|s video)?)\s*[\)\]]",
        "", title, flags=re.IGNORECASE,
    )
    title = re.sub(
        r"\s*[\(\[]\s*(?:feat(?:uring)?|ft)\.?\s+[^\)\]]*[\)\]]",
        "", title, flags=re.IGNORECASE,
    )
    return title.strip()


def _track_art_key(artist, title, album, art_url, source_id="", fallback_art_url=""):
    return f"{source_id}|{artist}|{title}|{album}|{art_url}|{fallback_art_url}"


def _image_content_type(data, path):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return mimetypes.guess_type(path)[0] or "image/jpeg"


def _mpris_track_info(identity, player_properties):
    metadata = player_properties.get("Metadata", {})
    if not isinstance(metadata, dict):
        metadata = {}
    artists = metadata.get("xesam:artist", [])
    if isinstance(artists, str):
        artist = artists
    else:
        artist = ", ".join(str(value) for value in artists)
    duration = metadata.get("mpris:length")
    position = player_properties.get("Position")
    duration = int(duration) if isinstance(duration, int) and not isinstance(duration, bool) and duration > 0 else None
    position = int(position) if isinstance(position, int) and not isinstance(position, bool) and position >= 0 else None
    track_id = str(metadata.get("mpris:trackid", ""))
    rate = player_properties.get("Rate", 1.0)
    rate = float(rate) if isinstance(rate, (int, float)) and math.isfinite(rate) and rate > 0 else 1.0
    return {
        "title": str(metadata.get("xesam:title", "Unknown Title")),
        "artist": artist or identity,
        "album": str(metadata.get("xesam:album", "")),
        "status": str(player_properties.get("PlaybackStatus", "Stopped")).lower(),
        "device": identity,
        "art": "",
        "duration": duration,
        "position": position,
        "positionUpdatedAt": time.monotonic(),
        "playbackRate": rate,
        "trackId": track_id,
        "canSeek": bool(player_properties.get("CanSeek", False))
                   and track_id.startswith("/") and track_id != "/org/mpris/MediaPlayer2/TrackList/NoTrack"
                   and duration is not None and position is not None,
    }, str(metadata.get("mpris:artUrl", "")), str(metadata.get("kde:pid", ""))


def _find_matching_mpris_artwork(players, artist, title, album, source_id=""):
    players = list(players)
    for player in players:
        track = player.get("track", {})
        art_url = player.get("art_url", "")
        if not art_url or not art_url.startswith("file://"):
            continue
        if (
            _metadata_match_score(title, track.get("title", "")) < 0.9
            or _artist_match_score(artist, track.get("artist", "")) < 0.9
        ):
            continue
        candidate_album = track.get("album", "")
        if album and candidate_album and _metadata_match_score(album, candidate_album) < 0.88:
            continue
        return art_url

    selected_player = next(
        (player for player in players if player.get("id") == source_id), None
    )
    if not selected_player:
        return ""
    selected_bus_name = selected_player.get("bus_name", "")
    selected_pid = selected_player.get("pid", "")
    if not selected_pid:
        match = re.search(r"\.instance(\d+)$", selected_bus_name)
        selected_pid = match.group(1) if match else ""
    if not selected_pid:
        return ""

    for player in players:
        if player is selected_player or not player.get("art_url", "").startswith("file://"):
            continue
        bus_name = player.get("bus_name", "")
        player_pid = player.get("pid", "")
        if not player_pid:
            match = re.search(r"\.instance(\d+)$", bus_name)
            player_pid = match.group(1) if match else ""
        if player_pid != selected_pid:
            continue
        candidate_track = player.get("track", {})
        candidate_title = _normalize_metadata(candidate_track.get("title", ""))
        expected_title = _normalize_metadata(title)
        if candidate_title not in {
            expected_title,
            f"{expected_title} youtube music",
        }:
            continue
        candidate_artist = candidate_track.get("artist", "")
        if (
            candidate_artist != candidate_track.get("device", "")
            and _artist_match_score(artist, candidate_artist) < 0.9
        ):
            continue
        candidate_album = candidate_track.get("album", "")
        if album and candidate_album and _metadata_match_score(album, candidate_album) < 0.88:
            continue
        return player["art_url"]
    return ""


def _find_matching_album_cover(hits, artist, title, album):
    best_cover = ""
    best_score = 0.0
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        album_data = hit.get("album") or {}
        artist_data = hit.get("artist") or {}
        if not isinstance(album_data, dict) or not isinstance(artist_data, dict):
            continue
        title_score = _track_match_score(title, hit.get("title", ""))
        artist_score = _catalog_artist_match_score(artist, artist_data.get("name", ""), title_score)
        album_score = _metadata_match_score(album, album_data.get("title", "")) if album else 1.0
        if title_score < 0.9 or artist_score < 0.9 or album_score < 0.88:
            continue

        cover = album_data.get("cover_xl") or album_data.get("cover_big") or album_data.get("cover_medium")
        if not cover:
            continue
        score = title_score * 5 + artist_score * 3 + album_score * 2
        if score > best_score:
            best_cover = cover
            best_score = score
    return best_cover


def _find_matching_track_cover(hits, artist, title):
    return _find_matching_album_cover(hits, artist, title, "")


def _normalize_device_art_url(art_url):
    art_url = str(art_url or "").strip()
    if not art_url:
        return ""
    if art_url.startswith(("http://", "https://", "file://")):
        return art_url
    if art_url.startswith("data:image/"):
        return art_url
    if os.path.isabs(art_url):
        return f"file://{art_url}"
    return art_url


def _find_matching_itunes_cover(hits, artist, title, album):
    best_cover = ""
    best_score = 0.0
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        hit_title = hit.get("trackName") or hit.get("collectionName") or ""
        hit_artist = hit.get("artistName") or ""
        hit_album = hit.get("collectionName") or ""
        title_score = _track_match_score(title, hit_title)
        artist_score = _catalog_artist_match_score(artist, hit_artist, title_score)
        album_score = _metadata_match_score(album, hit_album) if album else 1.0
        if title_score < 0.9 or artist_score < 0.9 or album_score < 0.88:
            continue
        cover = str(hit.get("artworkUrl100") or "")
        if not cover:
            continue
        cover = re.sub(r"/\d+x\d+bb(?:\.(?:jpg|png))?$", "/600x600bb.jpg", cover)
        score = title_score * 5 + artist_score * 3 + album_score * 2
        if score > best_score:
            best_cover = cover
            best_score = score
    return best_cover


def _download_remote_album_art(artist, title, album):
    title = _catalog_track_title(artist, title) or title
    query_title = title.replace('"', "")
    cover_urls = []
    failures = []
    credits = _artist_credits(artist)
    queries = [artist]
    if len(credits) > 1:
        queries.extend(credit for credit in credits if credit != artist)

    unavailable = set()
    for query_artist in queries:
        for service in ("Deezer", "iTunes"):
            if service in unavailable:
                continue
            try:
                if service == "Deezer":
                    safe_artist = query_artist.replace('"', "")
                    query = urllib.parse.urlencode({
                        "q": f"{safe_artist} {query_title}", "limit": 25,
                    })
                    url = "https://api.deezer.com/search?" + query
                    user_agent = "Mozilla/5.0 (X11; Linux x86_64) AmbiBeat/1.0"
                else:
                    query = urllib.parse.urlencode({
                        "term": f"{query_artist} {title}", "media": "music", "limit": 25,
                    })
                    url = "https://itunes.apple.com/search?" + query
                    user_agent = "AmbiBeat/1.0"
                req = urllib.request.Request(
                    url, headers={"User-Agent": user_agent, "Accept": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=6) as resp:
                    data = json.loads(resp.read().decode("utf-8", errors="ignore"))
                if not isinstance(data, dict):
                    raise ValueError("Invalid catalog response")
                if service == "Deezer":
                    hits = data.get("data", [])
                    art_url = (_find_matching_album_cover(hits, artist, title, album)
                               or _find_matching_track_cover(hits, artist, title))
                else:
                    hits = data.get("results", [])
                    art_url = (_find_matching_itunes_cover(hits, artist, title, album)
                               or _find_matching_itunes_cover(hits, artist, title, ""))
                if art_url:
                    cover_urls.append((service, art_url))
                else:
                    failures.append(f"{service}: no matching artwork")
            except (OSError, ValueError, TypeError) as error:
                unavailable.add(service)
                failures.append(f"{service}: {error}")

        if cover_urls or len(unavailable) == 2:
            break

    for service_name, art_url in cover_urls:
        try:
            art_req = urllib.request.Request(
                art_url,
                headers={"User-Agent": "AmbiBeat/1.0", "Accept": "image/*"},
            )
            with urllib.request.urlopen(art_req, timeout=8) as resp:
                image_data = resp.read()
            if not image_data:
                raise ValueError(f"Empty album-art response from {service_name}")
            return image_data, service_name
        except Exception as error:
            failures.append(f"{service_name}: {error}")

    if failures:
        return None, "; ".join(failures)
    return None, "no matching artwork"


class AmbiBeatBackend:
    """Own playback state, audio capture, event-loop resources, and settings."""

    def __init__(self, session_bus):
        self.session_bus = session_bus
        os.makedirs(ART_CACHE_DIR, exist_ok=True)

        self.track_info = {
            "title": "Waiting for music...",
            "artist": "",
            "album": "",
            "status": "stopped",
            "device": "",
            "art": "",
            "artStatus": "loading",
        }
        self._mpris_players = {}
        self._mpris_errors = set()
        self._last_art_key = ""
        self.bars = [0.0] * 24
        self.wave = [0.0] * 64
        self.sample_rate = 48000
        settings = _load_app_settings()
        self.photosensitivity_warning_acknowledged = settings.get(
            "photosensitivity_warning_acknowledged", False
        )
        self.sample_rate = settings.get("sample_rate", self.sample_rate)
        self.source_prompt_seconds = settings.get("source_prompt_seconds", SOURCE_PROMPT_SECONDS_DEFAULT)
        self.source_prompt_enabled = settings.get("source_prompt_enabled", True)
        self.source_volume = SourceVolume(
            settings.get("source_gains", {}), settings.get("volume_journal", {}),
            self._save_settings, self._source_process_ids,
        )
        self.boost_warning = ""
        self.system_volume = None
        self._last_system_volume_error = ""
        self.visualizer_mode = settings.get("visualizer_mode", 0)
        self.scope_zero_line = settings.get("scope_zero_line", True)
        self.scope_line_width = settings.get("scope_line_width", SCOPE_LINE_WIDTH_DEFAULT)
        self.edge_swirl = settings.get("edge_swirl", False)
        self.lighting_mode = settings.get("lighting_mode", "border")
        self.custom_palettes = settings.get("custom_palettes", [])
        self.lighting_color_mode = settings.get("lighting_color_mode", "artwork")
        self.disco_light_style = settings.get("disco_light_style", "glow")
        self.disco_wire_color = settings.get("disco_wire_color", "green")
        self.disco_light_count = settings.get("disco_light_count", DISCO_LIGHT_COUNT_DEFAULT)
        self.disco_light_size = settings.get("disco_light_size", DISCO_LIGHT_SIZE_DEFAULT)
        self.edge_swirl_direction = settings.get("edge_swirl_direction", "clockwise")
        self._edge_direction_lock = threading.Lock()
        self._edge_direction_track = None
        self._edge_random_direction = None
        self.edge_swirl_slowdown = settings.get("edge_swirl_slowdown", 1)
        self.edge_color_intensity = settings.get(
            "edge_color_intensity", EDGE_COLOR_INTENSITY_DEFAULT
        )
        self.edge_fade = settings.get("edge_fade", EDGE_FADE_DEFAULT)
        self.edge_depth = settings.get("edge_depth", EDGE_DEPTH_DEFAULT)
        self.edge_opacity = settings.get("edge_opacity", EDGE_OPACITY_DEFAULT)
        self.edge_color_count = settings.get("edge_color_count", EDGE_COLOR_COUNT_DEFAULT)
        self.album_art_opacity = settings.get("album_art_opacity", ALBUM_ART_OPACITY_DEFAULT)
        self.artwork_mode = settings.get("artwork_mode", "album")
        self.selected_source = settings.get("selected_source", "")
        self.last_source_instances = settings.get("last_source_instances", {})
        self.running = True
        self._stopped = False
        self._capture_lock = threading.Lock()
        self._capture_proc = None
        self._idle_inhibitor = None

        try:
            self.source_volume.restore()
        except (OSError, subprocess.SubprocessError, ValueError, RuntimeError) as error:
            self.boost_warning = f"Source volume could not be restored: {error}"
            print(self.boost_warning, flush=True)
        self._refresh_mpris_players()
        if self.selected_source and self.selected_source == settings.get("selected_source"):
            player = self._mpris_players.get(self.selected_source.removeprefix("mpris:"))
            if player:
                self.last_source_instances.setdefault(player["application"], self.selected_source)
        self._refresh_system_volume()
        self._timer_ids = [
            GLib.timeout_add_seconds(3, self._refresh_mpris_players),
            GLib.timeout_add_seconds(5, self._refresh_system_volume),
            GLib.timeout_add_seconds(3, self._refresh_source_volume),
        ]
        self._glib_loop = GLib.MainLoop()
        self._glib_thread = threading.Thread(target=self._glib_loop.run, daemon=True)
        self._glib_thread.start()
        deadline = time.monotonic() + 5
        while not self._glib_loop.is_running():
            if not self._glib_thread.is_alive() or time.monotonic() >= deadline:
                for timer_id in self._timer_ids:
                    GLib.source_remove(timer_id)
                self._stop_idle_inhibition()
                raise RuntimeError("Audio backend event loop could not be started")
            time.sleep(0.001)

        self.audio_thread = threading.Thread(target=self._capture_audio, daemon=True)
        self.audio_thread.start()

    def activate(self):
        try:
            self.source_volume.activate(self.selected_source)
        except (OSError, subprocess.SubprocessError, ValueError, RuntimeError) as error:
            self.boost_warning = f"Source volume could not be updated: {error}"
            print(self.boost_warning, flush=True)

    def stop(self):
        if self._stopped:
            return
        self.running = False
        for timer_id in self._timer_ids:
            GLib.source_remove(timer_id)
        self._timer_ids.clear()
        if self._glib_thread.is_alive():
            self._glib_loop.quit()
            self._glib_thread.join(timeout=5)
        errors = []
        if self._glib_thread.is_alive():
            errors.append("audio backend event loop did not stop")
        with self._capture_lock:
            proc = self._capture_proc
            if proc is not None and proc.poll() is None:
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
                except OSError as error:
                    errors.append(f"audio capture could not be terminated: {error}")
        self.audio_thread.join(timeout=2)
        if self.audio_thread.is_alive():
            with self._capture_lock:
                proc = self._capture_proc
                if proc is not None and proc.poll() is None:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                    except OSError as error:
                        errors.append(f"audio capture could not be killed: {error}")
            self.audio_thread.join(timeout=3)
        if self.audio_thread.is_alive():
            errors.append("audio capture did not stop")
        try:
            self._stop_idle_inhibition()
        except (OSError, subprocess.SubprocessError) as error:
            errors.append(f"idle inhibition could not be stopped: {error}")
        try:
            self.source_volume.deactivate()
        except (OSError, subprocess.SubprocessError, ValueError, RuntimeError) as error:
            errors.append(f"source volume could not be restored: {error}")
        if errors:
            raise RuntimeError("; ".join(errors))
        self._stopped = True

    def set_sample_rate(self, sample_rate):
        if sample_rate not in SAMPLE_RATES:
            return False
        previous_rate = self.sample_rate
        self.sample_rate = sample_rate
        try:
            self._save_settings()
        except OSError:
            self.sample_rate = previous_rate
            raise
        return True

    def acknowledge_photosensitivity_warning(self):
        self.set_show_photosensitivity_warning(False)

    def set_source_prompt_seconds(self, seconds):
        if (not isinstance(seconds, int) or isinstance(seconds, bool)
                or not SOURCE_PROMPT_SECONDS_MIN <= seconds <= SOURCE_PROMPT_SECONDS_MAX):
            return False
        previous = self.source_prompt_seconds
        self.source_prompt_seconds = seconds
        try:
            self._save_settings()
        except OSError:
            self.source_prompt_seconds = previous
            raise
        return True

    def set_source_prompt_enabled(self, enabled):
        if not isinstance(enabled, bool):
            return False
        previous = self.source_prompt_enabled
        self.source_prompt_enabled = enabled
        try:
            self._save_settings()
        except OSError:
            self.source_prompt_enabled = previous
            raise
        return True

    def set_show_photosensitivity_warning(self, show):
        if not isinstance(show, bool):
            return False
        with self.source_volume.lock:
            previous = self.photosensitivity_warning_acknowledged
            self.photosensitivity_warning_acknowledged = not show
            try:
                self._save_settings()
            except OSError:
                self.photosensitivity_warning_acknowledged = previous
                raise
        return True

    def update_setting(self, name, value, source_id=None):
        setters = {
            "sample-rate": self.set_sample_rate,
            "source-prompt-seconds": self.set_source_prompt_seconds,
            "source-prompt-enabled": self.set_source_prompt_enabled,
            "show-photosensitivity-warning": self.set_show_photosensitivity_warning,
            "system-volume": self.set_system_volume,
            "visualizer-mode": self.set_visualizer_mode,
            "scope-zero-line": self.set_scope_zero_line,
            "scope-line-width": self.set_scope_line_width,
            "artwork-mode": self.set_artwork_mode,
            "album-art-opacity": self.set_album_art_opacity,
            "lighting-mode": self.set_lighting_mode,
            "lighting-color-mode": self.set_lighting_color_mode,
            "custom-palettes": self.set_custom_palettes,
            "edge-swirl": self.set_edge_swirl,
            "edge-color-intensity": self.set_edge_color_intensity,
            "edge-fade": self.set_edge_fade,
            "edge-opacity": self.set_edge_opacity,
            "edge-color-count": self.set_edge_color_count,
            "edge-depth": self.set_edge_depth,
            "disco-light-style": self.set_disco_light_style,
            "disco-wire-color": self.set_disco_wire_color,
            "disco-light-count": self.set_disco_light_count,
            "disco-light-size": self.set_disco_light_size,
            "edge-swirl-slowdown": self.set_edge_swirl_slowdown,
            "edge-swirl-direction": self.set_edge_swirl_direction,
        }
        if name == "source":
            success, warning = self.set_source(value)
        elif name == "source-gain":
            success = self.set_source_gain(source_id, value)
            warning = self.boost_warning if source_id == self.selected_source else ""
        else:
            setter = setters.get(name)
            if setter is None:
                raise ValueError(f"Unknown setting: {name}")
            success, warning = setter(value), ""
        if not success:
            raise ValueError(warning or f"Could not change {name}")
        return warning

    def set_edge_color_intensity(self, intensity):
        if (
            not isinstance(intensity, int)
            or isinstance(intensity, bool)
            or not EDGE_COLOR_INTENSITY_MIN <= intensity <= EDGE_COLOR_INTENSITY_MAX
        ):
            return False
        previous_intensity = self.edge_color_intensity
        self.edge_color_intensity = intensity
        try:
            self._save_settings()
        except OSError:
            self.edge_color_intensity = previous_intensity
            raise
        return True

    def set_edge_fade(self, fade):
        if (
            not isinstance(fade, int)
            or isinstance(fade, bool)
            or not EDGE_FADE_MIN <= fade <= EDGE_FADE_MAX
        ):
            return False
        previous_fade = self.edge_fade
        self.edge_fade = fade
        try:
            self._save_settings()
        except OSError:
            self.edge_fade = previous_fade
            raise
        return True

    def set_edge_depth(self, depth):
        if (
            not isinstance(depth, int)
            or isinstance(depth, bool)
            or not EDGE_DEPTH_MIN <= depth <= EDGE_DEPTH_MAX
        ):
            return False
        previous_depth = self.edge_depth
        self.edge_depth = depth
        try:
            self._save_settings()
        except OSError:
            self.edge_depth = previous_depth
            raise
        return True

    def set_edge_opacity(self, opacity):
        if (
            not isinstance(opacity, int)
            or isinstance(opacity, bool)
            or not EDGE_OPACITY_MIN <= opacity <= EDGE_OPACITY_MAX
        ):
            return False
        previous_opacity = self.edge_opacity
        self.edge_opacity = opacity
        try:
            self._save_settings()
        except OSError:
            self.edge_opacity = previous_opacity
            raise
        return True

    def set_edge_color_count(self, count):
        if (
            not isinstance(count, int)
            or isinstance(count, bool)
            or not EDGE_COLOR_COUNT_MIN <= count <= EDGE_COLOR_COUNT_MAX
        ):
            return False
        previous_count = self.edge_color_count
        self.edge_color_count = count
        try:
            self._save_settings()
        except OSError:
            self.edge_color_count = previous_count
            raise
        return True

    def set_artwork_mode(self, mode):
        if mode not in ARTWORK_MODES:
            return False
        previous_mode = self.artwork_mode
        self.artwork_mode = mode
        try:
            self._save_settings()
        except OSError:
            self.artwork_mode = previous_mode
            raise
        return True

    def set_album_art_opacity(self, opacity):
        if (
            not isinstance(opacity, int)
            or isinstance(opacity, bool)
            or not ALBUM_ART_OPACITY_MIN <= opacity <= ALBUM_ART_OPACITY_MAX
        ):
            return False
        previous_opacity = self.album_art_opacity
        self.album_art_opacity = opacity
        try:
            self._save_settings()
        except OSError:
            self.album_art_opacity = previous_opacity
            raise
        return True

    def set_source_gain(self, source_id, gain):
        if (not source_id or isinstance(gain, bool) or not math.isfinite(gain) or not 1.0 <= gain <= 4.0
                or not source_id.startswith(f"mpris:{MPRIS_PREFIX}")):
            return False
        with self.source_volume.lock:
            previous = self.source_volume.gains.get(source_id)
            self.source_volume.gains[source_id] = round(gain, 1)
            try:
                self._save_settings()
                if self.selected_source == source_id:
                    try:
                        self.source_volume.apply(source_id)
                    except StreamNotFoundError as error:
                        self.boost_warning = f"Source boost will be applied when a matching stream is available: {error}"
                        print(self.boost_warning, flush=True)
                    else:
                        self.boost_warning = ""
            except (OSError, subprocess.SubprocessError, ValueError, RuntimeError):
                if previous is None:
                    self.source_volume.gains.pop(source_id, None)
                else:
                    self.source_volume.gains[source_id] = previous
                try:
                    self.source_volume.apply(self.selected_source)
                    self._save_settings()
                except (OSError, subprocess.SubprocessError, ValueError, RuntimeError) as error:
                    print(f"Source boost rollback failed: {error}", flush=True)
                raise
        return True

    def set_visualizer_mode(self, mode):
        if not isinstance(mode, int) or isinstance(mode, bool) or mode not in (0, 1, 2, 4, 5):
            return False
        previous_mode = self.visualizer_mode
        self.visualizer_mode = mode
        try:
            self._save_settings()
        except OSError:
            self.visualizer_mode = previous_mode
            raise
        return True

    def set_scope_zero_line(self, enabled):
        if not isinstance(enabled, bool):
            return False
        previous = self.scope_zero_line
        self.scope_zero_line = enabled
        try:
            self._save_settings()
        except OSError:
            self.scope_zero_line = previous
            raise
        return True

    def set_scope_line_width(self, width):
        if (not isinstance(width, int) or isinstance(width, bool)
                or not SCOPE_LINE_WIDTH_MIN <= width <= SCOPE_LINE_WIDTH_MAX):
            return False
        previous = self.scope_line_width
        self.scope_line_width = width
        try:
            self._save_settings()
        except OSError:
            self.scope_line_width = previous
            raise
        return True

    def set_disco_light_style(self, style):
        if style not in DISCO_LIGHT_STYLES:
            return False
        previous = self.disco_light_style
        self.disco_light_style = style
        try:
            self._save_settings()
        except OSError:
            self.disco_light_style = previous
            raise
        return True

    def set_disco_wire_color(self, color):
        if color not in DISCO_WIRE_COLORS:
            return False
        previous = self.disco_wire_color
        self.disco_wire_color = color
        try:
            self._save_settings()
        except OSError:
            self.disco_wire_color = previous
            raise
        return True

    def set_disco_light_count(self, count):
        if (not isinstance(count, int) or isinstance(count, bool)
                or not DISCO_LIGHT_COUNT_MIN <= count <= DISCO_LIGHT_COUNT_MAX):
            return False
        previous = self.disco_light_count
        self.disco_light_count = count
        try:
            self._save_settings()
        except OSError:
            self.disco_light_count = previous
            raise
        return True

    def set_disco_light_size(self, size):
        if (not isinstance(size, int) or isinstance(size, bool)
                or not DISCO_LIGHT_SIZE_MIN <= size <= DISCO_LIGHT_SIZE_MAX):
            return False
        previous = self.disco_light_size
        self.disco_light_size = size
        try:
            self._save_settings()
        except OSError:
            self.disco_light_size = previous
            raise
        return True

    def set_lighting_color_mode(self, mode):
        if mode not in LIGHTING_COLOR_MODES and not custom_palette_for_mode(self.custom_palettes, mode):
            return False
        previous_mode = self.lighting_color_mode
        self.lighting_color_mode = mode
        try:
            self._save_settings()
        except OSError:
            self.lighting_color_mode = previous_mode
            raise
        return True

    def set_custom_palettes(self, palettes):
        normalized = normalize_custom_palettes(palettes)
        previous, previous_mode = self.custom_palettes, self.lighting_color_mode
        self.custom_palettes = normalized
        if (self.lighting_color_mode not in LIGHTING_COLOR_MODES
                and not custom_palette_for_mode(normalized, self.lighting_color_mode)):
            self.lighting_color_mode = "artwork"
        try:
            self._save_settings()
        except OSError:
            self.custom_palettes, self.lighting_color_mode = previous, previous_mode
            raise
        return True

    def set_lighting_mode(self, mode):
        if mode not in LIGHTING_MODES:
            return False
        previous_mode = self.lighting_mode
        self.lighting_mode = mode
        try:
            self._save_settings()
        except OSError:
            self.lighting_mode = previous_mode
            raise
        return True

    def set_edge_swirl(self, enabled):
        if not isinstance(enabled, bool):
            return False
        previous_enabled = self.edge_swirl
        self.edge_swirl = enabled
        try:
            self._save_settings()
        except OSError:
            self.edge_swirl = previous_enabled
            raise
        return True

    def set_edge_swirl_slowdown(self, slowdown):
        if (
            not isinstance(slowdown, int)
            or isinstance(slowdown, bool)
            or not EDGE_SWIRL_SLOWDOWN_MIN <= slowdown <= EDGE_SWIRL_SLOWDOWN_MAX
        ):
            return False
        previous_slowdown = self.edge_swirl_slowdown
        self.edge_swirl_slowdown = slowdown
        try:
            self._save_settings()
        except OSError:
            self.edge_swirl_slowdown = previous_slowdown
            raise
        return True

    def set_edge_swirl_direction(self, direction):
        if direction not in EDGE_SWIRL_DIRECTIONS:
            return False
        previous_direction = self.edge_swirl_direction
        self.edge_swirl_direction = direction
        try:
            self._save_settings()
        except OSError:
            self.edge_swirl_direction = previous_direction
            raise
        return True

    def _resolved_edge_swirl_direction(self, track, source_id):
        track_key = (source_id, track.get("title"), track.get("artist"), track.get("album"))
        with self._edge_direction_lock:
            if track_key != self._edge_direction_track:
                self._edge_direction_track = track_key
                self._edge_random_direction = None
            if self.edge_swirl_direction != "random":
                return self.edge_swirl_direction
            if self._edge_random_direction is None:
                self._edge_random_direction = random.choice(EDGE_SWIRL_DIRECTIONS[:2])
            return self._edge_random_direction

    def _save_settings(self):
        with self.source_volume.lock:
            _write_app_settings({
                "sample_rate": self.sample_rate,
                "source_prompt_seconds": self.source_prompt_seconds,
                "source_prompt_enabled": self.source_prompt_enabled,
                "photosensitivity_warning_acknowledged": self.photosensitivity_warning_acknowledged,
                "source_gains": self.source_volume.gains,
                "volume_journal": self.source_volume.journal,
                "selected_source": self.selected_source,
                "last_source_instances": self.last_source_instances,
                "visualizer_mode": self.visualizer_mode,
                "scope_zero_line": self.scope_zero_line,
                "scope_line_width": self.scope_line_width,
                "edge_swirl": self.edge_swirl,
                "lighting_mode": self.lighting_mode,
                "custom_palettes": self.custom_palettes,
                "lighting_color_mode": self.lighting_color_mode,
                "disco_light_style": self.disco_light_style,
                "disco_wire_color": self.disco_wire_color,
                "disco_light_count": self.disco_light_count,
                "disco_light_size": self.disco_light_size,
                "edge_swirl_direction": self.edge_swirl_direction,
                "edge_swirl_slowdown": self.edge_swirl_slowdown,
                "edge_color_intensity": self.edge_color_intensity,
                "edge_fade": self.edge_fade,
                "edge_depth": self.edge_depth,
                "edge_opacity": self.edge_opacity,
                "edge_color_count": self.edge_color_count,
                "album_art_opacity": self.album_art_opacity,
                "artwork_mode": self.artwork_mode,
            })

    def set_system_volume(self, volume):
        if not 0 <= volume <= 100:
            return False
        try:
            subprocess.run(
                ["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{volume}%"],
                check=True,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.SubprocessError) as error:
            print(f"System volume could not be changed: {error}", flush=True)
            return False
        self.system_volume = volume
        self._last_system_volume_error = ""
        return True

    def _read_system_volume(self):
        default_sink = subprocess.run(
            ["pactl", "get-default-sink"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if not default_sink:
            raise RuntimeError("No default audio output is available")
        sinks = json.loads(
            subprocess.run(
                ["pactl", "-f", "json", "list", "sinks"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
        )
        sink = next((item for item in sinks if item.get("name") == default_sink), None)
        if not sink:
            raise RuntimeError(f"Default audio output was not found: {default_sink}")
        values = [
            int(channel["value"])
            for channel in (sink.get("volume") or {}).values()
            if isinstance(channel, dict) and "value" in channel
        ]
        if not values:
            raise RuntimeError(f"Default audio output has no readable volume: {default_sink}")
        return round(sum(values) * 100 / 65536 / len(values))

    def _refresh_system_volume(self):
        try:
            self.system_volume = self._read_system_volume()
        except (OSError, subprocess.SubprocessError, ValueError, RuntimeError) as error:
            message = str(error)
            if message != self._last_system_volume_error:
                print(f"System volume could not be read: {error}", flush=True)
                self._last_system_volume_error = message
        else:
            self._last_system_volume_error = ""
        return True

    def _source_process_ids(self, source_id):
        player = self._mpris_players.get(source_id.removeprefix("mpris:"))
        if not player:
            return set()
        pid = player.get("pid", "")
        if pid and str(pid).isdecimal():
            return {str(pid)}
        try:
            daemon = dbus.Interface(
                self.session_bus.get_object("org.freedesktop.DBus", "/org/freedesktop/DBus"),
                "org.freedesktop.DBus",
            )
            return {str(daemon.GetConnectionUnixProcessID(player["bus_name"]))}
        except dbus.DBusException as error:
            raise RuntimeError(f"Could not identify the selected media player: {error}") from error

    def _refresh_source_volume(self):
        try:
            self.source_volume.refresh(self.selected_source)
        except (OSError, subprocess.SubprocessError, ValueError, RuntimeError) as error:
            warning = f"Source volume could not be updated: {error}"
            if warning != self.boost_warning:
                print(warning, flush=True)
            self.boost_warning = warning
        else:
            self.boost_warning = ""
        return True

    def _refresh_mpris_players(self):
        players = {}
        for bus_name in self.session_bus.list_names():
            bus_name = str(bus_name)
            if not bus_name.startswith(MPRIS_PREFIX):
                continue
            try:
                obj = self.session_bus.get_object(
                    bus_name, MPRIS_OBJECT_PATH, introspect=False
                )
                properties = dbus.Interface(obj, "org.freedesktop.DBus.Properties")
                root_properties = properties.GetAll("org.mpris.MediaPlayer2")
                identity = str(root_properties.get("Identity", ""))
                if not identity:
                    raise ValueError(f"Media player {bus_name} has no identity")
                desktop_entry = str(root_properties.get("DesktopEntry", ""))
                player_properties = properties.GetAll(MPRIS_PLAYER_INTERFACE)
                track, art_url, pid = _mpris_track_info(identity, player_properties)
            except (dbus.DBusException, ValueError) as error:
                if bus_name not in self._mpris_errors:
                    print(f"Unable to read media player {bus_name}: {error}", flush=True)
                    self._mpris_errors.add(bus_name)
                continue
            self._mpris_errors.discard(bus_name)
            players[bus_name] = {
                "id": f"mpris:{bus_name}",
                "bus_name": bus_name,
                "identity": identity,
                "application": _source_application_key(identity),
                "desktop_entry": desktop_entry,
                "pid": pid,
                "track": track,
                "art_url": art_url,
            }

        identity_counts = {}
        for player in players.values():
            identity_counts[player["identity"]] = (
                identity_counts.get(player["identity"], 0) + 1
            )
        for bus_name, player in players.items():
            player["label"] = player["identity"]
            if identity_counts[player["identity"]] > 1:
                suffix = bus_name[len(MPRIS_PREFIX):].replace("_", " ")
                player["label"] = f'{player["identity"]} ({suffix})'

        self._mpris_players = players
        if self.selected_source.startswith("mpris:"):
            bus_name = self.selected_source[len("mpris:"):]
            player = players.get(bus_name)
            if player:
                self._publish_track(player["track"], player["id"], player["art_url"])
        selected_available = (self.selected_source.startswith("mpris:")
                              and self.selected_source[len("mpris:"):] in players)
        if not selected_available:
            active_player = next(
                (player for player in players.values()
                 if player["track"]["status"] == "playing"),
                None,
            )
            candidate = active_player or next(iter(players.values()), None)
            preferred_id = self.last_source_instances.get(candidate["application"]) if candidate else None
            preferred = players.get(preferred_id.removeprefix("mpris:")) if preferred_id else None
            if preferred and preferred["application"] == candidate["application"]:
                candidate = preferred
            target = candidate["id"] if candidate else ""
            with self.source_volume.lock:
                try:
                    self.source_volume.restore()
                except (OSError, subprocess.SubprocessError, ValueError, RuntimeError) as error:
                    self.boost_warning = f"Source volume could not be restored: {error}"
                    print(self.boost_warning, flush=True)
                    return True
                self.selected_source = target
                if target:
                    player = players[target[len("mpris:"):]]
                    self._publish_track(player["track"], target, player["art_url"])
                else:
                    self.track_info = {
                        "title": "Waiting for music...", "artist": "", "album": "",
                        "status": "stopped", "device": "", "art": "", "artStatus": "fallback",
                    }
                    self._sync_idle_inhibition()
        return True

    def set_source(self, source_id):
        with self.source_volume.lock:
            return self._set_source(source_id)

    def _set_source(self, source_id):
        previous_source = self.selected_source
        if not isinstance(source_id, str) or not source_id.startswith("mpris:"):
            return False, ""
        bus_name = source_id[len("mpris:"):]
        player = self._mpris_players.get(bus_name)
        if not player:
            return False, ""

        warnings = []
        switching = previous_source != source_id
        if switching:
            try:
                self.source_volume.restore()
            except (OSError, subprocess.SubprocessError, ValueError, RuntimeError) as error:
                warning = f"Source volume could not be restored: {error}"
                print(warning, flush=True)
                return False, warning
            for other_player in self._mpris_players.values():
                if (
                    other_player["id"] != source_id
                    and other_player["track"].get("status") == "playing"
                ):
                    warning = self._mpris_control_warning(other_player["id"], "Pause")
                    if warning:
                        warnings.append(warning)
        self.selected_source = source_id
        self.last_source_instances[player["application"]] = source_id
        self._publish_track(player["track"], source_id, player["art_url"])
        if switching:
            warning = self._mpris_control_warning(source_id, "Play")
            if warning:
                warnings.append(warning)
        try:
            self.source_volume.apply(source_id)
        except (OSError, subprocess.SubprocessError, ValueError, RuntimeError) as error:
            warning = f"Source volume boost could not be applied: {error}"
            print(warning, flush=True)
            self.boost_warning = warning
            warnings.append(warning)
        try:
            self._save_settings()
        except OSError as error:
            warning = f"Playback changed, but settings could not be saved: {error}"
            print(warning, flush=True)
            warnings.append(warning)
        return True, "; ".join(warnings)

    def _mpris_control_warning(self, source_id, action):
        try:
            self._control_mpris_player(source_id, action)
            return ""
        except (dbus.DBusException, RuntimeError) as error:
            warning = f"Media player could not be {action.lower()}ed: {error}"
        print(warning, flush=True)
        return warning

    def _sync_idle_inhibition(self):
        should_inhibit = self.running and self.track_info.get("status") == "playing"
        if should_inhibit:
            if getattr(self, "_idle_inhibitor", None) is not None:
                if self._idle_inhibitor.poll() is None:
                    return
                self._idle_inhibitor = None
            try:
                self._idle_inhibitor = subprocess.Popen(
                    [
                        "systemd-inhibit",
                        "--what=idle:sleep",
                        "--who=AmbiBeat",
                        "--why=Audio playback is active",
                        "--mode=block",
                        "sleep",
                        "infinity",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                )
            except (OSError, subprocess.SubprocessError) as error:
                print(f"Screen idle inhibition could not be started: {error}", flush=True)
        elif getattr(self, "_idle_inhibitor", None) is not None:
            self._idle_inhibitor.terminate()
            try:
                self._idle_inhibitor.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self._idle_inhibitor.kill()
            self._idle_inhibitor = None

    def _stop_idle_inhibition(self):
        if self._idle_inhibitor is not None:
            self._idle_inhibitor.terminate()
            try:
                self._idle_inhibitor.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self._idle_inhibitor.kill()
            self._idle_inhibitor = None

    def _publish_track(self, track, source_id, art_url):
        current_art = self.track_info.get("art", "")
        self.track_info.update(track)
        self.track_info["art"] = current_art
        art_url = _normalize_device_art_url(art_url)
        fallback_art_url = ""
        usable_primary_art = art_url.startswith(("http://", "https://"))
        if art_url.startswith("file://"):
            usable_primary_art = os.path.isfile(
                urllib.parse.unquote(art_url[7:])
            )
        if not usable_primary_art:
            fallback_art_url = _find_matching_mpris_artwork(
                self._mpris_players.values(),
                track["artist"],
                track["title"],
                track["album"],
                source_id,
            )
        art_key = _track_art_key(
            track["artist"], track["title"], track["album"], art_url,
            source_id, fallback_art_url
        )
        if art_key != self._last_art_key:
            self._last_art_key = art_key
            self.track_info["art"] = ""
            self.track_info["artStatus"] = "loading"
            self._fetch_album_art(
                track["artist"],
                track["title"],
                track["album"],
                art_url,
                source_id,
                fallback_art_url,
            )
        self._sync_idle_inhibition()

    def _set_art_if_current(self, art_key, art_url):
        if self._last_art_key == art_key:
            self.track_info["art"] = art_url
            self.track_info["artStatus"] = "loaded"

    def _set_device_art_fallback(self, art_key, fallback_art_url):
        if not fallback_art_url.startswith("file://"):
            return False
        local_path = urllib.parse.unquote(fallback_art_url[7:])
        if not os.path.isfile(local_path):
            return False
        self._set_art_if_current(art_key, fallback_art_url)
        return True

    def _set_app_icon_fallback(self, art_key):
        if self._last_art_key != art_key:
            return False
        if not os.path.isfile(APP_ICON_PATH):
            self.track_info["artStatus"] = "failed"
            return False
        self.track_info["art"] = f"file://{APP_ICON_PATH}"
        self.track_info["artStatus"] = "fallback"
        return True

    def _fetch_album_art(
        self, artist, title, album, artwork_url="", source_id="", fallback_art_url=""
    ):
        art_key = _track_art_key(
            artist, title, album, artwork_url, source_id, fallback_art_url
        )
        self._last_art_key = art_key
        self.track_info["art"] = ""
        self.track_info["artStatus"] = "loading"
        self._set_app_icon_fallback(art_key)

        if artwork_url and artwork_url.startswith("file://"):
            local_path = urllib.parse.unquote(artwork_url[7:])
            if os.path.isfile(local_path):
                self._set_art_if_current(art_key, artwork_url)
                return

        if (
            not title
            or title in ("Waiting for music...", "Unknown Title")
            or not artist
            or artist == "Unknown Artist"
        ):
            self._set_app_icon_fallback(art_key)
            return

        cache_key = f"v2|{artist}|{title}|{album}".casefold()
        cache_hash = hashlib.md5(cache_key.encode("utf-8")).hexdigest()
        cache_file = os.path.join(ART_CACHE_DIR, f"{cache_hash}.jpg")

        def _download():
            try:
                if artwork_url.startswith(("http://", "https://")):
                    art_req = urllib.request.Request(
                        artwork_url,
                        headers={"User-Agent": "AmbiBeat/1.0", "Accept": "image/*"},
                    )
                    with urllib.request.urlopen(art_req, timeout=8) as resp:
                        image_data = resp.read()
                    if not image_data:
                        raise ValueError(
                            f"Empty device album-art response for {artist} - {title}"
                        )
                    with open(cache_file, "wb") as image_file:
                        image_file.write(image_data)
                    self._set_art_if_current(art_key, f"file://{cache_file}")
                    return
            except Exception as e:
                print(f"Device artwork fetch error: {e}", flush=True)

            if self._set_device_art_fallback(art_key, fallback_art_url):
                return

            if os.path.isfile(cache_file):
                self._set_art_if_current(art_key, f"file://{cache_file}")
                return

            image_data, service_name = _download_remote_album_art(
                artist, title, album
            )
            if not image_data:
                self._set_app_icon_fallback(art_key)
                print(
                    f"Artwork fetch error: {service_name}; "
                    f"artist={artist}; title={title}",
                    flush=True,
                )
                return

            try:
                with open(cache_file, "wb") as image_file:
                    image_file.write(image_data)
                self._set_art_if_current(art_key, f"file://{cache_file}")
            except OSError as error:
                print(f"Artwork cache write error: {error}", flush=True)

        threading.Thread(target=_download, daemon=True).start()

    def _get_default_monitor(self):
        try:
            sinks = subprocess.check_output(
                ["pactl", "list", "short", "sinks"], text=True
            )
            for line in sinks.splitlines():
                parts = line.split("\t")
                if len(parts) >= 5 and "RUNNING" in parts[4]:
                    return f"{parts[1]}.monitor"

            sink = subprocess.check_output(
                ["pactl", "get-default-sink"], text=True
            ).strip()
            return f"{sink}.monitor"
        except (OSError, subprocess.SubprocessError) as error:
            print(f"Audio monitor could not be determined: {error}", file=sys.stderr)
            return "@DEFAULT_MONITOR@"

    def _capture_audio(self):
        frame_size = 1024
        num_bars = 24
        agc_peak = 0.05

        while self.running:
            if self.track_info.get("status") != "playing":
                time.sleep(0.1)
                continue
            rate = self.sample_rate
            capture_samples = max(frame_size, round(rate * ANALYSIS_INTERVAL))
            monitor = self._get_default_monitor()
            try:
                with self._capture_lock:
                    if not self.running:
                        break
                    proc = subprocess.Popen(
                        [
                            "parec",
                            "--format=s16le",
                            f"--rate={rate}",
                            "--channels=1",
                            "--latency-msec=10",
                            "--process-time-msec=10",
                            f"--device={monitor}",
                        ],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                    )
                    self._capture_proc = proc
            except OSError as error:
                print(f"Audio capture could not be started: {error}", file=sys.stderr)
                time.sleep(1)
                continue
            last_check = time.time()
            audio_frame = np.empty(frame_size, dtype="<i2")
            try:
                while self.running and proc.poll() is None:
                    if self.sample_rate != rate:
                        break
                    if self.track_info.get("status") != "playing":
                        time.sleep(0.1)
                        continue
                    if time.time() - last_check > 2.0:
                        last_check = time.time()
                        if self._get_default_monitor() != monitor:
                            break

                    raw = proc.stdout.read(capture_samples * 2)
                    if not raw or len(raw) != capture_samples * 2:
                        break
                    incoming = np.frombuffer(raw, dtype="<i2")
                    audio_frame[:] = incoming[-frame_size:]

                    wave, bars, agc_peak = calculate_visualization(
                        audio_frame.tobytes(),
                        rate=rate,
                        num_bars=num_bars,
                        previous_bars=self.bars,
                        agc_peak=agc_peak,
                        hop_size=capture_samples,
                    )
                    self.wave = wave
                    self.bars = bars
            finally:
                with self._capture_lock:
                    if proc.poll() is None:
                        try:
                            proc.terminate()
                        except ProcessLookupError:
                            pass
                    self._capture_proc = None
                try:
                    proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()

    def get_state(self):
        track = self.track_info.copy()
        if track.get("position") is not None and track.get("duration") is not None:
            if track.get("status") == "playing":
                elapsed = max(0, time.monotonic() - track["positionUpdatedAt"])
                track["position"] += int(elapsed * track["playbackRate"] * 1_000_000)
            track["position"] = min(track["position"], track["duration"])
        selected_source = self.selected_source
        resolved_direction = self._resolved_edge_swirl_direction(track, selected_source)
        sources = [
            {"id": player["id"], "label": player["label"],
             "identity": player.get("identity", ""),
             "application": player.get("application", ""),
             "desktopEntry": player.get("desktop_entry", ""),
             "lastSelected": self.last_source_instances.get(player.get("application", "")) == player["id"],
             "status": player["track"].get("status", "")}
            for player in sorted(
                self._mpris_players.values(),
                key=lambda item: item["label"].casefold(),
            )
        ]
        return {
                "track": track,
                "bars": self.bars,
                "wave": self.wave,
                "sampleRate": self.sample_rate,
                "sourcePromptSeconds": self.source_prompt_seconds,
                "sourcePromptEnabled": self.source_prompt_enabled,
                "sampleRates": SAMPLE_RATES,
                "showPhotosensitivityWarning": not self.photosensitivity_warning_acknowledged,
                "sourceGains": self.source_volume.gains.copy(),
                "boostWarning": self.boost_warning,
                "visualizerMode": self.visualizer_mode,
                "scopeZeroLine": self.scope_zero_line,
                "scopeLineWidth": self.scope_line_width,
                "edgeSwirl": self.edge_swirl,
                "lightingMode": self.lighting_mode,
                "customPalettes": [{**palette, "colors": list(palette["colors"])}
                                   for palette in self.custom_palettes],
                "lightingColorMode": self.lighting_color_mode,
                "discoLightStyle": self.disco_light_style,
                "discoWireColor": self.disco_wire_color,
                "discoLightCount": self.disco_light_count,
                "discoLightSize": self.disco_light_size,
                "edgeSwirlDirection": self.edge_swirl_direction,
                "edgeSwirlResolvedDirection": resolved_direction,
                "edgeSwirlSlowdown": self.edge_swirl_slowdown,
                "edgeColorIntensity": self.edge_color_intensity,
                "edgeFade": self.edge_fade,
                "edgeDepth": self.edge_depth,
                "edgeOpacity": self.edge_opacity,
                "edgeColorCount": self.edge_color_count,
                "albumArtOpacity": self.album_art_opacity,
                "artworkMode": self.artwork_mode,
                "systemVolume": self.system_volume,
                "sources": sources,
                "selectedSource": selected_source,
            }

    def media_control(self, action):
        if self.selected_source.startswith("mpris:"):
            self._control_mpris_player(self.selected_source, action)
            return

        raise RuntimeError("No playback source is selected")

    def seek_track(self, source_id, track_id, position):
        if (source_id != self.selected_source or not source_id.startswith("mpris:")
                or not isinstance(position, int) or isinstance(position, bool)):
            raise ValueError("Playback source or position is no longer valid")
        track = self.track_info
        if (not track.get("canSeek") or track.get("trackId") != track_id
                or not 0 <= position <= track["duration"]):
            raise ValueError("Track is no longer available for seeking")
        bus_name = source_id[len("mpris:"):]
        if bus_name not in self._mpris_players:
            raise RuntimeError(f"Media player is no longer available: {bus_name}")
        obj = self.session_bus.get_object(bus_name, MPRIS_OBJECT_PATH, introspect=False)
        player = dbus.Interface(obj, MPRIS_PLAYER_INTERFACE)
        player.SetPosition(dbus.ObjectPath(track_id), dbus.Int64(position))
        if self.selected_source == source_id and self.track_info.get("trackId") == track_id:
            self.track_info["position"] = position
            self.track_info["positionUpdatedAt"] = time.monotonic()

    def _control_mpris_player(self, source_id, action):
        bus_name = source_id[len("mpris:"):]
        if bus_name not in self._mpris_players:
            raise RuntimeError(f"Media player is no longer available: {bus_name}")
        obj = self.session_bus.get_object(
            bus_name, MPRIS_OBJECT_PATH, introspect=False
        )
        player = dbus.Interface(obj, MPRIS_PLAYER_INTERFACE)
        getattr(player, action)()


