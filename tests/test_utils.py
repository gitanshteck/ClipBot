"""Tests for clipbot/utils.py's resolve_tool()."""

import sys
import unittest
from pathlib import Path
from unittest import mock

from clipbot import utils
from clipbot.utils import ToolMissingError, resolve_tool


class TestResolveTool(unittest.TestCase):
    def test_an_existing_file_is_returned(self):
        self.assertEqual(resolve_tool(sys.executable), str(Path(sys.executable)))

    def test_a_bare_name_goes_through_path(self):
        with mock.patch.object(utils.shutil, "which", return_value="/usr/bin/tool") as which:
            self.assertEqual(resolve_tool("tool"), "/usr/bin/tool")
        which.assert_called_once_with("tool")

    def test_missing_tool_raises_with_the_hint(self):
        with self.assertRaises(ToolMissingError) as ctx:
            resolve_tool("definitely-not-a-real-binary-xyz", "install it like so")
        self.assertIn("definitely-not-a-real-binary-xyz", str(ctx.exception))
        self.assertIn("install it like so", str(ctx.exception))

    def test_a_value_that_cannot_be_a_path_is_not_found_rather_than_a_crash(self):
        # A stray control character (or quote) makes Path.is_file() raise
        # OSError (WinError 123) on Windows: it used to escape as a traceback,
        # or a 500 from the dashboard's Doctor, instead of "not found".
        for bad in ("C:\\tools\ttab\\yt-dlp.exe", "C:\\tools\\ba\x00d\\yt-dlp.exe", '"unbalanced'):
            with self.subTest(command=bad):
                with self.assertRaises(ToolMissingError):
                    resolve_tool(bad, "hint")

    def test_oserror_from_the_filesystem_is_treated_as_not_found(self):
        with mock.patch.object(Path, "is_file", side_effect=OSError(123, "bad path")):
            with mock.patch.object(utils.shutil, "which", return_value=None):
                with self.assertRaises(ToolMissingError):
                    resolve_tool("whatever")

    def test_wrapping_quotes_are_stripped(self):
        # `setx CLIPBOT_YT_DLP "C:\path\yt-dlp.exe"` can leave the quotes in the value.
        self.assertEqual(resolve_tool('"' + sys.executable + '"'), str(Path(sys.executable)))

    def test_surrounding_whitespace_is_ignored(self):
        self.assertEqual(resolve_tool("  " + sys.executable + "\n"), str(Path(sys.executable)))

    def test_an_empty_value_is_not_found(self):
        with self.assertRaises(ToolMissingError):
            resolve_tool("")
        with self.assertRaises(ToolMissingError):
            resolve_tool("   ")


if __name__ == "__main__":
    unittest.main()
