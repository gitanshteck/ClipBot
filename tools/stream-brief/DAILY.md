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
   ISO time. Take the markers of the most recent stream date. Find that stream's
   workspace (newest `state.json` whose `stream_started_at` falls on that date).
   VOD time = `at - stream_started_at - chat_offset_seconds` (the offset is 0 when
   absent); say "approximate" if there is no workspace yet and use the first
   segment marker as 0:00. Write:
   - `brief.json.last_stream.chapters`: one per segment marker, `{t, label}`,
     YouTube rules: first chapter at 0:00, at least three, at least 10 s apart.
     Labels in his voice (lowercase).
   - `brief.json.last_stream.markers`: the clip markers, `{t, label}`.
   - `work/_brief/markers/<date>.json` with both lists plus the workspace slug, for
     the editors and ClipBot.

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
   `angle` (why it lands from him specifically), `clip` (high/med/low), `risk`,
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
   its `why`, tied to evidence in `knowledge.json`.
6. **Write `work/_brief/brief.json`** in the same shape as the last one (schema 2):
   new `date`, `weekday`, `edition` + 1, `generated_at`, headline, titles (2-3, his
   voice), checklist, platforms, timing, `production` unchanged unless his layout
   changed.
7. **Build.** `python tools/stream-brief/build_page.py` must print `built ...`.
   Fix whatever it reports and re-run.
8. **Publish.** Republish the artifact named in `knowledge.json` (`artifact.slug`)
   with `work/_brief/site/index.html` served as `index.html`, using the live
   version as `base_version`. Keep the title, icon and declared capabilities
   (do not call declare_capabilities). Then stop: no message to anyone.
