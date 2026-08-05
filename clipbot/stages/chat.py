"""Stage 1b: harvest the stream's chat and key it to VOD offsets.

Runs straight after download, and deliberately early: Kick keeps a VOD for 7 days
on an unverified channel (30 if verified) and the chat goes with it. The video can
be re-downloaded from a URL; the chat cannot be re-derived from anything, so it is
the one artifact in the workspace that is genuinely unrecoverable once lost.

How the API actually works, as measured - not as documented anywhere:

* `GET /api/v2/channels/<channel_id>/messages` returns the 25 most recent
  messages, newest first, plus a `cursor`.
* `?start_time=<ISO>` is **not** pagination. It returns only messages at that
  exact second - usually zero - so walking it backwards silently yields nothing.
  The published Python wrappers for this API all use it, and all of them are
  wrong about what it does.
* `?cursor=<n>` *is* pagination, and the cursor is a **microsecond unix epoch**.
  That is the useful part: it is seekable. Rather than paging back through every
  message since the stream ended, we seek straight to the stream's end and walk
  back to its start.
* The channel id is the one in `channels/<slug>.id`, which is also yt-dlp's
  `channel_id`. The `user_id` and `chatroom.id` both return 200 with zero
  messages, which is an easy and silent way to conclude a stream had no chat.

Volume is low enough that this is a fast, simple stage: a 4.6-hour stream on this
channel was 358 messages across 15 requests in 16 seconds. There is deliberately
no incremental-write/resume machinery - it would be more moving parts than the
work it protects.
"""

import calendar
import datetime
import json
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from ..config import Settings
from ..progress import NULL_PROGRESS, JobCancelled, Progress
from ..utils import StageError, get_logger
from ..workspace import Workspace

log = get_logger(__name__)

STAGE = "chat"

SCHEMA_VERSION = 1

API_BASE = "https://kick.com/api/v2/channels/{0}/messages"

# Kick renders these inline in the message body.
_EMOTE_RE = re.compile(r"\[emote:(\d+):([^\]]*)\]")

CURL_CFFI_HINT = (
    'Install with: pip install -U "yt-dlp[default,curl-cffi]" - Kick is behind '
    "Cloudflare and refuses requests without a browser TLS fingerprint."
)


# --------------------------------------------------------------------------
# time
# --------------------------------------------------------------------------


def _parse_created_at(text: str) -> Optional[float]:
    """Kick's `created_at` is UTC, second precision: 2026-08-03T14:57:17Z.

    `datetime.fromisoformat` doesn't accept the trailing Z until Python 3.11 and
    this package still targets 3.9, so parse it explicitly.
    """
    if not text:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%d %H:%M:%S"):
        try:
            return float(calendar.timegm(time.strptime(text, fmt)))
        except ValueError:
            continue
    return None


def _refined_epoch(message: Dict[str, Any], created: float) -> float:
    """Recover sub-second timing where it's safe to.

    `created_at` is only second-accurate, so a busy moment collapses a dozen
    messages onto one instant and they all pop in together. `metadata.message_ref`
    is a millisecond epoch that fixes that - but it is set by the *sender's*
    browser, so a skewed client clock could throw a message minutes out. Trust it
    only when it corroborates the server's own timestamp.
    """
    raw = message.get("metadata")
    if not raw:
        return created
    try:
        ref = json.loads(raw).get("message_ref")
        ms = float(ref) / 1000.0
    except (ValueError, TypeError, AttributeError):
        return created
    return ms if abs(ms - created) <= 2.0 else created


def _iso(epoch: float) -> str:
    return datetime.datetime.utcfromtimestamp(epoch).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# anchor
# --------------------------------------------------------------------------


def stream_anchor(ws: Workspace) -> Tuple[int, float]:
    """(channel_id, stream_started_at) for this workspace.

    Reads state first, then falls back to yt-dlp's info.json so workspaces
    downloaded before the anchor was recorded still work without re-downloading.
    """
    state = ws.read_state()
    channel_id = state.get("kick_channel_id")
    started = state.get("stream_started_at")

    if not channel_id or not started:
        info_path = ws.root / "video.info.json"
        if info_path.exists():
            try:
                with info_path.open("r", encoding="utf-8") as fh:
                    info = json.load(fh)
                channel_id = channel_id or info.get("channel_id")
                started = started or info.get("timestamp")
            except (ValueError, OSError) as exc:
                log.debug("Could not read %s: %s", info_path, exc)

    if not channel_id:
        raise StageError(
            "No Kick channel id for this workspace. Re-run the download stage - "
            "it records one from yt-dlp's metadata (video.info.json 'channel_id')."
        )
    if not started:
        raise StageError(
            "No stream start time for this workspace, so chat messages cannot be "
            "mapped onto VOD offsets. Re-run the download stage."
        )
    return int(channel_id), float(started)


