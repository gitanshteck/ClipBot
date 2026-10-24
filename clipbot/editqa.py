"""Verify and inspect a rendered edit: the editor's eyes and ears.

A render that finishes is not a render that is right: an edit can be the wrong
length, a frame out of sync with its sound, ten decibels too quiet, pop at a join,
or open on black, and none of that fails the ffmpeg exit code. `verify` runs the
checks that catch it, and `inspect` writes the artefacts a person (or an AI that
cannot watch video) reads instead: a storyboard of every join, a loudness
waveform with the cuts marked, and a plain report.

The rule from github.com/ranahaani/i-hate-editing that this module exists to
honour (MIT): *inspect the output before presenting it*. Every check here reads
the file that was actually written, never the plan it was rendered from.
"""

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import editaudio, editframe, editoverlay, editscene
from .utils import StageError, get_logger

log = get_logger(__name__)

# A join is "clicky" past this ratio of sample jump to local RMS (see
# editaudio.click_score). Programme audio sits in single digits.
CLICK_LIMIT = 14.0
MOMENT_LAG = 0.6      # seconds after a marker: the callout has landed
MAX_MOMENTS = 64      # four contact sheets
JOIN_WINDOW = 1.5     # seconds of sound either side of a join that its level is judged on
JUMP_LIMIT_DB = 9.0   # a louder-or-quieter step than this across a join is flagged
STATIC_MIN = 3.0      # seconds of picture that barely moves before it is called dead
STATIC_MOVING = 0.02  # fraction of a 160x90 frame whose pixels change by > 12 between samples (4/s)
WEBCAM_TOL = 0.02     # the webcam box may sit this far (frame fractions) from where the camera should put it


# --------------------------------------------------------------------------
# probing
# --------------------------------------------------------------------------


