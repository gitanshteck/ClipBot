"""FastAPI app for the ClipBot dashboard.

Binds loopback only and has no authentication, so it also refuses requests whose
Host header isn't localhost (blocks DNS rebinding) and requires an X-ClipBot
header on mutating routes (a cross-origin page cannot set it without a preflight
that will fail, since no CORS headers are sent).
"""

import asyncio
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import chatsync
from .. import compilations
from .. import fxspec
from .. import library
from .. import manifest as manifest_module
from .. import platforms
from .. import review
from .. import speakerfx
from .. import ytsegments
from ..config import PROJECT_ROOT, load_settings
from ..ffrun import run_ffmpeg
from ..preview import PreviewRunner
from ..stages import analyze as analyze_stage
from ..stages import audio as audio_stage
from ..stages import chat as chat_stage
from ..stages import compile as compile_stage
from ..stages import cut as cut_stage
from ..stages import download as download_stage
from ..stages import reel as reel_stage
from ..stages import transcribe as transcribe_stage
from ..stages import diarize as diarize_stage
from ..stages import transliterate as transliterate_stage
from ..stages import youtube as youtube_stage
from .. import reelspec
from .. import speakers as speakers_module
from ..utils import StageError, ToolMissingError, get_logger, human_size, resolve_tool
from ..workspace import MODE_EMBED, Workspace, slug_for_url
from .jobs import BusLogHandler, EventBus, JobRunner
from .media import serve_file

log = get_logger("clipbot.server")

HERE = Path(__file__).resolve().parent
SLUG_RE = re.compile(r"^[a-z0-9._-]{1,120}$")

settings = load_settings()
bus = EventBus()
runner = JobRunner(settings, bus)
# Proxy previews live in the shared cache, deliberately outside every workspace:
# workspace_summary rglobs a workspace to report its disk usage, so previews
# stored inside one would inflate that figure with throwaway files.
previews = PreviewRunner(settings, bus, settings.work_root / "_cache" / "preview")

