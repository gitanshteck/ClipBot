"""Stage: render a complete edit from an edit spec.

`clipbot edit render` is the general counterpart of `compile.py`. A compilation is
a concat of ranges from one kind of source; an edit is a timeline of clips from any
mix of YouTube videos, Kick VODs and files, each retimed, framed, graded and
decorated, joined by transitions, scored with music and sound effects, and mastered
to a loudness target. The spec (`editspec.py`) says *what*; `edittimeline.py` works
out *when*; `editfilters.py` builds the ffmpeg graphs; this module does the IO.

Pipeline
--------
1. **sources** - open every workspace/file the spec names and probe it.
2. **segments** - for each clip, cut `[in - pad, out + pad]` into a near-lossless,
   short-GOP, lossless-audio *source segment* (cached, shared between edits). From
   YouTube that is `ytsegments.fetch_segment` (only those seconds cross the
   network); from a file it is one ffmpeg cut. The segment is what the clip's
   retiming pieces seek into, which is why its GOP is 15 frames: a seek then costs
   at most half a second of decode instead of four.
3. **clips** - one ffmpeg per clip -> a video-only mp4 and a wav, cached by a
   fingerprint of everything that shapes them.
4. **assemble** - the clips' video is joined by concat-copy when every join is a cut
   (no second encode at all) or by an xfade graph when any join overlaps; the audio
   is joined by concat/acrossfade into a lossless wav.
5. **master** - music and sound effects are mixed in (beds sidechain-ducked by the
   game audio), the result is loudness-measured and then normalised in a second,
   linear pass, and encoded to AAC once.
6. **mux** - the picture is *copied* next to the new audio. An audio change never
   costs a video generation.

A clip's encode quality depends on its fate: if the join is concat-copy the clips
*are* the final picture and are encoded at final quality; if an xfade re-encodes
them anyway they are encoded at an intermediate quality and the assembler produces
the final.

`-/filter_complex <file>` is used rather than `-filter_complex_script`: the latter
was removed from ffmpeg 8, and a 40-piece retime graph is far over any command-line
limit.
"""

import hashlib
import json
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .. import editaudio, editfilters as ef, editoverlay, editspec, edittimeline, library, ytsegments
from ..config import Settings
from ..ffrun import run_ffmpeg
from ..progress import NULL_PROGRESS, Progress
from ..specerror import SpecError
from ..utils import StageError, get_logger, human_size, resolve_tool
from ..workspace import MODE_EMBED, Workspace

log = get_logger(__name__)

STAGE = "edit"

# patched by tests, same convention as compile.py / reel.py
_run_ffmpeg = run_ffmpeg

FFMPEG_HINT = (
    "Download from https://www.gyan.dev/ffmpeg/builds/ (Windows), then either add the bin "
    "folder to PATH or set tools.ffmpeg in config/settings.json."
)

SEGMENT_GOP = 15  # frames; see the module docstring


# --------------------------------------------------------------------------
# paths
# --------------------------------------------------------------------------


def edits_root(settings: Settings) -> Path:
    """`work/_edits`. Underscore-prefixed like `_cache` and `_library`, so the
    dashboard's workspace listing and its slug validator both ignore it."""
    return Path(settings.work_root) / "_edits"


def edit_dir(settings: Settings, name: str) -> Path:
    return edits_root(settings) / name


def cache_root(settings: Settings) -> Path:
    return Path(settings.work_root) / "_cache" / "edit"


def _sha(*parts: Any) -> str:
    blob = "\x1f".join(str(p) for p in parts)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------
# probing
# --------------------------------------------------------------------------


