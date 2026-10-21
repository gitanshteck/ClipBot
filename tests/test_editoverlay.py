"""Tests for clipbot/editoverlay.py: overlay PNG rendering and its cache key."""

import tempfile
import unittest
from pathlib import Path

from clipbot import editoverlay as O

CANVAS = (1920, 1080)


def text(**kw):
    d = {"kind": "text", "text": "HELLO", "size": 0.08, "color": "#FFFFFF", "at": 0.0, "dur": 2.0}
    d.update(kw)
    return d


def callout(**kw):
    d = {"kind": "callout", "text": "headshot", "sub": "17 m  KULLER", "size": 0.05, "color": "#FFFFFF",
         "accent": "#FF3B3B", "plate": True, "at": 0.0, "dur": 2.0}
    d.update(kw)
    return d


class TestRender(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _open(self, path):
        from PIL import Image
        return Image.open(path)

    def test_text_is_a_transparent_png_sized_to_its_content(self):
        w, h = O.render(text(), CANVAS, self.dir / "t.png", font=O.find_font())
        im = self._open(self.dir / "t.png")
        self.assertEqual(im.mode, "RGBA")
        self.assertEqual(im.size, (w, h))
        self.assertLess(w, CANVAS[0])
        self.assertLess(h, CANVAS[1] // 2)
        self.assertEqual(im.getpixel((0, 0))[3], 0)          # the corner is transparent

    def test_bigger_size_is_a_bigger_image(self):
        w1, h1 = O.render(text(size=0.05), CANVAS, self.dir / "a.png", font=O.find_font())
        w2, h2 = O.render(text(size=0.10), CANVAS, self.dir / "b.png", font=O.find_font())
        self.assertGreater(w2, w1 * 1.5)
        self.assertGreater(h2, h1 * 1.5)

    def test_callout_with_sub_line_is_taller_than_without(self):
        _, h1 = O.render(callout(sub=None), CANVAS, self.dir / "a.png", font=O.find_font())
        _, h2 = O.render(callout(), CANVAS, self.dir / "b.png", font=O.find_font())
        self.assertGreater(h2, h1)

    def test_callout_icon_widens_it(self):
        from PIL import Image
        icon = self.dir / "icon.png"
        Image.new("RGBA", (120, 40), (255, 255, 255, 255)).save(icon)
        w1, _ = O.render(callout(), CANVAS, self.dir / "a.png", font=O.find_font())
        w2, _ = O.render(callout(), CANVAS, self.dir / "b.png", font=O.find_font(), icon=str(icon))
        self.assertGreater(w2, w1)

    def test_image_overlay_scales_to_a_fraction_of_the_canvas(self):
        from PIL import Image
        src = self.dir / "logo.png"
        Image.new("RGBA", (200, 100), (255, 0, 0, 255)).save(src)
        w, h = O.render({"kind": "image", "image": str(src), "w": 0.1}, CANVAS, self.dir / "o.png")
        self.assertEqual(w, 192)
        self.assertEqual(h, 96)

    def test_missing_image_is_an_error(self):
        from clipbot.utils import StageError
        with self.assertRaises(StageError):
            O.render({"kind": "image", "image": str(self.dir / "nope.png"), "w": 0.1}, CANVAS, self.dir / "o.png")


class TestFingerprint(unittest.TestCase):
    def test_same_content_same_key(self):
        self.assertEqual(O.fingerprint(text(), CANVAS, None, None), O.fingerprint(text(at=5.0), CANVAS, None, None))

    def test_anything_that_changes_pixels_changes_the_key(self):
        base = O.fingerprint(text(), CANVAS, None, None)
        self.assertNotEqual(base, O.fingerprint(text(text="BYE"), CANVAS, None, None))
        self.assertNotEqual(base, O.fingerprint(text(color="#FF0000"), CANVAS, None, None))
        self.assertNotEqual(base, O.fingerprint(text(), (1080, 1920), None, None))


if __name__ == "__main__":
    unittest.main()
