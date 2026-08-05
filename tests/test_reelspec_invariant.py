"""The effects feature must be invisible to a clip that has no effects.

`stages/reel.py` decides whether to re-render by hashing the ffmpeg argv. So if
adding effects perturbed the command for an effects-free clip by even one
character, every reel in every workspace would silently re-render - minutes of
x264 per clip, for no change in the output.

These tests pin that. The golden strings were captured from the code as it
stood immediately before the effects work began; if one of them fails, the
frozen path in reelspec (`_build_filter_legacy`, and the `if not fx_plan`
branch of `build_argv`) has been edited and needs putting back.

Run: python -m unittest discover -s tests
"""

import unittest

from clipbot import reelspec as R


def _spec(preset, chat=False):
    spec = {"preset": preset, "canvas": "1080x1920",
            "src": {"cam": dict(R.DEFAULT_CAM)}}
    if chat:
        spec["chat"] = {"enabled": True, "mode": "panel", "side": "bottom",
                        "size": 0.34}
    return spec


def _cases():
    for preset in sorted(R.PRESETS):
        for chat in (False, True):
            yield preset, chat, R.resolve(_spec(preset, chat), 1280, 720)


# Captured from the pre-effects code. Two representative cases: the default
# stacked preset with no chat (which exercises the fused overlay shortcut) and
# the blur preset with a chat panel (which exercises the pad + chat tail).
GOLDEN_FILTER = {
    "cam_top|nochat": (
        "[0:v]crop=592:720:352:0,scale=1080:1312:force_original_aspect_ratio=increase"
        ":flags=lanczos,crop=1080:1312,setsar=1,pad=1080:1920:0:608:color=black[base];"
        "[0:v]crop=320:180:32:216,scale=1080:608:force_original_aspect_ratio=increase"
        ":flags=lanczos,crop=1080:608,setsar=1[cam];"
        "[base][cam]overlay=x=0:y=0:format=auto,format=yuv420p[v]"
    ),
    "blur_fill|chat": (
        "[0:v]scale=270:316:force_original_aspect_ratio=increase:flags=bilinear,"
        "crop=270:316,gblur=sigma=9.0,eq=brightness=-0.06:saturation=1.25,"
        "scale=1080:1268:flags=bilinear,setsar=1[bg];"
        "[0:v]crop=1280:720:0:0,scale=1080:608:force_original_aspect_ratio=increase"
        ":flags=lanczos,crop=1080:608,setsar=1[fg];"
        "[bg][fg]overlay=x=0:y=330[t0];"
        "[0:v]crop=320:180:32:216,scale=380:210:force_original_aspect_ratio=increase"
        ":flags=lanczos,crop=380:210,setsar=1,pad=388:218:4:4:color=0x19A2D2[cam];"
        "[t0][cam]overlay=x=648:y=88:format=auto[comp];"
        "[comp]pad=1080:1920:0:0:color=0x18181B[stage];"
        "[1:v]setpts=PTS-STARTPTS,format=rgba[chat];"
        "[stage][chat]overlay=x=0:y=1268:eof_action=repeat:repeatlast=1:shortest=0"
        ":format=auto,format=yuv420p[v]"
    ),
}


class TestLegacyPathUnchanged(unittest.TestCase):
    def test_empty_fx_is_the_legacy_path(self):
        for preset, chat, plan in _cases():
            chat_input = 1 if chat else None
            with self.subTest(preset=preset, chat=chat):
                self.assertEqual(
                    R.build_filter(plan, chat_input=chat_input),
                    R.build_filter(plan, chat_input=chat_input, fx_plan=None),
                )
                self.assertEqual(
                    R.build_filter(plan, chat_input=chat_input),
                    R.build_filter(plan, chat_input=chat_input, fx_plan={"video": []}),
                )

    def test_argv_unchanged_without_fx(self):
        for preset, chat, plan in _cases():
            chat_list = "LIST.txt" if chat else None
            with self.subTest(preset=preset, chat=chat):
                base = R.build_argv("ffmpeg", "SRC.mp4", "OUT.mp4", 10.0, 45.0,
                                    plan, chat_list=chat_list)
                self.assertEqual(
                    base,
                    R.build_argv("ffmpeg", "SRC.mp4", "OUT.mp4", 10.0, 45.0, plan,
                                 chat_list=chat_list, fx_plan=None),
                )
                # The effects-free argv must still carry the tolerant audio map,
                # not a filtergraph label - a source with no audio depends on it.
                self.assertIn("0:a:0?", base)

    def test_golden_filter_strings(self):
        for key, expected in GOLDEN_FILTER.items():
            preset, chat = key.split("|")
            plan = R.resolve(_spec(preset, chat == "chat"), 1280, 720)
            with self.subTest(case=key):
                self.assertEqual(
                    R.build_filter(plan, chat_input=1 if chat == "chat" else None),
                    expected,
                )

    def test_normalize_omits_empty_fx(self):
        # An effects-free spec must round-trip to exactly the dict it had
        # before this feature existed, or re-saving one churns clips.json.
        out = R.normalize(_spec("cam_top"))
        self.assertNotIn("fx", out)
        self.assertNotIn("fx", R.normalize(dict(_spec("cam_top"), fx=[])))


if __name__ == "__main__":
    unittest.main()
