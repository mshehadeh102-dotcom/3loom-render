"""Run with python3 -m unittest discover -s tests -p 'test_preflight.py'."""
import importlib.util
from pathlib import Path
import unittest

p = Path(__file__).resolve().parents[1] / "render" / "preflight.py"
spec = importlib.util.spec_from_file_location("preflight", p)
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.job = {
            "job_id": "science_001",
            "script": "كيف تتكون البراكين؟ تتراكم الصهارة تحت القشرة الأرضية.",
            "clips": [{"url": "https://upload.wikimedia.org/wikipedia/commons/a/ab/sample.mp4"}],
            "audio_b64": "A" * 1000,
            "callback_url": "https://example.org/return",
        }

    def test_valid(self):
        self.assertEqual(preflight.check_job(self.job), [])

    def test_private_address_rejected(self):
        self.job["clips"][0]["url"] = "https://127.0.0.1/secret"
        self.assertTrue(preflight.check_job(self.job))

    def test_upload_blocked(self):
        self.job["upload"] = True
        self.assertTrue(any("publishing" in e for e in preflight.check_job(self.job)))

    def test_excess_clips_blocked(self):
        self.job["clips"] *= 9
        self.assertTrue(preflight.check_job(self.job))

    def test_nan_loudness_rejected(self):
        self.job["loudness_lufs"] = "NaN"
        self.assertTrue(preflight.check_job(self.job))


if __name__ == "__main__":
    unittest.main()
