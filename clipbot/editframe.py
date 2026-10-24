"""Framing that knows where the facecam is: the camera move, adjusted so it never crops it.

A camera key says "zoom Z around (x, y)". Authored with the defaults that is a zoom
around the centre of the frame, which is fine for a full-screen picture and wrong for a
stream: the webcam sits against an edge, so a centred 1.09x zoom pushes its border out of
the frame. Where the box is depends on the scene (`editscene`), and scenes switch.

`safe_camera` takes a clip's keys (output seconds) and its scene spans and returns keys
whose visible window always contains the webcam box, plus a margin: the pan moves the
least it must, and the zoom is capped only if the box would not fit at all. The picture
still zooms (the webcam grows with it, like everything else) - what changes is the pivot,
by a few percent, towards the box. At a second where the box changes (a scene switch) two
keys one frame apart carry the old and the new pan, so the pan jumps with the layout
instead of drifting across it.

Everything here is pure: positions are fractions of the source frame, times are the
clip's output seconds, and `camera_at` evaluates a key list the way
`editfilters.camera_chain` does, so a test can check the invariant on a fine time grid.
"""

from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import editscene, editspec

# Kept clear between the webcam box and the window edge. It also absorbs the pan's
# interpolation between keys (<= ~0.001 for zooms up to 1.1).
MARGIN = 0.008
# The two keys either side of a layout change are this far apart: closer than any frame is long,
# so no frame is drawn between the old pan and the new one.
SWITCH_GAP = 0.001
# `violations` accepts a box this far outside the window (3 px at 1080p). A box with `MARGIN` of
# frame around it is held exactly (a test pins that), but one flush with the source's own edge has
# a margin the frame cannot supply, and between two keys at different zooms the pan - which moves
# linearly while the window's half-width goes as 1/zoom - falls short by up to ~0.0025.
TOLERANCE = 0.003

Rect = Dict[str, float]
Key = Dict[str, Any]


def camera_at(keys: Sequence[Key], t: float) -> Tuple[float, float, float]:
    """(zoom, x, y) of a key list at output second `t`: the first key's before it, the last's
    after it, and between two keys the later key's ease - what `editfilters.keyed_expr` builds."""
    if not keys:
        return 1.0, 0.5, 0.5
    ks = sorted(keys, key=lambda k: k["t"])
    if t <= ks[0]["t"]:
        return ks[0]["zoom"], ks[0]["x"], ks[0]["y"]
    for a, b in zip(ks, ks[1:]):
        if t <= b["t"]:
            p = editspec.ease_value(b["ease"], (t - a["t"]) / max(1e-3, b["t"] - a["t"]))
            return tuple(a[n] + (b[n] - a[n]) * p for n in ("zoom", "x", "y"))  # type: ignore[return-value]
    last = ks[-1]
    return last["zoom"], last["x"], last["y"]


def window(zoom: float, x: float, y: float) -> Tuple[float, float, float, float]:
    """The visible part of the frame, (x0, y0, x1, y1) in frame fractions, for a zoom around
    (x, y): the crop is clamped inside the scaled picture, so it slides to the edge rather than past it."""
    half = 0.5 / max(zoom, 1.0)
    cx = min(max(x, half), 1.0 - half)
    cy = min(max(y, half), 1.0 - half)
    return cx - half, cy - half, cx + half, cy + half


def box_in_window(box: Rect, zoom: float, x: float, y: float, tol: float = 0.0) -> bool:
    x0, y0, x1, y1 = window(zoom, x, y)
    return (box["x"] >= x0 - tol and box["y"] >= y0 - tol
            and box["x"] + box["w"] <= x1 + tol and box["y"] + box["h"] <= y1 + tol)


def box_in_output(box: Rect, zoom: float, x: float, y: float) -> Rect:
    """Where the box lands in the output frame (fractions of it) under a zoom around (x, y)."""
    x0, y0, _, _ = window(zoom, x, y)
    z = max(zoom, 1.0)
    return {"x": (box["x"] - x0) * z, "y": (box["y"] - y0) * z, "w": box["w"] * z, "h": box["h"] * z}


def safe_view(zoom: float, x: float, y: float, box: Optional[Rect], margin: float = MARGIN) -> Tuple[float, float, float]:
    """(zoom, x, y) moved the least that keeps `box` (plus `margin`) in the window.

    The pan is clamped into the range that contains the box; if the box plus its margin is
    wider or taller than the window at this zoom, the zoom is capped to the one at which
    it just fits (never below 1.0 - a box that fills the frame allows no zoom at all).
    """
    if box is None or zoom <= 1.0 + 1e-9:
        return zoom, x, y
    ax0, ax1 = max(0.0, box["x"] - margin), min(1.0, box["x"] + box["w"] + margin)
    ay0, ay1 = max(0.0, box["y"] - margin), min(1.0, box["y"] + box["h"] + margin)
    zoom = max(1.0, min(zoom, 1.0 / max(ax1 - ax0, ay1 - ay0)))
    half = 0.5 / zoom

    def axis(c: float, a0: float, a1: float) -> float:
        lo, hi = max(a1 - half, half), min(a0 + half, 1.0 - half)
        return min(max(c, lo), hi)

    return zoom, axis(x, ax0, ax1), axis(y, ay0, ay1)


