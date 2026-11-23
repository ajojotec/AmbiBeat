"""Convert PCM audio into waveform and spectrum data for the visualiser."""

from functools import lru_cache

import numpy as np


@lru_cache(maxsize=32)
def _visualization_analysis_setup(sample_count, rate, num_bars):
    window = np.hanning(sample_count)
    freqs = np.fft.rfftfreq(sample_count, 1.0 / rate)
    bin_edges = np.geomspace(40, min(14000, rate / 2), num_bars + 1)
    bin_ranges = tuple(
        (
            int(np.searchsorted(freqs, bin_edges[index], side="left")),
            int(np.searchsorted(freqs, bin_edges[index + 1], side="left")),
            float(np.sqrt(bin_edges[index] * bin_edges[index + 1])),
        )
        for index in range(num_bars)
    )
    return window, bin_ranges


def calculate_visualization(
    raw,
    rate=48000,
    num_bars=24,
    previous_bars=None,
    agc_peak=0.05,
    hop_size=1024,
    gain=1.0,
):
    """Convert one raw PCM chunk into 64 waveform samples and frequency bars."""
    if not raw:
        return [0.0] * 64, [0.0] * num_bars, agc_peak

    samples = np.frombuffer(raw, dtype="<i2").astype(np.float32)
    if samples.size == 0:
        return [0.0] * 64, [0.0] * num_bars, agc_peak
    samples /= 32768.0
    hop_ratio = hop_size / 1024.0

    frame_max = float(np.max(np.abs(samples)))
    if frame_max > 0.0005:
        agc_peak = max(frame_max, agc_peak * (0.992 ** hop_ratio))
    else:
        agc_peak = max(0.02, agc_peak * (0.995 ** hop_ratio))

    norm_samples = np.clip(samples / max(agc_peak, 0.01) * gain, -1.0, 1.0)

    if norm_samples.size >= 64:
        step = norm_samples.size // 64
        wave = [
            round(float(norm_samples[i * step]), 3)
            for i in range(64)
        ]
    else:
        wave = [
            round(float(norm_samples[min(i * norm_samples.size // 64, norm_samples.size - 1)]), 3)
            for i in range(64)
        ]

    window, bin_ranges = _visualization_analysis_setup(samples.size, rate, num_bars)
    fft = np.abs(np.fft.rfft(norm_samples * window))
    previous = previous_bars if previous_bars is not None else [0.0] * num_bars
    bars = []
    for i, (start, end, center) in enumerate(bin_ranges):
        if end > start:
            val = float(np.mean(fft[start:end]))
        else:
            # Narrow low-frequency bands can fall between FFT bins.
            position = center * samples.size / rate
            lower = min(int(position), len(fft) - 1)
            upper = min(lower + 1, len(fft) - 1)
            val = float(fft[lower] + (fft[upper] - fft[lower]) * (position - lower))
        tilt = 1.0 + (i / num_bars) * 1.8
        scaled = min(1.0, ((val * tilt) / 22.0) ** 0.75)
        smoothed = max(scaled, float(previous[i]) * (0.65 ** hop_ratio))
        bars.append(round(smoothed, 3))
    return wave, bars, agc_peak
