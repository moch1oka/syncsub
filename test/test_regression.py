# Run: python -m unittest discover test
# Processes test/en.ass + test/vi.srt with test/settings.yaml and no filter rules, submits the editor data
# unchanged, and compares the result with test/expected.ass
import os, sys, json, tempfile, unittest
import yaml

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(here, ".."))
import syncsub as sync

class RegressionTest(unittest.TestCase):
    def test_default_output(self):
        out = os.path.join(tempfile.mkdtemp(), "output.ass")
        with open(os.path.join(here, "settings.yaml"), encoding="utf-8") as f:
            sync.parameter = yaml.safe_load(f)
        sync.parameter.update({"origin_sub_path": os.path.join(here, "en.ass"),
                               "ocr_sub_path": os.path.join(here, "vi.srt"),
                               "output_filename": out})
        sync.filter_rules_path = os.devnull
        res, msg = sync.convert_parameter()
        self.assertTrue(res, msg)
        sync.prepare()
        with sync.app.test_client() as c:
            data = json.loads(c.get('/data').data)
            res = c.post('/submit?token=%d' % sync.state["token"], json=data).get_json()
        self.assertTrue(res["success"], res)
        with open(out, encoding="utf-8") as f:
            actual = f.read()
        with open(os.path.join(here, "expected.ass"), encoding="utf-8") as f:
            expected = f.read()
        self.assertEqual(actual, expected)

if __name__ == "__main__":
    unittest.main()
