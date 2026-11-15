"""Stage 6a: upload a Kick VOD to YouTube.

Two ways to get the bytes there, picked by `mode` (`auto` by default):

* file   - the VOD is already on disk (stage 1 ran): upload that file. The
           resumable session URI is saved in state.json, so a dropped connection
           or a closed laptop resumes where it stopped instead of starting over.
* stream - nothing is downloaded. yt-dlp resolves Kick's HLS playlist, ffmpeg
           remuxes it (stream copy, no re-encode) to a fragmented MP4 on a pipe,
           and `ytupload.upload_stream` feeds that to YouTube in chunks. Disk use
           is zero and memory is a few chunks; the machine is just a relay.
           If YouTube refuses an upload of unknown length (its guide documents
           only known sizes) the bytes read so far are spooled to
           `_upload/spool.mp4` and the upload continues from there with a known
           size - slower and disk-hungry, but it still works.

Never part of `clipbot run` or the dashboard's `pipeline` job: it publishes to an
external service, so it only ever runs when asked for. Default visibility is
`private`.

Kick-only. A YouTube workspace is already on YouTube.
"""

import collections
import json
import re
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

from .. import platforms, youtube_auth, ytupload
from ..config import Settings
from ..progress import NULL_PROGRESS, Progress
from ..utils import StageError, get_logger, human_size, resolve_tool
from ..workspace import Workspace
from . import download as download_stage

log = get_logger(__name__)

STAGE = "upload"
MODES = ("auto", "stream", "file")

# Output container for the relayed stream. Fragmented MP4 is the default: it is
# an MP4 (on YouTube's documented format list) yet writable to a non-seekable
# pipe, which a plain MP4 (moov atom patched in at the end) is not.
CONTAINERS = {
    "mp4": (
        ["-movflags", "+frag_keyframe+empty_moov+default_base_moof", "-f", "mp4"],
        "video/mp4",
    ),
    "matroska": (["-f", "matroska"], "video/x-matroska"),
    "mpegts": (["-f", "mpegts"], "video/mp2t"),
}

# Kick's own duration runs ~30s long against what actually plays (see
# download.py), so "ffmpeg stopped early" is only declared past this slack.
SHORTFALL_SLACK_SECONDS = 120.0
SHORTFALL_SLACK_FRACTION = 0.03

_MIME_BY_SUFFIX = {
    ".mp4": "video/mp4",
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".ts": "video/mp2t",
    ".flv": "video/x-flv",
}


# --- resolving the VOD -----------------------------------------------------


class VodStream(NamedTuple):
    urls: List[str]                 # one muxed URL, or video + audio
    headers: Dict[str, str]
    info: Dict[str, Any]            # the whole yt-dlp info dict
    duration: Optional[float]
    approx_bytes: Optional[int]     # for a progress bar; never used for correctness


def _has(fmt: Dict[str, Any], key: str) -> bool:
    return fmt.get(key) not in (None, "none")


def vod_stream_from_info(info: Dict[str, Any]) -> VodStream:
    formats = info.get("requested_formats") or [info]
    video = next((f for f in formats if _has(f, "vcodec") and f.get("url")), None)
    if video is None:
        raise StageError("Kick offered no video stream for this VOD.")
    chosen = [video]
    if not _has(video, "acodec"):
        audio = next(
            (f for f in formats if _has(f, "acodec") and not _has(f, "vcodec") and f.get("url")),
            None,
        )
        if audio is None:
            raise StageError("Kick offered no audio stream for this VOD.")
        chosen.append(audio)

    duration = info.get("duration")
    size = 0
    for fmt in chosen:
        guess = fmt.get("filesize") or fmt.get("filesize_approx")
        if not guess and fmt.get("tbr") and duration:
            guess = float(fmt["tbr"]) * 1000.0 / 8.0 * float(duration)
        size += int(guess or 0)

    headers = {
        str(k): str(v)
        for k, v in (video.get("http_headers") or info.get("http_headers") or {}).items()
    }
    return VodStream(
        urls=[str(f["url"]) for f in chosen],
        headers=headers,
        info=info,
        duration=float(duration) if duration else None,
        approx_bytes=size or None,
    )


