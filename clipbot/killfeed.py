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
import re
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


def observe(frame_words: List[Dict[str, Any]], t: float, frame: str, match: Callable[[str], Optional[str]]
            ) -> List[Dict[str, Any]]:
    """The player's kill rows in one frame: [{t, frame, dist, victim, strong, kx, dx, cy, h}].

    A row is a kill observation when its first real word is the player's name in the
    killer position, and it either carries a distance tag or is followed by a name.
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
        if not found and len(vname) < 3:
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


def icon_blobs(img) -> List[Tuple[int, int, int, int, int]]:
    """White blobs in a crop, left to right: (x0, x1, y0, y1, white pixel count).

    The weapon icon, then any modifier icons, are separated by gaps of at least 7
    empty columns; digits and brackets of the distance tag merge into wide blobs
    that never look like an icon.
    """
    import numpy as np

    a = np.asarray(img.convert("RGB")).astype(int)
    white = (a.min(axis=2) > 185) & ((a.max(axis=2) - a.min(axis=2)) < 40)
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
