"""Stage: read a stream's kill feed and record one player's kills.

Writes `killfeed.json` into the workspace: every kill the game's own feed logged
for the player - the moment, the distance, the victim's name (when it can be read),
the weapon class (gun / grenade), headshot and through-cover flags - plus a crop of
each weapon icon (`killfeed_icons/`) that an edit can put in a callout. It is a
better source of "the kills" than the transcript, which hears shouting, not
kills: on the first stream this was run against, the transcript's "nade" mentions
included none of the grenade kills the feed logged.

Method (see `clipbot/killfeed.py` for the parsing and why it is built this way):
decode every frame at `killfeed.fps` (1/s by default), crop and 3x-upscale the feed
region, OCR it, and group the rows that name the player. Decoding every frame - not
only keyframes - matters on YouTube, whose keyframes are ~6 s apart: a feed row can
come and go between two of them. A YouTube workspace has no video on disk, so the
picture track is copied to a scratch file (`ytsegments.download_video_track`) and
deleted afterwards.

OCR is the built-in Windows OCR engine, driven by `clipbot/data/windows_ocr.ps1`:
no install, no API, nothing leaves the machine. It is Windows-only; the OCR call is
isolated in `_ocr_dir` so another backend is one function.

Where it cannot work: a stream whose overlay covers the feed (a webcam sitting
over the left-middle of the screen). The scan then finds nothing, which is
reported as such rather than as "no kills".
"""

import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .. import killfeed, ytsegments
from ..config import Settings
from ..progress import NULL_PROGRESS, JobCancelled, Progress
from ..utils import StageError, get_logger, resolve_tool
from ..workspace import MODE_EMBED, Workspace

log = get_logger(__name__)

STAGE = "killfeed"
SCHEMA = 1
OCR_SCRIPT = Path(__file__).resolve().parent.parent / "data" / "windows_ocr.ps1"
FFMPEG_HINT = (
    "Download from https://www.gyan.dev/ffmpeg/builds/ (Windows), then either add the bin "
    "folder to PATH or set tools.ffmpeg in config/settings.json."
)


def killfeed_path(ws: Workspace) -> Path:
    return ws.root / "killfeed.json"


def load(ws: Workspace) -> Optional[Dict[str, Any]]:
    p = killfeed_path(ws)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def _ocr_dir(frames: Path, out_jsonl: Path, progress: Progress) -> None:
    """OCR every image in `frames`, one JSON object per image, into `out_jsonl`."""
    if not OCR_SCRIPT.exists():
        raise StageError("OCR script missing: {0}".format(OCR_SCRIPT))
    powershell = shutil.which("powershell") or shutil.which("powershell.exe")
    if powershell is None:
        raise StageError(
            "The kill-feed reader needs the Windows OCR engine (PowerShell). It is not available on this machine; "
            "another OCR backend would have to replace `_ocr_dir` in clipbot/stages/killfeed.py.")
    proc = subprocess.Popen(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(OCR_SCRIPT), "-Dir", str(frames),
         "-Out", str(out_jsonl)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
        encoding="utf-8", errors="replace")
    try:
        while proc.poll() is None:
            if progress.cancelled:
                proc.terminate()
                raise JobCancelled("cancelled by request")
            time.sleep(0.4)
    finally:
        if proc.poll() is None:
            proc.kill()
    if proc.returncode != 0:
        err = (proc.stderr.read() if proc.stderr else "")[-600:]
        raise StageError("Windows OCR failed (exit {0}): {1}".format(proc.returncode, err.strip()))


def _chunks(duration: float, size: float, start: float = 0.0) -> List[Tuple[float, float]]:
    """(start, span) windows covering [start, duration) in steps of `size`, with no sliver at the end."""
    out, t = [], max(0.0, start)
    while t < duration - 0.5:
        out.append((t, min(size, duration - t)))
        t += size
    return out


