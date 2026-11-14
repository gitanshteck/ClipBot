"""Live chat for the stream brief: ask a small, fast model a question while on air.

The OBS dock sends a question; ``ask()`` builds a stream-aware prompt (we are
live, the answer is read out, what segment and topic are on screen), runs it
through a provider and streams the answer back through ``on_delta`` so the
dock and the overlays can type it out as it arrives.

Providers (``brief.chat.provider``; ``auto`` tries them in this order):

- ``claude-cli``  Claude Code's print mode, on the user's own Claude login
- ``gemini-cli``  the Gemini CLI, on the user's Google sign-in
- ``anthropic``   the Anthropic API (``ANTHROPIC_API_KEY`` or an ``ant`` login),
                  a Haiku-class model with low effort

The CLI command lines are settings (``brief.chat.claude_cli`` /
``brief.chat.gemini_cli``), so a flag change in either tool is a config edit.
The prompt always goes in on stdin, never on the command line.

Answer shape the model is asked for (``parse_answer`` reads it): a first line
that can be said in one breath, then up to three lines starting with "- ".
"""

import os
import shutil
import subprocess
import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from .config import Settings
from .utils import get_logger

log = get_logger("clipbot.briefchat")

PROVIDERS = ("claude-cli", "gemini-cli", "anthropic")
DEFAULT_MODEL = "claude-haiku-5-5"
# Claude Code print mode: prompt on stdin, Haiku, no tools, one turn, nothing saved.
DEFAULT_CLAUDE_CLI = ["claude", "-p", "--model", "haiku", "--output-format", "text", "--tools", "",
                      "--max-turns", "1", "--no-session-persistence"]
# Gemini CLI: stdin is appended to the -p text. Google moved free and AI Pro/Ultra
# sign-ins off this CLI in June 2026, so it may need GEMINI_API_KEY (unverified here).
DEFAULT_GEMINI_CLI = ["gemini", "-m", "flash", "-p", "Answer the question at the end of this input."]
QUESTION_MAX = 600
HISTORY_TURNS = 6
TIMEOUT_SECONDS = 60

SYSTEM = """You are the on-stream assistant for a live gaming and tech stream.
The streamer is Teck: an Indian streamer living in Australia who talks in Hinglish
(Hindi and English mixed) about FPS games, PC hardware and real life. He asks you
things live, mid-stream, and he or his viewers read your answer on screen right now.

How to answer:
- First line: the answer itself, in at most 18 words, easy to say out loud. One sentence.
- Then at most three lines starting with "- ", each under 14 words: facts, angles or a
  question he can throw to chat, so he can keep talking about it.
- Plain text only. No headings, no markdown beyond the "- " lines.
- Casual and direct. Hinglish is fine when he asks in Hinglish.
- If you are not sure, or it is recent news you may not know, say so in the first
  line instead of guessing. Never invent numbers, quotes or dates.
- It is a public live stream: nothing private about anyone, no slurs, no personal
  attacks on real people, no medical or legal certainty, no betting tips.
- Use the stream context below when the question refers to "this", "the topic" or
  what is on screen."""


def _which(cmd: List[str]) -> Optional[str]:
    return shutil.which(cmd[0]) if cmd else None


def available(settings: Settings) -> List[str]:
    """Providers that can run on this machine right now, in preference order."""
    out = []
    if _which(settings.get("brief.chat.claude_cli", DEFAULT_CLAUDE_CLI)):
        out.append("claude-cli")
    if _which(settings.get("brief.chat.gemini_cli", DEFAULT_GEMINI_CLI)):
        out.append("gemini-cli")
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN") or _have_anthropic_profile():
        out.append("anthropic")
    return out


def _have_anthropic_profile() -> bool:
    return os.path.isdir(os.path.join(os.path.expanduser("~"), ".config", "anthropic"))


def pick_provider(settings: Settings) -> Optional[str]:
    wanted = str(settings.get("brief.chat.provider", "auto") or "auto")
    have = available(settings)
    if wanted != "auto":
        return wanted if wanted in have else None
    return have[0] if have else None


