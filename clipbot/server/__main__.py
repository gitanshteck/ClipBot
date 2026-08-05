"""Entry point: python -m clipbot.server

Note the UTF-8 reconfiguration. Transcripts are Devanagari and the Windows
console defaults to cp1252, so any log line containing transcript text would
raise UnicodeEncodeError without this.
"""

import sys
import threading
import webbrowser

from ..config import load_settings
from ..utils import setup_logging


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    setup_logging("-v" in sys.argv or "--verbose" in sys.argv)

    try:
        import uvicorn
    except ImportError:
        print(
            "The dashboard needs a few extra packages:\n"
            "    pip install -r requirements-server.txt",
            file=sys.stderr,
        )
        return 1

    settings = load_settings()
    host = str(settings.get("server.host", "127.0.0.1"))
    port = int(settings.get("server.port", 8765))
    url = "http://{0}:{1}/".format(host, port)

    if settings.get("server.open_browser", True) and "--no-browser" not in sys.argv:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    print("ClipBot dashboard: {0}".format(url), file=sys.stderr)

    # reload must stay off: it would kill in-flight jobs, and a transcription
    # run is ~90 minutes.
    uvicorn.run(
        "clipbot.server.app:app",
        host=host,
        port=port,
        reload=False,
        log_level="warning",
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
