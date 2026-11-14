# Stream brief

A daily, on-stream run of show for the channel: how to open the stream, what to
react to, which chat games to run, where to go live and what to play from the
games installed on this PC. It runs in two places from the same page:

- **On this PC, inside the ClipBot dashboard** (`python -m clipbot.server`): the
  rundown at `/brief`, the OBS overlays and the OBS control dock. This is the
  one to stream with.
- **As a private claude.ai artifact**: the same rundown to read on your phone and
  to comment on (comments sent to Claude reach the daily run).

## OBS setup (once)

| Address | Add it to | Size |
|---|---|---|
| `http://127.0.0.1:8765/brief/overlay/chat` | your Cam scene | 1920 × 1080 |
| `http://127.0.0.1:8765/brief/overlay/game` | Game Scene | 1920 × 1080 |
| `http://127.0.0.1:8765/brief/overlay/full` | Intro (pre-show) | 1920 × 1080 |
| `http://127.0.0.1:8765/brief/overlay/vertical` | your vertical scenes | 1080 × 1920 |
| `http://127.0.0.1:8765/brief/dock` | Docks → Custom Browser Docks | any |

Each overlay is a **Browser Source**: paste the address, set the size, leave the
background transparent, and put it at the top of the scene's source list. The
overlays draw nothing until you press **Start** in the dock, then follow whatever
you press there. Tomorrow's brief loads by itself. `?bg=green` gives a chroma-key
background instead, for a Window Capture setup.

## On stream

Press **Start the run** in the dock, then **Next** through the segments. Item ▲ ▼
steps through a reaction block or guessing rounds, Reveal shows an answer, K and D
count kills and deaths, and **CLIP THAT** drops a marker. Segment changes and clip
markers are saved to `<brief dir>/markers/<date>.json`; the next day's run turns
them into YouTube chapters and an editor clip list.

## Ask the AI

The dock has an **Ask the AI** panel: type a question and a small, fast model
answers while you are live (a one-line answer you can say out loud plus up to
three points to riff on). **Show on stream** puts the answer on every overlay in
place of the current segment; press it again to go back. The model is told the
stream is live and what segment and topic are on screen. Nothing is posted to
chat.

Setup, one of:

- **Claude Code signed in with your Claude plan** (preferred): install it and run
  `claude` once. Questions run through it on your plan, not API billing.
- `ANTHROPIC_API_KEY` set in the environment where the dashboard starts: used as
  the fallback.
- The Gemini CLI with a `GEMINI_API_KEY` (unverified; Google moved free sign-ins
  off it in June 2026).

`brief.chat.provider` in `config/settings.json` pins one; `auto` tries them in
that order.

## Files

| File | What it does |
|---|---|
| `scan_library.py` | Scans Steam, Epic, Riot, Ubisoft, EA, GOG and Xbox app installs plus Steam play history into `<brief dir>/library.json` |
| `steam_ccu.py` | Current Steam player counts for those games into `<brief dir>/steam_ccu.json` (public endpoint, no key) |
| `build_page.py` | Checks `<brief dir>/brief.json` and writes the artifact copy to `<brief dir>/site/index.html` |
| `DAILY.md` | The daily refresh procedure (for a scheduled Claude task or by hand) |

The page, scenes, overlays and dock live in `clipbot/server/static/brief/` and
`clipbot/server/templates/brief_*.html`; data handling is `clipbot/brief.py`.
The brief dir is `brief.dir` in settings, else `<work_root>/_brief` (gitignored):
the brief draws on stream transcripts and chat, which do not belong in this
public repo.

```
python tools/stream-brief/scan_library.py
python tools/stream-brief/steam_ccu.py
python tools/stream-brief/build_page.py
```
