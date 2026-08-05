"""Per-VOD workspace layout.

Every stage reads and writes through a Workspace, so stages never need to know
how any other stage found its inputs. Layout:

    work/<slug>/
        state.json        pipeline state / metadata (what's done, source URL, ...)
        video.<ext>       downloaded VOD (deleted by stage 6)
        audio.wav         extracted audio (kept)
        transcript.json   segment-level transcript (kept)
        candidates.json   Claude's clip candidates (kept)
        clips/            cut clip files (kept)
        manifest.json     per-clip description + rationale (kept)
        manifest.csv      same, spreadsheet-friendly
        logs/             raw tool output
"""

import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .utils import find_largest_file, get_logger, slugify

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

# https://kick.com/<channel>/videos/<uuid>  |  https://kick.com/video/<uuid>
_KICK_VIDEO_RE = re.compile(
    r"kick\.com/(?:video/|(?P<channel>[^/]+)/videos?/)(?P<video_id>[0-9a-zA-Z-]+)",
    re.IGNORECASE,
)


def slug_for_url(url: str) -> str:
    """Stable, filesystem-safe workspace name derived from the VOD URL."""
    match = _KICK_VIDEO_RE.search(url)
    if match:
        channel = match.group("channel") or "kick"
        video_id = match.group("video_id")
        return slugify("{0}-{1}".format(channel, video_id))
    return slugify(url.rstrip("/").split("/")[-1] or "vod")


class Workspace:
    def __init__(self, root: Path):
        self.root = Path(root)

    # ---- construction -----------------------------------------------------

    @classmethod
    def for_url(cls, work_root: Path, url: str) -> "Workspace":
        ws = cls(Path(work_root) / slug_for_url(url))
        ws.ensure()
        state = ws.read_state()
        state.setdefault("url", url)
        state.setdefault("slug", ws.slug)
        state.setdefault("created_at", time.time())
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
