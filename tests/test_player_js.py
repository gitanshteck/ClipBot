"""Runs the Node-based tests for clipbot/server/static/player.js.

The embedded-YouTube adapter carries several behaviours that were measured
against the real player rather than assumed (see the header of player.js), so
they are worth pinning. There is no browser test infrastructure in this repo, so
tests/js/player_adapter.test.js is a dependency-free Node script driving the
adapter against a fake YT.Player; this wrapper makes it part of
`python -m unittest discover -s tests`. Skipped when Node isn't installed.
"""

import shutil
import subprocess
import unittest
from pathlib import Path

NODE = shutil.which("node")
SCRIPT = Path(__file__).parent / "js" / "player_adapter.test.js"


@unittest.skipUnless(NODE, "node is not installed")
class TestPlayerAdapterInNode(unittest.TestCase):
    def test_player_adapter(self):
        proc = subprocess.run(
            [NODE, str(SCRIPT)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, "\n" + proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
