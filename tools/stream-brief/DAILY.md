# Daily stream brief: how to rebuild it

This is the procedure a Claude session follows each day to refresh the stream
brief artifact. It is written for a scheduled task but works by hand too.

Run everything from the repo root (the checkout that holds `tools/stream-brief/`).
Private inputs live in `work/_brief/` (gitignored): `knowledge.json` (the stable
content study, his taste, and the artifact's slug), yesterday's `brief.json`, and
the `archive/` of past briefs. Stream workspaces are read from the main checkout's
`work/` folder (the path is in `knowledge.json` under `stream_data`).

## Rules

- The page is shown live on stream. Nothing private goes into `brief.json`: see
  `never_on_stream` in `knowledge.json`. `build_page.py` refuses obvious leaks,
  but the check is a backstop, not the filter.
- Read only outside `work/_brief/`: never post, upload, message anyone or write to
  Notion. The only writes outside the folder are replies on his own comment
  threads on the brief (step 0b).
- Mark rumours and "reported" numbers as such. Leave out politics, tragedy and
  personal allegations about creators.
- Use cheaper models (Sonnet subagents) for the web research; keep the run of
  show, the headline and the game pick for the main session.

## Step 0: read what he told the brief

The page saves three kinds of input. On this PC (the dashboard's `/brief`, the
OBS dock) they are files in the brief dir: `feedback.json` (keyed
`<date>_<kind>_<target>`, same row shape as below) and `markers/<date>.json`
(`{date, markers: [...]}`, same marker shape, `at` in UTC). On the claude.ai
artifact they go to its database and comments (artifact slug in
`knowledge.json`). Read both and treat them the same way; markers from the dock
are the ones that matter most, since that is where he streams from.

a. **Reactions** (`db_query`, collection `feedback`): one document per item he
   marked, `{date, edition, kind: docket|segment|game|scene, target, label,
   reaction: more|less|drop, at}`. Take the ones newer than
   `knowledge.json.feedback_seen_until`. A `drop` means never bring that item or
   format back; `less` and `more` shift the mix. When two or more reactions point
   the same way (for example two tech items dropped), write a dated line into
   `knowledge.json.taste` with the evidence. Then set `feedback_seen_until` to the
   newest `at` you processed. Summarise what changed in one stream-safe sentence
   in `brief.json.feedback_note` (or leave it empty).
b. **Comments** (`read_comments` on the slug): threads he sent to Claude. For each
   unresolved one, read it in full, apply the request to today's brief (content,
   curation, wording) or to `tools/stream-brief/page/template.html` if it is a
   small visual change and the build still passes. Reply in the thread
   (`reply_comment`) with one line saying what changed and in which edition, and
   resolve it (`resolve_comment`). If a request needs a design decision or a big
   change, reply saying so and leave it open for a session with him.
c. **Markers** (`db_query`, collection `markers`): written while he is on air.
   `{date, kind: segment|clip, seg_id, seg_name, item, label, at}` with `at` as an
   ISO time. **Only when a real stream happened that date**: a workspace whose
   `state.json` `stream_started_at` falls on it, or a Kick VOD newer than the
   newest workspace (`clipbot.stages.download.list_channel_vods('teckislive', 4)`).
   Dock presses with no stream behind them are testing (a date with hundreds of
   markers and one clip marker, no VOD): ignore them, write no chapters, never
   invent "approximate" ones. With a stream: VOD time =
   `at - stream_started_at - chat_offset_seconds` (the offset is 0 when absent).
   Write:
   - `brief.json.last_stream.chapters`: one per segment marker, `{t, label}`,
     YouTube rules: first chapter at 0:00, at least three, at least 10 s apart.
     Labels in his voice (lowercase).
   - `brief.json.last_stream.markers`: the clip markers, `{t, label}`.
   - `work/_brief/markers/<date>.json` with both lists plus the workspace slug, for
     the editors and ClipBot.

## Step 0d: read the profile (curation is about him, not just games)

