"""Live chat awareness for Cue (the stream brief): what chat is doing right now.

``ChatFeed`` is pure logic with no network: a transport (``kickchat.KickPoller``)
calls ``add()`` for every new message and the dashboard reads ``snapshot()``.
It answers the questions the dock cares about:

- pulse: messages per minute, a sparkline, how long chat has been quiet, a spike flag
- votes: one vote per chatter ("1" / "2" / "3"), the latest wins, counted from a start time
- questions: unanswered questions, newest first, that can be sent to the AI or pinned
- repeat asks: the same command or short phrase from several different chatters
- newcomers: people seen for the first time since the feed started
- plus: "+1" / "-1" typed in chat, one per chatter (the latest wins), counted from a start time
- mood and words: a rough vibe meter and the most-typed words of the last few minutes

Everything is read-only: nothing here posts to chat. Messages are plain dicts:
``{id, user, slug, text, at (epoch seconds), badges: [str]}``.
"""

import re
import threading
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional, Set

KEEP = 600                 # messages remembered
SPARK_BIN_SECONDS = 30
SPARK_BINS = 20            # 10 minutes of 30 s bins
QUIET_ALERT_SECONDS = 180
SPIKE_MIN_MESSAGES = 6     # in the last 30 s
SPIKE_FACTOR = 2.5         # against the previous 5 minutes' 30 s average
REPEAT_WINDOW_SECONDS = 20 * 60
REPEAT_MIN_CHATTERS = 3
QUESTION_WORDS = ("kya", "kab", "kaun", "kyu", "kyun", "kaise", "kitne", "kitna", "kahan", "kidhar", "how", "what", "when",
                  "why", "which", "who", "where", "can you", "do you", "are you", "is it", "will you")
HINDI_QUESTION_RE = re.compile(r"\b(kya|kab|kaun|kyu|kyun|kaise|kitne|kitna|kahan|kidhar)\b")
VOTE_RE = re.compile(r"^\s*([1-3])\s*[.!)]*\s*$")
PLUS_RE = re.compile(r"^\s*(\+\s?1|\+\+|\+)\s*[.!)]*\s*$")
MINUS_RE = re.compile(r"^\s*(-\s?1|−1|--|–\s?1)\s*[.!)]*\s*$")
HYPE_RE = re.compile(r"(lol|lmao|lmfao|haha+|pog+|gg|w|ez|insane|crazy|clutch|nice|bruh|omg|wtf)|[😂🤣🔥💀👏🎉]", re.I)
WORDS_WINDOW_SECONDS = 300
WORDS_STOP = frozenset(
    "the a an and or but of to in on at for with from by is are was were be been it this that these those i you he she we they me my "
    "your his her our their what when where why how who which did do does done have has had not no yes can could would should will "
    "just like get got into out over then than also very really bro bhai yaar hai hain tha thi kya ka ki ke ko se me mein par aur ya "
    "toh to nahi nhi bhi hi ho hoga kuch koi sab aap tum main hum yeh woh ye wo abhi".split())
WORD_RE = re.compile(r"[a-zऀ-ॿ][a-z0-9ऀ-ॿ']{2,}", re.I)
_TEXT_MAX = 300


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s!?#@']", "", text.lower())).strip()


