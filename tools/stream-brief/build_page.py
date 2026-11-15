"""Build the stream brief page for the claude.ai artifact.

Reads <brief dir>/brief.json (written each day, see DAILY.md) plus
steam_ccu.json if present, checks the brief has everything the page renders,
and writes <brief dir>/site/index.html: one self-contained file with the shared
scene code and the data inlined (clipbot/brief.py does the work; the dashboard
renders the same page live at /brief). A copy of the brief goes to
<brief dir>/archive/brief-<date>.json.

Run from the repo root:  python tools/stream-brief/build_page.py
Exit code 1 (and no page written) if the brief is incomplete.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from clipbot import brief as brief_module  # noqa: E402
from clipbot.config import load_settings  # noqa: E402


def main():
    settings = load_settings()
    data = brief_module.brief_dir(settings)
    brief = json.loads((data / "brief.json").read_text(encoding="utf-8"))
    issues = brief_module.problems(brief)
    if issues:
        print("brief.json is not ready:")
        for issue in issues:
            print("  " + issue)
        sys.exit(1)
    ccu_path = data / "steam_ccu.json"
    ccu = json.loads(ccu_path.read_text(encoding="utf-8")) if ccu_path.exists() else None
    brief = brief_module.merge_steam(brief, ccu)
    base = "http://{0}:{1}".format(settings.get("server.host", "127.0.0.1"), settings.get("server.port", 8765))
    html = brief_module.render_page(brief, "claude", base)
    (data / "site").mkdir(parents=True, exist_ok=True)
    (data / "archive").mkdir(parents=True, exist_ok=True)
    out = data / "site" / "index.html"
    out.write_text(html, encoding="utf-8")
    (data / "archive" / "brief-{0}.json".format(brief["date"])).write_text(
        json.dumps(brief, ensure_ascii=False, indent=1), encoding="utf-8")
    print("built {0} for {1} (edition {2})".format(out, brief["date"], brief["edition"]))


if __name__ == "__main__":
    main()
