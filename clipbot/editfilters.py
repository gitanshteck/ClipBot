"""ffmpeg filtergraph fragments for the edit engine.

String building only - no IO, no subprocess. The renderer (`stages/edit.py`) probes
sources, rasterises overlays and runs ffmpeg; this module decides what the graph
*says*, so the arrangement can be unit-tested (golden strings, plus a test that
evaluates the easing expressions numerically against `editspec.ease_value`) and a
real-ffmpeg smoke test can prove a built graph actually parses.

How a clip is built
-------------------
One ffmpeg process per clip, one *input per retiming piece* (`-ss a -t d -i
segment`), not one input split many ways. A 12-second 1080p60 clip split into 40
`trim` branches makes `concat` queue every later branch's frames until the earlier
pieces finish - gigabytes of RAM for a graph that is conceptually trivial. Separate
inputs are read in order, so only one piece is ever in memory. The segment file the
pieces seek into is encoded with a short GOP by the renderer, which makes each
seek cheap.

Video and audio leave the clip as two files (video-only mp4, PCM wav) and are
assembled and mastered on separate paths: that is what lets the final audio be
mixed, ducked, loudness-normalised and encoded *once*, and the video stream be
copied rather than re-encoded again for the sake of an audio change.

Order of operations, and why
----------------------------
  retime -> fps conform -> canvas fit -> grade -> camera -> punch/shake -> overlays
  -> flash -> transition fades -> exact-length trim

Retiming comes first so every later stage works in *output* time (`edittimeline`
has already mapped authored source seconds onto it); the grade sits under the
overlays so a callout is never tinted; flash is last of the visible layers - the
same reason `fxspec.build_tail_chains` gives: a flash the title punches through is
not a flash.
"""

from typing import Any, Dict, List, Optional, Tuple

from . import editspec

# --------------------------------------------------------------------------
# expression helpers
# --------------------------------------------------------------------------


def q(expr: str) -> str:
    """Single-quote an expression. Not cosmetic: `between(t,1,2)` has commas, and an
    unquoted option value ends at the first one, splitting the graph in two."""
    return "'" + expr + "'"


def num(v: float) -> str:
    """A compact, locale-proof number for a filtergraph."""
    s = "{0:.4f}".format(v).rstrip("0").rstrip(".")
    return s if s not in ("", "-0") else "0"


def ease_expr(kind: str, p: str) -> str:
    """The ffmpeg expression for `editspec.ease_value(kind, p)`; p is an expression."""
    P = "clip({0},0,1)".format(p)
    if kind == "linear":
        return P
    if kind == "in2":
        return "pow({0},2)".format(P)
    if kind == "out2":
        return "(1-pow(1-{0},2))".format(P)
    if kind == "inout2":
        return "if(lt({0},0.5),2*pow({0},2),1-pow(-2*{0}+2,2)/2)".format(P)
    if kind == "in3":
        return "pow({0},3)".format(P)
    if kind == "out3":
        return "(1-pow(1-{0},3))".format(P)
    if kind == "inout3":
        return "if(lt({0},0.5),4*pow({0},3),1-pow(-2*{0}+2,3)/2)".format(P)
    raise editspec.SpecError("unknown ease {0!r}".format(kind))


def back_out_expr(p: str, overshoot: float = 1.70158) -> str:
    """easeOutBack: overshoots 1.0 then settles - the 'pop' of a callout arriving."""
    P = "clip({0},0,1)".format(p)
    c3 = overshoot + 1.0
    return "(1+{0}*pow({1}-1,3)+{2}*pow({1}-1,2))".format(num(c3), P, num(overshoot))


def keyed_expr(keys: List[Tuple[float, float, str]], var: str = "t") -> str:
    """Piecewise value over time from (time, value, ease) keys.

    The segment from key A to key B eases with B's curve. Before the first key the
    value is the first key's; after the last it is the last key's. One nested
    if() per key - a handful for a camera move, never hundreds.
    """
    if not keys:
        raise editspec.SpecError("keyed_expr needs at least one key")
    if len(keys) == 1:
        return num(keys[0][1])
    expr = num(keys[-1][1])
    for (t0, v0, _), (t1, v1, ease) in reversed(list(zip(keys, keys[1:]))):
        span = max(1e-3, t1 - t0)
        seg = "{0}+({1})*{2}".format(num(v0), num(v1 - v0), ease_expr(ease, "({0}-{1})/{2}".format(var, num(t0), num(span))))
        expr = "if(lte({0},{1}),{2},{3})".format(var, num(t1), seg, expr)
    return "if(lte({0},{1}),{2},{3})".format(var, num(keys[0][0]), num(keys[0][1]), expr)


class Graph(object):
    """An append-only list of chains with a counter for unique labels."""

    def __init__(self) -> None:
        self.lines: List[str] = []
        self._n = 0

    def label(self, prefix: str = "n") -> str:
        self._n += 1
        return "{0}{1}".format(prefix, self._n)

    def add(self, line: str) -> None:
        self.lines.append(line)

    def chain(self, lin: str, filters: str, prefix: str = "n") -> str:
        """Append `[lin]filters[out]` and return the new label."""
        out = self.label(prefix)
        self.add("[{0}]{1}[{2}]".format(lin, filters, out))
        return out

    def render(self) -> str:
        return ";".join(self.lines)


# --------------------------------------------------------------------------
# video: canvas
# --------------------------------------------------------------------------


