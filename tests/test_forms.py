"""Round-13 form family: interactive_form, progress_notice, secure_input and
the secure_http_request connector, end to end through the real engine.

The security contract under test: a secret typed by a human exists only in the
vault; the run state, its results, the status payload and every log line stay
free of the raw value, while the agent-facing output carries an opaque handle.
"""

import json
import shutil
import tempfile
import time
import unittest
from unittest import mock

from automation.api import ApiError, AutomationApi, FlowStore
from automation.engine import TERMINAL_STATES, AutomationEngine
from tests.test_engine import FakeBackend, FakeClock


def wait_until(condition, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


POLICY = {
    "scope": "test-api",
    "headerTemplate": "Authorization: Bearer {{secret}}",
    "allowedHosts": ["api.example.dev"],
    "allowedPaths": ["/v1"],
    "methods": ["GET"],
    "ttlSeconds": 600,
    "maxUses": 5,
}

FORM_FLOW = {
    "name": "form-flow",
    "steps": [{
        "type": "interactive_form",
        "title": "اطلاعات شهر",
        "fields": [{"id": "city", "type": "text", "label": "شهر", "required": True}],
        "output": {"namespace": "info"},
    }],
}

SECRET_FLOW = {
    "name": "secret-flow",
    "steps": [{
        "type": "secure_input",
        "title": "کلید API",
        "fields": [{"id": "api_key", "type": "secret", "label": "کلید"}],
        "policy": dict(POLICY),
        "output": {"handle": "creds"},
    }],
}


class FakeResponse:
    def __init__(self, body=b'{"ok": true}', status=200):
        self._body = body
        self.status = status
        self.headers = {}

    def read(self, size=-1):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    def __init__(self, response=None):
        self.req = None
        self._response = response or FakeResponse()

    def open(self, request, timeout=0):
        self.req = request
        return self._response


class FormFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.clock = FakeClock()
        self.backend = FakeBackend()
        self.engine = AutomationEngine(self.backend, self.tmp,
                                       sleep=self.clock.sleep, clock=self.clock.time)

    def tearDown(self):
        if self.engine.busy():
            self.engine.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def wait_pending(self, kind):
        ok = wait_until(lambda: (self.engine.status().get("awaitingConfirmation")
                                 or {}).get("kind") == kind)
        self.assertTrue(ok, "the %s card never surfaced" % kind)
        return self.engine.status()["awaitingConfirmation"]

    def wait_done(self):
        ok = wait_until(lambda: self.engine.status()["status"] in TERMINAL_STATES)
        self.assertTrue(ok, "the run never finished")
        return self.engine.status()

    def run_flow(self, flow):
        self.engine.start(flow)

    def test_form_submit_resumes_and_saves_values(self):
        self.run_flow(FORM_FLOW)
        pending = self.wait_pending("form")
        form_id = pending["formId"]
        self.assertEqual(pending["formSchema"]["title"], "اطلاعات شهر")
        self.assertEqual(self.engine.form_status(form_id)["status"], "waiting")
        self.engine.submit_form(form_id, {"city": "تهران"})
        status = self.wait_done()
        self.assertEqual(status["status"], "done")
        self.assertEqual(self.engine._state.form_outputs["info"]["city"], "تهران")
        # the interpolated reference resolves from the run's own outputs
        self.assertEqual(
            self.engine._interpolate("x=${form.info.city}", self.engine._state),
            "x=تهران")

    def test_form_cancel_stops_the_run_safely(self):
        self.run_flow(FORM_FLOW)
        form_id = self.wait_pending("form")["formId"]
        self.engine.cancel_form(form_id)
        status = self.wait_done()
        self.assertEqual(status["status"], "stopped")

    def test_unknown_form_ids_are_refused(self):
        self.run_flow(FORM_FLOW)
        self.wait_pending("form")
        with self.assertRaises(RuntimeError):
            self.engine.submit_form("form-nope", {"city": "x"})
        with self.assertRaises(RuntimeError):
            self.engine.cancel_form("form-nope")
        self.assertIsNone(self.engine.form_status("form-nope"))

    def test_progress_notice_never_blocks(self):
        self.run_flow({"name": "notice", "steps": [
            {"type": "progress_notice", "title": "مرحلهٔ ۲ از ۵",
             "message": "دانلود ادامه دارد", "level": "success"},
        ]})
        status = self.wait_done()
        self.assertEqual(status["status"], "done")
        self.assertEqual(status["results"][0]["status"], "ok")

    def test_secret_values_only_reach_the_vault(self):
        self.run_flow(SECRET_FLOW)
        pending = self.wait_pending("secret")
        form_id = pending["formId"]
        # the card carries the human-approval scope display
        self.assertEqual(pending["formSchema"]["policy"]["allowedHosts"],
                         ["api.example.dev"])
        handles = self.engine.submit_secret(form_id, {"api_key": "SUPERSECRET"})
        status = self.wait_done()
        self.assertEqual(status["status"], "done")
        meta = handles["api_key"]
        handle = meta["fullHandle"]
        self.assertTrue(handle.startswith("sec_"))
        stored = self.engine._state.form_outputs["creds"]["api_key"]
        self.assertEqual(stored["fullHandle"], handle)
        # the raw value exists nowhere except inside the vault
        self.assertNotIn("SUPERSECRET", json.dumps(status, ensure_ascii=False, default=str))
        self.assertNotIn("SUPERSECRET", json.dumps(
            self.engine._state.results, ensure_ascii=False, default=str))
        self.assertNotIn("SUPERSECRET", json.dumps(
            self.engine._state.entries, ensure_ascii=False, default=str))
        value, _ = self.engine.vault.acquire(handle, "GET",
                                             "https://api.example.dev/v1/x")
        self.assertEqual(value, "SUPERSECRET")

    def test_connector_sends_the_policy_header_and_records_a_digest(self):
        meta = self.engine.vault.create("SUPERSECRET", dict(POLICY))
        handle = meta["fullHandle"]
        opener = FakeOpener()
        with mock.patch("urllib.request.build_opener", return_value=opener):
            self.run_flow({"name": "connector", "steps": [{
                "type": "secure_http_request",
                "handle": handle, "method": "GET",
                "url": "https://api.example.dev/v1/data",
                "saveAs": "resp",
            }]})
            status = self.wait_done()
        self.assertEqual(status["status"], "done")
        self.assertEqual(opener.req.get_header("Authorization"), "Bearer SUPERSECRET")
        self.assertEqual(self.engine._state.form_outputs["resp"], {"ok": True})
        # step stdout lands in the run log, never in a results row; the log
        # carries the digest, not the value
        log_text = json.dumps(self.engine._state.entries,
                              ensure_ascii=False, default=str)
        self.assertIn("sha256:", log_text)
        self.assertIn("secure request GET api.example.dev/v1/data -> 200", log_text)
        self.assertNotIn("SUPERSECRET", log_text)

    def test_connector_refuses_off_scope_urls(self):
        meta = self.engine.vault.create("SUPERSECRET", dict(POLICY))
        handle = meta["fullHandle"]
        opener = FakeOpener()
        with mock.patch("urllib.request.build_opener", return_value=opener):
            self.run_flow({"name": "connector-bad", "steps": [{
                "type": "secure_http_request",
                "handle": handle, "method": "GET",
                "url": "https://evil.example.net/v1/data",
            }]})
            status = self.wait_done()
        self.assertEqual(status["status"], "error")
        self.assertIn("vault refused", status["results"][0]["error"])
        self.assertIsNone(opener.req)  # never even built a request


class FormApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.clock = FakeClock()
        self.backend = FakeBackend()
        self.engine = AutomationEngine(self.backend, self.tmp,
                                       sleep=self.clock.sleep, clock=self.clock.time)

    def tearDown(self):
        if self.engine.busy():
            self.engine.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_form_routes_answer_409_without_engine_and_404_without_form(self):
        bare = AutomationApi(None, FlowStore(self.tmp), data_dir=self.tmp)
        with self.assertRaises(ApiError) as ctx:
            bare.route_form_submit(params={"*": "x"}, body=b"{}")
        self.assertEqual(ctx.exception.status, 409)
        api = AutomationApi(self.engine, FlowStore(self.tmp), data_dir=self.tmp)
        with self.assertRaises(ApiError) as ctx:
            api.route_form_submit(params={"*": "form-nope"},
                                  body=json.dumps({"values": {"city": "x"}}).encode())
        self.assertEqual(ctx.exception.status, 404)
        with self.assertRaises(ApiError) as ctx:
            api.route_secret_status(params={"*": "sec_nope"})
        self.assertEqual(ctx.exception.status, 404)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