def probe_media(ffprobe: str, path: Any) -> Dict[str, Any]:
    """Display size, frame rate, duration and audio presence of a media file.

    *Display* size, not stream size: phone footage stores rotation in side data, so
    a stream reporting 1920x1080 can decode as 1080x1920, and sizing the frame from
    the stream size letterboxes the whole edit.
    """
    proc = subprocess.run(
        [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, encoding="utf-8", errors="replace",
        timeout=60)
    if proc.returncode != 0:
        raise StageError("ffprobe could not read {0}: {1}".format(path, proc.stderr.strip()[-300:]))
    data = json.loads(proc.stdout or "{}")
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    if video is None:
        raise StageError("{0} has no video stream".format(path))
    w, h = int(video["width"]), int(video["height"])
    rotation = 0
    for sd in video.get("side_data_list", []) or []:
        if "rotation" in sd:
            rotation = int(round(float(sd["rotation"])))
    rotation = int((video.get("tags") or {}).get("rotate", rotation) or rotation)
    if abs(rotation) % 180 == 90:
        w, h = h, w
    rate = video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/1"
    try:
        n, d = rate.split("/")
        fps = float(n) / float(d) if float(d) else 0.0
    except (ValueError, ZeroDivisionError):
        fps = 0.0
    duration = float((data.get("format") or {}).get("duration") or video.get("duration") or 0.0)
    return {"w": w, "h": h, "fps": fps, "duration": duration, "has_audio": audio is not None}


def probe_audio_duration(ffprobe: str, path: Any) -> float:
    proc = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=60)
    try:
        return float(proc.stdout.strip())
    except ValueError:
        raise StageError("could not read the duration of {0}".format(path))


# --------------------------------------------------------------------------
# sources and segments
# --------------------------------------------------------------------------


class Source(object):
    """One spec source: a local video file, or a YouTube video fetched on demand."""

    def __init__(self, alias: str, settings: Settings, entry: Dict[str, Any]):
        self.alias = alias
        self.settings = settings
        self.resolver = None  # type: Optional[ytsegments.StreamResolver]
        self.path = None  # type: Optional[Path]
        ffprobe = resolve_tool(settings.tool("ffprobe"), FFMPEG_HINT)
        if "file" in entry:
            self.path = Path(entry["file"])
            if not self.path.is_file():
                raise StageError("source {0!r}: no such file {1}".format(alias, self.path))
            self.label = str(self.path)
        else:
            root = Path(settings.work_root) / entry["workspace"]
            if not root.is_dir():
                raise StageError("source {0!r}: no workspace {1!r} under {2}".format(alias, entry["workspace"], settings.work_root))
            ws = Workspace(root)
            video = ws.video_path()
            if video and video.exists():
                self.path = video
            elif ws.source_mode() == MODE_EMBED:
                self.resolver = ytsegments.StreamResolver(ws, settings)
            else:
                raise StageError("source {0!r}: workspace {1} has no video on disk and is not a YouTube workspace".format(
                    alias, entry["workspace"]))
            self.label = entry["workspace"]
        if self.path is not None:
            info = probe_media(ffprobe, self.path)
            st = self.path.stat()
            self.tag = "file:{0}:{1}:{2}".format(self.path, st.st_size, int(st.st_mtime))
            self.duration = info["duration"]
        else:
            streams = self.resolver.get()
            info = {"w": streams.width, "h": streams.height, "fps": streams.fps, "has_audio": True}
            self.tag = self.resolver.tag()
            self.duration = float(streams.video_duration or 0.0)
        self.w, self.h, self.fps, self.has_audio = info["w"], info["h"], info["fps"], info["has_audio"]

    def segment(self, start: float, end: float, seg_dir: Path, progress: Progress, base: float, span: float,
                label: str) -> Path:
        """The cached source segment for [start, end]; cut it if it is not there."""
        key = _sha(self.tag, "{0:.3f}".format(start), "{0:.3f}".format(end), SEGMENT_GOP, "v1")
        out = seg_dir / (key + ".mp4")
        if out.exists() and out.stat().st_size > 0:
            return out
        seg_dir.mkdir(parents=True, exist_ok=True)
        part = out.with_name(key + ".part.mp4")
        ffmpeg = resolve_tool(self.settings.tool("ffmpeg"), FFMPEG_HINT)
        log_path = seg_dir / (key + ".log")
        try:
            if self.resolver is not None:
                ytsegments.fetch_segment(
                    self.resolver, start, end, part, self.settings, "libx264", "veryfast", 12,
                    progress=progress, base=base, span=span, log_path=log_path, label=label,
                    audio_args=["-c:a", "alac"],
                    video_args=["-g", str(SEGMENT_GOP), "-keyint_min", str(SEGMENT_GOP)])
            else:
                argv = [ffmpeg, "-hide_banner", "-nostats", "-loglevel", "error", "-progress", "pipe:1", "-y",
                        "-ss", "{0:.3f}".format(start), "-t", "{0:.3f}".format(end - start), "-i", str(self.path),
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "12", "-g", str(SEGMENT_GOP),
                        "-keyint_min", str(SEGMENT_GOP), "-pix_fmt", "yuv420p"]
                argv += ["-c:a", "alac"] if self.has_audio else ["-an"]
                argv += ["-movflags", "+faststart", str(part)]
                _run_ffmpeg(argv, end - start, progress, base, span, log_path, part, label=label)
            part.replace(out)
        except BaseException:
            if part.exists():
                part.unlink()
            raise
        return out


