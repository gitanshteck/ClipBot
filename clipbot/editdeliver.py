"""Delivery package for a finished edit: platform variants, thumbnails, post text.

An edit is only finished when it can be posted. This module turns a rendered master
into what the upload actually needs:

* **variants** - the same edit re-framed for other shapes (9:16 Shorts, 1:1, 4:5)
  with the blur-fit canvas, audio copied untouched. A re-frame is a second encode of
  the picture, so a hero platform deserves a real render at its own preset instead
  (`clipbot edit render --preset shorts_1080x1920`: overlays are then drawn at that
  canvas's size); a variant is the fast path for the rest.
* **thumbnails** - candidate frames taken just after the edit's own kill/hit
  markers (the callout has landed, the camera is mid-punch) and scored by sharpness
  and exposure, never within 0.3 s of a join (a thumbnail with a half-faded second
  clip in it looks broken), spread at least 5 s apart. YouTube's limit is 2 MB, so
  they are written as 1280x720 JPEGs under that.
* **chapters** - the timestamp list for the description, with YouTube's three
  conditions checked (starts at 0:00, three or more entries, each at least 10 s).
* **post scaffold** - title candidates, a description, hashtags, and the warnings a
  human should read before publishing.

Everything that decides something is a pure function (`chapter_lines`,
`thumbnail_candidates`, `sharpness`, `pick_spread`, `variant_filter`,
`title_options`) so it is tested without a render.
"""

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import editfilters as ef
from .specerror import SpecError

# name -> (width, height, longest allowed in seconds or None)
VARIANTS = {
    "shorts": (1080, 1920, 180.0),
    "square": (1080, 1080, 600.0),
    "portrait": (1080, 1350, 600.0),
}

SEAM_MARGIN = 0.3          # seconds around a join a thumbnail may not come from
MIN_THUMB_GAP = 5.0        # seconds between two chosen thumbnails
THUMB_MAX_BYTES = 2 * 1024 * 1024
MARKER_LAG = 0.45          # a callout has landed this long after its marker


# --------------------------------------------------------------------------
# pure helpers
# --------------------------------------------------------------------------


def clock(t: float) -> str:
    """YouTube-style timestamp: m:ss, or h:mm:ss from an hour on."""
    s = int(round(t))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return "{0}:{1:02d}:{2:02d}".format(h, m, sec) if h else "{0}:{1:02d}".format(m, sec)


def chapter_lines(chapters: Sequence[Dict[str, Any]], total: float) -> Dict[str, Any]:
    """{"text", "qualifies", "problems"} for a `[{t, title}]` list.

    YouTube turns a description's timestamps into chapters only when the first is
    0:00, there are at least three, and every chapter lasts at least 10 s; the last
    one runs to `total`.
    """
    problems: List[str] = []
    ch = sorted(chapters, key=lambda c: c["t"])
    if not ch:
        problems.append("no chapters: tag a clip `chapter` or use a card")
    else:
        if ch[0]["t"] > 0.01:
            problems.append("the first chapter must start at 0:00")
        if len(ch) < 3:
            problems.append("YouTube needs at least 3 chapters (this has {0})".format(len(ch)))
        for i, c in enumerate(ch):
            end = ch[i + 1]["t"] if i + 1 < len(ch) else total
            if end - c["t"] < 10.0:
                problems.append("chapter {0!r} lasts {1:.1f} s; YouTube needs 10 s".format(c["title"], end - c["t"]))
    text = "\n".join("{0} {1}".format(clock(c["t"]), c["title"]) for c in ch)
    return {"text": text, "qualifies": not problems, "problems": problems}


def thumbnail_candidates(timeline: Dict[str, Any], lag: float = MARKER_LAG) -> List[Tuple[float, str]]:
    """(time, reason) frames worth scoring: just after each marker, then the middle of
    any clip with none. Times within SEAM_MARGIN of a join, or past the end, are dropped."""
    clips = timeline["clips"]
    total = timeline["duration"]
    seams = []
    for e in clips:
        if e["index"] > 0:
            seams.append(e["start"])
    out: List[Tuple[float, str]] = []

    def ok(t: float) -> bool:
        return SEAM_MARGIN <= t <= total - SEAM_MARGIN and all(abs(t - s) >= SEAM_MARGIN for s in seams) and \
            not any(e["start"] < t < e["start"] + e["overlap_in"] for e in clips)

    for e in clips:
        if e["type"] != "clip":
            continue
        marks = e.get("markers") or []
        for m in marks:
            t = e["start"] + m["at"] + lag
            if t < e["end"] and ok(t):
                out.append((round(t, 3), "{0} {1}".format(e["id"], m["name"])))
        if not marks:
            t = e["start"] + e["dur"] / 2.0
            if ok(t):
                out.append((round(t, 3), "{0} middle".format(e["id"])))
    return out


