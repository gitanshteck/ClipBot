# Clip-worthiness rubric — gitanshteck (Kick)

<!--
This file is yours to edit. The pipeline reads it verbatim and drops it into the
prompt sent to Claude — no pipeline code changes when you rewrite it.
Personalised from the Notion "Streaming — Kick Channel" project, 2026-08-02.
-->

## The channel

`kick.com/gitanshteck` — brand is **Teck**. Three pillars, weighted roughly equally:

1. **FPS gaming** (and other titles — Assassin's Creed Shadows, Valorant)
2. **Tech** — hardware, setups, software, builds, deals, general nerdery
3. **Unscripted real talk** — opinions, stories, tangents, hanging out with chat

The vibe is chill and community-first, not a hype-and-shout channel. Streams are
tagged **Hindi**; the delivery is Hinglish and that is the differentiator, not a
defect — never prefer a moment just because it happened to be in English.

**Goal: growth.** Clips are the primary growth surface, going to a clips-only
Instagram (`GitanshTeckClips`) first, YouTube later. So judge every candidate as
a **vertical short-form clip shown to someone who has never heard of this
channel** — not as a highlight for existing regulars.

## What makes a clip

Strongest candidates have a **hook in the first 2 seconds**, then a setup, then a
payoff — all inside one window. If a stranger scrolling past wouldn't stop within
two seconds, it's not a clip.

Signals, roughly in order of how well they travel:

1. **Gameplay payoff** — a clutch, a multi-kill, an absurd death, a comeback, a
   near-miss. Must be legible without the preceding hour.
2. **Emotional spike** — genuine laughter, panic, disbelief, going quiet and then
   losing it. Energy *change* matters more than volume; forced hype travels badly
   and is off-brand here.
3. **Funny bit** — a joke that lands, a running gag, self-roast, banter with chat.
   Hinglish wordplay and code-switched punchlines count — a lot of the humour
   lives in the switch itself.
4. **A take worth arguing with** — a sharp opinion on a game, a company, a piece
   of hardware, an industry move. Strong opinions travel; safe ones don't.
5. **Tech moment that teaches something** — a setting that fixes a real problem, a
   spec myth busted, a purchase called good or bad and *why*. Must be self-
   contained and useful to someone who wasn't there.
6. **Chat interaction** — reading a donation, reacting to a raid, a viewer callout
   that gets a real reaction.

### Use your judgement on topics

The list above is not exhaustive, and it is not a filter to apply mechanically.
**If something genuinely interesting came up during the stream, flag it even if it
doesn't fit any pillar above.** Unscripted real talk is a third of this channel —
a tangent about work, life in Australia, money, learning to build things, an
opinion about something entirely unrelated to gaming or tech, a story that just
happens to be good — all of that is in scope.

The test is: *would this hold a stranger's attention on its own?* If yes, flag it
and say plainly in the `why` field what makes it interesting. Prefer flagging a
borderline-interesting moment over missing it — a human reviews every candidate,
so a false positive costs 30 seconds and a miss costs the clip.

## What to skip

- Dead air, loading screens, menu navigation, AFK stretches, setup troubleshooting.
- Long strategy talk with no payoff.
- Moments that need an hour of prior context to land.
- Anything embarrassing or harmful out of context — assume it will be seen with no
  context whatsoever, by people who don't know him.
- **Segments with copyrighted music audible underneath.** The channel is strictly
  royalty-free on purpose, but if a track is clearly playing under a moment, note
  it in the `why` field so it can be checked before posting — Instagram and
  YouTube both enforce harder than Kick does.

## Clip shape

- Target **20–60 seconds**; Instagram Reels is the first destination. Never exceed 90.
- The best moment should land **early**, not at the end. If the payoff is 40
  seconds in, start closer to it.
- Start 1–3 seconds **before** the setup line so it doesn't open mid-word.
- End shortly after the reaction settles — don't run into the next topic.
- Prefer boundaries that fall on transcript segment edges.

## Language notes

Primarily **Hindi with English code-switching**; gaming and tech vocabulary is
usually English. Judge the moment by meaning, not by which language it's in.
Mid-sentence switching is normal here and is not a sign of a bad transcript.

Transcript segments carry a `low_confidence` flag — Hindi/English mixing throws
the transcription model off. Treat flagged text as approximate: don't build a
candidate on flagged text alone, but don't discard a moment just because flagged
segments sit inside it.

## Writing the output

Write `description` in his own voice — **casual, lowercase, direct, minimal
punctuation**. Not marketing copy. It should read like a note to himself about
what's in the clip.

Write `why` plainly: which signal it hits and what makes it land. If it's a
judgement call rather than an obvious pillar match, say so.

## Volume

Roughly **5–15 candidates per hour** of stream. Quality over coverage, but lean
slightly generous on genuinely interesting talk — those are the easiest ones to
miss and the hardest to find again by scrubbing. An empty list is a valid answer
for a slow stream.