class ChatFeed(object):
    def __init__(self, bots: Optional[List[str]] = None, clock: Callable[[], float] = time.time,
                 sink: Optional[Callable[[Dict[str, Any]], None]] = None):
        self.sink = sink   # called with every accepted message (the dashboard keeps a chat log with it)
        self._lock = threading.RLock()
        self._clock = clock
        self.bots = {str(b).lower() for b in (bots or [])}
        self.messages = deque(maxlen=KEEP)   # type: Deque[Dict[str, Any]]
        self._ids = set()                    # type: Set[str]
        self._order = deque(maxlen=KEEP * 2)  # ids in arrival order, to forget old ones
        self._users = set()                  # type: Set[str]
        self.started = clock()
        self.newcomers = deque(maxlen=30)    # type: Deque[Dict[str, Any]]
        self.dismissed = set()               # type: Set[str]
        self.total = 0

    # -- input -------------------------------------------------------------

    def add(self, msg: Dict[str, Any], count_newcomer: bool = True) -> bool:
        """Record one message. Returns False for a duplicate, an empty message or a bot."""
        mid = str(msg.get("id") or "")
        text = str(msg.get("text") or "").strip()[:_TEXT_MAX]
        slug = str(msg.get("slug") or msg.get("user") or "").lower()
        if not mid or not text:
            return False
        with self._lock:
            if mid in self._ids:
                return False
            if len(self._order) == self._order.maxlen:
                self._ids.discard(self._order[0])
            self._ids.add(mid)
            self._order.append(mid)
            if slug in self.bots:
                return False
            row = {"id": mid, "user": str(msg.get("user") or slug)[:40], "slug": slug, "text": text,
                   "at": float(msg.get("at") or self._clock()), "badges": list(msg.get("badges") or [])[:6]}
            self.messages.append(row)
            self.total += 1
            if self.sink is not None:
                try:
                    self.sink(dict(row))
                except Exception:  # a broken log must never stop the feed
                    pass
            if slug and slug not in self._users:
                self._users.add(slug)
                if count_newcomer:
                    self.newcomers.append({"user": row["user"], "slug": slug, "at": row["at"]})
            return True

    def dismiss(self, mid: str) -> None:
        with self._lock:
            self.dismissed.add(str(mid))

    def find(self, mid: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return next((m for m in self.messages if m["id"] == str(mid)), None)

    # -- pulse -------------------------------------------------------------

    def _count(self, seconds: float, until: Optional[float] = None) -> int:
        end = until if until is not None else self._clock()
        return sum(1 for m in self.messages if end - seconds <= m["at"] <= end)

    def rate_per_minute(self, window: float = 60.0) -> float:
        with self._lock:
            return round(self._count(window) * 60.0 / window, 1)

    def sparkline(self) -> List[int]:
        with self._lock:
            now = self._clock()
            bins = [0] * SPARK_BINS
            for m in self.messages:
                age = now - m["at"]
                idx = int(age // SPARK_BIN_SECONDS)
                if 0 <= idx < SPARK_BINS:
                    bins[SPARK_BINS - 1 - idx] += 1
            return bins

    def quiet_seconds(self) -> Optional[float]:
        """Seconds since the last message; None if there has been none since the feed started."""
        with self._lock:
            if not self.messages:
                return None
            return max(0.0, round(self._clock() - self.messages[-1]["at"], 1))

    def spike(self) -> bool:
        with self._lock:
            now = self._clock()
            recent = self._count(30.0, now)
            before = self._count(300.0, now - 30.0)
            baseline = before / 10.0   # 5 minutes is ten 30 s bins
            return recent >= SPIKE_MIN_MESSAGES and recent >= SPIKE_FACTOR * max(baseline, 1.0)

    # -- votes -------------------------------------------------------------

    def tally(self, options: int, since: float) -> List[int]:
        """One vote per chatter, the latest "1".."N" since ``since`` (epoch seconds) wins."""
        options = max(0, min(3, int(options)))
        if options < 2:
            return []
        with self._lock:
            latest = {}  # type: Dict[str, int]
            for m in self.messages:
                if m["at"] < since:
                    continue
                hit = VOTE_RE.match(m["text"])
                if hit and int(hit.group(1)) <= options:
                    latest[m["slug"] or m["user"]] = int(hit.group(1))
            counts = [0] * options
            for choice in latest.values():
                counts[choice - 1] += 1
            return counts

    def plus(self, since: float) -> List[int]:
        """[plus, minus]: "+1" / "-1" typed since ``since``, one per chatter, the latest wins."""
        with self._lock:
            latest = {}  # type: Dict[str, int]
            for m in self.messages:
                if m["at"] < since:
                    continue
                if PLUS_RE.match(m["text"]):
                    latest[m["slug"] or m["user"]] = 1
                elif MINUS_RE.match(m["text"]):
                    latest[m["slug"] or m["user"]] = -1
            return [sum(1 for v in latest.values() if v > 0), sum(1 for v in latest.values() if v < 0)]

    def mood(self) -> int:
        """A rough vibe meter, 0-100: how busy chat is against its own recent baseline, and how many
        of the last minute's messages are laughing or hyped. A hint for the eye, not a measurement."""
        with self._lock:
            now = self._clock()
            recent = [m for m in self.messages if now - m["at"] <= 60.0]
            if not recent:
                return 0
            before = self._count(300.0, now - 60.0) / 5.0   # per minute over the 5 minutes before
            busy = len(recent)
            lift = (busy - before) / max(before, 6.0)
            score = 40 + max(-25.0, min(35.0, lift * 35.0))
            hype = sum(1 for m in recent if HYPE_RE.search(m["text"])) / float(len(recent))
            return int(max(0, min(100, round(score + hype * 30.0))))

    def words(self, limit: int = 12) -> List[Dict[str, Any]]:
        """The most-typed words of the last five minutes (commands, votes and +1s left out)."""
        with self._lock:
            now = self._clock()
            counts = {}  # type: Dict[str, int]
            for m in self.messages:
                if now - m["at"] > WORDS_WINDOW_SECONDS or VOTE_RE.match(m["text"]) or PLUS_RE.match(m["text"]) or MINUS_RE.match(m["text"]):
                    continue
                if m["text"].startswith("!"):
                    continue
                for w in set(x.lower() for x in WORD_RE.findall(m["text"])):
                    if w not in WORDS_STOP:
                        counts[w] = counts.get(w, 0) + 1
            rows = [{"w": w, "n": n} for w, n in counts.items() if n >= 2]
            rows.sort(key=lambda r: (-r["n"], r["w"]))
            return rows[:limit]

    # -- questions, repeats, people ----------------------------------------

    @staticmethod
    def looks_like_question(text: str) -> bool:
        low = text.lower().strip()
        if VOTE_RE.match(low) or PLUS_RE.match(low) or MINUS_RE.match(low) or low.startswith("!"):
            return False
        if "?" in low:
            return True
        words = low.split()
        if len(words) < 3:
            return False
        if HINDI_QUESTION_RE.search(low):          # "gta 6 kab aa raha hai": the word can sit anywhere
            return True
        return any(low.startswith(w + " ") for w in QUESTION_WORDS)

    def questions(self, limit: int = 6) -> List[Dict[str, Any]]:
        with self._lock:
            now = self._clock()
            out = []
            for m in reversed(self.messages):
                if m["id"] in self.dismissed or not self.looks_like_question(m["text"]):
                    continue
                out.append({"id": m["id"], "user": m["user"], "text": m["text"], "age": round(now - m["at"])})
                if len(out) >= limit:
                    break
            return out

    def repeats(self, limit: int = 5) -> List[Dict[str, Any]]:
        """Commands and short phrases asked by several different chatters recently."""
        with self._lock:
            now = self._clock()
            who = {}  # type: Dict[str, Set[str]]
            shown = {}  # type: Dict[str, str]
            for m in self.messages:
                if now - m["at"] > REPEAT_WINDOW_SECONDS or VOTE_RE.match(m["text"]) or PLUS_RE.match(m["text"]) or MINUS_RE.match(m["text"]) or len(m["text"]) > 60:
                    continue
                key = _norm(m["text"])
                if len(key) < 3:
                    continue
                who.setdefault(key, set()).add(m["slug"] or m["user"])
                shown.setdefault(key, m["text"])
            rows = [{"text": shown[k], "chatters": len(v)} for k, v in who.items() if len(v) >= REPEAT_MIN_CHATTERS]
            rows.sort(key=lambda r: -r["chatters"])
            return rows[:limit]

    def recent(self, limit: int = 8) -> List[Dict[str, Any]]:
        with self._lock:
            return [{"id": m["id"], "user": m["user"], "text": m["text"]} for m in list(self.messages)[-limit:]]

    def newcomer_list(self, limit: int = 8) -> List[str]:
        with self._lock:
            return [n["user"] for n in list(self.newcomers)[-limit:]][::-1]

    # -- snapshot ----------------------------------------------------------

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            quiet = self.quiet_seconds()
            return {
                "rate": self.rate_per_minute(), "spark": self.sparkline(), "quiet": quiet,
                "quietAlert": quiet is not None and quiet >= QUIET_ALERT_SECONDS,
                "spike": self.spike(), "total": self.total, "chatters": len(self._users),
                "questions": self.questions(), "repeats": self.repeats(), "newcomers": self.newcomer_list(),
                "recent": self.recent(), "mood": self.mood(), "words": self.words(),
            }
