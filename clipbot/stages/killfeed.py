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


def _chunks(duration: float, size: float) -> List[Tuple[float, float]]:
    out, t = [], 0.0
    while t < duration - 0.5:
        out.append((t, min(size, duration - t)))
        t += size
    return out


def _icon_crop(img, o: Dict[str, Any]):
    x0, x1 = o["kx"] + 1, o["dx"] + 45
    cy, h = o["cy"], max(o["h"], 24)
    return img.crop((max(0, int(x0)), int(cy - 0.75 * h), max(int(x0) + 8, int(x1)), int(cy + 0.75 * h)))


def scan_killfeed(
    ws: Workspace,
    settings: Settings,
    player: Optional[str] = None,
    video: Optional[Path] = None,
    force: bool = False,
    keep_video: bool = False,
    progress: Progress = NULL_PROGRESS,
) -> Path:
    """Scan one workspace's video for the player's kills. Returns the killfeed.json path."""
    from PIL import Image

    out_path = killfeed_path(ws)
    if out_path.exists() and not force:
        log.info("%s already scanned (use force to redo)", out_path.name)
        return out_path

    player = (player or settings.get("killfeed.player") or "").strip()
    if not player:
        raise StageError("Which player? Pass --player NAME or set killfeed.player in config/settings.json.")
    match = killfeed.make_matcher(player)
    fps = float(settings.get("killfeed.fps", 1.0))
    scale = int(settings.get("killfeed.scale", 3))
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

    cw, ch, cx, cy = crop
    vf = "fps={0:g},crop={1}:{2}:{3}:{4},scale={5}:{6}:flags=lanczos".format(fps, cw, ch, cx, cy, cw * scale, ch * scale)
    chunks = _chunks(duration, chunk_seconds)
    progress.phase("scan", total=max(1, len(chunks)), unit="chunks")
    obs: List[Dict[str, Any]] = []
    frames_seen = 0
    try:
        for ci, (t0, span) in enumerate(chunks):
            progress.check_cancelled()
            frames = scratch / "frames"
            if frames.exists():
                shutil.rmtree(frames)
            frames.mkdir()
            argv = [ffmpeg, "-hide_banner", "-nostats", "-loglevel", "error", "-nostdin", "-y", "-ss", "{0:.3f}".format(t0),
                    "-t", "{0:.3f}".format(span), "-i", str(source), "-vf", vf, "-q:v", "2", str(frames / "%06d.jpg")]
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
                for o in killfeed.observe(words, t, d["f"], match):
                    with Image.open(frames / d["f"]) as im:
                        crop_im = _icon_crop(im, o).copy()
                    o["blobs"] = killfeed.icon_blobs(crop_im)
                    o["cls"] = killfeed.classify_icons(o["blobs"])[0]
                    o["crop"] = crop_im
                    obs.append(o)
            shutil.rmtree(frames, ignore_errors=True)
            progress.update(ci + 1, label="{0:.0f} min".format((t0 + span) / 60.0))
    finally:
        shutil.rmtree(scratch / "frames", ignore_errors=True)
        if downloaded and not keep_video and (scratch / "video.mp4").exists():
            (scratch / "video.mp4").unlink()

    events = _events(obs, ws, fps)
    doc = {
        "schema": SCHEMA, "player": player, "video": str(source) if not downloaded else "youtube:" + ws.slug,
        "fps": fps, "crop": crop, "scale": scale, "duration": duration, "frames": frames_seen,
        "scanned_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "kills": len(events),
        "nades": sum(1 for e in events if e["weapon"] == "grenade"),
        "headshots": sum(1 for e in events if e["headshot"]), "events": events,
    }
    if not events:
        doc["note"] = ("No kills found. If the player was in the match, the stream's overlay probably covers the kill "
                       "feed (a webcam over the left-middle of the screen), or the feed was not on.")
    ws.write_json(out_path, doc)
    ws.mark_stage(STAGE, kills=len(events), player=player)
    log.info("Kill feed: %d kill(s) for %s (%d grenade, %d headshot) in %s", len(events), player, doc["nades"],
             doc["headshots"], ws.slug)
    return out_path


def _probe_duration(settings: Settings, path: Path) -> float:
    ffprobe = resolve_tool(settings.tool("ffprobe"), FFMPEG_HINT)
    proc = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    try:
        return float(proc.stdout.strip())
    except ValueError:
        raise StageError("could not read the duration of {0}".format(path))


def _events(obs: List[Dict[str, Any]], ws: Workspace, fps: float) -> List[Dict[str, Any]]:
    """Group observations into kill events and save each event's best icon crop."""
    import collections

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
            bl = [b for b in o["blobs"] if (b[3] - b[2]) >= 24]
            return (bl[0][1] - bl[0][0]) * (bl[0][3] - bl[0][2]) if bl else 0

        best = max(pool, key=area)
        named = max(r, key=lambda o: (o["strong"], o["vlen"]))
        weapon, head, cover = killfeed.classify_icons(best["blobs"])
        events.append({
            "first": r[0]["t"], "last": r[-1]["t"], "dist_m": killfeed.modal_distance(r), "n_obs": len(r),
            "victim_raw": named["victim"], "victim": killfeed.readable_name(named["victim"]),
            "weapon": weapon, "headshot": head, "through_cover": cover,
            "strength": "strong" if any(o["strong"] for o in r) else "weak",
            "blobs": [list(b) for b in best["blobs"]], "_crop": best["crop"], "_has_icon": bool(best["blobs"]),
        })
    events = killfeed.drop_prefixed_twins(events)
    # a lone frame with no icon at all is OCR noise
    events = [e for e in events if e["n_obs"] >= 2 or (e["dist_m"] is not None and e["_has_icon"])]
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
        right = killfeed.icon_extent([tuple(b) for b in e["blobs"]])
        if right is not None and right < crop_im.width:
            crop_im = crop_im.crop((0, 0, max(8, right), crop_im.height))
        crop_im.save(ws.root / rel)
        # The row appears on the first frame after the kill: on average half a frame late.
        e.update({"id": kid, "t": round(max(0.0, e["first"] - 0.5 / fps), 2), "icon": rel})
        out.append(e)
    return out