def sharpness(gray) -> float:
    """Variance of the Laplacian of a 2-D float array: high when edges are crisp,
    low for motion blur, fades and flat colour."""
    import numpy as np

    g = np.asarray(gray, dtype="float64")
    if g.ndim != 2 or g.shape[0] < 3 or g.shape[1] < 3:
        return 0.0
    lap = -4.0 * g[1:-1, 1:-1] + g[:-2, 1:-1] + g[2:, 1:-1] + g[1:-1, :-2] + g[1:-1, 2:]
    return float(lap.var())


def exposure_factor(mean_luma: float) -> float:
    """1.0 for a well-exposed frame, falling toward 0.3 for near-black or blown-out."""
    if mean_luma < 35:
        return 0.3 + 0.7 * max(0.0, mean_luma) / 35.0
    if mean_luma > 220:
        return 0.3 + 0.7 * max(0.0, 255.0 - mean_luma) / 35.0
    return 1.0


def pick_spread(scored: Sequence[Tuple[float, float, str]], n: int, min_gap: float = MIN_THUMB_GAP) -> List[Tuple[float, float, str]]:
    """The best `n` of (time, score, reason), no two closer than `min_gap` seconds."""
    chosen: List[Tuple[float, float, str]] = []
    for item in sorted(scored, key=lambda x: -x[1]):
        if all(abs(item[0] - c[0]) >= min_gap for c in chosen):
            chosen.append(item)
        if len(chosen) >= n:
            break
    return sorted(chosen, key=lambda x: -x[1])


def variant_filter(src_wh: Tuple[int, int], name: str) -> Tuple[str, str]:
    """(filter_complex text, output label) re-framing a master to a variant's canvas."""
    if name not in VARIANTS:
        raise SpecError("unknown variant {0!r} (use one of {1})".format(name, ", ".join(sorted(VARIANTS))))
    w, h, _ = VARIANTS[name]
    g = ef.Graph()
    out = ef.canvas_chain(g, "0:v", src_wh, (w, h), "blur")
    return g.render(), out


def title_options(player: str, game: str, kills: int, nades: int, far: Optional[float]) -> List[str]:
    """A handful of honest title candidates built from what the edit actually contains."""
    who = player or "Me"
    where = " in {0}".format(game) if game else ""
    out = ["{0} - {1} kills{2}".format(who, kills, where)]
    if nades:
        out.append("{0} nade kill{1}{2} - {3}".format(nades, "" if nades == 1 else "s", where, who))
    if far and far >= 100:
        out.append("{0:.0f} m kill{1} - {2}".format(far, where, who))
    out.append("{0}'s best kills{1}".format(who, where))
    return out


def description(title: str, chapters: Dict[str, Any], facts: Sequence[str], links: Sequence[str] = ()) -> str:
    lines = [title, ""]
    lines += list(facts)
    if chapters["text"]:
        lines += ["", "Chapters:" if chapters["qualifies"] else "Timestamps (YouTube will not chapter these yet):", chapters["text"]]
    if links:
        lines += [""] + list(links)
    return "\n".join(lines).strip() + "\n"


# --------------------------------------------------------------------------
# IO
# --------------------------------------------------------------------------


def _sha1(path: Path) -> str:
    h = hashlib.sha1()
    with open(str(path), "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def make_variant(ffmpeg: str, master: Path, src_wh: Tuple[int, int], name: str, out_path: Path, runner=None) -> Path:
    """Re-frame `master` to a variant. Audio is stream-copied: the loudness mastering
    is already done and must not be touched twice."""
    graph, label = variant_filter(src_wh, name)
    w, h, _ = VARIANTS[name]
    script = out_path.with_suffix(".filter.txt")
    script.write_text(graph, encoding="utf-8")
    argv = [ffmpeg, "-hide_banner", "-nostats", "-loglevel", "error", "-y", "-i", str(master),
            "-/filter_complex", str(script), "-map", "[{0}]".format(label), "-map", "0:a:0",
            "-c:v", "libx264", "-preset", "slow", "-crf", "19", "-pix_fmt", "yuv420p", "-profile:v", "high",
            "-bf", "2", "-g", "30", "-keyint_min", "30", "-x264-params", "open-gop=0:scenecut=0",
            "-c:a", "copy", "-movflags", "+faststart", str(out_path)]
    if runner is not None:
        runner(argv)
    else:
        proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
                              encoding="utf-8", errors="replace")
        if proc.returncode != 0:
            raise SpecError("ffmpeg could not build the {0} variant: {1}".format(name, proc.stderr.strip()[-300:]))
    script.unlink()
    return out_path


