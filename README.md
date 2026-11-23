# AmbiBeat

A Qt music visualiser that reads media-player metadata and captures PulseAudio/PipeWire monitor audio.

## Requirements

AmbiBeat runs on Linux with Python 3.10 or later, a Qt-supported desktop session,
a session D-Bus, and a PulseAudio-compatible audio server (including PipeWire).
It uses NumPy, PySide6, the Python D-Bus and GObject bindings, and the `pactl`
and `parec` commands. Debian 13 has an installation helper below; other Linux
distributions may require different system package names. A Flatpak has not yet
been provided or validated.

## Development

On a vanilla Debian 13 installation, install the system dependencies manually:

```bash
sudo ./scripts/install-dependencies-debian13.sh
```

This installs Python's venv support, the system NumPy/D-Bus/GI bindings, and PulseAudio-compatible audio tools.

```bash
python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install --editable ".[dev]"
.venv/bin/python -m pytest
.venv/bin/ambibeat
```

The app uses Python D-Bus bindings and GI, plus the `pactl` and `parec` commands. On Debian/Ubuntu, install those system packages if they are missing:

```bash
sudo apt install python3-dbus python3-gi gir1.2-glib-2.0 pulseaudio-utils
```

## Qt application

Launch the standalone Qt visualiser:

```bash
./.venv/bin/ambibeat-qt
```

Before the visualiser opens, a photosensitivity warning follows the system theme and accessibility settings. Select **Remember my acknowledgement** to save it in that user's settings; otherwise the warning appears again on the next launch. **Show photosensitivity warning on startup** in Settings lets you change this later.

The Qt visualiser includes Bars, Scope, Halo, Time Vortex, and No visualisation. Move the pointer or press a key to reveal the controls. Click the left or right third of the visualiser to open visual effects or settings, respectively; double-click the middle third to cycle visualisations. Scope and Halo share a line thickness setting (25–300%, with 100% as the default); Scope also offers a zero reference line toggle.

The lower Qt controls, including the source menu and fullscreen button, appear above the track title and artist. The artist and album text matches the control border colour and has a dark shadow for contrast. When Qt starts, it selects a currently playing computer app if one is available, even if another source was saved; if that app has multiple instances, it prefers the one last selected in AmbiBeat when still available. Source selectors show app icons when available.

When another playback source starts playing, a card at the top offers to switch to it. Choose **Switch** or **Keep current**; if it expires, the current source remains selected. In Settings, **Show switch to new source message** enables the card and **Source switch prompt duration** sets its duration to 3–60 seconds (10 seconds by default). The duration control is disabled when the message is off. Source status is checked approximately every three seconds.

Percentage and prompt-duration sliders snap to multiples of 10 when dragged. Hold Shift while dragging, or use the arrow keys, for finer adjustments. Sliders for counts and gain retain their normal steps.

A track timeline appears above the controls and follows the selected colour theme. Click or drag it to seek when the selected player reports a duration, position and seek support; otherwise the selector is disabled.

Showing or hiding the controls does not resize or move the visual effects; the controls appear over them.

In Qt, artwork is centred on the screen in every visualisation mode. In Halo, selected artwork fades outward in a circle in response to bass, surrounded by a glowing scope-style outline that pulses with the bass while following individual frequency bands. The artwork opacity setting still applies; with no artwork, the halo retains its coloured glow.

Qt control borders use a bright colour derived from the selected artwork palette, reverting to their default appearance when no artwork colours are active.

In Qt, the visual-effects button opens a left-side menu; click outside it to close it. Changes in both panels apply immediately. Each panel has **Reset to defaults**; the settings reset restores all source boosts and app settings but leaves the selected source and system volume unchanged. Sliders ignore mouse-wheel input.

The visual-effects menu offers **Show Album Art**, **Show Logo**, and **No artwork**. The choice is saved. Visualisation colours come from the selected album art or logo; **No artwork** uses the default colours and a background that fades from blue in the centre to black at the edges.

The **Lighting** setting selects edge lighting, lights, or none, independently of the visualisation. The effects panel shows the controls relevant to the selected lighting mode. Lights are evenly spaced across the top and sides, using the current edge palette. **Light style** offers the original semicircular glow, fairy lights, classic bulbs and stars; the shaped lights have a gently curved wire that can be green, use the lighting colours, or be hidden with **Wireless**. With swirl off, the lights remain steady; with it on, they react to audio and different colours fade at different rates. During playback, quiet lights remain dimly lit; they fade out when playback pauses. **Light count** sets the total number of lights (3–40), and **Light size** sets their size (40–200%). **Lighting fade** controls their softness and decay, while the lighting opacity and vividness controls also apply.

**Lighting colours** selects artwork-derived, rainbow or Christmas colours for the border, lights and visualisations. Rainbow uses the default blue-to-black background and white Qt control borders; Christmas uses red, green and gold lighting with a festive background and matching controls.

**Border depth** controls how far edge lighting extends into the screen (25–200%); 100% is the standard depth. It does not change light size.

Qt's edge lighting fades around a circular centre, including on widescreen displays.

