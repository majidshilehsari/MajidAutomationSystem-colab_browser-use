"""Tests for automation/cdp.py against a real fake CDP endpoint."""

import os
import re
import subprocess
import sys
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from automation import cdp  # noqa: E402

FAKE = os.path.join(REPO_ROOT, "tests", "fake_cdp.mjs")


@unittest.skipUnless(os.path.exists(FAKE), "fake CDP script missing")
class CdpClientTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.proc = subprocess.Popen(["node", FAKE], cwd=REPO_ROOT,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True)
        line = cls.proc.stdout.readline()
        match = re.search(r"PORT:(\d+)", line or "")
        if not match:
            cls.proc.kill()
            raise RuntimeError("fake CDP did not report a port: %r" % line)
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

    def test_lists_targets(self):
        targets = cdp.list_targets(self.port)
        self.assertEqual(len(targets), 2)
        self.assertEqual({t["type"] for t in targets}, {"page", "service_worker"})

    def test_picks_the_page_target_not_the_service_worker(self):
        target = cdp.pick_page_target(self.port)
        self.assertIsNotNone(target)
        self.assertEqual(target["type"], "page")
        self.assertEqual(target["id"], "PAGE1")

    def test_evaluate_returns_the_dom_payload(self):
        result = cdp.evaluate("ignored", port=self.port)
        value = result["value"]
        self.assertEqual(result["url"], "https://example.com/search?q=panda")
        self.assertEqual(result["title"], "Example search")
        self.assertEqual(value["elementCount"], 2)
        self.assertEqual(value["elements"][0]["selector"], "#search")
        self.assertEqual(value["elements"][0]["desktop"], {"x": 683, "y": 418})
        self.assertIn("cute panda", value["bodyText"])

    def test_unreachable_port_raises_cdp_error(self):
        with self.assertRaises(cdp.CdpError):
            cdp.list_targets(1)  # nothing listens here

    def test_handshake_against_a_plain_http_endpoint_fails_cleanly(self):
        with self.assertRaises(cdp.CdpError):
            cdp.ws_connect("ws://127.0.0.1:%d/json/list" % self.port)

    def test_non_ws_url_is_rejected(self):
        with self.assertRaises(cdp.CdpError):
            cdp.ws_connect("http://127.0.0.1:%d/" % self.port)


class FramingTest(unittest.TestCase):
    """Frame encoding must follow RFC 6455 (client frames are masked)."""

    def test_small_frame_is_masked_and_length_is_inline(self):
        class Sink:
            def __init__(self):
                self.sent = b""

            def sendall(self, data):
                self.sent += data

        sink = Sink()
        cdp.ws_send(sink, b"hello")
        self.assertEqual(sink.sent[0], 0x81)  # FIN + text
        self.assertTrue(sink.sent[1] & 0x80, "client frames must be masked")
        self.assertEqual(sink.sent[1] & 0x7F, 5)
        mask = sink.sent[2:6]
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(sink.sent[6:]))
        self.assertEqual(payload, b"hello")

    def test_medium_frame_uses_the_16_bit_length(self):
        class Sink:
            def __init__(self):
                self.sent = b""

            def sendall(self, data):
                self.sent += data

        sink = Sink()
        cdp.ws_send(sink, b"x" * 500)
        self.assertEqual(sink.sent[1] & 0x7F, 126)
        self.assertEqual(int.from_bytes(sink.sent[2:4], "big"), 500)


if __name__ == "__main__":
    unittest.main(verbosity=2)
