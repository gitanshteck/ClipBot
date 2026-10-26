"""The sound of the HUD: short synthesised cues for the motion graphics (counting ticks, a lock, a title slam).

Made from nothing - sines, noise and envelopes - so they are CC0 by construction and fit the graphics' timing
exactly. Each cue is built so its *loudest moment* is the moment the graphic lands: the engine places a sound
effect by its peak (`align: peak`), so `hud_contact` is a run of ticks that decelerate into a lock and the lock
is the peak, and the Contact graphic's hit is where the lock falls. `CUES` lists them; `write_library` writes
them as WAVs (with the sidecar the library reads) into the library's `sfx/` folder.

Deterministic: noise comes from a seeded generator, so a cue's bytes - and therefore its library id, which is a
hash of the file - are the same on every machine with the same numpy.
"""

import json
import wave
from pathlib import Path
from typing import Any, Dict, List

import numpy as np

SR = 48000
LICENSE = {"name": "CC0-1.0", "author": "ClipBot (synthesised)", "source": "clipbot/hudsounds.py",
           "url": "https://creativecommons.org/publicdomain/zero/1.0/"}


def _t(dur: float) -> np.ndarray:
    return np.arange(int(round(dur * SR))) / SR


def _noise(dur: float, seed: int) -> np.ndarray:
    return np.random.default_rng(seed).standard_normal(int(round(dur * SR)))


def _bandpass(x: np.ndarray, lo: float, hi: float) -> np.ndarray:
    """A smooth-edged band in the frequency domain (no scipy: numpy is already a dependency)."""
    spec = np.fft.rfft(x)
    f = np.fft.rfftfreq(len(x), 1.0 / SR)
    gain = np.ones_like(f)
    if lo > 0:
        gain *= 1.0 / (1.0 + (lo / np.maximum(f, 1e-9)) ** 4)
    if hi < SR / 2:
        gain *= 1.0 / (1.0 + (f / hi) ** 4)
    return np.fft.irfft(spec * gain, len(x))


