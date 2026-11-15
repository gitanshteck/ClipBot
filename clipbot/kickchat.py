"""Read-only Kick chat transport for Cue's chat feed.

Polls the same public messages endpoint ``stages/chat.py`` already uses for VODs
(``/api/v2/channels/<id>/messages?cursor=<microsecond epoch>`` returns the 25
messages before that moment, newest first) with a browser TLS fingerprint
(curl_cffi), every few seconds, and hands new messages to a ``ChatFeed``.

This is NOT Kick's official API. The official route delivers chat only as
webhooks to a public HTTPS address, which a loopback-only dashboard cannot
receive, so it is out of scope here. This route is unofficial and may break or be
blocked without notice; Kick's terms restrict scraping, so it is opt-in (the dock's
Connect button), read-only, slow (one request every few seconds), and only ever
reads the user's own channel. The transport is deliberately small and swappable:
a ``ChatFeed`` does not care where messages come from.
"""

import threading
import time
from typing import Any, Callable, Dict, Optional

from .chatfeed import ChatFeed
from .stages.chat import _parse_created_at
from .utils import get_logger

log = get_logger("clipbot.kickchat")

CHANNEL_URL = "https://kick.com/api/v2/channels/{0}"
MESSAGES_URL = "https://kick.com/api/v2/channels/{0}/messages"
SEED_WINDOW_SECONDS = 120   # on connect, only messages from the last two minutes count
BACKOFF = (3, 6, 12, 30)


def parse_message(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """One API message -> the ChatFeed shape, or None for something without text or an id."""
    sender = raw.get("sender") or {}
    text = str(raw.get("content") or "").strip()
    mid = raw.get("id")
    if not mid or not text:
        return None
    created = _parse_created_at(str(raw.get("created_at") or ""))
    badges = [str(b.get("type")) for b in ((sender.get("identity") or {}).get("badges") or []) if isinstance(b, dict)]
    return {"id": str(mid), "user": str(sender.get("username") or sender.get("slug") or "?"),
            "slug": str(sender.get("slug") or "").lower(), "text": text,
            "at": created if created is not None else time.time(), "badges": badges, "type": raw.get("type")}


class KickPoller(threading.Thread):
    """Background thread: resolve the channel, then poll until ``stop()``."""

    def __init__(self, channel: str, feed: ChatFeed, interval: float = 3.0, impersonate: str = "chrome",
                 http=None, clock: Callable[[], float] = time.time):
        threading.Thread.__init__(self, name="kick-chat", daemon=True)
        self.channel = channel.strip().lower()
        self.feed = feed
        self.interval = max(1.5, float(interval))
        self.impersonate = impersonate
        self._http = http                  # injected in tests; curl_cffi's requests module otherwise
        self._clock = clock
        self._halt = threading.Event()
        self.channel_id = None             # type: Optional[int]
        self.status = "connecting"         # connecting | live | error | stopped
        self.error = ""
        self.last_ok = 0.0
        self._first = True

    def stop(self) -> None:
        self._halt.set()
        self.status = "stopped"

    def state(self) -> Dict[str, Any]:
        return {"status": self.status, "channel": self.channel, "error": self.error,
                "last_ok_age": round(self._clock() - self.last_ok, 1) if self.last_ok else None}

    # -- one step, separated so tests can drive it ----------------------------

    def _get(self, url: str) -> Dict[str, Any]:
        http = self._http
        if http is None:
            try:
                from curl_cffi import requests as http  # type: ignore
            except ImportError:
                raise RuntimeError("curl_cffi is not installed (pip install curl_cffi)")
        resp = http.get(url, impersonate=self.impersonate, timeout=15)
        if resp.status_code != 200:
            raise RuntimeError("Kick answered HTTP {0}".format(resp.status_code))
        return resp.json()

    def resolve(self) -> int:
        data = self._get(CHANNEL_URL.format(self.channel))
        cid = data.get("id")
        if not isinstance(cid, int):
            raise RuntimeError("No Kick channel called '{0}'".format(self.channel))
        self.channel_id = cid
        return cid

    def poll_once(self) -> int:
        """Fetch the newest page and feed anything new. Returns how many messages were added."""
        if self.channel_id is None:
            self.resolve()
        cursor = int(self._clock() * 1e6) + 5_000_000
        data = (self._get(MESSAGES_URL.format(self.channel_id) + "?cursor={0}".format(cursor)).get("data") or {})
        now = self._clock()
        added = 0
        for raw in reversed(data.get("messages") or []):   # oldest first
            msg = parse_message(raw)
            if not msg:
                continue
            fresh = now - msg["at"] <= SEED_WINDOW_SECONDS
            # first poll after connecting: remember old messages without counting them
            if self._first and not fresh:
                self.feed._ids.add(msg["id"])
                continue
            if self.feed.add(msg, count_newcomer=not self._first):
                added += 1
        self._first = False
        if not self._halt.is_set():
            self.status, self.error, self.last_ok = "live", "", now
        return added

    def run(self) -> None:
        failures = 0
        while not self._halt.is_set():
            try:
                self.poll_once()
                failures = 0
                self._halt.wait(self.interval)
            except Exception as exc:
                failures += 1
                self.status, self.error = "error", str(exc)[:200]
                log.warning("kick chat poll failed (%s): %s", failures, exc)
                self._halt.wait(BACKOFF[min(failures - 1, len(BACKOFF) - 1)])
