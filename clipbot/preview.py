"""Full-fidelity proxy previews for the reel editor.

The instant `<canvas>` preview in the dashboard is exact for geometry - it draws
the same crop rectangles `reelspec.resolve` computed - but it cannot show what a
punch, a shake, a burned-in title or a ducked music bed will actually look and
sound like. So effects get confirmed by rendering them, through the *real*
filter graph, at a third of the canvas.

That is the whole design constraint: the proxy differs from the final render
only in canvas size and encoder settings. Nothing about the effects is
approximated, so a preview that looks right cannot be followed by a render that
doesn't.

Why not the JobRunner
---------------------
`JobRunner.submit` refuses a second job for a workspace that already has one
queued or running. A preview routed through it would therefore be rejected
while a reel renders - and, worse, would occupy the workspace's only slot and
block a real render behind a throwaway. Previews also need supersede semantics
(you moved a slider; the render in flight is now worthless) that the JobRunner
has no concept of. Hence a separate, single-worker runner - which still reuses
`Progress` and `ffrun.run_ffmpeg`, so cancellation is the same code path the
reel stage uses rather than a second implementation of it.
"""

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, Optional

from .progress import JobCancelled, Progress
from .utils import StageError, get_logger

log = get_logger(__name__)

KEY_RE = re.compile(r"^pv_[a-f0-9]{16}$")


class _State(object):
    __slots__ = ("key", "slug", "started", "progress", "cancel_event", "future",
                 "error")

    def __init__(self, key, slug, progress, cancel_event):
        self.key = key
        self.slug = slug
        self.started = time.time()
        self.progress = progress
        # Progress reports cancellation but does not trigger it - the event is
        # the handle, exactly as JobRunner uses it.
        self.cancel_event = cancel_event
        self.future = None
        self.error = None


class PreviewRunner(object):
    """One proxy render at a time, deduped by key and superseded per workspace."""

    def __init__(self, settings, bus, cache_dir: Path):
        self.settings = settings
        self.bus = bus
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._active: Dict[str, _State] = {}
        self._pool = ThreadPoolExecutor(max_workers=1,
                                        thread_name_prefix="clipbot-preview")

    # -- paths ----------------------------------------------------------

    def path_for(self, key: str) -> Path:
        if not KEY_RE.match(key or ""):
            raise KeyError(key)
        path = (self.cache_dir / (key + ".mp4")).resolve()
        # Belt and braces alongside the regex: the served path must be inside
        # the cache directory no matter what arrived in the URL.
        if self.cache_dir.resolve() != path.parent:
            raise KeyError(key)
        return path

    def frames_dir(self, key: str) -> Path:
        return self.cache_dir / "frames" / key

    # -- submission -----------------------------------------------------

    def state(self, key: str) -> Optional[_State]:
        with self._lock:
            return self._active.get(key)

    def submit(self, key: str, slug: str, render):
        """Start `render(progress, out_path)` unless this key is already going.

        Returns "rendering" either way; the caller has already checked the
        cache, so reaching here means there is work to do.
        """
        with self._lock:
            if key in self._active:
                return "rendering"  # identical request already in flight

            # Anything else for this workspace is now stale by definition - the
            # user changed something, which is what produced a new key.
            for other in list(self._active.values()):
                if other.slug == slug:
                    other.cancel_event.set()

            cancel_event = threading.Event()
            progress = Progress(cancel_event=cancel_event)
            state = _State(key, slug, progress, cancel_event)
            self._active[key] = state
            state.future = self._pool.submit(self._run, state, render)
            return "rendering"

    def cancel(self, slug: str) -> int:
        with self._lock:
            targets = [s for s in self._active.values() if s.slug == slug]
        for state in targets:
            state.cancel_event.set()
        return len(targets)

    def _run(self, state, render):
        out_path = self.path_for(state.key)
        timeout = float(self.settings.get("reel.preview.timeout_seconds", 60))
        watchdog = threading.Timer(timeout, state.cancel_event.set)
        watchdog.daemon = True
        watchdog.start()
        error = None
        try:
            render(state.progress, out_path)
        except JobCancelled:
            error = "cancelled"
        except StageError as exc:
            error = str(exc)[:600]
        except Exception as exc:  # noqa: BLE001 - a preview must never take the server down
            log.exception("preview %s failed", state.key)
            error = str(exc)[:600]
        finally:
            watchdog.cancel()
            with self._lock:
                self._active.pop(state.key, None)

        if error and out_path.exists():
            try:
                out_path.unlink()
            except OSError:
                pass

        elapsed = int((time.time() - state.started) * 1000)
        if not error:
            self._evict()
        self.bus.publish("preview", {
            "slug": state.slug,
            "key": state.key,
            "ok": not error,
            "error": error,
            "ms": elapsed,
            "url": "/media/{0}/preview/{1}.mp4".format(state.slug, state.key),
        })

    # -- cache ----------------------------------------------------------

    def _evict(self):
        """Trim the cache to reel.preview.cache_max_mb, oldest first."""
        limit = float(self.settings.get("reel.preview.cache_max_mb", 512)) * 1048576
        try:
            files = [p for p in self.cache_dir.glob("pv_*.mp4") if p.is_file()]
        except OSError:
            return
        total = 0
        stats = []
        for path in files:
            try:
                stat = path.stat()
            except OSError:
                continue
            stats.append((stat.st_mtime, stat.st_size, path))
            total += stat.st_size
        if total <= limit:
            return
        for _mtime, size, path in sorted(stats):
            if total <= limit:
                break
            try:
                path.unlink()
                total -= size
            except OSError:
                pass
