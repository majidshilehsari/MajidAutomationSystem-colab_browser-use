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


CHAIN_FLOW = {
    "name": "chain",
    "steps": [
        {"type": "progress_notice", "title": "شروع", "level": "info"},
        {"type": "interactive_form", "title": "مشخصات",
         "fields": [
             {"id": "city", "type": "text", "label": "شهر", "required": True,
              "default": "اصفهان"},
             {"id": "count", "type": "number", "label": "تعداد", "required": True},
             {"id": "express", "type": "checkbox", "label": "فوری", "default": True},
             {"id": "kind", "type": "radio", "label": "نوع", "default": "a",
              "options": [{"value": "a"}, {"value": "b"}]}],
         "output": {"namespace": "req"}},
        {"type": "secure_input", "title": "کلید",
         "fields": [{"id": "api_key", "type": "secret", "label": "کلید"}],
         "policy": dict(POLICY), "output": {"handle": "creds"}},
        {"type": "secure_http_request", "secretRef": "creds.api_key",
         "method": "GET", "url": "https://api.example.dev/v1/data",
         "saveAs": "resp"},
    ],
}


class SubmitValidationTests(unittest.TestCase):
    """The submit endpoint must enforce the schema the human actually saw."""

    SCHEMA = {"fields": [
        {"id": "city", "type": "text", "required": True, "maxLength": 10},
        {"id": "count", "type": "number", "required": True, "min": 1, "max": 9},
        {"id": "express", "type": "checkbox"},
        {"id": "kind", "type": "radio", "options": [{"value": "a"}, {"value": "b"}]},
    ]}

    def validate(self, values):
        return AutomationEngine._validate_form_values(self.SCHEMA, values)

    def test_valid_submission_is_coerced(self):
        clean = self.validate({"city": "تهران", "count": "5",
                               "express": True, "kind": "b"})
        self.assertEqual(clean, {"city": "تهران", "count": 5,
                                 "express": True, "kind": "b"})

    def test_required_maxlength_options_and_unknown_keys_are_enforced(self):
        for bad in (
            {"city": "", "count": 2},                              # required
            {"city": "x" * 11, "count": 2},                        # maxLength
            {"city": "x", "count": 2, "kind": "z"},                # options
            {"city": "x", "count": 2, "smuggled": 1},              # unknown key
            {"city": "x", "count": 99},                            # max bound
            {"city": "x", "count": "abc"},                         # number type
        ):
            with self.assertRaises(ValueError, msg=str(bad)):
                self.validate(bad)