def safe_camera(keys: Sequence[Key], spans: Sequence[Dict[str, Any]], fps: float = 60.0,
                margin: float = MARGIN) -> Tuple[List[Key], List[str]]:
    """The clip's camera keys with every pan made safe for the scene at that second.

    Returns (keys, notes). `notes` say what could not be made safe: a zoom capped because
    the box would not fit, or a zoom held through a second where the box is sliding between
    layouts and so not known. Keys with no zoom anywhere, or no spans, come back as given.
    """
    notes: List[str] = []
    ks = sorted((dict(k) for k in keys), key=lambda k: k["t"])
    if not ks or not spans or all(abs(k["zoom"] - 1.0) < 1e-6 for k in ks):
        return ks, notes

    out: List[Key] = []
    for k in ks:
        box = editscene.box_at(spans, k["t"])
        z, x, y = safe_view(k["zoom"], k["x"], k["y"], box, margin)
        if z < k["zoom"] - 1e-6:
            notes.append("zoom {0:.3f} at {1:.1f}s limited to {2:.3f}: the webcam box would not fit the window".format(
                k["zoom"], k["t"], z))
        out.append(dict(k, zoom=round(z, 4), x=round(x, 4), y=round(y, 4)))

    # A key at zoom 1.0 shows the whole frame, so its pan is free - and it is the pan the
    # zoom starts from or returns to. Left at the authored centre, the first frames of a
    # push-in would be cropped around the wrong point; it takes the pan of the nearest key
    # that does zoom, and then the window only ever grows around the box.
    zoomed = [i for i, k in enumerate(out) if k["zoom"] > 1.0 + 1e-6]
    for i, k in enumerate(out):
        if k["zoom"] > 1.0 + 1e-6 or not zoomed:
            continue
        near = min(zoomed, key=lambda j: abs(out[j]["t"] - k["t"]))
        out[i] = dict(k, x=out[near]["x"], y=out[near]["y"])

    # a second where the box changes: the pan must change with it, between two frames
    lo, hi = ks[0]["t"], ks[-1]["t"]
    for a, b in zip(spans, spans[1:]):
        t = b["t0"]
        if not (lo + SWITCH_GAP < t < hi) or editscene.same_box(a["cam"], b["cam"]):
            continue
        z0, x0_, y0_ = camera_at(ks, t - SWITCH_GAP)
        z1, x1_, y1_ = camera_at(ks, t)
        if max(z0, z1) <= 1.0 + 1e-6:
            continue
        for tt, (z, x, y), box in ((t - SWITCH_GAP, (z0, x0_, y0_), a["cam"]), (t, (z1, x1_, y1_), b["cam"])):
            z, x, y = safe_view(z, x, y, box, margin)
            if any(abs(o["t"] - tt) < SWITCH_GAP / 2.0 for o in out):
                continue
            out.append({"t": round(tt, 4), "zoom": round(z, 4), "x": round(x, 4), "y": round(y, 4), "ease": "linear"})
    out.sort(key=lambda k: k["t"])

    for s in spans:
        if s.get("unstable") and any(camera_at(ks, t)[0] > 1.0 + 1e-6 for t in (s["t0"], (s["t0"] + s["t1"]) / 2.0, s["t1"])):
            notes.append("the webcam is moving between layouts at {0:.1f}-{1:.1f}s while the picture is zoomed".format(
                s["t0"], s["t1"]))
    return out, notes


def violations(keys: Sequence[Key], spans: Sequence[Dict[str, Any]], step: float = 1.0 / 120.0,
               tol: float = TOLERANCE) -> List[Dict[str, float]]:
    """Every sampled second at which the webcam box is not fully inside the window. A check, used
    by tests and by `edit verify`'s plan-level view: `safe_camera`'s output must return []."""
    bad: List[Dict[str, float]] = []
    if not keys or not spans:
        return bad
    t = min(k["t"] for k in keys)
    end = max(max(k["t"] for k in keys), spans[-1]["t1"])
    while t <= end + 1e-9:
        box = editscene.box_at(spans, t)
        if box is not None:
            z, x, y = camera_at(keys, t)
            if not box_in_window(box, z, x, y, tol=tol):
                x0, y0, x1, y1 = window(z, x, y)
                over = max(x0 - box["x"], y0 - box["y"], box["x"] + box["w"] - x1, box["y"] + box["h"] - y1)
                bad.append({"t": round(t, 4), "overshoot": round(over, 4)})
        t += step
    return bad