# --------------------------------------------------------------------------
# fetch
# --------------------------------------------------------------------------


def _session():
    try:
        from curl_cffi import requests
    except ImportError:
        raise StageError("curl_cffi is not installed.\n" + CURL_CFFI_HINT)
    return requests


def _fetch_page(requests, channel_id: int, cursor, impersonate: str, timeout: int):
    """One page. Returns (messages, next_cursor). Raises StageError on a hard failure."""
    url = API_BASE.format(channel_id)
    if cursor is not None:
        url = "{0}?cursor={1}".format(url, int(cursor))
    response = requests.get(url, impersonate=impersonate, timeout=timeout)
    if response.status_code != 200:
        raise _HttpError(response.status_code, (response.text or "")[:200])
    try:
        data = (response.json() or {}).get("data") or {}
    except ValueError:
        raise StageError("Kick returned a non-JSON chat response for {0}".format(url))
    return data.get("messages") or [], data.get("cursor")


class _HttpError(Exception):
    def __init__(self, status, body):
        Exception.__init__(self, "HTTP {0}: {1}".format(status, body))
        self.status = status


def _fetch_with_retry(requests, channel_id, cursor, settings, attempts=4):
    impersonate = str(settings.get("download.impersonate", "chrome") or "chrome")
    timeout = int(settings.get("chat.timeout_seconds", 25))
    delay = 1.0
    last = None
    for attempt in range(attempts):
        try:
            return _fetch_page(requests, channel_id, cursor, impersonate, timeout)
        except _HttpError as exc:
            last = exc
            # 4xx other than rate limiting won't fix themselves.
            if exc.status not in (408, 429) and exc.status < 500:
                raise StageError("Kick chat API refused the request ({0})".format(exc))
        except Exception as exc:  # network blips, TLS resets
            last = exc
        if attempt < attempts - 1:
            time.sleep(delay)
            delay *= 2
    raise StageError("Kick chat API failed after {0} attempts: {1}".format(attempts, last))


# --------------------------------------------------------------------------
# shaping
# --------------------------------------------------------------------------


def parse_parts(content: str) -> List[Dict[str, Any]]:
    """Split a message body into text and emote runs.

    Done here rather than in the renderer so the renderer never re-parses, and so
    `chat.json` stays readable on its own.
    """
    parts: List[Dict[str, Any]] = []
    position = 0
    for match in _EMOTE_RE.finditer(content or ""):
        if match.start() > position:
            parts.append({"t": "text", "v": content[position:match.start()]})
        parts.append({"t": "emote", "id": match.group(1), "name": match.group(2)})
        position = match.end()
    tail = (content or "")[position:]
    if tail:
        parts.append({"t": "text", "v": tail})
    return parts