When hardware OpenGL is available, Qt uses it to render the visualiser and lighting. It automatically falls back to CPU rendering if OpenGL or the required shader is unavailable; the fallback reason is written to standard error.

While playback is active, the app requests system idle/sleep inhibition so the display stays on. The inhibition ends when playback stops or the app shuts down.
Use the fullscreen icon or press **F11** to toggle fullscreen; press **Esc** to exit.
Choose a visualisation from the visual-effects menu; the selected mode is saved automatically. With no visualisation, album art is shown prominently without the animated effects. Time Vortex is an original audio-reactive tunnel: its irregular rings ripple outward and fade into darkness, while the centre moves with bass beats and the spectrum's pitch balance.
Up to three distinct colors from the album art blend into a soft glow along the screen edges for Ambilight sampling; the default blue/purple edge glow is used when artwork is unavailable. When edge swirl is enabled, this glow itself flows around the screen in time with detected bass beats instead of adding a separate border.
Icon-only previous, play/pause, and next controls support the left/right arrows, Space, and media keys. Controls fade after 2.5 seconds of inactivity and reappear on pointer movement or keyboard input.
The visual-effects menu provides edge-colour vividness and speed controls (tempo-matched to 20× slower), and a switch to swirl edge colours or make lights react to music.
When edge swirl is enabled, **Border rotation direction** selects clockwise, counter-clockwise, or a randomly chosen direction for each track.
Use the playback-source menu or Settings to switch between MPRIS players in the current user session. Switching sources pauses other currently playing media players and starts the selected source.
The **Selected source volume boost** slider (1x–4x) remembers a separate level per source. It changes only identifiable audio streams belonging to that source, and restores their unboosted volume when switching sources or closing the app. If the app exits unexpectedly, it uses its recovery record to restore the volume on its next launch. Some media players use a separate process for audio; if their stream cannot be identified, the boost is not applied and a warning is shown in Settings. High settings may distort, so raise the level gradually. System volume is available in Settings and from the speaker control in the bottom media controls.
Settings are saved separately for each user in `${XDG_CONFIG_HOME:-~/.config}/ambibeat/settings.json`. The app remembers per-source boosts, the selected source, sample rate, visualiser mode and Scope zero line, artwork and lighting choices, edge-swirl setting and direction, edge-colour speed, and edge-colour vividness. Existing settings in the user's former `bt-audio-companion` directory are imported automatically. System volume is read from and managed by the audio server.
Artwork lookup uses the selected player's art URL, matching local artwork from other players, and catalog artwork. Catalog searches remove matching artist prefixes and official-audio/video markers from track titles. If a search for a collaboration finds no match, AmbiBeat also searches for its individual credited artists.

## Privacy and local data

When artwork needs a catalog lookup, AmbiBeat sends the track title and artist
to Deezer and Apple's iTunes Search API. It may also fetch artwork from URLs
provided by a media player or those catalogs. Album art is cached under
`/tmp/ambibeat-art`; settings and volume-recovery information are stored in the
current user's configuration directory described above. System audio volume
and selected-source boost affect the local audio server.

## Code layout

- `src/ambibeat/qt_frontend.py`: application startup, window, controls and rendering.
- `src/ambibeat/qt_panels.py`: slide-out controls and settings.
- `src/ambibeat/qt_gpu.py`: OpenGL edge rendering and GPU availability checks.
- `src/ambibeat/backend.py`: playback sources, artwork, audio capture and app state.
- `src/ambibeat/audio_analysis.py`: PCM waveform and spectrum analysis.
- `src/ambibeat/source_volume.py`: per-source boost and volume recovery.
- `src/ambibeat/assets/`: packaged application icons.
- `tests/`: backend, audio and Qt tests.

The Qt process owns the backend, its GLib event loop and audio-capture thread.
The backend exposes state and settings methods directly to Qt. On shutdown it
stops capture and idle inhibition and attempts to restore boosted streams.

## Contributing

Use the development setup above and run `.venv/bin/python -m pytest` before
submitting changes. Qt tests use an offscreen platform by default; tests that
require hardware OpenGL may be skipped when it is unavailable. Keep changes
focused, add tests for changed behaviour, and describe how changes were tested.
Do not include local configuration, virtual environments, logs, or personal
data in contributions. Check dependency updates and compatibility before
upgrading; dependencies are currently specified as minimum versions rather
than a reproducible lock file.

## Install

```bash
./scripts/install.sh
```

The installer creates a development virtual environment, installs the project in editable mode, and installs:

- `~/.local/bin/ambibeat`
- `~/.local/bin/ambibeat-qt`
- `~/.local/share/applications/ambibeat.desktop`

The installer also redirects the old `ambibeat-native` launcher to Qt. Launch AmbiBeat from the application menu or run `ambibeat`.

## License

AmbiBeat's source code is licensed under the GNU General Public License,
version 3 or any later version; see [LICENSE](LICENSE). The application icons
are by AjojoTec and may not be redistributed without AjojoTec's express
permission; see [icon attribution](src/ambibeat/assets/ATTRIBUTION.txt).
Third-party dependencies and downloaded artwork retain their own licenses.