def overlay_collisions(overlays: Sequence[Dict[str, Any]], keys: Sequence[Key], spans: Sequence[Dict[str, Any]],
                       canvas: Tuple[int, int], step: float = 0.25) -> List[Dict[str, Any]]:
    """Overlays (lower-thirds, callouts) that sit on the webcam while they are up.

    Each overlay is `{"label", "t0", "t1", "x", "y", "anchor", "w", "h"}` with `x`,`y` the anchor
    point as canvas fractions and `w`,`h` the rasterised size in canvas pixels (the renderer
    knows them). The webcam's place in the output is its place in the source moved by the
    camera at that second. Returns one entry per overlay that overlaps it: {label, t0, t1}.
    """
    W, H = canvas
    hits: List[Dict[str, Any]] = []
    for o in overlays:
        ox = o["x"] * W - {"center": o["w"] / 2.0, "left": 0.0, "right": float(o["w"])}[o.get("anchor", "center")]
        oy = o["y"] * H - o["h"] / 2.0
        first = last = None
        t = o["t0"]
        while t <= o["t1"] + 1e-9:
            box = editscene.box_at(spans, t)
            if box is not None:
                z, x, y = camera_at(keys, t)
                r = box_in_output(box, z, x, y)
                bx, by, bw, bh = r["x"] * W, r["y"] * H, r["w"] * W, r["h"] * H
                if ox < bx + bw and ox + o["w"] > bx and oy < by + bh and oy + o["h"] > by:
                    first = t if first is None else first
                    last = t
            t += step
        if first is not None:
            hits.append({"label": o.get("label", ""), "t0": round(first, 3), "t1": round(last, 3)})
    return hits


def _overlay_rect(o: Dict[str, Any], canvas: Tuple[int, int]) -> Tuple[float, float, float, float]:
    W, H = canvas
    ox = o["x"] * W - {"center": o["w"] / 2.0, "left": 0.0, "right": float(o["w"])}[o.get("anchor", "center")]
    return ox, o["y"] * H - o["h"] / 2.0, float(o["w"]), float(o["h"])


def clear_of_webcam(o: Dict[str, Any], keys: Sequence[Key], spans: Sequence[Dict[str, Any]], canvas: Tuple[int, int],
                    step: float = 0.25, pad: float = 0.012) -> Optional[Dict[str, float]]:
    """Where to move an overlay so it stays off the webcam for as long as it is up.

    The webcam's footprint is the union of where it lands in the output over the overlay's life
    (the camera moves it), plus `pad` of the frame. The overlay is moved the shortest way that
    clears it and stays inside the frame: sideways before up or down. Returns {"dx", "dy"} in
    canvas pixels, {"dx": 0, "dy": 0} when it already clears, or None when no move fits.
    """
    W, H = canvas
    xs0, ys0, xs1, ys1 = [], [], [], []
    t = o["t0"]
    while True:
        tt = min(t, o["t1"])
        box = editscene.box_at(spans, tt)
        if box is not None:
            z, x, y = camera_at(keys, tt)
            r = box_in_output(box, z, x, y)
            xs0.append(r["x"] * W - pad * W)
            ys0.append(r["y"] * H - pad * H)
            xs1.append((r["x"] + r["w"]) * W + pad * W)
            ys1.append((r["y"] + r["h"]) * H + pad * H)
        if tt >= o["t1"]:
            break
        t += step
    if not xs0:
        return {"dx": 0.0, "dy": 0.0}
    bx0, by0, bx1, by1 = min(xs0), min(ys0), max(xs1), max(ys1)
    ox, oy, w, h = _overlay_rect(o, canvas)
    if not (ox < bx1 and ox + w > bx0 and oy < by1 and oy + h > by0):
        return {"dx": 0.0, "dy": 0.0}
    options = []
    left, right = bx0 - (ox + w), bx1 - ox
    up, down = by0 - (oy + h), by1 - oy
    if ox + left >= 0.015 * W:
        options.append((abs(left), 0, {"dx": left, "dy": 0.0}))
    if ox + right + w <= 0.985 * W:
        options.append((abs(right), 0, {"dx": right, "dy": 0.0}))
    if oy + up >= 0.01 * H:
        options.append((abs(up), 1, {"dx": 0.0, "dy": up}))
    if oy + down + h <= 0.99 * H:
        options.append((abs(down), 1, {"dx": 0.0, "dy": down}))
    if not options:
        return None
    return min(options, key=lambda c: (c[0] + c[1] * W * 0.25, c[1]))[2]

