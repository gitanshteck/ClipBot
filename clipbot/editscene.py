"""The scene map: where the stream's webcam box sits, second by second.

A stream is a game picture with an overlay on top, and the overlay is not fixed: the
streamer's scenes put the facecam top-right, left-middle, bottom-left or nowhere
(full-cam), and the layout switches mid-stream. A camera move that ignores this crops
the facecam off the frame (`rules/framing.md`): the zoom has to know where the box is
*at that second*.

What is detected is the box, and only the box. The facecam is a rectangle with a
saturated cyan border (the same one on Kick and on YouTube), so it is found as a
rectangle of thin border-coloured lines - four sides, 16:9-ish (1.2 to 2.2 wide), a tenth
of the frame or more - rather than by looking for a face. A frame with no such rectangle is `cam:
None`: a full-cam scene, a scene with no facecam, or a frame the border is hidden in;
`spans_from_samples` bridges the last kind when the same box is on either side of a
short gap. Not detected: menus, maps, black screens, the chat panel, the HUD - nothing
here classifies a scene, it only finds the one rectangle a zoom must not cut off.

Pure functions (`find_cam_box`, `spans_from_samples`, `map_to_clip`) and the part that
shells out to ffmpeg (`scan_segment`, `scene_spans`, cached per source segment).
Spans are `{"t0", "t1", "cam": {x, y, w, h} | None[, "unstable": True]}` with the
rectangle in fractions of the source frame; `scan_segment` speaks segment-file seconds,
`map_to_clip` turns them into the clip's own output seconds.
"""

import json
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

import numpy as np

from . import edittimeline
from .config import Settings
from .utils import StageError, get_logger

log = get_logger(__name__)

DETECT_W, DETECT_H = 960, 540
SAMPLE_FPS = 4.0           # coarse sampling across a clip
REFINE_FPS = 30.0          # between two samples that disagree, to find the frame the layout changed on
SAME_TOL = 0.006           # two boxes closer than this (fractions of the frame, per number) are the same box
GAP_FILL = 0.3             # a missed detection this short, with the same box on both sides, is a miss
BLIP = 0.4                 # a box seen for less than this, with no box either side, was never there
UNSTABLE_STATES = 4        # this many different boxes inside one sample interval: the box is animating
SIDE_SAT = 0.35            # saturation floor for a side of the box (the long lines use border_mask's default)
DETECTOR_VERSION = 2

Rect = Dict[str, float]


# --------------------------------------------------------------------------
# finding the box in one frame
# --------------------------------------------------------------------------


def border_mask(rgb: Any, min_sat: float = 0.55) -> Any:
    """True where a pixel looks like the facecam border: hue 170-215 degrees, saturated, not dark.

    `min_sat` is 0.55 for the long top and bottom lines. The sides are looked at with a lower one
    (see `SIDE_SAT`): after a zoom and an encode a vertical border loses chroma to its neighbours
    (4:2:0 halves it horizontally) and measured 0.52 - a visible box with its sides "missing"."""
    f = rgb.astype(np.float32) / 255.0
    r, g, b = f[..., 0], f[..., 1], f[..., 2]
    mx = f.max(axis=2)
    d = mx - f.min(axis=2) + 1e-6
    sat = d / (mx + 1e-6)
    hue = np.zeros_like(mx)
    m = (mx == b) & (d > 1e-5)
    hue[m] = 60.0 * (4.0 + (r - g)[m] / d[m])
    m = (mx == g) & (mx != b)
    hue[m] = 60.0 * (2.0 + (b - r)[m] / d[m])
    return (hue > 170) & (hue < 215) & (sat > min_sat) & (mx > 0.45)


