"""Scan this PC's game library for the daily stream brief.

Writes work/_brief/library.json: installed games across Steam, Epic, Riot,
Ubisoft/EA/GOG (registry), Xbox app folders, plus Steam titles that are owned
and have play history but are not installed.

Run from the repo root:  python tools/stream-brief/scan_library.py
Only Steam app ids are ever sent anywhere (to Steam's own store API, to turn an
id into a name); results are cached in work/_brief/names_cache.json so repeat
runs are offline.
"""
import json
import sys
import re
import string
import subprocess
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from clipbot.brief import brief_dir  # noqa: E402
from clipbot.config import load_settings  # noqa: E402

DATA = brief_dir(load_settings())
STEAM = Path(r"C:\Program Files (x86)\Steam")
CACHE = DATA / "names_cache.json"
SKIP = re.compile(r"redistributable|steamvr|steamworks|soundtrack|dedicated server|sdk|benchmark|"
                  r"precision x1|wallpaper engine|open beta|playtest|controller configs|dota 2 beta", re.I)


def load_cache():
    cache = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}
    seed = DATA / "steam_names_cache_seed.json"
    if seed.exists():
        for row in json.loads(seed.read_text(encoding="utf-8")):
            if row.get("name") not in (None, "?", "ERR"):
                cache.setdefault(row["appid"], row["name"])
    return cache


def steam_name(appid, cache):
    if appid in cache:
        return cache[appid]
    try:
        req = urllib.request.Request(
            f"https://store.steampowered.com/api/appdetails?appids={appid}&filters=basic",
            headers={"User-Agent": "Mozilla/5.0"})
        data = json.loads(urllib.request.urlopen(req, timeout=20).read())
        name = data[appid]["data"]["name"] if data[appid]["success"] else None
        time.sleep(0.4)
    except Exception:
        name = None
    cache[appid] = name
    return name


def library_paths():
    vdf = STEAM / "steamapps" / "libraryfolders.vdf"
    text = vdf.read_text(encoding="utf-8", errors="ignore") if vdf.exists() else ""
    paths = [p.replace("\\\\", "\\") for p in re.findall(r'"path"\s+"([^"]+)"', text)]
    known = {Path(p).resolve() for p in paths}
    # libraries Steam has forgotten (e.g. an old X:\SteamLibrary) can still hold games
    for letter in string.ascii_uppercase:
        cand = Path(f"{letter}:/SteamLibrary")
        try:
            if cand.is_dir() and cand.resolve() not in known:
                paths.append(str(cand))
        except OSError:
            pass
    return paths


def steam_installed():
    out = []
    for lib in library_paths():
        apps = Path(lib) / "steamapps"
        manifests = list(apps.glob("appmanifest_*.acf"))
        names = set()
        for acf in manifests:
            raw = acf.read_bytes()
            try:
                t = raw.decode("utf-8")
            except UnicodeDecodeError:
                t = raw.decode("cp1252", errors="replace")
            aid = re.search(r'"appid"\s+"(\d+)"', t)
            name = re.search(r'"name"\s+"([^"]+)"', t)
            folder = re.search(r'"installdir"\s+"([^"]+)"', t)
            if folder:
                names.add(folder.group(1).lower())
            if aid and name and not SKIP.search(name.group(1)):
                out.append({"game": name.group(1), "store": "Steam", "appid": aid.group(1)})
        common = apps / "common"
        if common.is_dir():   # game folders with no manifest: a forgotten library, or leftovers
            for d in common.iterdir():
                if d.is_dir() and d.name.lower() not in names and not SKIP.search(d.name) and big_folder(d):
                    out.append({"game": d.name, "store": "Steam", "verify": "folder without a Steam manifest"})
    return out


def big_folder(path, limit=2 * 1024 ** 3):
    """True once the folder holds more than `limit` bytes: a real install, not a leftover shader cache."""
    total = 0
    for root, _dirs, files in __import__("os").walk(path):
        for f in files:
            try:
                total += (Path(root) / f).stat().st_size
            except OSError:
                pass
            if total > limit:
                return True
    return False