def _sweep_noise(dur: float, f0: float, f1: float, seed: int, width: float = 0.6) -> np.ndarray:
    """Noise whose pass band slides from f0 to f1 (a whoosh): the noise is cut into short blocks, each band-passed
    at its own centre, and cross-faded."""
    x = _noise(dur, seed)
    n = len(x)
    blocks = 24
    out = np.zeros(n)
    edges = np.linspace(0, n, blocks + 1).astype(int)
    for i in range(blocks):
        c = f0 * (f1 / f0) ** ((i + 0.5) / blocks)
        a, b = max(0, edges[i] - 800), min(n, edges[i + 1] + 800)
        seg = _bandpass(x[a:b], c * (1 - width), min(c * (1 + width), SR / 2 - 100))
        win = np.ones(b - a)
        ramp = min(800, (b - a) // 2)
        win[:ramp] = np.linspace(0, 1, ramp)
        win[-ramp:] = np.linspace(1, 0, ramp)
        out[a:b] += seg * win
    return out / (np.abs(out).max() + 1e-9)


def _place(buf: np.ndarray, x: np.ndarray, at: float, gain: float = 1.0) -> None:
    i = int(round(at * SR))
    n = min(len(x), len(buf) - i)
    if n > 0 and i >= 0:
        buf[i:i + n] += x[:n] * gain


def tick(freq: float = 2200.0, dur: float = 0.045, seed: int = 0) -> np.ndarray:
    t = _t(dur)
    body = np.sin(2 * np.pi * freq * t) * np.exp(-t / 0.007)
    click = _bandpass(_noise(dur, seed), 2500, 9000) * np.exp(-t / 0.0015) * 0.8
    return body + click


def thud(dur: float = 0.6, f0: float = 150.0, f1: float = 40.0, decay: float = 0.16) -> np.ndarray:
    t = _t(dur)
    freq = f1 + (f0 - f1) * np.exp(-t / 0.07)
    phase = 2 * np.pi * np.cumsum(freq) / SR
    return np.sin(phase) * np.exp(-t / decay)


def ping(freq: float, dur: float = 0.6, decay: float = 0.16, detune: float = 1.0) -> np.ndarray:
    t = _t(dur)
    return (np.sin(2 * np.pi * freq * t) + 0.45 * np.sin(2 * np.pi * freq * 2.76 * detune * t)) * np.exp(-t / decay)


def _norm(x: np.ndarray, peak: float = 0.85) -> np.ndarray:
    return x / (np.abs(x).max() + 1e-9) * peak


def _fade_out(x: np.ndarray, ms: float = 8.0) -> np.ndarray:
    n = min(len(x), int(ms * SR / 1000))
    x = x.copy()
    x[-n:] *= np.linspace(1, 0, n)
    return x


# --------------------------------------------------------------------------
# the cues
# --------------------------------------------------------------------------


def hud_contact() -> np.ndarray:
    """A contact plate: ticks that count up and slow, then the lock. The lock (the peak) falls at 0.55 s, where
    the Contact graphic's hit is."""
    buf = np.zeros(int(1.25 * SR))
    n = 16
    for i in range(n):
        at = 0.06 + 0.46 * (1 - (1 - i / (n - 1)) ** 2)            # dense at first, spreading as the count slows
        _place(buf, tick(1500 + 70 * i, seed=i), at, 0.18 + 0.012 * i)
    lock = 0.55
    _place(buf, thud(0.7, 160, 46, 0.2), lock, 1.0)
    _place(buf, ping(2900, 0.55, 0.12), lock, 0.30)
    _place(buf, _bandpass(_noise(0.05, 99), 1800, 8000) * np.exp(-_t(0.05) / 0.01), lock, 0.5)
    return _norm(_fade_out(buf))


def hud_slam() -> np.ndarray:
    """The series title: a riser that builds for a second and a boom that lands on it (the peak, at 1.0 s)."""
    buf = np.zeros(int(2.6 * SR))
    t = _t(1.0)
    swell = _sweep_noise(1.0, 250, 7000, 7) * (t / 1.0) ** 2.2
    rise = np.sin(2 * np.pi * np.cumsum(180 + 1500 * (t / 1.0) ** 2) / SR) * (t / 1.0) ** 3
    _place(buf, swell * 0.42 + rise * 0.18, 0.0)
    boom = thud(1.6, 110, 30, 0.5)
    _place(buf, boom, 1.0, 1.0)
    _place(buf, _bandpass(_noise(0.4, 3), 60, 3000) * np.exp(-_t(0.4) / 0.07), 1.0, 0.55)
    _place(buf, ping(660, 1.3, 0.35, 1.01), 1.0, 0.10)
    return _norm(_fade_out(buf, 40))


def hud_op() -> np.ndarray:
    """An operation header: a short whoosh up into a soft thump on the title's reveal (peak at 0.5 s)."""
    buf = np.zeros(int(1.1 * SR))
    t = _t(0.5)
    _place(buf, _sweep_noise(0.5, 400, 5000, 11) * (t / 0.5) ** 1.6, 0.0, 0.5)
    _place(buf, thud(0.5, 190, 60, 0.12), 0.5, 1.0)
    _place(buf, tick(1900, seed=5), 0.5, 0.35)
    return _norm(_fade_out(buf, 30))


def hud_ratchet() -> np.ndarray:
    """The stats card: six groups of counting ticks, each a little higher than the last, spaced like the six
    tiles' count-ups (a group every 0.22 s, each 0.95 s long)."""
    buf = np.zeros(int((5 * 0.22 + 0.95 + 0.2) * SR))
    for g in range(6):
        start = 0.22 * g
        n = 11
        for i in range(n):
            at = start + 0.9 * (1 - (1 - i / (n - 1)) ** 2.2)
            _place(buf, tick(1300 * 2 ** (g / 12.0) + 40 * i, seed=g * 20 + i), at, 0.16 + 0.015 * i)
        _place(buf, thud(0.3, 130, 55, 0.08), start + 0.95, 0.35)
    return _norm(_fade_out(buf))


def hud_stamp() -> np.ndarray:
    """The OK stamp: a hard thud with a short metallic ring (peak at the start)."""
    buf = np.zeros(int(0.9 * SR))
    _place(buf, thud(0.5, 200, 55, 0.1), 0.0, 1.0)
    _place(buf, ping(1320, 0.8, 0.2, 1.03), 0.0, 0.22)
    _place(buf, _bandpass(_noise(0.06, 17), 800, 7000) * np.exp(-_t(0.06) / 0.012), 0.0, 0.6)
    return _norm(_fade_out(buf, 30))


def hud_sweep() -> np.ndarray:
    """A scan across a cut: noise sliding up and back down (peak in the middle)."""
    t = _t(0.7)
    up = _sweep_noise(0.7, 300, 6500, 21)
    env = np.sin(np.pi * t / 0.7) ** 1.5
    return _norm(_fade_out(up * env), 0.8)


def hud_blip() -> np.ndarray:
    """A chip or tag appearing: a two-note chirp."""
    buf = np.zeros(int(0.25 * SR))
    _place(buf, tick(1900, 0.06, seed=1), 0.0, 0.7)
    _place(buf, tick(2700, 0.07, seed=2), 0.07, 1.0)
    return _norm(_fade_out(buf, 10), 0.7)


CUES: Dict[str, Dict[str, Any]] = {
    "hud_contact": {"fn": hud_contact, "name": "HUD Contact", "tags": ["hud", "contact", "lock", "count"],
                    "about": "ticks counting up into a lock; the peak is the lock, 0.55 s in"},
    "hud_slam": {"fn": hud_slam, "name": "HUD Title Slam", "tags": ["hud", "title", "boom", "riser"],
                 "about": "a one-second riser and a boom (peak at 1.0 s)"},
    "hud_op": {"fn": hud_op, "name": "HUD Operation", "tags": ["hud", "title", "whoosh"],
               "about": "a short whoosh into a soft thump (peak at 0.5 s)"},
    "hud_ratchet": {"fn": hud_ratchet, "name": "HUD Ratchet", "tags": ["hud", "stats", "count"],
                    "about": "six counting groups, a group every 0.22 s"},
    "hud_stamp": {"fn": hud_stamp, "name": "HUD Stamp", "tags": ["hud", "stamp", "thud"],
                  "about": "a hard thud with a metallic ring (peak at the start)"},
    "hud_sweep": {"fn": hud_sweep, "name": "HUD Sweep", "tags": ["hud", "whoosh", "transition"],
                  "about": "a scan of noise up and down across a cut"},
    "hud_blip": {"fn": hud_blip, "name": "HUD Blip", "tags": ["hud", "blip", "chip"], "about": "a two-note chirp"},
}


def write_wav(path: Path, mono: np.ndarray) -> None:
    pcm = (np.clip(mono, -1, 1) * 32767).astype("<i2")
    stereo = np.repeat(pcm[:, None], 2, axis=1)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(stereo.tobytes())


def write_library(sfx_dir: Path) -> List[str]:
    """Write every cue into `sfx_dir` as `<id>.wav` plus the sidecar the library reads; returns the file names."""
    sfx_dir = Path(sfx_dir)
    sfx_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for cid, cue in CUES.items():
        path = sfx_dir / (cid + ".wav")
        write_wav(path, cue["fn"]())
        side = {"name": cue["name"], "tags": cue["tags"], "license": LICENSE, "about": cue["about"]}
        Path(str(path) + ".json").write_text(json.dumps(side, indent=2), encoding="utf-8")
        written.append(path.name)
    return written