def _badges(identity: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Flatten both badge shapes into one list.

    `badges` carries the role badges (broadcaster/moderator/subscriber) as type +
    text with no artwork; `badges_v2` carries an `image_url` for things like the
    level badge. Keeping the url means the renderer never has to ship artwork for
    badges it has never seen.
    """
    out = []
    for badge in identity.get("badges") or []:
        if not isinstance(badge, dict):
            continue
        out.append(
            {
                "type": badge.get("type"),
                "text": badge.get("text"),
                "count": badge.get("count"),
                "image": None,
            }
        )
    for badge in identity.get("badges_v2") or []:
        if not isinstance(badge, dict):
            continue
        out.append(
            {
                "type": badge.get("name"),
                "text": badge.get("name"),
                "count": (badge.get("metadata") or {}).get("level"),
                "image": badge.get("image_url"),
            }
        )
    return out


def _keep(message: Dict[str, Any], bots, drop_commands: bool, types) -> bool:
    if message.get("type") not in types:
        return False
    sender = message.get("sender") or {}
    # Match on slug: it's the stable identifier, while `username` is a display
    # name the user can change.
    if (sender.get("slug") or "").lower() in bots:
        return False
    content = (message.get("content") or "").strip()
    if not content:
        return False
    if drop_commands and content.startswith("!"):
        return False
    return True


def _shape(message: Dict[str, Any], started_at: float) -> Dict[str, Any]:
    created = _parse_created_at(message.get("created_at"))
    epoch = _refined_epoch(message, created)
    sender = message.get("sender") or {}
    identity = sender.get("identity") or {}
    return {
        "id": message.get("id"),
        "offset": round(epoch - started_at, 3),
        "at": message.get("created_at"),
        "user": {
            "name": sender.get("username") or sender.get("slug") or "?",
            "slug": sender.get("slug"),
            "color": identity.get("color"),
            "badges": _badges(identity),
        },
        "text": message.get("content") or "",
        "parts": parse_parts(message.get("content") or ""),
    }


# --------------------------------------------------------------------------
# stage
# --------------------------------------------------------------------------


def fetch_chat(
    ws: Workspace,
    settings: Settings,
    force: bool = False,
    progress: Progress = NULL_PROGRESS,
) -> Any:
    """Harvest chat for this workspace's VOD. Returns the chat.json path."""
    if ws.chat_path.exists() and not force:
        try:
            existing = ws.read_json(ws.chat_path)
        except (ValueError, OSError):
            existing = {}
        if existing.get("status") == "ok":
            log.info(
                "Chat already harvested (%s messages), skipping. Use --force to refetch.",
                existing.get("count"),
            )
            return ws.chat_path

    channel_id, started_at = stream_anchor(ws)
    state = ws.read_state()
    # Prefer the probed duration; Kick's own figure is the fallback and runs a
    # little long, which only costs us a few extra messages past the end.
    duration = float(state.get("duration") or state.get("kick_duration") or 0.0)
    if duration <= 0:
        raise StageError(
            "Unknown VOD duration, so there is no window to harvest. Run the "
            "audio stage (it probes and records one) or re-run download."
        )

    # Margin either side: the anchor is when Kick opened the livestream record,
    # which need not be when the recording starts, and chat_offset_seconds can
    # later shift the whole track. Harvesting wide keeps that adjustable without
    # a refetch.
    margin = float(settings.get("chat.margin_seconds", 300))
    window_start = started_at - margin
    window_end = started_at + duration + margin

    bots = set(str(b).lower() for b in (settings.get("chat.bots", []) or []))
    drop_commands = bool(settings.get("chat.drop_commands", True))
    types = tuple(settings.get("chat.keep_types", ["message", "reply"]) or ["message", "reply"])
    delay = float(settings.get("chat.request_delay_ms", 300)) / 1000.0
    max_pages = int(settings.get("chat.max_pages", 4000))
    empty_stride = float(settings.get("chat.empty_stride_seconds", 120))

    requests = _session()

    log.info(
        "Harvesting chat for channel %s, %s .. %s (%.1f min)",
        channel_id,
        _iso(window_start),
        _iso(window_end),
        duration / 60.0,
    )
    progress.phase("chat", total=window_end - window_start, unit="s")

    seen: Dict[str, Dict[str, Any]] = {}
    dropped = 0
    pages = 0
    cursor = int(window_end * 1_000_000)
    reached = window_end

    while pages < max_pages:
        progress.check_cancelled()
        messages, next_cursor = _fetch_with_retry(requests, channel_id, cursor, settings)
        pages += 1

        if not messages:
            # A quiet stretch is not the end of the stream. Step back a fixed
            # stride and carry on; only the window bound below stops the walk.
            cursor = int(cursor - empty_stride * 1_000_000)
            if cursor / 1_000_000.0 < window_start:
                break
            log.debug("  empty page, stepping back to %s", _iso(cursor / 1e6))
            time.sleep(delay)
            continue

        oldest = window_end
        for message in messages:
            created = _parse_created_at(message.get("created_at"))
            if created is None:
                continue
            oldest = min(oldest, created)
            if created < window_start or created > window_end:
                continue
            if not _keep(message, bots, drop_commands, types):
                dropped += 1
                continue
            shaped = _shape(message, started_at)
            if shaped["id"]:
                seen[shaped["id"]] = shaped

        reached = min(reached, oldest)
        progress.update(
            max(0.0, window_end - reached),
            label="{0} messages".format(len(seen)),
        )

        if oldest < window_start:
            break

        # The cursor must strictly retreat or the walk spins forever on a page
        # whose messages all share one timestamp.
        candidate = int(next_cursor) if next_cursor else 0
        if not candidate or candidate >= cursor:
            candidate = int(min(oldest * 1_000_000, cursor - 1_000_000))
        cursor = candidate
        if cursor / 1_000_000.0 < window_start:
            break
        time.sleep(delay)

    if pages >= max_pages:
        log.warning(
            "Stopped at the %d-page cap with %d messages - raise chat.max_pages "
            "if this stream was busier than expected.",
            max_pages, len(seen),
        )

    messages = sorted(seen.values(), key=lambda m: m["offset"])

    if not messages:
        # Never write a bare empty list: it is indistinguishable from "this
        # stream had no chat" and would quietly produce chatless clips forever.
        ws.write_json(
            ws.chat_path,
            {
                "schema": SCHEMA_VERSION,
                "status": "unavailable",
                "reason": (
                    "Kick returned no messages in the stream's window. The VOD's "
                    "chat may have expired (7 days unverified / 30 verified), or "
                    "the channel id may be wrong."
                ),
                "channel_id": channel_id,
                "stream_started_at": started_at,
                "fetched_at": time.time(),
                "count": 0,
                "messages": [],
            },
        )
        raise StageError(
            "No chat messages found for this VOD in {0} request(s). Kick keeps "
            "chat only as long as it keeps the VOD.".format(pages)
        )

    payload = {
        "schema": SCHEMA_VERSION,
        "status": "ok",
        "source": "kick-api-v2-cursor",
        "channel_id": channel_id,
        "stream_started_at": started_at,
        "window": [round(window_start - started_at, 3), round(window_end - started_at, 3)],
        "fetched_at": time.time(),
        "pages": pages,
        "dropped": dropped,
        "count": len(messages),
        "messages": messages,
    }
    ws.write_json(ws.chat_path, payload)

    span = messages[-1]["offset"] - messages[0]["offset"]
    log.info(
        "Harvested %d message(s) over %d page(s), %s dropped by filters",
        len(messages), pages, dropped,
    )
    log.info(
        "  offsets %.1fs .. %.1fs (%.1f min of stream, %.1f msg/min) -> %s",
        messages[0]["offset"], messages[-1]["offset"],
        span / 60.0,
        len(messages) / (span / 60.0) if span > 0 else 0.0,
        ws.chat_path.name,
    )

    ws.mark_stage(STAGE, messages=len(messages), pages=pages, dropped=dropped)
    return ws.chat_path


def load_chat(ws: Workspace) -> Dict[str, Any]:
    """Read chat.json, raising a useful error when it isn't usable."""
    if not ws.chat_path.exists():
        raise StageError(
            "No chat for this workspace. Run: python -m clipbot chat --workspace {0}".format(
                ws.slug
            )
        )
    doc = ws.read_json(ws.chat_path)
    if doc.get("status") != "ok":
        raise StageError(
            "Chat is unavailable for this workspace: {0}".format(
                doc.get("reason") or "unknown reason"
            )
        )
    return doc


def messages_between(
    doc: Dict[str, Any],
    start: float,
    end: float,
    offset: float = 0.0,
) -> List[Dict[str, Any]]:
    """Messages whose adjusted offset falls in [start, end].

    `offset` is the calibration correction (VOD `recording_start` minus the
    stream's `stream_started_at` anchor). Message offsets are stored relative
    to `stream_started_at`, which is normally *earlier* than the moment the
    VOD's frame 0 was actually recorded - so raw offsets run ahead of the VOD's
    own clock by that gap, and correcting means subtracting it to pull each
    message earlier onto the VOD timeline. Measured on this channel: recording
    started ~26.6s after Kick opened the livestream record, so
    chat_offset_seconds ends up positive.
    """
    out = []
    for message in doc.get("messages") or []:
        at = message["offset"] - offset
        if start <= at <= end:
            item = dict(message)
            item["offset"] = round(at, 3)
            out.append(item)
    return out