The brief should reflect the whole person, not mostly gaming and news. Before
choosing the docket, read the private profile in `work/_brief/profile/` (path in
`knowledge.json` under `profile`), in this order: the newest `interview_*.md`
(his own answers; they win over everything else), `profile_draft.md` (inferred
from his data; lower weight, may be wrong) and `log.md`. Then:

- **Draw from the whole list.** Of the 8-12 docket items, at most about 4 are
  gaming or games-industry; at least 3 come from the non-gaming interests he named
  in the interview; vary which ones from day to day (check `log.md`).
- **One wildcard a day.** Add one item in a block named `wildcard`: a topic from
  outside his usual lists that the profile suggests he could enjoy (his answers
  say how much stretch he wants). Say in its `angle` why it might land.
- **Wanted-more areas first.** Interests he said he wants more of get the
  freshest slots until `log.md` shows chat or he reacting well or badly.
- **His limits.** Whatever the interview says about family, sensitive areas or
  banned topics applies on top of `never_on_stream`; where the interview says
  "no preference", do not invent a ban. Never copy private details from the
  profile into `brief.json`: use the topic, not the evidence.
- **His voice.** `take` and `question` lines reuse his live phrasing and humour
  as described in the profile; do not default to generic copy.
- **Tone of the run, not fixed.** His stream mode and gaming share vary by day:
  rotate the spine accordingly rather than assuming a ratio.
- **Learn.** After building, append one dated line per run to `profile/log.md`
  (topics used, block names, which interests were covered) and, from step 0a,
  dated lines for what he reacted to (more / less / drop). A `drop` on a topic
  means skip that topic for a week unless he asks again. Patterns that repeat
  go into the profile's interest ranking, marked as inferred.

If the profile folder is missing, run as before and say so in the reply.

## Step 0e: refresh the lore (what the on-stream AI remembers)

`work/_brief/lore/` (see `clipbot/lore.py`) is what Ask the AI knows about past
streams. When step 2 finds a stream newer than the newest `date` in
`lore/streams.json`, add a record for it at the top of that file (`date`, `slug`,
`title`, `platform`, `duration_hours`, `chat_msgs_per_hour`, `games`, up to 8
`topics`, 3-8 `moments` with `t_min`, `what`, `kind` taken from `candidates.json`
and chat spikes, up to 4 `lines`), then rewrite `lore/recent.md` (last 7 streams,
about 400 words, plus open threads). Add a callback to `lore/lore.md` only when a
bit repeated or he promised something; never copy private items into it and keep the
"Never state on stream" section aligned with `knowledge.json` `never_on_stream`.
Do not rewrite `lore.md` wholesale: he edits it by hand. If no new stream, change
nothing.

## Step 0f: the overnight batch (two jobs, once per real stream)

Only when a real stream happened on that date (the same test as step 0c). These
are batch calls, not per-minute ones.

1. **Lore log.** Read `work/_brief/chatlogs/<date>.jsonl` (every message the live
   feed accepted: `{at, user, slug, text}`), `work/_brief/live/ai_moments/<date>.json`
   (what the AI said on stream and why; `status` shown / vetoed) and the markers.
   Add to `lore/lore.md` (append only, never rewrite): inside jokes or running bits
   that repeated, a regular who did something memorable (handle only, what they
   said publicly), things he promised on stream, and any AI line that landed (a
   vetoed one is a signal too: do not repeat its angle). Then Step 0e's
   `streams.json` record and `recent.md` rewrite. Keep private items out: the
   "Never state on stream" section applies.
2. **Clip titles.** For each `clip` marker in `markers/<date>.json`, take the chat
   from 45 seconds before to 45 after the marker's UTC `at` (the chat log's `at` is
   epoch seconds) and, when the workspace transcript exists, the lines around the
   matching VOD time. Write 3 short title options per marker (under 60 characters,
   his Hinglish voice or plain English, no clickbait that the clip does not
   deliver) and save them as `clip_titles: [{t, titles: [...]}]` in the same
   markers file, next to the chapters. These are for the editors; nothing is
   published.