def probe(ffprobe: str, path: Any) -> Dict[str, Any]:
    """Container facts about a render: size, fps, per-stream duration and frame count."""
    proc = subprocess.run(
        [ffprobe, "-v", "error", "-count_packets", "-show_entries",
         "stream=codec_type,codec_name,width,height,r_frame_rate,avg_frame_rate,duration,nb_read_packets,sample_rate,channels"
         ":format=duration,size,bit_rate", "-of", "json", str(path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, encoding="utf-8", errors="replace",
        timeout=300)
    if proc.returncode != 0:
        raise StageError("ffprobe could not read {0}: {1}".format(path, proc.stderr.strip()[-300:]))
    data = json.loads(proc.stdout or "{}")
    out: Dict[str, Any] = {"format": data.get("format", {})}
    for s in data.get("streams", []):
        out[s["codec_type"]] = s
    return out


def _fps(stream: Dict[str, Any]) -> float:
    try:
        n, d = (stream.get("r_frame_rate") or "0/1").split("/")
        return float(n) / float(d)
    except (ValueError, ZeroDivisionError):
        return 0.0


# --------------------------------------------------------------------------
# ffmpeg detectors
# --------------------------------------------------------------------------


def _stderr(ffmpeg: str, args: Sequence[str]) -> str:
    proc = subprocess.run([ffmpeg, "-hide_banner", "-nostats", "-nostdin"] + list(args),
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
                          encoding="utf-8", errors="replace", timeout=1800)
    return proc.stderr or ""


def black_frames(ffmpeg: str, path: Any, min_dur: float = 0.2) -> List[Dict[str, float]]:
    text = _stderr(ffmpeg, ["-i", str(path), "-vf", "blackdetect=d={0}:pix_th=0.10".format(min_dur), "-an", "-f", "null", "-"])
    return [{"start": float(a), "end": float(b), "dur": float(c)} for a, b, c in
            re.findall(r"black_start:([\d.]+)\s+black_end:([\d.]+)\s+black_duration:([\d.]+)", text)]


def frozen_frames(ffmpeg: str, path: Any, min_dur: float = 1.0) -> List[Dict[str, float]]:
    text = _stderr(ffmpeg, ["-i", str(path), "-vf", "freezedetect=n=-55dB:d={0}".format(min_dur), "-an", "-f", "null", "-"])
    starts = [float(x) for x in re.findall(r"freeze_start: ([\d.]+)", text)]
    durs = [float(x) for x in re.findall(r"freeze_duration: ([\d.]+)", text)]
    return [{"start": s, "dur": d, "end": s + d} for s, d in zip(starts, durs)]


def silences(ffmpeg: str, path: Any, min_dur: float = 1.5, floor_db: float = -50.0) -> List[Dict[str, float]]:
    text = _stderr(ffmpeg, ["-i", str(path), "-af", "silencedetect=n={0}dB:d={1}".format(floor_db, min_dur), "-vn", "-f", "null", "-"])
    out = []
    for a, b in re.findall(r"silence_end: ([\d.]+) \| silence_duration: ([\d.]+)", text):
        end, dur = float(a), float(b)
        out.append({"start": end - dur, "end": end, "dur": dur})
    return out


def static_stretches(ffmpeg: str, path: Any, min_dur: float = STATIC_MIN, moving: float = STATIC_MOVING,
                     rate: int = 4) -> List[Dict[str, float]]:
    """Stretches of at least `min_dur` seconds where the picture barely moves.

    `freezedetect` only fires on a byte-for-byte frozen frame, and a stream overlay never is
    (chat scrolls, a webcam breathes), so a loading screen with a small webcam sails through
    it. This samples the picture at `rate` per second at 160x90 and counts the share of
    pixels that changed by more than 12 grey levels since the previous sample; a run of
    samples under `moving` is a dead stretch. Found on a real episode: a menu, a black
    screen and a privacy-policy page, each 8-15 s, all passing `frozen picture`.
    [{start, end, dur, moving}] where `moving` is the stretch's median share.
    """
    import numpy as np

    w, h = 160, 90
    proc = subprocess.Popen(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(path), "-an",
         "-vf", "fps={0},scale={1}:{2},format=gray".format(rate, w, h), "-f", "rawvideo", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    shares: List[float] = []
    prev = None
    size = w * h
    while True:
        buf = proc.stdout.read(size)
        if len(buf) < size:
            break
        cur = np.frombuffer(buf, dtype=np.uint8).astype(np.int16)
        if prev is not None:
            shares.append(float((np.abs(cur - prev) > 12).mean()))
        prev = cur
    proc.stdout.close()
    proc.wait()
    return stretches_from_shares(shares, rate, min_dur, moving)


def stretches_from_shares(shares: Sequence[float], rate: int, min_dur: float, moving: float) -> List[Dict[str, float]]:
    """The pure half of `static_stretches`: sample i is the change between sample i and i+1."""
    import numpy as np

    out: List[Dict[str, float]] = []
    i, n = 0, len(shares)
    while i < n:
        if shares[i] >= moving:
            i += 1
            continue
        j = i
        while j + 1 < n and shares[j + 1] < moving:
            j += 1
        dur = (j - i + 1) / float(rate)
        if dur >= min_dur:
            out.append({"start": i / float(rate), "end": (j + 1) / float(rate), "dur": dur,
                        "moving": float(np.median(shares[i:j + 1]))})
        i = j + 1
    return out


def join_jumps(samples: Any, sample_rate: int, timeline: Dict[str, Any], window: float = JOIN_WINDOW,
               limit_db: float = JUMP_LIMIT_DB) -> List[Dict[str, Any]]:
    """Joins where the sound steps up or down by more than `limit_db` across the cut.

    Level on each side is the mean of its loudest 40% of 10 ms frames over `window` seconds (how
    loud it sounds while something is going on, not what its pauses average - the measure
    `editcut.clip_level` matches clips with). Joins touching a card are skipped: a title card is
    meant to change the sound. [{clip, t, jump_db}], worst first.
    """
    import numpy as np

    from . import editcut

    times, db = editcut.envelope(samples, sample_rate)
    if len(times) == 0:
        return []
    cards = {c["id"] for c in timeline["clips"] if c["type"] == "card"}
    clips = timeline["clips"]
    out: List[Dict[str, Any]] = []

    def level(a: float, b: float) -> Optional[float]:
        seg = db[(times >= a) & (times < b)]
        if len(seg) < 20:
            return None
        return float(np.mean(np.sort(seg)[int(len(seg) * 0.6):]))

    for prev, c in zip(clips, clips[1:]):
        if c["id"] in cards or prev["id"] in cards:
            continue
        t = c["start"]
        before, after = level(max(0.0, t - window), t), level(t, min(t + window, timeline["duration"]))
        if before is None or after is None:
            continue
        jump = after - before
        if abs(jump) > limit_db:
            out.append({"clip": c["id"], "t": round(t, 2), "jump_db": round(jump, 1)})
    return sorted(out, key=lambda j: -abs(j["jump_db"]))


def loudness(ffmpeg: str, path: Any) -> Optional[Dict[str, float]]:
    """Integrated loudness (LUFS), true peak (dBTP) and range (LU) of the file's audio."""
    text = _stderr(ffmpeg, ["-i", str(path), "-af", "loudnorm=I=-14:TP=-1:LRA=11:print_format=json", "-vn", "-f", "null", "-"])
    i, j = text.rfind("{"), text.rfind("}")
    if i < 0 or j < i:
        return None
    try:
        raw = json.loads(text[i:j + 1])
        return {"lufs": float(raw["input_i"]), "true_peak": float(raw["input_tp"]), "lra": float(raw["input_lra"])}
    except (ValueError, KeyError):
        return None


# --------------------------------------------------------------------------
# artefacts
# --------------------------------------------------------------------------


def _webcam_times(entry: Dict[str, Any], per_clip: int) -> List[float]:
    """Clip seconds worth looking at for the webcam: while the picture is zoomed, away from the clip's
    edges (a join may be blending two frames there) and from a layout change (the box is in transit)."""
    dur = entry["dur"]
    keys = entry.get("camera") or []
    lo, hi = 0.5, dur - 0.5
    if hi <= lo or not any(k["zoom"] > 1.02 for k in keys):
        return []
    bounds = [s["t0"] for s in (entry.get("scene") or [])[1:]]
    cand = [k["t"] for k in keys if k["zoom"] > 1.02]
    cand += [(a["t"] + b["t"]) / 2.0 for a, b in zip(keys, keys[1:]) if min(a["zoom"], b["zoom"]) > 1.02]
    cand = sorted({round(min(max(t, lo), hi), 3) for t in cand
                   if editframe.camera_at(keys, min(max(t, lo), hi))[0] > 1.02})
    cand = [t for t in cand if all(abs(t - b) > 0.2 for b in bounds)]
    if len(cand) <= per_clip:
        return cand
    step = (len(cand) - 1) / float(per_clip - 1) if per_clip > 1 else 0.0
    return [cand[int(round(i * step))] for i in range(per_clip)]


def webcam_in_frame(ffmpeg: str, render: Any, timeline: Dict[str, Any], per_clip: int = 3) -> List[Dict[str, Any]]:
    """For every clip that zooms over a webcam, read frames of the render and look for the box.

    Where the box is expected comes from the clip's scene map and camera keys
    (`editframe.box_in_output`); a frame passes if the same detector that mapped the scene finds
    the box within `WEBCAM_TOL` of it - whole, not cropped by the frame edge, which it could not
    be found as. Returns one {clip, checked, matched, misses[(second, found, expected)]} per clip.
    """
    out: List[Dict[str, Any]] = []
    for e in timeline.get("clips", []):
        if e.get("type") != "clip" or not e.get("scene"):
            continue
        res = {"clip": e["id"], "checked": 0, "matched": 0, "misses": []}
        for t in _webcam_times(e, per_clip):
            box = editscene.box_at(e["scene"], t)
            if box is None:
                continue
            z, x, y = editframe.camera_at(e["camera"], t)
            want = editframe.box_in_output(box, z, x, y)
            T = e["start"] + t
            try:
                _, frame = next(editscene.read_frames(ffmpeg, render, T, T + 0.15, 30.0))
            except StopIteration:
                continue
            found = editscene.find_cam_box(frame)
            res["checked"] += 1
            if found is not None and all(abs(found[k] - want[k]) <= WEBCAM_TOL for k in ("x", "y", "w", "h")):
                res["matched"] += 1
            else:
                res["misses"].append((round(T, 2), found, {k: round(v, 3) for k, v in want.items()}))
        if res["checked"]:
            out.append(res)
    return out


def webcam_sheet(ffmpeg: str, render: Any, timeline: Dict[str, Any], out_dir: Path, cols: int = 4, thumb_w: int = 480) -> List[str]:
    """Contact sheets of one frame per zoomed clip, each with the box where the camera should have put the
    webcam drawn on it in magenta: if the magenta frame is not the real one, the camera is wrong. A person (or an
    AI that cannot watch video) reads this instead of the numbers."""
    from PIL import Image, ImageDraw

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tiles = []
    for e in timeline.get("clips", []):
        if e.get("type") != "clip" or not e.get("scene"):
            continue
        times = _webcam_times(e, 1) or [e["dur"] / 2.0]
        t = times[0]
        try:
            _, frame = next(editscene.read_frames(ffmpeg, render, e["start"] + t, e["start"] + t + 0.15, 30.0, (thumb_w, thumb_w * 9 // 16)))
        except StopIteration:
            continue
        im = Image.fromarray(frame)
        d = ImageDraw.Draw(im)
        box = editscene.box_at(e["scene"], t)
        z, x, y = editframe.camera_at(e.get("camera") or [], t)
        if box is not None:
            r = editframe.box_in_output(box, z, x, y)
            d.rectangle([r["x"] * im.width, r["y"] * im.height, (r["x"] + r["w"]) * im.width, (r["y"] + r["h"]) * im.height],
                        outline=(255, 0, 255), width=2)
        d.text((4, 3), "{0} {1:.1f}s zoom {2:.2f}{3}".format(e["id"], e["start"] + t, z, "" if box else " (no webcam)"), fill=(255, 220, 80))
        tiles.append(im)
    sheets: List[str] = []
    per = cols * 6
    for s0 in range(0, len(tiles), per):
        group = tiles[s0:s0 + per]
        tw, th = group[0].size
        sheet = Image.new("RGB", (cols * tw, ((len(group) + cols - 1) // cols) * th), (0, 0, 0))
        for k, im in enumerate(group):
            sheet.paste(im, ((k % cols) * tw, (k // cols) * th))
        f = out_dir / "webcam_{0:02d}.jpg".format(len(sheets) + 1)
        sheet.save(f, quality=82)
        sheets.append(str(f))
    return sheets


def _extract_frame(ffmpeg: str, path: Any, t: float, out_png: Path, width: int) -> bool:
    out_png.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-ss", "{0:.3f}".format(max(0.0, t)), "-i", str(path),
         "-frames:v", "1", "-vf", "scale={0}:-2".format(width), str(out_png)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    return proc.returncode == 0 and out_png.exists()


def storyboard(ffmpeg: str, path: Any, shots: Sequence[Tuple[float, str]], out_dir: Path, name: str = "board",
               cols: int = 4, rows: int = 4, thumb_w: int = 480) -> List[str]:
    """Contact sheets of labelled frames. `shots` is (time, caption); each sheet holds
    cols*rows frames, so 40 joins become a handful of images that can be read in
    one look instead of 40 files."""
    from PIL import Image, ImageDraw, ImageFont

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_dir / "_frames"
    fpath = editoverlay.find_font()
    font = ImageFont.truetype(fpath, 16) if fpath else ImageFont.load_default()
    sheets: List[str] = []
    per = cols * rows
    for s0 in range(0, len(shots), per):
        group = shots[s0:s0 + per]
        tiles = []
        for k, (t, cap) in enumerate(group):
            png = tmp / "{0}_{1:04d}.png".format(name, s0 + k)
            if _extract_frame(ffmpeg, path, t, png, thumb_w):
                im = Image.open(png).convert("RGB")
                d = ImageDraw.Draw(im)
                label = "{0:6.2f}s {1}".format(t, cap)[:46]
                d.rectangle((0, 0, im.width, 22), fill=(0, 0, 0))
                d.text((4, 3), label, fill=(255, 220, 80), font=font)
                tiles.append(im)
        if not tiles:
            continue
        tw, th = tiles[0].size
        n_rows = (len(tiles) + cols - 1) // cols
        sheet = Image.new("RGB", (cols * tw, n_rows * th), (0, 0, 0))
        for k, im in enumerate(tiles):
            sheet.paste(im, ((k % cols) * tw, (k // cols) * th))
        out = out_dir / "{0}_{1:02d}.jpg".format(name, len(sheets) + 1)
        sheet.save(out, quality=82)
        sheets.append(str(out))
    for f in tmp.glob("*.png"):
        f.unlink()
    try:
        tmp.rmdir()
    except OSError:
        pass
    return sheets


def waveform_png(samples, sample_rate: int, out_png: Path, markers: Sequence[Tuple[float, str]] = (),
                 target_lufs: Optional[float] = None, width: int = 1800, height: int = 360) -> str:
    """A loudness-over-time picture (short-term RMS, dBFS) with cuts and events marked.

    Quiet stretches, a clipped section, a bed that never ducks and a cut that lands
    in the middle of a loud hit are all visible at a glance, which is the point: an
    editor that cannot listen needs to see.
    """
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont

    out_png = Path(out_png)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (width, height), (16, 16, 20))
    d = ImageDraw.Draw(img)
    fpath = editoverlay.find_font()
    font = ImageFont.truetype(fpath, 13) if fpath else ImageFont.load_default()
    n = len(samples)
    dur = n / float(sample_rate) if n else 0.0
    if dur <= 0:
        img.save(out_png)
        return str(out_png)
    floor_db, top_db = -60.0, 0.0

    def y_of(db: float) -> float:
        return height - 20 - (min(top_db, max(floor_db, db)) - floor_db) / (top_db - floor_db) * (height - 40)

    for db in (-6, -12, -20, -30, -40, -50):
        d.line((0, y_of(db), width, y_of(db)), fill=(40, 40, 48))
        d.text((4, y_of(db) - 14), "{0} dB".format(db), fill=(90, 90, 110), font=font)
    if target_lufs is not None:
        y = y_of(target_lufs - 8.0)  # programme RMS runs ~8 dB below its integrated LUFS
        d.line((0, y, width, y), fill=(60, 120, 80))
    step = max(1, int(sample_rate * dur / width))
    sq = samples.astype(np.float64) ** 2
    for px in range(width):
        a = px * step
        if a >= n:
            break
        rms = float(np.sqrt(np.mean(sq[a:a + step]))) if a + step > a else 0.0
        peak = float(np.max(np.abs(samples[a:a + step])))
        y_r = y_of(20 * np.log10(max(rms, 1e-6)))
        y_p = y_of(20 * np.log10(max(peak, 1e-6)))
        d.line((px, height - 20, px, y_r), fill=(70, 150, 255))
        d.line((px, y_r, px, y_p), fill=(150, 90, 90) if peak < 0.98 else (255, 60, 60))
    for t, label in markers:
        x = t / dur * width
        d.line((x, 0, x, height - 20), fill=(255, 200, 60))
        d.text((x + 2, 2 + 12 * (hash(label) % 3)), label[:18], fill=(255, 200, 60), font=font)
    for k in range(0, int(dur) + 1, max(1, int(dur / 12) or 1)):
        d.text((k / dur * width, height - 16), "{0}s".format(k), fill=(130, 130, 150), font=font)
    img.save(out_png)
    return str(out_png)


# --------------------------------------------------------------------------
# verify
# --------------------------------------------------------------------------


def verify(ffmpeg: str, ffprobe: str, render: Any, timeline: Optional[Dict[str, Any]], master: Optional[Dict[str, Any]],
          out_dir: Path, deep: bool = True) -> Dict[str, Any]:
    """Run every check on a rendered file and write the inspection artefacts.

    Returns {ok, checks[{name, status, detail}], artifacts{...}}. Status is `pass`,
    `warn` or `fail`; `ok` is False only on a fail. `timeline` is the compiled
    timeline the file was rendered from (its joins and events are marked on the
    artefacts and its length is the one the file must match); without it only the
    file itself is checked.
    """
    import numpy as np

    render = Path(render)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    checks: List[Dict[str, str]] = []

    def add(name: str, status: str, detail: str) -> None:
        checks.append({"name": name, "status": status, "detail": detail})

    info = probe(ffprobe, render)
    v, a = info.get("video"), info.get("audio")
    if not v:
        add("video stream", "fail", "the file has no video")
        return {"ok": False, "checks": checks, "artifacts": {}}
    fps = _fps(v)
    frames = int(v.get("nb_read_packets") or 0)
    vdur = float(v.get("duration") or info["format"].get("duration") or 0.0)
    add("picture", "pass", "{0}x{1} at {2:g} fps, {3} frames, {4:.2f} s".format(v["width"], v["height"], fps, frames, vdur))

    if not a:
        add("audio stream", "fail", "the file has no audio")
        adur = 0.0
    else:
        adur = float(a.get("duration") or 0.0)
        add("sound", "pass", "{0} {1} Hz, {2} ch, {3:.2f} s".format(a["codec_name"], a.get("sample_rate"), a.get("channels"), adur))

    one_frame = 1.0 / fps if fps else 0.05
    if a and abs(vdur - adur) > 1.5 * one_frame:
        add("A/V length", "fail", "picture is {0:.3f} s and sound {1:.3f} s - {2:+.0f} ms out of step".format(
            vdur, adur, (vdur - adur) * 1000))
    elif a:
        add("A/V length", "pass", "picture and sound agree to {0:.0f} ms".format(abs(vdur - adur) * 1000))

    if timeline:
        want = timeline["duration"]
        if abs(vdur - want) > 1.5 * one_frame:
            add("length vs plan", "fail", "the timeline says {0:.3f} s, the file is {1:.3f} s".format(want, vdur))
        else:
            add("length vs plan", "pass", "{0:.3f} s as planned".format(want))

    # loudness
    lm = loudness(ffmpeg, render) if a else None
    if lm:
        target = master["loudness_lufs"] if master and master.get("normalize") else None
        tp_limit = (master["true_peak_db"] + 0.5) if master else -0.5
        if target is not None and abs(lm["lufs"] - target) > 1.5:
            add("loudness", "warn", "{0:.1f} LUFS; the target is {1:.1f}".format(lm["lufs"], target))
        else:
            add("loudness", "pass", "{0:.1f} LUFS, {1:.1f} LU range".format(lm["lufs"], lm["lra"]))
        if lm["true_peak"] > tp_limit:
            add("true peak", "warn", "{0:.1f} dBTP is over the {1:.1f} ceiling".format(lm["true_peak"], tp_limit))
        else:
            add("true peak", "pass", "{0:.1f} dBTP".format(lm["true_peak"]))

    # picture and sound dropouts
    if deep:
        black = black_frames(ffmpeg, render)
        planned_dip = [c for c in (timeline or {}).get("clips", []) if c["transition_in"]["type"] == "dip"]
        # a title card is mostly dark and still by design: neither check applies inside one
        cards = [(c["start"], c["end"]) for c in (timeline or {}).get("clips", []) if c["type"] == "card"]

        def in_card(a: float, b: float) -> bool:
            return any(a >= c0 - 0.15 and b <= c1 + 0.15 for c0, c1 in cards)

        stray = [b for b in black if not any(abs(b["start"] - c["start"]) < 1.0 for c in planned_dip)
                 and not in_card(b["start"], b["end"])]
        if stray:
            add("black frames", "warn", "{0} stretch(es), first at {1:.2f} s for {2:.2f} s".format(len(stray), stray[0]["start"], stray[0]["dur"]))
        else:
            add("black frames", "pass", "none outside planned dips")
        frozen = [f for f in frozen_frames(ffmpeg, render) if not in_card(f["start"], f["end"])]
        if frozen:
            add("frozen picture", "warn", "{0} stretch(es) with no motion, first {1:.2f}-{2:.2f} s".format(
                len(frozen), frozen[0]["start"], frozen[0]["end"]))
        else:
            add("frozen picture", "pass", "none over 1 s")
        dead = [d for d in static_stretches(ffmpeg, render) if not in_card(d["start"], d["end"])]
        if dead:
            worst = max(dead, key=lambda d: d["dur"])
            add("static picture", "warn", "{0} stretch(es) where the picture barely moves for {1:g} s or more, longest {2:.1f} s at {3:.1f} s "
                "(first {4:.1f}-{5:.1f} s)".format(len(dead), STATIC_MIN, worst["dur"], worst["start"], dead[0]["start"], dead[0]["end"]))
        else:
            add("static picture", "pass", "nothing still for {0:g} s or more".format(STATIC_MIN))
        if a:
            sil = silences(ffmpeg, render)
            if sil:
                add("silence", "warn", "{0} gap(s) over 1.5 s, first {1:.2f}-{2:.2f} s".format(len(sil), sil[0]["start"], sil[0]["end"]))
            else:
                add("silence", "pass", "no gap over 1.5 s")

    artifacts: Dict[str, Any] = {}
    markers: List[Tuple[float, str]] = []
    if timeline:
        for c in timeline["clips"]:
            markers.append((c["start"], c["id"]))

    # sound-level picture + click test at every join
    if a:
        try:
            samples = editaudio.decode_mono(ffmpeg, render)
        except StageError as exc:
            samples = None
            add("audio decode", "warn", str(exc))
        if samples is not None and len(samples):
            if timeline:
                bad = []
                for c in timeline["clips"][1:]:
                    t = c["start"] + (c["overlap_in"] or 0.0) / 2.0 if c["transition_in"]["type"] == "xfade" else c["start"]
                    score = editaudio.click_score(samples, editaudio.ANALYSIS_RATE, t)
                    if score > CLICK_LIMIT:
                        bad.append((c["id"], round(score, 1)))
                if bad:
                    add("clicks at joins", "warn", "{0} join(s) pop (id, score): {1}".format(len(bad), ", ".join(
                        "{0} {1}".format(i, s) for i, s in bad[:6])))
                else:
                    add("clicks at joins", "pass", "no join over {0:g}".format(CLICK_LIMIT))
                jumps = join_jumps(samples, editaudio.ANALYSIS_RATE, timeline)
                if jumps:
                    add("level at joins", "warn", "{0} join(s) step the sound by more than {1:g} dB, worst (clip, dB): {2}".format(
                        len(jumps), JUMP_LIMIT_DB, ", ".join("{0} {1:+.1f}".format(j["clip"], j["jump_db"]) for j in jumps[:6])))
                else:
                    add("level at joins", "pass", "no join steps the sound by more than {0:g} dB".format(JUMP_LIMIT_DB))
            ev = [(e["t"], e["kind"][:3]) for e in (timeline or {}).get("events", []) if e["kind"] in ("sfx", "slowmo", "overlay")][:60]
            artifacts["waveform"] = waveform_png(samples, editaudio.ANALYSIS_RATE, out_dir / "loudness.png",
                                                 markers + ev, master["loudness_lufs"] if master else None)

    # storyboard: a frame just after every join, plus the first and last
    if timeline:
        shots = [(0.1, "open")]
        for c in timeline["clips"][1:]:
            shots.append((c["start"] + 0.25, c["id"]))
        shots.append((max(0.0, timeline["duration"] - 0.15), "close"))
        artifacts["storyboard"] = storyboard(ffmpeg, render, shots, out_dir, "joins")
        # and a frame just after every marker: the joins sheet shows the start of each
        # clip, never the moment a callout is up - and a callout is where a wrong name,
        # a covered crosshair or a clipped word shows
        moments = [(c["start"] + m["at"] + MOMENT_LAG, "{0} {1}".format(c["id"], m["name"]))
                   for c in timeline["clips"] for m in c.get("markers", [])][:MAX_MOMENTS]
        if moments:
            artifacts["moments"] = storyboard(ffmpeg, render, moments, out_dir, "moments")

    if timeline:
        for issue in _pacing_checks(timeline):
            add(issue["name"], issue["status"], issue["detail"])
        if any(c.get("scene") for c in timeline.get("clips", [])):
            sheets = webcam_sheet(ffmpeg, render, timeline, out_dir)
            if sheets:
                artifacts["webcam"] = sheets
            rows = webcam_in_frame(ffmpeg, render, timeline, per_clip=3 if deep else 1)
            lost = [r for r in rows if r["matched"] == 0]
            part = [r for r in rows if 0 < r["matched"] < r["checked"]]
            n_frames = sum(r["checked"] for r in rows)
            if lost:
                first = lost[0]["misses"][0]
                add("webcam in frame", "fail", "{0} zoomed clip(s) do not show the webcam where the camera should put it: {1} (first: {2} s, "
                    "found {3}, expected {4})".format(len(lost), ", ".join(r["clip"] for r in lost[:8]), first[0], first[1], first[2]))
            elif part:
                add("webcam in frame", "warn", "{0} of {1} sampled frame(s) miss the webcam box: {2}".format(
                    sum(r["checked"] - r["matched"] for r in part), n_frames, ", ".join(r["clip"] for r in part[:8])))
            elif rows:
                add("webcam in frame", "pass", "the webcam is whole and where the camera puts it in all {0} sampled frame(s) of {1} zoomed clip(s)".format(
                    n_frames, len(rows)))

    ok = not any(c["status"] == "fail" for c in checks)
    result = {"ok": ok, "checks": checks, "artifacts": artifacts, "file": str(render)}
    (out_dir / "verify.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    (out_dir / "verify.md").write_text(render_markdown(result), encoding="utf-8")
    return result


def _pacing_checks(timeline: Dict[str, Any]) -> List[Dict[str, str]]:
    from . import edittimeline

    rep = edittimeline.pacing(timeline)
    out = []
    if rep["dead"]:
        longest = max(rep["dead"], key=lambda d: d["length"])
        out.append({"name": "pacing", "status": "warn", "detail": "{0} stretch(es) with nothing new on screen, longest {1:.1f} s at {2:.1f} s (limit {3:g} s)".format(
            len(rep["dead"]), longest["length"], longest["start"], rep["max_gap"])})
    else:
        out.append({"name": "pacing", "status": "pass", "detail": "something new at least every {0:g} s".format(rep["max_gap"])})
    for issue in rep["issues"]:
        if issue["what"] in ("opening", "dwell", "sfx-density"):
            out.append({"name": issue["what"], "status": "warn", "detail": issue["detail"]})
    return out


def render_markdown(result: Dict[str, Any]) -> str:
    mark = {"pass": "ok  ", "warn": "WARN", "fail": "FAIL"}
    lines = ["# Verify: {0}".format(result.get("file", "")), "", "Overall: **{0}**".format("OK" if result["ok"] else "FAILED"), ""]
    for c in result["checks"]:
        lines.append("- [{0}] {1}: {2}".format(mark[c["status"]], c["name"], c["detail"]))
    arts = result.get("artifacts", {})
    if arts:
        lines += ["", "Artefacts:"]
        if arts.get("waveform"):
            lines.append("- loudness over time: {0}".format(arts["waveform"]))
        for s in arts.get("storyboard", []):
            lines.append("- join storyboard: {0}".format(s))
        for s in arts.get("moments", []):
            lines.append("- marker moments (callouts up): {0}".format(s))
    return "\n".join(lines) + "\n"
