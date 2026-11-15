# Cue

Cue is the name of the daily stream brief. The files, folders, routes and settings
still say "brief" (`tools/stream-brief/`, `clipbot/brief.py`, `/brief`,
`brief.*` in `config/settings.json`); only the project's name changed.

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
| `http://127.0.0.1:8765/brief/overlay/sound` | any always-on scene; tick "Control audio via OBS" | 1920 × 1080 |
| `http://127.0.0.1:8765/brief/dock` | Docks → Custom Browser Docks | any |

Each overlay is a **Browser Source**: paste the address, set the size, leave the
background transparent, and put it at the top of the scene's source list. The
overlays draw nothing until you press **Start** in the dock, then follow whatever
you press there. Tomorrow's brief loads by itself. `?bg=green` gives a chroma-key
background instead, for a Window Capture setup.

## Look and sounds

The scenes use the brand colours only: Teck Blue on near-black. Two files make
them match the brand exactly; both are optional and go in the brief dir
(`<work_root>/_brief` unless `brief.dir` is set):

- `fonts/BigJohn.woff2` (or `.otf` / `.ttf`): the Big John display face. Until it
  is there the scenes use Unbounded.
- `brand/lockup.png`: the TECK lockup. Until it is there a text mark is shown.

Reload the overlays after adding them. The brief's optional `look` field
(`{theme: clean | hud | slash, tag}`) changes the card shape and adds a small tag
such as the game name; it never changes colours.

Transition sounds play from the `sound` overlay (one Browser Source, "Control audio
via OBS", so they get their own fader in the mixer). They use files from the
library's sfx folder (`<work_root>/_library/sfx`): start, segment, card, reveal,
vote, ask, clip, kill and death, each with a default file name (see
`SOUND_DEFAULTS` in `clipbot/brief.py`); an event whose file is missing is silent.
To change one, set `brief.sounds.<event>` in `config/settings.json`: `false` turns
it off, `"my_sound.ogg"` swaps the file, `{"file": "my_sound.ogg", "volume": 0.5}`
also sets the volume (0 to 1). The dock's "Sounds on/muted" button silences them all. Nothing
plays when the page first loads or on a new day.

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
place of the current segment; press it again to go back, or just press Next, Back or Item and it comes down by itself. Long answers shrink to fit the card. The model is told the
stream is live and what segment and topic are on screen. Nothing is posted to
chat.

The **Search** checkbox lets the model search the web for news, prices and
anything recent (slower, up to about 90 seconds); the answer card then shows
"via <site>". It is remembered in the browser and defaults to
`brief.chat.search` (on). It does not apply to the Gemini CLI.

Setup, one of:

- **Claude Code signed in with your Claude plan** (preferred): install it and run
  `claude` once, then `/login`. Questions run through it on your plan, not API
  billing. A Claude Code installed at `~/.local/bin/claude.exe` is found without
  any PATH change, but it has its own login, separate from the desktop app, and
  needs that one-time `/login`. Until then questions fall back to
  `ANTHROPIC_API_KEY`.
- `ANTHROPIC_API_KEY` set in the environment where the dashboard starts: used as
  the fallback.
- The Gemini CLI with a `GEMINI_API_KEY` (unverified; Google moved free sign-ins
  off it in June 2026).

`brief.chat.provider` in `config/settings.json` pins one; `auto` tries them in
that order.

## Chat awareness (Kick)

The dock's **Chat** panel reads your Kick chat while you are live. It never posts
anything to chat.

**Connect:** in the dock, open Chat, check the channel name (it remembers the last
one; the first time it offers `teckislive`), and press **Connect**. Press
**Disconnect** to stop. Nothing connects by itself, and it starts from scratch
each time the dashboard restarts. `brief.kick.channel` in `config/settings.json`
sets a default channel (empty unless you set it) and `brief.kick.interval_seconds`
the polling gap (default 3 seconds, never faster than 1.5).

What the panel shows:

- **Pulse:** messages a minute and a sparkline of the last 10 minutes. A **spike**
  (at least 6 messages in 30 seconds and well above the last 5 minutes) shows a
  **Clip it** button that drops the usual clip marker. A **quiet** alert appears
  after 3 minutes without a message and suggests a keep-alive beat from the brief.
- **Votes:** with "Count 1 / 2 votes from chat" ticked, a vote card counts the
  messages that are just `1`, `2` or `3` into the live tallies, one vote per
  chatter and the latest one wins. It restarts when the card changes, and the dock's
  reset restarts it. While chat is counting, the hand vote buttons are disabled.
- **Questions:** messages that look like questions (a `?`, or words such as kab,
  kya, how, what), newest first. **Ask AI** sends one to the Ask the AI panel,
  **Pin** puts it on every overlay as a "FROM CHAT" card in place of the current
  segment (an Ask the AI answer shown on stream takes priority over it; **Take it
  down** removes it), and the cross dismisses it.
- **Chat keeps asking:** the same short message from at least 3 different chatters
  in the last 20 minutes, with the count.
- **New here:** people who chatted for the first time since you connected.
- **Ask the AI** also gets the current chat rate and the last six messages while
  chat is connected.

Caveats, plainly:

- **It is not Kick's official API.** It polls the same unofficial endpoint that
  downloads a VOD's chat. Kick can change or block it without notice, and Kick's
  terms restrict scraping, so it is opt-in, read-only, slow (one request every few
  seconds) and meant for your own channel only. The dock works without it.
- Kick's official chat feed is webhooks to a public HTTPS address, which a
  loopback-only dashboard cannot receive. Setting up a Kick developer app and a
  tunnel for it was deliberately not done.
- **Not verified on a live stream.** It was tested against a fake chat source and
  the real endpoint while the channel was offline. How it behaves with real chat
  volume, and whether Kick serves your own channel this way while live, is
  unknown until the first stream.
- Follows, subs and gifts are not in this feed, so there are no alerts for them.
- OBS WebSocket automation (adding the sources and switching scenes for you) is
  deliberately not built; you add the Browser Sources by hand as above.
- Not built yet: a locked "winner" moment with its own sound after a vote,
  auto-advance, closest-guess counting for guess-the-price, YouTube chat. See
  `ROADMAP.md`.

## Files

| File | What it does |
|---|---|
| `scan_library.py` | Scans Steam, Epic, Riot, Ubisoft, EA, GOG and Xbox app installs plus Steam play history into `<brief dir>/library.json` |
| `steam_ccu.py` | Current Steam player counts for those games into `<brief dir>/steam_ccu.json` (public endpoint, no key) |
| `build_page.py` | Checks `<brief dir>/brief.json` and writes the artifact copy to `<brief dir>/site/index.html` |
| `DAILY.md` | The daily refresh procedure (for a scheduled Claude task or by hand) |
| `ROADMAP.md` | What is built and what is not |

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
