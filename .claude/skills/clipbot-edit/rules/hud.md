# The HUD look: motion graphics (AFTER ACTION)

Read with `HARD-RULES.md` and `look.md`. This is the style where the graphics are the point: a series card that
slams in, an operation header over each chapter, a contact plate on every kill (the range counting up to a lock on a
0-300 m ruler), and a debrief card of what the video contained. It is made with Remotion (React) drawn to alpha video;
the engine composites it like any overlay. `clipbot edit motion list` prints what exists.

## When

- A kill montage or a "report" the viewer should feel was *made*: the first AFTER ACTION, a best-of, a milestone.
- **Not** Teo's roundups: those cut hard and say little (`roundups.md`). The HUD look on a variety show is a
  costume. And not for a stream whose feed was unreadable: the plates then say KILL CONFIRMED with no range, which
  is honest and a little empty; use the plain callouts or fix the scan first (`moments coverage`).

## What a graphic may say

- **Only what was measured, and only about what is in the edit.** The distance on a plate is the feed's; a kill with
  none shows KILL CONFIRMED and no number (`edithud.contact`). The chapter's "3 CONTACTS // LONGEST 99 M" and the
  debrief's totals are counted from the kills that are in the lineup, never from the ones considered
  (`edithud.tally`). The tease (`Acquire`) scrambles its digits and never settles: it claims nothing.
- A name the OCR could not read is not drawn (HARD-RULES 33 territory; `killfeed.readable_name` already blanks it).

## How to build one

1. `clipbot library synth` once: the cues exist as library assets (`hud_contact`, `hud_slam`, `hud_op`,
   `hud_ratchet`, `hud_stamp`, `hud_sweep`). Put their ids in the lineup's `hud_sfx` / `beat_sfx`.
2. A lineup with `"look": "hud"`: `hud {kicker, sub}`, `hud_sfx`, `beds` (one music bed under several chapters),
   chapters of clips that name moments (`moment`) or kills (`kill_ids`), `hero: true` on the clip with the longest
   shot, and a cold open with `motion: [{comp: "Acquire", at, dur}]` if there is a shot to save.
3. **Draw stills before you render** (`clipbot edit motion still ID --frame N --props FILE`, laid over a real frame).
   Look for text on text, a plate over the squad list or the minimap, a number that is too big for its box. The
   webcam is top-right, chat bottom-right, minimap bottom-left on this stream: plates and headers sit top-left.
4. Then the usual ladder: `validate` -> `render --preview` -> **look at the frames at every hit** -> final -> `verify`.

## Timing

- A graphic is told its length and hit time and animates against them. The overlay's `at` is where it **lands** (the
  kill, the title's slam), so a plate's count locks on the kill frame and the thud lands with it. Do not author a
  graphic's start time: author where it lands.
- Sounds land on the same hit (`align: peak`): the contact cue is ticks that slow into a lock, and the lock is its
  peak. A card is never silent: the title has its riser and boom, the debrief its ratchet and stamp, a bed under both.
- Two plates closer than 3 s stack; a third on screen is noise. A header needs the first ~3 s of a chapter's first clip
  clear of kills (the context rule already keeps 10 s of lead-up).

## Check

`edit verify` passes or fails the picture and sound; it cannot tell you a plate hid the thing the viewer should see.
Open `moments_NN.jpg` (a frame 0.6 s after every marker: the plates are up there) and `joins_NN.jpg`.
