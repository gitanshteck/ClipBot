"""Per-VOD workspace layout.

Every stage reads and writes through a Workspace, so stages never need to know
how any other stage found its inputs. Layout:

    work/<slug>/
        state.json        pipeline state / metadata (what's done, source URL, ...)
        video.<ext>       downloaded VOD (deleted by stage 6). Kick always has
                          one; a YouTube workspace normally has none - the
                          dashboard plays the embedded YouTube video instead
        source_audio.<ext>  YouTube's audio-only download, deleted once
                          audio.wav has been extracted from it
        audio.wav         extracted audio (kept)
        transcript.json   segment-level transcript (kept)
        candidates.json   Claude's clip candidates (kept)
        clips/            cut clip files (kept)
        manifest.json     per-clip description + rationale (kept)
        manifest.csv      same, spreadsheet-friendly
        logs/             raw tool output
"""

import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from . import platforms
from .utils import StageError, find_largest_file, get_logger, slugify

log = get_logger(__name__)

# One lock per workspace root. State updates are read-modify-write, so two
# stages running concurrently against the same workspace (which the dashboard
# makes possible) could otherwise lose fields.
_ROOT_LOCKS = {}
_ROOT_LOCKS_GUARD = threading.Lock()


def _lock_for(root: Path) -> threading.RLock:
    key = str(Path(root).resolve()).lower()
    with _ROOT_LOCKS_GUARD:
        lock = _ROOT_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _ROOT_LOCKS[key] = lock
        return lock

STATE_FILE = "state.json"
VIDEO_EXTENSIONS = [".mp4", ".mkv", ".ts", ".webm", ".mov", ".flv"]

# YouTube's audio-only download. Matched by exact stem so yt-dlp's scratch
# files (source_audio.webm.part, source_audio.info.json, ...) never count.
SOURCE_AUDIO_STEM = "source_audio"
SOURCE_AUDIO_EXTENSIONS = (".m4a", ".webm", ".opus", ".ogg", ".mp3", ".aac", ".mka", ".mp4", ".wav")

# source_mode() values
MODE_LOCAL = "local"   # a video file is on disk; the page plays that
MODE_EMBED = "embed"   # YouTube workspace with no local video: embedded player
MODE_NONE = "none"     # nothing to play (yet, or deleted by cleanup)


def slug_for_url(url: str) -> str:
    """Stable, filesystem-safe workspace name derived from the VOD URL.

    Kick slugs are unchanged from before YouTube support (they are directory
    names); see clipbot/platforms.py. Never raises: a YouTube-looking URL that
    isn't a video falls through to the generic fallback like any other
    unrecognised URL - `Workspace.for_url` is what rejects those.
    """
    try:
        parsed = platforms.parse_url(url)
    except ValueError:
        parsed = None
    if parsed:
        return platforms.slug_for(parsed)
    return slugify(url.rstrip("/").split("/")[-1] or "vod")