GAMES = ("wardogs", "mw4")


def _icon_crop(img, o: Dict[str, Any], mw4: bool = False):
    cy, h = o["cy"], max(o["h"], 24)
    if mw4:
        # the weapon icon starts ~25 px (at the 3x working scale) after the name and, with a headshot's skull,
        # runs to ~235; the victim's name follows it. There is no distance tag to measure from.
        x0, x1 = o["kx"] + 25, o["kx"] + 275
        return img.crop((max(0, int(x0)), max(0, int(cy - 0.9 * h)), min(img.width, int(x1)), int(cy + 0.9 * h)))
    x0, x1 = o["kx"] + 1, o["dx"] + 45
    return img.crop((max(0, int(x0)), int(cy - 0.75 * h), max(int(x0) + 8, int(x1)), int(cy + 0.75 * h)))


def scan_killfeed(
    ws: Workspace,
    settings: Settings,
    player: Optional[str] = None,
    video: Optional[Path] = None,
    force: bool = False,
    keep_video: bool = False,
    progress: Progress = NULL_PROGRESS,
    game: Optional[str] = None,
    first: Optional[float] = None,
    last: Optional[float] = None,
    text: bool = True,
) -> Path:
    """Scan one workspace's video for the player's kills. Returns the killfeed.json path.

    `game` picks the profile (`killfeed.game`, default `wardogs`): `wardogs` is the original reader (a
    distance-tagged feed, a `KILL CONFIRMED` popup, both OCR'd); `mw4` is Call of Duty: Modern Warfare 4
    (a feed with no distance, a `<skull>KILL` popup found by template matching). `first`/`last` scan only a
    stretch of the video, in video seconds - `killfeed.json` records where it starts. `text` (mw4) then reads what each
    kill's popup says (`read_popup_text`).
    """
    from PIL import Image

    out_path = killfeed_path(ws)
    if out_path.exists() and not force:
        log.info("%s already scanned (use force to redo)", out_path.name)
        return out_path

    player = (player or settings.get("killfeed.player") or "").strip()
    if not player:
        raise StageError("Which player? Pass --player NAME or set killfeed.player in config/settings.json.")
    game = str(game or settings.get("killfeed.game") or "wardogs").strip().lower()
    if game not in GAMES:
        raise StageError("Unknown game {0!r} for the kill-feed reader; choose one of: {1}.".format(game, ", ".join(GAMES)))
    mw4 = game == "mw4"
    match = killfeed.make_matcher(player)
    classify = killfeed.classify_icons_mw4 if mw4 else killfeed.classify_icons
    fps = float(settings.get("killfeed.fps", 1.0))
    scale = int(settings.get("killfeed.scale", 3))
    if mw4:
        crop = [float(x) for x in str(settings.get("killfeed.mw4.feed_crop", ":".join(str(v) for v in killfeed.MW4_FEED_CROP))).split(":")]
        if len(crop) != 4:
            raise StageError("killfeed.mw4.feed_crop must look like X:Y:W:H as fractions (got {0!r})".format(settings.get("killfeed.mw4.feed_crop")))
        popup_fps = float(settings.get("killfeed.mw4.popup_fps", 6.0))
        popup_threshold = float(settings.get("killfeed.mw4.popup_threshold", killfeed.MW4_POPUP_THRESHOLD))
        popup_gap = float(settings.get("killfeed.mw4.popup_gap", killfeed.MW4_POPUP_GAP))
    else:
        crop = [int(x) for x in str(settings.get("killfeed.crop", "620:230:0:440")).split(":")]
        if len(crop) != 4:
            raise StageError("killfeed.crop must look like W:H:X:Y (got {0!r})".format(settings.get("killfeed.crop")))
    chunk_seconds = float(settings.get("killfeed.chunk_seconds", 1800))
    ffmpeg = resolve_tool(settings.tool("ffmpeg"), FFMPEG_HINT)

    scratch = ws.root / "_killfeed"
    scratch.mkdir(parents=True, exist_ok=True)
    downloaded = False
    source = Path(video) if video else ws.video_path()
    if not source or not Path(source).exists():
        if ws.source_mode() != MODE_EMBED:
            raise StageError("No video for {0}; pass --video PATH.".format(ws.slug))
        resolver = ytsegments.StreamResolver(ws, settings)
        source = scratch / "video.mp4"
        if not source.exists():
            progress.phase("download", total=1, unit="fraction")
            ytsegments.download_video_track(resolver, source, settings, progress, ws.logs_dir / "killfeed-download.log")
        downloaded = True
    duration = _probe_duration(settings, source)

    if mw4:
        # everything as fractions of the frame, sized against the 1280x720 reference the geometry was measured in,
        # so a 1080p copy of the same layout is cropped and OCR'd at the same scale
        fx, fy, fw, fh = crop
        ref_w, ref_h = killfeed.MW4_REF
        feed_vf = "crop=iw*{2}:ih*{3}:iw*{0}:ih*{1},scale={4}:{5}:flags=lanczos".format(
            fx, fy, fw, fh, int(round(fw * ref_w * scale)), int(round(fh * ref_h * scale)))
        px, py, pw, ph = killfeed.skull_region_fractions()
        popup_vf = "fps={0:g},crop=iw*{3}:ih*{4}:iw*{1}:ih*{2},scale={5}:{6}:flags=area,format=gray".format(
            popup_fps, px, py, pw, ph, killfeed.MW4_SKULL_REGION[2], killfeed.MW4_SKULL_REGION[3])
        want_confirm = False  # the popup's skulls are template-matched, not OCR'd (see killfeed.py)
        cfrac = [px, py, pw, ph]
    else:
        cw, ch, cx, cy = crop
        feed_vf = "crop={0}:{1}:{2}:{3},scale={4}:{5}:flags=lanczos".format(cw, ch, cx, cy, cw * scale, ch * scale)
        # the centre-of-screen "KILL CONFIRMED" popup, as fractions of the frame (any resolution): no layout covers it
        want_confirm = bool(settings.get("killfeed.confirm", True))
        cfrac = [float(x) for x in str(settings.get("killfeed.confirm_crop", "0.335:0.65:0.33:0.15")).split(":")]
        if len(cfrac) != 4:
            raise StageError("killfeed.confirm_crop must look like X:Y:W:H as fractions (got {0!r})".format(settings.get("killfeed.confirm_crop")))
        confirm_vf = "crop=iw*{2}:ih*{3}:iw*{0}:ih*{1},scale=1260:330:flags=lanczos".format(*cfrac)
    scan_end = min(duration, float(last)) if last else duration
    scan_start = max(0.0, float(first)) if first else 0.0
    chunks = _chunks(scan_end, chunk_seconds, start=scan_start)
    progress.phase("scan", total=max(1, len(chunks)), unit="chunks")
    obs: List[Dict[str, Any]] = []
    confirm_obs: List[Dict[str, Any]] = []
    popup_obs: List[Tuple[float, List[Tuple[float, int]]]] = []   # (time, skulls) for frames that have any
    popup_frames_seen = 0
    frames_seen = 0
    try:
        for ci, (t0, span) in enumerate(chunks):
            progress.check_cancelled()
            frames = scratch / "frames"
            cframes = scratch / "confirm"
            for d in (frames, cframes):
                if d.exists():
                    shutil.rmtree(d)
                d.mkdir()
            argv = [ffmpeg, "-hide_banner", "-nostats", "-loglevel", "error", "-nostdin", "-y", "-ss", "{0:.3f}".format(t0),
                    "-t", "{0:.3f}".format(span), "-i", str(source)]
            if mw4:
                # two rates from one decode: the feed is OCR'd at `fps`, the popup (up for ~1 s) is matched at `popup_fps`
                argv += ["-filter_complex", "[0:v]split=2[a][b];[a]fps={0:g},{1}[f];[b]{2}[p]".format(fps, feed_vf, popup_vf),
                         "-map", "[f]", "-q:v", "2", str(frames / "%06d.jpg"), "-map", "[p]", "-f", "rawvideo", str(scratch / "popup.raw")]
            elif want_confirm:
                argv += ["-filter_complex", "[0:v]fps={0:g},split=2[a][b];[a]{1}[f];[b]{2}[c]".format(fps, feed_vf, confirm_vf),
                         "-map", "[f]", "-q:v", "2", str(frames / "%06d.jpg"), "-map", "[c]", "-q:v", "2", str(cframes / "%06d.jpg")]
            else:
                argv += ["-vf", "fps={0:g},{1}".format(fps, feed_vf), "-q:v", "2", str(frames / "%06d.jpg")]
            proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
                                  encoding="utf-8", errors="replace")
            if proc.returncode != 0:
                raise StageError("ffmpeg failed extracting kill-feed frames: {0}".format(proc.stderr[-400:]))
            n_frames = len(list(frames.glob("*.jpg")))
            frames_seen += n_frames
            jsonl = scratch / "ocr.jsonl"
            _ocr_dir(frames, jsonl, progress)
            for line in open(jsonl, encoding="utf-8"):
                d = json.loads(line)
                idx = int(Path(d["f"]).stem)
                t = t0 + (idx - 1) / fps
                words = [w for ln in d["lines"] for w in ln["w"]]
                for o in killfeed.observe(words, t, d["f"], match, bare=mw4):
                    with Image.open(frames / d["f"]) as im:
                        crop_im = _icon_crop(im, o, mw4=mw4).copy()
                    o["blobs"] = killfeed.icon_blobs(crop_im, killfeed.MW4_ICON_THRESH) if mw4 else killfeed.icon_blobs(crop_im)
                    o["cls"] = classify(o["blobs"])[0]
                    o["crop"] = crop_im
                    obs.append(o)
            if mw4:
                raw_path = scratch / "popup.raw"
                per_frame = killfeed.popup_frames(raw_path.read_bytes(), threshold=popup_threshold)
                popup_frames_seen += len(per_frame)
                popup_obs.extend((t0 + i / popup_fps, hits) for i, hits in enumerate(per_frame) if hits)
                raw_path.unlink()
            if want_confirm:
                cjsonl = scratch / "ocr_confirm.jsonl"
                _ocr_dir(cframes, cjsonl, progress)
                for line in open(cjsonl, encoding="utf-8"):
                    d = json.loads(line)
                    seen = killfeed.parse_confirm([w for ln in d["lines"] for w in ln["w"]])
                    if seen is not None:
                        confirm_obs.append(dict(seen, t=t0 + (int(Path(d["f"]).stem) - 1) / fps))
            shutil.rmtree(frames, ignore_errors=True)
            shutil.rmtree(cframes, ignore_errors=True)
            progress.update(ci + 1, label="{0:.0f} min".format((t0 + span) / 60.0))
    finally:
        shutil.rmtree(scratch / "frames", ignore_errors=True)
        shutil.rmtree(scratch / "confirm", ignore_errors=True)
        if (scratch / "popup.raw").exists():
            (scratch / "popup.raw").unlink()

    try:
        popup_list: List[Dict[str, Any]] = []
        popup_unsure = 0
        if mw4:
            # a feed row read once is kept only if a popup confirms it: a single OCR hit alone is as likely noise
            events = _events(obs, ws, fps, min_obs=1, mw4=True)
            runs = killfeed.popup_runs(popup_obs, popup_gap, fps=popup_fps)
            # a run whose skull never scored `MW4_POPUP_STRONG` on 3 frames stands only if a feed row agrees with it
            popup_list = [r for r in runs if r["sure"] or any(
                r["first"] - killfeed.CONFIRM_MATCH <= e["first"] <= r["last"] + killfeed.CONFIRM_MATCH for e in events)]
            popup_unsure = len(runs) - len(popup_list)
            events = killfeed.merge_confirms(events, popup_list, popup_fps, prefer_popup_time=True)
            noise = [e for e in events if e["source"] == "feed" and e["n_obs"] < 2]
            for e in noise:
                if e.get("icon"):
                    (ws.root / e["icon"]).unlink()
            events = [e for e in events if e not in noise]
        else:
            events = _events(obs, ws, fps)
            if want_confirm:
                events = killfeed.merge_confirms(events, killfeed.group_confirms(confirm_obs), fps)
        text_stats: Dict[str, Any] = {}
        if mw4 and text and events:
            text_stats = _read_popup_text(events, Path(source), settings, progress)
        doc = {
            "schema": SCHEMA, "game": game, "player": player, "video": str(source) if not downloaded else "youtube:" + ws.slug,
            "fps": fps, "crop": crop, "scale": scale, "duration": duration, "frames": frames_seen,
            "scanned_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "kills": len(events),
            "nades": sum(1 for e in events if e["weapon"] == "grenade"),
            "headshots": sum(1 for e in events if e["headshot"]), "events": events,
        }
        if first or last:
            doc["window"] = [scan_start, scan_end]
        if mw4:
            doc.update({"confirm_crop": cfrac, "confirm_sightings": sum(r["n_obs"] for r in popup_list),
                        "popup_fps": popup_fps, "popup_threshold": popup_threshold, "popup_frames": popup_frames_seen,
                        "popup_unsure_dropped": popup_unsure,
                        "feed_only": sum(1 for e in events if e["source"] == "feed"),
                        "confirm_only": sum(1 for e in events if e["source"] == "confirm"),
                        "both": sum(1 for e in events if e["source"] == "both")})
            if text_stats:
                doc["popup_text"] = text_stats
        elif want_confirm:
            doc.update({"confirm_crop": cfrac, "confirm_sightings": len(confirm_obs),
                        "feed_only": sum(1 for e in events if e["source"] == "feed"),
                        "confirm_only": sum(1 for e in events if e["source"] == "confirm"),
                        "both": sum(1 for e in events if e["source"] == "both")})
        if not events:
            doc["note"] = ("No kills found. If the player was in the match, the stream's overlay probably covers the kill "
                           "feed (a webcam over the left-middle of the screen) and the popup was not read either, or the player "
                           "had no kills.")
        ws.write_json(out_path, doc)
        ws.mark_stage(STAGE, kills=len(events), player=player)
        log.info("Kill feed: %d kill(s) for %s (%d grenade, %d headshot) in %s", len(events), player, doc["nades"],
                 doc["headshots"], ws.slug)
        return out_path
    finally:
        if downloaded and not keep_video and (scratch / "video.mp4").exists():
            (scratch / "video.mp4").unlink()