# --------------------------------------------------------------------------
# helpers shared with the CLI
# --------------------------------------------------------------------------


def load_spec(spec: Any) -> Dict[str, Any]:
    """A path, a JSON string or a dict -> a validated spec."""
    if isinstance(spec, dict):
        return editspec.normalize(spec)
    p = Path(str(spec))
    if p.is_file():
        return editspec.load(p)
    raise SpecError("{0}: not a spec file".format(spec))


def preview_output(out: Dict[str, Any]) -> Dict[str, Any]:
    """The same frame shape at proxy size: shorter side 540 (never larger than the
    final), 30 fps, fastest encode."""
    p = dict(out)
    base = editspec.OUTPUT_PRESETS["preview_540p"]
    scale = min(1.0, 540.0 / min(out["width"], out["height"]))
    p["width"] = max(2, int(round(out["width"] * scale / 2.0)) * 2)
    p["height"] = max(2, int(round(out["height"] * scale / 2.0)) * 2)
    for k in ("fps", "crf", "x264_preset", "audio_bitrate", "bframes", "gop_seconds", "preview"):
        p[k] = base[k]
    p["fps"] = min(out["fps"], base["fps"])
    return p


def _video_encode_args(out: Dict[str, Any], tier: str) -> List[str]:
    """x264 arguments for a clip or the final picture. `tier` is final, intermediate
    or preview. GOP is half a second and closed, with 2 B-frames: the shape YouTube's
    upload guide asks for, and a clean seek point for every clip when the clips are
    joined by concat-copy."""
    if tier == "preview":
        preset, crf = "ultrafast", out["crf"]
    elif tier == "intermediate":
        preset, crf = "veryfast", 14
    else:
        preset, crf = out["x264_preset"], out["crf"]
    gop = max(1, int(round(out["fps"] * out["gop_seconds"])))
    return ["-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p", "-profile:v", "high",
            "-g", str(gop), "-keyint_min", str(gop), "-bf", str(out["bframes"]),
            "-x264-params", "open-gop=0:scenecut=0"]