def canvas_chain(g: Graph, lin: str, src_wh: Tuple[int, int], out_wh: Tuple[int, int], mode: str,
                 focus: Optional[Dict[str, float]] = None, crop: Optional[Dict[str, float]] = None) -> str:
    """Fit the source onto the output canvas.

    `fit` letterboxes, `fill` crops to the canvas around the focus point (default
    the centre), `blur` fits the picture over a blurred, zoomed copy of itself - the
    standard way to put 16:9 gameplay in a 9:16 frame without bars. An optional
    `crop` rectangle (fractions of the source) is applied first, which is how a
    stream's webcam or chat can be cut out before framing.
    """
    sw, sh = src_wh
    W, H = out_wh
    label = lin
    if crop:
        cw = max(2, int(sw * crop["w"]) // 2 * 2)
        chh = max(2, int(sh * crop["h"]) // 2 * 2)
        label = g.chain(label, "crop={0}:{1}:{2}:{3}".format(
            cw, chh, int(sw * crop["x"]) // 2 * 2, int(sh * crop["y"]) // 2 * 2), "cr")
        sw, sh = cw, chh
    if (sw, sh) == (W, H):
        return g.chain(label, "setsar=1", "cv")

    fx_, fy_ = (focus or {}).get("x", 0.5), (focus or {}).get("y", 0.5)
    if mode == "fit":
        return g.chain(label, "scale={0}:{1}:force_original_aspect_ratio=decrease:flags=lanczos,"
                              "pad={0}:{1}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1".format(W, H), "cv")
    if mode == "fill":
        return g.chain(label, "scale={0}:{1}:force_original_aspect_ratio=increase:flags=lanczos,"
                              "crop={0}:{1}:x={2}:y={3},setsar=1".format(
                                  W, H, q("min(max({0}*iw-ow/2,0),iw-ow)".format(num(fx_))),
                                  q("min(max({0}*ih-oh/2,0),ih-oh)".format(num(fy_)))), "cv")
    if mode == "blur":
        a, b = g.label("bs"), g.label("bs")
        g.add("[{0}]split=2[{1}][{2}]".format(label, a, b))
        # blur at 1/6 size and scale back up: same look, a fraction of the cost
        bg = g.chain(a, "scale={0}:{1}:force_original_aspect_ratio=increase:flags=bilinear,crop={0}:{1},"
                        "scale=iw/6:ih/6,gblur=sigma=6,scale={0}:{1},eq=brightness=-0.08".format(W, H), "bg")
        fg = g.chain(b, "scale={0}:{1}:force_original_aspect_ratio=decrease:flags=lanczos".format(W, H), "fg")
        out = g.label("cv")
        g.add("[{0}][{1}]overlay=(W-w)/2:(H-h)/2,setsar=1[{2}]".format(bg, fg, out))
        return out
    raise editspec.SpecError("unknown framing mode {0!r}".format(mode))


# --------------------------------------------------------------------------
# video: grade
# --------------------------------------------------------------------------

# (shadow lift, contrast, saturation, gamma, brightness, temperature K) per strength.
# The numbers follow the measured presets in github.com/ranahaani/i-hate-editing
# (MIT) - subtle/normal/strong - with `gamer_pop` added: more saturation and bite,
# no white-balance shift, because game footage has its own palette to protect.
_GRADE_TABLE = {
    "subtle": (0.02, 1.04, 1.03, 1.03, 0.02, 5600.0),
    "normal": (0.04, 1.08, 1.08, 1.06, 0.04, 5200.0),
    "strong": (0.06, 1.12, 1.16, 1.10, 0.06, 4400.0),
}


def grade_filters(grade: Dict[str, Any]) -> Optional[str]:
    """The filter chain (no labels) for a grade spec, or None when it does nothing."""
    custom = grade.get("custom")
    if custom:
        parts = ["eq=brightness={0}:contrast={1}:saturation={2}:gamma={3}".format(
            num(custom["brightness"]), num(custom["contrast"]), num(custom["saturation"]), num(custom["gamma"]))]
        if abs(custom["temperature"] - 6500.0) > 50:
            parts.append("colortemperature=temperature={0}".format(num(custom["temperature"])))
        if custom["sharpen"] > 0:
            parts.append("unsharp=5:5:{0}".format(num(custom["sharpen"])))
        return ",".join(parts)
    preset = grade["preset"]
    if preset == "none":
        return None
    lift, contrast, sat, gamma, bright, temp = _GRADE_TABLE[grade["strength"]]
    curves = "curves=all={0}".format(q("0/{0} 0.5/{1} 1/0.99".format(num(lift), num(0.5 + 2 * lift))))
    if preset == "neutral_punch":
        sat = 1 + (sat - 1) * 0.4
        return ",".join([curves, "eq=brightness={0}:contrast={1}:saturation={2}:gamma={3}".format(
            num(bright), num(contrast), num(sat), num(gamma)), "unsharp=5:5:0.3"])
    if preset == "warm_lift":
        return ",".join([curves, "eq=brightness={0}:contrast={1}:saturation={2}:gamma={3}".format(
            num(bright), num(contrast), num(sat), num(gamma)),
            "colortemperature=temperature={0}".format(num(temp)), "unsharp=5:5:0.3"])
    if preset == "cool_clean":
        return ",".join([curves, "eq=brightness={0}:contrast={1}:saturation={2}:gamma={3}".format(
            num(bright), num(contrast), num(sat), num(gamma)),
            "colortemperature=temperature=7200", "unsharp=5:5:0.3"])
    if preset == "gamer_pop":
        sat_g = 1 + (sat - 1) * 1.6
        return ",".join(["eq=brightness={0}:contrast={1}:saturation={2}:gamma={3}".format(
            num(bright * 0.5), num(contrast + 0.02), num(sat_g), num(gamma)), "unsharp=5:5:0.45"])
    raise editspec.SpecError("unknown grade preset {0!r}".format(preset))


def grade_chain(g: Graph, lin: str, grade: Optional[Dict[str, Any]], window: Optional[List[float]]) -> str:
    """Apply a grade to the whole clip, or only inside `window` (output seconds)
    with a smoothstep blend so the change eases in and out instead of popping."""
    if not grade:
        return lin
    body = grade_filters(grade)
    if body is None:
        return lin
    if not window:
        return g.chain(lin, body, "gr")
    t0, t1 = window
    ease = min(float(grade.get("ease", 0.1)), (t1 - t0) / 2.0)
    a, b = g.label("gs"), g.label("gs")
    g.add("[{0}]split=2[{1}][{2}]".format(lin, a, b))
    graded = g.chain(b, body, "gg")
    # weight 0 outside the window, 1 inside, smoothstep over `ease` at each edge
    w = "clip(min((T-{0})/{2},({1}-T)/{2}),0,1)".format(num(t0), num(t1), num(max(ease, 1e-3)))
    smooth = "({0}*{0}*(3-2*{0}))".format(w)
    out = g.label("gw")
    g.add("[{0}][{1}]blend=all_expr={2}[{3}]".format(a, graded, q("A*(1-{0})+B*{0}".format(smooth)), out))
    return out


# --------------------------------------------------------------------------
# video: camera
# --------------------------------------------------------------------------


def camera_chain(g: Graph, lin: str, keys: List[Dict[str, Any]], size: Tuple[int, int]) -> str:
    """Zoom and pan over time, with easing between keys.

    A scale with eval=frame (the zoom) followed by a crop whose x/y follow the pan.
    The crop window is clamped inside the scaled frame, so asking for a pan past the
    edge slides to the edge rather than showing black. `keys` carry `t` (output
    seconds), `zoom`, `x`, `y` (window centre as a frame fraction) and `ease`.
    """
    if not keys:
        return lin
    W, H = size
    ks = sorted(keys, key=lambda k: k["t"])
    zoom = keyed_expr([(k["t"], k["zoom"], k["ease"]) for k in ks])
    cx = keyed_expr([(k["t"], k["x"], k["ease"]) for k in ks])
    cy = keyed_expr([(k["t"], k["y"], k["ease"]) for k in ks])
    if all(abs(k["zoom"] - 1.0) < 1e-6 for k in ks):
        return lin
    return g.chain(
        lin,
        "scale=w={w}:h={h}:eval=frame:flags=bicubic,crop={W}:{H}:x={x}:y={y}".format(
            w=q("trunc({0}*({1})/2)*2".format(W, zoom)), h=q("trunc({0}*({1})/2)*2".format(H, zoom)),
            W=W, H=H,
            x=q("min(max(({0})*iw-ow/2,0),iw-ow)".format(cx)),
            y=q("min(max(({0})*ih-oh/2,0),ih-oh)".format(cy))),
        "cm")


# --------------------------------------------------------------------------
# video: overlays
# --------------------------------------------------------------------------


def overlay_chains(g: Graph, lin: str, items: List[Dict[str, Any]], size: Tuple[int, int]) -> str:
    """Composite rasterised overlays with an animated arrival and exit.

    Each item carries `index` (its ffmpeg input), `png_w`/`png_h`, and the overlay
    fields (`t0`, `t1`, `x`, `y`, `anchor`, `anim_in`, `anim_out`, `in_dur`,
    `out_dur`). The PNG's own timeline is shifted so its t=0 lands at `t0`
    (`setpts=PTS-STARTPTS+T/TB`, the same fix `fxspec` uses for stickers - without it
    you see the middle of the animation, not the start), and every expression below
    speaks that one clock.
    """
    W, H = size
    label = lin
    for it in items:
        t0, t1 = it["t0"], it["t1"]
        ind, outd = it["in_dur"], it["out_dur"]
        # The renderer feeds each PNG as `-loop 1 -framerate <fps> -t <clip length> -i png`
        # so it is a real stream; shift it first so every `t` below is main-timeline time.
        tag = g.label("ov")
        parts = ["[{0}:v]format=rgba".format(it["index"]), "setpts=PTS-STARTPTS+{0}/TB".format(num(t0))]
        if it["anim_in"] == "pop" and ind > 0:
            s = "(0.55+0.45*{0})".format(back_out_expr("(t-{0})/{1}".format(num(t0), num(ind))))
            parts.append("scale=w={0}:h={1}:eval=frame".format(
                q("max(2,trunc(iw*{0}/2)*2)".format(s)), q("max(2,trunc(ih*{0}/2)*2)".format(s))))
        if it["anim_in"] in ("fade", "pop") and ind > 0:
            parts.append("fade=t=in:st={0}:d={1}:alpha=1".format(num(t0), num(min(ind, 0.12) if it["anim_in"] == "pop" else ind)))
        if it["anim_out"] in ("fade", "slide") and outd > 0:
            parts.append("fade=t=out:st={0}:d={1}:alpha=1".format(num(max(t0, t1 - outd)), num(outd)))
        g.add("{0}[{1}]".format(",".join(parts), tag))

        # position: anchor point (x, y as canvas fractions) minus the overlay's own offset
        ax = {"center": "overlay_w/2", "left": "0", "right": "overlay_w"}[it["anchor"]]
        x0 = "{0}*{1}-{2}".format(W, num(it["x"]), ax)
        y0 = "{0}*{1}-overlay_h/2".format(H, num(it["y"]))
        slide = 0.12
        if it["anim_in"] in ("slide_left", "slide_right", "slide_up", "slide_down") and ind > 0:
            p_in = ease_expr("out3", "(t-{0})/{1}".format(num(t0), num(ind)))
            off = "(1-{0})".format(p_in)
            if it["anim_in"] == "slide_left":
                x0 = "({0})-{1}*{2}*W".format(x0, num(slide), off)
            elif it["anim_in"] == "slide_right":
                x0 = "({0})+{1}*{2}*W".format(x0, num(slide), off)
            elif it["anim_in"] == "slide_up":
                y0 = "({0})+{1}*{2}*H".format(y0, num(slide), off)
            else:
                y0 = "({0})-{1}*{2}*H".format(y0, num(slide), off)
        if it["anim_out"] == "slide" and outd > 0:
            p_out = ease_expr("in3", "(t-{0})/{1}".format(num(max(t0, t1 - outd)), num(outd)))
            x0 = "({0})-{1}*{2}*W".format(x0, num(slide), p_out)
        out = g.label("ovo")
        g.add("[{0}][{1}]overlay=x={2}:y={3}:enable={4}:format=auto[{5}]".format(
            label, tag, q(x0), q(y0), q("between(t,{0},{1})".format(num(t0), num(t1))), out))
        label = out
    return label


def flash_chain(g: Graph, lin: str, t0: float, dur: float, amount: float, color: str = "#FFFFFF") -> str:
    """A decaying exposure flash at t0 (used by the `flash` transition and `flash` fx)."""
    lift = "if(between(t,{0},{1}),{2}*(1-(t-{0})/{3}),0)".format(
        num(t0), num(t0 + dur), num(amount), num(max(dur, 1e-3)))
    # eq's brightness takes an expression only with eval=frame (see fxspec).
    return g.chain(lin, "eq=brightness={0}:eval=frame".format(q(lift)), "fl")


def edge_fades(g: Graph, lin: str, head: Optional[Dict[str, Any]], tail: Optional[Dict[str, Any]], dur: float) -> str:
    """Dip-to-colour fades at a clip's edges (the `dip` transition) and the
    brightness flash of the `flash` transition on the incoming clip."""
    label = lin
    if head:
        if head["type"] == "dip":
            h = head["dur"] / 2.0
            label = g.chain(label, "fade=t=in:st=0:d={0}:color={1}".format(num(h), "0x" + head["color"][1:]), "fi")
        elif head["type"] == "flash":
            label = flash_chain(g, label, 0.0, head["dur"], 0.7, head.get("color", "#FFFFFF"))
    if tail and tail["type"] == "dip":
        h = tail["dur"] / 2.0
        label = g.chain(label, "fade=t=out:st={0}:d={1}:color={2}".format(
            num(max(0.0, dur - h)), num(h), "0x" + tail["color"][1:]), "fo")
    return label


# --------------------------------------------------------------------------
# audio
# --------------------------------------------------------------------------


def atempo_chain(rate: float) -> str:
    """atempo takes 0.5..2.0 per instance; chain it for anything outside that."""
    parts = []
    r = rate
    while r < 0.5 - 1e-9:
        parts.append("atempo=0.5")
        r /= 0.5
    while r > 2.0 + 1e-9:
        parts.append("atempo=2.0")
        r /= 2.0
    parts.append("atempo={0}".format(num(r)))
    return ",".join(parts)


def audio_piece(rate: float, mode: str, sample_rate: int, gain_db: float, slow_threshold: float = 0.95) -> str:
    """The audio filters for one retiming piece of rate `rate`.

    `mode` says what the game audio does while the picture is slowed: `tempo`
    (pitch-correct, the default), `pitch` (drops with the speed - the classic
    slow-motion rumble) or `mute`.
    """
    base = "aresample={0},aformat=sample_fmts=fltp:channel_layouts=stereo".format(sample_rate)
    vol = ",volume={0}dB".format(num(gain_db)) if abs(gain_db) > 0.01 else ""
    if abs(rate - 1.0) < 1e-6:
        return base + vol
    if rate < slow_threshold and mode == "mute":
        return base + ",volume=0"
    if rate < slow_threshold and mode == "pitch":
        # asetrate re-labels the sample rate (pitch and speed move together); resample back
        return "{0},asetrate={1},aresample={2}{3}".format(base, int(sample_rate * rate), sample_rate, vol)
    return "{0},{1}{2}".format(base, atempo_chain(rate), vol)


def sfx_mix_chain(g: Graph, lin: str, sfx: List[Dict[str, Any]], sample_rate: int) -> str:
    """Mix the clip's own sound effects (`fx` type sfx) into its audio.

    amix with normalize=0 is load-bearing, exactly as in `fxspec.build_audio_chains`:
    left at its default, amix divides every input by the input count and silently
    drops the game audio by several dB the moment an effect is added.
    """
    if not sfx:
        return lin
    labels = [lin]
    for e in sfx:
        tag = g.label("sx")
        g.add("[{0}:a]aresample={1},aformat=channel_layouts=stereo,atrim=0:{2},asetpts=PTS-STARTPTS,"
              "volume={3}dB,adelay={4}:all=1[{5}]".format(
                  e["index"], sample_rate, num(e["take"]), num(e["gain_db"]), int(round(e["at"] * 1000)), tag))
        labels.append(tag)
    out = g.label("am")
    g.add("{0}amix=inputs={1}:duration=first:dropout_transition=0:normalize=0[{2}]".format(
        "".join("[{0}]".format(x) for x in labels), len(labels), out))
    return out


def audio_edges(g: Graph, lin: str, dur: float, fade_ms: float, head_fade: float, tail_fade: float,
                sample_rate: int) -> str:
    """Fade the audio in and out (30 ms by default - the cure for the pop at every
    hard cut), apply any dip fade, and force the exact length the video has."""
    f = max(fade_ms / 1000.0, 0.0)
    parts: List[str] = []
    fi = max(f, head_fade)
    fo = max(f, tail_fade)
    if fi > 0:
        parts.append("afade=t=in:st=0:d={0}".format(num(fi)))
    if fo > 0:
        parts.append("afade=t=out:st={0}:d={1}".format(num(max(0.0, dur - fo)), num(fo)))
    parts.append("apad=whole_dur={0}".format(num(dur)))
    parts.append("atrim=end={0}".format(num(dur)))
    parts.append("asetpts=PTS-STARTPTS")
    parts.append("aformat=sample_rates={0}:sample_fmts=s16:channel_layouts=stereo".format(sample_rate))
    return g.chain(lin, ",".join(parts), "ae")


# --------------------------------------------------------------------------
# assembly
# --------------------------------------------------------------------------


def groups(clips: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """Split the clip list wherever the incoming transition overlaps (xfade).

    Clips joined by cuts, dips or flashes just follow each other; an xfade needs
    its neighbours to overlap in time, so it is where one group ends and the next
    begins. Inside a group the join is a plain concat.
    """
    out: List[List[Dict[str, Any]]] = []
    for c in clips:
        if out and c["transition_in"]["type"] == "xfade" and c["overlap_in"] > 0:
            out.append([c])
        elif out:
            out[-1].append(c)
        else:
            out.append([c])
    return out


def needs_filter_assembly(clips: List[Dict[str, Any]]) -> bool:
    return len(groups(clips)) > 1


def video_assembly(clips: List[Dict[str, Any]], fps: float) -> Tuple[str, str]:
    """Graph joining the clips' video-only files (input i = clip i). Returns
    (graph, output label). xfade offsets are the next group's timeline start, since
    the overlap begins where the incoming clip does. The result is trimmed to the
    exact frame count the timeline promised: an xfade chain can come out a frame long
    and a frame of picture is enough to leave the sound ahead of it."""
    g = Graph()
    idx = {c["id"]: i for i, c in enumerate(clips)}
    node_labels: List[str] = []
    for grp in groups(clips):
        if len(grp) == 1:
            node_labels.append("{0}:v".format(idx[grp[0]["id"]]))
        else:
            ins = "".join("[{0}:v]".format(idx[c["id"]]) for c in grp)
            out = g.label("vg")
            g.add("{0}concat=n={1}:v=1:a=0[{2}]".format(ins, len(grp), out))
            node_labels.append(out)
    # xfade insists its two inputs share a timebase; a file input arrives in its own
    # (1/15360), a concat output in AVTB. Normalising every node removes the question.
    node_labels = [g.chain(n, "fps={0},settb=AVTB,setpts=PTS-STARTPTS".format(num(fps)), "nb") for n in node_labels]
    cur = node_labels[0]
    for grp, nxt in zip(groups(clips)[1:], node_labels[1:]):
        first = grp[0]
        out = g.label("xf")
        g.add("[{0}][{1}]xfade=transition={2}:duration={3}:offset={4}[{5}]".format(
            cur, nxt, first["transition_in"].get("name", "fade"), num(first["overlap_in"]), num(first["start"]), out))
        cur = out
    total_frames = int(round(clips[-1]["end"] * fps))
    cur = g.chain(cur, "trim=end_frame={0},setpts=PTS-STARTPTS".format(total_frames), "vt")
    return g.render(), cur


def audio_assembly(clips: List[Dict[str, Any]]) -> Tuple[str, str]:
    """The same arrangement for the clips' wav files: concat inside a group,
    acrossfade across an xfade boundary (equal-power, so the join has no dip)."""
    g = Graph()
    idx = {c["id"]: i for i, c in enumerate(clips)}
    node_labels: List[str] = []
    for grp in groups(clips):
        if len(grp) == 1:
            node_labels.append("{0}:a".format(idx[grp[0]["id"]]))
        else:
            ins = "".join("[{0}:a]".format(idx[c["id"]]) for c in grp)
            out = g.label("ag")
            g.add("{0}concat=n={1}:v=0:a=1[{2}]".format(ins, len(grp), out))
            node_labels.append(out)
    cur = node_labels[0]
    for grp, nxt in zip(groups(clips)[1:], node_labels[1:]):
        out = g.label("ax")
        g.add("[{0}][{1}]acrossfade=d={2}:c1=qsin:c2=qsin[{3}]".format(cur, nxt, num(grp[0]["overlap_in"]), out))
        cur = out
    return g.render(), cur


# --------------------------------------------------------------------------
# music, sfx and mastering
# --------------------------------------------------------------------------


def mix_graph(music: List[Dict[str, Any]], sfx: List[Dict[str, Any]], total: float, sample_rate: int) -> Tuple[str, str]:
    """The full soundtrack mix: dry audio (input 0) + music beds + sound effects.

    `music` items carry `index` (input), `t0`, `t1`, `gain_db`, `fade_in`,
    `fade_out`, `offset`, `duck` (a `fxspec.DEFAULT_DUCK`-shaped dict); `sfx` items
    carry `index`, `start` (seconds on the timeline, already peak-aligned),
    `take`, `gain_db`. Beds are sidechain-ducked by the dry audio so they recover in
    the gaps instead of just sitting low; amix keeps normalize=0 (see
    `sfx_mix_chain`).
    """
    g = Graph()
    labels: List[str] = []
    ducked = [m for m in music if m["duck"]["enabled"]]
    if ducked:
        keys = [g.label("key") for _ in ducked]
        g.add("[0:a]asplit={0}[dry]{1}".format(len(ducked) + 1, "".join("[{0}]".format(k) for k in keys)))
        labels.append("dry")
        key_iter = iter(keys)
    else:
        labels.append("0:a")
        key_iter = iter([])

    for m in music:
        ln = max(0.01, m["t1"] - m["t0"])
        tag = g.label("mu")
        chain = ["aresample={0}".format(sample_rate), "aformat=channel_layouts=stereo",
                 "atrim=start={0}:end={1}".format(num(m["offset"]), num(m["offset"] + ln)),
                 "asetpts=PTS-STARTPTS", "volume={0}dB".format(num(m["gain_db"]))]
        if m["fade_in"] > 0:
            chain.append("afade=t=in:st=0:d={0}".format(num(min(m["fade_in"], ln / 2))))
        if m["fade_out"] > 0:
            chain.append("afade=t=out:st={0}:d={1}".format(num(max(0.0, ln - m["fade_out"])), num(min(m["fade_out"], ln / 2))))
        g.add("[{0}:a]{1}[{2}]".format(m["index"], ",".join(chain), tag))
        if m["duck"]["enabled"]:
            d = m["duck"]
            key = next(key_iter)
            dtag = g.label("md")
            g.add("[{0}][{1}]sidechaincompress=threshold={2}:ratio={3}:attack={4}:release={5}:level_sc=1[{6}]".format(
                tag, key, num(d["threshold"]), num(d["ratio"]), num(d["attack"]), num(d["release"]), dtag))
            tag = dtag
        # place the bed on the timeline
        placed = g.chain(tag, "adelay={0}:all=1".format(int(round(m["t0"] * 1000))), "mp")
        labels.append(placed)

    for s in sfx:
        tag = g.label("sx")
        skip = s.get("skip", 0.0)  # a sound whose peak sits before t=0 is trimmed from its head
        g.add("[{0}:a]aresample={1},aformat=channel_layouts=stereo,atrim=start={2}:end={3},asetpts=PTS-STARTPTS,"
              "volume={4}dB,adelay={5}:all=1[{6}]".format(
                  s["index"], sample_rate, num(skip), num(skip + s["take"]), num(s["gain_db"]),
                  int(round(s["start"] * 1000)), tag))
        labels.append(tag)

    if len(labels) == 1:
        out = g.chain(labels[0], "anull", "mix")
    else:
        out = g.label("mix")
        g.add("{0}amix=inputs={1}:duration=first:dropout_transition=0:normalize=0[{2}]".format(
            "".join("[{0}]".format(x) for x in labels), len(labels), out))
    return g.render(), out


def loudnorm_analysis(master: Dict[str, Any]) -> str:
    """Pass 1: measure only (prints JSON on stderr)."""
    return "loudnorm=I={0}:TP={1}:LRA={2}:print_format=json".format(
        num(master["loudness_lufs"]), num(master["true_peak_db"]), num(master["lra"]))


LIMITER_MARGIN_DB = 0.7


def master_filters(master: Dict[str, Any], measured: Optional[Dict[str, float]], total: float) -> str:
    """Pass 2: apply the measured gain linearly (no pumping), then catch strays.

    `linear=true` with the pass-1 numbers is a straight gain change, so the mix keeps
    its dynamics; without measurements loudnorm falls back to a dynamic mode that
    can pump on a track with big swings - exactly this material.
    """
    parts: List[str] = []
    if master["fade_in"] > 0:
        parts.append("afade=t=in:st=0:d={0}".format(num(master["fade_in"])))
    if master["normalize"]:
        if measured:
            parts.append(
                "loudnorm=I={I}:TP={TP}:LRA={LRA}:measured_I={mi}:measured_LRA={ml}:measured_TP={mt}"
                ":measured_thresh={mth}:offset={off}:linear=true".format(
                    I=num(master["loudness_lufs"]), TP=num(master["true_peak_db"]), LRA=num(master["lra"]),
                    mi=num(measured["input_i"]), ml=num(measured["input_lra"]), mt=num(measured["input_tp"]),
                    mth=num(measured["input_thresh"]), off=num(measured["target_offset"])))
        else:
            parts.append("loudnorm=I={0}:TP={1}:LRA={2}".format(
                num(master["loudness_lufs"]), num(master["true_peak_db"]), num(master["lra"])))
    if master["limiter"]:
        # The limiter sees sample peaks; true peak (4x oversampled) and the AAC encode that
        # follows each add a few tenths of a dB. A 0.7 dB margin keeps the delivered file
        # under the ceiling instead of 0.5 dB over it.
        limit = 10 ** ((master["true_peak_db"] - LIMITER_MARGIN_DB) / 20.0)
        parts.append("alimiter=limit={0}:level=disabled".format(num(min(0.99, limit))))
    if master["fade_out"] > 0 and total > master["fade_out"]:
        parts.append("afade=t=out:st={0}:d={1}".format(num(total - master["fade_out"]), num(master["fade_out"])))
    return ",".join(parts) if parts else "anull"


# --------------------------------------------------------------------------
# one clip, end to end
# --------------------------------------------------------------------------


def retime_video(g: Graph, pieces: List[Dict[str, Any]], base: int, fps: float, interp: str) -> str:
    """The retimed picture (input `base + k` per piece, video only).

    A `freeze` piece takes the first frame of its input and holds it. A `play` piece
    is `setpts=(PTS-STARTPTS)/rate`, optionally frame-interpolated when slowed.
    One concat joins them; every later stage then works in output time.
    """
    labels: List[str] = []
    for k, p in enumerate(pieces):
        i = base + k
        if p["kind"] == "freeze":
            labels.append(g.chain("{0}:v".format(i), "trim=end_frame=1,setpts=PTS-STARTPTS,tpad=stop_mode=clone:stop_duration={0}".format(
                num(max(0.0, p["out_dur"] - 1.0 / fps))), "fz"))
            continue
        r = p["rate"]
        body = "setpts=(PTS-STARTPTS)/{0}".format(num(r)) if abs(r - 1.0) > 1e-9 else "setpts=PTS-STARTPTS"
        if r < 0.9 and interp != "dup":
            if interp == "mci":
                body += ",minterpolate=fps={0}:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1".format(num(fps))
            else:
                body += ",minterpolate=fps={0}:mi_mode=blend".format(num(fps))
        labels.append(g.chain("{0}:v".format(i), body, "pv"))
    if len(labels) == 1:
        return labels[0]
    out = g.label("rv")
    g.add("{0}concat=n={1}:v=1:a=0[{2}]".format("".join("[{0}]".format(x) for x in labels), len(labels), out))
    return out


# atempo drops about 25 ms at the tail of every instance (filter latency), and concat
# then fills the gap with silence - a dip at every piece boundary. So the source is
# trimmed a little long and the result cut back to the exact planned length.
ATEMPO_LATENCY_PAD = 0.07


def audio_groups(pieces: List[Dict[str, Any]], mode: str) -> List[Dict[str, Any]]:
    """Group a clip's pieces for the audio path.

    Picture needs the fine 0.1 s steps of a ramp; sound does not, and a pitch-
    preserving atempo per 0.1 s would be 30 stitches. In `tempo` mode a run of
    slowed pieces becomes ONE group played at its average rate (it starts and ends
    in step with the picture, which is all that matters); in `pitch` mode every
    piece is its own exact resample; `mute` and freezes are silence.
    """
    groups: List[Dict[str, Any]] = []
    for p in pieces:
        if p["kind"] == "freeze":
            groups.append({"kind": "silence", "dur": p["out_dur"]})
        elif abs(p["rate"] - 1.0) < 1e-9:
            groups.append({"kind": "plain", "a": p["s0"], "b": p["s1"], "dur": p["out_dur"]})
        elif mode == "mute":
            groups.append({"kind": "silence", "dur": p["out_dur"]})
        elif mode == "pitch":
            groups.append({"kind": "pitch", "a": p["s0"], "b": p["s1"], "rate": p["rate"], "dur": p["out_dur"]})
        elif groups and groups[-1]["kind"] == "ramp":
            groups[-1]["b"] = p["s1"]
            groups[-1]["dur"] += p["out_dur"]
        else:
            groups.append({"kind": "ramp", "a": p["s0"], "b": p["s1"], "dur": p["out_dur"]})
    return groups


def retime_audio(g: Graph, pieces: List[Dict[str, Any]], audio_input: Optional[int], in_start: float, mode: str,
                 sample_rate: int, gain_db: float) -> str:
    """The retimed sound. `audio_input` is ONE input spanning the clip's source
    window (audio is small, so splitting it is free - unlike picture); times are
    relative to it. Returns a label of exactly the planned length of every piece."""
    groups = audio_groups(pieces, mode)
    base = "aresample={0},aformat=sample_fmts=fltp:channel_layouts=stereo".format(sample_rate)
    vol = ",volume={0}dB".format(num(gain_db)) if abs(gain_db) > 0.01 else ""

    def silence(d: float) -> str:
        lab = g.label("as")
        g.add("aevalsrc=0:d={0}:s={1}:c=stereo[{2}]".format(num(d), sample_rate, lab))
        return lab

    if audio_input is None:
        labels = [silence(gr["dur"]) for gr in groups]
    else:
        need = [gr for gr in groups if gr["kind"] != "silence"]
        src = g.chain("{0}:a".format(audio_input), base + vol, "ai")
        if len(need) > 1:
            outs = [g.label("ag") for _ in need]
            g.add("[{0}]asplit={1}{2}".format(src, len(need), "".join("[{0}]".format(o) for o in outs)))
            taps = iter(outs)
        else:
            taps = iter([src])
        labels = []
        for gr in groups:
            if gr["kind"] == "silence":
                labels.append(silence(gr["dur"]))
                continue
            tap = next(taps)
            a, b = gr["a"] - in_start, gr["b"] - in_start
            D = gr["dur"]
            if gr["kind"] == "plain":
                f = "atrim=start={0}:end={1},asetpts=PTS-STARTPTS".format(num(a), num(b))
            elif gr["kind"] == "pitch":
                r = gr["rate"]
                f = "atrim=start={0}:end={1},asetpts=PTS-STARTPTS,asetrate={2},aresample={3}".format(
                    num(a), num(b), int(round(sample_rate * r)), sample_rate)
            else:  # ramp, pitch-preserving
                rate = max(0.05, (b - a) / D)
                f = "atrim=start={0}:end={1},asetpts=PTS-STARTPTS,{2}".format(
                    num(a), num(b + ATEMPO_LATENCY_PAD), atempo_chain(rate))
            f += ",apad=whole_dur={0},atrim=end={0},asetpts=PTS-STARTPTS".format(num(D))
            labels.append(g.chain(tap, f, "ap"))
    if len(labels) == 1:
        return labels[0]
    out = g.label("ra")
    g.add("{0}concat=n={1}:v=0:a=1[{2}]".format("".join("[{0}]".format(x) for x in labels), len(labels), out))
    return out


def build_clip_graph(
    clip: Dict[str, Any],
    entry: Dict[str, Any],
    out: Dict[str, Any],
    src: Optional[Dict[str, Any]],
    head: Optional[Dict[str, Any]] = None,
    tail: Optional[Dict[str, Any]] = None,
    overlay_pngs: Optional[List[Dict[str, Any]]] = None,
    assets: Optional[Dict[str, Dict[str, Any]]] = None,
    styles: Optional[Dict[str, Dict[str, Any]]] = None,
    fallback_font: Optional[str] = None,
) -> Dict[str, Any]:
    """Everything ffmpeg needs to render one clip to a video-only mp4 and a wav.

    `clip` is the normalized spec clip, `entry` its `edittimeline` entry, `out` the
    spec's output block. `src` describes the segment file the pieces seek into:
    {"path", "in_rel" (where the clip's `in` sits in that file), "w", "h", "has_audio"}
    - None for a card. `head`/`tail` are the incoming transition of this clip and of
    the next one (only `dip` and `flash` act inside the clip; xfade is the
    assembler's). `overlay_pngs` is parallel to `entry["overlays"]`.

    Returns {"inputs": [[argv...]...], "filter", "vmap", "amap", "frames", "warnings"}.
    """
    from . import fxspec  # local: fxspec is pure but keeps this module's import graph flat

    W, H = out["width"], out["height"]
    fps, sr = float(out["fps"]), int(out["sample_rate"])
    dur, frames = entry["dur"], entry["frames"]
    g = Graph()
    inputs: List[List[str]] = []
    warnings: List[str] = []
    a_opts = clip["audio"]

    # ---- sources -> retimed picture and sound (labels vl, al)
    if clip["type"] == "card":
        inputs.append(["-f", "lavfi", "-t", num(dur), "-i", "color=c=0x{0}:s={1}x{2}:r={3}".format(
            clip["bg"][1:], W, H, num(fps))])
        inputs.append(["-f", "lavfi", "-t", num(dur), "-i", "anullsrc=r={0}:cl=stereo".format(sr)])
        vl, al = "0:v", "1:a"
    else:
        pieces = entry["pieces"]
        # -an: picture pieces never demux or decode the sound they are not using
        for p in pieces:
            if p["kind"] == "freeze":
                inputs.append(["-an", "-ss", num(src["in_rel"] + (p["s0"] - clip["in"])), "-t", "0.2", "-i", src["path"]])
            else:
                inputs.append(["-an", "-ss", num(src["in_rel"] + (p["s0"] - clip["in"])),
                               "-t", num(p["s1"] - p["s0"]), "-i", src["path"]])
        vl = retime_video(g, pieces, 0, fps, clip.get("interp", "dup"))
        a_in = None
        if src.get("has_audio", True):
            a_in = len(inputs)
            inputs.append(["-vn", "-ss", num(src["in_rel"]), "-t", num((clip["out"] - clip["in"]) + 0.3), "-i", src["path"]])
        al = retime_audio(g, pieces, a_in, clip["in"], a_opts["slowmo"], sr, a_opts["gain_db"])

    # ---- picture
    vl = g.chain(vl, "fps={0},setpts=PTS-STARTPTS".format(num(fps)), "fp")
    if clip["type"] == "clip":
        framing = clip.get("framing") or {}
        vl = canvas_chain(g, vl, (src["w"], src["h"]), (W, H), framing.get("mode", out["fit"]),
                          framing.get("focus"), framing.get("crop"))
        vl = grade_chain(g, vl, clip.get("grade"), entry.get("grade_window"))
        vl = camera_chain(g, vl, entry.get("camera") or [], (W, H))

    plan = None
    if clip["type"] == "clip" and entry.get("fx"):
        plan = fxspec.resolve_fx(entry["fx"], 0.0, dur, (W, H), assets=assets or {}, styles=styles or {},
                                 next_input=len(inputs), strict=True, fallback_font=fallback_font)
    if plan:
        warnings.extend(plan["warnings"])
        for spec_in in plan["inputs"]:
            inputs.append(spec_in["args"])
        chains, vl = fxspec.build_camera_chains(plan, vl, W, H)
        for line in chains:
            g.add(line)

    # overlays (rasterised PNGs)
    items = []
    for o, png in zip(entry.get("overlays") or [], overlay_pngs or []):
        item = dict(o)
        item["index"] = len(inputs)
        item["png_w"], item["png_h"] = png["w"], png["h"]
        inputs.append(["-loop", "1", "-framerate", num(fps), "-t", num(dur), "-i", png["path"]])
        items.append(item)
    vl = overlay_chains(g, vl, items, (W, H))

    if plan:
        chains, vl = fxspec.build_overlay_chains(plan, vl, W, H)
        for line in chains:
            g.add(line)
        chains, vl = fxspec.build_tail_chains(plan, vl)
        for line in chains:
            g.add(line)

    vl = edge_fades(g, vl, head if head and head["type"] in ("dip", "flash") else None,
                    tail if tail and tail["type"] == "dip" else None, dur)
    # exactly `frames` frames, whatever rounding the retime left behind
    vout = g.label("vout")
    g.add("[{0}]tpad=stop_mode=clone:stop_duration=1,trim=end_frame={1},setpts=PTS-STARTPTS,"
          "format=yuv420p,setsar=1[{2}]".format(vl, frames, vout))

    # ---- sound
    if a_opts["mute"]:
        al = g.chain(al, "volume=0", "mu")
    if plan:
        sfx = [e for e in plan["audio"] if e["type"] == "sfx"]
        al = sfx_mix_chain(g, al, sfx, sr)
    head_fade = head["dur"] / 2.0 if head and head["type"] == "dip" else 0.0
    tail_fade = tail["dur"] / 2.0 if tail and tail["type"] == "dip" else 0.0
    aout = audio_edges(g, al, dur, a_opts["fade_ms"], head_fade, tail_fade, sr)

    return {"inputs": inputs, "filter": g.render(), "vmap": "[{0}]".format(vout), "amap": "[{0}]".format(aout),
            "frames": frames, "warnings": warnings}