def _runs(mask: Any, min_len: int, gap: int = 3) -> List[Tuple[int, int, int]]:
    """(row, c0, c1) of every horizontal run of True at least `min_len` long, bridging gaps of <= `gap`."""
    pad = np.zeros((mask.shape[0], mask.shape[1] + 2), dtype=np.int8)
    pad[:, 1:-1] = mask
    d = np.diff(pad, axis=1)
    sr, sc = np.nonzero(d == 1)
    _, ec = np.nonzero(d == -1)
    out: List[Tuple[int, int, int]] = []
    cur: Optional[List[int]] = None
    for r, a, b in zip(sr.tolist(), sc.tolist(), ec.tolist()):
        if cur is not None and cur[0] == r and a - cur[2] <= gap:
            cur[2] = b
            continue
        if cur is not None and cur[2] - cur[1] >= min_len:
            out.append((cur[0], cur[1], cur[2]))
        cur = [r, a, b]
    if cur is not None and cur[2] - cur[1] >= min_len:
        out.append((cur[0], cur[1], cur[2]))
    return out


def _lines(runs: Sequence[Tuple[int, int, int]], tol_pos: int = 2, tol_ext: int = 6) -> List[Dict[str, int]]:
    """Runs on adjacent rows with the same extent are one line `{r0, r1, a, b}` (a border several pixels thick)."""
    out: List[Dict[str, int]] = []
    for r, a, b in sorted(runs):
        for ln in out:
            if r - ln["r1"] <= tol_pos and abs(a - ln["a"]) <= tol_ext and abs(b - ln["b"]) <= tol_ext:
                ln["r1"] = r
                ln["a"] = min(ln["a"], a)
                ln["b"] = max(ln["b"], b)
                break
        else:
            out.append({"r0": r, "r1": r, "a": a, "b": b})
    return out


def find_cam_box(rgb: Any) -> Optional[Rect]:
    """The facecam box in an RGB frame (H x W x 3 uint8) as fractions of the frame, or None.

    Two long border-coloured lines of the same extent make the top and bottom; the sides
    must be border-coloured for most of their height unless the box runs to the edge of
    the frame (a box pushed against the right edge has no right border to see). Of
    several candidates the best-supported, then the largest, wins.
    """
    h, w = rgb.shape[:2]
    mask = border_mask(rgb)
    side_mask = border_mask(rgb, SIDE_SAT)
    # a border is a thin line: a filled cyan area (a colour bar, a sky) merges into one thick "line" and is no side
    thin = max(12, int(0.025 * h))
    # a border is bridged across breaks up to 3% of the frame wide: a thin line over foliage came out of the
    # encoder as 216 px, a 10 px hole, then 146 px
    hs = [ln for ln in _lines(_runs(mask, int(0.10 * w), gap=max(3, int(0.03 * w)))) if ln["r1"] - ln["r0"] + 1 <= thin]
    if len(hs) < 2:
        return None
    best: Optional[Tuple[float, Tuple[int, int, int, int]]] = None
    for top in hs:
        for bot in hs:
            if bot["r0"] <= top["r1"] + 0.08 * h:
                continue
            if abs(top["a"] - bot["a"]) > 8 or abs(top["b"] - bot["b"]) > 8:
                continue
            x0, x1 = min(top["a"], bot["a"]), max(top["b"], bot["b"])
            bw, bh = x1 - x0, bot["r1"] - top["r0"]
            if not (0.10 * w <= bw <= 0.6 * w and 0.10 * h <= bh <= 0.7 * h and 1.2 <= bw / bh <= 2.2):
                continue

            def side(x: int) -> float:
                col = side_mask[top["r0"]:bot["r1"] + 1, max(0, x - 3):min(w, x + 4)].any(axis=1)
                return float(col.mean())

            left, right = side(x0 + 2), side(x1 - 2)
            if not ((left > 0.6 or x0 <= 0.012 * w) and (right > 0.6 or x1 >= 0.988 * w)):
                continue
            score = (min(left, 1.0) + min(right, 1.0)) / 2.0 + 0.002 * (bw + bh)
            if best is None or score > best[0]:
                best = (score, (x0, top["r0"], x1, bot["r1"]))
    if best is None:
        return None
    x0, y0, x1, y1 = best[1]
    return {"x": round(x0 / w, 4), "y": round(y0 / h, 4), "w": round((x1 - x0) / w, 4), "h": round((y1 - y0) / h, 4)}


# --------------------------------------------------------------------------
# samples -> spans (pure)
# --------------------------------------------------------------------------