def steam_history(cache):
    rows = []
    cutoff = datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp()
    for cfg in (STEAM / "userdata").glob("*/config/localconfig.vdf"):
        txt = cfg.read_text(encoding="utf-8", errors="ignore")
        for aid, body in re.findall(r'\n\t{4,6}"(\d+)"\n\t{4,6}\{(.*?)\n\t{4,6}\}', txt, re.S):
            lp = re.search(r'"LastPlayed"\s+"(\d+)"', body)
            pt = re.search(r'"Playtime"\s+"(\d+)"', body)
            if not (lp or pt):
                continue
            last = int(lp.group(1)) if lp else 0
            mins = int(pt.group(1)) if pt else 0
            if mins < 120 and last < cutoff:
                continue
            name = steam_name(aid, cache)
            if not name or SKIP.search(name):
                continue
            rows.append({"appid": aid, "game": name, "hours": round(mins / 60, 1),
                         "last_played": datetime.fromtimestamp(last, timezone.utc).date().isoformat() if last else None})
    return rows


def epic():
    out = []
    for item in Path(r"C:\ProgramData\Epic\EpicGamesLauncher\Data\Manifests").glob("*.item"):
        try:
            d = json.loads(item.read_text(encoding="utf-8"))
            if d.get("DisplayName"):
                out.append({"game": d["DisplayName"], "store": "Epic"})
        except Exception:
            pass
    return out


def registry():
    ps = (r"$k='HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*',"
          r"'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*',"
          r"'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*';"
          r"Get-ItemProperty $k -EA SilentlyContinue | ?{$_.DisplayName -and $_.InstallLocation} |"
          r" select DisplayName,Publisher,InstallLocation | ConvertTo-Json -Compress")
    try:
        raw = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=90).stdout
        rows = json.loads(raw or "[]")
    except Exception:
        return []
    rows = rows if isinstance(rows, list) else [rows]
    stores = {"Ubisoft": "Ubisoft", "Electronic Arts": "EA", "EA Games": "EA", "GOG": "GOG",
              "Riot Games": "Riot", "Rockstar": "Rockstar"}
    out = []
    for r in rows:
        name, pub, loc = r.get("DisplayName") or "", r.get("Publisher") or "", r.get("InstallLocation") or ""
        if re.search(r"launcher|client|connect|anti-cheat|vanguard|\bapp\b|galaxy|social club|"
                     r"prerequisites|online services|ssd|redlauncher|battle\.net", name, re.I):
            continue
        if "steamapps" in loc.lower():
            continue   # covered by the Steam manifests
        store = next((v for k, v in stores.items() if k.lower() in pub.lower() or k.lower() in loc.lower()), None)
        if store or re.search(r"[\\/]games[\\/]", loc, re.I):
            out.append({"game": name, "store": store or "PC"})
    return out


def xbox():
    out = []
    for letter in string.ascii_uppercase:
        for root in (Path(f"{letter}:/XboxGames"), Path(f"{letter}:/Xbox")):
            try:
                if not root.is_dir():
                    continue
                for d in root.iterdir():
                    if d.is_dir() and d.name not in ("GameSave", "Shaders"):
                        out.append({"game": d.name.replace("- ", ": "), "store": "Xbox app"})
            except OSError:
                pass
    return out


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    cache = load_cache()
    seen, installed = set(), []
    for g in steam_installed() + epic() + registry() + xbox():
        key = re.sub(r"[^a-z0-9]", "", g["game"].lower())
        if key and key not in seen:
            seen.add(key)
            installed.append(g)
    history = steam_history(cache)
    by_id = {h["appid"]: h for h in history}
    squash = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())
    for g in installed:
        h = by_id.get(g.get("appid"))
        if not h and not g.get("appid"):   # folder-only install: match play history by name
            h = next((x for x in history if squash(x["game"]).startswith(squash(g["game"])[:8])), None)
            if h:
                g["game"], g["appid"] = h["game"], h["appid"]
        if h:
            g["hours"], g["last_played"] = h["hours"], h["last_played"]
    installed_ids = {g.get("appid") for g in installed}
    owned = [h for h in history if h["appid"] not in installed_ids]
    CACHE.write_text(json.dumps(cache, indent=1, ensure_ascii=False), encoding="utf-8")
    doc = {"scanned_at": datetime.now().isoformat(timespec="minutes"),
           "installed": sorted(installed, key=lambda g: (-(g.get("hours") or 0), g["game"])),
           "owned_not_installed": sorted(owned, key=lambda g: -g["hours"])}
    (DATA / "library.json").write_text(json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"installed: {len(installed)}  owned, not installed (with play history): {len(owned)}")


if __name__ == "__main__":
    main()