def resolve_vod_stream(
    url: str, settings: Settings, quality: Optional[str] = None, timeout: int = 180
) -> VodStream:
    """One yt-dlp extraction: the direct playlist URL(s) for the VOD."""
    binary = resolve_tool(settings.tool("yt_dlp"), download_stage.YT_DLP_HINT)
    argv = [binary, "--no-playlist", "-J", "-f", download_stage.resolve_format(settings, quality)]
    impersonate = settings.get("download.impersonate", "chrome")
    if impersonate:
        argv += ["--impersonate", str(impersonate)]
    argv.append(url)
    try:
        proc = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise StageError("Asking Kick for the stream URL timed out after {0}s.".format(timeout))
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip()[-1500:]
        raise StageError(
            "yt-dlp couldn't resolve {0}:\n{1}{2}".format(
                url, detail, download_stage._hint_for(url, detail)
            )
        )
    try:
        info = json.loads(proc.stdout)
    except ValueError:
        raise StageError("yt-dlp returned unreadable JSON while resolving {0}.".format(url))
    return vod_stream_from_info(info)


def _is_hls(vod: VodStream) -> bool:
    """HLS carries its AAC audio as ADTS inside MPEG-TS."""
    if any(u.split("?", 1)[0].lower().endswith(".m3u8") for u in vod.urls):
        return True
    formats = vod.info.get("requested_formats") or [vod.info]
    return any(str(f.get("protocol", "")).startswith("m3u8") for f in formats)


def stream_argv(ffmpeg: str, vod: VodStream, container: str = "mp4") -> List[str]:
    """ffmpeg command that stream-copies the VOD to stdout in `container`.

    Progress goes to stderr (`-progress pipe:2`) because stdout carries the media.
    """
    if container not in CONTAINERS:
        raise StageError(
            "Unknown upload.youtube.stream_container '{0}' - choose: {1}".format(
                container, ", ".join(sorted(CONTAINERS))
            )
        )
    argv = [str(ffmpeg), "-hide_banner", "-nostdin", "-loglevel", "error", "-progress", "pipe:2"]
    headers = dict(vod.headers)
    user_agent = headers.pop("User-Agent", None) or headers.pop("user-agent", None)
    for url in vod.urls:
        argv += [
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "10",
            "-rw_timeout", "30000000",
        ]
        if user_agent:
            argv += ["-user_agent", user_agent]
        if headers:
            argv += ["-headers", "".join("{0}: {1}\r\n".format(k, v) for k, v in headers.items())]
        argv += ["-i", url]
    if len(vod.urls) == 2:
        argv += ["-map", "0:v:0", "-map", "1:a:0"]
    else:
        argv += ["-map", "0:v:0", "-map", "0:a:0?"]
    argv += ["-c", "copy"]
    # Stream-copying ADTS AAC (what HLS carries) into MP4/Matroska needs the
    # ADTS header converted to an AudioSpecificConfig. Older ffmpeg inserted this
    # itself; ffmpeg 8 does not and the muxer fails with "Malformed AAC
    # bitstream". Only for HLS input: the filter rejects raw (non-ADTS) AAC, and
    # MPEG-TS output wants the ADTS form left alone.
    if container in ("mp4", "matroska") and _is_hls(vod):
        argv += ["-bsf:a", "aac_adtstoasc"]
    argv += CONTAINERS[container][0] + ["pipe:1"]
    return argv


def _redact(argv: List[str]) -> str:
    """argv for the log file, with URL query strings (signed tokens) removed."""
    return " ".join(re.sub(r"\?.*$", "?...", str(a)) for a in argv)


# --- the ffmpeg relay ------------------------------------------------------

_PROGRESS_LINE = re.compile(r"^[A-Za-z0-9_]+=\S*$")


