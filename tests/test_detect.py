"""Tests for automation/detect.py, including a real fake CDP endpoint."""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from automation.detect import Detector, page_key, slugify  # noqa: E402
from automation.engine import ControlBackend  # noqa: E402

FAKE = os.path.join(REPO_ROOT, "tests", "fake_cdp.mjs")

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a49444154789c6300010000050001"
    "0d0a2db40000000049454e44ae426082")


class FakeBackend(ControlBackend):
    """Pretends to be browser_control.sh on a live desktop."""

    def __init__(self, shots_dir):
        super().__init__(script_path="/nonexistent/browser_control.sh")
        self.shots_dir = shots_dir
        self.page_text_value = "clipboard fallback text"

    def control(self, args, timeout=None):
        name = args[0] if args else ""
        if name == "screenshot":
            path = os.path.join(self.screenshot_dir or self.shots_dir, args[1])
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as handle:
                handle.write(PNG)
            return 0, path, ""
        if name == "active":
            return 0, "Example search - Google Chrome", ""
        if name == "windows":
            # wmctrl -lx: id, desktop, class, host, title
            return 0, ("0x1234 0x1 chrome.Chrome colab Example search\n"
                       "0x5678 0x1 xterm.XTerm colab root@colab: /content"), ""
        if name in ("key",):
            return 0, "", ""
        return 0, "ok", ""

    def page_text(self):
        return 0, self.page_text_value, ""


class StaticCdp:
    @staticmethod
    def evaluate(expression, port=9222, timeout=8.0):
        dom = {
            "url": "https://example.com/security?session=private",
            "title": "Verify you are human",
            "bodyText": "Please verify you are human to continue.",
            "elements": [], "elementCount": 0, "challengeHints": [],
        }
        return {"value": dom, "url": dom["url"], "title": dom["title"]}


class DetectorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="automation-detect-")
        self.backend = FakeBackend(self.tmp)
        self.detector = Detector(self.backend, self.tmp, {"width": 1366, "height": 768})

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_screenshot_now_writes_into_the_shots_dir(self):
        path = self.detector.screenshot_now("a.png")
        self.assertIsNotNone(path)
        self.assertTrue(os.path.exists(path))
        self.assertEqual(os.path.dirname(path), os.path.join(self.tmp, "shots"))

    def test_cdp_is_reported_unavailable_when_nothing_listens(self):
        self.assertFalse(self.detector.cdp_available())

    def test_detect_without_cdp_falls_back_to_clipboard_and_windows(self):
        snapshot = self.detector.detect()
        self.assertFalse(snapshot["cdp"])
        self.assertEqual(snapshot["text"], "clipboard fallback text")
        self.assertEqual(snapshot["activeWindow"], "Example search - Google Chrome")
        self.assertEqual(len(snapshot["windows"]), 2)
        self.assertEqual(snapshot["windows"][0]["title"], "Example search")
        self.assertIsNotNone(snapshot["screenshot"])
        self.assertTrue(snapshot["id"])

    def test_manual_detect_includes_a_best_effort_challenge_assessment(self):
        detector = Detector(self.backend, self.tmp, {"width": 1366, "height": 768},
                            cdp_module=StaticCdp)
        snapshot = detector.detect()
        self.assertTrue(snapshot["challenge"]["detected"])
        self.assertEqual(snapshot["challenge"]["pageOrigin"], "https://example.com")
        pages = detector.pages()
        self.assertTrue(pages[0]["challengeDetected"])
        self.assertIn("human-verification-wording", pages[0]["challengeSignals"])

    def test_detect_is_remembered_in_the_page_index(self):
        first = self.detector.detect()
        second = self.detector.detect()
        pages = self.detector.pages()
        self.assertEqual(len(pages), 2)
        self.assertEqual({p["id"] for p in pages}, {first["id"], second["id"]})
        self.assertEqual(pages[0]["capturedAt"] >= pages[1]["capturedAt"], True)

    def test_load_returns_the_full_snapshot_including_text(self):
        snapshot = self.detector.detect()
        loaded = self.detector.load(snapshot["id"])
        self.assertEqual(loaded["text"], "clipboard fallback text")
        # the index entry stays small; only the snapshot carries the full text
        with open(self.detector.index_path, encoding="utf-8") as handle:
            index = json.load(handle)
        self.assertNotIn("text", index[0])
        self.assertIn("textPreview", index[0])

    def test_load_refuses_path_traversal(self):
        self.assertIsNone(self.detector.load("../../etc/passwd"))
        self.assertIsNone(self.detector.load("missing-id"))

    def test_index_survives_corruption(self):
        with open(self.detector.index_path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        self.assertEqual(self.detector.pages(), [])
        self.detector.detect()
        self.assertEqual(len(self.detector.pages()), 1)


class HelperTest(unittest.TestCase):
    def test_slugify(self):
        self.assertEqual(slugify("Example.Com/Path"), "example-com-path")
        self.assertEqual(slugify(""), "page")
        self.assertLessEqual(len(slugify("x" * 500, 40)), 40)

    def test_page_key_prefers_host_and_path(self):
        self.assertEqual(page_key("https://example.com/a/b?x=1", "ignored"), "example.com/a/b")
        self.assertEqual(page_key("https://example.com/", None), "example.com")
        self.assertEqual(page_key(None, "Some Title"), "Some Title")
        self.assertEqual(page_key(None, None), "unknown-page")

    def test_url_is_recovered_from_a_chrome_title_when_possible(self):
        detector = Detector.__new__(Detector)
        from automation.detect import _url_from_title
        self.assertEqual(_url_from_title("page - https://example.com/x - Chrome"),
                         "https://example.com/x")
        self.assertIsNone(_url_from_title("just a title"))
        self.assertIsNone(_url_from_title(None))


@unittest.skipUnless(os.path.exists(FAKE), "fake CDP script missing")
class DetectorWithCdpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.proc = subprocess.Popen(["node", FAKE], cwd=REPO_ROOT,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True)
        match = re.search(r"PORT:(\d+)", cls.proc.stdout.readline() or "")
        if not match:
            cls.proc.kill()
            raise RuntimeError("fake CDP did not report a port")
        cls.port = int(match.group(1))

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
        for stream in (cls.proc.stdout, cls.proc.stderr):
            if stream:
                stream.close()

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="automation-detect-cdp-")
        self.detector = Detector(FakeBackend(self.tmp), self.tmp,
                                 {"width": 1366, "height": 768}, cdp_port=self.port)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cdp_is_detected_as_available(self):
        self.assertTrue(self.detector.cdp_available())

    def test_detect_uses_the_dom_and_skips_the_clipboard_fallback(self):
        snapshot = self.detector.detect()
        self.assertTrue(snapshot["cdp"])
        self.assertIsNone(snapshot["error"])
        self.assertEqual(snapshot["url"], "https://example.com/search?q=panda")
        self.assertEqual(snapshot["title"], "Example search")
        self.assertEqual(snapshot["elementCount"], 2)
        self.assertEqual(snapshot["elements"][0]["selector"], "#search")
        self.assertIn("cute panda", snapshot["text"])
        self.assertEqual(snapshot["pageKey"], "example.com/search")

    def test_dom_snapshot_is_persisted_with_elements(self):
        snapshot = self.detector.detect()
        loaded = self.detector.load(snapshot["id"])
        self.assertEqual(len(loaded["elements"]), 2)
        self.assertTrue(loaded["cdp"])

    def test_detect_without_dom_still_works(self):
        snapshot = self.detector.detect(include_dom=False)
        self.assertFalse(snapshot["cdp"])
        self.assertEqual(snapshot["text"], "clipboard fallback text")


if __name__ == "__main__":
    unittest.main(verbosity=2)