def pick_thumbnails(ffmpeg: str, video: Path, timeline: Dict[str, Any], out_dir: Path, n: int = 3) -> List[Dict[str, Any]]:
    """Score the candidate frames and write the best `n` as <=2 MB 1280x720 JPEGs."""
    import numpy as np
    from PIL import Image

    from . import editqa

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_dir / "_cand"
    scored: List[Tuple[float, float, str]] = []
    for k, (t, reason) in enumerate(thumbnail_candidates(timeline)):
        png = tmp / "c{0:03d}.png".format(k)
        if not editqa._extract_frame(ffmpeg, video, t, png, 1280):
            continue
        with Image.open(png) as im:
            gray = np.asarray(im.convert("L").resize((640, 360)), dtype="float64")
        scored.append((t, sharpness(gray) * exposure_factor(float(gray.mean())), reason))
    best = pick_spread(scored, n)
    results: List[Dict[str, Any]] = []
    for rank, (t, score, reason) in enumerate(best, start=1):
        dst = out_dir / "thumb_{0}.jpg".format(rank)
        png = tmp / "final_{0}.png".format(rank)
        editqa._extract_frame(ffmpeg, video, t, png, 1280)
        quality = 92
        with Image.open(png) as im:
            im = im.convert("RGB")
            while True:
                im.save(dst, quality=quality)
                if dst.stat().st_size <= THUMB_MAX_BYTES or quality <= 60:
                    break
                quality -= 8
        results.append({"file": str(dst), "t": t, "score": round(score, 1), "reason": reason, "bytes": dst.stat().st_size})
    for f in tmp.glob("*.png"):
        f.unlink()
    try:
        tmp.rmdir()
    except OSError:
        pass
    return results


# --------------------------------------------------------------------------
# designed thumbnails
# --------------------------------------------------------------------------

THUMB_SIZE = (1280, 720)


def _cover(im: Any, size: Tuple[int, int]) -> Any:
    """Scale to cover `size`, centre-cropped (never letterboxed)."""
    from PIL import Image

    w, h = im.size
    k = max(size[0] / float(w), size[1] / float(h))
    im = im.resize((max(size[0], int(round(w * k))), max(size[1], int(round(h * k)))), Image.LANCZOS)
    x, y = (im.width - size[0]) // 2, (im.height - size[1]) // 2
    return im.crop((x, y, x + size[0], y + size[1]))


def _wrap(words: List[str], max_lines: int) -> List[str]:
    """Split into at most `max_lines` lines of similar length (a thumbnail reads in one glance)."""
    n = len(words)
    if n <= 1 or max_lines <= 1:
        return [" ".join(words)]
    best, best_cost = None, None
    for cuts in range(1, 1 << (n - 1)):
        idx = [i + 1 for i in range(n - 1) if cuts >> i & 1]
        if len(idx) + 1 > max_lines:
            continue
        lines = [" ".join(words[a:b]) for a, b in zip([0] + idx, idx + [n])]
        cost = max(len(l) for l in lines) * 10 + len(lines)
        if best_cost is None or cost < best_cost:
            best, best_cost = lines, cost
    return best or [" ".join(words)]


def _rgb(accent: str) -> Tuple[int, int, int]:
    return int(accent[1:3], 16), int(accent[3:5], 16), int(accent[5:7], 16)


def _frame_subject(im: Any, zoom: float, focus: Optional[Tuple[float, float]], side: str) -> Any:
    """Crop `im` closer so the subject (at `focus`, fractions of the source frame) lands on the side opposite
    the words: a third in from the edge, vertically centred if the crop allows. `zoom` 1 leaves the frame alone."""
    if zoom <= 1.0 or focus is None:
        return im
    w, h = im.size
    cw, ch = w / zoom, h / zoom
    want_x = 0.30 if side == "right" else 0.70          # where the subject should sit in the crop
    left = min(max(focus[0] * w - want_x * cw, 0.0), w - cw)
    top = min(max(focus[1] * h - 0.5 * ch, 0.0), h - ch)
    return im.crop((int(left), int(top), int(left + cw), int(top + ch)))


