"""End-to-end test for automation/server.py.

Starts the real server (websockify's handler plus our API and threaded accept
loop) against a stand-in RFB server, then drives it with a real HTTP client and
a real WebSocket client (node + ws). Skipped when websockify is not installed.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from automation import server as srv  # noqa: E402
from automation.api import AutomationApi, FlowStore  # noqa: E402
from automation.detect import Detector  # noqa: E402
from automation.engine import AutomationEngine, ControlBackend  # noqa: E402

try:
    srv.load_websockify()
    HAVE_WEBSOCKIFY = True
    IMPORT_ERROR = ""
except Exception as exc:  # pragma: no cover - environment dependent
    HAVE_WEBSOCKIFY = False
    IMPORT_ERROR = str(exc)

WS_PROBE = os.path.join(REPO_ROOT, "tests", "ws_client_probe.mjs")
TOKEN = "test-token-1234"


def http_request(url, method="GET", body=None, token=TOKEN, timeout=5.0):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("X-Automation-Token", token)
    if data:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


class FakeVncServer(threading.Thread):
    """Stands in for x11vnc: sends a banner, then echoes."""

    def __init__(self):
        super().__init__(daemon=True)
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(5)
        self.port = self.listener.getsockname()[1]
        self.stop = threading.Event()
        self.received = []

    def run(self):
        self.listener.settimeout(0.2)
        while not self.stop.is_set():
            try:
                conn, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()
        self.listener.close()

    def _serve(self, conn):
        try:
            conn.sendall(b"RFB 003.008\n")
            while not self.stop.is_set():
                data = conn.recv(1024)
                if not data:
                    break
                self.received.append(data)
                conn.sendall(data)
        except OSError:
            pass
        finally:
            conn.close()


@unittest.skipUnless(HAVE_WEBSOCKIFY, "websockify is not importable: %s" % IMPORT_ERROR)
class ServerIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="automation-server-")
        cls.data_dir = os.path.join(cls.tmp, "data")
        cls.web_root = os.path.join(cls.tmp, "www")
        os.makedirs(cls.web_root, exist_ok=True)
        os.makedirs(cls.data_dir, exist_ok=True)

        # A minimal stand-in for the noVNC tree.
        with open(os.path.join(cls.web_root, "vnc.html"), "w", encoding="utf-8") as handle:
            handle.write("<html><body><div id='screen'>noVNC</div></body></html>\n")
        with open(os.path.join(cls.web_root, "app.js"), "w", encoding="utf-8") as handle:
            handle.write("// novnc\n")

        cls.vnc = FakeVncServer()
        cls.vnc.start()

        proxy_class, base_handler = srv.load_websockify()
        backend = ControlBackend(script_path="/nonexistent/browser_control.sh")
        cls.engine = AutomationEngine(backend, cls.data_dir)
        detector = Detector(backend, cls.data_dir, {"width": 1366, "height": 768})
        cls.api = AutomationApi(cls.engine, FlowStore(cls.data_dir), detector,
                                data_dir=cls.data_dir, token=TOKEN,
                                control_script="/nonexistent/browser_control.sh")
        cls.proxy = proxy_class(
            RequestHandlerClass=srv.make_handler_class(cls.api, base_handler),
            listen_host="127.0.0.1", listen_port=0,
            target_host="127.0.0.1", target_port=cls.vnc.port,
            web=cls.web_root, file_only=True, timeout=0, idle_timeout=0)

        cls.front = srv.ThreadedFrontServer(cls.proxy, "127.0.0.1", 0)
        cls.front_thread = threading.Thread(target=cls.front.serve_forever, daemon=True)
        cls.front_thread.start()

        cls.cwd = os.getcwd()
        os.chdir(cls.web_root)  # websockify resolves the document root from cwd

        deadline = time.time() + 5
        while time.time() < deadline:
            if cls.front.port:
                try:
                    with socket.create_connection(("127.0.0.1", cls.front.port), 0.2):
                        break
                except OSError:
                    pass
            time.sleep(0.05)
        cls.base = "http://127.0.0.1:%d" % cls.front.port

    @classmethod
    def tearDownClass(cls):
        os.chdir(cls.cwd)
        cls.front.stop()
        cls.vnc.stop.set()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def url(self, path):
        return self.base + path

    # -- API ------------------------------------------------------------
    def test_info_is_reachable_and_reports_the_viewport(self):
        status, body = http_request(self.url("/automation/api/info"))
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["viewport"], {"width": 1366, "height": 768})
        self.assertTrue(payload["authRequired"])

    def test_api_requires_the_token(self):
        status, body = http_request(self.url("/automation/api/status"), token="wrong")
        self.assertEqual(status, 401)
        self.assertIn("X-Automation-Token", body.decode())

    def test_status_endpoint_answers(self):
        status, body = http_request(self.url("/automation/api/status"))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["status"], "idle")

    def test_unknown_endpoint_is_404(self):
        status, _ = http_request(self.url("/automation/api/nope"))
        self.assertEqual(status, 404)

    def test_flow_round_trip(self):
        flow = {"schema": 1, "name": "saved", "steps": [{"type": "wait", "ms": 5}]}
        status, body = http_request(self.url("/automation/api/flows/saved"),
                                    method="PUT", body={"flow": flow})
        self.assertEqual(status, 200, body)
        self.assertTrue(json.loads(body)["ok"])

        status, body = http_request(self.url("/automation/api/flows"))
        names = [item["name"] for item in json.loads(body)["flows"]]
        self.assertIn("saved", names)

        status, body = http_request(self.url("/automation/api/flows/saved"))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["flow"]["steps"][0]["type"], "wait")

        status, _ = http_request(self.url("/automation/api/flows/saved"), method="DELETE")
        self.assertEqual(status, 200)
        status, _ = http_request(self.url("/automation/api/flows/saved"))
        self.assertEqual(status, 404)

    def test_invalid_flow_is_rejected_with_422(self):
        status, body = http_request(self.url("/automation/api/flows/bad"), method="PUT",
                                    body={"flow": {"steps": [{"type": "nope"}]}})
        self.assertEqual(status, 422)
        self.assertTrue(json.loads(body)["errors"])

    def test_run_endpoint_rejects_a_broken_flow_before_touching_the_engine(self):
        status, body = http_request(self.url("/automation/api/run"), method="POST",
                                    body={"flow": {"steps": [{"type": "nope"}]}})
        self.assertEqual(status, 422)
        self.assertTrue(json.loads(body)["errors"])
        self.assertFalse(self.engine.busy())

    def test_artifact_refuses_to_escape_the_data_dir(self):
        status, _ = http_request(self.url("/automation/api/artifact?path=../../../../etc/passwd"))
        self.assertEqual(status, 404)
        status, _ = http_request(self.url("/automation/api/artifact?path="))
        self.assertEqual(status, 404)

    def test_guide_is_served(self):
        status, body = http_request(self.url("/automation/api/guide"))
        self.assertEqual(status, 200)
        self.assertIn(b"Step types", body)

    def test_a_flow_name_with_a_space_can_be_saved(self):
        # Regression: the browser sends "Untitled flow" as "Untitled%20flow" and
        # the router used to reject the escape, answering "no such endpoint".
        flow = {"name": "Untitled flow", "steps": [{"type": "click", "x": 5, "y": 6}]}
        status, body = http_request(self.url("/automation/api/flows/Untitled%20flow"),
                                    method="PUT", body=flow)
        self.assertEqual(status, 200, body)
        status, body = http_request(self.url("/automation/api/flows"))
        self.assertIn("Untitled flow", body.decode("utf-8"))

        # A percent-encoded separator must still be refused, not decoded.
        status, _ = http_request(self.url("/automation/api/flows/a%2F..%2F..%2Fx"),
                                 method="PUT", body=flow)
        self.assertEqual(status, 404)

    def test_paste_step_with_a_quoted_label_runs(self):
        # Regression: generated labels look like: paste "سلام" and the old
        # plain-text rule rejected the double quote, so the run failed with 422.
        flow = {"name": "farsi", "steps": [
            {"type": "click", "x": 5, "y": 6},
            {"type": "paste", "text": "سلام", "label": 'paste "سلام"'},
        ]}
        status, body = http_request(self.url("/automation/api/flows/paste-check"),
                                    method="PUT", body=flow)
        self.assertNotEqual(status, 422, body)
        # The reported failure came from actually running the flow.
        try:
            status, body = http_request(self.url("/automation/api/run"),
                                        method="POST", body={"flow": flow})
            self.assertNotEqual(status, 422, "the flow was rejected: %s" % body)
        finally:
            # xdotool is absent here, so the run ends in "error" and the shared
            # engine would hand that to later tests as the current status.
            for _ in range(300):
                if not self.engine.busy():
                    break
                time.sleep(0.01)
            self.engine._state = None

    def test_report_says_which_step_stopped_the_run(self):
        from automation.api import _report_section
        status = {
            "runId": "r1", "status": "error", "index": 2, "passIndex": 1,
            "stepCount": 4, "elapsedMs": 12340,
            "error": "step #2 (type) failed with rc=124: timed out",
            "results": [
                {"index": 0, "passIndex": 1, "type": "click",
                 "label": "click 100,200", "status": "ok", "durationMs": 42, "error": None},
                {"index": 1, "passIndex": 1, "type": "paste",
                 "label": 'paste "سلام"', "status": "ok", "durationMs": 88, "error": None},
                {"index": 2, "passIndex": 1, "type": "type", "label": "type 'x'",
                 "status": "error", "durationMs": 60000, "error": "timed out after 60s"},
            ],
            "entries": [{"t": 1, "level": "error", "message": "type -> rc=124",
                         "stderr": "timed out"}],
        }
        text = _report_section(status)
        self.assertIn("## گزارش اجرای سیستم", text)
        self.assertIn("3 گام اجرا شده", text)
        self.assertIn("timed out after 60s", text)
        # The model must be told exactly where it stopped.
        self.assertIn("اجرا روی گام **#2", text)
        self.assertIn("| 1 |", text)  # the per-step table

    def test_report_endpoint_answers_when_nothing_has_run(self):
        status, body = http_request(self.url("/automation/api/report"))
        self.assertEqual(status, 200)
        self.assertIn("هنوز اجرایی", body.decode("utf-8"))

    def test_prompt_asks_for_summary_code_and_questions(self):
        # The model must not just dump code: it says what it understood, gives
        # the code, then raises questions or improvements.
        status, body = http_request(self.url("/automation/api/prompt"), method="POST",
                                    body={"flow": {"name": "f", "steps": []}})
        self.assertEqual(status, 200, body)
        text = body.decode("utf-8")
        for heading in ("چه فهمیدی", "کد", "سؤال و نکته"):
            self.assertIn(heading, text)
        self.assertLess(text.index("چه فهمیدی"), text.index("سؤال و نکته"),
                        "the sections are out of order")

    def test_prompt_hands_back_the_previous_answer(self):
        status, body = http_request(self.url("/automation/api/prompt"), method="POST",
                                    body={"flow": {"name": "f", "steps": []},
                                          "previousReply": "دور قبل این را گفتم"})
        self.assertEqual(status, 200, body)
        text = body.decode("utf-8")
        self.assertIn("پاسخ قبلی تو", text)
        self.assertIn("دور قبل این را گفتم", text)

    def test_prompt_carries_the_run_report(self):
        # A finished run must reach the prompt, so the model can diagnose it.
        flow = {"name": "rep", "steps": [{"type": "click", "x": 1, "y": 1}]}
        http_request(self.url("/automation/api/run"), method="POST", body={"flow": flow})
        try:
            for _ in range(200):
                if not self.engine.busy():
                    break
                time.sleep(0.01)
            status, body = http_request(self.url("/automation/api/prompt"),
                                        method="POST", body={"flow": flow})
            self.assertEqual(status, 200, body)
            self.assertIn("گزارش اجرای سیستم", body.decode("utf-8"))
        finally:
            self.engine._state = None

    def test_prompt_carries_the_user_request_and_the_shot_link(self):
        pages = os.path.join(self.data_dir, "pages")
        os.makedirs(pages, exist_ok=True)
        snapshot = {"id": "p1", "title": "گوگل", "url": "https://example.com/",
                    "capturedAt": 1, "publicShot": "shot-1.png", "text": "سلام",
                    "elements": [], "viewport": {"width": 1366, "height": 768}}
        with open(os.path.join(pages, "p1.json"), "w", encoding="utf-8") as handle:
            json.dump(snapshot, handle, ensure_ascii=False)

        status, body = http_request(self.url("/automation/api/prompt"), method="POST",
                                    body={"pageId": "p1", "request": "روی اولین نتیجه کلیک کن",
                                          "publicBase": "https://abc.trycloudflare.com",
                                          "flow": {"name": "f", "steps": []}})
        self.assertEqual(status, 200, body)
        text = body.decode("utf-8")
        self.assertIn("درخواست کاربر", text)
        self.assertIn("روی اولین نتیجه کلیک کن", text)
        self.assertIn("https://abc.trycloudflare.com/automation/api/public/shot/shot-1.png", text)

    def test_public_shot_is_reachable_without_a_token(self):
        pub = os.path.join(self.data_dir, "public")
        os.makedirs(pub, exist_ok=True)
        with open(os.path.join(pub, "shot.png"), "wb") as handle:
            handle.write(b"\x89PNG\r\n\x1a\n")

        status, body = http_request(self.url("/automation/api/public/shot/shot.png"),
                                    token="")  # no token on purpose
        self.assertEqual(status, 200)
        self.assertEqual(body, b"\x89PNG\r\n\x1a\n")

    def test_public_shot_blocks_traversal_and_missing_files(self):
        for bad in ("/automation/api/public/shot/..%2F..%2Fx.png",
                    "/automation/api/public/shot/note.txt",
                    "/automation/api/public/shot/absent.png"):
            status, _ = http_request(self.url(bad), token="")
            self.assertEqual(status, 404, bad)

    def test_detect_stores_a_public_screenshot(self):
        from automation.detect import Detector
        det = Detector(ControlBackend("/nonexistent"), self.data_dir,
                       {"width": 1366, "height": 768})
        # share() copies into data/public and returns just the basename
        src = os.path.join(self.data_dir, "shots", "s.png")
        os.makedirs(os.path.dirname(src), exist_ok=True)
        with open(src, "wb") as handle:
            handle.write(b"png")
        self.assertEqual(det.share(src), "s.png")
        self.assertTrue(os.path.exists(os.path.join(self.data_dir, "public", "s.png")))
        self.assertIsNone(det.share(None))
        self.assertIsNone(det.share("/no/such/file.png"))

    def test_post_outside_the_api_prefix_is_refused(self):
        status, _ = http_request(self.url("/vnc.html"), method="POST", body={})
        self.assertEqual(status, 405)

    # -- static ---------------------------------------------------------
    def test_static_files_are_served(self):
        status, body = http_request(self.url("/vnc.html"))
        self.assertEqual(status, 200)
        self.assertIn(b"noVNC", body)

    def test_directory_listing_is_disabled(self):
        status, _ = http_request(self.url("/"))
        self.assertEqual(status, 404)

    def test_path_traversal_is_blocked(self):
        status, _ = http_request(self.url("/../../etc/passwd"))
        self.assertIn(status, (400, 404))

    # -- WebSocket proxy ------------------------------------------------
    @unittest.skipUnless(os.path.exists(WS_PROBE), "ws probe missing")
    def test_websocket_path_still_reaches_the_vnc_server(self):
        result = subprocess.run(
            ["node", WS_PROBE, "ws://127.0.0.1:%d/websockify" % self.front.port],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("OPEN", result.stdout)
        self.assertIn("MSG:RFB 003.008", result.stdout)
        self.assertIn("MSG:PING", result.stdout)
        self.assertIn("DONE", result.stdout)
        self.assertIn(b"PING", b"".join(self.vnc.received))


class WebRootTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="automation-webroot-")
        self.novnc = os.path.join(self.tmp, "novnc")
        self.static = os.path.join(self.tmp, "static")
        self.web = os.path.join(self.tmp, "www")
        os.makedirs(self.novnc)
        os.makedirs(self.static)
        with open(os.path.join(self.novnc, "vnc.html"), "w", encoding="utf-8") as handle:
            handle.write("<html><body>noVNC</body></html>")
        with open(os.path.join(self.static, "automation.js"), "w", encoding="utf-8") as handle:
            handle.write("// sidebar\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_dedicated_panel_page_is_published_and_versioned(self):
        real_static = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "automation", "static")
        report = srv.prepare_web_root(self.web, self.novnc, real_static)
        self.assertIn("panel.html", report["staticCopied"])
        self.assertTrue(report["panel"])

        with open(os.path.join(self.web, "automation", "panel.html"),
                  encoding="utf-8") as handle:
            html = handle.read()
        self.assertIn("mas-standalone", html)
        # Assets carry the build hash, so a rebuild is never served from cache.
        self.assertRegex(html, r"automation\.js\?v=[0-9a-f]{10}")
        self.assertRegex(html, r"automation\.css\?v=[0-9a-f]{10}")
        self.assertEqual(report["cacheKey"],
                         html.split("automation.js?v=")[1][:10])

    def test_copies_novnc_static_and_patches_vnc_html_once(self):
        report = srv.prepare_web_root(self.web, self.novnc, self.static)
        self.assertTrue(report["novncCopied"])
        self.assertIn("automation.js", report["staticCopied"])
        self.assertTrue(report["patched"])
        with open(os.path.join(self.web, "vnc.html"), encoding="utf-8") as handle:
            html = handle.read()
        self.assertIn("automation/automation.js", html)
        self.assertIn("</body>", html)
        self.assertTrue(html.index("automation/automation.js") < html.index("</body>"))

        again = srv.prepare_web_root(self.web, self.novnc, self.static)
        self.assertFalse(again["novncCopied"], "a second run must not recopy noVNC")
        self.assertFalse(again["patched"], "the marker must not be injected twice")
        with open(os.path.join(self.web, "vnc.html"), encoding="utf-8") as handle:
            self.assertEqual(handle.read().count(srv.MARKER), 1)

    def test_a_changed_sidebar_invalidates_the_browser_cache_key(self):
        srv.prepare_web_root(self.web, self.novnc, self.static)
        vnc_html = os.path.join(self.web, "vnc.html")
        with open(vnc_html, encoding="utf-8") as handle:
            first = handle.read()
        self.assertIn("?v=", first)
        self.assertIn(srv.END_MARKER, first)
        self.assertEqual(first.count(srv.MARKER), 1)

        # Ship a new build of the sidebar: the tag must be rewritten in place so
        # the browser cannot keep serving the old script.
        with open(os.path.join(self.static, "automation.js"), "w", encoding="utf-8") as handle:
            handle.write("// sidebar v2\n")
        report = srv.prepare_web_root(self.web, self.novnc, self.static)
        self.assertTrue(report["patched"], "a new build must rewrite the injected tags")

        with open(vnc_html, encoding="utf-8") as handle:
            second = handle.read()
        self.assertEqual(second.count(srv.MARKER), 1, "the block must be replaced, not appended")
        self.assertEqual(second.count(srv.END_MARKER), 1)
        self.assertNotEqual(first, second)
        self.assertTrue(second.index("automation/automation.js") < second.index("</body>"))

    def test_missing_novnc_is_reported_clearly(self):
        with self.assertRaises(FileNotFoundError):
            srv.prepare_web_root(os.path.join(self.tmp, "www2"),
                                 os.path.join(self.tmp, "nope"), self.static)


if __name__ == "__main__":
    unittest.main(verbosity=2)
