# Run: python -m unittest discover test
import os, sys, json, shutil, tempfile, unittest
import pysubs2

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(here, ".."))
import syncsub as sync

class UnitTest(unittest.TestCase):
    def setUp(self):
        sync.parameter = sync.get_para_from_recent(default=True)
        sync.filter_rules_path = os.devnull
        sync.convert_parameter()
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def make_pair(self, eng_text, ocr_text, rules=None):
        # 1 timed line + 1 ocr line at the same time, ready for prepare()
        for name, text in (("en.ass", eng_text), ("ocr.ass", ocr_text)):
            subs = pysubs2.SSAFile()
            subs.append(pysubs2.SSAEvent(start=0, end=2000, text=text))
            subs.save(os.path.join(self.tmp, name))
        if rules is not None:
            sync.filter_rules_path = os.path.join(self.tmp, "rules.txt")
            with open(sync.filter_rules_path, "w", encoding="utf-8") as f:
                f.write(rules)
        sync.parameter.update({"origin_sub_path": os.path.join(self.tmp, "en.ass"),
                               "ocr_sub_path": os.path.join(self.tmp, "ocr.ass"),
                               "output_filename": os.path.join(self.tmp, "out.ass")})
        sync.prepare()

    def submit(self, client):
        data = json.loads(client.get('/data').data)
        return client.post('/submit?token=%d' % sync.state["token"], json=data).get_json()

    def test_split_long_word_no_crash(self):
        text = "a" * 70
        self.assertEqual(sync.split_vietnamese_sentence(text), text)

    def test_split_removes_dash_after_tags(self):
        self.assertEqual(sync.split_line("{\\an8}- Cách nhanh nhất là không kích.\\N- Ở Night City à?"),
                         ["{\\an8}Cách nhanh nhất là không kích.", "Ở Night City à?"])
        self.assertEqual(sync.split_line("{\\i1}- Sao thế ạ?{\\i0}\\N{\\i1}- Anh đang lên hình đấy.{\\i0}"),
                         ["{\\i1}Sao thế ạ?{\\i0}", "{\\i1}Anh đang lên hình đấy.{\\i0}"])
        self.assertEqual(sync.split_line("{\\i1}- mau lên…{\\i0}\\N{\\i1}- Mau lên.{\\i0}"),
                         ["{\\i1}Mau lên…{\\i0}", "{\\i1}Mau lên.{\\i0}"])

    def test_strip_ocr_tags(self):
        sync.parameter["strip_ocr_tags"] = True
        self.make_pair("Hello", "{\\an8}{\\i1}Xin chào{\\i0}")
        self.assertEqual(sync.state["web_groups"][0]["group_b"][0].text, "Xin chào")

    def test_clean_inline_comment_keeps_bracket(self):
        self.assertEqual(sync.clean_inline_comment("[Music]{note}\\Nabc"), "[Music]\\Nabc")

    def test_clean_html_keeps_escaped_angle_brackets(self):
        self.assertEqual(sync.clean_html_from_sub("a &lt;3 b<br><i>c</i>"), "a <3 b\nc")

    def test_angle_brackets_and_entities_survive_editor(self):
        self.make_pair("Hello", "Say <hi> & AT&amp;T")
        with sync.app.test_client() as c:
            self.assertTrue(self.submit(c)["success"])
        self.assertEqual(pysubs2.load(os.path.join(self.tmp, "out.ass"))[0].text, "{Hello}Say <hi> & AT&amp;T")

    def test_submit_retry_after_failed_save(self):
        self.make_pair("Hello", "Xin chào")
        out = os.path.join(self.tmp, "missing", "out.ass")
        sync.parameter["output_filename"] = out
        with sync.app.test_client() as c:
            self.assertFalse(self.submit(c)["success"])
            os.makedirs(os.path.dirname(out))
            self.assertTrue(self.submit(c)["success"])
        self.assertEqual(pysubs2.load(out)[0].text, "{Hello}Xin chào")

    def test_old_token_rejected(self):
        self.make_pair("Hello", "Xin chào")
        old = sync.state["token"]
        sync.prepare()
        with sync.app.test_client() as c:
            data = json.loads(c.get('/data').data)
            self.assertFalse(c.post('/submit?token=%d' % old, json=data).get_json()["success"])
            self.assertFalse(c.post('/draft?token=%d' % old, json={"0_0": "x"}).get_json()["success"])
            self.assertFalse(os.path.exists(sync.draft_path()))

    def test_rule_newline(self):
        rules = "replace________ / ________\\n\nregex________ - ________\\n\n"
        self.make_pair("Hello", "Một / Hai - Ba", rules)
        self.assertEqual(sync.state["web_groups"][0]["group_b"][0].text, "Một\\NHai\\NBa")

    def test_bad_encoding_message(self):
        path = os.path.join(self.tmp, "bad.srt")
        with open(path, "wb") as f:
            f.write("1\n00:00:01,000 --> 00:00:02,000\nXin chào\n".encode("cp1258"))
        with self.assertRaises(ValueError) as e:
            sync.pysubs2load(path)
        self.assertIn("not UTF-8 or UTF-16", str(e.exception))

    def test_browse_needs_header(self):
        with sync.app.test_client() as c:
            self.assertEqual(c.get('/browse').status_code, 403)

    def test_episode_number(self):
        cases = {"[SubsPlease] Show - 05 (1080p) [ABCD1234].ass": 5, "Show.S01E07.1080p.x264.vi.srt": 7,
                 "Ep 12 v2.ass": 12, "Bocchi 03 [BD 1080p x265 10bit].ass": 3, "Paprika.srt": None,
                 "[Group] Show - 05 [1080p][AAC 2.0].ass": 5, "Show - 12 (BD 1080p Opus 5.1).ass": 12,
                 "Show.S02E03.NF.WEB-DL.DDP5.1.H.264.srt": 3}
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
        tmp = os.path.join(self.tmp, "ocr.ass")
        subs = pysubs2.load(os.path.join(here, "en.ass"))
        subs.insert(0, pysubs2.SSAEvent(start=1000, end=2000, text="comment 1", type="Comment"))
        subs.insert(1, pysubs2.SSAEvent(start=1000, end=2000, text="comment 2", type="Comment"))
        subs.save(tmp)
        sync.parameter.update({"origin_sub_path": os.path.join(here, "en.ass"), "ocr_sub_path": tmp})
        sync.prepare()
        texts = [e.text for g in sync.state["web_groups"] for e in g["group_b"]]
        self.assertFalse(any("comment" in t for t in texts))

if __name__ == "__main__":
    unittest.main()