def stream_context(brief: Optional[Dict[str, Any]], state: Optional[Dict[str, Any]], page: str = "") -> str:
    """What is on stream right now, as plain lines for the model."""
    lines = ["Right now: " + datetime.now().strftime("%A %d %B %Y, %H:%M") + " (streamer's local time). The stream is LIVE."]
    if page:
        lines.append("Asked from: " + page)
    if not brief:
        return "\n".join(lines)
    lines.append("Tonight's theme: " + str((brief.get("headline") or {}).get("kicker", "")))
    run = brief.get("run") or []
    st = state or {}
    if run and st.get("date") == brief.get("date"):
        seg = run[max(0, min(len(run) - 1, int(st.get("seg", 0) or 0)))]
        lines.append("On screen: segment '{0}' ({1}), card '{2}'.".format(seg.get("name", ""), seg.get("kind", ""), (seg.get("screen") or {}).get("title", "")))
        docket = {d.get("id"): d for d in brief.get("docket") or []}
        items = [docket[i] for i in seg.get("items", []) if i in docket]
        rounds = seg.get("rounds") or []
        idx = int(st.get("item", 0) or 0)
        if items:
            it = items[min(idx, len(items) - 1)]
            lines.append("Current topic: {0}. {1} (source: {2}, {3}).".format(it.get("headline", ""), it.get("summary", ""), it.get("source", ""), it.get("date", "")))
        elif rounds:
            r = rounds[min(idx, len(rounds) - 1)]
            lines.append("Current guessing round: {0} (answer hidden from chat until revealed).".format(r.get("q", "")))
        if seg.get("kind") == "game":
            lines.append("Playing: {0}. Kills {1}, deaths {2}.".format((seg.get("screen") or {}).get("title", ""), st.get("kills", 0), st.get("deaths", 0)))
    else:
        lines.append("The run of show has not started yet.")
    return "\n".join(lines)


def build_prompt(question: str, history: List[Dict[str, str]], context: str) -> str:
    """One text prompt (the CLIs take a single prompt): rules, context, recent turns, question."""
    parts = [SYSTEM, "", "Stream context:", context]
    turns = [h for h in history if isinstance(h, dict) and h.get("q")][-HISTORY_TURNS:]
    if turns:
        parts += ["", "Earlier in this chat:"]
        for h in turns:
            parts.append("Q: " + str(h.get("q", ""))[:QUESTION_MAX])
            if h.get("a"):
                parts.append("A: " + str(h.get("a", ""))[:800])
    parts += ["", "Question: " + question.strip()[:QUESTION_MAX]]
    return "\n".join(parts)


def parse_answer(text: str) -> Dict[str, Any]:
    """Split a reply into the headline line and up to three riff points."""
    lines = [ln.strip() for ln in (text or "").replace("\r", "").split("\n") if ln.strip()]
    lines = [ln.lstrip("#").strip() if ln.startswith("#") else ln for ln in lines]
    head, points = "", []
    for ln in lines:
        if ln[:2] in ("- ", "* ", "• "):
            if len(points) < 3:
                points.append(ln[2:].strip())
        elif not head:
            head = ln
        elif not points:
            head = (head + " " + ln).strip()
    return {"head": head, "points": points}


class Ask(object):
    """One question being answered. ``text`` grows as the answer streams in."""

    def __init__(self, ask_id: str, question: str, provider: str):
        self.id = ask_id
        self.question = question
        self.provider = provider
        self.text = ""
        self.done = False
        self.error = ""
        self.started = time.time()

    def to_dict(self) -> Dict[str, Any]:
        d = {"id": self.id, "q": self.question, "text": self.text, "done": self.done, "error": self.error,
             "provider": self.provider}
        d.update(parse_answer(self.text))
        return d