class RoundFourteenChainTests(unittest.TestCase):
    """The letter's mandatory end-to-end scenario through the real router."""

    TOKEN = "tok-e2e"
    # the server layer lowercases header names before dispatch
    HEADERS = {"x-automation-token": "tok-e2e"}

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.clock = FakeClock()
        self.backend = FakeBackend()
        self.engine = AutomationEngine(self.backend, self.tmp,
                                       sleep=self.clock.sleep, clock=self.clock.time)
        self.api = AutomationApi(self.engine, FlowStore(self.tmp),
                                 data_dir=self.tmp, token=self.TOKEN)
        self.opener = FakeOpener()

    def tearDown(self):
        if self.engine.busy():
            self.engine.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def dispatch(self, method, path, payload=None):
        body = json.dumps(payload).encode() if payload is not None else b""
        return self.api.handle(method, path, body=body, headers=dict(self.HEADERS))

    def wait_pending(self, kind):
        ok = wait_until(lambda: (self.engine.status().get("awaitingConfirmation")
                                 or {}).get("kind") == kind)
        self.assertTrue(ok, "the %s card never surfaced" % kind)
        return self.engine.status()["awaitingConfirmation"]

    def wait_done(self):
        ok = wait_until(lambda: self.engine.status()["status"] in TERMINAL_STATES)
        self.assertTrue(ok, "the run never finished")
        return self.engine.status()

    def test_every_registered_route_pattern_really_matches(self):
        # The letter's problem 3: the table knew /forms/*/submit but the old
        # matcher only understood a trailing '*', so it 404'd in production.
        from automation.api import _match
        for method, pattern, _handler in self.api.routes:
            sample = "".join("/" + ("sample-1" if seg == "*" else seg)
                             for seg in pattern.split("/") if seg)
            self.assertIsNotNone(_match(pattern, sample),
                                 "dead route in the table: %s %s" % (method, pattern))

    def test_the_full_chain_notice_form_secret_connector(self):
        with mock.patch("urllib.request.build_opener", return_value=self.opener):
            self.engine.start(CHAIN_FLOW)

            # 1+2: notice passes without stopping, the form card opens
            pending = self.wait_pending("form")
            form_id = pending["formId"]

            # a duplicate/unknown submit is refused with a clear error
            missing = self.dispatch("POST", "/forms/form-other-999/submit",
                                    {"values": {"city": "x"}})
            self.assertEqual(missing[0], 404)

            # 3+4+5: text changed, checkbox switched off, radio moved to 'b',
            #        submitted through the real router -> HTTP 200
            answer = self.dispatch("POST", "/forms/%s/submit" % form_id,
                                   {"values": {"city": "تهران", "count": 5,
                                               "express": False, "kind": "b"}})
            self.assertEqual(answer[0], 200, answer[2])
            again = self.dispatch("POST", "/forms/%s/submit" % form_id,
                                  {"values": {"city": "تهران", "count": 5}})
            self.assertIn(again[0], (404, 409), "a second submit must be refused")

            # server-side validation reaches the router as 400
            pending2 = self.wait_pending("secret")

            # an empty secret submit is refused and the card STAYS open
            empty = self.dispatch("POST", "/secrets/%s/submit" % pending2["formId"],
                                  {"values": {"api_key": ""}})
            self.assertEqual(empty[0], 400)
            self.assertEqual((self.engine.status().get("awaitingConfirmation")
                              or {}).get("kind"), "secret")

            # 7+8: the real submit only answers masked metadata
            answer = self.dispatch("POST", "/secrets/%s/submit" % pending2["formId"],
                                   {"values": {"api_key": "SUPERSECRET"}})
            self.assertEqual(answer[0], 200, answer[2])
            self.assertNotIn(b"SUPERSECRET", answer[2])
            handles = json.loads(answer[2].decode())["handles"]["api_key"]
            handle = handles["fullHandle"]
            self.assertTrue(handle.startswith("sec_"))

            # 9: the connector resolves secretRef -> GET with the policy header
            status = self.wait_done()
            self.assertEqual(status["status"], "done",
                             json.dumps(status["results"], ensure_ascii=False,
                                        default=str))
            self.assertEqual(self.opener.req.get_header("Authorization"),
                             "Bearer SUPERSECRET")
            self.assertEqual(self.opener.req.get_method(), "GET")

            # 6: the namespace carries exactly the values the human chose
            outputs = self.engine._state.form_outputs
            self.assertEqual(outputs["req"], {"city": "تهران", "count": 5,
                                              "express": False, "kind": "b"})
            self.assertEqual(outputs["resp"], {"ok": True})

            # 10: the use counter moved and status shows it, masked
            answer = self.dispatch("GET", "/secrets/%s/status" % handle)
            self.assertEqual(answer[0], 200)
            body = json.loads(answer[2].decode())
            self.assertEqual(body["uses"], 1)
            self.assertEqual(body["maxUses"], POLICY["maxUses"])
            self.assertNotIn("SUPERSECRET", answer[2].decode())

            # 11: revoke, then the next acquire is refused for good
            answer = self.dispatch("POST", "/secrets/%s/revoke" % handle)
            self.assertEqual(answer[0], 200)
            from automation.vault import VaultError
            with self.assertRaises(VaultError) as ctx:
                self.engine.vault.acquire(handle, "GET",
                                          "https://api.example.dev/v1/data")
            self.assertEqual(ctx.exception.status, 410)

            # 12: the secret appears in no observable output
            sweep = json.dumps({
                "status": self.engine.status(),
                "results": self.engine._state.results,
                "entries": self.engine._state.entries,
                "outputs": self.engine._state.form_outputs,
            }, ensure_ascii=False, default=str)
            self.assertNotIn("SUPERSECRET", sweep)

    def test_secret_ref_that_never_ran_is_a_clear_error(self):
        self.engine.start({"name": "lonely-connector", "steps": [
            {"type": "secure_http_request", "secretRef": "creds.api_key",
             "method": "GET", "url": "https://api.example.dev/v1/data"},
        ]})
        status = self.wait_done()
        self.assertEqual(status["status"], "error")
        self.assertIn("did not resolve", status["results"][0]["error"])

    def test_secure_input_never_reports_success_without_a_handle(self):
        # Defense in depth: even if a submit reached the step empty, the step
        # fails instead of handing {} to the next stage (run-1791705532781).
        self.engine.start(SECRET_FLOW)
        form_id = self.wait_pending("secret")["formId"]
        self.engine._form_values = {}
        self.engine._confirmed = True
        self.engine._wakeup.set()
        status = self.wait_done()
        self.assertEqual(status["status"], "error")
        self.assertIn("no secret reached the vault", status["results"][0]["error"])
