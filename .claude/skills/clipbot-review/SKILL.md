---
name: clipbot-review
description: How to turn the user's timestamped comments on a ClipBot edit (`clipbot edit review`, or the review page at /edits/<name>/review in the dashboard) into spec changes, taste rules and a changelog. Use whenever the user says they left comments or notes on a render, asks "what did I comment", "apply my notes", "fix the things I flagged", "what changed since v4", or gives feedback with a time in it ("at 3:12 the zoom is too much"). Read the clipbot-edit skill's HARD-RULES.md before changing a spec.
---

# ClipBot review

The user watches a render and leaves notes at times. Your job is to turn each note
into something that **happened and was seen**, or into an honest "I did not do this,
because". A comment is never "resolved" because a spec was edited; it is resolved
when the next render was looked at and shows the change.

## Read them

```
python -m clipbot edit review NAME list                 # open comments, in viewing order
python -m clipbot edit review NAME list --all           # including done ones (what was said, what was done)
python -m clipbot edit versions NAME --changes 8        # every playable render and what changed between them
```

Each line is `id kind time on <render> [clip +offset] text`. The **clip and offset are what
to follow**: the time is a second of the render named by `on`, and a re-render moves it.
`on` says which render the user was watching (`preview`, `final` or `v<N>`); a note on an
old version may already be fixed - check the clip in the current spec before acting.

Kinds, and what each asks of you:

| kind | what it means | what you do |
|---|---|---|
| `change` | do this here | change the spec (or the lineup, then `edit episode`), re-render, look, resolve |
| `taste` | how they always / never want things | `edit review NAME taste ID` (keeps their words), and apply it to this edit by hand |
| `question` | why is it like this? | answer in `reply` with the real reason (a number, a rule, a measurement); change nothing unless the answer exposes a mistake |
| `note` | nothing to do | acknowledge with `reply`, then resolve |

## For every comment

1. **Find the moment.** Open the clip in the spec (`edit inspect NAME`, or `spec.json`
   by clip id) and read what it contains at that offset: source, in/out, camera keys,
   overlays, markers, captions (`captions.json`). If the comment is about words, check
   `words.json`; if about framing, `edit scenes NAME`. Do not guess from the text alone.
2. **Say what you think it means before you change anything** when it is ambiguous
   ("too much" - zoom, shake, flash, or the whole clip?). One short question to the user
   beats a wrong re-render. If you can only guess, reply on the comment with the guess
   and leave it open.
3. **Make the smallest change that answers it**, in the spec's own terms (a camera key's
   zoom, an overlay's `at`, a clip's `in`/`out`, a `gain_db`). Obey `HARD-RULES.md` - a
   comment does not suspend them. If a rule forbids what was asked (zooming into the
   webcam, cutting inside a word), say so in `reply` and offer the nearest thing that
   is allowed.
4. **Re-render only what changed** (`edit render NAME --preview` - unchanged clips are
   reused; `edit validate` first). Then **look at the clip** at the same offset:
   `edit verify NAME --preview`, the contact sheets, or a frame at that second. A change
   you did not look at is not done.
5. **Resolve with what was changed and where it was seen:**
   `edit review NAME resolve ID --how "camera zoom 1.18 -> 1.10" --in preview`
   (`--in v5` once it is in a final). If you could not or should not do it:
   `resolve ID --wontfix --how "why"`. If it did not work: `reopen ID`.

## The changelog is written for you

A **final** render (`edit render NAME`) archives the previous final as `render/v<N>/`,
records what changed in `versions.json` and `changelog.md` (clips added, removed, trimmed,
gain, captions, camera, music, master...), and prints it. Do not write one by hand; read
it back (`edit versions NAME --changes 20`) and make sure it says what you meant to do.
If a line surprises you, something changed that you did not intend: find out why.
Previews are not versioned.

## Taste

A `taste` comment, or a `change` comment that is really a rule ("always bigger
captions"), becomes a standing rule: `edit review NAME taste ID --text "the rule, in
general terms"`. Keep what *they* said (the command does); the rule is yours to word,
specific enough to apply ("captions at least 6% of the frame tall", not "better
captions"). `edit episode` and `edit new` print the rules in force; nothing in the
renderer reads them, so **you** apply them, in the spec, where the user can see it.
Retire one that stops being true: `edit taste retire ID`.

## Report

When you are done, tell the user, per comment: what it said, what you did (or why you
did not), and where they can see it (version and time). Name the comments you left open
and why. Say what you could not check (you cannot hear the render: loudness and timing
are measured, taste is not). Do not summarise as "addressed all feedback" - list them.

## Mistakes to avoid

- Resolving a comment because the spec was edited, without looking at the render.
- Applying a comment to the wrong clip because the time moved between versions
  (follow the clip id and offset, not the second).
- Treating a preference as a one-off, so the same note comes back on the next edit.
- Changing three things when one was asked.
- Answering a `question` comment with a change, or a `change` with an explanation.