def same_box(a: Optional[Rect], b: Optional[Rect], tol: float = SAME_TOL) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return all(abs(a[k] - b[k]) <= tol for k in ("x", "y", "w", "h"))


def _median_box(boxes: Sequence[Rect]) -> Rect:
    return {k: round(float(np.median([b[k] for b in boxes])), 4) for k in ("x", "y", "w", "h")}


Sample = Tuple[float, Optional[Rect], bool]      # (seconds, box, unstable)


def spans_from_samples(samples: Sequence[Sample], start: float, end: float,
                       gap_fill: float = GAP_FILL) -> List[Dict[str, Any]]:
    """Merge samples into spans of one box (or no box), covering [start, end] without holes.

    A run of misses shorter than `gap_fill` with the same box either side is filled: the
    border was hidden by a flash or a compression artefact, not taken off the screen.
    A span's box is the median of its samples', so one pixel of jitter between frames
    does not make two spans.
    """
    pts = sorted(samples, key=lambda s: s[0])
    if not pts:
        return [{"t0": round(start, 3), "t1": round(end, 3), "cam": None}]
    # a box that shows up for an instant between two stretches with none is a pattern in the game
    # picture that happened to look like one, not a webcam
    i = 0
    while i < len(pts):
        if pts[i][1] is not None and not pts[i][2]:
            j = i
            while j + 1 < len(pts) and pts[j + 1][1] is not None and not pts[j + 1][2]:
                j += 1
            blip = pts[j][0] - pts[i][0] + 1.0 / REFINE_FPS < BLIP
            if blip and 0 < i and j + 1 < len(pts) and pts[i - 1][1] is None and pts[j + 1][1] is None:
                for k in range(i, j + 1):
                    pts[k] = (pts[k][0], None, False)
            i = j + 1
        else:
            i += 1
    # fill short misses
    i = 0
    while i < len(pts):
        if pts[i][1] is None and not pts[i][2] and i > 0 and pts[i - 1][1] is not None:
            j = i
            while j < len(pts) and pts[j][1] is None and not pts[j][2]:
                j += 1
            if j < len(pts) and pts[j][1] is not None and same_box(pts[i - 1][1], pts[j][1]) \
                    and pts[j][0] - pts[i][0] <= gap_fill:
                for k in range(i, j):
                    pts[k] = (pts[k][0], pts[i - 1][1], False)
            i = j
        else:
            i += 1
    groups: List[Dict[str, Any]] = []
    for t, box, unstable in pts:
        g = groups[-1] if groups else None
        if g is not None and g["unstable"] == unstable and (unstable or same_box(g["first"], box)):
            g["boxes"].append(box)
            continue
        groups.append({"t0": t, "first": box, "boxes": [box], "unstable": unstable})
    groups[0]["t0"] = start
    spans: List[Dict[str, Any]] = []
    for n, g in enumerate(groups):
        t1 = groups[n + 1]["t0"] if n + 1 < len(groups) else end
        found = [b for b in g["boxes"] if b is not None]
        span: Dict[str, Any] = {"t0": round(g["t0"], 3), "t1": round(t1, 3),
                                "cam": _median_box(found) if found and not g["unstable"] else None}
        if g["unstable"]:
            span["unstable"] = True
        spans.append(span)
    return spans


def box_at(spans: Sequence[Dict[str, Any]], t: float) -> Optional[Rect]:
    """The box at second `t` (None when there is none, or the span is unstable)."""
    for s in spans:
        if s["t0"] - 1e-9 <= t < s["t1"] - 1e-9:
            return s["cam"]
    return spans[-1]["cam"] if spans and t >= spans[-1]["t1"] - 1e-9 else None


