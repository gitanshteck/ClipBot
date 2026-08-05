"""Structured progress reporting for pipeline stages.

Stages log to stderr, which is fine for the CLI but gives a UI nothing to draw a
bar with. Each stage takes an optional `progress: Progress = NULL_PROGRESS`
keyword; the CLI passes nothing, so CLI behaviour is unchanged, while the server
passes a live object that forwards to SSE.

Nothing here imports a web framework - this module is safe to use from the CLI.
"""

import threading
import time
from typing import Any, Callable, Dict, List, Optional


class JobCancelled(Exception):
    """Raised inside a stage when the caller asked it to stop."""


class Progress(object):
    """Collects progress updates and forwards them to an optional sink.

    The base class is a no-op sink, so `NULL_PROGRESS` is just an instance with
    no callback. Every method is safe to call from any thread.
    """

    def __init__(
        self,
        sink=None,  # Optional[Callable[[str, Dict[str, Any]], None]]
        cancel_event=None,  # Optional[threading.Event]
        min_interval=0.2,
    ):
        self._sink = sink
        self._cancel_event = cancel_event
        self._min_interval = float(min_interval)
        self._lock = threading.Lock()
        self._last_emit = 0.0
        self._phase = None
        self._total = None
        self._unit = "s"
        self._started = time.time()
        self._phase_started = time.time()

    # ---- emitting -------------------------------------------------------

    def _emit(self, kind, payload, throttle=False):
        if self._sink is None:
            return
        if throttle:
            now = time.time()
            with self._lock:
                if now - self._last_emit < self._min_interval:
                    return
                self._last_emit = now
        try:
            self._sink(kind, payload)
        except Exception:
            # A broken UI sink must never take down a 90-minute transcription.
            pass

    def phase(self, name, total=None, unit="s"):
        """Start a named phase, optionally with a known total."""
        self._phase = name
        self._total = float(total) if total else None
        self._unit = unit
        self._phase_started = time.time()
        self._emit(
            "phase",
            {"phase": name, "total": self._total, "unit": unit},
        )

    def update(self, current, total=None, label=None):
        """Report position within the current phase."""
        if total:
            self._total = float(total)
        current = float(current)
        fraction = None
        eta = None
        if self._total:
            fraction = max(0.0, min(1.0, current / self._total))
            elapsed = time.time() - self._phase_started
            if fraction > 0.01 and elapsed > 1.0:
                eta = (elapsed / fraction) - elapsed
        self._emit(
            "progress",
            {
                "phase": self._phase,
                "current": current,
                "total": self._total,
                "fraction": fraction,
                "eta_seconds": eta,
                "label": label,
                "unit": self._unit,
            },
            throttle=True,
        )

    def event(self, kind, payload):
        """Emit a domain event, e.g. a transcript segment as it is produced."""
        self._emit(kind, payload)

    def log(self, message, level="INFO"):
        self._emit("log", {"message": message, "level": level})

    # ---- cancellation ---------------------------------------------------

    @property
    def cancelled(self):
        return self._cancel_event is not None and self._cancel_event.is_set()

    def check_cancelled(self):
        """Raise JobCancelled if the caller asked us to stop.

        Called from inside stage loops, so cancellation lands within one
        iteration rather than waiting for a 90-minute job to finish.
        """
        if self.cancelled:
            raise JobCancelled("cancelled by request")

    @property
    def cancel_event(self):
        return self._cancel_event


NULL_PROGRESS = Progress()