class FfmpegRelay(object):
    """Runs the remux and exposes its stdout as a file-like source.

    Reading to EOF also verifies ffmpeg: a non-zero exit, or a run that ended well
    short of the VOD's duration, raises *from `read`* - i.e. before the uploader
    has sent the final chunk, so a truncated video is never confirmed on YouTube.
    """

    def __init__(self, argv: List[str], expected_seconds: Optional[float] = None,
                 log_path: Optional[Path] = None):
        self.argv = argv
        self.expected_seconds = expected_seconds
        self.log_path = log_path
        self.media_seconds = 0.0
        self._tail = collections.deque(maxlen=60)
        self._proc = None  # type: Optional[subprocess.Popen]
        self._thread = None  # type: Optional[threading.Thread]

    def __enter__(self) -> "FfmpegRelay":
        self._proc = subprocess.Popen(
            [str(a) for a in self.argv], stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        self._thread = threading.Thread(target=self._drain_stderr, name="clipbot-ffmpeg-stderr")
        self._thread.daemon = True
        self._thread.start()
        return self

    def __exit__(self, *exc_info) -> None:
        proc = self._proc
        if proc is not None:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
            try:
                proc.stdout.close()
            except Exception:
                pass
            proc.wait()
        if self._thread is not None:
            self._thread.join(timeout=2)
        if proc is not None:
            try:
                proc.stderr.close()
            except Exception:
                pass
        if self.log_path is not None:
            try:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                self.log_path.write_text(
                    "\n".join([_redact(self.argv), ""] + list(self._tail)), encoding="utf-8"
                )
            except OSError:
                pass

    def _drain_stderr(self) -> None:
        for raw in iter(self._proc.stderr.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            if _PROGRESS_LINE.match(line):
                if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                    try:
                        self.media_seconds = int(line.split("=", 1)[1]) / 1e6
                    except ValueError:
                        pass
            else:
                self._tail.append(line)

    # file-like source for ChunkReader
    def read(self, n: int) -> bytes:
        data = self._proc.stdout.read(n)
        if data:
            return data
        code = self._proc.wait()
        if self._thread is not None:
            self._thread.join(timeout=2)
        if code != 0:
            raise StageError(
                "ffmpeg stopped (exit {0}) before the VOD finished - nothing was "
                "confirmed on YouTube.\n{1}".format(code, "\n".join(self._tail)[-1500:] or "(no output)")
            )
        if self.expected_seconds:
            slack = max(SHORTFALL_SLACK_SECONDS, SHORTFALL_SLACK_FRACTION * self.expected_seconds)
            if self.media_seconds and self.media_seconds < self.expected_seconds - slack:
                raise StageError(
                    "The stream ended at {0:.0f}s of an expected {1:.0f}s - Kick cut it "
                    "off, so the upload was stopped rather than publish a partial VOD.".format(
                        self.media_seconds, self.expected_seconds
                    )
                )
        return b""


# --- metadata --------------------------------------------------------------


class _Blank(dict):
    def __missing__(self, key):
        return ""


def _render(template: Any, ctx: Dict[str, str], fallback: str) -> str:
    try:
        return str(template).format_map(_Blank(ctx))
    except (ValueError, IndexError, KeyError):
        return fallback


def _stream_date(state: Dict[str, Any]) -> str:
    ts = state.get("stream_started_at")
    if isinstance(ts, (int, float)):
        try:
            return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        except (OverflowError, OSError, ValueError):
            pass
    raw = str(state.get("upload_date") or "")
    if re.fullmatch(r"\d{8}", raw):
        return "{0}-{1}-{2}".format(raw[:4], raw[4:6], raw[6:])
    return ""


def build_video_metadata(
    ws: Workspace, settings: Settings, privacy: str,
    title: Optional[str], description: Optional[str],
) -> Dict[str, Any]:
    state = ws.read_state()
    ctx = {
        "title": str(state.get("title") or ws.slug),
        "channel": str(state.get("uploader") or ""),
        "date": _stream_date(state),
        "slug": ws.slug,
        "url": str(state.get("url") or ""),
    }
    final_title = title or _render(
        settings.get("upload.youtube.title_template", "{title}"), ctx, ctx["title"]
    )
    final_desc = description if description is not None else _render(
        settings.get(
            "upload.youtube.description_template",
            "{title}\n\nOriginally streamed live on {date}.",
        ),
        ctx,
        ctx["title"],
    )
    return ytupload.build_metadata(
        final_title,
        final_desc,
        privacy=privacy,
        category_id=str(settings.get("upload.youtube.category_id", "20")),
        made_for_kids=bool(settings.get("upload.youtube.made_for_kids", False)),
    )


# --- the stage -------------------------------------------------------------


def _spool_path(ws: Workspace) -> Path:
    # A subdirectory, not the workspace root: Workspace.video_path() scans the
    # root for video extensions and must never mistake a spool for the VOD.
    return ws.root / "_upload" / "spool.mp4"


def _mime_for(path: Path) -> str:
    return _MIME_BY_SUFFIX.get(path.suffix.lower(), "application/octet-stream")


def _adopt_metadata(ws: Workspace, info: Dict[str, Any]) -> None:
    """A stream-only workspace has no video.info.json, so record what a download
    would have - but never overwrite a value already there (`duration` belongs to
    the audio probe)."""
    have = ws.read_state()
    fields = {
        k: v
        for k, v in download_stage.metadata_from_info(info).items()
        if v is not None and have.get(k) in (None, "")
    }
    if fields.get("kick_duration") and not have.get("duration"):
        fields["duration"] = fields["kick_duration"]
    if fields:
        ws.update_state(**fields)


def _upload_file(
    ws: Workspace, uploader: ytupload.ResumableUpload, path: Path,
    metadata: Dict[str, Any], progress: Progress,
) -> Dict[str, Any]:
    stat = path.stat()
    size, mtime = stat.st_size, int(stat.st_mtime)
    progress.phase("upload", total=size, unit="bytes")

    for attempt in (1, 2):
        saved = ws.read_state().get("upload_session") or {}
        session, offset = None, 0
        if (
            saved.get("uri")
            and saved.get("size") == size
            and saved.get("mtime") == mtime
            and saved.get("file") == path.name
        ):
            probed = uploader.probe(saved["uri"], size)
            if probed is not None:
                kind, committed, result = probed
                if kind == "complete":
                    log.info("YouTube already has the whole file from an earlier run.")
                    ws.update_state(upload_session=None)
                    return result
                session, offset = saved["uri"], int(committed)
                log.info("Resuming the upload at %s of %s", human_size(offset), human_size(size))
        if session is None:
            log.info("Starting upload of %s (%s)", path.name, human_size(size))
            session = uploader.start_session(metadata, size=size, mime=_mime_for(path))
            ws.update_state(
                upload_session={
                    "uri": session, "size": size, "mtime": mtime,
                    "file": path.name, "started_at": time.time(),
                }
            )
        try:
            result = uploader.upload_file(
                path, session, size, offset=offset, mime=_mime_for(path)
            )
            ws.update_state(upload_session=None)
            return result
        except ytupload.SessionExpired:
            ws.update_state(upload_session=None)
            if attempt == 2:
                raise
            log.warning("The upload session expired - starting a fresh one.")
    raise StageError("Upload failed")  # unreachable; keeps the type checker honest


def _spool_stream(pending: List[bytes], reader: ytupload.ChunkReader, dest: Path,
                  progress: Progress) -> Path:
    """Write the not-yet-sent chunks, then the rest of the stream, to `dest`."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(".mp4.part")
    written = 0
    progress.phase("spool", unit="bytes")
    with part.open("wb") as fh:
        for chunk in pending:
            fh.write(chunk)
            written += len(chunk)
        while True:
            progress.check_cancelled()
            chunk = reader.next_chunk()
            if not chunk:
                break
            fh.write(chunk)
            written += len(chunk)
            progress.update(written)
    part.replace(dest)  # existing only once complete: its presence means "whole"
    log.info("Spooled %s to %s", human_size(written), dest)
    return dest


def _upload_stream(
    ws: Workspace, settings: Settings, uploader: ytupload.ResumableUpload,
    vod: VodStream, metadata: Dict[str, Any], progress: Progress,
) -> Tuple[Dict[str, Any], bool, int]:
    """Relay `vod` to YouTube. Returns (video resource, used_spool, bytes)."""
    ffmpeg = resolve_tool(settings.tool("ffmpeg"), "Set tools.ffmpeg in config/settings.json")
    container = str(settings.get("upload.youtube.stream_container", "mp4"))
    if container not in CONTAINERS:
        raise StageError(
            "Unknown upload.youtube.stream_container '{0}' - choose: {1}".format(
                container, ", ".join(sorted(CONTAINERS))
            )
        )
    argv = stream_argv(ffmpeg, vod, container)
    mime = CONTAINERS[container][1]

    progress.phase("upload", total=vod.approx_bytes, unit="bytes")
    log.info(
        "Relaying %s straight to YouTube (no download to disk)",
        "the VOD" if not vod.duration else "{0:.0f} min of video".format(vod.duration / 60.0),
    )

    spool = None  # type: Optional[Path]
    with FfmpegRelay(argv, vod.duration, ws.logs_dir / "upload.log") as relay:
        reader = ytupload.ChunkReader(relay, uploader.chunk_bytes)
        try:
            try:
                session = uploader.start_session(metadata, size=None, mime=mime)
                result, sent = uploader.upload_stream(reader, session, size_hint=vod.approx_bytes)
                return result, False, sent
            except ytupload.UnknownLengthUnsupported as exc:
                log.warning(
                    "%s Falling back to spooling the VOD to disk, then uploading it "
                    "with a known size.", exc
                )
                spool = _spool_stream(
                    list(getattr(exc, "pending", [])), reader, _spool_path(ws), progress
                )
        finally:
            reader.close()

    result = _upload_file(ws, uploader, spool, metadata, progress)
    return result, True, spool.stat().st_size


def upload_vod(
    ws: Workspace,
    settings: Settings,
    force: bool = False,
    privacy: Optional[str] = None,
    title: Optional[str] = None,
    description: Optional[str] = None,
    mode: Optional[str] = None,
    quality: Optional[str] = None,
    progress: Progress = NULL_PROGRESS,
) -> Dict[str, Any]:
    """Upload the workspace's VOD to YouTube. Returns the recorded result
    (`video_id`, `url`, `privacy`, `mode`, `bytes`, ...).

    Idempotent: a workspace that has already been uploaded returns its record
    without touching YouTube unless `force` is set, because a second upload
    makes a second public-facing video.
    """
    if ws.platform == platforms.YOUTUBE:
        raise StageError(
            "{0} is a YouTube workspace - the video is already on YouTube.".format(ws.slug)
        )
    state = ws.read_state()
    done = (state.get("stages") or {}).get(STAGE)
    if done and not force:
        log.info("Already uploaded: %s (use --force to upload it again)", done.get("url"))
        return dict(done)

    privacy = privacy or str(settings.get("upload.youtube.privacy", "private"))
    if privacy not in ytupload.PRIVACY_CHOICES:
        raise StageError(
            "Unknown privacy '{0}' - choose one of: {1}".format(
                privacy, ", ".join(ytupload.PRIVACY_CHOICES)
            )
        )
    mode = mode or str(settings.get("upload.youtube.mode", "auto"))
    if mode not in MODES:
        raise StageError("Unknown mode '{0}' - choose one of: {1}".format(mode, ", ".join(MODES)))

    # Fail fast on credentials, before resolving or downloading anything.
    token = youtube_auth.TokenSource(settings)
    uploader = ytupload.ResumableUpload(
        token,
        progress=progress,
        chunk_bytes=int(float(settings.get("upload.youtube.chunk_mb", 16)) * 1024 * 1024),
        max_retries=int(settings.get("upload.youtube.max_retries", 8)),
    )

    url = state.get("url")
    video = ws.video_path()
    spool = _spool_path(ws)
    if mode == "auto":
        mode = "file" if (video or spool.exists()) else "stream"

    used = mode
    if mode == "file":
        source = video or (spool if spool.exists() else None)
        if source is None:
            if not url:
                raise StageError("No VOD file and no URL recorded for {0}.".format(ws.slug))
            log.info("No local VOD - downloading it first (mode=file).")
            progress.phase("download")
            source = download_stage.acquire(url, ws, settings, quality=quality, progress=progress)
        metadata = build_video_metadata(ws, settings, privacy, title, description)
        result = _upload_file(ws, uploader, Path(source), metadata, progress)
        sent_bytes = Path(source).stat().st_size  # type: Optional[int]
    else:
        if not url:
            raise StageError("No VOD URL recorded for {0}.".format(ws.slug))
        log.info("Resolving the VOD stream from Kick...")
        vod = resolve_vod_stream(url, settings, quality)
        # Adopt the title/date first, so a stream-only workspace (which has no
        # video.info.json) still builds real YouTube metadata.
        _adopt_metadata(ws, vod.info)
        metadata = build_video_metadata(ws, settings, privacy, title, description)
        result, used_spool, sent_bytes = _upload_stream(
            ws, settings, uploader, vod, metadata, progress
        )
        if used_spool:
            used = "stream+spool"

    video_id = (result or {}).get("id")
    if not video_id:
        raise StageError("YouTube accepted the upload but returned no video id: {0}".format(result))

    record = {
        "video_id": video_id,
        "url": "https://youtu.be/{0}".format(video_id),
        "studio_url": "https://studio.youtube.com/video/{0}/edit".format(video_id),
        "privacy": privacy,
        "mode": used,
        "title": metadata["snippet"]["title"],
        "uploaded_at": time.time(),
    }
    if sent_bytes is not None:
        record["bytes"] = sent_bytes
    ws.mark_stage(STAGE, **record)
    ws.update_state(youtube_video_id=video_id, youtube_url=record["url"], upload_session=None)

    # The spool is a scratch copy of the VOD - remove it once it is safely on YouTube.
    if spool.exists():
        try:
            spool.unlink()
            spool.parent.rmdir()
        except OSError:
            pass

    log.info("Uploaded: %s (%s). YouTube keeps processing it after this.", record["url"], privacy)
    return record