class Workspace:
    def __init__(self, root: Path):
        self.root = Path(root)

    # ---- construction -----------------------------------------------------

    @classmethod
    def for_url(cls, work_root: Path, url: str) -> "Workspace":
        try:
            parsed = platforms.parse_url(url)
        except ValueError as exc:
            # A YouTube channel/playlist link: refuse rather than create a
            # workspace under a meaningless generic slug.
            raise StageError(str(exc))
        ws = cls(Path(work_root) / slug_for_url(url))
        ws.ensure()
        state = ws.read_state()
        state.setdefault("url", parsed.canonical_url if parsed else url)
        state.setdefault("slug", ws.slug)
        state.setdefault("created_at", time.time())
        if parsed:
            state.setdefault("platform", parsed.platform)
            if parsed.platform == platforms.YOUTUBE:
                state.setdefault("video_id", parsed.video_id)
        ws.write_state(state)
        return ws

    @classmethod
    def open(cls, path: Path) -> "Workspace":
        ws = cls(Path(path))
        if not ws.root.is_dir():
            raise FileNotFoundError("No workspace at {0}".format(ws.root))
        return ws

    def ensure(self) -> "Workspace":
        self.root.mkdir(parents=True, exist_ok=True)
        self.clips_dir.mkdir(exist_ok=True)
        self.logs_dir.mkdir(exist_ok=True)
        return self

    # ---- paths ------------------------------------------------------------

    @property
    def slug(self) -> str:
        return self.root.name

    @property
    def clips_dir(self) -> Path:
        return self.root / "clips"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def audio_path(self) -> Path:
        return self.root / "audio.wav"

    @property
    def transcript_path(self) -> Path:
        return self.root / "transcript.json"

    @property
    def candidates_path(self) -> Path:
        return self.root / "candidates.json"

    @property
    def captions_path(self) -> Path:
        """Hinglish (Latin-script) transliteration of transcript.json, kept as
        a sibling file rather than a mutation - transcribe.py stays the single
        writer of transcript.json, same discipline as candidates.json/clips.json."""
        return self.root / "captions.json"

    @property
    def diarization_path(self) -> Path:
        """Raw speaker turns from stages/diarize.py (pyannote), generic
        SPEAKER_00-style cluster labels - naming/merging lives in
        speaker_map.json + the library speakers.json registry, not here."""
        return self.root / "diarization.json"

    @property
    def speaker_map_path(self) -> Path:
        """Per-workspace segment id -> speaker id overlay. See clipbot/speakers.py."""
        return self.root / "speaker_map.json"

    @property
    def manifest_json_path(self) -> Path:
        return self.root / "manifest.json"

    @property
    def manifest_csv_path(self) -> Path:
        return self.root / "manifest.csv"

    @property
    def reels_dir(self) -> Path:
        """Vertical 9:16 exports. A subdirectory so they don't inflate the cut
        count, which globs clips/*.mp4 non-recursively."""
        return self.clips_dir / "reels"

    @property
    def clips_path(self) -> Path:
        """Per-clip review state. Owned by the reviewer, not by the analyzer."""
        return self.root / "clips.json"

    @property
    def compilations_path(self) -> Path:
        """Named multi-range compilations (see clipbot/compilations.py).

        A sibling of clips.json, not a part of it - a compilation's segments
        must never be picked up by a normal `clipbot cut` run as individual
        clip outputs, so they live in their own sidecar rather than the
        approve/reject/cut review queue.
        """
        return self.root / "compilations.json"

    @property
    def compilations_dir(self) -> Path:
        """Rendered compilation videos. A subdirectory of clips_dir so they
        don't inflate the cut count, same treatment reels_dir already gets."""
        return self.clips_dir / "compilations"

    def compile_scratch_dir(self, name: str) -> Path:
        """Scratch directory for one compilation's per-segment cut files.

        Not a deliverable itself - consumed by the concat join and kept
        around (not cleaned up) so a re-render can skip segments whose
        source range hasn't changed, same disposable-but-reusable treatment
        cut.py's own clips/*.mp4 outputs get.
        """
        return self.root / "_compile" / name

    @property
    def chat_path(self) -> Path:
        """Stream chat, harvested from Kick and keyed to VOD offsets.

        Archival: Kick drops a VOD (and its chat) 7 days after an unverified
        channel's stream, and unlike the video this cannot be re-derived from
        anything else on disk. Fetch it early and keep it.
        """
        return self.root / "chat.json"

    def chat_frames_dir(self, clip_id: str) -> Path:
        """Scratch directory for one clip's rendered chat PNGs + ffconcat list.

        Not under `clips_dir`: these are consumed by the ffmpeg run that
        produces a reel and deleted immediately after, never a deliverable in
        their own right.
        """
        return self.root / "chatframes" / clip_id

    def caption_frames_dir(self, clip_id: str) -> Path:
        """Scratch directory for one clip's rendered caption PNGs + ffconcat
        list. Same disposable-scratch treatment as chat_frames_dir."""
        return self.root / "captionframes" / clip_id

    @property
    def waveform_path(self) -> Path:
        return self.root / "waveform.json"

    @property
    def keyframes_path(self) -> Path:
        return self.root / "keyframes.json"

    @property
    def thumbs_dir(self) -> Path:
        return self.root / "thumbs"

    @property
    def analysis_dir(self) -> Path:
        """Archived analysis runs, one per rubric version."""
        return self.root / "analysis"

    @property
    def lock(self) -> threading.RLock:
        return _lock_for(self.root)

    def video_path(self) -> Optional[Path]:
        """Current VOD file, if one is on disk.

        Prefers the path stage 1 recorded; falls back to scanning, so a manually
        placed file still works.
        """
        recorded = self.read_state().get("video_file")
        if recorded:
            candidate = Path(recorded)
            if not candidate.is_absolute():
                candidate = self.root / candidate
            if candidate.exists():
                return candidate
        return find_largest_file(self.root, VIDEO_EXTENSIONS)

    def source_audio_path(self) -> Optional[Path]:
        """YouTube's audio-only download, if it is still on disk."""
        if not self.root.is_dir():
            return None
        found = [
            p
            for p in self.root.iterdir()
            if p.is_file()
            and p.stem == SOURCE_AUDIO_STEM
            and p.suffix.lower() in SOURCE_AUDIO_EXTENSIONS
        ]
        return max(found, key=lambda p: p.stat().st_size) if found else None

    # ---- platform / source ------------------------------------------------

    @property
    def platform(self) -> str:
        """`kick` or `youtube`. Absent in state.json means Kick: every
        workspace that predates YouTube support is one."""
        return platforms.platform_of(self.read_state())

    @property
    def video_id(self) -> Optional[str]:
        """The platform's own video id (YouTube: case-sensitive, 11 chars).
        Only recorded for YouTube; Kick workspaces don't need one."""
        return self.read_state().get("video_id")

    def source_mode(self) -> str:
        """How the picture reaches the reviewer. Derived from what is on disk,
        never stored, so it can't drift from reality (same rule the dashboard's
        stage list follows): a video file -> `local`; otherwise a YouTube
        workspace -> `embed`; otherwise `none`."""
        if self.root.is_dir():
            video = self.video_path()
            if video and video.exists():
                return MODE_LOCAL
        state = self.read_state()
        if platforms.platform_of(state) == platforms.YOUTUBE and state.get("video_id"):
            return MODE_EMBED
        return MODE_NONE

    # ---- state ------------------------------------------------------------

    @property
    def state_path(self) -> Path:
        return self.root / STATE_FILE

    def read_state(self) -> Dict[str, Any]:
        if not self.state_path.exists():
            return {}
        try:
            # utf-8-sig: tolerate a BOM if the file was hand-edited on Windows
            with self.state_path.open("r", encoding="utf-8-sig") as fh:
                return json.load(fh)
        except (ValueError, OSError) as exc:
            log.warning("Could not read %s (%s); starting fresh", self.state_path, exc)
            return {}

    def write_state(self, state: Dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, ensure_ascii=False)
        tmp.replace(self.state_path)

    def update_state(self, **fields: Any) -> Dict[str, Any]:
        with self.lock:
            state = self.read_state()
            state.update(fields)
            state["updated_at"] = time.time()
            self.write_state(state)
            return state

    def mark_stage(self, stage: str, **details: Any) -> None:
        with self.lock:
            state = self.read_state()
            stages = state.setdefault("stages", {})
            entry = {"completed_at": time.time()}
            entry.update(details)
            stages[stage] = entry
            state["updated_at"] = time.time()
            self.write_state(state)

    def stage_done(self, stage: str) -> bool:
        return stage in self.read_state().get("stages", {})

    def write_json(self, path: Path, payload: Any) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
        tmp.replace(path)
        return path

    def read_json(self, path: Path) -> Any:
        with path.open("r", encoding="utf-8-sig") as fh:
            return json.load(fh)

    def __repr__(self) -> str:
        return "Workspace({0})".format(self.root)
