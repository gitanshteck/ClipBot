# Stream brief: what's next

Phase 1 (local rundown, OBS overlays, OBS dock) is done. Next, in rough order.

## Phase 2: talk to OBS
OBS WebSocket (built into OBS 28+; switch the server on under Tools → WebSocket
Server Settings). A one-command setup that adds the overlay Browser Sources to
the right scenes at the right size; optional automatic scene switching per
segment; Replay Buffer saves become clip markers with the exact stream time and
the clip file's path.

## Phase 3: Kick
Kick's official API (a developer app the streamer creates): set the title and
category on Start, post each segment's question into chat. Live vote bars and
"closest guess" need live chat reading through Kick's unofficial chat
connection, which can break without notice.

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
