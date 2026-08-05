"""The cross-stream editing library: sounds, stickers, fonts, presets, styles.

Everything here lives in one folder outside any workspace - `work/_library/` by
default, a sibling of the shared `work/_cache/`. That location is the whole
point of the feature: a sound effect belongs to *you*, not to one VOD, so it
must survive `clipbot cleanup` deleting a workspace and must be reachable from
a clip in any other one.

The leading underscore is not decoration. `/api/workspaces` skips directories
whose name starts with one, so the library never shows up as a workspace.

Assets arrive by being dropped into a folder, not uploaded. Scanning turns that
folder into an index, and the index is a **disposable cache** - the files plus
`meta.json` are the truth, and deleting `index.json` then rescanning must
reproduce it exactly.

Asset ids are content-addressed (`<kind>_<sha1 of the bytes>`), which is what
makes a drop-in folder survive contact with a human: renaming `airhorn.mp3` to
`AIRHORN big.mp3`, or moving it into a subfolder, keeps every clip that
references it working. A path-addressed id would break all of them silently.
"""

import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .specerror import SpecError
from .utils import get_logger, resolve_tool

log = get_logger(__name__)

INDEX_VERSION = 1

# kind -> (subdirectory, id prefix)
KINDS = {
    "sfx": ("sfx", "sfx"),
    "music": ("music", "mus"),
    "sticker": ("stickers", "stk"),
    "font": ("fonts", "fnt"),
    "avatar": ("avatars", "avt"),
}

DEFAULT_EXTENSIONS = {
    "sfx": (".wav", ".mp3", ".ogg", ".m4a", ".flac"),
    "music": (".wav", ".mp3", ".ogg", ".m4a", ".flac"),
    "sticker": (".png", ".gif", ".webp", ".apng", ".jpg", ".jpeg"),
    "font": (".ttf", ".otf"),
    # Static only - unlike a sticker, an avatar's "moving" is the ring/glow
    # toggling in reelspec.py, not an animated source image.
    "avatar": (".png", ".jpg", ".jpeg", ".webp"),
}

ANIMATED_EXTENSIONS = (".gif", ".webp", ".apng")

MAX_ASSET_MB = 20
SCAN_MAX_FILES = 5000
TOMBSTONE_DAYS = 30

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,60}$")

# The subdirectory a downloaded file lands in while it is still incomplete.
DOWNLOAD_DIR = ".downloads"


class LibraryError(RuntimeError):
    """Something is wrong with the library folder itself."""


# --------------------------------------------------------------------------
# locating the library
# --------------------------------------------------------------------------


