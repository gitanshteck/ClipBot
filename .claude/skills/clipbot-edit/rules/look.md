# Look: beats and transitions

The moves an editor reaches for, named so a lineup can ask for them: `clipbot edit beats`
lists them. They are *vocabulary*, not decoration. Use one when the footage earns it, and
say in your report which you used and why. A roundup with a beat in every clip is the
machine-made look this file exists to avoid.

## When

- **Restrained (the roundup default):** hard cuts and a whoosh at each chapter. At most
  one beat per chapter, on the line that is the joke. A transition preset on a chapter only
  when the author asks for it or the two clips are different in kind (a cam-only talking
  clip into gameplay, where a whip hides the layout change). Teo's series cuts hard.
- **Kill montages and hype edits:** beats and presets are the point, within the profile's
  pacing gate and `max_gap`.
- **Never on a kill clip's kill moment** - the callout, punch and slow-mo are already doing
  that job - and never two moves within a second of each other.

## The beats (a lineup clip's `"beats": [{"do": ..., "at": source second, ...}]`)

| do | use it for | notes |
|---|---|---|
| `freeze_zoom` | the punchline or reveal: hold the frame, punch in, optional caption | the zoom is a **camera key**, so safe framing keeps the webcam in the window; the hold adds exactly `hold` seconds to the clip; `text` pops for the hold and a second after |
| `caption_pop` | a line the picture needs said out loud ("wait bro"), optionally with a sticker | big, outlined, brand display face; put it where nothing important is (`y`, default 0.78) |
| `impact` | a hit: shake, flash, sound, optional sticker | shake is a moving crop and is not a zoom: keep `shake` under ~12 px near a corner webcam |
| `replay` | play the moment again, slower, tagged REPLAY | inserts a clip after this one (`<id>r<N>`), 0.5x by default, flash in; it adds `(back + forward) / rate` seconds, so budget for it |

A beat 0.3 s from a clip's edge has no picture on one side of it: `edit episode` refuses it
by name rather than dropping it. Sounds come from the lineup's `beat_sfx` (`hit`, `whoosh`,
`glitch`, `scratch`: `{asset|file, gain_db}`); a role with no entry plays nothing and
`edit episode` prints a `BEAT:` line. **Use the library's "Error" sound for `glitch`, not
"Glitch":** that asset is 30 ms and the engine drops anything under 50 ms.

## The transitions (a lineup chapter's `"transition"`)

Chosen by looking at each candidate between two real clips, not from names:

| preset | what it is | measured |
|---|---|---|
| `whip` | xfade `hblur`, 0.3 s, whoosh | a real whip-pan streak; at 0.5 s it is heavy, 0.3 s reads as a fast pan |
| `glitch` | xfade `pixelize`, 0.2 s | a convincing digital mosaic |
| `zoom_through` | push in on the outgoing clip, out of the incoming one, under a 0.3 s cross-fade | **not** xfade `zoomin`: that dips through a muddy brown-black at its midpoint. The push is camera keys, so it falls back to a plain cross-fade (and says so) when a punch is in the way or a card is on either side |
| `flash` / `dip` / `crossfade` | a white flash, a dip through black, a plain cross-fade | the engine's own |

A chapter with a `transition` does not also get the chapter whoosh (the preset brings its
own sound) unless it says `"whoosh": true`. A cross-fade overlaps the clips, so the edit
is shorter by the overlap.

## Check

`edit validate` (pacing), `edit render --preview`, then *look at the frames around each
move*: the freeze must hold, the caption must clear the webcam and the chat, the whip
must be a streak and not a smear. `edit verify` catches a click or a level jump at the
join but cannot tell you the move was a good idea. Preview-tier `webcam in frame` misses
are noise (the one-pixel border smears at 540p); the final render is the check.
