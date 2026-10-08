"""Tests for automation/engine.py using a fake control backend.

The fake only replaces the process boundary (browser_control.sh / xdotool), so
the sequencing, error handling, pause, confirmation and repeat logic under test
is the real code that runs on Colab.
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from automation import schema  # noqa: E402
from automation.engine import (  # noqa: E402
    TERMINAL_STATES, AutomationEngine, ControlBackend,
)


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeBackend(ControlBackend):
    """Implements the primitive layer without touching X."""

    def __init__(self):
        super().__init__(script_path="/nonexistent/browser_control.sh")
        self.calls = []
        self.fail_on = set()
        self.page_text_value = ""
        self.shell_result = (0, "shell-ok", "")

    def _record(self, name, *args):
        self.calls.append((name,) + args)
        if name in self.fail_on:
            return 1, "", "%s failed" % name
        return 0, "ok", ""

    def click(self, x, y, button="left", clicks=1):
        return self._record("click", x, y, button, clicks)

    def move(self, x, y):
        return self._record("move", x, y)

    def drag(self, x1, y1, x2, y2, button="left"):
        return self._record("drag", x1, y1, x2, y2, button)

    def type_text(self, text):
        return self._record("type", text)

    def paste(self, text):
        return self._record("paste", text)

    def key(self, keys):
        return self._record("key", list(keys))

    def scroll(self, amount, x=None, y=None):
        return self._record("scroll", amount, x, y)

    def goto_url(self, url):
        return self._record("goto_url", url)

    def focus_window(self, title):
        return self._record("focus_window", title)

    def screenshot(self, name):
        self._record("screenshot", name)
        return 0, os.path.join("/tmp", name), ""

    def shell(self, command, timeout=30.0):
        self._record("shell", command)
        return self.shell_result

    def page_text(self):
        self._record("page_text")
        return 0, self.page_text_value, ""

    @property
    def names(self):
        return [c[0] for c in self.calls]


def build_flow(steps, **settings):
    flow = {
        "schema": 1,
        "name": "test flow",
        "steps": steps,
        "settings": dict({"defaultDelayAfterMs": 0}, **settings),
    }
    normalized, errors = schema.validate_flow(flow)
    assert not errors, errors
    return normalized


def wait_until(predicate, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class EngineTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="automation-test-")
        self.clock = FakeClock()
        self.backend = FakeBackend()
        self.engine = AutomationEngine(self.backend, self.tmp,
                                       sleep=self.clock.sleep, clock=self.clock.time)
        self.engines = [self.engine]

    def tearDown(self):
        for engine in self.engines:
            if engine.busy():
                engine.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_to_completion(self, flow, timeout=5.0):
        self.engine.start(flow)
        ok = wait_until(lambda: self.engine.status()["status"] in
                        ("done", "error", "stopped"), timeout)
        self.assertTrue(ok, "run did not finish: %s" % self.engine.status()["status"])
        return self.engine.status()


class HappyPathTest(EngineTestBase):
    def test_executes_every_enabled_step_in_order(self):
        flow = build_flow([
            {"type": "goto_url", "url": "https://example.com"},
            {"type": "click", "x": 10, "y": 20},
            {"type": "paste", "text": "panda"},
            {"type": "key", "keys": ["Return"]},
            {"type": "scroll", "amount": 3},
        ])
        status = self.run_to_completion(flow)
        self.assertEqual(status["status"], "done")
        self.assertEqual(self.backend.names,
                         ["goto_url", "click", "paste", "key", "scroll"])
        self.assertEqual(self.backend.calls[1], ("click", 10, 20, "left", 1))
        self.assertEqual(self.backend.calls[3], ("key", ["Return"]))

    def test_double_click_uses_the_doubleclick_primitive(self):
        flow = build_flow([{"type": "double_click", "x": 5, "y": 6}])
        self.run_to_completion(flow)
        self.assertEqual(self.backend.calls[0], ("click", 5, 6, "left", 2))

    def test_disabled_steps_are_skipped(self):
        flow = build_flow([
            {"type": "click", "x": 1, "y": 1, "enabled": False},
            {"type": "click", "x": 2, "y": 2},
        ])
        status = self.run_to_completion(flow)
        self.assertEqual(status["status"], "done")
        self.assertEqual(self.backend.calls, [("click", 2, 2, "left", 1)])

    def test_delay_after_step_is_applied(self):
        before = self.clock.now
        flow = build_flow([{"type": "click", "x": 1, "y": 1, "delayAfterMs": 250}])
        self.run_to_completion(flow)
        self.assertGreaterEqual(self.clock.now - before, 0.25)

    def test_repeat_runs_every_pass(self):
        flow = build_flow([{"type": "click", "x": 1, "y": 1}], repeat=3)
        status = self.run_to_completion(flow)
        self.assertEqual(status["status"], "done")
        self.assertEqual(self.backend.names.count("click"), 3)
        self.assertEqual(status["passIndex"], 3)

    def test_run_artifacts_are_written_to_disk(self):
        flow = build_flow([{"type": "click", "x": 1, "y": 1}])
        status = self.run_to_completion(flow)
        run_dir = os.path.join(self.tmp, "runs", status["runId"])
        self.assertTrue(os.path.exists(os.path.join(run_dir, "log.jsonl")))
        with open(os.path.join(run_dir, "summary.json"), encoding="utf-8") as handle:
            summary = json.load(handle)
        self.assertEqual(summary["status"], "done")
        self.assertEqual(summary["steps"], 1)

    def test_screenshot_after_each_step_uses_the_run_directory(self):
        flow = build_flow([{"type": "click", "x": 1, "y": 1}], screenshotAfterEachStep=True)
        status = self.run_to_completion(flow)
        self.assertIn("screenshot", self.backend.names)
        shot = [c for c in self.backend.calls if c[0] == "screenshot"][0]
        self.assertTrue(shot[1].startswith("step-000-"))
        self.assertEqual(self.backend.screenshot_dir, None)  # restored afterwards
        self.assertTrue(os.path.isdir(os.path.join(self.tmp, "runs", status["runId"])))


class ErrorHandlingTest(EngineTestBase):
    def test_stop_on_error_halts_the_run(self):
        self.backend.fail_on.add("paste")
        flow = build_flow([
            {"type": "paste", "text": "boom"},
            {"type": "click", "x": 1, "y": 1},
        ])
        status = self.run_to_completion(flow)
        self.assertEqual(status["status"], "error")
        self.assertIn("failed with rc=1", status["error"])
        self.assertEqual(self.backend.names, ["paste"])

    def test_continue_on_error_keeps_going(self):
        self.backend.fail_on.add("paste")
        flow = build_flow([
            {"type": "paste", "text": "boom", "continueOnError": True},
            {"type": "click", "x": 1, "y": 1},
        ])
        status = self.run_to_completion(flow)
        self.assertEqual(status["status"], "done")
        self.assertEqual(self.backend.names, ["paste", "click"])

    def test_global_stop_on_error_false_keeps_going(self):
        self.backend.fail_on.add("paste")
        flow = build_flow([
            {"type": "paste", "text": "boom"},
            {"type": "click", "x": 1, "y": 1},
        ], stopOnError=False)
        status = self.run_to_completion(flow)
        self.assertEqual(status["status"], "done")
        self.assertEqual(self.backend.names, ["paste", "click"])


class ControlTest(EngineTestBase):
    def test_second_start_is_refused_while_busy(self):
        # A step waiting for a human blocks on a real event, so this is a
        # deterministic "still busy" state rather than a timing race.
        flow = build_flow([{"type": "click", "x": 1, "y": 1, "requiresConfirmation": True}])
        self.engine.start(flow)
        self.assertTrue(wait_until(lambda: self.engine.status()["status"] == "waiting"))
        with self.assertRaises(RuntimeError):
            self.engine.start(flow)
        self.engine.stop()
        self.assertTrue(wait_until(lambda: self.engine.status()["status"] == "stopped"))

    def test_stop_ends_the_run_early(self):
        # The second step blocks on a real event, so the stop lands while the
        # run is genuinely mid-flight instead of racing a fake clock.
        flow = build_flow([
            {"type": "click", "x": 1, "y": 1},
            {"type": "click", "x": 2, "y": 2, "requiresConfirmation": True},
        ])
        self.engine.start(flow)
        self.assertTrue(wait_until(lambda: self.engine.status()["status"] == "waiting"))
        self.engine.stop()
        self.assertTrue(wait_until(lambda: self.engine.status()["status"] == "stopped"))
        self.assertEqual(self.backend.calls, [("click", 1, 1, "left", 1)])

    def test_pause_and_resume(self):
        # This one needs a real clock: pausing only means something while an
        # actual delay is in progress.
        engine = AutomationEngine(self.backend, self.tmp)
        self.engines.append(engine)
        flow = build_flow([{"type": "wait", "ms": 600}, {"type": "click", "x": 2, "y": 2}])
        engine.start(flow)
        time.sleep(0.1)
        engine.pause()
        self.assertEqual(engine.status()["status"], "paused")
        self.assertTrue(engine.busy(), "a paused run still owns the desktop")
        time.sleep(0.3)
        self.assertEqual(self.backend.calls, [], "no progress while paused")
        engine.resume()
        self.assertTrue(wait_until(lambda: engine.status()["status"] == "done"))
        self.assertEqual([c[0] for c in self.backend.calls], ["click"])

    def test_pause_without_a_running_flow_raises(self):
        with self.assertRaises(RuntimeError):
            self.engine.pause()


class ConfirmationTest(EngineTestBase):
    def test_approved_step_runs(self):
        flow = build_flow([
            {"type": "click", "x": 9, "y": 9, "requiresConfirmation": True},
            {"type": "click", "x": 1, "y": 1},
        ])
        self.engine.start(flow)
        self.assertTrue(wait_until(lambda: self.engine.status()["status"] == "waiting"))
        status = self.engine.status()
        self.assertEqual(status["awaitingConfirmation"]["index"], 0)
        self.assertTrue(self.engine.busy(), "waiting for a human still owns the desktop")
        self.assertEqual(self.backend.calls, [], "nothing runs before confirmation")
        self.engine.confirm(True)
        self.assertTrue(wait_until(lambda: self.engine.status()["status"] == "done"))
        self.assertEqual(self.backend.names, ["click", "click"])

    def test_refused_confirmation_stops_the_run(self):
        flow = build_flow([
            {"type": "click", "x": 9, "y": 9, "requiresConfirmation": True},
            {"type": "click", "x": 1, "y": 1},
        ])
        self.engine.start(flow)
        self.assertTrue(wait_until(lambda: self.engine.status()["status"] == "waiting"))
        self.engine.confirm(False)
        self.assertTrue(wait_until(lambda: self.engine.status()["status"] == "stopped"))
        self.assertEqual(self.backend.calls, [], "a refused step must not run")

    def test_confirm_without_a_pending_step_raises(self):
        with self.assertRaises(RuntimeError):
            self.engine.confirm(True)


class WaitTest(EngineTestBase):
    def test_wait_advances_the_clock(self):
        before = self.clock.now
        self.run_to_completion(build_flow([{"type": "wait", "ms": 1200}]))
        self.assertGreaterEqual(self.clock.now - before, 1.2)

    def test_wait_for_text_succeeds_when_present(self):
        self.backend.page_text_value = "Hello search results for panda"
        status = self.run_to_completion(
            build_flow([{"type": "wait_for_text", "text": "panda", "timeoutMs": 3000}]))
        self.assertEqual(status["status"], "done")
        self.assertIn("page_text", self.backend.names)

    def test_wait_for_text_times_out(self):
        self.backend.page_text_value = "nothing relevant"
        status = self.run_to_completion(
            build_flow([{"type": "wait_for_text", "text": "panda", "timeoutMs": 1000}]))
        self.assertEqual(status["status"], "error")
        self.assertIn("not found", status["error"])

    def test_wait_for_text_absent_mode(self):
        self.backend.page_text_value = "the spinner is gone"
        status = self.run_to_completion(
            build_flow([{"type": "wait_for_text", "text": "loading", "absent": True,
                         "timeoutMs": 2000}]))
        self.assertEqual(status["status"], "done")


class MiscStepTest(EngineTestBase):
    def test_shell_step_is_dispatched(self):
        flow = build_flow([{"type": "shell", "command": "echo hi"}], allowShellSteps=True)
        status = self.run_to_completion(flow)
        self.assertEqual(status["status"], "done")
        self.assertEqual(self.backend.calls[0], ("shell", "echo hi"))

    def test_screenshot_step_reports_a_path(self):
        status = self.run_to_completion(build_flow([{"type": "screenshot", "name": "x.png"}]))
        self.assertEqual(status["status"], "done")
        self.assertEqual(self.backend.names, ["screenshot"])

    def test_drag_and_move_and_focus(self):
        flow = build_flow([
            {"type": "drag", "x1": 1, "y1": 2, "x2": 3, "y2": 4},
            {"type": "move", "x": 7, "y": 8},
            {"type": "focus_window", "title": "Google Chrome"},
        ])
        self.run_to_completion(flow)
        self.assertEqual(self.backend.names, ["drag", "move", "focus_window"])
        self.assertEqual(self.backend.calls[0], ("drag", 1, 2, 3, 4, "left"))

    def test_scroll_with_coordinates_is_dispatched(self):
        self.run_to_completion(build_flow([{"type": "scroll", "amount": -2, "x": 100, "y": 200}]))
        self.assertEqual(self.backend.calls, [("scroll", -2, 100, 200)])

    def test_history_records_finished_runs(self):
        self.run_to_completion(build_flow([{"type": "wait", "ms": 1}]))
        self.run_to_completion(build_flow([{"type": "wait", "ms": 1}]))
        history = self.engine.status()["history"]
        self.assertEqual(len(history), 2)


class RecordingBackend(ControlBackend):
    """Keeps the real ControlBackend logic but records the argv it would run."""

    def __init__(self):
        super().__init__(script_path="/usr/bin/true")
        self.argv = []

    def control(self, args, timeout=None):
        self.argv.append([str(a) for a in args])
        if args and args[0] == "screenshot":
            return 0, "/shots/%s" % args[1], ""
        return 0, "ok", ""


class BackendCommandTest(unittest.TestCase):
    """The real ControlBackend must build the exact browser_control.sh calls."""

    def setUp(self):
        self.backend = RecordingBackend()

    def test_click_maps_to_the_click_subcommand(self):
        self.backend.click(683, 384, "left", 1)
        self.assertEqual(self.backend.argv, [["click", "683", "384", "left"]])

    def test_double_click_uses_doubleclick(self):
        self.backend.click(10, 20, "left", 2)
        self.assertEqual(self.backend.argv, [["doubleclick", "10", "20"]])

    def test_many_clicks_move_once_then_click_repeatedly(self):
        self.backend.click(1, 2, "right", 4)
        self.assertEqual(self.backend.argv[0], ["move", "1", "2"])
        self.assertEqual(self.backend.argv[1:], [["click", "1", "2", "right"]] * 4)

    def test_scroll_moves_the_pointer_first_when_coordinates_are_given(self):
        self.backend.scroll(-2, 100, 200)
        self.assertEqual(self.backend.argv, [["move", "100", "200"], ["scroll", "-2"]])

    def test_scroll_without_coordinates_does_not_move(self):
        self.backend.scroll(3)
        self.assertEqual(self.backend.argv, [["scroll", "3"]])

    def test_primitives_map_to_documented_subcommands(self):
        self.backend.drag(1, 2, 3, 4, "middle")
        self.backend.type_text("hello world")
        self.backend.paste("متن فارسی")
        self.backend.key(["ctrl", "l"])
        self.backend.goto_url("https://example.com")
        self.backend.focus_window("Google Chrome")
        self.backend.screenshot("a.png")
        self.backend.windows()
        self.backend.active_window()
        self.backend.position()
        self.assertEqual(self.backend.argv, [
            ["drag", "1", "2", "3", "4", "middle"],
            ["type", "hello world"],
            ["paste", "متن فارسی"],
            ["key", "ctrl", "l"],
            ["url", "https://example.com"],
            ["focus", "Google Chrome"],
            ["screenshot", "a.png"],
            ["windows"],
            ["active"],
            ["position"],
        ])

    def test_missing_script_is_reported_not_raised(self):
        backend = ControlBackend(script_path="/nonexistent/browser_control.sh")
        rc, _, err = backend.click(1, 2)
        self.assertEqual(rc, 127)
        self.assertIn("control script not found", err)

    def test_env_carries_display_and_screenshot_dir(self):
        backend = ControlBackend(script_path="x", display=":9", screenshot_dir="/shots")
        env = backend.env()
        self.assertEqual(env["DISPLAY"], ":9")
        self.assertEqual(env["BROWSER_SCREENSHOT_DIR"], "/shots")

    def test_quoting_helper_is_shell_safe(self):
        from automation.engine import quote_command
        self.assertEqual(quote_command(["paste", "a b; rm -rf /"]),
                         "paste 'a b; rm -rf /'")


if __name__ == "__main__":
    unittest.main(verbosity=2)


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class RunResultsTest(EngineTestBase):
    """The per-step result rows are what the AI reads to find the failure."""

    def test_every_executed_step_gets_a_result_row(self):
        self.backend.fail_on = {"type"}
        status = self.run_to_completion(build_flow([
            {"type": "click", "x": 1, "y": 2},
            {"type": "paste", "text": "سلام"},
            {"type": "type", "text": "boom"},
            {"type": "click", "x": 3, "y": 4},
        ]))

        rows = status["results"]
        self.assertEqual([r["index"] for r in rows], [0, 1, 2])
        self.assertEqual([r["status"] for r in rows], ["ok", "ok", "error"])
        self.assertEqual(rows[2]["type"], "type")
        self.assertTrue(rows[2]["error"], "the failing step must say why")
        self.assertIsInstance(rows[0]["durationMs"], int)
        self.assertEqual(status["status"], "error")

    def test_disabled_steps_are_recorded_as_skipped(self):
        status = self.run_to_completion(build_flow([
            {"type": "click", "x": 1, "y": 2, "enabled": False},
            {"type": "click", "x": 3, "y": 4},
        ]))
        self.assertEqual([r["status"] for r in status["results"]], ["skipped", "ok"])

    def test_summary_on_disk_carries_the_rows(self):
        status = self.run_to_completion(build_flow([{"type": "click", "x": 1, "y": 2}]))
        path = os.path.join(self.tmp, "runs", status["runId"], "summary.json")
        self.assertTrue(os.path.exists(path), "summary.json was not written")
        with open(path, encoding="utf-8") as handle:
            summary = json.load(handle)
        self.assertEqual(summary["executed"], 1)
        self.assertEqual(summary["failed"], 0)
        self.assertEqual(len(summary["results"]), 1)


class RealProcessTest(unittest.TestCase):
    """Runs the real ControlBackend against real scripts, no fakes.

    These cover the two ways a step used to hang: a helper that blocks reading
    stdin, and a helper that forks a survivor holding the captured pipes.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.script = os.path.join(self.tmp, "control.sh")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _backend(self, body, timeout=20.0):
        with open(self.script, "w") as handle:
            handle.write("#!/bin/sh\n" + body)
        os.chmod(self.script, 0o755)
        return ControlBackend(self.script, timeout=timeout)

    def test_stdin_is_closed_so_a_helper_cannot_block_on_it(self):
        backend = self._backend("read line || true; printf 'done\\n'")
        started = time.time()
        rc, out, _ = backend.control(["anything"])
        self.assertLess(time.time() - started, 10, "control() blocked on stdin")
        self.assertEqual(rc, 0)
        self.assertEqual(out, "done")

    def test_paste_returns_without_waiting_for_the_clipboard_owner(self):
        # xclip forks a process that stays alive to serve the clipboard. When it
        # inherited the captured stdout/stderr the caller never saw EOF, so the
        # step sat until its 60s timeout and reported rc=124 even though the
        # paste itself had already happened.
        bin_dir = os.path.join(self.tmp, "bin")
        os.makedirs(bin_dir)
        stubs = {
            "xdpyinfo": "#!/bin/sh\nexit 0\n",
            # The survivor must inherit stdout/stderr, exactly like the real
            # xclip clipboard owner does.
            "xclip": "#!/bin/sh\ncat >/dev/null\nsleep 20 &\nexit 0\n",
            "xdotool": "#!/bin/sh\nexit 0\n",
        }
        for name, body in stubs.items():
            path = os.path.join(bin_dir, name)
            with open(path, "w") as handle:
                handle.write(body)
            os.chmod(path, 0o755)

        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = bin_dir + os.pathsep + old_path
        try:
            backend = ControlBackend(os.path.join(REPO_ROOT, "browser_control.sh"),
                                     timeout=15.0)
            started = time.time()
            rc, _, err = backend.paste("سلام")
            elapsed = time.time() - started
        finally:
            os.environ["PATH"] = old_path

        self.assertEqual(rc, 0, err)
        self.assertLess(elapsed, 10, "paste blocked for %.1fs" % elapsed)

    def test_stop_kills_a_step_that_is_stuck_in_a_subprocess(self):
        # Regression: the stop button only set a flag, so a step blocked in a
        # subprocess ignored it until the step timed out.
        backend = self._backend("sleep 30")
        engine = AutomationEngine(backend, self.tmp)
        flow, errors = schema.validate_flow(
            {"name": "stuck", "steps": [{"type": "click", "x": 1, "y": 1}]})
        self.assertEqual(errors, [])
        engine.start(flow)
        for _ in range(100):
            if backend._proc is not None:
                break
            time.sleep(0.02)
        self.assertIsNotNone(backend._proc, "the step never reached the script")

        started = time.time()
        engine.stop()
        deadline = time.time() + 10
        status = engine.status()["status"]
        while time.time() < deadline and status not in TERMINAL_STATES:
            time.sleep(0.05)
            status = engine.status()["status"]
        elapsed = time.time() - started
        self.assertIn(status, TERMINAL_STATES, "stop did not interrupt the running step")
        self.assertLess(elapsed, 8, "stop took %.1fs" % elapsed)