def _probe_duration(settings: Settings, path: Path) -> float:
    ffprobe = resolve_tool(settings.tool("ffprobe"), FFMPEG_HINT)
    proc = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    try:
        return float(proc.stdout.strip())
    except ValueError:
        raise StageError("could not read the duration of {0}".format(path))


def _events(obs: List[Dict[str, Any]], ws: Workspace, fps: float, min_obs: int = 2, mw4: bool = False) -> List[Dict[str, Any]]:
    """Group observations into kill events and save each event's best icon crop.

    A run seen on fewer than `min_obs` frames is dropped as OCR noise unless it carries a distance and an icon.
    `mw4` reads the icons by that game's measurements (`killfeed.classify_icons_mw4`)."""
    import collections

    classify = killfeed.classify_icons_mw4 if mw4 else killfeed.classify_icons
    min_h = 12 if mw4 else 24    # MW4's icons are ~25 px tall at the working scale, Wardogs' ~45

    runs = killfeed.group_runs(obs)
    # a run that mixes two weapon classes with real support for each is two kills
    split: List[List[Dict[str, Any]]] = []
    for r in runs:
        cnt = collections.Counter(o["cls"] for o in r if o["cls"] in ("gun", "grenade"))
        if len(cnt) == 2 and min(cnt.values()) >= 2:
            for cls in cnt:
                split.append([o for o in r if o["cls"] in (cls, "unknown", "other")])
        else:
            split.append(r)

    events: List[Dict[str, Any]] = []
    for r in split:
        cls_count = collections.Counter(o["cls"] for o in r if o["cls"] != "unknown")
        want = cls_count.most_common(1)[0][0] if cls_count else "unknown"
        pool = [o for o in r if o["cls"] == want] or r

        def area(o):
            bl = [b for b in o["blobs"] if (b[3] - b[2]) >= min_h]
            return (bl[0][1] - bl[0][0]) * (bl[0][3] - bl[0][2]) if bl else 0

        best = max(pool, key=area)
        named = max(r, key=lambda o: (o["strong"], o["vlen"]))
        weapon, head, cover = classify(best["blobs"])
        events.append({
            "first": r[0]["t"], "last": r[-1]["t"], "dist_m": killfeed.modal_distance(r), "n_obs": len(r),
            "victim_raw": named["victim"], "victim": killfeed.readable_name(named["victim"]),
            "weapon": weapon, "headshot": head, "through_cover": cover,
            "strength": "strong" if any(o["strong"] for o in r) else "weak",
            "blobs": [list(b) for b in best["blobs"]], "_crop": best["crop"], "_has_icon": bool(best["blobs"]),
        })
    events = killfeed.drop_prefixed_twins(events)
    # a lone frame with no icon at all is OCR noise
    events = [e for e in events if e["n_obs"] >= min_obs or (e["dist_m"] is not None and e["_has_icon"])]
    events.sort(key=lambda e: e["first"])

    icons = ws.root / "killfeed_icons"
    if icons.exists():
        shutil.rmtree(icons)
    icons.mkdir(parents=True)
    out = []
    for n, e in enumerate(events, start=1):
        crop_im = e.pop("_crop")
        e.pop("_has_icon")
        kid = "k{0:03d}".format(n)
        rel = "killfeed_icons/{0}.png".format(kid)
        # keep the weapon and its modifier icons; the distance tag is drawn by the callout itself
        if mw4:
            real = [b for b in e["blobs"] if (b[1] - b[0]) >= 10 and (b[3] - b[2]) >= 12]
            right = max(b[1] for b in real) + 6 if real else None   # the victim's name follows the skull: not part of the icon
        else:
            right = killfeed.icon_extent([tuple(b) for b in e["blobs"]])
        if right is not None and right < crop_im.width:
            crop_im = crop_im.crop((0, 0, max(8, right), crop_im.height))
        crop_im.save(ws.root / rel)
        # The row appears on the first frame after the kill: on average half a frame late.
        e.update({"id": kid, "t": round(max(0.0, e["first"] - 0.5 / fps), 2), "icon": rel})
        out.append(e)
    return out


