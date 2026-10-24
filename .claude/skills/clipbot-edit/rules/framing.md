# Framing, grade, overlays, platforms

## Canvas

- `fit` letterboxes; `fill` crops to the canvas around a focus point; `blur` fits the
  picture over a blurred, zoomed copy of itself - the standard way to put 16:9 gameplay
  in 9:16 or 1:1 without bars. Use `blur` for non-native shapes, never `fill`, unless
  you have checked that the HUD and the action survive the crop.
- `framing.crop` takes fractions of the source and runs *first*: it is how a webcam,
  chat or a facecam box is removed before framing. The engine makes crop sizes even.
- **Display size, not stream size.** A phone clip can be 1920x1080 with a rotation of
  90; `probe_media` swaps width and height, because sizing from the stream size
  letterboxes the whole edit. Do not work around this in a spec.
- The master is built at the spec's output preset. For the hero platform render at its
  preset (`edit render --preset shorts_1080x1920`) - overlays are then drawn at that
  canvas size. `edit deliver --variants` re-frames the finished master for the rest
  (a second encode; audio is copied).

## The webcam: zooms must keep it, and know where it is

The stream's overlay is not fixed: the facecam is a cyan-bordered box that sits top-right on
the YouTube layout, left-middle on Kick's, bottom-left on a privacy-policy page, and is absent
in a full-cam scene - and the stream switches between them. A camera move that ignores that
crops it.

- The engine handles it (`edit.safe_framing`, on): it finds the box in the clip's footage,
  second by second, and moves each zoom's pivot a few percent towards it so the window always
  holds it; it also moves a lower-third or callout that would sit on it. You do not author
  this - leave a camera key's `x`/`y` at the default unless you mean something else.
- **Read the warnings**: `moved the overlay ... to keep it off the webcam` (fine, but a very
  long title is better shortened), `zoom ... limited` (the box would not fit the window),
  `webcam is moving between layouts ... while the picture is zoomed` (the layout is switching
  inside a zoom: shorten the zoom or cut the clip at the switch), `the webcam could not be
  located` (the scan failed: that clip's camera is as authored - look at it).
- **Full-cam scenes have no box**, so nothing protects the chat panel or the logo there: a
  1.07x zoom still crops their edges. Prefer no zoom, or a deliberate one aimed at the face.
- Look at `verify/<tier>/webcam_NN.jpg` after every render: it is the only check that is a
  picture, and the one that would have caught a camera that pivoted on the wrong corner.

## Grade

- `gamer_pop` (default for game footage): a bit more saturation and bite, no
  white-balance shift - game footage has a palette worth protecting. `neutral_punch`,
  `warm_lift`, `cool_clean` for real-world footage. Strengths: `subtle`, `normal`,
  `strong`.
- The grade sits under overlays so a callout is never tinted. Grade the whole clip, not
  a window, unless one moment needs it (`grade.window`, source seconds).
- Do not grade to fix a bad source. A 720p upscale stays a 720p upscale.

## Overlays

- Font: a library font (`clipbot library list`, e.g. Anton for impact, Bebas Neue for
  titles). The engine falls back to a system font; without any real font ffmpeg's
  drawtext renders nothing on Windows - a silent failure, so overlays are PNGs drawn by
  Pillow instead.
- Size is a fraction of canvas height (`0.05` callout, `0.08-0.10` card title). Check
  the storyboard on the smallest output you ship: a callout legible at 1080p can be
  unreadable on a phone-sized Short.
- Anchor at `x=0.5` and `y` 0.8 for callouts (bottom third, clear of the crosshair and
  scope); move up (0.68) for a second overlay, never on top of the first.
- Accent colours carry meaning across the reel and should not be reused for something
  else: nade orange `#FF8A00`, headshot red `#FF3B3B`, long-shot cyan `#35D0FF`,
  multi-kill purple `#B96BFF`, default amber `#FFB300`.

## Output presets (YouTube's upload guide)

H.264 High, yuv420p, 2 B-frames, closed GOP of half a second, AAC 48 kHz, faststart;
`youtube_1080p60`, `youtube_1080p30`, `shorts_1080x1920`, `square_1080`, `portrait_4x5`.
60 fps only if the source is 60 fps; otherwise frames are repeated and the file is
bigger for nothing (the engine warns).

## Thumbnails

Take them from the edit's own moments (callout landed, camera mid-punch), sharp and
well exposed, never near a join; `edit deliver` ranks candidates and writes the best
three at 1280x720 under 2 MB. Choose by eye: a frame with a readable victim name and
the callout beats a sharper empty one.
