"""Round 26: the old JSON step language converts to honest python.

Two promises are tested here:
  1. every old step type becomes the python line it should become
     (golden code, ${form.*} lookups, uniform Persian labels);
  2. a converted flow drives the desk EXACTLY like the old flow did
     (same backend calls, same typing delays, same failures).
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from automation import convert, schema  # noqa: E402
from automation.engine import AutomationEngine  # noqa: E402

from tests.test_engine import (  # noqa: E402
    FakeBackend, FakeClock, FakeOpsAgent, build_flow, wait_until)


class CodeTest(unittest.TestCase):
    def code(self, step):
        return convert.step_to_code(step)[0]

    def test_desk_primitives_become_checked_one_liners(self):
        self.assertEqual(self.code({"type": "click", "x": 100, "y": 200}),
                         "check(click(100, 200))")
        self.assertEqual(
            self.code({"type": "click", "x": 1, "y": 2,
                       "button": "right", "clicks": 2}),
            "check(click(1, 2, button='right', clicks=2))")
        self.assertEqual(self.code({"type": "double_click", "x": 5, "y": 6}),
                         "check(double_click(5, 6))")
        self.assertEqual(self.code({"type": "move", "x": 7, "y": 8}),
                         "check(move(7, 8))")
        self.assertEqual(
            self.code({"type": "drag", "x1": 1, "y1": 2, "x2": 3, "y2": 4}),
            "check(drag(1, 2, 3, 4))")
        self.assertEqual(self.code({"type": "scroll", "amount": 3}),
                         "check(scroll(3))")
        self.assertEqual(self.code({"type": "wait", "ms": 350}), "wait(0.35)")
        self.assertEqual(self.code({"type": "wait", "ms": 2000}), "wait(2)")
        self.assertEqual(self.code({"type": "key", "keys": ["Return"]}),
                         "check(press_key(['Return']))")
        self.assertEqual(
            self.code({"type": "goto_url", "url": "https://x.dev/"}),
            "check(goto('https://x.dev/'))")
        self.assertEqual(self.code({"type": "focus_window", "title": "Chat"}),
                         "check(focus_window('Chat'))")
        self.assertEqual(self.code({"type": "shell", "command": "ls"}),
                         "check(shell('ls'))")
        self.assertEqual(self.code({"type": "screenshot"}),
                         "check(screenshot())")
        self.assertEqual(self.code({"type": "screenshot", "name": "manual"}),
                         "check(screenshot('manual'))")
        self.assertEqual(self.code({"type": "capture_text"}),
                         "log(check(capture_text()))")
        self.assertEqual(self.code({"type": "capture_text", "limit": 8000}),
                         "log(check(capture_text(limit=8000)))")
        self.assertEqual(self.code({"type": "type", "text": "سلام"}),
                         "check(type_text('سلام'))")
        self.assertEqual(
            self.code({"type": "type", "text": "fast one",
                       "typingMode": "fast"}),
            "check(type_text('fast one', mode='fast'))")
        self.assertEqual(self.code({"type": "paste", "text": "panda"}),
                         "check(paste('panda'))")
        self.assertEqual(
            self.code({"type": "run_operation", "operation": "op-two"}),
            "check(run_operation('op-two'))")

    def test_special_steps_map_to_their_wrappers(self):
        self.assertEqual(
            self.code({"type": "wait_for_text", "text": "خوش آمدید"}),
            "check(wait_for_text('خوش آمدید'))")
        self.assertEqual(
            self.code({"type": "wait_for_text", "text": "bye",
                       "timeoutMs": 30000, "absent": True}),
            "check(wait_for_text('bye', timeout_ms=30000, absent=True))")
        self.assertEqual(
            self.code({"type": "pause_for_human_verification",
                       "prompt": "ورود را تأیید کنید"}),
            "check(human_verify('ورود را تأیید کنید'))")
        self.assertEqual(
            self.code({"type": "agent_open", "provider": "deepseek"}),
            "check(open_agent(provider='deepseek'))")
        self.assertEqual(
            self.code({"type": "agent_ask", "prompt": "سلام",
                       "saveAs": "note"}),
            "check(ask_agent('سلام', save_as=True))")
        self.assertEqual(
            self.code({"type": "agent_ask", "prompt": "s",
                       "provider": "deepseek", "timeout": 45,
                       "freshChat": True}),
            "check(ask_agent('s', provider='deepseek', timeout=45.0, "
            "fresh_chat=True))")
        self.assertEqual(
            self.code({"type": "captcha_solve", "label": "recaptcha",
                       "autoClick": True}),
            "check(solve_captcha(label='recaptcha', auto_click=True))")
        self.assertEqual(
            self.code({"type": "progress_notice", "title": "تقریباً تمام",
                       "level": "success"}),
            "check(notify_progress('تقریباً تمام', level='success'))")

    def test_forms_and_secrets_keep_their_schema(self):
        code = self.code({
            "type": "interactive_form", "title": "اطلاعات",
            "fields": [{"id": "prompt", "type": "textarea",
                        "label": "پرامپت"}],
            "output": "chatgpt"})
        self.assertIn("answers = check(form_collect(", code)
        self.assertIn("title='اطلاعات'", code)
        self.assertIn("'id': 'prompt'", code)
        self.assertIn("output='chatgpt'", code)
        code = self.code({
            "type": "secure_input", "title": "کلید",
            "fields": [{"id": "api_key", "type": "password"}],
            "policy": {"scope": "run"}, "output": "creds"})
        self.assertIn("handles = check(secure_collect(", code)
        self.assertIn("policy={'scope': 'run'}", code)
        self.assertIn("output='creds'", code)
        self.assertEqual(
            self.code({"type": "secure_http_request", "method": "POST",
                       "url": "https://api.example/x",
                       "secretRef": "creds.api_key",
                       "json": {"a": 1}, "saveAs": "resp", "timeout": 20}),
            "check(http_request(method='POST', url='https://api.example/x', "
            "secret_ref='creds.api_key', json_body={'a': 1}, "
            "save_as='resp', timeout=20))")

    def test_form_references_become_python_lookups(self):
        self.assertEqual(
            self.code({"type": "paste", "text": "${form.chatgpt.prompt}"}),
            "check(paste(forms['chatgpt']['prompt']))")
        code = self.code({"type": "type",
                          "text": "پاسخ ${form.chatgpt.prompt} بود"})
        self.assertIn("f\"پاسخ {forms['chatgpt']['prompt']} بود\"", code)

    def test_labels_are_uniform_persian(self):
        self.assertEqual(
            convert.label_for({"type": "click", "x": 100, "y": 200}),
            "کلیک در (100، 200)")
        self.assertEqual(convert.label_for({"type": "wait", "ms": 350}),
                         "صبر 0.35 ثانیه")
        self.assertEqual(
            convert.label_for({"type": "run_operation",
                               "operation": "دو گام"}),
            "اجرای عملیات «دو گام»")
        self.assertEqual(
            convert.label_for({"type": "wait_for_text", "text": "x",
                               "absent": True}),
            "انتظار برای نبودِ «x»")
        self.assertEqual(convert.label_for({"type": "python", "code": "x"}),
                         "کد پایتون")

    def test_convert_flow_is_idempotent_and_keeps_common_fields(self):
        flow = build_flow([
            {"type": "click", "x": 1, "y": 2, "note": "یادداشت"},
            {"type": "python", "code": "log('hi')", "label": "سلام"},
        ])
        once = convert.convert_flow(flow)
        twice = convert.convert_flow(once)
        self.assertEqual(once, twice)
        self.assertFalse(convert.needs_conversion(once))
        self.assertTrue(convert.needs_conversion(flow))
        first = once["steps"][0]
        self.assertEqual(first["type"], "python")
        self.assertEqual(first["note"], "یادداشت")
        self.assertEqual(first["id"], flow["steps"][0]["id"])
        self.assertTrue(first["enabled"])
        self.assertEqual(once["steps"][1]["code"], "log('hi')")
        self.assertEqual(once["steps"][1]["label"], "سلام")
        # the converted flow validates with the ordinary schema
        _, errors = schema.validate_flow(once)
        self.assertEqual(errors, [])

    def test_unknown_types_are_refused_not_guessed(self):
        self.assertRaises(ValueError, convert.step_to_code,
                          {"type": "dance"})


class EngineParityTest(unittest.TestCase):
    """A converted flow must drive the desk exactly like the old one did."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="convert-test-")
        self.clock = FakeClock()
        self.backend = FakeBackend()
        self.engine = AutomationEngine(self.backend, self.tmp,
                                       sleep=self.clock.sleep,
                                       clock=self.clock.time)

    def tearDown(self):
        if self.engine.busy():
            self.engine.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_converted(self, steps, agent=None, **settings):
        if agent is not None:
            self.engine = AutomationEngine(
                self.backend, self.tmp, sleep=self.clock.sleep,
                clock=self.clock.time, agent=agent)
        flow = convert.convert_flow(build_flow(steps, **settings))
        self.engine.start(flow)
        ok = wait_until(lambda: self.engine.status()["status"] in
                        ("done", "error", "stopped"), 5.0)
        self.assertTrue(ok, "run did not finish: %s"
                        % self.engine.status()["status"])
        return self.engine.status()

    def test_primitives_hit_the_backend_like_the_old_types(self):
        status = self.run_converted([
            {"type": "goto_url", "url": "https://example.com"},
            {"type": "click", "x": 10, "y": 20},
            {"type": "type", "text": "first"},
            {"type": "type", "text": "second", "typingMode": "fast"},
            {"type": "paste", "text": "panda"},
            {"type": "key", "keys": ["Return"]},
            {"type": "scroll", "amount": 3},
            {"type": "move", "x": 1, "y": 2},
            {"type": "double_click", "x": 5, "y": 6},
            {"type": "drag", "x1": 1, "y1": 1, "x2": 2, "y2": 2},
            {"type": "wait", "ms": 250},
        ])
        self.assertEqual(status["status"], "done")
        self.assertEqual(self.backend.names,
                         ["goto_url", "click", "type", "type", "paste",
                          "key", "scroll", "move", "click", "drag"])
        self.assertEqual(self.backend.calls[1], ("click", 10, 20, "left", 1))
        self.assertEqual(self.backend.calls[2], ("type", "first", 15))
        self.assertEqual(self.backend.calls[3], ("type", "second", 0))
        self.assertEqual(self.backend.calls[8], ("click", 5, 6, "left", 2))

    def test_the_flow_typing_mode_reaches_converted_type_steps(self):
        status = self.run_converted([{"type": "type", "text": "آروم"}],
                                    typingMode="low")
        self.assertEqual(status["status"], "done")
        self.assertEqual(self.backend.calls[0], ("type", "آروم", 50))

    def test_a_failing_primitive_still_fails_the_run(self):
        self.backend.fail_on.add("click")
        status = self.run_converted([{"type": "click", "x": 1, "y": 1}])
        self.assertEqual(status["status"], "error")
        self.assertIn("failed", status["error"])

    def test_captured_text_lands_in_the_result_row(self):
        self.backend.page_text_value = "Battle Arena"
        status = self.run_converted([{"type": "capture_text"}])
        self.assertEqual(status["status"], "done")
        self.assertIn("Battle Arena", status["results"][0]["text"] or "")

    def test_wait_for_text_uses_the_same_polling(self):
        self.backend.page_text_value = "welcome back"
        status = self.run_converted([
            {"type": "wait_for_text", "text": "Welcome", "timeoutMs": 2000}])
        self.assertEqual(status["status"], "done")

    def test_progress_notice_lands_in_the_log(self):
        status = self.run_converted([
            {"type": "progress_notice", "title": "تقریباً تمام",
             "level": "success"}])
        self.assertEqual(status["status"], "done")

    def test_run_operation_nests_from_python(self):
        agent = FakeOpsAgent({
            "op-two": {"id": "op-two", "name": "دو گام", "steps": [
                {"type": "click", "x": 3, "y": 4},
                {"type": "move", "x": 5, "y": 6}]},
        })
        status = self.run_converted(
            [{"type": "run_operation", "operation": "دو گام"}], agent=agent)
        self.assertEqual(status["status"], "done")
        self.assertEqual(self.backend.calls,
                         [("click", 3, 4, "left", 1), ("move", 5, 6)])

    def test_a_failing_nested_operation_fails_the_python_step(self):
        agent = FakeOpsAgent({
            "op-bad": {"id": "op-bad", "name": "خراب", "steps": [
                {"type": "click", "x": 3, "y": 4}]},
        })
        self.backend.fail_on.add("click")
        status = self.run_converted(
            [{"type": "run_operation", "operation": "op-bad"}], agent=agent)
        self.assertEqual(status["status"], "error")


if __name__ == "__main__":
    unittest.main(verbosity=2)
