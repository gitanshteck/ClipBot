# Cue (the stream brief): what's next

Cue is the project's name (chosen 2026-11-15). Files, routes and settings keep
"brief".

Phase 1 (local rundown, OBS overlays, OBS dock) is done, and so is a first round
of chat awareness (below). Next, in rough order.

## Phase 2: talk to OBS
**Deliberately skipped (decided 2026-11-15):** not built, and not planned unless
he asks again. The Browser Sources are added to OBS by hand.

OBS WebSocket (built into OBS 28+; switch the server on under Tools → WebSocket
Server Settings). A one-command setup that adds the overlay Browser Sources to
the right scenes at the right size; optional automatic scene switching per
segment; Replay Buffer saves become clip markers with the exact stream time and
the clip file's path.

## Phase 3: Kick
**Deliberately skipped (decided 2026-11-15):** the Kick developer app and
everything that needs it (set the title and category on Start, post each
segment's question into chat, webhook chat over a tunnel). Chat reading went the
unofficial polling way instead; see "Chat awareness" below.

Original note: Kick's official API (a developer app the streamer creates): set
the title and category on Start, post each segment's question into chat. Live
vote bars and "closest guess" need live chat reading through Kick's unofficial
chat connection, which can break without notice.

## Live chatbot on stream (requested 2026-11-14)
**Built (phase 1):** the dock's "Ask the AI" panel, providers claude-cli /
gemini-cli / anthropic picked by `brief.chat.provider`, stream context in every
prompt, streamed answers, and Show on stream drawing the answer card on every
overlay. **Not built yet:** voice input, choosing which overlay shows the answer,
answering from chat messages.

Original request: an assistant the streamer can ask questions and talk to while live, when a topic
comes up that he wants to dig into, shown as a scene.

- **Interactive:** ask from the dock (type, later voice), follow-ups keep the
  thread, answers can be pinned on screen or kept host-only.
- **Themed and designed:** a new scene in `scenes.css|js` in the brief's visual
  language: question as the kicker, short answer in large type, two or three
  bullet points to riff on, the source when there is one. Fits the chat, game
  and vertical frames without covering the cam, chat or HUD.
- **Model:** a small, fast model for quick short answers, through his own
  subscription rather than a paid API key where possible:
  - Claude: Claude Code's headless mode (`claude -p`, a Haiku-class model) runs
    on the logged-in subscription; the dashboard calls it as a subprocess.
  - Gemini: the Gemini CLI (`gemini -p`) on a Google sign-in, or a Gemini API key.
  - A setting picks the provider, so either can be swapped in.
  Check each one's current terms, limits and latency before building.
- **Context:** every question carries the stream context: it is live and the
  answer will be read on air (short, spoken-friendly, Hinglish ok, nothing
  private), the current segment and item (from the live state), tonight's brief
  (topic, docket item, source), and which page or scene is showing.
- **Guardrails:** answers are stream-safe and say when something is unverified;
  nothing is posted to chat or anywhere else by the bot.

## Chat reactions, +1 counter, AI moments (requested 2026-11-15, built the same day)
**Built:** a chat "+1" / "-1" tally with an on-screen counter (one count per
chatter, resets with each card); a vibe meter and a word wall (opt in per overlay
with `?w=plus,callout,mood,words`); a one-tap welcome for new chatters; the chat
log on disk; **AI moments**: Cue's own short line on stream when chat does
something (a spike, a repeated message, a quiet spell, a closed vote, now and
then at random), shown in the dock first with a 5 second veto, every one logged
with its reason and the chat at the time; a private chat recap every 12 minutes;
and the overnight batch (lore log, clip titles) in `DAILY.md` Step 0f.
**Not built:** viewers asking the AI by command (**parked**, decided 2026-11-15:
it would cost usage on someone else's schedule and invite abuse; if it comes back
it goes through an approval queue in the dock with a cooldown); an AI voice;
automatic kill/death counting.
Cost guards: at most one on-stream line per 8 minutes, 5 an hour, 40 model calls a
day; nothing runs before the show is on or while the chat feed is down.

## Chat awareness for the dock (planned 2026-11-14, first round built 2026-11-15)
Read-only: the dock listens to live chat and never posts. Verify each platform's
current API before building (Kick's official API and its unofficial chat
connection both need a fresh check; YouTube Live chat needs an API key and has
a daily quota).

**Built (2026-11-15), Kick only, opt-in via the dock's Connect button:**
`clipbot/chatfeed.py` (logic) and `clipbot/kickchat.py` (transport that polls
Kick's unofficial messages endpoint every ~3 s). Only tested against a fake
source and the real endpoint while offline; **not yet seen on a live stream**.

- **Chat pulse (built):** messages per minute, a 10-minute sparkline, a "chat
  quiet for N min" alert that suggests a keep-alive beat from the brief, and a
  spike alert that offers a clip marker.
- **Votes (built):** live tally for 1/2/3 polls (the vote scene and docket
  polls) counted from chat into `state.votes`, one vote per chatter, latest
  wins, restarting when the card changes; the dock's hand buttons stay for when
  chat is not connected. **Not built:** the lock-and-reveal moment for the
  winner with its own sound, auto-advance, and for guess-the-price the closest
  guess ready to read out.
- **Question queue (built):** unanswered questions (a "?", "kab", "kya") with
  the asker's name; Ask AI, Pin or dismiss.
- **Repeat asks (built):** a short message from at least 3 different chatters in
  20 minutes is listed with its count. Not saved anywhere, so it does not feed
  the next day's brief yet.
- **People (partly built):** first-time chatters since connecting are listed.
  **Not built:** regulars, and new follows/subs/gifts as thank-you prompts (the
  polling endpoint does not carry them; Kick's official webhooks do, which need
  the developer app that was skipped).
- **Chat in the AI's context (built):** the chat rate and the last six messages
  go into the prompt while chat is connected.
- **Pin to screen (built):** a chat message as a "FROM CHAT" card on every
  overlay in place of the segment (an Ask the AI card takes priority).
- **Transport:** Kick polling built. **YouTube chat not built** (needs the Live
  Chat API and a `youtube.readonly` consent). A simulcast would merge both into
  one `ChatFeed`; the feed does not care where messages come from.

## Visual style: make it Teck's own (planned 2026-11-14)
- Display face: the brand's Big John in place of Unbounded (Latin only; needs
  the font file bundled locally, it is not on Google Fonts); Poppins stays for
  body and Devanagari.
- The real wordmark (solid fill, accent on the T) and logo in the card bug.
- Per-game looks: Warzone, Wardogs, Siege, Valorant each get a tint, a corner
  detail and a HUD-style frame; the brief picks one from tonight's game.
- Share the HUD look with the edit engine's HUD graphics so a clip made from the
  stream matches the edits.
- Motion: a signature intro/outro stinger, a tuned card transition, a sound cue
  per card (royalty-free only).
- **Sounds in transitions (requested 2026-11-14):** a short sound on every scene
  transition: card in/out, segment change, reveal, vote open, clip marked, Ask the
  AI answer shown, stinger on Start. Plays from the overlay Browser Source so it
  goes through OBS (its audio setting "Control audio via OBS", so it has its own
  fader and can be muted or ducked under your mic). Royalty-free only: his own
  library under work/_library/sfx or the sounds he supplies. Volume per sound
  type, one master mute in the dock, and a "quiet mode" for when he is mid-fight.
  Browsers block audio until the page has had a click, so the overlay needs a
  one-time unlock (OBS Browser Sources allow it by default).
- Day accents (weekday colour shift), a milestone mode for giveaways and
  ONETHREE streams, a Hinglish line style for Devanagari captions.

## Name
**Cue** (chosen 2026-11-15). Candidates were Cue, Teck Deck, Backstage, Rundown,
Teck Control. File, route and setting names keep "brief".

## Chat access: what the research found (2026-11-15)
Full notes: the research agent's report (chat_access.md, not in the repo).
- **Kick has no official way to read chat from a loopback-only app.** Its official
  API delivers `chat.message.sent`, follows, subs and gifts only as webhooks, which
  need a public HTTPS callback (a tunnel such as Cloudflare Tunnel or ngrok) and an
  OAuth app with the `events:subscribe` scope. A websocket option has been "on the
  roadmap" since Feb 2025 with no date.
- **Kick's unofficial websocket (Pusher) still works but is fragile:** Kick retired
  its Pusher app key in Oct 2026 and community clients now fetch the key at every
  connect; the web client is moving to Centrifugo; Kick's terms (23 Mar 2026) bar
  scraping and bypassing access controls. No maintained Python 3.9 client exists
  (`kick-chat` needs 3.11+). One report says a broadcaster's own channel may not
  get a usable subscription this way: needs a live test. The REST polling in
  `stages/chat.py` is unconfirmed for live use.
- **YouTube is the clean option:** `liveChatMessages.list` costs 5 quota units a
  call against 10,000 a day (roughly 1,800 units an hour at one call per 10 s,
  assumed interval); it needs the `youtube.readonly` scope, so the existing
  loopback OAuth needs a second consent.
- **Ranking for a local read-only dock:** YouTube official polling, then a Kick
  Pusher client behind a swappable transport, with Kick webhooks over a tunnel as
  the reserve.
- **What was built instead (2026-11-15):** Kick REST polling (the endpoint
  `stages/chat.py` uses for VODs, called live with a browser TLS fingerprint),
  because it needs no public address, no developer app and no new dependency, and
  suits a loopback-only dashboard. It is unofficial and fragile like the Pusher
  route; the research above had marked it unconfirmed for live use, and it is
  still unconfirmed on a real stream. The webhook route and the Pusher websocket
  were not built.

## "Type 1 or 2" cards are chat-aware (requested 2026-11-15)
Built: the vote scene and news-card polls draw live tallies (bars and counts, the
leader highlighted) from `state.votes`; the dock counts by hand, or the chat feed
counts "1" / "2" / "3" messages into the same field (one vote per chatter, latest
wins) when it is connected.
Not built: a lock-and-reveal moment for the winner with its own sound, an
optional auto-advance, and the winner passed to the next segment (for example the
game chosen). The same counting could power guess-the-price (closest guess); that
is not built either.