def _run_cli(argv: List[str], prompt: str, ask: Ask, emit: Callable[[], None], env: Optional[Dict[str, str]] = None,
             cwd: Optional[str] = None) -> None:
    exe = shutil.which(argv[0]) or argv[0]   # resolves claude.cmd / gemini.cmd on Windows
    proc = subprocess.Popen([exe] + argv[1:], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            encoding="utf-8", errors="replace", bufsize=1, env=env, cwd=cwd,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    timer = threading.Timer(TIMEOUT_SECONDS, proc.kill)
    timer.start()
    try:
        proc.stdin.write(prompt)
        proc.stdin.close()
        for line in iter(proc.stdout.readline, ""):
            ask.text += line
            emit()
        proc.wait()
        if proc.returncode != 0 and not ask.text.strip():
            err = (proc.stderr.read() or "").strip().splitlines()
            raise RuntimeError("{0} exited {1}: {2}".format(argv[0], proc.returncode, err[-1] if err else "no output"))
    finally:
        timer.cancel()
        for pipe in (proc.stdout, proc.stderr):
            if pipe:
                pipe.close()


def _run_anthropic(settings: Settings, prompt: str, ask: Ask, emit: Callable[[], None]) -> None:
    import anthropic
    client = anthropic.Anthropic(timeout=TIMEOUT_SECONDS, max_retries=1)
    model = str(settings.get("brief.chat.model", DEFAULT_MODEL))
    system, _, rest = prompt.partition("\n\nStream context:\n")
    with client.messages.stream(
        model=model,
        max_tokens=1024,
        system=system,
        output_config={"effort": str(settings.get("brief.chat.effort", "low"))},
        messages=[{"role": "user", "content": "Stream context:\n" + rest}],
    ) as stream:
        last = 0.0
        for text in stream.text_stream:
            ask.text += text
            if time.time() - last > 0.12:
                emit()
                last = time.time()
        final = stream.get_final_message()
    if final.stop_reason == "refusal":
        ask.text = ask.text or "Not answering that one on stream."


def ask(settings: Settings, ask_obj: Ask, prompt: str, on_delta: Callable[[Dict[str, Any]], None]) -> Ask:
    """Answer ``ask_obj`` with its provider, calling ``on_delta`` as text arrives and once at the end."""
    emit = lambda: on_delta(ask_obj.to_dict())  # noqa: E731
    try:
        if ask_obj.provider == "claude-cli":
            # An API key in the environment would beat the subscription login, so drop it for
            # this child; run from the user's home so no project CLAUDE.md is loaded.
            env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
            _run_cli(list(settings.get("brief.chat.claude_cli", DEFAULT_CLAUDE_CLI)), prompt, ask_obj, emit,
                     env=env, cwd=os.path.expanduser("~"))
        elif ask_obj.provider == "gemini-cli":
            _run_cli(list(settings.get("brief.chat.gemini_cli", DEFAULT_GEMINI_CLI)), prompt, ask_obj, emit)
        elif ask_obj.provider == "anthropic":
            _run_anthropic(settings, prompt, ask_obj, emit)
        else:
            raise RuntimeError("No chat provider is available")
        if not ask_obj.text.strip():
            ask_obj.error = "The model gave an empty answer."
    except Exception as exc:  # shown in the dock, never on stream
        log.warning("brief chat via %s failed: %s", ask_obj.provider, exc)
        ask_obj.error = str(exc)[:300]
    ask_obj.done = True
    emit()
    return ask_obj


def setup_hint(settings: Settings) -> str:
    return ("No chat model found. Install Claude Code and run 'claude' once to sign in with your Claude plan, "
            "or set ANTHROPIC_API_KEY, or install the Gemini CLI with a GEMINI_API_KEY.")


def history_payload(items: List[Any]) -> List[Dict[str, str]]:
    return [{"q": str(i.get("q", "")), "a": str(i.get("a", ""))} for i in items if isinstance(i, dict)]


__all__ = ["PROVIDERS", "Ask", "ask", "available", "build_prompt", "parse_answer", "pick_provider",
           "setup_hint", "stream_context", "history_payload"]