def map_to_clip(spans: Sequence[Dict[str, Any]], seg_src_start: float, pieces: List[Dict[str, Any]],
                clip_in: float, clip_out: float) -> List[Dict[str, Any]]:
    """Segment-file spans -> spans in the clip's own output seconds.

    `seg_src_start` is the source second the segment file's frame 0 sits at; a span is
    first placed on the source clock, clipped to [clip_in, clip_out], then pushed through
    the clip's retiming (a slow-motion ramp stretches a span, a freeze is one instant).
    """
    out: List[Dict[str, Any]] = []
    for s in spans:
        a, b = max(s["t0"] + seg_src_start, clip_in), min(s["t1"] + seg_src_start, clip_out)
        if b <= a + 1e-6:
            continue
        o0, o1 = edittimeline.src_to_out(pieces, a), edittimeline.src_to_out(pieces, b)
        if o1 <= o0 + 1e-6:
            continue
        item = {"t0": round(o0, 4), "t1": round(o1, 4), "cam": s["cam"]}
        if s.get("unstable"):
            item["unstable"] = True
        if out and out[-1]["cam"] == item["cam"] and bool(out[-1].get("unstable")) == bool(item.get("unstable")) \
                and abs(out[-1]["t1"] - item["t0"]) < 1e-3:
            out[-1]["t1"] = item["t1"]
        else:
            out.append(item)
    return out


# --------------------------------------------------------------------------
# reading frames and scanning a segment (ffmpeg)
# --------------------------------------------------------------------------


def read_frames(ffmpeg: str, path: Any, start: float, end: float, fps: float,
                size: Tuple[int, int] = (DETECT_W, DETECT_H)) -> Iterator[Tuple[float, Any]]:
    """(seconds, RGB array) for frames sampled at `fps` from [start, end] of a video file."""
    w, h = size
    argv = [ffmpeg, "-v", "error", "-ss", "{0:.3f}".format(max(0.0, start)), "-t", "{0:.3f}".format(max(0.05, end - start)),
            "-i", str(path), "-an", "-vf", "fps={0:g},scale={1}:{2}:flags=area".format(fps, w, h),
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    n = 0
    size_bytes = w * h * 3
    try:
        while True:
            buf = proc.stdout.read(size_bytes)
            if len(buf) < size_bytes:
                break
            yield max(0.0, start) + n / fps, np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 3)
            n += 1
    finally:
        proc.stdout.close()
        proc.stderr.close()
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def scan_segment(ffmpeg: str, path: Any, start: float, end: float, fps: float = SAMPLE_FPS) -> List[Dict[str, Any]]:
    """Spans of [start, end] (seconds of the segment file): a coarse pass at `fps`, then a
    frame-accurate pass across every interval where two neighbouring samples disagree."""
    coarse: List[Sample] = [(t, find_cam_box(f), False) for t, f in read_frames(ffmpeg, path, start, end, fps)]
    if not coarse:
        raise StageError("no frames could be read from {0} between {1:.2f}s and {2:.2f}s".format(path, start, end))
    samples: List[Sample] = list(coarse)
    for (t0, a, _), (t1, b, _) in zip(coarse, coarse[1:]):
        if same_box(a, b):
            continue
        fine = [(t, find_cam_box(f)) for t, f in read_frames(ffmpeg, path, t0, t1, REFINE_FPS)]
        states: List[Optional[Rect]] = []
        for _, box in fine:
            if not states or not same_box(states[-1], box):
                states.append(box)
        if len(states) >= UNSTABLE_STATES:      # the box is sliding or fading between layouts
            samples += [(t, None, True) for t, _ in fine]
        else:
            samples += [(t, box, False) for t, box in fine]
    return spans_from_samples(samples, start, end)


def scene_spans(settings: Settings, ffmpeg: str, seg_path: Any, seg_key: str, start: float, end: float) -> List[Dict[str, Any]]:
    """`scan_segment`, cached by segment, window and detector version under `_cache/edit/scenes/`."""
    cache = Path(settings.work_root) / "_cache" / "edit" / "scenes"
    f = cache / "{0}-{1:.2f}-{2:.2f}-v{3}.json".format(seg_key, start, end, DETECTOR_VERSION)
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))["spans"]
        except (ValueError, KeyError, OSError):
            pass
    spans = scan_segment(ffmpeg, seg_path, start, end)
    cache.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".part")
    tmp.write_text(json.dumps({"v": DETECTOR_VERSION, "start": start, "end": end, "spans": spans}), encoding="utf-8")
    tmp.replace(f)
    return spans