## Days prepared by hand

If `brief.json` (or `draft/brief.json`) already carries today's date in
Australia/Sydney time, the scheduled run does nothing and says so: a day the
user prepared by hand is left alone, and the next day runs normally. Before
replacing `brief.json` on any run, check he is not live on Kick (the channel API's
`livestream` field); if he is, write `draft/brief.json`, publish only the artifact
copy and leave the live brief alone.

## Steps

1. **Library and player counts.**
   `python tools/stream-brief/scan_library.py` then `python tools/stream-brief/steam_ccu.py`.
2. **Last stream.** Find the newest workspace under the stream-data folder by
   `state.json` `stream_started_at`. If it is newer than the last brief's
   `last_stream.date`, fill `last_stream` from its `state.json` and `chat.json`
   (messages, messages per hour over `state.duration`, unique chatters) and pick
   one callback from its `candidates.json`. If nothing new, keep the previous one
   (and its chapters).
3. **Today's docket.** Collect 8-12 items from the last 72 hours from
   `knowledge.json` `daily_sources` plus web search: gaming news for the games he
   plays, tech/hardware, Indian internet and gaming culture, one light item. Apply
   `taste`. For each: `id`, `block`, `headline`, `card` (the on-screen title:
   lowercase, at most 48 characters, readable in one glance), `summary`, `take`
   (his voice), `question` (for chat), optional `poll` (2-3 short options),
   `angle` (why it lands from him specifically), `clip` (high/med/low), `risk`, `links`
   (up to 6 `{label, url, kind}` he can open live from the dock: the official trailer
   or reveal video first, then the official announcement, then the best article;
   kind is trailer|video|official|article|post|source, label at most 32
   characters; only URLs you opened and confirmed, never constructed ones; leave
   it out rather than guess),
   `source`, `url`, `date`. Group into 2-3 blocks.
4. **Games and calendar.** Re-rank `games.ranked` from `library.json`, the fresh
   Steam counts and any new season/update news (`games_policy`). Refresh Twitch
   viewers-per-channel weekly (Mondays) from SullyGnome's 30-day category pages.
   Update `calendar` and `week` (drop past days, add the next).
5. **Run of show.** Same spine every day, contents rotate. Every segment has a
   `scene` the on-air view draws:
   `starting` (pre-show agenda + countdown, t = -5), `card` (talk), `topic` (react
   block, `items` = docket ids), `guess` (`rounds` of `{q, a, note}`, answers must
   be sourced), `vote` (`poll` of two games + `rule`), `game` (`rule`, `predict`;
   kills/deaths are counted live), `next` (outro). Give each segment an `agenda`
   label for the pre-show screen. Pick segments from `segment_library` so the same
   chat game does not run two days in a row (check `archive/`). Every segment keeps
   its `why`, tied to evidence in `knowledge.json`. Optionally add a `look` to
   the brief or to a segment: `{theme: "clean" | "hud" | "slash", tag: "up to 24
   characters"}` (for example the game name as the tag). Themes change the card
   shape only. Colours never vary by game: Teck Blue is the only accent.
6. **Write `work/_brief/brief.json`** in the same shape as the last one (schema 2):
   new `date`, `weekday`, `edition` + 1, `generated_at`, headline, titles (2-3, his
   voice), checklist, platforms, timing, `production` unchanged unless his layout
   changed.
6b. **Links on every card.** Every docket item gets `links` where a real one exists, and
   a run segment can carry its own `links` (for example the game block's trailer).
   The dock shows them next to the card's notes and opens them on the PC. Check
   that a trailer link is the publisher's official upload, not a re-upload.
7. **Build.** `python tools/stream-brief/build_page.py` must print `built ...`.
   Fix whatever it reports and re-run.
8. **Publish.** Republish the artifact named in `knowledge.json` (`artifact.slug`)
   with `work/_brief/site/index.html` served as `index.html`, using the live
   version as `base_version`. Keep the title, icon and declared capabilities
   (do not call declare_capabilities). Then stop: no message to anyone.
