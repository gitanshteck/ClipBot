# Roundups (a numbered variety episode)

The format is Teo's MISH MASH series (YouTube channel Teo, Twitch `teosgame`): 10-16
minutes of stream moments from many games and friends, a cold open *before* a short
sting, then 8-12 chaptered segments with recurring bits ("Out of context ..."), joke
chapter names, the funniest line as the title, an outro. `edittemplates.episode`
builds the structure; **choosing the moments is the author's job** and is most of the
work.

## Choosing moments

- **Evidence per moment, written down.** A moment goes in the lineup only with a
  transcript window you read, a frame you looked at, or a measurement. Say which in
  the clip's `note`. `candidates.json` descriptions are a reading list, not evidence
  (HARD-RULES 1).
- **Look for running gags before one-offs.** Three mortar misses, two deaths in his own
  smoke, four helicopter fiascos make a *chapter*; a single funny line is an
  "out of context" beat. Search the transcripts for the recurring word, then read each
  hit.
- **Setup, then payoff, then stop.** In on the first line of the setup, out right
  after the last word of the payoff (plus a breath). Whisper's line starts landed within
  ~0.1-0.3 s of the speech onset, but its ends are guesses: read the pauses
  (`editcut.find_pauses` around the time) and cut in the gap after the punchline.
- **Cut dead air out of the middle, not the ends.** Two clips of one bit, 10 s of
  silence apart, are better than one clip with the silence in it.
- **A story is a chapter.** "The Base" (build, "I'm a genius", the roof worry, the
  helicopter glitch, "I told you", the first time out) works because each act is its own
  beat with its own lower-third. Look for arcs with a payoff, not only one-liners.
- **Do not name a person or a number the footage does not show.** Credits are the
  stream titles and dates (verifiable), not a guessed cast list.

## Structure

- Cold open first (a clean 20-40 s bit, nothing on top of it), then the sting card
  (`TECK MISH MASH #N`, a jingle and a few seconds of theme), then the chapters, the
  strongest near the end, an end card with a bed under it (dark; YouTube's end-screen
  elements go there).
- Chapter names are the lower-thirds (top centre, 2.6 s) and the YouTube chapters
  (>= 10 s apart, 0:00 forced). Keep them short and a little joke.
- Hard cuts only, a whoosh at each chapter start. Music only where the audio is not the
  point (the kill reel, the sting, the end card); under talk it fights the voice.
- A talking clip needs camera life (`_ambient_camera`: a slow push-in and a snap-in on
  each `punch`): the pacing gate counts events, and a still shot of a map over a joke is
  a dead stretch. A punch belongs on the punchline, at most a few per clip.
- Quiet clips are matched up (`edit episode` does it); a clip with music or a stranger's
  mic inside it is *measured*, not assumed.

## Before you present

- Strong language: count it per clip from the transcript and list the chapters; the
  first 30 s of the video matters most to the platform. Say what you could not hear.
- Music inside a clip (a song someone sings, an outro track) can draw a Content ID claim:
  say so in the notes.
- Source mix: Kick is 720p60, YouTube is 1080p60 - state the share that is upscaled.
- The title is a placeholder until the author chooses one; `post.md` says so.
