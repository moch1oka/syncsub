# Run: python -m unittest discover test
import os, sys, unittest
import pysubs2

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(here, ".."))
import syncsub as sync

class UnitTest(unittest.TestCase):
    def setUp(self):
        sync.parameter = sync.get_para_from_recent(default=True)
        sync.convert_parameter()

    def test_split_long_word_no_crash(self):
        text = "a" * 70
        self.assertEqual(sync.split_vietnamese_sentence(text), text)

    def test_clean_inline_comment_keeps_bracket(self):
        self.assertEqual(sync.clean_inline_comment("[Music]{note}\\Nabc"), "[Music]\\Nabc")

    def test_clean_html_keeps_escaped_angle_brackets(self):
        self.assertEqual(sync.clean_html_from_sub("a &lt;3 b<br><i>c</i>"), "a <3 b\nc")

    def test_episode_number(self):
        cases = {"[SubsPlease] Show - 05 (1080p) [ABCD1234].ass": 5, "Show.S01E07.1080p.x264.vi.srt": 7,
                 "Ep 12 v2.ass": 12, "Bocchi 03 [BD 1080p x265 10bit].ass": 3, "Paprika.srt": None}
        for name, ep in cases.items():
            self.assertEqual(sync.episode_number(name), ep, name)

    def test_actor_name_roundtrip(self):
        for name in ["A__B:c", "1.50", "", "Narrator: Bob"]:
            packed = sync.convert_actor({"id": "7", "oldname": name})
            self.assertEqual(sync.convert_actor(packed + "__tag:sign"), {"id": 7, "oldname": name, "tag": "sign"})

    def test_cli_bool(self):
        sync.parameter["comment_timed_sub"] = "false"
        sync.parameter["is_use_sign_ocr"] = "True"
        sync.convert_parameter()
        self.assertIs(sync.parameter["comment_timed_sub"], False)
        self.assertIs(sync.parameter["is_use_sign_ocr"], True)

    def test_consecutive_ocr_comments_removed(self):
        tmp = os.path.join(here, "_tmp_ocr.ass")
        subs = pysubs2.load(os.path.join(here, "en.ass"))
        subs.insert(0, pysubs2.SSAEvent(start=1000, end=2000, text="comment 1", type="Comment"))
        subs.insert(1, pysubs2.SSAEvent(start=1000, end=2000, text="comment 2", type="Comment"))
        subs.save(tmp)
        try:
            sync.parameter.update({"origin_sub_path": os.path.join(here, "en.ass"), "ocr_sub_path": tmp})
            sync.prepare()
            texts = [e.text for g in sync.state["web_groups"] for e in g["group_b"]]
            self.assertFalse(any("comment" in t for t in texts))
        finally:
            os.remove(tmp)

if __name__ == "__main__":
    unittest.main()
