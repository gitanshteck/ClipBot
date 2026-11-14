"""Current Steam player counts for the games the brief may recommend.

Reads work/_brief/library.json (from scan_library.py) plus EXTRA below, finds
each game's Steam app id (from its manifest, else the Steam store search,
cached), asks Steam's public GetNumberOfCurrentPlayers endpoint and writes
work/_brief/steam_ccu.json. No API key; only app ids and game names are sent.

Run from the repo root:  python tools/stream-brief/steam_ccu.py
"""
import json
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from clipbot.brief import brief_dir  # noqa: E402
from clipbot.config import load_settings  # noqa: E402

DATA = brief_dir(load_settings())
IDS = DATA / "appid_cache.json"
# Games he plays outside Steam that also sell on Steam: the Steam count is a
# popularity signal, not where he plays. Valorant and League are not on Steam.
EXTRA = {
    "Tom Clancy's Rainbow Six Siege": "359550",
    "PUBG: BATTLEGROUNDS": "578080",
    "Counter-Strike 2": "730",
    "Marvel Rivals": "2767030",
    "THE FINALS": "2073850",
}
NOT_ON_STEAM = ("VALORANT", "League of Legends", "Skribbl.io")


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return json.loads(urllib.request.urlopen(req, timeout=20).read())


def find_appid(name, cache):
    if name in cache:
        return cache[name]
    aid = None
    try:
        q = urllib.parse.quote(name)
        items = get(f"https://store.steampowered.com/api/storesearch/?term={q}&cc=au&l=english").get("items", [])
        norm = lambda s: "".join(ch for ch in s.lower() if ch.isalnum())
        for it in items[:3]:
            if norm(it["name"]).startswith(norm(name)[:12]):
                aid = str(it["id"])
                break
        time.sleep(0.4)
    except Exception:
        pass
    cache[name] = aid
    return aid


def main():
    lib = json.loads((DATA / "library.json").read_text(encoding="utf-8"))
    cache = json.loads(IDS.read_text(encoding="utf-8")) if IDS.exists() else {}
    targets = {}
    for g in lib["installed"] + lib["owned_not_installed"]:
        if g["game"] in NOT_ON_STEAM:
            continue
        aid = g.get("appid") or find_appid(g["game"], cache)
        if aid:
            targets[g["game"]] = aid
    targets.update(EXTRA)
    out = {}
    for name, aid in targets.items():
        try:
            r = get(f"https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/?appid={aid}")["response"]
            out[name] = {"appid": aid, "players": r.get("player_count") if r.get("result") == 1 else None}
        except Exception:
            out[name] = {"appid": aid, "players": None}
        time.sleep(0.2)
    IDS.write_text(json.dumps(cache, indent=1, ensure_ascii=False), encoding="utf-8")
    doc = {"fetched_at": datetime.now().isoformat(timespec="minutes"),
           "source": "Steam Web API GetNumberOfCurrentPlayers: players on Steam right now (Steam only)",
           "games": out}
    (DATA / "steam_ccu.json").write_text(json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
    for k, v in sorted(out.items(), key=lambda kv: -(kv[1]["players"] or 0)):
        print(f"{v['players'] if v['players'] is not None else '-':>8}  {k}")


if __name__ == "__main__":
    main()