def library_root(settings) -> Path:
    """Where the library lives. `library.dir` overrides, else work/_library."""
    configured = str(settings.get("library.dir", "") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (settings.work_root / "_library").resolve()


def ensure_layout(root: Path) -> Path:
    """Create the folder tree, plus a README explaining the drop-in workflow."""
    root.mkdir(parents=True, exist_ok=True)
    for sub, _prefix in KINDS.values():
        (root / sub).mkdir(exist_ok=True)
    (root / "presets").mkdir(exist_ok=True)
    readme = root / "README.txt"
    if not readme.exists():
        readme.write_text(_README, encoding="utf-8")
    return root


_README = """\
ClipBot editing library
=======================

Drop files into these folders, then hit "Rescan" in the review page (or run
`python -m clipbot library scan`):

  sfx/       one-shot sound effects - airhorn, vine boom, bruh, impacts
  music/     background beds; they loop automatically under a clip
  stickers/  PNG / GIF / WebP overlays with transparency
  fonts/     .ttf / .otf used by hook text

Notes
-----
* Assets are identified by their *contents*, not their path, so renaming or
  moving a file will not break clips that already use it.
* Deleting a file is remembered for 30 days: the review page will tell you an
  effect's sound is missing rather than silently dropping it.
* `meta.json` here is yours to edit - it renames and tags assets without
  touching the files.
* `python -m clipbot library fetch` downloads a small starter set of CC0
  sounds. `python -m clipbot library licenses` prints where everything came
  from.

Nothing in this folder is committed to the repository.
"""


# --------------------------------------------------------------------------
# scanning
# --------------------------------------------------------------------------


def _kind_for(rel: Path) -> Optional[str]:
    top = rel.parts[0] if rel.parts else ""
    for kind, (sub, _prefix) in KINDS.items():
        if top == sub:
            return kind
    return None


def _extensions(settings, kind: str) -> Tuple[str, ...]:
    if settings is None:
        return DEFAULT_EXTENSIONS[kind]
    key = {"sfx": "audio", "music": "audio", "sticker": "image", "font": "font",
           "avatar": "image"}[kind]
    configured = settings.get("library.{0}_extensions".format(key))
    if not configured:
        return DEFAULT_EXTENSIONS[kind]
    return tuple(str(e).lower() for e in configured)


def _sha1(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def asset_id(kind: str, sha: str) -> str:
    return "{0}_{1}".format(KINDS[kind][1], sha[:16])


def scan(settings, prune: bool = False) -> Dict[str, Any]:
    """Walk the library folder and rewrite index.json. Returns the new index.

    Only files whose (size, mtime) changed since the last scan are re-hashed
    and re-probed, so a 200-asset library rescans in well under a second.
    """
    root = ensure_layout(library_root(settings))
    previous = _read_index(root)
    by_rel = {a["rel"]: a for a in previous.get("assets") or []}
    tombstones = {t["id"]: t for t in previous.get("missing") or []}

    max_bytes = int(float(settings.get("library.max_asset_mb", MAX_ASSET_MB)
                          if settings else MAX_ASSET_MB) * 1024 * 1024)
    max_files = int(settings.get("library.scan_max_files", SCAN_MAX_FILES)
                    if settings else SCAN_MAX_FILES)

    assets: List[Dict[str, Any]] = []
    duplicates: Dict[str, List[str]] = {}
    errors: List[Dict[str, str]] = []
    seen_ids: Dict[str, str] = {}
    seen_rels = set()
    count = 0

    for kind in sorted(KINDS):
        sub = KINDS[kind][0]
        exts = _extensions(settings, kind)
        base = root / sub
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            if path.suffix.lower() not in exts:
                continue
            if DOWNLOAD_DIR in path.parts:
                continue
            count += 1
            if count > max_files:
                errors.append({"rel": sub, "reason": "scan stopped at "
                                                     "{0} files".format(max_files)})
                break
            rel = path.relative_to(root).as_posix()
            seen_rels.add(rel)
            try:
                stat = path.stat()
            except OSError as exc:
                errors.append({"rel": rel, "reason": str(exc)})
                continue
            if stat.st_size > max_bytes:
                errors.append({
                    "rel": rel,
                    "reason": "larger than the {0} MB limit".format(
                        round(max_bytes / 1048576.0, 1)),
                })
                continue

            cached = by_rel.get(rel)
            unchanged = (
                cached
                and cached.get("bytes") == stat.st_size
                and cached.get("mtime_ns") == stat.st_mtime_ns
                and cached.get("kind") == kind
            )
            if unchanged:
                record = dict(cached)
            else:
                try:
                    record = _probe(settings, path, kind, rel, stat)
                except Exception as exc:  # a broken file must not stop the scan
                    errors.append({"rel": rel, "reason": str(exc)[:300]})
                    continue

            if record["id"] in seen_ids:
                # Same bytes under two names. One wins; the other is reported
                # so the user can delete it, rather than silently shadowed.
                duplicates.setdefault(record["id"], []).append(rel)
                continue
            seen_ids[record["id"]] = rel
            assets.append(record)
        else:
            continue
        break

    now = time.time()
    missing = []
    keep_days = float(settings.get("library.missing_tombstone_days", TOMBSTONE_DAYS)
                      if settings else TOMBSTONE_DAYS)
    for old in previous.get("assets") or []:
        if old["rel"] in seen_rels or old["id"] in seen_ids:
            continue
        # Content-addressed ids mean a *renamed* file reappears with the same
        # id under a new rel and never reaches here; only a real deletion does.
        tomb = tombstones.get(old["id"]) or {
            "id": old["id"], "rel": old["rel"], "name": old.get("name"),
            "kind": old.get("kind"), "missing_since": now,
        }
        missing.append(tomb)
    for tomb in tombstones.values():
        if tomb["id"] in seen_ids or any(m["id"] == tomb["id"] for m in missing):
            continue
        missing.append(tomb)
    if not prune:
        missing = [m for m in missing
                   if (now - float(m.get("missing_since") or now)) < keep_days * 86400]
    else:
        missing = []

    index = {
        "version": INDEX_VERSION,
        "scanned_at": now,
        "counts": {k: sum(1 for a in assets if a["kind"] == k) for k in sorted(KINDS)},
        "assets": _apply_meta(root, assets),
        "missing": missing,
        "duplicates": duplicates,
        "errors": errors,
    }
    _write_json(root / "index.json", index)
    return index


def _probe(settings, path: Path, kind: str, rel: str, stat) -> Dict[str, Any]:
    sha = _sha1(path)
    record = {
        "id": asset_id(kind, sha),
        "kind": kind,
        "rel": rel,
        "name": path.stem,
        "bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha1": sha,
        "duration": None,
        "sample_rate": None,
        "channels": None,
        "width": None,
        "height": None,
        "animated": False,
        "tags": [],
        "license": None,
        "added_at": time.time(),
    }
    if kind in ("sfx", "music"):
        record.update(_probe_audio(settings, path))
    elif kind == "sticker":
        record.update(_probe_image(path))

    sidecar = path.with_suffix(path.suffix + ".json")
    if sidecar.is_file():
        # Written by `library fetch`, and the reason provenance survives a
        # rescan without anyone having to retype it.
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        for key in ("name", "tags", "license"):
            if data.get(key):
                record[key] = data[key]
    return record


def _probe_audio(settings, path: Path) -> Dict[str, Any]:
    binary = resolve_tool(
        settings.tool("ffprobe") if settings else "ffprobe",
        "ffprobe ships with ffmpeg; set tools.ffprobe in config/settings.json.",
    )
    argv = [binary, "-v", "error", "-select_streams", "a:0",
            "-show_entries", "stream=sample_rate,channels",
            "-show_entries", "format=duration", "-of", "json", str(path)]
    proc = subprocess.run(argv, capture_output=True, text=True)
    if proc.returncode != 0:
        raise LibraryError("ffprobe exit {0}: {1}".format(
            proc.returncode, (proc.stderr or "").strip()[:200]))
    data = json.loads(proc.stdout or "{}")
    streams = data.get("streams") or [{}]
    duration = (data.get("format") or {}).get("duration")
    if duration in (None, "N/A"):
        raise LibraryError("no readable audio duration")
    return {
        "duration": round(float(duration), 3),
        "sample_rate": int(streams[0].get("sample_rate") or 0) or None,
        "channels": int(streams[0].get("channels") or 0) or None,
    }


def _probe_image(path: Path) -> Dict[str, Any]:
    try:
        from PIL import Image  # noqa: WPS433 - optional at import time
    except ImportError:
        raise LibraryError(
            "Pillow is needed to read sticker dimensions. Install it with: "
            "pip install Pillow"
        )
    with Image.open(path) as img:
        width, height = img.size
        animated = bool(getattr(img, "n_frames", 1) > 1)
        frames = int(getattr(img, "n_frames", 1))
    if path.suffix.lower() in ANIMATED_EXTENSIONS and frames > 1:
        animated = True
    return {"width": int(width), "height": int(height), "animated": animated,
            "frames": frames}


def _apply_meta(root: Path, assets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Overlay the user's hand-editable renames and tags."""
    meta = _read_json(root / "meta.json") or {}
    overrides = meta.get("assets") or {}
    for asset in assets:
        override = overrides.get(asset["id"])
        if not override:
            continue
        if override.get("name"):
            asset["name"] = str(override["name"])[:120]
        if override.get("tags") is not None:
            asset["tags"] = _tags(override["tags"])
        if override.get("license"):
            asset["license"] = override["license"]
    return assets


def _tags(value) -> List[str]:
    if isinstance(value, str):
        value = [v for v in re.split(r"[,\s]+", value) if v]
    out = []
    for tag in list(value or [])[:20]:
        tag = re.sub(r"[^a-z0-9_-]", "", str(tag).strip().lower())[:24]
        if tag and tag not in out:
            out.append(tag)
    return out


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------


def load_index(settings, scan_if_missing: bool = True) -> Dict[str, Any]:
    root = library_root(settings)
    index = _read_index(root)
    if not index.get("assets") and scan_if_missing:
        return scan(settings)
    return index


def asset_map(settings, index: Optional[Dict[str, Any]] = None) -> Dict[str, Dict]:
    """id -> asset record with an absolute `path`, ready for fxspec.resolve_fx.

    fxspec is pure and never touches disk, so this is where an id becomes a
    file. Records whose file has since vanished are left out entirely, which is
    what makes the renderer's `strict=True` report them by name.
    """
    root = library_root(settings)
    index = index if index is not None else load_index(settings)
    out = {}
    for asset in index.get("assets") or []:
        path = root / asset["rel"]
        if not path.is_file():
            continue
        record = dict(asset)
        record["path"] = str(path)
        out[asset["id"]] = record
    return out


def fallback_font(settings) -> Optional[str]:
    """A real font path for drawtext when no library font is chosen.

    Without one, ffmpeg asks fontconfig - which the Windows builds ship
    without - and the render succeeds having drawn nothing.
    """
    configured = str(settings.get("reel.fx.text.fallback_font", "") or "").strip()
    candidates = [configured] if configured else []
    candidates += [
        r"C:\Windows\Fonts\segoeuib.ttf",
        r"C:\Windows\Fonts\arialbd.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate))
    return None


def update_asset_meta(settings, asset_id_value: str, patch: Dict[str, Any]) -> Dict:
    """Rename / retag an asset. Writes meta.json, never touches the file."""
    root = ensure_layout(library_root(settings))
    index = load_index(settings)
    known = {a["id"]: a for a in index.get("assets") or []}
    if asset_id_value not in known:
        raise KeyError(asset_id_value)

    meta = _read_json(root / "meta.json") or {"version": 1, "assets": {}}
    meta.setdefault("assets", {})
    entry = dict(meta["assets"].get(asset_id_value) or {})
    if "name" in patch:
        name = str(patch["name"] or "").strip()[:120]
        if not name:
            raise SpecError("name must not be empty")
        entry["name"] = name
    if "tags" in patch:
        entry["tags"] = _tags(patch["tags"])
    meta["assets"][asset_id_value] = entry
    _write_json(root / "meta.json", meta)

    asset = dict(known[asset_id_value])
    asset.update(entry)
    return asset


def resolve_media(settings, kind: str, name: str) -> Path:
    """Locate an asset file for serving, refusing anything outside the library."""
    if kind not in KINDS:
        raise KeyError(kind)
    root = library_root(settings).resolve()
    base = (root / KINDS[kind][0]).resolve()
    if any(token in name for token in ("..", "\\")) or name.startswith("/"):
        raise KeyError(name)
    path = (base / name).resolve()
    if base != path.parent and base not in path.parents:
        raise KeyError(name)
    if not path.is_file():
        raise KeyError(name)
    return path


# --------------------------------------------------------------------------
# presets and text styles
# --------------------------------------------------------------------------


PRESET_ANCHORS = ("at", "clip_start", "clip_end")


def slugify_name(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", str(name or "").strip().lower()).strip("_")
    slug = slug[:60] or "preset"
    if not _SLUG_RE.match(slug):
        slug = "preset"
    return slug


def list_presets(settings) -> List[Dict[str, Any]]:
    root = library_root(settings)
    folder = root / "presets"
    if not folder.is_dir():
        return []
    out = []
    for path in sorted(folder.glob("*.json")):
        data = _read_json(path)
        if data and data.get("effects") is not None:
            out.append(data)
    return out


def save_preset(settings, name: str, effects: Any, anchor: str = "at",
                overwrite: bool = False) -> Dict[str, Any]:
    """Store a reusable effect chain.

    Effects are stored with an `offset` from the anchor, never an absolute
    `at`. That is the whole difference between a preset and a placed effect: a
    preset is portable across clips and across streams by construction.
    """
    from . import fxspec

    if anchor not in PRESET_ANCHORS:
        raise SpecError("anchor must be one of {0}".format(", ".join(PRESET_ANCHORS)))
    if not isinstance(effects, list) or not effects:
        raise SpecError("a preset needs at least one effect")

    cleaned = []
    requires = []
    for index, raw in enumerate(effects):
        if not isinstance(raw, dict):
            raise SpecError("preset effect {0} must be an object".format(index))
        entry = dict(raw)
        offset = entry.pop("offset", 0.0)
        entry.pop("at", None)
        # Validated by round-tripping through the real effect validator with a
        # placeholder time, so a preset can never store something a clip would
        # later reject.
        probe = dict(entry)
        probe["id"] = "p{0}".format(index)
        if probe.get("type") in fxspec.TIMED_TYPES:
            probe["at"] = 1000.0
        validated = fxspec.normalize_fx([probe])[0]
        validated.pop("at", None)
        validated.pop("id", None)
        try:
            validated["offset"] = round(float(offset), 3)
        except (TypeError, ValueError):
            raise SpecError("preset effect {0}.offset must be a number".format(index))
        if validated.get("asset"):
            requires.append(validated["asset"])
        cleaned.append(validated)

    root = ensure_layout(library_root(settings))
    slug = slugify_name(name)
    path = root / "presets" / "{0}.json".format(slug)
    if path.exists() and not overwrite:
        raise SpecError("a preset named {0!r} already exists".format(slug))

    now = time.time()
    existing = _read_json(path) or {}
    doc = {
        "version": 1,
        "id": slug,
        "name": str(name or slug)[:80],
        "anchor": anchor,
        "requires": sorted(set(requires)),
        "effects": cleaned,
        "created_at": existing.get("created_at", now),
        "updated_at": now,
    }
    _write_json(path, doc)
    return doc


def delete_preset(settings, preset_id: str) -> None:
    slug = slugify_name(preset_id)
    path = library_root(settings) / "presets" / "{0}.json".format(slug)
    if not path.is_file():
        raise KeyError(preset_id)
    path.unlink()


def load_text_styles(settings) -> Dict[str, Dict[str, Any]]:
    data = _read_json(library_root(settings) / "textstyles.json") or {}
    return data.get("styles") or {}


def save_text_style(settings, style_id: str, style: Any) -> Dict[str, Any]:
    from . import fxspec

    slug = slugify_name(style_id)
    validated = fxspec.normalize_text_style(style, "style")
    root = ensure_layout(library_root(settings))
    path = root / "textstyles.json"
    doc = _read_json(path) or {"version": 1, "styles": {}}
    doc.setdefault("styles", {})[slug] = validated
    _write_json(path, doc)
    return {slug: validated}


# --------------------------------------------------------------------------
# speakers (clipbot/stages/diarize.py, clipbot/speakers.py)
# --------------------------------------------------------------------------

_AVATAR_ASSET_RE = re.compile(r"^avt_[0-9a-f]{8,32}$")
_HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")

DEFAULT_SPEAKER_COLOR = "#19A2D2"


def list_speakers(settings) -> List[Dict[str, Any]]:
    """Speaker profiles are library-level, not per-workspace - the same
    cross-stream reasoning as sfx/stickers: a recurring co-host should look
    the same in every VOD, not be re-tagged from a blank slate each time."""
    data = _read_json(library_root(settings) / "speakers.json") or {}
    return data.get("speakers") or []


def save_speaker(
    settings,
    name: str,
    avatar_asset: Optional[str] = None,
    color: Optional[str] = None,
    style: Any = None,
    speaker_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a speaker profile, or update one in place if `speaker_id` names
    an existing one (the "name this diarization cluster" / "merge into an
    existing speaker" action). A new profile's id is `slugify_name(name)`,
    same scheme `save_preset` already uses - human-readable, not
    content-addressed, since a speaker profile isn't immutable file content
    the way a sound or sticker is.
    """
    from . import fxspec

    name = str(name or "").strip()[:80]
    if not name:
        raise SpecError("a speaker needs a name")

    if avatar_asset is not None and not _AVATAR_ASSET_RE.match(str(avatar_asset)):
        raise SpecError("avatar_asset must be a library avatar asset id")
    if color is not None and not _HEX_COLOR_RE.match(str(color)):
        raise SpecError("color must be a #RRGGBB hex string")
    if style is not None:
        style = fxspec.normalize_text_style(style, "speaker.style")

    root = ensure_layout(library_root(settings))
    path = root / "speakers.json"
    doc = _read_json(path) or {"version": 1, "speakers": []}
    speakers = doc.setdefault("speakers", [])
    now = time.time()

    if speaker_id:
        entry = next((s for s in speakers if s["id"] == speaker_id), None)
        if entry is None:
            raise KeyError(speaker_id)
        entry["name"] = name
        entry["avatar_asset"] = avatar_asset
        entry["color"] = color or entry.get("color") or DEFAULT_SPEAKER_COLOR
        entry["style"] = style
        entry["updated_at"] = now
    else:
        sid = slugify_name(name)
        if any(s["id"] == sid for s in speakers):
            raise SpecError("a speaker named {0!r} already exists".format(sid))
        entry = {
            "id": sid,
            "name": name,
            "avatar_asset": avatar_asset,
            "color": color or DEFAULT_SPEAKER_COLOR,
            "style": style,
            "created_at": now,
            "updated_at": now,
        }
        speakers.append(entry)

    _write_json(path, doc)
    return entry


def delete_speaker(settings, speaker_id: str) -> None:
    root = library_root(settings)
    path = root / "speakers.json"
    doc = _read_json(path) or {"version": 1, "speakers": []}
    speakers = doc.get("speakers") or []
    remaining = [s for s in speakers if s["id"] != speaker_id]
    if len(remaining) == len(speakers):
        raise KeyError(speaker_id)
    doc["speakers"] = remaining
    _write_json(path, doc)


# --------------------------------------------------------------------------
# starter pack
# --------------------------------------------------------------------------


DEFAULT_ALLOW_HOSTS = (
    "cdn.pixabay.com", "pixabay.com",
    "cdn.freesound.org", "freesound.org",
    "raw.githubusercontent.com", "github.com",
)


def _allowed_host(url: str, allow_hosts) -> str:
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise LibraryError("{0} is not https".format(url))
    host = (parsed.hostname or "").lower()
    if host not in allow_hosts:
        raise LibraryError(
            "{0} is not in library.fetch.allow_hosts - the manifest is data, "
            "so it must not be able to point ClipBot at an arbitrary "
            "host".format(host or url)
        )
    return host


def fetch_starter(settings, manifest_path=None, only=None, force=False) -> Dict:
    """Download the CC0 starter assets named in the manifest.

    The manifest ships in the repo; the media does not. Every entry declares a
    sha256, and a mismatch fails that one asset rather than the run - a swapped
    CDN file becomes a clean error instead of a silent substitution.
    """
    root = ensure_layout(library_root(settings))
    path = Path(manifest_path or settings.get(
        "library.fetch.manifest", "config/library_starter.json"))
    if not path.is_file():
        raise LibraryError("no manifest at {0}".format(path))
    manifest = json.loads(path.read_text(encoding="utf-8"))
    allow_hosts = set(
        settings.get("library.fetch.allow_hosts", list(DEFAULT_ALLOW_HOSTS))
        or DEFAULT_ALLOW_HOSTS
    )
    timeout = float(settings.get("library.fetch.timeout_seconds", 30))
    max_bytes = int(float(settings.get("library.max_asset_mb", MAX_ASSET_MB))
                    * 1024 * 1024)

    wanted = set(only or [])
    results = {"ok": [], "skipped": [], "failed": []}
    staging = root / DOWNLOAD_DIR
    staging.mkdir(exist_ok=True)

    for entry in manifest.get("assets") or []:
        slug = str(entry.get("slug") or "")
        if wanted and slug not in wanted:
            continue
        try:
            kind = entry["kind"]
            if kind not in KINDS:
                raise LibraryError("unknown kind {0!r}".format(kind))
            filename = _safe_filename(entry.get("filename"), kind, settings)
            target = root / KINDS[kind][0] / filename
            if target.is_file() and not force:
                if _sha256(target) == entry.get("sha256"):
                    results["skipped"].append(slug)
                    continue
            _allowed_host(entry["url"], allow_hosts)
            tmp = staging / (slug + ".part")
            digest = _download(entry["url"], tmp, allow_hosts, timeout,
                               min(max_bytes, int(entry.get("bytes") or max_bytes) * 2))
            if entry.get("sha256") and digest != entry["sha256"]:
                tmp.unlink(missing_ok=True)
                raise LibraryError(
                    "sha256 mismatch (expected {0}, got {1}) - the file at that "
                    "URL is not the one this manifest was written "
                    "against".format(entry["sha256"][:12], digest[:12])
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(str(tmp), str(target))
            _write_json(target.with_suffix(target.suffix + ".json"), {
                "name": entry.get("name") or slug,
                "tags": _tags(entry.get("tags")),
                "license": entry.get("license"),
            })
            results["ok"].append(slug)
        except Exception as exc:  # one bad entry must not sink the run
            log.warning("  %s failed: %s", slug or "?", exc)
            results["failed"].append({"slug": slug, "error": str(exc)[:300]})

    try:
        staging.rmdir()
    except OSError:
        pass
    results["index"] = scan(settings)
    return results


def _safe_filename(name, kind, settings) -> str:
    text = str(name or "")
    if not text or "/" in text or "\\" in text or ".." in text:
        raise LibraryError("unsafe filename {0!r}".format(name))
    if Path(text).suffix.lower() not in _extensions(settings, kind):
        raise LibraryError(
            "{0} is not an allowed extension for {1}".format(Path(text).suffix, kind)
        )
    return text


def _download(url, dest, allow_hosts, timeout, max_bytes) -> str:
    """Stream to `dest`, hashing as we go and aborting if it runs oversize."""
    digest = hashlib.sha256()
    total = 0
    dest.parent.mkdir(parents=True, exist_ok=True)

    stream = _open_stream(url, allow_hosts, timeout)
    with dest.open("wb") as fh:
        for chunk in stream:
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                fh.close()
                dest.unlink(missing_ok=True)
                raise LibraryError("download exceeded {0} bytes".format(max_bytes))
            digest.update(chunk)
            fh.write(chunk)
    if total == 0:
        dest.unlink(missing_ok=True)
        raise LibraryError("empty response")
    return digest.hexdigest()


def _open_stream(url, allow_hosts, timeout):
    """Yield response chunks. curl_cffi if present (it already is, via yt-dlp)."""
    try:
        from curl_cffi import requests as cffi_requests
    except ImportError:
        cffi_requests = None

    if cffi_requests is not None:
        response = cffi_requests.get(url, timeout=timeout, stream=True,
                                     allow_redirects=True)
        final = getattr(response, "url", url)
        _allowed_host(str(final), allow_hosts)
        if response.status_code != 200:
            raise LibraryError("HTTP {0}".format(response.status_code))
        return response.iter_content(chunk_size=65536)

    import urllib.request

    request = urllib.request.Request(url, headers={"User-Agent": "ClipBot"})
    response = urllib.request.urlopen(request, timeout=timeout)
    _allowed_host(response.geturl(), allow_hosts)
    return iter(lambda: response.read(65536), b"")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------------
# json helpers (atomic writes, matching workspace.py)
# --------------------------------------------------------------------------


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _read_index(root: Path) -> Dict[str, Any]:
    data = _read_json(root / "index.json")
    if not isinstance(data, dict) or data.get("version") != INDEX_VERSION:
        return {}
    return data


def _write_json(path: Path, payload) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
    tmp.replace(path)
    return path
