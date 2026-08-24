"""Settings loading.

Config lives in config/settings.json so it can be edited without touching pipeline
logic. Any value can be overridden per-run by an environment variable listed in
ENV_OVERRIDES below.
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SETTINGS_FILE = PROJECT_ROOT / "config" / "settings.json"

# env var -> dotted path into the settings dict
ENV_OVERRIDES = {
    "CLIPBOT_WORK_ROOT": "work_root",
    "CLIPBOT_LIBRARY_DIR": "library.dir",
    "CLIPBOT_FFMPEG": "tools.ffmpeg",
    "CLIPBOT_FFPROBE": "tools.ffprobe",
    "CLIPBOT_YT_DLP": "tools.yt_dlp",
    "CLIPBOT_KICK_DL": "tools.kick_dl",
    "CLIPBOT_WHISPER_MODEL": "transcribe.model",
    "CLIPBOT_WHISPER_DEVICE": "transcribe.device",
    "CLIPBOT_WHISPER_LANGUAGE": "transcribe.language",
    "CLIPBOT_TRANSCRIBE_BACKEND": "transcribe.backend",
    "CLIPBOT_CLAUDE_MODEL": "analyze.model",
    "CLIPBOT_RUBRIC_FILE": "analyze.rubric_file",
}


class Settings:
    """Thin wrapper over the settings dict with dotted-path lookup."""

    def __init__(self, data: Dict[str, Any], source: Path):
        self._data = data
        self.source = source

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def require(self, dotted: str) -> Any:
        sentinel = object()
        value = self.get(dotted, sentinel)
        if value is sentinel:
            raise KeyError(
                "Missing setting '{0}' in {1}".format(dotted, self.source)
            )
        return value

    def tool(self, name: str) -> str:
        """Resolve an external binary path (e.g. 'ffmpeg').

        A bare name ('ffmpeg') is left alone for PATH lookup. A *relative path*
        is resolved against the project root rather than the current working
        directory, so pointing at the vendored ffmpeg build keeps working
        whichever directory the CLI or the dashboard happens to be started from.
        """
        value = str(self.require("tools.{0}".format(name)))
        candidate = Path(value)
        if len(candidate.parts) > 1 and not candidate.is_absolute():
            return str(PROJECT_ROOT / candidate)
        return value

    @property
    def work_root(self) -> Path:
        root = Path(str(self.get("work_root", "work")))
        return root if root.is_absolute() else PROJECT_ROOT / root

    def project_path(self, dotted: str) -> Path:
        """Resolve a settings value that names a file relative to the project root."""
        value = Path(str(self.require(dotted)))
        return value if value.is_absolute() else PROJECT_ROOT / value

    def as_dict(self) -> Dict[str, Any]:
        return self._data


def _set_dotted(data: Dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    node = data
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def load_settings(path: Optional[Path] = None) -> Settings:
    path = Path(path) if path else DEFAULT_SETTINGS_FILE
    if not path.exists():
        raise FileNotFoundError("Settings file not found: {0}".format(path))
    # utf-8-sig: Notepad and PowerShell write a BOM that json.load chokes on
    with path.open("r", encoding="utf-8-sig") as fh:
        data = json.load(fh)

    for env_var, dotted in ENV_OVERRIDES.items():
        override = os.environ.get(env_var)
        if override:
            _set_dotted(data, dotted, override)

    return Settings(data, path)
