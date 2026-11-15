"""Runs the Node-based tests for clipbot/server/static/brief/scenes.js.

scenes.js is the stream brief's scene model and on-air state (sound events, vote
tallies, the reducer behind the dock buttons). There is no browser test
infrastructure in this repo, so tests/js/scenes.test.js is a dependency-free Node
script run in a vm context with a stub window/document; this wrapper makes it part
of `python -m unittest discover -s tests`. Skipped when Node isn't installed.
"""

import shutil
import subprocess
import unittest
from pathlib import Path

NODE = shutil.which("node")
SCRIPT = Path(__file__).parent / "js" / "scenes.test.js"


@unittest.skipUnless(NODE, "node is not installed")
class TestBriefScenesInNode(unittest.TestCase):
    def test_scenes(self):
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
