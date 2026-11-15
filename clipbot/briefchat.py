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
import re
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
# With Search on, the same command may use only the two web tools (--allowedTools makes them
# run without a prompt in print mode) and gets a few turns to search, read and answer.
SEARCH_TOOLS = "WebSearch,WebFetch"
SEARCH_MAX_TURNS = "6"
SEARCH_TIMEOUT_SECONDS = 90
DEFAULT_SEARCH_TOOL = "web_search_20250305"   # the basic variant; the 20260209 one runs code under the hood and is slower
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
- Casual and direct. Answer in the language he asked in: plain English by default,
  Hinglish only when his question is in Hinglish. Never mix them mid-answer.
- Every line is one short sentence he can read out in one breath.
- If you are not sure, or it is recent news you may not know, say so in the first
  line instead of guessing. Never invent numbers, quotes or dates.
- It is a public live stream: nothing private about anyone, no slurs, no personal
  attacks on real people, no medical or legal certainty, no betting tips.
- Use the stream context below when the question refers to "this", "the topic" or
  what is on screen."""

SEARCH_RULES = """
You can search the web. For news, prices, releases, posts, patch notes, scores or
anything recent or that you do not know, search first, then answer. Do not write
anything before searching and never describe the search. Keep the answer in the
format above and name where it came from in one point, for example "per PC Gamer".
Answer from what you found; if the search found nothing useful, say that plainly."""


def find_exe(name: str) -> Optional[str]:
    """PATH first, then where the Windows installers put these tools (Claude Code's
    native installer uses ~/.local/bin and does not add it to PATH)."""
    found = shutil.which(name)
    if found:
        return found
    home = os.path.expanduser("~")
    for folder, exts in ((os.path.join(home, ".local", "bin"), (".exe", "")),
                         (os.path.join(os.environ.get("APPDATA", ""), "npm"), (".cmd", ".exe"))):
        for ext in exts:
            path = os.path.join(folder, name + ext)
            if folder and os.path.isfile(path):
                return path
    return None


def _which(cmd: List[str]) -> Optional[str]:
    return find_exe(cmd[0]) if cmd else None


def with_search(argv: List[str]) -> List[str]:
    """The Claude Code command line with only the web tools allowed and room for a few turns."""
    out = list(argv)

    def put(flag: str, value: str) -> None:
        if flag in out and out.index(flag) + 1 < len(out):
            out[out.index(flag) + 1] = value
        else:
            out.extend([flag, value])

    put("--tools", SEARCH_TOOLS)
    put("--max-turns", SEARCH_MAX_TURNS)
    if "--allowedTools" not in out:
        out.extend(["--allowedTools", SEARCH_TOOLS])
    return out


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


def stream_context(brief: Optional[Dict[str, Any]], state: Optional[Dict[str, Any]], page: str = "",
                   chat: Optional[Dict[str, Any]] = None) -> str:
    """What is on stream right now, as plain lines for the model."""
    lines = ["Right now: " + datetime.now().strftime("%A %d %B %Y, %H:%M") + " (streamer's local time). The stream is LIVE."]
    if page:
        lines.append("Asked from: " + page)
    if not brief:
        if chat:
            lines.append("Chat right now: {0} messages a minute. Latest from chat: {1}".format(
                chat.get("rate", 0), " | ".join("{0}: {1}".format(m.get("user", ""), m.get("text", "")) for m in chat.get("recent", [])[-6:])))
        return "\n".join(lines)
    if chat:
        lines.append("Chat right now: {0} messages a minute. Latest from chat: {1}".format(
            chat.get("rate", 0), " | ".join("{0}: {1}".format(m.get("user", ""), m.get("text", "")) for m in chat.get("recent", [])[-6:])))
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


def build_prompt(question: str, history: List[Dict[str, str]], context: str, search: bool = False, background: str = "") -> str:
    """One text prompt (the CLIs take a single prompt): rules, context, recent turns, question.
    ``background`` is the lore block (clipbot/lore.py); an empty one leaves the prompt as it was."""
    parts = [SYSTEM + (SEARCH_RULES if search else ""), "", "Stream context:", context]
    if background:
        parts += ["", background]
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

    def __init__(self, ask_id: str, question: str, provider: str, search: bool = False):
        self.id = ask_id
        self.question = question
        self.provider = provider
        self.search = search
        self.searching = False        # True from the moment a search starts until text arrives
        self.sources = []             # type: List[Dict[str, str]]
        self.text = ""
        self.done = False
        self.error = ""
        self.started = time.time()

    def to_dict(self) -> Dict[str, Any]:
        d = {"id": self.id, "q": self.question, "text": self.text, "done": self.done, "error": self.error,
             "provider": self.provider, "search": self.search,
             "searching": self.searching and not self.done, "sources": self.sources[:2]}
        d.update(parse_answer(self.text))
        return d


def _run_cli(argv: List[str], prompt: str, ask: Ask, emit: Callable[[], None], env: Optional[Dict[str, str]] = None,
             cwd: Optional[str] = None) -> None:
    exe = find_exe(argv[0]) or argv[0]   # resolves claude.cmd / gemini.cmd on Windows
    proc = subprocess.Popen([exe] + argv[1:], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            encoding="utf-8", errors="replace", bufsize=1, env=env, cwd=cwd,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    timer = threading.Timer(SEARCH_TIMEOUT_SECONDS if ask.search else TIMEOUT_SECONDS, proc.kill)
    timer.start()
    try:
        proc.stdin.write(prompt)
        proc.stdin.close()
        for line in iter(proc.stdout.readline, ""):
            ask.text += line
            emit()
        proc.wait()
        if proc.returncode != 0:
            # A failed run can still print a message on stdout ("Not logged in"): never show it as the answer.
            err = (proc.stderr.read() or "").strip().splitlines()
            said = ask.text.strip().splitlines()
            ask.text = ""
            raise RuntimeError("{0} exited {1}: {2}".format(
                argv[0], proc.returncode, (err[-1] if err else (said[-1] if said else "no output"))))
    finally:
        timer.cancel()
        for pipe in (proc.stdout, proc.stderr):
            if pipe:
                pipe.close()


def _host(url: str) -> str:
    return re.sub(r"^www\.", "", re.sub(r"^https?://", "", url or "").split("/")[0])


def _run_anthropic(settings: Settings, prompt: str, ask: Ask, emit: Callable[[], None]) -> None:
    import anthropic
    client = anthropic.Anthropic(timeout=SEARCH_TIMEOUT_SECONDS if ask.search else TIMEOUT_SECONDS, max_retries=1)
    model = str(settings.get("brief.chat.model", DEFAULT_MODEL))
    system, _, rest = prompt.partition("\n\nStream context:\n")
    kwargs = {}  # type: Dict[str, Any]
    if ask.search:
        kwargs["tools"] = [{"type": str(settings.get("brief.chat.search_tool", DEFAULT_SEARCH_TOOL)),
                            "name": "web_search", "max_uses": 2}]
    with client.messages.stream(
        model=model,
        max_tokens=1024,
        system=system,
        output_config={"effort": str(settings.get("brief.chat.effort", "low"))},
        messages=[{"role": "user", "content": "Stream context:\n" + rest}],
        **kwargs
    ) as stream:
        last = 0.0
        for event in stream:
            kind = getattr(event, "type", "")
            if kind == "content_block_start":
                block = event.content_block
                btype = getattr(block, "type", "")
                if btype == "server_tool_use":
                    ask.text = ""          # anything written before a search is narration, not the answer
                    ask.searching = True
                    emit()
                elif btype == "web_search_tool_result":
                    for res in (getattr(block, "content", None) or []):
                        url = getattr(res, "url", "")
                        if url and not any(src["host"] == _host(url) for src in ask.sources):
                            ask.sources.append({"title": str(getattr(res, "title", ""))[:80], "host": _host(url)})
            elif kind == "text":
                ask.searching = False
                ask.text += event.text
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
            argv = list(settings.get("brief.chat.claude_cli", DEFAULT_CLAUDE_CLI))
            if ask_obj.search:
                argv = with_search(argv)
            try:
                _run_cli(argv, prompt, ask_obj, emit, env=env, cwd=os.path.expanduser("~"))
            except Exception as exc:
                # Installed but not signed in (or out of plan allowance): in auto mode use the next provider.
                if str(settings.get("brief.chat.provider", "auto") or "auto") == "auto" and "anthropic" in available(settings):
                    log.warning("claude-cli failed (%s); falling back to the Anthropic API", exc)
                    ask_obj.text, ask_obj.provider = "", "anthropic"
                    _run_anthropic(settings, prompt, ask_obj, emit)
                else:
                    raise
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
        message = str(exc)
        if "not logged in" in message.lower():
            message = ("Claude Code isn't signed in yet: open a terminal, run 'claude', then /login. "
                       "Until then set ANTHROPIC_API_KEY to use the API.")
        ask_obj.error = message[:300]
    ask_obj.done = True
    emit()
    return ask_obj


def setup_hint(settings: Settings) -> str:
    return ("No chat model found. Sign in to Claude Code with your Claude plan (run 'claude' in a terminal, "
            "then /login), or set ANTHROPIC_API_KEY, or install the Gemini CLI with a GEMINI_API_KEY.")


def history_payload(items: List[Any]) -> List[Dict[str, str]]:
    return [{"q": str(i.get("q", "")), "a": str(i.get("a", ""))} for i in items if isinstance(i, dict)]


__all__ = ["PROVIDERS", "Ask", "ask", "available", "find_exe", "with_search", "build_prompt", "parse_answer", "pick_provider",
           "setup_hint", "stream_context", "history_payload"]