def compose_thumbnail(frame: Any, text: str, out_path: Any, font: Optional[str] = None, accent: str = "#35D0FF",
                      side: str = "right", max_lines: int = 3, size: Tuple[int, int] = THUMB_SIZE,
                      zoom: float = 1.0, focus: Optional[Tuple[float, float]] = None) -> Dict[str, Any]:
    """A designed 1280x720 thumbnail from one frame: graded and vignetted, with 1-3 lines of heavy
    outlined text on one side (the side without the face), the last line in the accent colour,
    and a thin accent frame. `side` is `left` or `right`: put the words opposite the person.
    With `zoom` > 1 and `focus` (the person's centre, fractions of the frame) the frame is cropped
    closer and shifted so the person sits on the other side from the words; without them the
    whole frame is used, which only works when the person is already at one edge.

    Returns {file, bytes, box, legibility, lines}; legibility is the luminance spread of the
    text box at the 168 px width YouTube shows in a sidebar - under 0.5 means the words would
    not survive being small. JPEG <= THUMB_MAX_BYTES.
    """
    import numpy as np
    from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

    from .editoverlay import find_font

    if side not in ("left", "right"):
        raise SpecError("side must be 'left' or 'right'")
    words = text.upper().split()
    if not words:
        raise SpecError("a thumbnail needs some text")
    W, H = size
    im = _cover(_frame_subject(Image.open(str(frame)).convert("RGB"), zoom, focus, side), size)
    im = ImageEnhance.Contrast(im).enhance(1.12)
    im = ImageEnhance.Color(im).enhance(1.22)
    im = im.filter(ImageFilter.UnsharpMask(radius=2, percent=70, threshold=3))
    # vignette: darker toward the corners so the words and the subject both lift
    yy, xx = np.mgrid[0:H, 0:W]
    r = np.sqrt(((xx - W / 2.0) / (W / 2.0)) ** 2 + ((yy - H / 2.0) / (H / 2.0)) ** 2)
    shade = 1.0 - 0.38 * np.clip((r - 0.55) / 0.9, 0.0, 1.0) ** 1.4
    im = Image.fromarray((np.asarray(im, dtype=np.float32) * shade[..., None]).clip(0, 255).astype(np.uint8))

    lines = _wrap(words, max_lines)
    fpath = find_font(font)
    draw = ImageDraw.Draw(im)
    zone_w = int(W * 0.50)
    lo, hi = 20, 260
    while hi - lo > 2:                    # the biggest type that keeps every line inside its zone
        mid = (lo + hi) // 2
        f = ImageFont.truetype(fpath, mid) if fpath else ImageFont.load_default()
        widest = max(draw.textlength(l, font=f) for l in lines)
        if widest <= zone_w - 40 and mid * len(lines) * 1.05 <= H - 120:
            lo = mid
        else:
            hi = mid
    f = ImageFont.truetype(fpath, lo) if fpath else ImageFont.load_default()
    lh = int(lo * 1.05)
    top = (H - lh * len(lines)) // 2
    stroke = max(4, lo // 12)
    box = [W, H, 0, 0]
    for i, line in enumerate(lines):
        tw = draw.textlength(line, font=f)
        x = 44 if side == "left" else W - 44 - tw
        y = top + i * lh
        colour = _rgb(accent) if i == len(lines) - 1 and len(lines) > 1 else (255, 255, 255)
        draw.text((x + 4, y + 6), line, font=f, fill=(0, 0, 0), stroke_width=stroke, stroke_fill=(0, 0, 0))
        draw.text((x, y), line, font=f, fill=colour, stroke_width=stroke, stroke_fill=(8, 8, 12))
        bb = draw.textbbox((x, y), line, font=f, stroke_width=stroke)
        box = [min(box[0], bb[0]), min(box[1], bb[1]), max(box[2], bb[2]), max(box[3], bb[3])]
    draw.rectangle((10, 10, W - 11, H - 11), outline=_rgb(accent), width=6)

    crop = im.crop(tuple(int(v) for v in box)).convert("L")
    small = np.asarray(crop.resize((max(8, int(crop.width * 168 / float(W))), max(4, int(crop.height * 168 / float(W))))),
                       dtype=np.float32) / 255.0
    legibility = float(np.percentile(small, 95) - np.percentile(small, 5))

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    quality = 93
    while True:
        im.save(out_path, quality=quality, subsampling=0)
        if out_path.stat().st_size <= THUMB_MAX_BYTES or quality <= 60:
            break
        quality -= 6
    return {"file": str(out_path), "bytes": out_path.stat().st_size, "box": tuple(int(v) for v in box),
            "legibility": round(legibility, 2), "lines": lines}


def build_package(
    ffmpeg: str,
    master: Path,
    timeline: Dict[str, Any],
    out_dir: Path,
    chapters: Sequence[Dict[str, Any]],
    variants: Sequence[str] = (),
    thumbs: int = 3,
    player: str = "",
    game: str = "",
    kills: int = 0,
    nades: int = 0,
    far: Optional[float] = None,
    src_wh: Tuple[int, int] = (1920, 1080),
    warnings: Sequence[str] = (),
    titles: Optional[Sequence[str]] = None,
    fact_lines: Optional[Sequence[str]] = None,
    credits: Sequence[str] = (),
    hashtags: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Write the whole package under `out_dir` and return its manifest.

    The post text is built from the kill counts unless the author supplies it: `titles` (a
    placeholder is fine - the author may not have chosen one yet), `fact_lines` (what the
    description says the video holds), `credits` (lines under the chapters) and `hashtags`.
    A roundup that is not about kills has nothing honest to say with a kill count."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    total = float(timeline["duration"])
    files: List[Dict[str, Any]] = []
    notes: List[str] = list(warnings)

    made: List[str] = []
    for name in variants:
        _, _, longest = VARIANTS[name] if name in VARIANTS else (0, 0, None)
        if longest and total > longest:
            notes.append("{0} variant skipped: the edit runs {1:.0f} s and that shape allows {2:.0f} s".format(name, total, longest))
            continue
        dst = make_variant(ffmpeg, Path(master), src_wh, name, out_dir / "{0}.mp4".format(name))
        files.append({"kind": "variant", "name": name, "file": str(dst)})
        made.append(name)

    thumb_info = pick_thumbnails(ffmpeg, Path(master), timeline, out_dir / "thumbnails", thumbs) if thumbs else []
    for t in thumb_info:
        files.append({"kind": "thumbnail", "file": t["file"], "t": t["t"], "reason": t["reason"]})

    ch = chapter_lines(chapters, total)
    notes.extend(ch["problems"])
    titles = list(titles) if titles else title_options(player, game, kills, nades, far)
    facts = list(fact_lines) if fact_lines is not None else [
        "{0} kill{1}".format(kills, "" if kills == 1 else "s") + ("; {0} with a grenade".format(nades) if nades else "") +
        ("; longest {0:.0f} m".format(far) if far else "")]
    tags = list(hashtags) if hashtags is not None else [
        t for t in ("#" + game.replace(" ", "").lower() if game else "", "#gaming") if t]
    if "shorts" in made and "#shorts" not in tags:
        tags.append("#shorts")
    post = {"titles": titles, "description": description(titles[0], ch, facts, credits), "chapters": ch,
            "hashtags": tags, "warnings": notes}
    (out_dir / "post.json").write_text(json.dumps(post, indent=1), encoding="utf-8")
    md = ["# Post scaffold", "", "## Title candidates", ""] + ["- " + t for t in titles] + ["", "## Description", "", "```", post["description"].rstrip(), "```", ""]
    if post["hashtags"]:
        md += ["## Hashtags", "", " ".join(post["hashtags"]), ""]
    if notes:
        md += ["## Read before publishing", ""] + ["- " + w for w in notes] + [""]
    (out_dir / "post.md").write_text("\n".join(md), encoding="utf-8")
    files.append({"kind": "post", "file": str(out_dir / "post.md")})

    manifest = {"master": str(master), "duration": round(total, 3), "files": [], "warnings": notes}
    master_p = Path(master)
    manifest["files"].append({"kind": "master", "file": str(master_p), "bytes": master_p.stat().st_size, "sha1": _sha1(master_p)})
    for f in files:
        p = Path(f["file"])
        manifest["files"].append(dict(f, bytes=p.stat().st_size, sha1=_sha1(p)))
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return manifest