TEXT_FPS = 6.0
TEXT_BEFORE, TEXT_SPAN = 0.1, 5.7     # each kill's window: from just before it to ~5.6 s after (medals trail by up to ~3 s)


def _read_popup_text(events: List[Dict[str, Any]], source: Path, settings: Settings, progress: Progress = NULL_PROGRESS
                     ) -> Dict[str, Any]:
    """Read the popup stack's text for every kill and put it on the event as `popup` (see `killfeed.attribute_lines`).

    One short ffmpeg decode per kill: gray frames of the stack's block at `TEXT_FPS`, scaled to the 1280x720
    reference size so any resolution of the same layout reads the same. A kill the text pass says is a HEADSHOT KILL
    has `headshot` set (the feed's skull is read less often than the popup's text). Returns what was read.
    """
    import numpy as np

    ffmpeg = resolve_tool(settings.tool("ffmpeg"), FFMPEG_HINT)
    bx, by, bw, bh = killfeed.MW4_TEXT_BLOCK
    rw, rh = killfeed.MW4_REF
    vf = "fps={0:g},crop=iw*{1}:ih*{2}:iw*{3}:ih*{4},scale={5}:{6}:flags=area,format=gray".format(
        TEXT_FPS, bw / rw, bh / rh, bx / rw, by / rh, bw, bh)
    size = bw * bh
    progress.phase("text", total=max(1, len(events)), unit="kills")
    found: List[Dict[str, Any]] = []
    for n, e in enumerate(events):
        progress.check_cancelled()
        a = max(0.0, float(e["t"]) - TEXT_BEFORE)
        proc = subprocess.run([ffmpeg, "-hide_banner", "-nostats", "-loglevel", "error", "-nostdin", "-ss", "{0:.3f}".format(a),
                               "-t", "{0:.3f}".format(TEXT_SPAN), "-i", str(source), "-vf", vf, "-f", "rawvideo", "-"],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if proc.returncode != 0:
            raise StageError("ffmpeg failed extracting popup frames: {0}".format(proc.stderr.decode("utf-8", "replace")[-400:]))
        raw = proc.stdout
        frames = np.frombuffer(raw, dtype=np.uint8)[:(len(raw) // size) * size].reshape(-1, bh, bw)
        found.extend(killfeed.read_lines(list(frames), a, TEXT_FPS))
        progress.update(n + 1, label="{0:.0f} min".format(float(e["t"]) / 60.0))
    instances = killfeed.merge_instances(found)
    killfeed.attribute_lines(events, instances)
    for e in events:
        if e["popup"]["kind"] == "HEADSHOT KILL":
            e["headshot"] = True
    counts: Dict[str, int] = {}
    for ln in instances:
        counts[ln["text"]] = counts.get(ln["text"], 0) + 1
    return {"seeds": killfeed.MW4_TEXT_FILE.name, "lines": len(instances), "phrases": dict(sorted(counts.items(), key=lambda kv: -kv[1])),
            "kills_with_kind": sum(1 for e in events if e["popup"]["kind"]),
            "kills_with_no_text": sum(1 for e in events if not e["popup"]["lines"])}


def read_popup_text(ws: Workspace, settings: Settings, video: Optional[Path] = None, progress: Progress = NULL_PROGRESS) -> Path:
    """(Re)read the popup text of an MW4 `killfeed.json` already scanned, without scanning again (~1 min for 240 kills).

    Needs the video on disk (a YouTube workspace: scan with `--keep-video`, or pass `--video`).
    """
    doc = load(ws)
    if doc is None:
        raise StageError("{0} has no killfeed.json; run: clipbot killfeed scan --workspace {0} --game mw4".format(ws.slug))
    if doc.get("game") != "mw4":
        raise StageError("The popup text is read for --game mw4 scans only (this killfeed.json is {0}).".format(doc.get("game", "wardogs")))
    source = Path(video) if video else ws.video_path()
    if not source or not Path(source).exists():
        raise StageError("No video for {0}; pass --video PATH.".format(ws.slug))
    events = doc["events"]
    stats = _read_popup_text(events, Path(source), settings, progress)
    doc["popup_text"] = stats
    doc["headshots"] = sum(1 for e in events if e["headshot"])
    ws.write_json(killfeed_path(ws), doc)
    log.info("Popup text: %d line(s) over %d kill(s) (%d with a kind, %d with no text) in %s", stats["lines"], len(events),
             stats["kills_with_kind"], stats["kills_with_no_text"], ws.slug)
    return killfeed_path(ws)