app = FastAPI(title="ClipBot", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
templates = Jinja2Templates(directory=str(HERE / "templates"))


def _asset_version() -> str:
    """Cache-buster for /static/*.css|js: a hash of their mtimes.

    Browsers cache these aggressively (no cache-control header, so pure
    heuristic freshness), and reload alone does not reliably revalidate a
    <script src> - so a stale tab can keep running yesterday's JS against
    today's API indefinitely. Templates append ?v={{ ASSET_V }} to every
    static reference so a real edit always changes the URL, not just the
    content behind it.
    """
    digest = hashlib.sha1()
    static_dir = HERE / "static"
    for name in (
        "app.css", "app.js", "review.css", "review.js", "reel.js", "fx.js",
        "compile.css", "compile.js", "player.js",
    ):
        path = static_dir / name
        if path.is_file():
            digest.update(str(path.stat().st_mtime_ns).encode("utf-8"))
    return digest.hexdigest()[:10]


templates.env.globals["ASSET_V"] = _asset_version()


# --- guards ---------------------------------------------------------------


@app.middleware("http")
async def guard(request: Request, call_next):
    host = (request.headers.get("host") or "").split(":")[0]
    if host not in ("127.0.0.1", "localhost", "[::1]", "::1"):
        return JSONResponse({"error": "bad host"}, status_code=400)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        if request.headers.get("x-clipbot") != "1":
            return JSONResponse({"error": "missing X-ClipBot header"}, status_code=403)
    return await call_next(request)


def get_workspace(slug: str) -> Workspace:
    if not SLUG_RE.match(slug or ""):
        raise HTTPException(status_code=400, detail="bad slug")
    if slug.startswith("_"):
        # SLUG_RE admits these, but work/_cache and work/_library are shared
        # infrastructure rather than workspaces - the listing route already
        # hides them, and they must not be reachable as one either.
        raise HTTPException(status_code=400, detail="reserved name")
    root = (settings.work_root / slug).resolve()
    work_root = settings.work_root.resolve()
    if work_root not in root.parents and root != work_root:
        raise HTTPException(status_code=400, detail="path escapes work root")
    if not root.is_dir():
        raise HTTPException(status_code=404, detail="no such workspace")
    return Workspace(root)


# --- job handlers ---------------------------------------------------------


def _h_download(ws, st, job, progress):
    url = job.options.get("url") or ws.read_state().get("url")
    if not url:
        raise StageError("No URL recorded for this workspace")
    # Kick: the full VOD. YouTube: audio only (see stages/youtube.py).
    return download_stage.acquire(
        url,
        ws,
        st,
        force=job.options.get("force", False),
        quality=job.options.get("quality"),
        progress=progress,
    )


def _h_chat(ws, st, job, progress):
    return chat_stage.fetch_chat(
        ws, st, force=job.options.get("force", False), progress=progress
    )


def _h_audio(ws, st, job, progress):
    return audio_stage.extract_audio(ws, st, force=job.options.get("force", False))


def _h_transcribe(ws, st, job, progress):
    return transcribe_stage.transcribe_audio(
        ws, st, force=job.options.get("force", False),
        backend=job.options.get("backend"), progress=progress,
    )


def _h_analyze(ws, st, job, progress):
    path = analyze_stage.analyze_transcript(ws, st, force=job.options.get("force", False))
    review.ensure_imported(ws)
    return path


def _h_transliterate(ws, st, job, progress):
    return transliterate_stage.transliterate_transcript(
        ws, st, force=job.options.get("force", False), progress=progress
    )


def _h_diarize(ws, st, job, progress):
    return diarize_stage.diarize_audio(
        ws, st, force=job.options.get("force", False), progress=progress
    )


def _h_cut(ws, st, job, progress):
    clips_dir = cut_stage.cut_clips(
        ws,
        st,
        force=job.options.get("force", False),
        clip_ids=job.options.get("clip_ids"),
        progress=progress,
    )
    manifest_module.write_manifest(ws, st)
    return clips_dir


def _h_reel(ws, st, job, progress):
    out = reel_stage.render_reels(
        ws,
        st,
        force=job.options.get("force", False),
        clip_ids=job.options.get("clip_ids"),
        preset=job.options.get("preset"),
        progress=progress,
    )
    manifest_module.write_manifest(ws, st)
    return out


def _h_compile(ws, st, job, progress):
    name = job.options.get("name")
    if not name:
        raise StageError("compile job requires a 'name'")
    return compile_stage.render_compilation(
        ws, st, name, force=job.options.get("force", False), progress=progress
    )


def _h_manifest(ws, st, job, progress):
    json_path, _ = manifest_module.write_manifest(ws, st)
    return json_path


def _h_cleanup(ws, st, job, progress):
    if not job.options.get("force"):
        pending = cut_stage.uncut_approved(ws)
        if pending:
            raise StageError(
                "{0} approved clip(s) are not cut yet. Cut them first, or force.".format(
                    len(pending)
                )
            )
        # Reels re-encode from the VOD, so deleting it strands any clip that has
        # reel settings but no rendered file.
        unrendered = reel_stage.unrendered_reels(ws)
        if unrendered:
            raise StageError(
                "{0} clip(s) have reel settings but no rendered reel. Render them "
                "first, or force.".format(len(unrendered))
            )
        # Same reasoning as reels: every compile segment re-encodes from the
        # VOD, so an outstanding compilation would be strandable too.
        unrendered_comps = compile_stage.unrendered_compilations(ws)
        if unrendered_comps:
            raise StageError(
                "{0} compilation(s) have segments but no rendered output. Render "
                "them first, or force.".format(len(unrendered_comps))
            )
        # A cross-stream compilation homed in a *different* workspace can
        # still depend on this VOD's footage.
        unrendered_elsewhere = compile_stage.unrendered_compilations_elsewhere(ws, st)
        if unrendered_elsewhere:
            detail = "; ".join(
                "{0!r} in workspace {1!r}".format(e["compilation"], e["workspace"])
                for e in unrendered_elsewhere
            )
            raise StageError(
                "{0} compilation(s) elsewhere still need this VOD's footage and "
                "aren't rendered yet ({1}). Render them first, or force.".format(
                    len(unrendered_elsewhere), detail
                )
            )
    download_stage.delete_vod(ws)
    return None


def _h_pipeline(ws, st, job, progress):
    force = job.options.get("force", False)
    url = job.options.get("url") or ws.read_state().get("url")
    progress.phase("download")
    media = download_stage.acquire(
        url, ws, st, force=force, quality=job.options.get("quality"), progress=progress
    )
    # Kick-only: YouTube has no chat stage yet.
    if ws.platform == platforms.KICK:
        progress.phase("chat")
        # Best-effort: an expired or chatless VOD must not sink the whole
        # pipeline, but it's fetched first because it's the one thing that
        # can't be recovered.
        try:
            chat_stage.fetch_chat(ws, st, force=force, progress=progress)
        except StageError as exc:
            log.warning("Chat unavailable, continuing without it: %s", exc)
    progress.phase("audio")
    audio_stage.extract_audio(ws, st, force=force, video=media)
    progress.phase("transcribe")
    transcribe_stage.transcribe_audio(ws, st, force=force, progress=progress)
    progress.phase("analyze")
    analyze_stage.analyze_transcript(ws, st, force=force)
    review.ensure_imported(ws)
    return ws.candidates_path


for _kind, _handler in (
    ("download", _h_download),
    ("chat", _h_chat),
    ("audio", _h_audio),
    ("transcribe", _h_transcribe),
    ("analyze", _h_analyze),
    ("transliterate", _h_transliterate),
    ("diarize", _h_diarize),
    ("cut", _h_cut),
    ("reel", _h_reel),
    ("compile", _h_compile),
    ("manifest", _h_manifest),
    ("cleanup", _h_cleanup),
    ("pipeline", _h_pipeline),
):
    runner.register(_kind, _handler)


# --- lifecycle ------------------------------------------------------------


@app.on_event("startup")
async def _startup():
    bus.bind_loop(asyncio.get_event_loop())
    BusLogHandler(bus).install()
    runner.start()
    log.info("Dashboard ready on http://%s:%s", settings.get("server.host", "127.0.0.1"), settings.get("server.port", 8765))


# --- status helpers -------------------------------------------------------


def stage_states(ws: Workspace) -> List[Dict[str, Any]]:
    """What's done, what's runnable, and what's blocked - derived from files."""
    state = ws.read_state()
    stages = state.get("stages", {})
    video = ws.video_path()

    def entry(key, label, done, ready, blocked_reason=""):
        return {
            "key": key,
            "label": label,
            "done": bool(done),
            "ready": bool(ready) and not done,
            "rerunnable": bool(done),
            "blocked": "" if (done or ready) else blocked_reason,
            "detail": stages.get(key, {}),
        }

    has_video = bool(video and video.exists())
    has_audio = ws.audio_path.exists()
    has_transcript = ws.transcript_path.exists()
    has_candidates = ws.candidates_path.exists()
    has_captions = ws.captions_path.exists()
    has_diarization = ws.diarization_path.exists()
    clip_doc = review.load(ws) if ws.clips_path.exists() else {"clips": []}
    approved = [c for c in clip_doc.get("clips") or [] if c.get("status") == "approved"]

    has_chat = ws.chat_path.exists()

    if platforms.platform_of(state) == platforms.YOUTUBE:
        # A YouTube workspace never downloads the video: the audio-only source
        # is the first artifact and the dashboard plays the embedded player. So
        # nothing here gates on `has_video`. Clips are cut straight from YouTube
        # (the same `cut` job, see clipbot/ytsegments.py). There are no chat /
        # reel / cleanup rows: chat replay isn't built, reels are not supported
        # for YouTube, and there is no VOD to delete.
        source_audio = ws.source_audio_path()
        return [
            entry("download", "Fetch audio", has_audio or bool(source_audio), True),
            entry("audio", "Extract audio", has_audio, bool(source_audio),
                  "needs the audio download"),
            entry("transcribe", "Transcribe", has_transcript, has_audio, "needs audio"),
            entry("analyze", "Find clips", has_candidates, has_transcript,
                  "needs a transcript"),
            entry("transliterate", "Hinglish captions", has_captions, has_transcript,
                  "needs a transcript"),
            entry(
                "diarize",
                "Speaker diarization",
                has_diarization,
                has_audio,
                "needs audio; also needs requirements-diarize.txt installed and "
                "an HF_TOKEN env var",
            ),
            entry(
                "cut",
                "Fetch approved clips",
                bool(stages.get("cut")),
                bool(approved),
                "needs at least one approved clip",
            ),
        ]

    return [
        entry("download", "Download VOD", has_video, True),
        # Deliberately before audio in the list: chat is the only artifact that
        # expires (Kick drops it with the VOD after 7/30 days) and cannot be
        # re-derived, so the UI should nudge toward fetching it first.
        entry("chat", "Fetch chat", has_chat, bool(state.get("url")), "needs a downloaded VOD"),
        entry("audio", "Extract audio", has_audio, has_video, "needs the video"),
        entry("transcribe", "Transcribe", has_transcript, has_audio, "needs audio"),
        entry("analyze", "Find clips", has_candidates, has_transcript, "needs a transcript"),
        entry(
            "transliterate",
            "Hinglish captions",
            has_captions,
            has_transcript,
            "needs a transcript",
        ),
        entry(
            "diarize",
            "Speaker diarization",
            has_diarization,
            has_audio,
            "needs audio; also needs requirements-diarize.txt installed and "
            "an HF_TOKEN env var",
        ),
        entry(
            "cut",
            "Cut clips",
            bool(stages.get("cut")),
            has_video and bool(approved),
            "needs the video and at least one approved clip"
            if not has_video
            else "needs at least one approved clip",
        ),
        entry(
            "reel",
            "Vertical reels",
            bool(stages.get("reel")),
            has_video and bool(approved),
            "needs the video and at least one approved clip",
        ),
        entry(
            "cleanup",
            "Delete VOD",
            state.get("video_deleted", False),
            has_video,
            # Reached only when there's no video AND it wasn't deleted - i.e.
            # nothing has been downloaded yet.
            "nothing downloaded yet",
        ),
    ]


def external_activity(ws: Workspace) -> Optional[Dict[str, Any]]:
    """Detect work happening outside the dashboard's own job runner.

    The CLI can be driven directly (`python -m clipbot run ...`) and that process
    is invisible to this server. Without this, a running download looks idle and
    the UI happily offers a Run button that would start a second one.

    Detection is filesystem-based, which is what makes it work regardless of who
    started the job or whether the server has been restarted since.
    """
    partials = list(ws.root.glob("*.part")) + list(ws.root.glob("*.ytdl"))
    if partials:
        newest = max(partials, key=lambda p: p.stat().st_mtime)
        total = 0
        for p in ws.root.glob("*.part*"):
            try:
                total += p.stat().st_size
            except OSError:
                pass
        age = time.time() - newest.stat().st_mtime
        return {
            "kind": "download",
            "source": "cli",
            "bytes": total,
            "human": human_size(total),
            "stalled": age > 120,
            "idle_seconds": round(age, 1),
        }

    # Whisper writes transcript.json only at the very end, so an in-progress
    # transcription leaves no partial file. Fall back to spotting the process.
    #
    # Match on the video id, not the slug: `clipbot run <url>` carries the URL
    # (".../gitanshteck/videos/79ac1495-...") while the slug joins channel and id
    # with a hyphen ("gitanshteck-79ac1495-..."), so a slug substring test never
    # matches a pipeline started from a URL - which is exactly how the CLI is
    # normally driven. Case-insensitive because YouTube ids are case-sensitive
    # ("dQw4w9WgXcQ") while slugs are lowercased; the true id is in state.json.
    needles = {(ws.video_id or ws.slug.split("-", 1)[-1]).lower(), ws.slug.lower()}
    try:
        import subprocess as _sp

        out = _sp.run(
            ["wmic", "process", "where", "name='python.exe'", "get", "commandline"],
            stdout=_sp.PIPE, stderr=_sp.DEVNULL, timeout=6,
        ).stdout.decode("utf-8", "replace")
        for line in out.splitlines():
            lowered = line.lower()
            if "clipbot" not in lowered:
                continue
            if not any(needle in lowered for needle in needles):
                continue
            for kind in (
                "transcribe", "analyze", "cut", "audio", "download", "compile", "run",
            ):
                if " {0}".format(kind) in line:
                    return {
                        "kind": "pipeline" if kind == "run" else kind,
                        "source": "cli",
                        "bytes": 0,
                        "human": "",
                        "stalled": False,
                    }
    except Exception:
        pass
    return None


def _backfill_dimensions(ws: Workspace, state: Dict[str, Any]) -> Dict[str, Any]:
    """Probe resolution for VODs downloaded before we recorded it."""
    video = ws.video_path()
    if state.get("video_height") or not (video and video.exists()):
        return state
    try:
        binary = resolve_tool(settings.tool("ffprobe"), "")
        out = subprocess.run(
            [
                binary, "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height", "-of", "json", str(video),
            ],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20,
        )
        stream = json.loads(out.stdout.decode("utf-8"))["streams"][0]
        return ws.update_state(
            video_width=stream.get("width"), video_height=stream.get("height")
        )
    except Exception as exc:
        log.debug("Could not probe %s: %s", video, exc)
        return state


def workspace_summary(ws: Workspace) -> Dict[str, Any]:
    state = _backfill_dimensions(ws, ws.read_state())
    video = ws.video_path()
    clip_doc = review.load(ws) if ws.clips_path.exists() else None
    tally = review.counts(clip_doc) if clip_doc else None
    cut_files = sorted(ws.clips_dir.glob("*.mp4")) if ws.clips_dir.is_dir() else []

    disk = 0
    for p in ws.root.rglob("*"):
        if p.is_file():
            try:
                disk += p.stat().st_size
            except OSError:
                pass

    title = state.get("title")
    if not title and (ws.root / "video.info.json").exists():
        # yt-dlp writes info.json up front, so the real title is available long
        # before the download finishes and state.title gets populated.
        try:
            title = ws.read_json(ws.root / "video.info.json").get("title")
        except (ValueError, OSError):
            title = None

    return {
        "slug": ws.slug,
        # Which library tab this workspace belongs to. Absent in state.json
        # means Kick: every workspace that predates YouTube support is one.
        "platform": platforms.platform_of(state),
        # local | embed | none - derived from what is on disk (see
        # Workspace.source_mode), never stored.
        "source_mode": ws.source_mode(),
        "video_id": state.get("video_id"),
        "title": title or state.get("url") or ws.slug,
        "external": external_activity(ws),
        "url": state.get("url"),
        "uploader": state.get("uploader"),
        "duration": state.get("duration"),
        "video_present": bool(video and video.exists()),
        "video_deleted": bool(state.get("video_deleted")),
        "video_width": state.get("video_width"),
        "video_height": state.get("video_height"),
        "low_res": bool(
            state.get("video_height")
            and int(state.get("video_height")) < int(settings.get("download.min_height_warn", 480))
        ),
        "disk_bytes": disk,
        "disk_human": human_size(disk),
        "counts": tally,
        "cut_files": len(cut_files),
        "stages": stage_states(ws),
        "active_job": runner.active_for(ws.slug),
        "updated_at": state.get("updated_at"),
    }


# --- pages ----------------------------------------------------------------


PLATFORM_COOKIE = "clipbot_platform"


def _library_page(request: Request, platform: str):
    template = "library.html" if platform == platforms.KICK else "library_youtube.html"
    response = templates.TemplateResponse(
        template, {"request": request, "platform": platform}
    )
    # Remember the tab, so "/" (and the brand link) returns to it.
    response.set_cookie(
        PLATFORM_COOKIE, platform, max_age=365 * 24 * 3600, samesite="lax"
    )
    return response


@app.get("/")
async def page_home(request: Request):
    """The library is one tab per platform: send you to the one you used last
    (YouTube on a first visit, since that is where new streams are)."""
    last = request.cookies.get(PLATFORM_COOKIE)
    target = last if last in platforms.PLATFORMS else platforms.YOUTUBE
    return RedirectResponse("/" + target, status_code=302)


@app.get("/kick", response_class=HTMLResponse)
async def page_library_kick(request: Request):
    return _library_page(request, platforms.KICK)


@app.get("/youtube", response_class=HTMLResponse)
async def page_library_youtube(request: Request):
    return _library_page(request, platforms.YOUTUBE)


def _player_source(ws: Workspace) -> Dict[str, Any]:
    """What the review and compile pages should play (static/player.js).

    `youtube`: embed the video - a YouTube workspace with no video on disk.
    `local`: the page's own <video> element, exactly as before; this is also
    what a Kick workspace whose VOD was deleted gets, so it still shows its
    existing "no video available" message.
    """
    if ws.source_mode() == MODE_EMBED:
        state = ws.read_state()
        return {
            "kind": "youtube",
            "video_id": state.get("video_id"),
            # The audio-probed length: the clock the transcript and every clip
            # range are on. The embed falls back to YouTube's own figure only
            # if this is missing.
            "duration": state.get("duration"),
        }
    return {"kind": "local"}


@app.get("/w/{slug}", response_class=HTMLResponse)
async def page_workspace(request: Request, slug: str):
    ws = get_workspace(slug)
    # `platform` lights the right topbar tab and points "back" at that tab.
    return templates.TemplateResponse(
        "workspace.html", {"request": request, "slug": slug, "platform": ws.platform}
    )


@app.get("/w/{slug}/review", response_class=HTMLResponse)
async def page_review(request: Request, slug: str):
    ws = get_workspace(slug)
    # The review UI draws the cut padding on the timeline, so it needs the same
    # numbers the cut stage will actually apply.
    return templates.TemplateResponse(
        "review.html",
        {
            "request": request,
            "slug": slug,
            "platform": ws.platform,
            "source": _player_source(ws),
            "pad_start": float(settings.get("cut.pad_start", 1.0)),
            "pad_end": float(settings.get("cut.pad_end", 1.5)),
        },
    )


@app.get("/w/{slug}/compile", response_class=HTMLResponse)
async def page_compile(request: Request, slug: str):
    ws = get_workspace(slug)
    # Same padding passthrough as page_review, for the same reason: the
    # compile page's timeline draws each segment's padded range, which has to
    # match what render_compilation will actually cut.
    return templates.TemplateResponse(
        "compile.html",
        {
            "request": request,
            "slug": slug,
            "platform": ws.platform,
            "source": _player_source(ws),
            "pad_start": float(settings.get("cut.pad_start", 1.0)),
            "pad_end": float(settings.get("cut.pad_end", 1.5)),
        },
    )


# --- API ------------------------------------------------------------------


@app.get("/api/workspaces")
async def api_workspaces():
    root = settings.work_root
    if not root.is_dir():
        return {"workspaces": []}
    out = []
    for path in sorted(root.iterdir()):
        if not path.is_dir() or path.name.startswith("_"):
            continue
        try:
            out.append(workspace_summary(Workspace(path)))
        except Exception as exc:
            log.warning("Skipping %s: %s", path.name, exc)
    out.sort(key=lambda w: w.get("updated_at") or 0, reverse=True)
    return {"workspaces": out}


@app.get("/api/workspaces/{slug}")
async def api_workspace(slug: str):
    return workspace_summary(get_workspace(slug))


@app.get("/api/kick/{channel}/vods")
async def api_channel_vods(channel: str, limit: int = 20):
    """List a Kick channel's VODs, so the dashboard can offer them without the
    user hunting down and pasting a URL per stream."""
    channel = channel.strip().lower()
    if not re.match(r"^[a-z0-9_-]{1,60}$", channel):
        raise HTTPException(status_code=400, detail="invalid channel name")
    vods = download_stage.list_channel_vods(channel, limit=max(1, min(limit, 50)))
    for vod in vods:
        slug = slug_for_url(vod["url"])
        vod["slug"] = slug
        vod["workspace_exists"] = (settings.work_root / slug).is_dir()
    return {"channel": channel, "vods": vods}


# A channel listing spawns yt-dlp and asks YouTube (several seconds, and
# repeated automated requests are what bot checks look for), so page loads are
# served from this short cache; the Fetch button passes refresh=1.
_STREAMS_TTL = 600.0
_streams_cache: Dict[Any, Any] = {}


@app.get("/api/youtube/{handle}/streams")
async def api_youtube_streams(handle: str, limit: int = 20, refresh: int = 0):
    """List a YouTube channel's past streams, so the dashboard can offer them
    without the user hunting down and pasting a URL per stream."""
    handle = handle.strip().lstrip("@")
    if not re.match(r"^[A-Za-z0-9._-]{1,60}$", handle):
        raise HTTPException(status_code=400, detail="invalid channel handle")
    limit = max(1, min(limit, 50))
    key = (handle.lower(), limit)
    cached = _streams_cache.get(key)
    if cached and not refresh and time.time() - cached[0] < _STREAMS_TTL:
        streams = cached[1]
    else:
        try:
            streams = await asyncio.to_thread(
                youtube_stage.list_channel_streams, handle, settings, limit
            )
        except (StageError, ToolMissingError) as exc:
            # Not a 500: the message says what to do (update yt-dlp, install a
            # JS runtime...) and the page shows it.
            raise HTTPException(status_code=502, detail=str(exc))
        _streams_cache[key] = (time.time(), streams)
    out = []
    for stream in streams:
        item = dict(stream)
        slug = slug_for_url(item["url"])
        item["slug"] = slug
        # Recomputed on every call, cached or not, so "Add" turns into "Open"
        # the moment a workspace exists.
        item["workspace_exists"] = (settings.work_root / slug).is_dir()
        out.append(item)
    return {"channel": handle, "streams": out}


@app.post("/api/workspaces")
async def api_create_workspace(payload: Dict[str, Any] = Body(...)):
    url = (payload.get("url") or "").strip()
    try:
        # `platform` is the library tab the request came from: it only matters
        # for a bare YouTube video id, which is ambiguous without it.
        parsed = platforms.parse_url(url, platform_hint=payload.get("platform"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if parsed is not None:
        canonical = parsed.canonical_url
    elif "kick.com" in url:
        # Unchanged legacy leniency: yt-dlp handles some Kick URL shapes the
        # slug regex doesn't know, and this route always accepted them.
        canonical = url
    else:
        raise HTTPException(
            status_code=400,
            detail="Expected a kick.com or youtube.com VOD URL",
        )
    ws = Workspace.for_url(settings.work_root, canonical)
    job = runner.submit(
        "pipeline",
        ws.slug,
        {
            "url": canonical,
            "force": bool(payload.get("force")),
            "quality": payload.get("quality"),
        },
    )
    return {"slug": ws.slug, "job": job.to_dict()}


@app.get("/api/workspaces/{slug}/clips")
async def api_clips(slug: str):
    ws = get_workspace(slug)
    doc = review.ensure_imported(ws) if ws.candidates_path.exists() else review.load(ws)
    return {"clips": doc.get("clips", []), "counts": review.counts(doc)}


@app.patch("/api/workspaces/{slug}/clips/{clip_id}")
async def api_update_clip(slug: str, clip_id: str, payload: Dict[str, Any] = Body(...)):
    ws = get_workspace(slug)
    duration = ws.read_state().get("duration")
    try:
        clip = review.update_clip(ws, clip_id, payload, duration=duration)
    except KeyError:
        raise HTTPException(status_code=404, detail="no such clip")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    bus.publish("clips", {"slug": slug})
    return clip


@app.post("/api/workspaces/{slug}/clips")
async def api_add_clip(slug: str, payload: Dict[str, Any] = Body(...)):
    ws = get_workspace(slug)
    try:
        clip = review.add_manual_clip(
            ws,
            float(payload["start"]),
            float(payload["end"]),
            title=payload.get("title", ""),
            description=payload.get("description", ""),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="start and end are required numbers")
    bus.publish("clips", {"slug": slug})
    return clip


@app.get("/api/workspaces/{slug}/compilations")
async def api_compilations(slug: str):
    ws = get_workspace(slug)
    return {"compilations": compilations.list_compilations(ws)}


@app.post("/api/workspaces/{slug}/compilations")
async def api_save_compilation(slug: str, payload: Dict[str, Any] = Body(...)):
    """Create, or fully replace the segment list of, a named compilation.

    Whole-document replace, not additive - this is the compile page's own
    editor Save. Adding one segment from elsewhere (the review page's "+
    Compilation" action) goes through the dedicated segments route below
    instead, so two tabs adding to the same compilation don't clobber each
    other's work.
    """
    ws = get_workspace(slug)
    try:
        comp = compilations.upsert(
            ws, payload.get("name"), payload.get("segments") or [], settings
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    bus.publish("compilations", {"slug": slug})
    return comp


@app.post("/api/workspaces/{slug}/compilations/{name}/segments")
async def api_add_compilation_segment(
    slug: str, name: str, payload: Dict[str, Any] = Body(...)
):
    ws = get_workspace(slug)
    try:
        comp = compilations.add_segment(ws, name, payload, settings)
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    bus.publish("compilations", {"slug": slug})
    return comp


@app.delete("/api/workspaces/{slug}/compilations/{name}")
async def api_delete_compilation(slug: str, name: str):
    ws = get_workspace(slug)
    try:
        compilations.delete(ws, name)
    except KeyError:
        raise HTTPException(status_code=404, detail="no such compilation")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    bus.publish("compilations", {"slug": slug})
    return {"deleted": name}


@app.get("/api/workspaces/{slug}/transcript")
async def api_transcript(slug: str):
    ws = get_workspace(slug)
    if not ws.transcript_path.exists():
        return {"segments": [], "present": False}
    doc = ws.read_json(ws.transcript_path)
    return {
        "present": True,
        "duration": doc.get("duration"),
        "language": doc.get("language"),
        "model": doc.get("model"),
        "segment_count": doc.get("segment_count"),
        "low_confidence_count": doc.get("low_confidence_count"),
        "realtime_factor": doc.get("realtime_factor"),
        "segments": doc.get("segments", []),
    }


@app.get("/api/workspaces/{slug}/captions")
async def api_captions(slug: str):
    # Same {id, start, end} keying as transcript.json's segments (see
    # stages/transliterate.py), so a frontend can index this by id to swap
    # display text without re-deriving offsets.
    ws = get_workspace(slug)
    if not ws.captions_path.exists():
        return {"segments": [], "present": False}
    doc = ws.read_json(ws.captions_path)
    return {
        "present": True,
        "model": doc.get("model"),
        "segment_count": doc.get("segment_count"),
        "failed_batches": doc.get("failed_batches"),
        "segments": doc.get("segments", []),
    }


@app.get("/api/workspaces/{slug}/speakers")
async def api_speakers(slug: str):
    """Transcript segments merged with their assigned speaker, for the
    transcript view's per-segment speaker chip + range-assign UI. Also
    reports whether diarization.json exists, so the UI can tell "never ran"
    apart from "ran and everything's unassigned"."""
    ws = get_workspace(slug)
    if not ws.transcript_path.exists():
        return {"present": False, "segments": [], "diarized": False}
    transcript = ws.read_json(ws.transcript_path)
    map_doc = speakers_module.load(ws)
    return {
        "present": True,
        "diarized": ws.diarization_path.exists(),
        "segments": speakers_module.resolved_segments(transcript, map_doc),
    }


@app.post("/api/workspaces/{slug}/speakers/assign")
async def api_speakers_assign(slug: str, payload: Dict[str, Any] = Body(...)):
    """Manual override: assign (or, with speaker_id=null, clear) every
    segment id in [start_id, end_id]. Also the only path at all for a
    workspace where diarization never ran."""
    ws = get_workspace(slug)
    try:
        start_id = int(payload["start_id"])
        end_id = int(payload["end_id"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=400, detail="start_id/end_id must be integers")
    doc = speakers_module.assign_range(ws, start_id, end_id, payload.get("speaker_id"))
    return doc


@app.post("/api/workspaces/{slug}/speakers/rename")
async def api_speakers_rename(slug: str, payload: Dict[str, Any] = Body(...)):
    """Bulk-rewrite every assignment pointing at `old_id` to `new_id` - the
    "name this diarization cluster" / "merge into an existing speaker"
    action, one call instead of one PATCH per segment."""
    ws = get_workspace(slug)
    old_id = str(payload.get("old_id") or "")
    new_id = str(payload.get("new_id") or "")
    if not old_id or not new_id:
        raise HTTPException(status_code=400, detail="old_id and new_id are required")
    doc = speakers_module.rename_speaker(ws, old_id, new_id)
    return doc


@app.post("/api/workspaces/{slug}/chat/sync")
async def api_chat_sync(slug: str, payload: Dict[str, Any] = Body(...)):
    """Estimate chat_offset_seconds by correlating chat rate against audio energy.

    Read-only unless `apply` is set, in which case the chosen estimate (the
    boundary heuristic when correlation isn't confident - see chatsync.py) is
    written into state.json, the same value `_chat_window` in stages/reel.py
    falls back to for any clip without its own chat.offset override.
    """
    ws = get_workspace(slug)
    try:
        chat_doc = chat_stage.load_chat(ws)
    except StageError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    try:
        result = chatsync.estimate_offset(ws, chat_doc, settings)
    except StageError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    if payload.get("apply"):
        chosen = result["offset_seconds"]
        if not result["confident"] and result.get("boundary_offset_seconds") is not None:
            chosen = result["boundary_offset_seconds"]
        ws.update_state(chat_offset_seconds=chosen)
        result["applied_offset_seconds"] = chosen

    return result


@app.get("/api/workspaces/{slug}/chat/offset")
async def api_chat_offset_get(slug: str):
    ws = get_workspace(slug)
    return {"chat_offset_seconds": ws.read_state().get("chat_offset_seconds", 0.0)}


@app.patch("/api/workspaces/{slug}/chat/offset")
async def api_chat_offset_set(slug: str, payload: Dict[str, Any] = Body(...)):
    """Manual override - the dashboard slider lands here after eyeballing sync."""
    try:
        value = float(payload["chat_offset_seconds"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=400, detail="chat_offset_seconds must be a number")
    ws = get_workspace(slug)
    ws.update_state(chat_offset_seconds=round(value, 1))
    return {"chat_offset_seconds": round(value, 1)}


@app.get("/api/workspaces/{slug}/chat/messages")
async def api_chat_messages(slug: str):
    """Raw harvested chat, offset-corrected by the workspace's current
    chat_offset_seconds, for the review page's sidebar/density track."""
    ws = get_workspace(slug)
    if not ws.chat_path.exists():
        return {"present": False, "messages": []}
    try:
        doc = chat_stage.load_chat(ws)
    except StageError as exc:
        return {"present": False, "reason": str(exc), "messages": []}
    offset = float(ws.read_state().get("chat_offset_seconds", 0.0))
    messages = chat_stage.messages_between(doc, -1e9, 1e9, offset=offset)
    return {
        "present": True,
        "count": len(messages),
        "chat_offset_seconds": offset,
        "messages": messages,
    }


@app.post("/api/workspaces/{slug}/reel/plan")
async def api_reel_plan(slug: str, payload: Dict[str, Any] = Body(...)):
    """Resolve a reel spec into pixel rectangles. Pure maths - runs no ffmpeg.

    The browser preview draws from this rather than doing its own layout, so the
    preview and the render share one implementation and cannot disagree.
    """
    ws = get_workspace(slug)
    state = _backfill_dimensions(ws, ws.read_state())
    src_w = int(state.get("video_width") or 0)
    src_h = int(state.get("video_height") or 0)
    if not src_w or not src_h:
        raise HTTPException(status_code=409, detail="source dimensions unknown")

    spec = payload.get("spec") or reelspec.default_spec(settings)
    if payload.get("canvas"):
        spec = dict(spec, canvas=payload["canvas"])
    try:
        plan = reelspec.resolve(spec, src_w, src_h, settings)
    except reelspec.SpecError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    plan["source"] = [src_w, src_h]
    plan["filter_complex"] = reelspec.build_filter(plan)
    plan["warn_above"] = float(settings.get("reel.warn_upscale_above", 1.5))

    clip = _clip_or_none(ws, payload.get("clip_id"))
    origin = 0.0
    span = float(state.get("duration") or 0.0) or 1.0
    if clip is not None:
        start, end = cut_stage._padded_range(clip, settings, state.get("duration"))
        origin, span = start, max(0.1, end - start)

    # Effects are resolved here too, non-strict, so the FX strip can draw itself
    # and flag a missing sound or an effect that has fallen outside the clip -
    # all without running ffmpeg. Only pixel-accurate confirmation costs a
    # render, which is what /reel/preview is for.
    plan["fx_plan"] = None
    plan["fx_warnings"] = []
    if plan.get("fx"):
        try:
            fx_plan = fxspec.resolve_fx(
                plan["fx"], origin=origin, span=span, canvas=plan["canvas"],
                assets=library.asset_map(settings),
                styles=library.load_text_styles(settings),
                next_input=1, strict=False,
                fallback_font=library.fallback_font(settings),
            )
        except reelspec.SpecError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        plan["fx_plan"] = fx_plan
        plan["fx_warnings"] = (fx_plan or {}).get("warnings") or []
    plan["fx_defaults"] = settings.get("reel.fx", {})

    # Same "geometry only, skip the IO-heavy render assets" treatment as fx
    # above: resolve_speaker_slots is pure math once handed the workspace's
    # speaking spans, so the canvas preview can draw real, named slot rects
    # for this layer instead of nothing at all (the previously-documented gap -
    # unlike chat/captions, a speaker slot's position depends on who's
    # actually talking in this clip, which pure-math reelspec.resolve() alone
    # has no way to know).
    plan["speaker_slots"] = []
    if plan.get("speakers"):
        spans = []
        if ws.transcript_path.exists():
            try:
                transcript_doc = ws.read_json(ws.transcript_path)
                map_doc = speakers_module.load(ws)
                resolved_segs = speakers_module.resolved_segments(transcript_doc, map_doc)
                spans = speakers_module.speaking_spans(resolved_segs)
            except (ValueError, OSError):
                spans = []
        slots = speakerfx.resolve_speaker_slots(plan["speakers"], spans, plan["canvas"], origin, span)
        if slots:
            registry = {s["id"]: s for s in library.list_speakers(settings)}
            for slot in slots:
                profile = registry.get(slot["speaker_id"])
                slot["name"] = profile["name"] if profile else slot["speaker_id"]
                slot["color"] = (profile or {}).get("color") or "#19A2D2"
            plan["speaker_slots"] = slots
    return plan


def _clip_or_none(ws: Workspace, clip_id):
    if not clip_id:
        return None
    try:
        return review.get_clip(review.load(ws), str(clip_id))
    except Exception:  # noqa: BLE001 - the plan is still useful without it
        return None


@app.post("/api/workspaces/{slug}/reel/apply")
async def api_reel_apply(slug: str, payload: Dict[str, Any] = Body(...)):
    """Write one spec to every matching clip, and remember it as the default."""
    ws = get_workspace(slug)
    try:
        spec = reelspec.normalize(payload.get("spec") or {})
    except reelspec.SpecError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    scope = payload.get("scope", "approved")
    doc = review.load(ws)
    updated = 0
    for clip in doc.get("clips") or []:
        if scope == "approved" and clip.get("status") not in (
            review.STATUS_APPROVED, review.STATUS_CUT
        ):
            continue
        clip["reel"] = dict(spec)
        clip["updated_at"] = time.time()
        updated += 1
    review.save(ws, doc)
    # New clips inherit this without the user re-applying.
    ws.update_state(reel_default=spec)
    bus.publish("clips", {"slug": slug})
    return {"updated": updated}


@app.post("/api/workspaces/{slug}/reel/preview")
async def api_reel_preview(slug: str, payload: Dict[str, Any] = Body(...)):
    """Render (or return) a low-resolution proxy through the real filter graph.

    Never automatic: the instant canvas preview stays the tool for crop work,
    and this only runs when the user asks for pixel-accurate confirmation.
    """
    ws = get_workspace(slug)
    clip = _clip_or_none(ws, payload.get("clip_id"))
    if clip is None:
        raise HTTPException(status_code=404, detail="no such clip")

    try:
        spec = reelspec.normalize(payload.get("spec") or reelspec.default_spec(settings))
        built = reel_stage.preview_command(
            ws, settings, clip, spec, window=payload.get("window")
        )
    except reelspec.SpecError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except StageError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    key = built["key"]
    out_path = previews.path_for(key)
    # The key names the file, so the destination is only knowable now. It is
    # excluded from the fingerprint, so writing it here cannot change the key.
    built["argv"][-1] = str(out_path)

    body = {
        "key": key,
        "url": "/media/{0}/preview/{1}.mp4".format(slug, key),
        "window": [built["start"], built["start"] + built["span"]],
        "duration": built["duration"],
        "canvas": built["canvas"],
        "warnings": built["warnings"],
    }
    if out_path.exists() and out_path.stat().st_size > 0:
        body["status"] = "ready"
        body["cached"] = True
        return body

    argv = built["argv"]
    span = built["span"]

    def render(progress, target):
        run_ffmpeg(argv, span, progress, 0.0, 1.0,
                   ws.logs_dir / "preview.log", target)

    previews.submit(key, slug, render)
    body["status"] = "rendering"
    body["cached"] = False
    return body


@app.post("/api/workspaces/{slug}/reel/preview/cancel")
async def api_reel_preview_cancel(slug: str):
    get_workspace(slug)
    return {"cancelled": previews.cancel(slug)}


@app.get("/media/{slug}/preview/{key}.mp4")
async def media_preview(slug: str, key: str, request: Request):
    get_workspace(slug)
    try:
        path = previews.path_for(key)
    except KeyError:
        raise HTTPException(status_code=400, detail="bad preview key")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="no such preview")
    return serve_file(path, request.headers.get("range", ""))


# --- the cross-stream editing library -------------------------------------


@app.get("/api/library")
async def api_library():
    return library.load_index(settings)


@app.post("/api/library/rescan")
async def api_library_rescan():
    """Re-read the drop-in folders.

    Runs inline: only files whose size or mtime changed are re-hashed and
    re-probed, so even a few hundred assets come back in well under a second.
    """
    index = library.scan(settings)
    bus.publish("library", {"counts": index.get("counts")})
    return index


@app.patch("/api/library/assets/{asset_id}")
async def api_library_asset(asset_id: str, payload: Dict[str, Any] = Body(...)):
    try:
        asset = library.update_asset_meta(settings, asset_id, payload)
    except KeyError:
        raise HTTPException(status_code=404, detail="no such asset")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    bus.publish("library", {})
    return asset


@app.get("/api/library/presets")
async def api_library_presets():
    return {"presets": library.list_presets(settings)}


@app.post("/api/library/presets")
async def api_library_save_preset(payload: Dict[str, Any] = Body(...)):
    try:
        preset = library.save_preset(
            settings,
            name=payload.get("name") or "",
            effects=payload.get("effects"),
            anchor=payload.get("anchor", "at"),
            overwrite=bool(payload.get("overwrite")),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    bus.publish("library", {})
    return preset


@app.delete("/api/library/presets/{preset_id}")
async def api_library_delete_preset(preset_id: str):
    try:
        library.delete_preset(settings, preset_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="no such preset")
    bus.publish("library", {})
    return {"deleted": preset_id}


@app.get("/api/library/speakers")
async def api_library_speakers():
    return {"speakers": library.list_speakers(settings)}


@app.post("/api/library/speakers")
async def api_library_save_speaker(payload: Dict[str, Any] = Body(...)):
    """Create a new speaker profile, or update one in place if `speaker_id`
    names an existing one - the "name this diarization cluster" action uses
    this with `speaker_id=None` (new profile) and then
    `POST .../speakers/rename` to point existing assignments at it."""
    try:
        speaker = library.save_speaker(
            settings,
            name=payload.get("name") or "",
            avatar_asset=payload.get("avatar_asset"),
            color=payload.get("color"),
            style=payload.get("style"),
            speaker_id=payload.get("speaker_id"),
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="no such speaker")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    bus.publish("library", {})
    return speaker


@app.delete("/api/library/speakers/{speaker_id}")
async def api_library_delete_speaker(speaker_id: str):
    try:
        library.delete_speaker(settings, speaker_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="no such speaker")
    bus.publish("library", {})
    return {"deleted": speaker_id}


@app.get("/api/library/textstyles")
async def api_library_textstyles():
    return {"styles": library.load_text_styles(settings)}


@app.put("/api/library/textstyles/{style_id}")
async def api_library_save_textstyle(style_id: str, payload: Dict[str, Any] = Body(...)):
    try:
        saved = library.save_text_style(settings, style_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    bus.publish("library", {})
    return saved


@app.get("/media/library/{kind}/{name}")
async def media_library(kind: str, name: str, request: Request):
    """Audition a library asset. Range-aware so <audio> can seek."""
    try:
        path = library.resolve_media(settings, kind, name)
    except KeyError:
        raise HTTPException(status_code=404, detail="no such asset")
    return serve_file(path, request.headers.get("range", ""))


@app.post("/api/workspaces/{slug}/jobs")
async def api_submit_job(slug: str, payload: Dict[str, Any] = Body(...)):
    ws = get_workspace(slug)
    kind = payload.get("kind")
    options = {k: v for k, v in payload.items() if k != "kind"}
    try:
        job = runner.submit(kind, ws.slug, options)
    except StageError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return job.to_dict()


@app.get("/api/jobs")
async def api_jobs():
    return {"jobs": runner.recent()}


@app.post("/api/jobs/{job_id}/cancel")
async def api_cancel(job_id: str):
    if not runner.cancel(job_id):
        raise HTTPException(status_code=409, detail="job is not cancellable")
    return {"ok": True}


@app.get("/api/doctor")
async def api_doctor():
    import os

    checks = []

    def probe(name, key, hint):
        try:
            path = resolve_tool(settings.tool(key), "")
            checks.append({"name": name, "ok": True, "detail": path})
        except Exception:
            checks.append({"name": name, "ok": False, "detail": hint})

    probe("ffmpeg", "ffmpeg", "not found on PATH - set tools.ffmpeg to an absolute path")
    probe("ffprobe", "ffprobe", "not found on PATH - set tools.ffprobe")
    probe("yt-dlp", "yt_dlp", "pip install -U 'yt-dlp[default,curl-cffi]'")

    # YouTube needs a current yt-dlp plus a JS runtime; both checks shell out
    # (yt-dlp --version alone takes a moment), so keep them off the event loop.
    checks.extend(await asyncio.to_thread(youtube_stage.health_checks, settings))
    # Cutting clips/segments straight from YouTube needs ffmpeg's -request_size
    # HTTP option; without it every read is throttled to a crawl (see
    # clipbot/ytsegments.py). Only reported when ffmpeg can be run at all - the
    # ffmpeg check above already covers "not found".
    supported = await asyncio.to_thread(ytsegments.ffmpeg_supports_request_size, settings)
    if supported is not None:
        checks.append(
            {
                "name": "ffmpeg -request_size (YouTube clips)",
                "ok": supported,
                "detail": "supported" if supported else
                "this ffmpeg is too old (needs 8.1+): cutting from YouTube would fail - "
                "set tools.ffmpeg to a current build",
            }
        )

    checks.append(
        {
            "name": "ANTHROPIC_API_KEY",
            "ok": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "detail": "set" if os.environ.get("ANTHROPIC_API_KEY") else "not set - stage 4 will fail",
        }
    )

    # transcribe.backend defaults to "openai", so this is a core-pipeline key
    # in the same way ANTHROPIC_API_KEY already is, not a conditional opt-in.
    checks.append(
        {
            "name": "OPENAI_API_KEY",
            "ok": bool(os.environ.get("OPENAI_API_KEY")),
            "detail": "set" if os.environ.get("OPENAI_API_KEY") else "not set - openai-backend transcribe will fail",
        }
    )

    try:
        import ctranslate2

        n = ctranslate2.get_cuda_device_count()
        checks.append(
            {
                "name": "CUDA devices",
                "ok": True,
                "detail": "{0} (transcription runs on {1})".format(
                    n, "GPU" if n else "CPU - expect ~0.8x realtime"
                ),
            }
        )
    except Exception:
        checks.append({"name": "faster-whisper", "ok": False, "detail": "not installed"})

    try:
        usage = shutil.disk_usage(str(settings.work_root.anchor or settings.work_root))
        checks.append(
            {
                "name": "Free disk",
                "ok": usage.free > 5 * 1024 ** 3,
                "detail": human_size(usage.free),
            }
        )
    except Exception:
        pass

    checks.append(
        {
            "name": "Python",
            "ok": sys.version_info >= (3, 9),
            "detail": sys.version.split()[0],
        }
    )
    return {"checks": checks}


@app.post("/api/doctor/youtube")
async def api_doctor_youtube():
    """Actually ask YouTube for a public video's format list - the one check
    that proves extraction works end to end. A network round trip of several
    seconds, so the Doctor dialog runs it on request rather than on open."""
    return await asyncio.to_thread(youtube_stage.probe_youtube, settings)


# --- media ----------------------------------------------------------------


def _safe_media_path(
    directory: Path, name: str, filename: Optional[str] = None, kind: str = "file"
) -> Path:
    """Resolve `filename` (defaulting to `name`) inside `directory`, rejecting
    any name that could escape it. Shared by every /media/* route below - each
    only differs in which directory it serves from, whether the on-disk
    filename is the raw name or a derived one (a compilation's file is always
    "<name>.mp4"), and what to call the 404 if it's missing.
    """
    if "/" in name or "\\" in name or ".." in name:
        raise HTTPException(status_code=400, detail="bad name")
    path = (directory / (filename or name)).resolve()
    if directory.resolve() not in path.parents or not path.exists():
        raise HTTPException(status_code=404, detail="no such {0}".format(kind))
    return path


@app.get("/media/{slug}/video")
async def media_video(slug: str, request: Request):
    ws = get_workspace(slug)
    video = ws.video_path()
    if not video or not video.exists():
        raise HTTPException(status_code=404, detail="no video (deleted or not downloaded)")
    return serve_file(video, request.headers.get("range", ""))


@app.get("/media/{slug}/clip/{name}")
async def media_clip(slug: str, name: str, request: Request):
    ws = get_workspace(slug)
    path = _safe_media_path(ws.clips_dir, name, kind="clip")
    return serve_file(path, request.headers.get("range", ""))


@app.get("/media/{slug}/reel/{name}")
async def media_reel(slug: str, name: str, request: Request, download: int = 0):
    # Separate route because the clip route rejects names containing "/", and
    # reels live in clips/reels/.
    ws = get_workspace(slug)
    path = _safe_media_path(ws.reels_dir, name, kind="reel")
    return serve_file(
        path, request.headers.get("range", ""), download_name=name if download else ""
    )


@app.get("/media/{slug}/compilation/{name}")
async def media_compilation(slug: str, name: str, request: Request, download: int = 0):
    # A compilation's rendered file is always named "<name>.mp4" - see
    # render_compilation - so the compilation's name doubles as the on-disk
    # filename stem, no lookup through compilations.json needed.
    ws = get_workspace(slug)
    filename = "{0}.mp4".format(name)
    path = _safe_media_path(ws.compilations_dir, name, filename=filename, kind="compilation")
    return serve_file(
        path, request.headers.get("range", ""), download_name=filename if download else ""
    )


@app.get("/media/{slug}/manifest.csv")
async def media_manifest(slug: str):
    ws = get_workspace(slug)
    if not ws.manifest_csv_path.exists():
        raise HTTPException(status_code=404, detail="no manifest yet")
    return serve_file(ws.manifest_csv_path, "", download_name="{0}-manifest.csv".format(slug))


# --- events ---------------------------------------------------------------


@app.get("/api/events")
async def api_events(request: Request):
    q = asyncio.Queue()
    bus.subscribe(q)

    last_id = request.headers.get("last-event-id")
    try:
        last_id = int(last_id) if last_id else None
    except ValueError:
        last_id = None

    async def stream():
        try:
            for event in bus.replay_since(last_id):
                yield _sse(event)
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield _sse(event)
        finally:
            bus.unsubscribe(q)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
    )


def _sse(event: Dict[str, Any]) -> str:
    return "id: {0}\nevent: {1}\ndata: {2}\n\n".format(
        event["id"], event["kind"], json.dumps(event["payload"], ensure_ascii=False)
    )
