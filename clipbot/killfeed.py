"""Read the in-game kill feed: turn OCR'd feed rows into one player's kill events.

Parsing and classification only - no ffmpeg, no OCR engine, no filesystem (apart
from reading a crop when measuring an icon); `stages/killfeed.py` extracts the
frames and runs the OCR and hands the words here.

What the feed looks like (Wardogs, measured on this channel's streams)
----------------------------------------------------------------------
The feed is a list of rows at the left of the screen:

    [dot] killer   [weapon icon] [headshot skull] [N m]   victim [dot]

*Only rows that involve the local player carry the `[N m]` distance tag.* That is
what makes the feed a reliable record of one player's kills: a row whose FIRST
name is the player is a kill, and the player's name after the tag is a death.
Everything else the feed shows (other people's kills) is ignored.

Reading it is a noisy job, and the module is built around the three ways it goes
wrong, each of which cost a wrong number before it was handled:

* **Brackets are read as digits.** `[10 m]` comes back as `110m`. A single frame is
  not to be trusted: a kill is a *run* of observations of the same row over several
  frames, and its distance is the **mode** of the run.
* **The name is read badly, the distance is not (or vice versa).** So a row counts
  when its first word is the player's name (fuzzy) AND either a distance tag or a
  plausible victim name follows; a row with the name in the *killer position*
  (`x <= LEFT_MARGIN`, never further right) is a kill - a name at the right of the
  row is a death.
* **Two kills close together look like one.** Runs are split when the weapon icon
  class changes (a grenade and a rifle kill seconds apart are two events).

Icon classes come from the *shape* of the white weapon icon (width, height, fill),
not from recognising a particular gun: `gun` (a wide silhouette), `grenade` (a
lone squarish icon with a medium fill), plus the modifier icons `headshot` (a solid
skull) and `cover` (a thinner icon: shot through a surface).
"""

import collections
import difflib
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# Words further right than this (in crop pixels at the 3x working scale) are not in
# the killer position: the feed starts at the left margin, with at most a status dot
# before the first name.
LEFT_MARGIN = 260

# rows whose centre is above this are the "who is talking" label at the top of the
# crop, not feed rows
TOP_LABEL_Y = 110

_DIST = re.compile(r"(\d{1,4}(?:[.,]\d)?)\s?(km|m)(?:[\]\)\}JjlI|!1]|\b|$)", re.I)


# --------------------------------------------------------------------------
# name matching
# --------------------------------------------------------------------------


def norm(word: str) -> str:
    return re.sub(r"[^a-z0-9]", "", word.lower())


def _edit1(a: str, b: str) -> bool:
    """True when a and b differ by at most one substitution, insertion or deletion."""
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(1 for x, y in zip(a, b) if x != y) <= 1
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    return any(long_[:i] + long_[i + 1:] == short for i in range(len(long_)))


def make_matcher(player: str) -> Callable[[str], Optional[str]]:
    """A function word -> 'strong' | None for a player's name.

    strong: within one edit of the name - a substituted, inserted or lost letter
            (Teck ~ Teek, Tcck, Tick, and eck / Tec, how a thin capital T or a
            trailing k dissolves into the background).
    A name shorter than 4 letters only ever matches exactly: one edit on three
    letters matches half the alphabet.
    """
    target = norm(player)
    if len(target) < 2:
        raise ValueError("the player name is too short to match: {0!r}".format(player))

    def classify(word: str) -> Optional[str]:
        w = norm(word)
        if not w:
            return None
        if w == target:
            return "strong"
        if len(target) < 4:
            return None
        if _edit1(w, target):
            return "strong"
        return None

    return classify


# --------------------------------------------------------------------------
# rows
# --------------------------------------------------------------------------