def _overlay_pngs(entry: Dict[str, Any], clip: Dict[str, Any], out: Dict[str, Any], png_dir: Path, font: Optional[str],
                  styles: Dict[str, Any], assets: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Rasterise a clip's overlays (cached by content) and return their PNG info."""
    pngs = []
    for o in entry.get("overlays") or []:
        use_font = font
        if o.get("style") and o["style"] in styles:
            f = styles[o["style"]].get("font")
            if f and f in assets:
                use_font = str(assets[f]["path"])
        icon = o.get("image") if o["kind"] == "callout" else None
        key = editoverlay.fingerprint(o, (out["width"], out["height"]), use_font, icon)
        path = png_dir / (key + ".png")
        if not path.exists():
            w, h = editoverlay.render(o, (out["width"], out["height"]), path, font=use_font, icon=icon)
        else:
            from PIL import Image
            with Image.open(path) as im:
                w, h = im.size
        pngs.append({"path": str(path), "w": w, "h": h, "key": key})
    return pngs


def _write_filter(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _clip_fingerprint(clip: Dict[str, Any], entry: Dict[str, Any], out: Dict[str, Any], tier: str, src_tag: str,
                      seg_key: str, head: Optional[Dict[str, Any]], tail: Optional[Dict[str, Any]],
                      pngs: List[Dict[str, Any]], asset_stamp: str) -> str:
    blob = json.dumps({
        "clip": clip, "dur": entry["dur"], "frames": entry["frames"],
        "pieces": [[round(p["s0"], 4), round(p["s1"], 4), round(p["rate"], 4), p["kind"]] for p in entry["pieces"]],
        "fx": entry.get("fx"), "cam": entry.get("camera"), "gw": entry.get("grade_window"),
        "ov": [(o["t0"], o["t1"]) for o in entry.get("overlays") or []],
        "out": {k: out[k] for k in ("width", "height", "fps", "fit", "crf", "x264_preset", "bframes", "gop_seconds",
                                    "sample_rate")},
        "tier": tier, "src": src_tag, "seg": seg_key, "head": head, "tail": tail,
        "png": [p["key"] for p in pngs], "assets": asset_stamp, "v": 3,
    }, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------
# the render
# --------------------------------------------------------------------------


def render_edit(
    spec: Any,
    settings: Settings,
    preview: bool = False,
    force: bool = False,
    progress: Progress = NULL_PROGRESS,
    master: bool = True,
) -> Dict[str, Any]:
    """Render an edit. Returns a report dict (output path, timeline, loudness, pacing,
    warnings). `preview` renders a small fast proxy; `master=False` skips the audio
    mastering (the quick-look path)."""
    t_start = time.time()
    spec = load_spec(spec)
    name = spec["name"]
    out = preview_output(spec["output"]) if preview else spec["output"]
    spec = dict(spec, output=out)

    ffmpeg = resolve_tool(settings.tool("ffmpeg"), FFMPEG_HINT)
    ffprobe = resolve_tool(settings.tool("ffprobe"), FFMPEG_HINT)
    root = edit_dir(settings, name)
    cache = cache_root(settings)
    tag = "preview" if preview else "final"
    work = root / "cache" / tag
    for d in (root, work / "clips", work / "filters", work / "overlays", work / "logs", root / "render"):
        d.mkdir(parents=True, exist_ok=True)
    # a record of what this render used - never spec.json itself, which is the author's
    # (a preview's 540p output block must not replace the spec it was made from)
    (root / "render").mkdir(parents=True, exist_ok=True)
    (root / "render" / "{0}.{1}.spec.json".format(name, tag)).write_text(editspec.to_json(spec), encoding="utf-8")

    timeline = edittimeline.compile_timeline(spec)
    clips = timeline["clips"]
    warnings: List[str] = list(timeline["warnings"])
    fps, sr = float(out["fps"]), int(out["sample_rate"])
    needs_xfade = ef.needs_filter_assembly(clips)
    tier = "preview" if preview else ("intermediate" if needs_xfade else "final")

    progress.phase("prepare", total=1, unit="steps")
    sources = {alias: Source(alias, settings, entry) for alias, entry in spec["sources"].items()
               if any(c.get("src") == alias for c in spec["clips"])}
    for alias, s in sources.items():
        if abs(s.fps - fps) > 0.5 and s.fps and not preview:
            warnings.append("source {0!r} runs at {1:.2f} fps; the edit is {2:g} fps, so frames are repeated or dropped".format(
                alias, s.fps, fps))
    assets = {}
    styles = {}
    try:
        assets = library.asset_map(settings)
        styles = library.load_text_styles(settings)
    except Exception as exc:  # noqa: BLE001 - a missing library must not block an edit with no assets
        log.debug("library not available: %s", exc)
    font = editoverlay.find_font(library.fallback_font(settings))
    progress.update(1)

    # ---- segments
    pad = float(settings.get("edit.segment_pad", 0.6))
    seg_dir = cache / "seg"
    real = [(i, c) for i, c in enumerate(spec["clips"]) if c["type"] == "clip"]
    progress.phase("segments", total=max(1, len(real)), unit="clips")
    segs: Dict[int, Dict[str, Any]] = {}
    for n, (i, c) in enumerate(real):
        progress.check_cancelled()
        s = sources[c["src"]]
        start = max(0.0, c["in"] - pad)
        end = c["out"] + pad
        if s.duration:
            end = min(end, s.duration)
        path = s.segment(start, end, seg_dir, progress, n, 1.0, "{0} segment".format(c["id"]))
        segs[i] = {"path": str(path), "in_rel": c["in"] - start, "w": s.w, "h": s.h, "has_audio": s.has_audio,
                   "tag": s.tag, "key": path.stem}
        progress.update(n + 1)

    # ---- clips
    progress.phase("clips", total=max(1, len(clips)), unit="clips")
    clip_files: List[Dict[str, Any]] = []
    for n, entry in enumerate(clips):
        progress.check_cancelled()
        c = spec["clips"][n]
        head = entry["transition_in"] if entry["index"] > 0 else None
        nxt = clips[n + 1]["transition_in"] if n + 1 < len(clips) else None
        pngs = _overlay_pngs(entry, c, out, work / "overlays", font, styles, assets)
        seg = segs.get(n)
        asset_stamp = "|".join(sorted("{0}:{1}".format(e.get("asset", ""), assets.get(e.get("asset"), {}).get("path", ""))
                                      for e in (c.get("fx") or []) if e.get("asset")))
        fp = _clip_fingerprint(c, entry, out, tier, seg["tag"] if seg else "", seg["key"] if seg else "", head, nxt,
                               pngs, asset_stamp)
        vfile = work / "clips" / "{0}-{1}.mp4".format(c["id"], fp)
        afile = work / "clips" / "{0}-{1}.wav".format(c["id"], fp)
        clip_files.append({"id": c["id"], "video": vfile, "audio": afile})
        if not force and vfile.exists() and afile.exists():
            progress.update(n + 1, label=c["id"])
            continue
        cg = ef.build_clip_graph(c, entry, out, seg, head=head, tail=nxt, overlay_pngs=pngs, assets=assets,
                                 styles=styles, fallback_font=font)
        warnings.extend("{0}: {1}".format(c["id"], w) for w in cg["warnings"])
        script = _write_filter(work / "filters" / "{0}.txt".format(c["id"]), cg["filter"])
        argv = [ffmpeg, "-hide_banner", "-nostats", "-loglevel", "error", "-progress", "pipe:1", "-y"]
        for inp in cg["inputs"]:
            argv += inp
        argv += ["-/filter_complex", str(script), "-map", cg["vmap"]] + _video_encode_args(out, tier) + \
            ["-an", "-movflags", "+faststart", str(vfile) + ".part.mp4",
             "-map", cg["amap"], "-c:a", "pcm_s16le", "-ar", str(sr), str(afile) + ".part.wav"]
        try:
            _run_ffmpeg(argv, entry["dur"], progress, n, 1.0, work / "logs" / "{0}.log".format(c["id"]), None, label=c["id"])
            Path(str(vfile) + ".part.mp4").replace(vfile)
            Path(str(afile) + ".part.wav").replace(afile)
        except BaseException:
            for p in (Path(str(vfile) + ".part.mp4"), Path(str(afile) + ".part.wav")):
                if p.exists():
                    p.unlink()
            raise
        progress.update(n + 1, label=c["id"])

    # ---- assemble the picture
    progress.phase("assemble", total=3, unit="steps")
    total = timeline["duration"]
    video_master = work / "video_master.mp4"
    if not needs_xfade:
        lst = work / "video.ffconcat"
        lst.write_text("ffconcat version 1.0\n" + "".join(
            "file '{0}'\n".format(str(f["video"]).replace("\\", "/").replace("'", "'\\''")) for f in clip_files),
            encoding="utf-8")
        argv = [ffmpeg, "-hide_banner", "-nostats", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
                "-c", "copy", str(video_master)]
        _run_ffmpeg(argv, 0, progress, 0, 0, work / "logs" / "assemble-video.log", None)
    else:
        graph, label = ef.video_assembly(clips, fps)
        script = _write_filter(work / "filters" / "assemble-video.txt", graph)
        argv = [ffmpeg, "-hide_banner", "-nostats", "-loglevel", "error", "-progress", "pipe:1", "-y"]
        for f in clip_files:
            argv += ["-i", str(f["video"])]
        argv += ["-/filter_complex", str(script), "-map", "[{0}]".format(label)] + \
            _video_encode_args(out, "preview" if preview else "final") + ["-an", str(video_master)]
        _run_ffmpeg(argv, total, progress, 0, 1.0, work / "logs" / "assemble-video.log", None, label="assemble")
    progress.update(1)

    # ---- assemble and mix the sound
    dry = work / "audio_dry.wav"
    graph, label = ef.audio_assembly(clips)
    script = _write_filter(work / "filters" / "assemble-audio.txt", graph) if graph else None
    argv = [ffmpeg, "-hide_banner", "-nostats", "-loglevel", "error", "-y"]
    for f in clip_files:
        argv += ["-i", str(f["audio"])]
    if graph:
        argv += ["-/filter_complex", str(script), "-map", "[{0}]".format(label)]
    argv += ["-c:a", "pcm_s16le", "-ar", str(sr), str(dry)]  # one clip: no graph, the file is the track
    _run_ffmpeg(argv, 0, progress, 0, 0, work / "logs" / "assemble-audio.log", None)

    mixed = dry
    music, sfx = _soundtrack(spec, timeline, assets, ffprobe, ffmpeg, cache, warnings)
    if music or sfx:
        mixed = work / "audio_mix.wav"
        for k, m_ in enumerate(music):  # ffmpeg input numbers: 0 is the dry track, then beds, then effects
            m_["index"] = 1 + k
        for k, s_ in enumerate(sfx):
            s_["index"] = 1 + len(music) + k
        graph, label = ef.mix_graph(music, sfx, total, sr)
        script = _write_filter(work / "filters" / "mix.txt", graph)
        argv = [ffmpeg, "-hide_banner", "-nostats", "-loglevel", "error", "-y", "-i", str(dry)]
        for m in music:
            argv += ["-stream_loop", "-1", "-i", m["path"]]
        for s in sfx:
            argv += ["-i", s["path"]]
        argv += ["-/filter_complex", str(script), "-map", "[{0}]".format(label), "-c:a", "pcm_s16le", "-ar", str(sr), str(mixed)]
        _run_ffmpeg(argv, 0, progress, 0, 0, work / "logs" / "mix.log", None)
    progress.update(2)

    # ---- master: measure, then apply linearly, then encode once
    measured = None
    m = spec["master"]
    if master and m["normalize"]:
        measured = measure_loudness(ffmpeg, mixed, m)
        if measured is None:
            warnings.append("the mix is silent or too quiet to measure; loudness mastering was skipped")
    audio_out = work / "audio_master.m4a"
    # a failed measurement turns normalisation off rather than guessing at a gain
    filt = ef.master_filters(dict(m, normalize=bool(measured)), measured, total) if master else "anull"
    argv = [ffmpeg, "-hide_banner", "-nostats", "-loglevel", "error", "-y", "-i", str(mixed), "-af", filt,
            "-c:a", "aac", "-b:a", out["audio_bitrate"], "-ar", str(sr), "-ac", "2", str(audio_out)]
    _run_ffmpeg(argv, 0, progress, 0, 0, work / "logs" / "master.log", None)

    # ---- mux
    final = root / "render" / ("{0}.preview.mp4".format(name) if preview else "{0}.mp4".format(name))
    argv = [ffmpeg, "-hide_banner", "-nostats", "-loglevel", "error", "-y", "-i", str(video_master), "-i", str(audio_out),
            "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", "-movflags", "+faststart", str(final)]
    _run_ffmpeg(argv, 0, progress, 0, 0, work / "logs" / "mux.log", None)
    progress.update(3)

    pacing = edittimeline.pacing(timeline)
    report = {
        "name": name, "preview": preview, "output": str(final), "duration": round(total, 3),
        "frames": int(round(total * fps)), "fps": fps, "size": [out["width"], out["height"]],
        "bytes": final.stat().st_size, "clips": len(clips), "xfade": needs_xfade,
        "loudness_target": m["loudness_lufs"] if m["normalize"] else None,
        "measured_input": measured, "pacing": pacing, "warnings": warnings,
        "render_seconds": round(time.time() - t_start, 1),
        "cache": str(work),
    }
    (root / "render" / ("{0}.{1}.timeline.json".format(name, tag))).write_text(
        json.dumps({"timeline": timeline, "report": report}, indent=1, default=str), encoding="utf-8")
    log.info("Rendered %s: %.1fs, %s, %d clip(s), in %.0fs", final.name, total, human_size(report["bytes"]),
             len(clips), report["render_seconds"])
    return report


def _soundtrack(spec, timeline, assets, ffprobe, ffmpeg, cache, warnings) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Resolve music and sfx items to file paths, durations and peak-aligned starts."""
    def path_for(item: Dict[str, Any]) -> Path:
        if "asset" in item:
            a = assets.get(item["asset"])
            if a is None:
                raise StageError("{0}: asset {1} is not in the library (python -m clipbot library scan)".format(
                    item.get("id"), item["asset"]))
            return Path(a["path"])
        p = Path(item["file"])
        if not p.is_file():
            raise StageError("{0}: no such file {1}".format(item.get("id"), p))
        return p

    music = []
    for m in timeline["tracks"]["music"]:
        music.append(dict(m, path=str(path_for(m))))
    sfx = []
    cache_file = cache / "peaks.json"
    for s in timeline["tracks"]["sfx"]:
        p = path_for(s)
        info = editaudio.peak_offset(ffmpeg, p, cache_file)
        start, skip = editaudio.sfx_start(s["hit"], info["peak_at"], s["lead"], s["align"])
        take = min(info["duration"] - skip, 8.0 if s["align"] == "start" else info["peak_at"] + 1.2 - skip)
        if take < 0.05:
            warnings.append("sfx {0}: nothing left to play after aligning its peak".format(s["id"]))
            continue
        if skip > 0.01:
            warnings.append("sfx {0} would start {1:.2f}s before the video; its head was trimmed".format(s["id"], skip))
        sfx.append(dict(s, path=str(p), start=start, skip=skip, take=take, peak_at=info["peak_at"]))
    return music, sfx


def measure_loudness(ffmpeg: str, path: Any, master: Dict[str, Any]) -> Optional[Dict[str, float]]:
    """loudnorm pass 1: integrated loudness, true peak, range and threshold of a file.
    None when the file is silent (loudnorm reports -inf) or the output cannot be read."""
    argv = [ffmpeg, "-hide_banner", "-nostats", "-nostdin", "-i", str(path), "-af", ef.loudnorm_analysis(master),
            "-f", "null", "-"]
    proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
                          encoding="utf-8", errors="replace")
    text = proc.stderr or ""
    i = text.rfind("{")
    j = text.rfind("}")
    if i < 0 or j < i:
        return None
    try:
        raw = json.loads(text[i:j + 1])
        vals = {k: float(raw[k]) for k in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")}
    except (ValueError, KeyError):
        return None
    if vals["input_i"] < -69:  # loudnorm's floor: effectively silence
        return None
    return vals
