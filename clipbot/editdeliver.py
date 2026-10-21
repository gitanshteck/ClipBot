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
) -> Dict[str, Any]:
    """Write the whole package under `out_dir` and return its manifest."""
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
    titles = title_options(player, game, kills, nades, far)
    facts = ["{0} kill{1}".format(kills, "" if kills == 1 else "s") + ("; {0} with a grenade".format(nades) if nades else "") +
             ("; longest {0:.0f} m".format(far) if far else "")]
    post = {"titles": titles, "description": description(titles[0], ch, facts), "chapters": ch,
            "hashtags": [t for t in ("#" + game.replace(" ", "").lower() if game else "", "#gaming", "#shorts" if "shorts" in made else "") if t],
            "warnings": notes}
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