def rows_of(words: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Group OCR words into visual rows by vertical centre; words sorted left to right.

    `words` are {t, x, y, w, h}. Rows are {cy, w: [...]}.
    """
    ordered = sorted(words, key=lambda w: w["y"] + w["h"] / 2.0)
    rows: List[Dict[str, Any]] = []
    for w in ordered:
        cy = w["y"] + w["h"] / 2.0
        if rows and abs(cy - rows[-1]["cy"]) <= max(14, 0.55 * w["h"]):
            rows[-1]["w"].append(w)
            rows[-1]["cy"] = (rows[-1]["cy"] + cy) / 2.0
        else:
            rows.append({"cy": cy, "w": [w]})
    for r in rows:
        r["w"].sort(key=lambda w: w["x"])
    return rows


def find_distance(words: List[Dict[str, Any]]) -> Optional[Tuple[int, int, float]]:
    """(first index, last index, metres) of the first `[N m]` tag in a row's words.

    Tolerant of what OCR does to the brackets: `[48mJ`, `(48m)`, `[55` + `m]`.
    """
    for i in range(len(words)):
        for span in (1, 2):
            if i + span > len(words):
                continue
            txt = " ".join(w["t"] for w in words[i:i + span])
            m = _DIST.search(txt)
            if not m:
                continue
            bracketed = any(ch in txt for ch in "[](){}|")
            plain = span == 1 and re.fullmatch(r"\W{0,2}\d{1,4}\s?(km|m)\W{0,2}", txt, re.I)
            if bracketed or plain:
                val = float(m.group(1).replace(",", "."))
                return i, i + span - 1, val * (1000.0 if m.group(2).lower() == "km" else 1.0)
    return None


def clean_victim(words: List[Dict[str, Any]]) -> str:
    parts = [w["t"] for w in words if len(re.sub(r"[^A-Za-z0-9À-鿿]", "", w["t"])) >= 2]
    return " ".join(parts).strip(" •-.'\"`")


def readable_name(raw: str) -> str:
    """The victim's name when the OCR looks sane, else ''. Many names are Chinese or
    symbols and come out as noise; showing noise on screen is worse than showing
    nothing."""
    raw = (raw or "").strip()
    ok = re.sub(r"[^A-Za-z0-9 _\-\.\[\]\(\)]", "", raw)
    balanced = raw.count("[") == raw.count("]") and raw.count("(") == raw.count(")")
    letters = len(re.findall(r"[A-Za-z]{2,}", raw))
    digits = len(re.findall(r"\d", raw))
    if balanced and len(raw) >= 3 and len(ok) / max(1, len(raw)) >= 0.9 and (letters >= 1 or digits >= 6) \
            and not re.search(r"\d[a-z]\d|[a-z]\d[a-z]", raw):
        return raw
    return ""


# --------------------------------------------------------------------------
# observations -> events
# --------------------------------------------------------------------------


def observe(frame_words: List[Dict[str, Any]], t: float, frame: str, match: Callable[[str], Optional[str]],
            bare: bool = False) -> List[Dict[str, Any]]:
    """The player's kill rows in one frame: [{t, frame, dist, victim, strong, kx, dx, cy, h}].

    A row is a kill observation when its first real word is the player's name in the
    killer position, and it either carries a distance tag or is followed by a name.
    `bare` drops that second condition: the MW4 feed has no distance tag and draws the
    victim's name in red, which this OCR cannot read, so the name in the killer position
    is all there is (the popup, `popup_runs`, is what says a kill happened).
    """
    out = []
    for row in rows_of(frame_words):
        if row["cy"] < TOP_LABEL_Y:
            continue
        ws = row["w"]
        named = [i for i, w in enumerate(ws) if len(norm(w["t"])) >= 2]
        if not named:
            continue
        first = ws[named[0]]
        kind = match(first["t"])
        if not kind or first["x"] > LEFT_MARGIN:
            continue
        rest = ws[named[0] + 1:]
        found = find_distance(rest)
        if found:
            i0, i1, metres = found
            victims = rest[i1 + 1:]
            dx = rest[i0]["x"]
        else:
            metres, victims = None, rest
            dx = rest[0]["x"] if rest else first["x"] + first["w"] + 100
        vname = clean_victim(victims)
        if not found and len(vname) < 3 and not bare:
            continue
        if any(match(w["t"]) == "strong" for w in victims):  # the player after the tag: a death
            continue
        out.append({"t": t, "frame": frame, "dist": metres, "victim": vname, "strong": kind == "strong",
                    "kx": first["x"] + first["w"], "dx": dx, "cy": row["cy"], "h": first["h"], "vlen": len(vname)})
    return out


def group_runs(obs: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """Chain observations of one row across frames.

    Observations within 3 s of each other (8 s when both carry a distance) belong to
    the same row unless their distances disagree - a row's distance is read as the
    same number frame after frame, a different row's is not.
    """
    runs: List[List[Dict[str, Any]]] = []
    for o in sorted(obs, key=lambda x: x["t"]):
        for r in reversed(runs[-8:]):
            last = r[-1]
            both = o["dist"] is not None and last["dist"] is not None
            if o["t"] - last["t"] > (8 if both else 4) or o["t"] - r[0]["t"] > 14:
                continue
            known = [round(x["dist"]) for x in r if x["dist"] is not None]
            if both and abs(o["dist"] - last["dist"]) > 1.0:
                continue
            if o["dist"] is not None and len(known) >= 2 and abs(o["dist"] - collections.Counter(known).most_common(1)[0][0]) > 1.0:
                continue
            r.append(o)
            break
        else:
            runs.append([o])
    return runs


# --------------------------------------------------------------------------
# icons
# --------------------------------------------------------------------------


def icon_blobs(img, thresh: int = 185) -> List[Tuple[int, int, int, int, int]]:
    """White blobs in a crop, left to right: (x0, x1, y0, y1, white pixel count).

    The weapon icon, then any modifier icons, are separated by gaps of at least 7
    empty columns; digits and brackets of the distance tag merge into wide blobs
    that never look like an icon. `thresh` is how bright the weakest channel must
    be: MW4's icons are softer than Wardogs' and are read at 150.
    """
    import numpy as np

    a = np.asarray(img.convert("RGB")).astype(int)
    white = (a.min(axis=2) > thresh) & ((a.max(axis=2) - a.min(axis=2)) < 40)
    cols = white.any(axis=0)
    blobs: List[Tuple[int, int]] = []
    start = None
    gap = 0
    end = 0
    for x, on in enumerate(cols):
        if on:
            if start is None:
                start = x
            gap = 0
            end = x
        elif start is not None:
            gap += 1
            if gap >= 7:
                blobs.append((start, end))
                start = None
    if start is not None:
        blobs.append((start, end))
    out = []
    for x0, x1 in blobs:
        if x1 - x0 < 6:
            continue
        ys = np.where(white[:, x0:x1 + 1].any(axis=1))[0]
        out.append((int(x0), int(x1), int(ys.min()), int(ys.max()), int(white[:, x0:x1 + 1].sum())))
    return out


ICON_MIN_HEIGHT = 36   # px at 3x: modifier icons (skull, through-cover) span the full row; digits do not


def icon_extent(blobs: List[Tuple[int, int, int, int, int]], margin: int = 6) -> Optional[int]:
    """Right edge (crop pixels) of the weapon icon and any modifier icons after it,
    excluding the distance tag's digits and brackets: those are blobs too, but short.
    None when there are no blobs at all."""
    if not blobs:
        return None
    right = blobs[0][1]
    for x0, x1, y0, y1, _area in blobs[1:]:
        if y1 - y0 >= ICON_MIN_HEIGHT:
            right = max(right, x1)
    return right + margin


def classify_icons(blobs: List[Tuple[int, int, int, int, int]]) -> Tuple[str, bool, bool]:
    """(weapon, headshot, through_cover) from the measured blobs.

    weapon: gun | grenade | other | unknown. Specks under 24 px tall are ignored.
    """
    blobs = [b for b in blobs if (b[3] - b[2]) >= 24]
    weapon, headshot, cover = "unknown", False, False
    if not blobs:
        return weapon, headshot, cover
    w0, h0 = blobs[0][1] - blobs[0][0], blobs[0][3] - blobs[0][2]
    fill0 = blobs[0][4] / float(max(1, w0 * h0))
    if w0 >= 55 and w0 / float(max(1, h0)) >= 2.0:
        weapon = "gun"
    elif 0.7 <= w0 / float(max(1, h0)) <= 1.5 and 0.35 <= fill0 < 0.5:
        weapon = "grenade"
    else:
        weapon = "other"
    for b in blobs[1:]:
        w, h = b[1] - b[0], b[3] - b[2]
        fill = b[4] / float(max(1, w * h))
        if 36 <= w <= 48 and h >= 38:  # modifier icons are ~43 px tall; text digits are not
            if fill >= 0.45:
                headshot = True
            else:
                cover = True
    return weapon, headshot, cover


def modal_distance(run: List[Dict[str, Any]]) -> Optional[float]:
    known = [round(o["dist"]) for o in run if o["dist"] is not None]
    if not known:
        return None
    return float(collections.Counter(known).most_common(1)[0][0])


def drop_prefixed_twins(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """`110` sitting next to a better-supported `10` within 12 s is the same kill:
    the bracket was read as a leading 1."""
    for e in events:
        d = e["dist_m"]
        if d is None:
            continue
        s = str(int(d))
        if len(s) >= 2 and s[0] == "1" and s[1:].isdigit():
            twin = int(s[1:])
            for o in events:
                if o is not e and o["dist_m"] is not None and abs(o["first"] - e["first"]) <= 12 \
                        and abs(o["dist_m"] - twin) <= 1 and o["n_obs"] >= e["n_obs"]:
                    e["drop"] = True
    return [e for e in events if not e.get("drop")]


# --------------------------------------------------------------------------
# the "KILL CONFIRMED" popup
# --------------------------------------------------------------------------
#
# The feed is not always readable: a stream layout can put the webcam over it, and then the
# feed reader finds nothing (the Kick layout of 11 and 15 September did). But the game also
# draws a popup at the crosshair on every kill the player makes - `+$2,750  KILL CONFIRMED
# 250XP`, centre of the screen, a few seconds - and no stream layout covers the centre. It
# carries no distance, weapon or victim, so it is the *record* of a kill and the feed is its
# *detail*; `merge_confirms` joins the two.

CONFIRM_GAP = 3.5            # popup sightings this close are one popup (it flickers: OCR reads it on 3 frames in 4)
CONFIRM_MATCH = 2.5          # a feed row this close to a popup's span is the same kill
_AMOUNT = re.compile(r"\+?\s*\$\s*([0-9][0-9,.\sOoSs]{2,8})")
_XP = re.compile(r"([0-9]{2,4})\s*X\s*P", re.I)


def _letters(word: str) -> str:
    return re.sub(r"[^A-Z]", "", word.upper())


def _like(token: str, target: str, floor: float) -> bool:
    return len(token) >= max(3, len(target) - 3) and difflib.SequenceMatcher(None, token, target).ratio() >= floor


def parse_confirm(words: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The popup in one frame's OCR words, or None. `CONFIRMED` (misread as `ONFJRMEO`, `CONFIRMEO`)
    must be there, and with it `KILL` or an `NNN XP` tag, so a shop that says "Confirm purchase $500" is not a kill."""
    toks = [_letters(w["t"]) for w in words]
    confirmed = any(_like(t, "CONFIRMED", 0.68) for t in toks) or any(_like(t, "KILLCONFIRMED", 0.75) for t in toks)
    if not confirmed:
        return None
    text = " ".join(w["t"] for w in words)
    kill = any(_like(t, "KILL", 0.7) for t in toks)
    m = _AMOUNT.search(text)
    amount = None
    if m:
        digits = re.sub(r"[^0-9]", "", m.group(1).replace("O", "0").replace("o", "0").replace("S", "5").replace("s", "5"))
        amount = int(digits) if digits else None
    xp = _XP.search(text)
    if not (kill or xp):
        return None
    return {"amount": amount, "xp": int(xp.group(1)) if xp else None}


def group_confirms(obs: List[Dict[str, Any]], gap: float = CONFIRM_GAP) -> List[Dict[str, Any]]:
    """Sightings `{t, amount, xp}` -> one run per popup: `{first, last, n_obs, amount, xp}`."""
    runs: List[Dict[str, Any]] = []
    for o in sorted(obs, key=lambda x: x["t"]):
        if runs and o["t"] - runs[-1]["last"] <= gap:
            r = runs[-1]
            r["last"] = o["t"]
            r["n_obs"] += 1
            r["_amounts"].append(o.get("amount"))
            r["_xps"].append(o.get("xp"))
        else:
            runs.append({"first": o["t"], "last": o["t"], "n_obs": 1, "_amounts": [o.get("amount")], "_xps": [o.get("xp")]})
    for r in runs:
        for key, out in (("_amounts", "amount"), ("_xps", "xp")):
            known = [v for v in r.pop(key) if v is not None]
            r[out] = collections.Counter(known).most_common(1)[0][0] if known else None
    return runs


def merge_confirms(events: List[Dict[str, Any]], runs: List[Dict[str, Any]], fps: float,
                   match: float = CONFIRM_MATCH, prefer_popup_time: bool = False) -> List[Dict[str, Any]]:
    """Join feed events with popup runs.

    A feed event inside (or within `match` s of) a run is `confirmed` and `source: "both"`; a feed
    event with no popup stays `source: "feed"`. A run no feed event falls in is a kill the feed did
    not show (a webcam over it): a new event, `source: "confirm"`, with no distance, weapon or
    victim - `weapon: "unknown"`, `dist_m: None` - and an id `c001`... Returns every event, by time.
    `prefer_popup_time` moves a confirmed event's `t` to the popup's first sighting (`fps` is then the
    popup's sampling rate): the MW4 popup is sampled three times as often as its feed, so it is the
    better clock.
    """
    out: List[Dict[str, Any]] = []
    used = set()
    for e in events:
        if prefer_popup_time:
            # several kills can sit on one popup stack, so take the run nearest in time, a free one first
            near = sorted((i for i, r in enumerate(runs) if r["first"] - match <= e["first"] <= r["last"] + match),
                          key=lambda i: (i in used, abs(runs[i]["first"] - e["first"])))
            hit = near[0] if near else None
        else:
            hit = next((i for i, r in enumerate(runs) if r["first"] - match <= e["first"] <= r["last"] + match), None)
        e = dict(e, source="feed", confirmed=False)
        if hit is not None:
            used.add(hit)
            e.update(source="both", confirmed=True, amount=runs[hit]["amount"])
            if prefer_popup_time:
                e["t"] = round(max(0.0, runs[hit]["first"] - 0.5 / fps), 2)
        out.append(e)
    n = 0
    for i, r in enumerate(runs):
        if i in used:
            continue
        n += 1
        out.append({"id": "c{0:03d}".format(n), "t": round(max(0.0, r["first"] - 0.5 / fps), 2), "first": r["first"], "last": r["last"],
                    "dist_m": None, "n_obs": r["n_obs"], "victim_raw": "", "victim": "", "weapon": "unknown", "headshot": False,
                    "through_cover": False, "strength": "strong" if r["n_obs"] >= 2 else "weak", "blobs": [], "icon": None,
                    "source": "confirm", "confirmed": True, "amount": r["amount"]})
    out.sort(key=lambda e: e["first"])
    return out


# --------------------------------------------------------------------------
# Call of Duty: Modern Warfare 4 (the second game profile)
# --------------------------------------------------------------------------
#
# Measured on the 2026-10-27 Kick stream (1280x720, 60 fps, the game full-screen under the stream's overlay).
#
# * The feed is at the LEFT edge a little below the middle (x 0-0.31, y 0.40-0.66 of the frame), newest row at the
#   bottom: `[killer] [weapon icon] [skull on a headshot] [victim]`. There is NO distance tag. A name's colour says whose
#   it is - the player's own is yellow, a teammate's teal, an enemy's red - and red on this footage is below what
#   Windows OCR can read, so a victim's name is mostly blank. The player's name in the killer position is readable
#   about two frames in five, and a row stays up for ~4 s.
# * Every kill the player makes adds a line to a stack at the crosshair, `100 XP <skull>KILL`: the line that carries a
#   SKULL is the kill, and its text is the kind - `KILL`, `MELEE KILL`, `HEADSHOT KILL`. Medal lines stack under it
#   (`ONE SHOT, ONE KILL`, `QUICK SCOPE`, `SURVIVOR`, `REVENGE`...) and scroll up and away over ~3 s, and objective
#   bonuses (`DEFENSE`, `OFFENSE`, `CAPTURE KILL`) sit beside them. The multi-kill banner (`DOUBLE KILL`) comes ~3 s
#   AFTER the kills it counts, alone, with no skull. The small letter-spaced pixel font is not read by Windows OCR
#   (tried raw, thresholded, colour-masked), so lines are found by TEMPLATE MATCHING.
# * The first version matched the word KILL. That is wrong: every medal line contains it (`ONE SHOT, ONE KILL`,
#   `BUZZKILL`, `DOUBLE KILL`), so a banner standing alone was counted as a kill of its own. The skull is the
#   marker: it sits at one fixed column (x 754-769 of 720p) whatever the text, and only a kill line has it.
# * Medal lines, the streak counter (`1ST KILL`) and the death card (`KILLED BY`) are not read here.
# * A kill made as the player dies (a trade) can show no popup at all; its feed row is then the only record.
#
# Why a popup and not the feed: the popup is the player's own kills only, needs no name match, and is anchored to the
# screen centre, which a stream's overlay never covers; the feed is the detail (weapon, headshot) where it can be read,
# and the second record where the popup is missing.

MW4_REF = (1280.0, 720.0)             # the frame the geometry below was measured in
MW4_FEED_CROP = (0.0, 0.40, 0.31, 0.26)   # X, Y, W, H as fractions of the frame
MW4_SKULL_REGION = (752, 296, 24, 76)     # x, y, w, h in MW4_REF pixels: the column the skull is in, over the 4-5 lines
MW4_POPUP_THRESHOLD = 0.7             # NCC of the skull template that counts as a sighting at all
MW4_POPUP_STRONG = 0.8                # ... and the score a SURE skull reaches: the 10 kills of one match all had 4+ frames at
                                      # 0.8 or more (best 0.82-1.0); the six false runs in it never had more than 1 (best 0.73-0.83)
MW4_POPUP_GAP = 0.5                   # frames this close are one popup (a kill line is up for ~2-3 s)
MW4_POPUP_MIN_OBS = 3                 # at 6 fps; the pop-in frame is bigger than the template and does not match
MW4_LINE_SEP = 12                     # px: two skulls closer than this are one (lines are ~18 px apart)
MW4_SKULL_FILE = Path(__file__).resolve().parent / "data" / "mw4_skull_template.png"

_SKULL_TEMPLATE: List[Any] = []


def skull_region_fractions() -> Tuple[float, float, float, float]:
    """MW4_SKULL_REGION as fractions of the frame, so any resolution of the same layout is cropped the same."""
    x, y, w, h = MW4_SKULL_REGION
    return x / MW4_REF[0], y / MW4_REF[1], w / MW4_REF[0], h / MW4_REF[1]


def skull_template():
    """The kill-line skull as a float image (16 x 15): the mean of 110 frames of the 27 Oct stream where it was found."""
    if not _SKULL_TEMPLATE:
        import numpy as np
        from PIL import Image

        with Image.open(MW4_SKULL_FILE) as im:
            _SKULL_TEMPLATE.append(np.asarray(im.convert("L"), dtype=np.float64))
    return _SKULL_TEMPLATE[0]


def ncc_map(window, template):
    """Normalised cross-correlation of `template` at every position inside `window`: an array of
    (window height - template height + 1) x (window width - template width + 1), or None when the window is smaller.

    Normalised means brightness and contrast cancel: a popup fades in and out and sits on any background, and its
    score stays the same. A position with no texture at all scores -1.

    Done with FFTs and box sums rather than a loop over positions, because there are ~58 000 frames to a stream: the
    numerator is the cross-correlation with the zero-mean template (subtracting the window's own mean changes nothing,
    the template sums to zero) and each position's variance comes from running sums.
    """
    import numpy as np

    w = np.asarray(window, dtype=np.float64)
    th, tw = template.shape
    h, wd = w.shape
    if h < th or wd < tw:
        return None
    t0 = template - template.mean()
    tn = float(np.sqrt((t0 * t0).sum()))
    if tn < 1e-9:
        return None
    fh, fw = h + th - 1, wd + tw - 1
    corr = np.fft.irfft2(np.fft.rfft2(w, (fh, fw)) * np.fft.rfft2(t0[::-1, ::-1], (fh, fw)), (fh, fw))
    num = corr[th - 1:h, tw - 1:wd]

    def box(a):
        i = np.pad(a.cumsum(axis=0).cumsum(axis=1), ((1, 0), (1, 0)))
        return i[th:, tw:] - i[:-th, tw:] - i[th:, :-tw] + i[:-th, :-tw]

    n = float(th * tw)
    var = box(w * w) - box(w) ** 2 / n
    den = np.sqrt(np.maximum(var, 0.0)) * tn
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(den > 1e-3, num / den, -1.0)


def ncc_best(window, template) -> float:
    """The best `ncc_map` score anywhere in `window`; -1.0 when the window is too small or has no texture."""
    m = ncc_map(window, template)
    return -1.0 if m is None else float(m.max())


def skull_hits(window, template, threshold: float = MW4_POPUP_THRESHOLD, sep: int = MW4_LINE_SEP) -> List[Tuple[float, int]]:
    """The skulls in one frame of the popup column: [(score, y)], best first, one per line.

    A skull is the best position in its own band of `sep` rows; two peaks closer than that are one skull seen twice.
    """
    import numpy as np

    m = ncc_map(window, template)
    if m is None:
        return []
    band = m.max(axis=1)
    out: List[Tuple[float, int]] = []
    for y in np.argsort(-band):
        if band[y] < threshold:
            break
        if all(abs(int(y) - t) >= sep for _s, t in out):
            out.append((float(band[y]), int(y)))
    return out


MW4_ICON_THRESH = 150


def classify_icons_mw4(blobs: List[Tuple[int, int, int, int, int]]) -> Tuple[str, bool, bool]:
    """(weapon, headshot, through_cover) from the white blobs of an MW4 feed row's icon crop (3x working scale).

    The weapon icon is the first blob: a long, thin silhouette for any gun - sniper, rifle, SMG - which is all this can
    say; anything squarer (a knife, a thrown weapon) is `other`, never `grenade`. No grenade kill has been seen to
    measure one by yet, and a wrong `grenade` would earn a kill the nade slow-motion. A headshot is the skull icon
    after it: a roughly square blob about as tall as the row. Through-cover is not drawn in this game's feed.
    """
    real = [b for b in blobs if (b[1] - b[0]) >= 10 and (b[3] - b[2]) >= 12]
    if not real:
        return "unknown", False, False
    w0, h0 = real[0][1] - real[0][0], real[0][3] - real[0][2]
    weapon = "gun" if w0 >= 36 and w0 / float(max(1, h0)) >= 1.6 else "other"
    head = any(24 <= (b[1] - b[0]) <= 60 and 26 <= (b[3] - b[2]) <= 60 and 0.6 <= (b[1] - b[0]) / float(max(1, b[3] - b[2])) <= 1.5
               for b in real[1:])
    return weapon, head, False


def popup_frames(raw: bytes, width: Optional[int] = None, height: Optional[int] = None,
                 threshold: float = MW4_POPUP_THRESHOLD) -> List[List[Tuple[float, int]]]:
    """The skulls in each frame of raw 8-bit gray frames of the popup column (what ffmpeg writes with `-f rawvideo`)."""
    import numpy as np

    w, h = width or MW4_SKULL_REGION[2], height or MW4_SKULL_REGION[3]
    size = w * h
    tmpl = skull_template()
    frames = np.frombuffer(raw, dtype=np.uint8)[:(len(raw) // size) * size].reshape(-1, h, w)
    return [skull_hits(f, tmpl, threshold) for f in frames]


def popup_runs(frames: List[Tuple[float, List[Tuple[float, int]]]], gap: float = MW4_POPUP_GAP,
               min_obs: int = MW4_POPUP_MIN_OBS, fps: float = 6.0, strong: float = MW4_POPUP_STRONG) -> List[Dict[str, Any]]:
    """(time, skulls) per sampled frame -> one run per kill, in the shape `merge_confirms` takes.

    Frames with at least one skull, no further apart than `gap` s, are one popup. Inside a popup, a skull COUNT that
    rises and stays risen for two frames is another kill landing on the stack (lines are ~18 px apart, so two kills a
    second apart show two skulls at once): each gets its own run, `first` being the frame its count was reached.
    `first`/`last` are that frame and the popup's last, `n_obs` the frames with at least that many skulls, `score` the
    best skull score; `amount` and `xp` are None, the figure is not read.

    `sure` says the run has at least 3 frames whose skull scores `strong` or more. Blank scenery and the objective
    rings match a skull to about 0.75 on a frame or two, so an unsure run is only believed when something else (a feed
    row) agrees; a sure one stands alone.
    """
    seen = sorted(((t, h) for t, h in frames if h), key=lambda x: x[0])
    popups: List[List[Tuple[float, List[Tuple[float, int]]]]] = []
    for t, h in seen:
        if popups and t - popups[-1][-1][0] <= gap + 1.5 / fps:
            popups[-1].append((t, h))
        else:
            popups.append([(t, h)])
    out: List[Dict[str, Any]] = []
    for pop in popups:
        if len(pop) < min_obs:
            continue
        counts = [len(h) for _t, h in pop]
        top = max(counts)
        starts = {}
        for k in range(1, top + 1):
            # the first frame from which the count is at least k for two frames running
            for i in range(len(pop)):
                if counts[i] >= k and (i + 1 < len(pop) and counts[i + 1] >= k or k == 1):
                    starts[k] = i
                    break
        for k, i in sorted(starts.items()):
            n_obs = sum(1 for c in counts[i:] if c >= k)
            if k > 1 and n_obs < 2:
                continue
            # the k-th best skull of each frame is this kill's line (the best ones are the earlier kills')
            kth = [sorted((s for s, _y in h), reverse=True)[k - 1] for _t, h in pop[i:] if len(h) >= k]
            out.append({"first": pop[i][0], "last": pop[-1][0], "n_obs": n_obs, "amount": None, "xp": None,
                        "score": round(max(kth), 3), "sure": sum(1 for s in kth if s >= strong) >= 3})
    out.sort(key=lambda r: r["first"])
    return out


# --------------------------------------------------------------------------
# MW4: what the popup stack SAYS (the text of each line)
# --------------------------------------------------------------------------
#
# The stack's lines are on an 18 px grid anchored by the skull (the skull's y modulo 18 is the same for every line of
# one popup, whether or not that line has a skull), left-aligned at x 770 of 720p, ~10 px tall letters in a pixel font.
# Windows OCR cannot read them (tried line by line, upscaled 3-6x, thresholded, inverted: a few words in ten), so each
# line is matched against labelled examples of itself: the strips of ~240 kills' popups were clustered, the clusters named
# by reading them (the vocabulary below is what the game printed on the 27 Oct 2026 stream, not a list from the game's
# files - a web search found no MW4 medal list, four days after release), and the compact seed file
# `data/mw4_text.npz` holds ~700 labelled strips, including ~140 of non-phrase junk (player names, scenery) so that a
# strip is only believed when a phrase is its nearest neighbour.
#
# A strip is high-passed (the background goes), cut to its ink, left-aligned and compared by cosine similarity, so the
# game's own fade-in and a bright or busy background change little. A phrase that is not in the seed file reads as
# nothing: it is never guessed.

MW4_TEXT_FILE = Path(__file__).resolve().parent / "data" / "mw4_text.npz"
MW4_TEXT_BLOCK = (750, 300, 160, 90)   # x, y, w, h in MW4_REF pixels: the stack and a margin
TEXT_COLS = (20, 152)                  # the text column inside the block
TEXT_SLOT_H = 16
LINE_PITCH = 18
TEXT_VEC = (12, 100)                   # rows x columns of a strip vector
TEXT_MIN_COS = 0.70
TEXT_MIN_MARGIN = 0.03
TEXT_KIND_MIN_COS = 0.45    # the skull's own line is one of three phrases, so it is believed on less ...
TEXT_KIND_MIN_MARGIN = 0.0  # ... and need not beat the others by anything
KIND_PHRASES = frozenset({"KILL", "HEADSHOT KILL", "MELEE KILL"})
# phrases the strips cannot tell apart (their first letters are 'OF' / 'DE' at ~10 px: 85% by the early columns, whatever
# the width): each is no rival to the other, and the label it gets is right about 85% of the time
TEXT_CONFUSABLE = (frozenset({"OFFENSE", "DEFENSE"}),)

# phrase -> what kind of line it is. `kind` lines are the kill itself (the skull line); the rest are what the game added.
MW4_PHRASES: Dict[str, str] = {
    "KILL": "kind", "HEADSHOT KILL": "kind", "MELEE KILL": "kind",
    "DOUBLE KILL": "multi", "TRIPLE KILL": "multi",
    "ONE SHOT, ONE KILL": "skill", "QUICK SCOPE": "skill", "LONGSHOT": "skill", "POINT BLANK": "skill", "WALL BANG": "skill",
    "LOW BLOW": "skill", "BACKFIRE": "skill", "BLOODTHIRSTY": "skill", "GROUNDED": "skill",
    "SURVIVOR": "situation", "REVENGE": "situation", "AVENGER": "situation", "SAVIOR": "situation", "KINGSLAYER": "situation",
    "BUZZKILL": "situation", "SCRAPPED": "situation", "TRADE": "situation",
    "OFFENSE": "objective", "DEFENSE": "objective", "CAPTURE KILL": "objective",
    "ASSIST": "support", "UAV REVEAL ASSIST": "support", "TROPHY SYSTEM INTERCEPT": "support",
}

_TEXT_SEEDS: List[Any] = []


def text_seeds():
    """(vectors float32 n x 1200, labels) of the seed file; label '' is a non-phrase strip."""
    if not _TEXT_SEEDS:
        import numpy as np

        z = np.load(str(MW4_TEXT_FILE), allow_pickle=False)
        x = z["q"].astype(np.float32) * float(z["scale"]) / 127.0
        x = x / np.maximum(np.linalg.norm(x, axis=1, keepdims=True), 1e-9)     # int8 rounding takes a vector off unit length
        _TEXT_SEEDS.extend([x, [str(s) for s in z["labels"]], np.array([str(s) for s in z["labels"]])])
    return _TEXT_SEEDS[0], _TEXT_SEEDS[1]


def strip_vector(strip):
    """A 16 x 132 gray strip of the text column -> a unit vector of its ink, or None when it holds no text.

    High-pass (minus a 4 px blur) so the background goes, ink where that is over 28 levels; the text is the run of ink
    columns (2+ ink pixels) from the left edge - starting within 6 px of it, text is left-aligned - up to the first gap of
    12 empty columns, so the HUD further right is not part of it (a 4-letter `KILL` is ~22 columns, `ONE SHOT, ONE KILL`
    ~95); it must be 10+ columns and 25-700 ink pixels. Then the 12 rows round the densest row and the first 100 columns
    from the first ink column, nothing kept beyond the text, mean-removed and normalised.
    """
    import numpy as np
    from PIL import Image, ImageFilter

    s = np.clip(np.asarray(strip, dtype=np.float64), 0, 255).astype(np.uint8)
    bl = np.asarray(Image.fromarray(s).filter(ImageFilter.GaussianBlur(4)), dtype=np.float64)
    ik = np.clip(s.astype(np.float64) - bl, 0, None)
    mask = ik > 28
    cols = np.where(mask.sum(axis=0) >= 2)[0]
    if len(cols) == 0 or cols.min() > 6:
        return None
    end = len(cols)
    for k in range(1, len(cols)):
        if cols[k] - cols[k - 1] > 12:
            end = k
            break
    cols = cols[:end]
    left, right = int(cols.min()), int(cols.max())
    n = int(mask[:, left:right + 1].sum())
    if right - left + 1 < 10 or n < 25 or n > 700:
        return None
    rows = mask[:, left:right + 1].sum(axis=1)
    c = int(np.argmax(np.convolve(rows, np.ones(5), "same")))
    vh, vw = TEXT_VEC
    r0 = min(max(0, c - vh // 2), TEXT_SLOT_H - vh)
    a = ik[r0:r0 + vh, left:min(right + 1, left + vw)]
    if a.shape[1] < vw:
        a = np.pad(a, ((0, 0), (0, vw - a.shape[1])))
    v = a.reshape(-1)
    v = v - v.mean()
    nv = float(np.linalg.norm(v))
    return None if nv < 1e-6 else v / nv


def read_strip(strip, only=None, exclude=None, min_cos: Optional[float] = None,
               min_margin: Optional[float] = None) -> Tuple[str, float]:
    """(phrase, cosine) for one strip: '' when it is not a phrase we know (no ink, junk, or too unlike any seed).

    A phrase is believed when its best seed scores `min_cos` (`TEXT_MIN_COS`) and beats the best seed of any OTHER phrase
    (or of junk) by `min_margin` (`TEXT_MIN_MARGIN`); seeds of a confusable partner do not count as rivals
    (`TEXT_CONFUSABLE`). Leave-one-kill-out over the labelled strips, that rule read 93% of them with 99.5% of what it
    read right; the first rule (cosine over 0.55 alone) read some medal lines as a different medal.

    `only` / `exclude` take phrases out of the running altogether (their seeds do not exist for this strip): the line
    that carries the skull is one of the three `kind` phrases, and no other line is.
    """
    import numpy as np

    v = strip_vector(strip)
    if v is None:
        return "", 0.0
    min_cos = TEXT_MIN_COS if min_cos is None else min_cos
    min_margin = TEXT_MIN_MARGIN if min_margin is None else min_margin
    x, _labels = text_seeds()
    arr = _TEXT_SEEDS[2]
    s = x @ v.astype("float32")
    if only is not None or exclude is not None:
        ok = np.ones(len(arr), dtype=bool)
        if only is not None:
            ok &= np.isin(arr, list(only)) | (arr == "")
        if exclude is not None:
            ok &= ~np.isin(arr, list(exclude))
        s = np.where(ok, s, -1.0)
    j = int(s.argmax())
    label, cos = str(arr[j]), float(s[j])
    if label == "" or cos < min_cos:
        return "", cos
    group = next((g for g in TEXT_CONFUSABLE if label in g), frozenset({label}))
    rivals = ~np.isin(arr, list(group))
    if cos - float(s[rivals].max()) < min_margin:
        return "", cos
    return label, cos


def text_phase(blocks) -> Optional[int]:
    """The line grid of a popup: the median of its skulls' y modulo the line pitch over frames of the block."""
    import numpy as np

    tm = skull_template()
    ys = [y % LINE_PITCH for g in blocks for _s, y in skull_hits(np.asarray(g)[:, :30], tm, MW4_POPUP_STRONG)]
    return int(np.median(ys)) if ys else None


def read_lines(blocks, t0: float, fps: float, phase: Optional[int] = None, min_frames: int = 2,
               gap: float = 0.5) -> List[Dict[str, Any]]:
    """Every line of text on a popup stack across a run of frames of the block: [{text, first, last, n, kind}].

    `blocks` are 2-D gray arrays of MW4_TEXT_BLOCK, `t0` the time of the first. A phrase is an instance from its first
    frame to its last, frames no more than `gap` s apart (the same line scrolls up through the slots); one seen on fewer
    than `min_frames` frames is dropped as a misread. Two lines of the same phrase more than `gap` apart are two instances.
    """
    import numpy as np

    if phase is None:
        phase = text_phase(blocks)
    if phase is None:
        return []
    seen: Dict[str, List[float]] = {}
    tm = skull_template()
    for i, g in enumerate(blocks):
        g = np.asarray(g, dtype=np.float64)
        t = t0 + i / fps
        skulls = [y for _s, y in skull_hits(g[:, :30], tm, MW4_POPUP_THRESHOLD)]
        for j in range(-1, 6):
            y = phase + LINE_PITCH * j
            if y < 0 or y + TEXT_SLOT_H > g.shape[0]:
                continue
            strip = g[y:y + TEXT_SLOT_H, TEXT_COLS[0]:TEXT_COLS[1]]
            if any(abs(sy - y) <= 4 for sy in skulls):
                text, _c = read_strip(strip, only=KIND_PHRASES, min_cos=TEXT_KIND_MIN_COS, min_margin=TEXT_KIND_MIN_MARGIN)   # the skull's line
            else:
                text, _c = read_strip(strip, exclude=KIND_PHRASES)
            if text:
                seen.setdefault(text, []).append(t)
    out: List[Dict[str, Any]] = []
    for text, ts in seen.items():
        ts = sorted(set(ts))
        run = [ts[0]]
        for t in ts[1:] + [None]:
            if t is not None and t - run[-1] <= gap + 1.0 / fps:
                run.append(t)
                continue
            if len(run) >= min_frames:
                out.append({"text": text, "first": round(run[0], 3), "last": round(run[-1], 3), "n": len(run),
                            "kind": MW4_PHRASES.get(text, "other")})
            run = [t] if t is not None else []
    out.sort(key=lambda r: (r["first"], r["text"]))
    return out


def merge_instances(lines: List[Dict[str, Any]], gap: float = 0.6) -> List[Dict[str, Any]]:
    """One instance per line that was on screen: windows read around neighbouring kills overlap, so the same line of text
    is read more than once; instances of one phrase whose frames are no more than `gap` s apart are the same line."""
    out: List[Dict[str, Any]] = []
    for ln in sorted(lines, key=lambda r: (r["text"], r["first"])):
        if out and out[-1]["text"] == ln["text"] and ln["first"] - out[-1]["last"] <= gap:
            out[-1]["last"] = max(out[-1]["last"], ln["last"])
            out[-1]["n"] = max(out[-1]["n"], ln["n"])
        else:
            out.append(dict(ln))
    out.sort(key=lambda r: (r["first"], r["text"]))
    return out


def attribute_lines(events: List[Dict[str, Any]], lines: List[Dict[str, Any]], kind_window: float = 1.2,
                    after: float = 6.0) -> None:
    """Put every instance of a line on the kill it belongs to, as `event["popup"]`; `events` are changed in place.

    A `kind` line (KILL / HEADSHOT KILL / MELEE KILL) is a kill's own text: it goes to the nearest event within
    `kind_window` s of its first frame. Everything else is something the game added to the kill before it: it goes to the
    latest event that began at or before it (up to `after` s earlier), because medals trail their kill by 0.2-1.5 s and
    the multi-kill banner by ~3 s. The popup is {kind, medals, situations, objective, multi, support, lines}: `kind` None
    when no kind line was read for the kill (a popup the feed row alone vouches for).
    """
    for e in events:
        e["popup"] = {"kind": None, "medals": [], "situations": [], "objective": [], "multi": None, "support": [], "lines": []}
    ordered = sorted(events, key=lambda e: e["t"])
    for ln in sorted(lines, key=lambda r: r["first"]):
        target = None
        if ln["kind"] == "kind":
            near = [e for e in ordered if abs(ln["first"] - e["t"]) <= kind_window]
            if near:
                target = min(near, key=lambda e: abs(ln["first"] - e["t"]))
                if target["popup"]["kind"] is None:
                    target["popup"]["kind"] = ln["text"]
        else:
            before = [e for e in ordered if e["t"] - 0.3 <= ln["first"] <= e["t"] + after]
            if before:
                target = max(before, key=lambda e: e["t"])
                pop = target["popup"]
                if ln["kind"] == "skill" and ln["text"] not in pop["medals"]:
                    pop["medals"].append(ln["text"])
                elif ln["kind"] == "situation" and ln["text"] not in pop["situations"]:
                    pop["situations"].append(ln["text"])
                elif ln["kind"] == "objective" and ln["text"] not in pop["objective"]:
                    pop["objective"].append(ln["text"])
                elif ln["kind"] == "multi":
                    pop["multi"] = ln["text"]
                elif ln["kind"] == "support" and ln["text"] not in pop["support"]:
                    pop["support"].append(ln["text"])
        if target is not None:
            target["popup"]["lines"].append({"text": ln["text"], "first": ln["first"], "n": ln["n"]})
