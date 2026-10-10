"""Tests for the coworker agent: store, scheduler, script gate, Telegram,
captcha chain, model access and the HTTP surface that ties them together.

Nothing here touches the network, an X server or a browser. Every transport is
faked at its narrowest point - a fake `cdp` module, a fake `urlopen`, a fake
notifier - so what is being tested is this project's logic and not somebody
else's availability.

Two things are tested harder than the rest, because they are the two ways this
feature could quietly hurt somebody:

* the script gate - code written by a model that reads untrusted web pages must
  not run until a human approves it, must lose that approval when its code
  changes, and must not inherit the platform's secrets;
* secret handling - a key must never reach the database, the audit log, a
  command line, or an API response.
"""

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from automation import schema  # noqa: E402
from automation.agent import API_INDEX, AgentError, AgentHub  # noqa: E402
from automation.agent_store import AgentStore  # noqa: E402
from automation.ai_browser import (AiBrowser, AiBrowserError,  # noqa: E402
                                   DEFAULT_PROVIDERS)
from automation.api import AutomationApi, FlowStore  # noqa: E402
from automation.captcha import CaptchaSolver  # noqa: E402
from automation.engine import AutomationEngine  # noqa: E402
from automation.llm import LlmClient, LlmError, extract_json  # noqa: E402
from automation.scheduler import (Scheduler, SchedulerBusy, next_cron,  # noqa: E402
                                  parse_interval, parse_moment)
from automation.scripts import ScriptRunner, build_env  # noqa: E402
from automation.telegram_client import (Notifier, TelegramBot,  # noqa: E402
                                        TelegramError)

TOKEN = "tok-agent-test"
PUBLIC_BASE = "https://af9833d9.eu-center.hostim.dev"


class FakeClock:
    """A clock the tests advance by hand, so nothing has to wait in real time."""

    def __init__(self, start=1_760_000_000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeBackend:
    """Stands in for ControlBackend: records calls, touches no display."""

    def __init__(self):
        self.calls = []

    def _record(self, name, *args):
        self.calls.append((name,) + args)
        return (0, "", "")

    def click(self, x, y, button="left", clicks=1):
        return self._record("click", x, y, button, clicks)

    def move(self, x, y):
        return self._record("move", x, y)

    def paste(self, text):
        return self._record("paste", text)

    def type_text(self, text, delay_ms=15):
        return self._record("type", text)

    def key(self, keys):
        return self._record("key", tuple(keys))

    def screenshot(self, name):
        return self._record("screenshot", name)

    def goto_url(self, url):
        return self._record("goto_url", url)

    def control(self, args, timeout=None):
        return self._record("control", tuple(args))


class FakeLlm:
    """Returns a canned reply, and records what it was asked."""

    def __init__(self, reply, provider="ai-browser"):
        self.reply = reply
        self.provider = provider
        self.calls = []

    def complete(self, prompt, system="", images=None, want_json=False, timeout=None,
                 chat_name=None):
        self.calls.append({"prompt": prompt, "system": system, "images": images,
                           "want_json": want_json, "chat_name": chat_name})
        return {"text": self.reply, "provider": self.provider, "elapsed": 0.2,
                "source": {"url": "https://chat.example/"}}

    def describe(self):
        return {"provider": self.provider, "configured": True}

    def chat_provider(self):
        return "deepseek"

    def chat_names(self):
        return ["deepseek", "generic"]


EMPTY_PAGE = {"count": 0, "texts": [], "url": "https://chat.example/", "title": "chat"}


def page(*texts, **extra):
    """One read of a chat page: `count` answers, the last of them still growing."""
    state = {"count": len(texts), "texts": list(texts),
             "url": "https://chat.example/", "title": "chat"}
    state.update(extra)
    return state


class TickingClock:
    """A clock whose injected sleep advances it, so a timeout can be tested."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeCdp:
    """A CDP module that answers from a script instead of a browser.

    The reply is chosen from the expression rather than from the call order,
    because `ask()` reads the page a number of times that depends on the page
    itself - a positional fake would break every time the polling changes.
    """

    def __init__(self, pages=None, page_states=None, inject=None, fail=False):
        self.pages = pages if pages is not None else [{"type": "page", "id": "1",
                                                       "url": "https://chat.example/",
                                                       "title": "chat",
                                                       "webSocketDebuggerUrl": "ws://x"}]
        self.page_states = list(page_states) if page_states else [EMPTY_PAGE]
        self.inject = ({"ok": True, "how": "enter", "selector": "#chat-input"}
                       if inject is None else inject)
        self.fail = fail
        self.reads = 0
        self.expressions = []

    def list_targets(self, port=9222, timeout=3.0):
        if self.fail:
            raise OSError("connection refused")
        return self.pages

    def evaluate(self, expression, port=9222, timeout=8.0):
        if self.fail:
            raise OSError("connection refused")
        self.expressions.append(expression)
        if "no input element matched" in expression:
            value = self.inject                            # the submit script
        elif "node.click(); return JSON.stringify({ok:true" in expression:
            value = {"ok": True, "used": "a[href='/']"}    # the new-chat script
        else:                                              # a read of the answers
            value = self.page_states[min(self.reads, len(self.page_states) - 1)]
            self.reads += 1
        return {"value": json.dumps(value, ensure_ascii=False),
                "url": "https://chat.example/", "title": "chat", "targetId": "1"}


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="agent-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.clock = FakeClock()


# ---------------------------------------------------------------------------
# the store
# ---------------------------------------------------------------------------
class AgentStoreTest(TempDirCase):
    def setUp(self):
        super().setUp()
        self.store = AgentStore(self.tmp, clock=self.clock)
        self.addCleanup(self.store.close)

    def test_secret_values_never_reach_the_database(self):
        self.store.set_setting("telegram.botToken", "12345:ABCDEF", secret=True)
        with open(self.store.db_path, "rb") as handle:
            blob = handle.read()
        self.assertNotIn(b"ABCDEF", blob,
                         "a secret landed in the database file, which gets backed up")
        self.assertEqual(self.store.get_secret("telegram.botToken"), "12345:ABCDEF")

    def test_the_secret_file_is_locked_down(self):
        self.store.set_setting("ai.apiKey", "sk-secret", secret=True)
        mode = os.stat(self.store.secrets_path).st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_listing_settings_masks_secret_values(self):
        self.store.set_setting("ai.apiKey", "sk-secret", secret=True)
        self.store.set_setting("ai.model", "deepseek-chat")
        listed = self.store.list_settings()
        self.assertEqual(listed["ai.model"], "deepseek-chat")
        self.assertEqual(listed["ai.apiKey"], {"secret": True, "set": True})
        self.assertNotIn("sk-secret", json.dumps(listed))

    def test_the_audit_log_records_the_key_name_but_not_its_value(self):
        self.store.set_setting("telegram.botToken", "12345:ABCDEF", secret=True)
        rows = self.store.list_audit()
        self.assertTrue(rows, "storing a secret was not audited at all")
        blob = json.dumps(rows)
        self.assertIn("telegram.botToken", blob)
        self.assertNotIn("ABCDEF", blob)

    def test_a_secret_file_is_written_through_a_0600_temporary_file(self):
        self.store.set_secret("a", "1")
        self.store.set_secret("b", "2")
        self.assertEqual(self.store.get_secret("a"), "1")
        self.assertEqual(self.store.get_secret("b"), "2")
        self.assertFalse(os.path.exists(self.store.secrets_path + ".tmp"))

    def test_editing_an_approved_script_cancels_the_approval(self):
        script = self.store.create_script({"name": "s", "code": "print(1)"})
        self.store.set_script_status(script["id"], "approved")
        self.assertEqual(self.store.get_script(script["id"])["status"], "approved")
        # The gate would be worthless if new code could ride an old approval.
        updated = self.store.update_script(script["id"], {"code": "print(2)"})
        self.assertEqual(updated["status"], "pending")
        self.assertIsNone(updated["approved_at"])

    def test_editing_only_the_name_keeps_the_approval(self):
        script = self.store.create_script({"name": "s", "code": "print(1)"})
        self.store.set_script_status(script["id"], "approved")
        updated = self.store.update_script(script["id"], {"name": "renamed"})
        self.assertEqual(updated["status"], "approved")

    def test_jobs_survive_a_new_store_instance(self):
        self.store.upsert_job({"name": "morning", "kind": "cron",
                               "schedule": "30 7 * * *", "target": "Arena"})
        self.store.close()
        reopened = AgentStore(self.tmp, clock=self.clock)
        self.addCleanup(reopened.close)
        jobs = reopened.list_jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["name"], "morning")
        self.assertEqual(jobs[0]["payload"], {})
        self.assertTrue(jobs[0]["enabled"])

    def test_the_kill_switch_round_trips(self):
        self.assertFalse(self.store.kill_switch_engaged())
        self.store.set_kill_switch(True)
        self.assertTrue(self.store.kill_switch_engaged())
        self.store.set_kill_switch(False)
        self.assertFalse(self.store.kill_switch_engaged())

    def test_notes_are_listed_newest_first_and_can_be_filtered(self):
        self.store.add_note("first", kind="note")
        self.clock.advance(1)
        self.store.add_note("second", kind="captcha")
        self.assertEqual([n["body"] for n in self.store.list_notes()],
                         ["second", "first"])
        self.assertEqual([n["body"] for n in self.store.list_notes(kind="captcha")],
                         ["second"])


# ---------------------------------------------------------------------------
# scheduling arithmetic
# ---------------------------------------------------------------------------
class ScheduleMathTest(unittest.TestCase):
    def setUp(self):
        # Saturday 2026-10-10 06:00 local time.
        self.base = time.mktime(time.strptime("2026-10-10 06:00:00",
                                              "%Y-%m-%d %H:%M:%S"))

    def test_cron_picks_the_next_matching_minute(self):
        moment = next_cron("30 7 * * *", self.base)
        self.assertEqual(time.strftime("%Y-%m-%d %H:%M %a", time.localtime(moment)),
                         "2026-10-10 07:30 Sat")

    def test_cron_steps_and_ranges(self):
        self.assertEqual(time.strftime("%H:%M", time.localtime(
            next_cron("*/15 * * * *", self.base))), "06:15")
        self.assertEqual(time.strftime("%H:%M", time.localtime(
            next_cron("0 9-17 * * *", self.base))), "09:00")

    def test_cron_rolls_over_to_the_next_matching_day(self):
        moment = next_cron("0 5 * * *", self.base)
        self.assertEqual(time.strftime("%Y-%m-%d %H:%M", time.localtime(moment)),
                         "2026-10-11 05:00")

    def test_cron_uses_the_or_rule_when_both_day_fields_are_restricted(self):
        # Standard cron: "0 0 1 * 1" means the 1st of the month OR any Monday,
        # not both. Getting this wrong silently skips most runs.
        moment = next_cron("0 0 1 * 1", self.base)
        self.assertEqual(time.strftime("%Y-%m-%d %a", time.localtime(moment)),
                         "2026-10-12 Mon")

    def test_cron_rejects_nonsense(self):
        for expression in ("", "* * *", "99 * * * *", "* * * * 9", "a b c d e",
                           "* * 0 * *"):
            with self.subTest(expression=expression):
                with self.assertRaises(ValueError):
                    next_cron(expression, self.base)

    def test_intervals(self):
        self.assertEqual(parse_interval("90"), 90.0)
        self.assertEqual(parse_interval("5m"), 300.0)
        self.assertEqual(parse_interval("2h"), 7200.0)
        self.assertEqual(parse_interval("1d"), 86400.0)
        for bad in ("", "0", "abc", "-5m"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    parse_interval(bad)

    def test_moments_accept_a_timestamp_and_a_local_datetime(self):
        self.assertEqual(parse_moment("1760000000"), 1760000000.0)
        self.assertEqual(time.strftime("%Y-%m-%d %H:%M", time.localtime(
            parse_moment("2026-12-01 07:30"))), "2026-12-01 07:30")
        with self.assertRaises(ValueError):
            parse_moment("sometime next week")

    def test_a_moment_in_the_past_never_runs_again(self):
        job = {"kind": "at", "schedule": str(self.base - 10)}
        from automation.scheduler import next_run
        self.assertIsNone(next_run(job, self.base))


class SchedulerTest(TempDirCase):
    def setUp(self):
        super().setUp()
        self.store = AgentStore(self.tmp, clock=self.clock)
        self.addCleanup(self.store.close)
        self.fired = []

    def make(self, runner=None, sleep=lambda _seconds: None):
        return Scheduler(self.store, runner or self.record, clock=self.clock,
                         sleep=sleep, interval=0.01)

    def record(self, job):
        self.fired.append(job["name"])
        return {"status": "done"}

    def test_a_due_job_fires_and_is_rescheduled(self):
        job = self.store.upsert_job({"name": "every-minute", "kind": "every",
                                     "schedule": "60", "target": "x"})
        scheduler = self.make()
        scheduler.ensure_schedules()
        self.assertEqual(scheduler.tick(), [])
        self.clock.advance(61)
        results = scheduler.tick()
        self.assertEqual(self.fired, ["every-minute"])
        self.assertEqual(results[0]["status"], "done")
        refreshed = self.store.get_job(job["id"])
        self.assertGreater(refreshed["next_run_at"], self.clock())
        self.assertEqual(refreshed["last_status"], "done")

    def test_a_one_shot_job_disables_itself_after_firing(self):
        job = self.store.upsert_job({"name": "once", "kind": "at",
                                     "schedule": str(self.clock() + 5), "target": "x"})
        scheduler = self.make()
        scheduler.ensure_schedules()
        self.clock.advance(6)
        scheduler.tick()
        refreshed = self.store.get_job(job["id"])
        self.assertEqual(self.fired, ["once"])
        self.assertFalse(refreshed["enabled"], "a one-shot job would fire forever")
        self.assertIsNone(refreshed["next_run_at"])

    def test_a_busy_desktop_defers_the_job_instead_of_failing_it(self):
        self.store.upsert_job({"name": "busy", "kind": "every", "schedule": "60",
                               "target": "x"})
        scheduler = self.make(runner=self.raise_busy)
        scheduler.ensure_schedules()
        self.clock.advance(61)
        results = scheduler.tick()
        self.assertEqual(results[0]["status"], "deferred")
        job = self.store.list_jobs()[0]
        self.assertEqual(job["last_status"], "deferred")
        self.assertAlmostEqual(job["next_run_at"], self.clock() + 30, delta=1)

    def raise_busy(self, job):
        raise SchedulerBusy("the desktop is busy")

    def test_a_failing_job_is_recorded_and_still_reschedules(self):
        def boom(job):
            raise RuntimeError("the flow exploded")

        self.store.upsert_job({"name": "boom", "kind": "every", "schedule": "60",
                               "target": "x"})
        scheduler = self.make(runner=boom)
        scheduler.ensure_schedules()
        self.clock.advance(61)
        results = scheduler.tick()
        self.assertEqual(results[0]["status"], "failed")
        self.assertIn("exploded", results[0]["error"])
        self.assertEqual(self.store.list_jobs()[0]["last_status"], "failed")

    def test_the_kill_switch_skips_without_firing(self):
        self.store.upsert_job({"name": "blocked", "kind": "every", "schedule": "60",
                               "target": "x"})
        self.store.set_kill_switch(True)
        scheduler = self.make()
        scheduler.ensure_schedules()
        self.clock.advance(61)
        results = scheduler.tick()
        self.assertEqual(results[0]["status"], "skipped")
        self.assertEqual(self.fired, [], "the runner was called despite the kill switch")

    def test_an_unusable_schedule_is_marked_invalid_not_retried_forever(self):
        self.store.upsert_job({"name": "bad", "kind": "cron", "schedule": "nonsense",
                               "target": "x"})
        scheduler = self.make()
        scheduler.ensure_schedules()
        job = self.store.list_jobs()[0]
        self.assertEqual(job["last_status"], "invalid")
        self.assertIsNone(job["next_run_at"])
        self.assertEqual(scheduler.tick(), [])

    def test_two_jobs_never_run_at_the_same_time(self):
        # The desktop has one mouse; overlapping runs would corrupt both.
        started = threading.Event()
        release = threading.Event()
        order = []

        def slow(job):
            order.append(("start", job["name"]))
            started.set()
            release.wait(2)
            order.append(("end", job["name"]))
            return {"status": "done"}

        self.store.upsert_job({"name": "a", "kind": "every", "schedule": "60",
                               "target": "x"})
        self.store.upsert_job({"name": "b", "kind": "every", "schedule": "60",
                               "target": "x"})
        scheduler = self.make(runner=slow)
        scheduler.ensure_schedules()
        self.clock.advance(61)
        worker = threading.Thread(target=scheduler.tick, daemon=True)
        worker.start()
        self.assertTrue(started.wait(2))
        second = scheduler.tick()
        release.set()
        worker.join(3)
        self.assertEqual([item["status"] for item in second], ["deferred"],
                         "a second job started while the first was still running")
        # Serial, not dropped: the second job waits for the first to finish and
        # then runs in the same tick.
        open_runs = 0
        for kind, _name in order:
            open_runs += 1 if kind == "start" else -1
            self.assertLessEqual(open_runs, 1, "two jobs ran at the same time")
        self.assertEqual(open_runs, 0)
        self.assertEqual(len([item for item in order if item[0] == "start"]), 2)

    def test_the_thread_can_be_started_and_stopped(self):
        scheduler = self.make()
        scheduler.start()
        self.assertTrue(scheduler.alive)
        scheduler.stop(timeout=3)
        self.assertFalse(scheduler.alive)


# ---------------------------------------------------------------------------
# the script gate
# ---------------------------------------------------------------------------
class ScriptGateTest(TempDirCase):
    def setUp(self):
        super().setUp()
        self.store = AgentStore(self.tmp, clock=self.clock)
        self.addCleanup(self.store.close)
        self.runner = ScriptRunner(self.store, clock=self.clock, data_dir=self.tmp,
                                   env_base={"PATH": os.environ.get("PATH", ""),
                                             "AUTOMATION_TOKEN": "super-secret",
                                             "VNC_PASSWORD": "vnc-secret"})

    def test_a_pending_script_cannot_run(self):
        script = self.runner.propose("hello", "print('hi')")
        with self.assertRaises(Exception) as caught:
            self.runner.run(script["id"])
        self.assertIn("pending", str(caught.exception))
        self.assertIn("approve", str(caught.exception))

    def test_a_rejected_script_cannot_run(self):
        script = self.runner.propose("hello", "print('hi')")
        self.runner.reject(script["id"])
        with self.assertRaises(Exception):
            self.runner.run(script["id"])

    def test_an_approved_script_runs_and_is_transcribed(self):
        script = self.runner.propose("persian", "print('سلام از ایجنت')")
        self.runner.approve(script["id"])
        result = self.runner.run(script["id"])
        self.assertEqual(result["exitCode"], 0)
        self.assertIn("سلام از ایجنت", result["output"])
        stored = self.store.get_script(script["id"])
        self.assertEqual(stored["run_count"], 1)
        self.assertEqual(stored["last_exit"], 0)
        self.assertIn("سلام از ایجنت", stored["last_output"])

    def test_bash_scripts_run_too(self):
        script = self.runner.propose("shell", "echo bash-ok", language="bash")
        self.runner.approve(script["id"])
        self.assertIn("bash-ok", self.runner.run(script["id"])["output"])

    def test_an_unknown_language_is_refused(self):
        script = self.store.create_script({"name": "x", "code": "id",
                                           "language": "perl"})
        self.store.set_script_status(script["id"], "approved")
        with self.assertRaises(Exception) as caught:
            self.runner.run(script["id"])
        self.assertIn("unsupported language", str(caught.exception))

    def test_a_failing_script_reports_its_exit_code_and_stderr(self):
        script = self.runner.propose("bad", "import sys; sys.stderr.write('boom\\n'); sys.exit(3)")
        self.runner.approve(script["id"])
        result = self.runner.run(script["id"])
        self.assertEqual(result["exitCode"], 3)
        self.assertEqual(result["status"], "failed")
        self.assertIn("boom", result["output"])

    def test_a_script_that_hangs_is_killed_at_its_timeout(self):
        script = self.runner.propose("hang", "import time; time.sleep(30)")
        self.runner.approve(script["id"])
        started = time.time()
        result = self.runner.run(script["id"], timeout=1)
        self.assertEqual(result["exitCode"], 124)
        self.assertIn("timeout", result["output"])
        self.assertLess(time.time() - started, 20, "the timeout was not enforced")

    def test_the_kill_switch_stops_execution(self):
        script = self.runner.propose("hello", "print('hi')")
        self.runner.approve(script["id"])
        self.store.set_kill_switch(True)
        with self.assertRaises(Exception) as caught:
            self.runner.run(script["id"])
        self.assertIn("kill switch", str(caught.exception))

    def test_only_one_script_runs_at_a_time(self):
        first = self.runner.propose("slow", "import time; time.sleep(1.2); print('one')")
        second = self.runner.propose("fast", "print('two')")
        self.runner.approve(first["id"])
        self.runner.approve(second["id"])
        results = {}

        def run(key, script_id):
            try:
                results[key] = self.runner.run(script_id)
            except Exception as exc:  # noqa: BLE001 - recorded for the assertion
                results[key] = exc

        worker = threading.Thread(target=run, args=("first", first["id"]), daemon=True)
        worker.start()
        time.sleep(0.3)
        run("second", second["id"])
        worker.join(10)
        self.assertIsInstance(results["second"], Exception,
                              "a second script ran while the first was still going")
        self.assertIn("already running", str(results["second"]))
        self.assertEqual(results["first"]["exitCode"], 0)

    def test_environment_sanitisation(self):
        env = build_env({"PATH": "/bin", "AUTOMATION_TOKEN": "super-secret",
                         "VNC_PASSWORD": "vnc-secret", "HOME": "/root"},
                        data_dir="/data/automation", api_base="https://x/api",
                        script_id="scr-1", display=":1")
        self.assertNotIn("AUTOMATION_TOKEN", env)
        self.assertNotIn("VNC_PASSWORD", env)
        self.assertEqual(env["AUTOMATION_DATA_DIR"], "/data/automation")
        self.assertEqual(env["AUTOMATION_API_BASE"], "https://x/api")
        self.assertEqual(env["AUTOMATION_SCRIPT_ID"], "scr-1")
        self.assertEqual(env["DISPLAY"], ":1")

    def test_a_script_cannot_read_the_platform_secrets_by_default(self):
        script = self.runner.propose(
            "leak",
            "import os\nprint('TOKEN=' + repr(os.environ.get('AUTOMATION_TOKEN')))\n"
            "print('VNC=' + repr(os.environ.get('VNC_PASSWORD')))")
        self.runner.approve(script["id"])
        output = self.runner.run(script["id"])["output"]
        self.assertIn("TOKEN=None", output)
        self.assertIn("VNC=None", output)
        self.assertNotIn("super-secret", output)

    def test_passing_the_token_is_a_deliberate_opt_in(self):
        self.store.set_setting("scripts.passToken", True)
        self.store.set_secret("automation.token", "opted-in-token")
        script = self.runner.propose(
            "token", "import os\nprint('TOKEN=' + str(os.environ.get('AUTOMATION_TOKEN')))")
        self.runner.approve(script["id"])
        output = self.runner.run(script["id"])["output"]
        self.assertIn("TOKEN=opted-in-token", output)

    def test_killing_a_running_script_is_reported_when_nothing_runs(self):
        self.assertEqual(self.runner.kill_running()["killed"], False)


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
class TelegramTest(TempDirCase):
    def setUp(self):
        super().setUp()
        self.store = AgentStore(self.tmp, clock=self.clock)
        self.addCleanup(self.store.close)
        self.notifier = Notifier(self.store, public_base=PUBLIC_BASE,
                                 clock=self.clock)

    def test_a_bot_message_goes_to_the_right_url_with_the_right_payload(self):
        calls = []

        def opener(method, payload, files):
            calls.append((method, payload, files))
            return {"message_id": 7}

        bot = TelegramBot("12345:ABC", opener=opener)
        bot.send_text(-100123, "سلام")
        self.assertEqual(calls[0][0], "sendMessage")
        self.assertEqual(calls[0][1]["chat_id"], -100123)
        self.assertEqual(calls[0][1]["text"], "سلام")
        self.assertIn("bot12345:ABC/sendMessage", bot._url("sendMessage"))

    def test_a_photo_is_uploaded_as_multipart_with_the_real_file(self):
        path = os.path.join(self.tmp, "shot.png")
        with open(path, "wb") as handle:
            handle.write(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
        seen = {}

        def opener(method, payload, files):
            seen.update({"method": method, "payload": payload, "files": files})
            return {"message_id": 8}

        TelegramBot("t", opener=opener).send_photo(-1, path, "کپچا")
        self.assertEqual(seen["method"], "sendPhoto")
        self.assertEqual(seen["files"]["photo"], path)
        self.assertEqual(seen["payload"]["caption"], "کپچا")

    def test_a_missing_screenshot_is_reported_not_sent(self):
        with self.assertRaises(TelegramError):
            TelegramBot("t", opener=lambda *a: {}).send_photo(
                -1, os.path.join(self.tmp, "gone.png"), "")

    def test_discovered_chats_come_from_get_updates(self):
        updates = [
            {"message": {"chat": {"id": -100123, "title": "گروه من",
                                  "type": "supergroup"}, "text": "سلام",
                         "date": 20}},
            {"message": {"chat": {"id": 555, "first_name": "Majid",
                                  "type": "private"}, "text": "hi", "date": 30}},
            {"message": {"chat": {"id": -100123, "title": "گروه من",
                                  "type": "supergroup"}, "text": "دوباره",
                         "date": 40}},
        ]
        bot = TelegramBot("t", opener=lambda method, payload, files: updates)
        targets = bot.list_targets()
        self.assertEqual(len(targets), 2, "duplicate chats were not collapsed")
        self.assertEqual(targets[0]["id"], -100123, "newest activity should come first")
        self.assertEqual(targets[0]["title"], "گروه من")
        self.assertEqual(targets[1]["title"], "Majid")

    def test_a_handoff_message_carries_the_desktop_and_the_screenshot(self):
        text = self.notifier.format_handoff({
            "label": "ورود", "message": "کپچا دیده شد",
            "signals": ["cloudflare"], "pageOrigin": "https://target.example",
            "publicShot": "shot-1.png", "runId": "run-9"})
        self.assertIn("ورود", text)
        self.assertIn("cloudflare", text)
        self.assertIn("%s/automation/api/public/shot/shot-1.png" % PUBLIC_BASE, text)
        self.assertIn("/vnc.html?autoconnect=true&resize=scale&path=websockify", text)
        self.assertIn("run-9", text)

    def test_a_result_message_is_localised_and_carries_the_error(self):
        text = self.notifier.format_result("اجرای زمان‌بندی‌شده", "Arena", "failed",
                                           "the page changed")
        self.assertIn("❌", text)
        self.assertIn("Arena", text)
        self.assertIn("the page changed", text)

    def test_delivery_is_skipped_entirely_when_notifications_are_off(self):
        self.store.set_setting("telegram.mode", "off")
        result = self.notifier.send("hi")
        self.assertEqual(result["sent"], 0)
        self.assertIn("off", result["reason"])

    def test_delivery_reports_which_targets_failed(self):
        self.store.set_setting("telegram.mode", "bot")
        self.store.set_secret("telegram.botToken", "t")
        self.store.set_setting("telegram.targets",
                               [{"id": 1, "title": "ok"}, {"id": 2, "title": "bad"}])

        class HalfBroken:
            def send_text(self, chat_id, text):
                if chat_id == 2:
                    raise TelegramError("chat not found")
                return {}

        self.notifier.transport = lambda mode=None: HalfBroken()
        result = self.notifier.send("hi")
        self.assertEqual(result["sent"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertIn("chat not found", result["reason"])

    def test_no_targets_configured_is_reported_clearly(self):
        self.store.set_setting("telegram.mode", "bot")
        self.store.set_secret("telegram.botToken", "t")
        result = self.notifier.send("hi")
        self.assertEqual(result["sent"], 0)
        self.assertIn("no telegram targets", result["reason"])

    def test_a_handoff_notification_never_raises_into_the_run(self):
        self.store.set_setting("telegram.mode", "bot")
        self.store.set_secret("telegram.botToken", "t")
        self.store.set_setting("telegram.targets", [{"id": 1, "title": "x"}])

        def explode(mode=None):
            raise TelegramError("network is down")

        self.notifier.transport = explode
        try:
            self.notifier.notify_handoff({"label": "x"})
            for thread in self.notifier._pending:
                thread.join(2)
        except Exception as exc:  # noqa: BLE001 - the whole point of the test
            self.fail("notify_handoff raised into the caller: %s" % exc)

    def test_account_mode_without_telethon_explains_itself(self):
        self.store.set_setting("telegram.mode", "account")
        self.store.set_secret("telegram.apiId", "1")
        self.store.set_secret("telegram.apiHash", "h")
        try:
            import telethon  # noqa: F401
            self.skipTest("telethon is installed here; the missing-dependency "
                          "path cannot be exercised")
        except ImportError:
            pass
        with self.assertRaises(TelegramError) as caught:
            self.notifier.transport()
        self.assertIn("telethon", str(caught.exception))
        self.assertIn("bot mode", str(caught.exception))

    def test_account_mode_without_credentials_explains_itself(self):
        # The import is checked first, so the credential message is only
        # reachable with telethon present - faked here rather than skipped,
        # because a missing credential still has to be named.
        import types
        from unittest import mock

        from automation.telegram_client import TelegramAccount
        fake = types.ModuleType("telethon")
        fake.TelegramClient = object
        self.store.set_setting("telegram.mode", "account")
        with mock.patch.dict(sys.modules, {"telethon": fake}):
            with self.assertRaises(TelegramError) as caught:
                TelegramAccount("", "", os.path.join(self.tmp, "session"))
            self.assertIn("apiId", str(caught.exception))
            self.assertIn("my.telegram.org", str(caught.exception))


# ---------------------------------------------------------------------------
# the model, by browser and by HTTP
# ---------------------------------------------------------------------------
class ExtractJsonTest(unittest.TestCase):
    def test_plain_json(self):
        self.assertEqual(extract_json('{"a": 1}'), {"a": 1})

    def test_json_inside_a_markdown_fence(self):
        self.assertEqual(extract_json('```json\n{"a": 1}\n```'), {"a": 1})

    def test_json_with_prose_around_it(self):
        self.assertEqual(extract_json('Sure!\n{"a": 1}\nHope that helps.'), {"a": 1})

    def test_an_array_is_returned_too(self):
        self.assertEqual(extract_json("here: [1, 2, 3]"), [1, 2, 3])

    def test_persian_text_inside_json_survives(self):
        parsed = extract_json('{"summary": "سه تصویر با چراغ راهنما"}')
        self.assertEqual(parsed["summary"], "سه تصویر با چراغ راهنما")

    def test_prose_without_json_returns_none(self):
        self.assertIsNone(extract_json("I cannot help with that."))
        self.assertIsNone(extract_json(""))


class AiBrowserTest(TempDirCase):
    def test_an_unreachable_port_is_reported_as_unavailable(self):
        browser = AiBrowser(FakeCdp(fail=True), port=9223)
        self.assertFalse(browser.available())
        with self.assertRaises(AiBrowserError) as caught:
            browser.ask("hello")
        self.assertIn("9223", str(caught.exception))
        self.assertIn("AI_BROWSER=1", str(caught.exception),
                      "the fix must be named in the error, not guessed at")

    def test_status_reports_the_page_it_found(self):
        info = AiBrowser(FakeCdp(page_states=[page("old answer")]),
                         port=9223).status()
        self.assertTrue(info["available"])
        self.assertEqual(info["answers"], 1)
        self.assertEqual(info["pageTitle"], "chat")

    def test_a_missing_input_box_reports_what_the_page_looked_like(self):
        browser = AiBrowser(FakeCdp(page_states=[EMPTY_PAGE], inject={
                                "ok": False, "reason": "no input element matched",
                                "tried": ["#chat-input"], "title": "Login",
                                "url": "https://chat/login",
                                "bodyStart": "Sign in to continue"}),
                            port=9223, sleep=lambda _s: None)
        with self.assertRaises(AiBrowserError) as caught:
            browser.ask("hello", timeout=2)
        message = str(caught.exception)
        self.assertIn("Sign in to continue", message,
                      "the operator cannot fix a selector they cannot see")

    def test_a_submitted_prompt_waits_for_the_answer_to_stop_growing(self):
        # An empty chat, then an answer that streams in and stops changing.
        states = [EMPTY_PAGE, EMPTY_PAGE, page("part"), page("part one"),
                  page("part one two"), page("part one two"),
                  page("part one two")]
        browser = AiBrowser(FakeCdp(page_states=states), port=9223,
                            sleep=lambda _s: None)
        result = browser.ask("what should I click?", timeout=10)
        self.assertEqual(result["text"], "part one two")
        self.assertEqual(result["answers"], 1)

    def test_no_answer_within_the_timeout_is_an_error(self):
        clock = TickingClock()
        browser = AiBrowser(FakeCdp(page_states=[EMPTY_PAGE]), port=9223,
                            sleep=clock.sleep, clock=clock)
        with self.assertRaises(AiBrowserError) as caught:
            browser.ask("hello", timeout=5)
        self.assertIn("no answer appeared", str(caught.exception))

    def test_the_prompt_is_handed_to_the_page_as_data_not_as_code(self):
        cdp = FakeCdp(page_states=[EMPTY_PAGE, EMPTY_PAGE, page("answer"),
                                   page("answer"), page("answer")])
        browser = AiBrowser(cdp, port=9223, sleep=lambda _s: None)
        # A prompt that would close the string literal - and so take over the
        # page - if it were interpolated raw into the injected JavaScript.
        browser.ask('break"; document.title = "pwned', timeout=5)
        injected = [item for item in cdp.expressions
                    if "no input element matched" in item]
        self.assertEqual(len(injected), 1, "the injection script was never sent")
        self.assertIn('\\"', injected[0], "the prompt was not escaped for JavaScript")
        self.assertNotIn('"; document.title = "pwned',
                         injected[0].replace('\\"', ''),
                         "the prompt broke out of its JavaScript string")

    def test_a_custom_provider_profile_overrides_the_selectors(self):
        browser = AiBrowser(FakeCdp(), port=9223,
                            providers={"deepseek": {"inputs": ["#my-box"]}})
        profile = browser.profile("deepseek")
        self.assertEqual(profile["inputs"], ["#my-box"])
        # Unset keys still come from the built-in profile.
        self.assertTrue(profile["answers"])

    def test_an_unknown_provider_falls_back_to_generic(self):
        browser = AiBrowser(FakeCdp(), port=9223)
        self.assertTrue(browser.profile("does-not-exist")["inputs"])


class LlmClientTest(TempDirCase):
    def setUp(self):
        super().setUp()
        self.store = AgentStore(self.tmp, clock=self.clock)
        self.addCleanup(self.store.close)

    def test_the_default_provider_is_the_browser_so_no_key_is_needed(self):
        client = LlmClient(self.store, ai_browser=None)
        self.assertEqual(client.provider(), "ai-browser")
        self.assertFalse(client.configured(), "no browser is attached here")

    def test_browser_mode_delegates_and_labels_its_answer(self):
        class Browser:
            def available(self):
                return True

            def ask(self, prompt, name="deepseek", timeout=120, fresh_chat=True,
                    image_path=""):
                return {"text": " روی خانهٔ ۳ کلیک کن ", "url": "u", "title": "t"}

        client = LlmClient(self.store, ai_browser=Browser())
        reply = client.complete("چی بزنم؟")
        self.assertEqual(reply["text"], "روی خانهٔ ۳ کلیک کن")
        self.assertEqual(reply["provider"], "ai-browser")

    def test_asking_for_json_appends_the_strict_instruction(self):
        class Browser:
            def available(self):
                return True

            def ask(self, prompt, **kwargs):
                self.prompt = prompt
                return {"text": "{}"}

        browser = Browser()
        LlmClient(self.store, ai_browser=browser).complete("x", want_json=True)
        self.assertIn("JSON", browser.prompt)

    def test_http_mode_builds_an_openai_compatible_request(self):
        self.store.set_setting("ai.provider", "http-api")
        self.store.set_setting("ai.baseUrl", "https://api.example/v1/")
        self.store.set_setting("ai.model", "deepseek-chat")
        self.store.set_secret("ai.apiKey", "sk-secret")
        captured = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps({"choices": [{"message": {"content": "answer"}}],
                                   "usage": {"total_tokens": 12}}).encode()

        def opener(request, timeout=None):
            captured["url"] = request.full_url
            captured["auth"] = request.get_header("Authorization")
            captured["body"] = json.loads(request.data.decode())
            return Response()

        client = LlmClient(self.store, opener=opener)
        reply = client.complete("سلام")
        self.assertEqual(captured["url"], "https://api.example/v1/chat/completions")
        self.assertEqual(captured["auth"], "Bearer sk-secret")
        self.assertEqual(captured["body"]["model"], "deepseek-chat")
        self.assertEqual(captured["body"]["messages"][0]["content"], "سلام")
        self.assertEqual(reply["text"], "answer")
        self.assertEqual(reply["usage"]["total_tokens"], 12)

    def test_http_mode_sends_an_image_as_base64(self):
        self.store.set_setting("ai.provider", "http-api")
        self.store.set_secret("ai.apiKey", "sk-secret")
        path = os.path.join(self.tmp, "shot.png")
        with open(path, "wb") as handle:
            handle.write(b"\x89PNG\r\n\x1a\n1234")
        captured = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps({"choices": [{"message": {"content": "3"}}]}).encode()

        def opener(request, timeout=None):
            captured["body"] = json.loads(request.data.decode())
            return Response()

        LlmClient(self.store, opener=opener).complete("کجا کلیک کنم؟", images=[path])
        content = captured["body"]["messages"][0]["content"]
        self.assertEqual(content[0]["type"], "text")
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_http_mode_without_a_key_says_so_instead_of_calling_out(self):
        self.store.set_setting("ai.provider", "http-api")
        with self.assertRaises(LlmError) as caught:
            LlmClient(self.store, opener=lambda *a, **k: None).complete("x")
        self.assertIn("no API key", str(caught.exception))

    def test_an_http_error_is_reported_with_its_body_but_not_the_key(self):
        import urllib.error
        self.store.set_setting("ai.provider", "http-api")
        self.store.set_secret("ai.apiKey", "sk-secret")

        def opener(request, timeout=None):
            raise urllib.error.HTTPError("https://api.example", 402, "Payment Required",
                                         {}, None)

        with self.assertRaises(LlmError) as caught:
            LlmClient(self.store, opener=opener).complete("x")
        self.assertIn("402", str(caught.exception))
        self.assertNotIn("sk-secret", str(caught.exception))

    def test_describe_never_includes_a_secret_value(self):
        self.store.set_setting("ai.provider", "http-api")
        self.store.set_secret("ai.apiKey", "sk-secret")
        blob = json.dumps(LlmClient(self.store).describe(), ensure_ascii=False)
        self.assertNotIn("sk-secret", blob)
        self.assertIn('"keySet": true', blob)


# ---------------------------------------------------------------------------
# the captcha chain
# ---------------------------------------------------------------------------
class CaptchaTest(TempDirCase):
    def setUp(self):
        super().setUp()
        self.store = AgentStore(self.tmp, clock=self.clock)
        self.addCleanup(self.store.close)
        self.backend = FakeBackend()
        self.shot = os.path.join(self.tmp, "shot.png")
        with open(self.shot, "rb" if False else "wb") as handle:
            handle.write(self.png(1366, 768))

    @staticmethod
    def png(width, height):
        import struct
        import zlib

        def chunk(kind, data):
            return (struct.pack(">I", len(data)) + kind + data
                    + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

        header = b"\x89PNG\r\n\x1a\n"
        ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        raw = b"".join(b"\x00" + b"\xff" * (width * 3) for _ in range(height))
        idat = chunk(b"IDAT", zlib.compress(raw, 1))
        return header + ihdr + idat + chunk(b"IEND", b"")

    def make(self, llm, notifier=None, control=None):
        from automation.engine import png_size
        return CaptchaSolver(self.store, llm, notifier=notifier, control=control,
                             clock=self.clock, image_size=png_size)

    def test_a_confident_vision_answer_becomes_a_validated_proposal(self):
        llm = FakeLlm(json.dumps({
            "kind": "image_select", "confidence": 0.92,
            "summary": "سه خانه با چراغ راهنما", "solvableByVision": True,
            "actions": [{"type": "click", "x": 120, "y": 340},
                        {"type": "click", "x": 400, "y": 340}]}))
        result = self.make(llm).vision(self.shot, "یک کپچا")
        self.assertTrue(result["ok"])
        self.assertEqual(result["kind"], "image_select")
        self.assertEqual(len(result["actions"]), 2)
        self.assertTrue(result["confident"])

    def test_clicks_outside_the_screenshot_are_refused(self):
        llm = FakeLlm(json.dumps({
            "kind": "click", "confidence": 0.9, "solvableByVision": True,
            "actions": [{"type": "click", "x": 5000, "y": 5000},
                        {"type": "click", "x": -3, "y": 10},
                        {"type": "click", "x": 100, "y": 100}]}))
        result = self.make(llm).vision(self.shot)
        self.assertEqual(len(result["actions"]), 1, "out-of-bounds clicks survived")
        self.assertEqual(len(result["rejected"]), 2)
        self.assertIn("outside", result["rejected"][0]["reason"])

    def test_unknown_action_types_are_refused(self):
        llm = FakeLlm(json.dumps({
            "kind": "click", "confidence": 0.9, "solvableByVision": True,
            "actions": [{"type": "shell", "command": "rm -rf /"},
                        {"type": "click", "x": 5, "y": 5}]}))
        result = self.make(llm).vision(self.shot)
        self.assertEqual([a["type"] for a in result["actions"]], ["click"])
        self.assertIn("unknown type", result["rejected"][0]["reason"])

    def test_more_than_nine_actions_are_truncated(self):
        llm = FakeLlm(json.dumps({
            "kind": "click", "confidence": 0.9, "solvableByVision": True,
            "actions": [{"type": "click", "x": i, "y": i} for i in range(30)]}))
        result = self.make(llm).vision(self.shot)
        self.assertLessEqual(len(result["actions"]), 9)
        self.assertTrue(any("more than 9" in str(item.get("reason"))
                            for item in result["rejected"]))

    def test_a_behavioural_challenge_is_handed_to_a_human_not_guessed_at(self):
        llm = FakeLlm(json.dumps({
            "kind": "checkbox", "confidence": 0.99, "solvableByVision": False,
            "reason": "reCAPTCHA scores behaviour, not pixels", "actions": []}))
        solver = self.make(llm)
        result = solver.solve(self.shot, {"label": "reCAPTCHA"})
        self.assertEqual(result["strategy"], "human")
        self.assertTrue(result["needsHuman"])

    def test_a_model_that_does_not_return_json_is_a_failed_strategy(self):
        llm = FakeLlm("I think you should click somewhere in the middle.")
        result = self.make(llm).vision(self.shot)
        self.assertFalse(result["ok"])
        self.assertIn("did not return JSON", result["error"])

    def test_a_low_confidence_answer_escalates_to_a_human(self):
        llm = FakeLlm(json.dumps({
            "kind": "image_select", "confidence": 0.2, "solvableByVision": True,
            "actions": [{"type": "click", "x": 10, "y": 10}]}))
        result = self.make(llm).solve(self.shot, {"label": "uncertain"})
        self.assertEqual(result["strategy"], "human")

    def test_by_default_a_proposal_is_not_clicked(self):
        llm = FakeLlm(json.dumps({
            "kind": "click", "confidence": 0.95, "solvableByVision": True,
            "actions": [{"type": "click", "x": 10, "y": 10}]}))
        solver = self.make(llm, control=self.backend)
        result = solver.solve(self.shot, {"label": "x"})
        self.assertTrue(result["needsApproval"])
        self.assertEqual(self.backend.calls, [], "it clicked without being told to")

    def test_autoclick_performs_the_validated_actions(self):
        self.store.set_setting("captcha.autoClick", True)
        llm = FakeLlm(json.dumps({
            "kind": "click", "confidence": 0.95, "solvableByVision": True,
            "actions": [{"type": "click", "x": 10, "y": 20}]}))
        solver = self.make(llm, control=self.backend)
        result = solver.solve(self.shot, {"label": "x"})
        self.assertFalse(result.get("needsApproval"))
        self.assertIn(("click", 10, 20, "left", 1), self.backend.calls)

    def test_the_kill_switch_blocks_execution(self):
        self.store.set_kill_switch(True)
        solver = self.make(FakeLlm("{}"), control=self.backend)
        with self.assertRaises(Exception) as caught:
            solver.execute([{"type": "click", "x": 1, "y": 1}])
        self.assertIn("kill switch", str(caught.exception))

    def test_escalation_tells_the_notifier_and_records_the_attempt(self):
        sent = []

        class Notifier:
            def notify_handoff(self, payload):
                sent.append(payload)

        solver = self.make(FakeLlm("no json"), notifier=Notifier())
        result = solver.solve(self.shot, {"label": "کپچا", "pageOrigin": "https://x"})
        self.assertEqual(result["strategy"], "human")
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["label"], "کپچا")
        self.assertTrue(any(item.get("event") == "escalated"
                            for item in solver.history()))

    def test_the_extension_slot_reports_what_is_honestly_known(self):
        solver = self.make(FakeLlm("{}"))
        self.store.set_setting("captcha.extension", "buster")
        info = solver.extension_status()
        self.assertEqual(info["name"], "buster")
        self.assertTrue(info["free"])
        self.assertFalse(info["verified"], "an unverified claim must not read as verified")
        self.assertIn("صوتی", info["covers"])

    def test_an_unknown_extension_is_rejected_by_the_config_endpoint(self):
        from automation.captcha import CaptchaError
        with self.assertRaises(CaptchaError):
            self.make(FakeLlm("{}")).set_config({"extension": "paid-service"})

    def test_config_validation(self):
        from automation.captcha import CaptchaError
        solver = self.make(FakeLlm("{}"))
        with self.assertRaises(CaptchaError):
            solver.set_config({"strategies": []})
        with self.assertRaises(CaptchaError):
            solver.set_config({"maxAttempts": 99})
        with self.assertRaises(CaptchaError):
            solver.set_config({"minConfidence": 3})
        saved = solver.set_config({"strategies": ["human"], "maxAttempts": 2,
                                   "minConfidence": 0.8, "autoClick": True})
        self.assertEqual(saved["strategies"], ["human"])
        self.assertEqual(saved["maxAttempts"], 2)
        self.assertTrue(saved["autoClick"])

    def test_a_disabled_solver_does_not_call_the_model(self):
        self.store.set_setting("captcha.enabled", False)
        llm = FakeLlm("{}")
        result = self.make(llm).solve(self.shot, {})
        self.assertFalse(result["ok"])
        self.assertEqual(llm.calls, [], "the model was asked despite being disabled")


# ---------------------------------------------------------------------------
# the engine's notification hook
# ---------------------------------------------------------------------------
class EngineNotifierTest(TempDirCase):
    def build(self, notifier):
        backend = FakeBackend()
        engine = AutomationEngine(backend, self.tmp, notifier=notifier)
        flow, errors = schema.validate_flow({
            "name": "handoff",
            "steps": [{"id": "s1", "type": "pause_for_human_verification",
                       "label": "کپچا",
                       "prompt": "کپچا را حل کنید و سپس ادامه دهید"}],
        })
        self.assertEqual(errors, [])
        return engine, flow

    def wait_for_status(self, engine, wanted, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if engine.status().get("status") == wanted:
                return True
            time.sleep(0.02)
        return False

    def test_a_human_handoff_is_announced_before_the_run_waits(self):
        received = []

        class Notifier:
            def notify_handoff(self, payload):
                received.append(payload)

        engine, flow = self.build(Notifier())
        engine.start(flow)
        self.assertTrue(self.wait_for_status(engine, "waiting"),
                        "the run never reached the handoff")
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["label"], "کپچا")
        self.assertEqual(received[0]["kind"], "manual_verification")
        self.assertTrue(received[0]["runId"], "the run id was not passed along")
        engine.confirm(True)
        self.assertTrue(self.wait_for_status(engine, "done"))

    def test_a_broken_notifier_cannot_break_the_run(self):
        class Broken:
            def notify_handoff(self, payload):
                raise RuntimeError("telegram exploded")

        engine, flow = self.build(Broken())
        engine.start(flow)
        self.assertTrue(self.wait_for_status(engine, "waiting"),
                        "a failing notifier stopped the run")
        engine.confirm(True)
        self.assertTrue(self.wait_for_status(engine, "done"))
        messages = [entry["message"] for entry in engine.status()["entries"]]
        self.assertTrue(any("notification failed" in item for item in messages),
                        "the failure was swallowed without a trace in the log")

    def test_without_a_notifier_nothing_changes(self):
        engine, flow = self.build(None)
        engine.start(flow)
        self.assertTrue(self.wait_for_status(engine, "waiting"))
        engine.confirm(False)
        self.assertTrue(self.wait_for_status(engine, "stopped"))


# ---------------------------------------------------------------------------
# the hub and its HTTP surface
# ---------------------------------------------------------------------------
class AgentHubTest(TempDirCase):
    def build(self, llm=None, engine=True, clock=None):
        backend = FakeBackend()
        flows = FlowStore(self.tmp)
        flows.save("Arena", {"name": "Arena", "steps": []})
        hub = AgentHub(self.tmp, engine=None, flows=flows, backend=backend,
                       token=TOKEN, public_base=PUBLIC_BASE,
                       clock=self.clock if clock is None else clock)
        if llm is not None:
            hub.llm = llm
            hub.captcha.llm = llm
        if engine:
            hub.engine = AutomationEngine(backend, self.tmp, notifier=hub.notifier)
        self.addCleanup(hub.store.close)
        return hub, backend, flows

    def test_the_overview_exposes_no_secret_value(self):
        hub, _, _ = self.build()
        hub.store.set_setting("telegram.botToken", "12345:SECRET", secret=True)
        blob = json.dumps(hub.overview(), ensure_ascii=False)
        self.assertNotIn("SECRET", blob)
        for key in ("settings", "llm", "telegram", "captcha", "scripts",
                    "scheduler", "killSwitch", "counts"):
            self.assertIn(key, blob)

    def test_unknown_settings_are_rejected(self):
        hub, _, _ = self.build()
        with self.assertRaises(AgentError):
            hub.save_settings({"not.a.setting": 1})

    def test_settings_are_typed_before_they_are_stored(self):
        hub, _, _ = self.build()
        hub.save_settings({"captcha.maxAttempts": "4", "captcha.autoClick": 1})
        self.assertEqual(hub.store.get_setting("captcha.maxAttempts"), 4)
        self.assertIs(hub.store.get_setting("captcha.autoClick"), True)
        with self.assertRaises(AgentError):
            hub.save_settings({"telegram.mode": "carrier-pigeon"})

    def test_an_empty_secret_clears_it(self):
        hub, _, _ = self.build()
        hub.save_settings({"ai.apiKey": "sk-1"})
        self.assertEqual(hub.store.get_secret("ai.apiKey"), "sk-1")
        hub.save_settings({"ai.apiKey": ""})
        self.assertEqual(hub.store.get_secret("ai.apiKey"), "")

    def test_the_kill_switch_blocks_acting(self):
        hub, _, _ = self.build(llm=FakeLlm("hi"))
        hub.set_kill_switch(True)
        with self.assertRaises(AgentError) as caught:
            hub.chat("سلام")
        self.assertEqual(caught.exception.status, 409)

    def test_chat_returns_a_validated_flow_when_the_model_produces_one(self):
        reply = json.dumps({"name": "پیشنهادی", "steps": [
            {"id": "s1", "type": "goto_url", "url": "https://example.com"}]})
        hub, _, _ = self.build(llm=FakeLlm(reply))
        result = hub.chat("یک جریان بساز")
        self.assertTrue(result["flowValid"])
        self.assertEqual(result["flow"]["steps"][0]["type"], "goto_url")

    def test_chat_reports_a_broken_flow_instead_of_hiding_it(self):
        reply = json.dumps({"steps": [{"type": "teleport"}]})
        hub, _, _ = self.build(llm=FakeLlm(reply))
        result = hub.chat("x")
        self.assertFalse(result["flowValid"])
        self.assertTrue(result["flowErrors"])

    def test_a_job_must_reference_something_real(self):
        hub, _, _ = self.build()
        with self.assertRaises(AgentError):
            hub.save_job({"name": "x", "kind": "every", "schedule": "60",
                          "target": "does-not-exist", "payload": {"action": "flow"}})
        job = hub.save_job({"name": "ok", "kind": "every", "schedule": "60",
                            "target": "Arena", "payload": {"action": "flow"}})
        self.assertTrue(job["next_run_at"], "the first run time was never computed")

    def test_a_scheduled_script_must_already_be_approved(self):
        hub, _, _ = self.build()
        script = hub.script_save({"name": "s", "code": "print(1)"})
        with self.assertRaises(AgentError) as caught:
            hub.save_job({"name": "nightly", "kind": "cron", "schedule": "0 3 * * *",
                          "target": script["id"], "payload": {"action": "script"}})
        self.assertEqual(caught.exception.status, 409)
        hub.script_decision(script["id"], True)
        hub.save_job({"name": "nightly", "kind": "cron", "schedule": "0 3 * * *",
                      "target": script["id"], "payload": {"action": "script"}})

    def test_the_sql_console_reads_but_never_writes(self):
        hub, _, _ = self.build()
        hub.save_job({"name": "j", "kind": "every", "schedule": "60",
                      "target": "Arena", "payload": {"action": "flow"}})
        result = hub.query("SELECT name, kind FROM jobs")
        self.assertEqual(result["rows"][0]["name"], "j")
        for statement in ("DELETE FROM jobs", "UPDATE jobs SET name='x'",
                          "SELECT 1; DROP TABLE jobs", "PRAGMA table_info(jobs)",
                          "INSERT INTO notes VALUES (1,2,3,4,5)", ""):
            with self.subTest(statement=statement):
                with self.assertRaises(AgentError):
                    hub.query(statement)

    def test_a_query_result_is_limited(self):
        hub, _, _ = self.build()
        for index in range(5):
            hub.store.add_note("note %d" % index)
        self.assertLessEqual(len(hub.query("SELECT id FROM notes", limit=2)["rows"]), 2)

    def test_running_a_job_now_uses_the_engine(self):
        hub, backend, flows = self.build()
        flows.save("Arena", schema.validate_flow({
            "name": "Arena",
            "steps": [{"id": "s1", "type": "goto_url",
                       "url": "https://example.com"}]})[0])
        job = hub.save_job({"name": "now", "kind": "every", "schedule": "60",
                            "target": "Arena", "payload": {"action": "flow"}})
        outcome = hub.run_job_now(job["id"], wait=True)
        self.assertIn(outcome["status"], ("done", "waiting", "running", "failed"))
        self.assertIn(("goto_url", "https://example.com"),
                      [tuple(item) for item in backend.calls])

    def test_run_now_answers_at_once_and_finishes_in_the_background(self):
        # The run can take minutes and this is reached over HTTP, so the answer
        # must not be the run itself - and the outcome must still be recorded.
        hub, _, flows = self.build(clock=time.time)
        flows.save("Wait", schema.validate_flow({
            "name": "Wait",
            "steps": [{"id": "s1", "type": "wait", "ms": 1500}]})[0])
        first = hub.save_job({"name": "first", "kind": "every", "schedule": "60",
                              "target": "Wait", "payload": {"action": "flow"}})
        second = hub.save_job({"name": "second", "kind": "every", "schedule": "60",
                               "target": "Wait", "payload": {"action": "flow"}})
        started_at = time.time()
        answer = hub.run_job_now(first["id"])
        self.assertLess(time.time() - started_at, 1.0,
                        "run_job_now waited for the run instead of starting it")
        self.assertEqual(answer["status"], "queued")
        deadline = time.time() + 5
        while time.time() < deadline and not hub.engine.busy():
            time.sleep(0.02)
        self.assertTrue(hub.engine.busy(), "the background run never started")
        # One mouse: a second run must be refused, not queued behind the first.
        with self.assertRaises(AgentError) as caught:
            hub.run_job_now(second["id"])
        self.assertEqual(caught.exception.status, 409)
        deadline = time.time() + 15
        while time.time() < deadline and not hub.store.get_job(first["id"])["last_status"]:
            time.sleep(0.05)
        job = hub.store.get_job(first["id"])
        self.assertEqual(job["last_status"], "done",
                         "the background outcome was never recorded")
        self.assertIsNotNone(job["next_run_at"],
                             "a manual run cleared the schedule")
        hub.engine.stop()

    def test_a_prompt_job_stores_the_answer_as_a_note(self):
        hub, _, _ = self.build(llm=FakeLlm("پاسخ ایجنت"))
        job = hub.save_job({"name": "ask", "kind": "every", "schedule": "60",
                            "payload": {"action": "prompt", "text": "چی کار کنم؟"}})
        hub.run_job_now(job["id"], wait=True)
        notes = hub.notes(kind="job-answer")
        self.assertEqual(len(notes), 1)
        self.assertIn("پاسخ ایجنت", notes[0]["body"])

    def test_telegram_status_is_local_until_a_probe_is_asked_for(self):
        hub, _, _ = self.build()
        hub.save_settings({"telegram.mode": "bot", "telegram.botToken": "12345:ABC"})
        info = hub.telegram_status()
        self.assertEqual(info["mode"], "bot")
        self.assertTrue(info["botTokenSet"])
        self.assertNotIn("identity", info, "an unprobed status touched the network")
        self.assertNotIn("ABC", json.dumps(info))


class AgentApiTest(TempDirCase):
    """The HTTP surface, exercised through AutomationApi.handle()."""

    def setUp(self):
        super().setUp()
        self.backend = FakeBackend()
        self.flows = FlowStore(self.tmp)
        self.flows.save("Arena", {"name": "Arena", "steps": []})
        self.hub = AgentHub(self.tmp, engine=None, flows=self.flows,
                            backend=self.backend, token=TOKEN,
                            public_base=PUBLIC_BASE, clock=self.clock)
        self.hub.llm = FakeLlm("سلام")
        self.hub.captcha.llm = self.hub.llm
        self.engine = AutomationEngine(self.backend, self.tmp,
                                       notifier=self.hub.notifier)
        self.hub.engine = self.engine
        self.addCleanup(self.hub.store.close)
        self.api = AutomationApi(self.engine, self.flows, None, data_dir=self.tmp,
                                 token=TOKEN, agent=self.hub)
        self.headers = {"x-automation-token": TOKEN}

    def call(self, method, path, body=None, headers=None):
        raw = json.dumps(body).encode("utf-8") if body is not None else b""
        status, _, data = self.api.handle(
            method, path, body=raw,
            headers=self.headers if headers is None else headers)
        try:
            payload = json.loads(data.decode("utf-8"))
        except ValueError:
            payload = data[:200]
        return status, payload

    def test_without_an_agent_the_original_route_table_is_untouched(self):
        plain = AutomationApi(self.engine, self.flows, None, data_dir=self.tmp,
                              token=TOKEN)
        self.assertNotIn("/agent", [pattern for _, pattern, _ in plain.routes])
        status, payload = self.call.__self__.__class__.call(
            self, "GET", "/agent", headers=self.headers)
        self.assertEqual(status, 200)
        with_agent = len(self.api.routes)
        self.assertGreater(with_agent, len(plain.routes))
        self.assertEqual(plain.handle("GET", "/agent", headers=self.headers)[0], 404)

    def test_every_agent_route_needs_the_token(self):
        for path in ("/agent", "/agent/jobs", "/agent/scripts", "/agent/notes",
                     "/agent/audit", "/agent/telegram/status",
                     "/agent/captcha/config", "/agent/ai/status"):
            with self.subTest(path=path):
                status, payload = self.call("GET", path, headers={})
                self.assertEqual(status, 401)
                self.assertIn("X-Automation-Token", payload["error"])

    def test_the_overview_is_reachable_and_secret_free(self):
        self.hub.save_settings({"ai.apiKey": "sk-secret"})
        status, payload = self.call("GET", "/agent")
        self.assertEqual(status, 200)
        self.assertNotIn("sk-secret", json.dumps(payload))

    def test_settings_round_trip_over_http(self):
        status, _ = self.call("POST", "/agent/settings",
                              {"telegram.mode": "bot", "telegram.botToken": "1:x"})
        self.assertEqual(status, 200)
        status, payload = self.call("GET", "/agent")
        self.assertEqual(payload["settings"]["telegram.mode"], "bot")
        self.assertNotIn("1:x", json.dumps(payload))

    def test_an_unknown_setting_is_a_400_not_a_500(self):
        status, payload = self.call("POST", "/agent/settings", {"bogus": 1})
        self.assertEqual(status, 400)
        self.assertIn("unknown setting", payload["error"])

    def test_the_script_gate_is_enforced_over_http_with_a_409(self):
        status, payload = self.call("POST", "/agent/scripts",
                                    {"name": "s", "code": "print(1)"})
        self.assertEqual(status, 201)
        script_id = payload["script"]["id"]
        status, payload = self.call("POST", "/agent/script-run", {"id": script_id})
        self.assertEqual(status, 409, "an unapproved script was allowed to run")
        self.assertIn("approve", payload["error"])
        self.call("POST", "/agent/script-decision", {"id": script_id, "approve": True})
        status, payload = self.call("POST", "/agent/script-run", {"id": script_id})
        self.assertEqual(status, 200)
        self.assertEqual(payload["exitCode"], 0)

    def test_the_kill_switch_returns_409_for_every_acting_route(self):
        self.call("POST", "/agent/kill-switch", {"on": True})
        self.assertEqual(self.call("POST", "/agent/chat", {"request": "x"})[0], 409)
        self.assertEqual(self.call("POST", "/agent/captcha/solve", {})[0], 409)
        self.call("POST", "/agent/kill-switch", {"on": False})
        self.assertEqual(self.call("POST", "/agent/chat", {"request": "x"})[0], 200)

    def test_jobs_are_validated_and_listed(self):
        status, payload = self.call("POST", "/agent/jobs",
                                    {"name": "bad", "kind": "cron", "schedule": "nope",
                                     "target": "Arena"})
        self.assertEqual(status, 400)
        status, payload = self.call("POST", "/agent/jobs",
                                    {"name": "good", "kind": "cron",
                                     "schedule": "30 7 * * *", "target": "Arena",
                                     "payload": {"action": "flow"}})
        self.assertEqual(status, 201)
        job_id = payload["job"]["id"]
        status, payload = self.call("PUT", "/agent/jobs/%s" % job_id,
                                    {"schedule": "0 8 * * *"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["job"]["schedule"], "0 8 * * *")
        # The pause button sends only what changed; that must not be rejected
        # as an incomplete job.
        status, payload = self.call("PUT", "/agent/jobs/%s" % job_id,
                                    {"enabled": False})
        self.assertEqual(status, 200)
        self.assertFalse(payload["job"]["enabled"])
        self.assertEqual(payload["job"]["name"], "good",
                         "a partial update wiped the rest of the job")
        self.call("PUT", "/agent/jobs/%s" % job_id, {"enabled": True})
        status, payload = self.call("GET", "/agent/jobs")
        self.assertEqual(len(payload["jobs"]), 1)
        self.assertEqual(self.call("DELETE", "/agent/jobs/%s" % job_id)[0], 200)
        self.assertEqual(self.call("DELETE", "/agent/jobs/%s" % job_id)[0], 404)

    def test_a_missing_job_or_script_is_a_404(self):
        self.assertEqual(self.call("POST", "/agent/job-run", {"id": "nope"})[0], 404)
        self.assertEqual(self.call("POST", "/agent/script-run", {"id": "nope"})[0], 404)
        self.assertEqual(self.call("PUT", "/agent/scripts/nope", {"code": "x"})[0], 404)
        self.assertEqual(self.call("DELETE", "/agent/notes/nope")[0], 404)

    def test_job_run_answers_immediately_and_records_the_outcome(self):
        self.flows.save("Fast", schema.validate_flow({
            "name": "Fast",
            "steps": [{"id": "s1", "type": "goto_url",
                       "url": "https://example.com"}]})[0])
        job = self.hub.save_job({"name": "quick", "kind": "every", "schedule": "60",
                                 "target": "Fast", "payload": {"action": "flow"}})
        status, payload = self.call("POST", "/agent/job-run", {"id": job["id"]})
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "queued")
        deadline = time.time() + 15
        row = self.hub.store.get_job(job["id"])
        while time.time() < deadline and not row["last_status"]:
            time.sleep(0.05)
            row = self.hub.store.get_job(job["id"])
        self.assertEqual(row["last_status"], "done")
        # `wait` is still available for a caller that wants the outcome inline.
        status, payload = self.call("POST", "/agent/job-run",
                                    {"id": job["id"], "wait": True})
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "done")

    def test_a_request_without_an_id_is_a_400(self):
        self.assertEqual(self.call("POST", "/agent/job-run", {})[0], 400)
        self.assertEqual(self.call("POST", "/agent/script-run", {})[0], 400)
        self.assertEqual(self.call("POST", "/agent/script-decision", {})[0], 400)
        self.assertEqual(self.call("POST", "/agent/chat", {})[0], 400)
        self.assertEqual(self.call("POST", "/agent/captcha/execute", {})[0], 400)

    def test_chat_sends_the_same_context_the_prompt_route_builds(self):
        status, payload = self.call("POST", "/agent/chat",
                                    {"request": "روی اولین نتیجه کلیک کن"})
        self.assertEqual(status, 200)
        prompt = self.hub.llm.calls[-1]["prompt"]
        self.assertIn("روی اولین نتیجه کلیک کن", prompt,
                      "the operator's request never reached the model")
        self.assertIn("1366", prompt, "the viewport context is missing")

    def test_the_prompt_route_still_returns_plain_text(self):
        raw = json.dumps({"request": "سلام"}).encode()
        status, headers, data = self.api.handle("POST", "/prompt", body=raw,
                                                headers=self.headers)
        self.assertEqual(status, 200)
        self.assertIn("text/plain", headers["Content-Type"])
        self.assertIn("سلام", data.decode("utf-8"))

    def test_captcha_config_and_history_over_http(self):
        status, payload = self.call("GET", "/agent/captcha/config")
        self.assertEqual(payload["config"]["strategies"], ["vision", "human"])
        self.assertFalse(payload["config"]["autoClick"])
        status, payload = self.call("POST", "/agent/captcha/config",
                                    {"autoClick": True, "maxAttempts": 2,
                                     "enabled": True, "notifyOnEscalation": False})
        self.assertEqual(status, 200)
        self.assertTrue(payload["config"]["autoClick"],
                        "the checkbox the UI sends was silently ignored")
        self.assertEqual(payload["config"]["maxAttempts"], 2)
        self.assertFalse(payload["config"]["notifyOnEscalation"])
        # A key nobody knows about must fail loudly, not do nothing.
        self.assertEqual(self.call("POST", "/agent/captcha/config",
                                   {"autoClik": True})[0], 400)
        self.assertEqual(self.call("GET", "/agent/captcha/history")[0], 200)

    def test_executing_a_captcha_proposal_clicks_the_real_backend(self):
        status, payload = self.call("POST", "/agent/captcha/execute",
                                    {"actions": [{"type": "click", "x": 12, "y": 34}]})
        self.assertEqual(status, 200)
        self.assertIn(("click", 12, 34, "left", 1), self.backend.calls)

    def test_notes_and_audit_over_http(self):
        self.assertEqual(self.call("POST", "/agent/notes",
                                   {"body": "یادداشت", "kind": "note"})[0], 201)
        status, payload = self.call("GET", "/agent/notes")
        self.assertEqual(payload["notes"][0]["body"], "یادداشت")
        note_id = payload["notes"][0]["id"]
        self.assertEqual(self.call("DELETE", "/agent/notes/%s" % note_id)[0], 200)
        status, payload = self.call("GET", "/agent/audit")
        self.assertTrue(payload["audit"], "nothing was audited")
        self.assertNotIn("sk-", json.dumps(payload))

    def test_telegram_is_off_by_default_and_says_what_to_do(self):
        status, payload = self.call("GET", "/agent/telegram/status")
        self.assertEqual(payload["mode"], "off")
        self.assertEqual(self.call("POST", "/agent/telegram/test", {})[0], 400)
        self.assertEqual(self.call("GET", "/agent/telegram/targets")[0], 400)

    def test_the_ai_browser_reports_that_it_is_absent_here(self):
        status, payload = self.call("GET", "/agent/ai/status")
        self.assertEqual(status, 200)
        self.assertFalse(payload["available"])
        self.assertEqual(self.call("POST", "/agent/ai/open", {})[0], 400)

    def test_a_query_over_http_cannot_write(self):
        self.assertEqual(self.call("POST", "/agent/query",
                                   {"sql": "SELECT 1 AS one"})[0], 200)
        self.assertEqual(self.call("POST", "/agent/query",
                                   {"sql": "DROP TABLE audit"})[0], 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)


# ---------------------------------------------------------------------------
# the two halves of the feature must still agree
# ---------------------------------------------------------------------------
class RouteCoverageTest(TempDirCase):
    """Every path the sidebar calls has to exist in the route table.

    The agent UI was written against the API from memory once already. A jsdom
    test cannot catch a renamed route because its fetch is stubbed, so the two
    files are compared directly here instead.
    """

    JS_PATH = os.path.join(REPO_ROOT, "automation", "static", "automation.js")
    CALL = re.compile(r"""(?:api|imageBlobUrl)\(\s*(['"`])(/[^'"`]*)\1""")

    def setUp(self):
        super().setUp()
        from automation.api import _match
        self._match = _match
        with open(self.JS_PATH, encoding="utf-8") as handle:
            self.source = handle.read()
        self.data_dir = os.path.join(self.tmp, "data")
        os.makedirs(self.data_dir, exist_ok=True)
        flows = FlowStore(self.data_dir)
        engine = AutomationEngine(FakeBackend(), self.data_dir)
        hub = AgentHub(self.data_dir, engine=None, flows=flows, backend=None,
                       token=TOKEN)
        self.addCleanup(hub.store.close)
        self.api = AutomationApi(engine, flows, None, data_dir=self.data_dir,
                                 token=TOKEN, agent=hub)
        self.plain = AutomationApi(engine, flows, None, data_dir=self.data_dir,
                                   token=TOKEN)
        self.patterns = sorted({pattern for _, pattern, _ in self.api.routes})

    def called_paths(self):
        """The paths in the JavaScript, with template holes filled in."""
        paths = set()
        for _quote, raw in self.CALL.findall(self.source):
            path = re.sub(r"\$\{[^}]*\}", "x", raw)   # `${id}` is one segment
            path = path.split("?")[0].rstrip("/") or "/"
            paths.add(path)
        return paths

    def routed(self, path, patterns=None):
        return any(self._match(pattern, path) is not None
                   for pattern in (patterns if patterns is not None
                                   else self.patterns))

    def test_the_ui_only_calls_paths_the_server_routes(self):
        missing = [path for path in sorted(self.called_paths())
                   if not self.routed(path)]
        self.assertEqual(missing, [],
                         "the sidebar calls routes the server does not have")

    def test_every_agent_route_the_server_offers_is_reachable_from_the_ui(self):
        # The other direction: a route no pane calls is dead code, or a pane
        # that was never wired up.
        called = self.called_paths()
        unused = []
        for pattern in self.patterns:
            if not pattern.startswith("/agent"):
                continue
            # No synthetic probe here on purpose: the UI's template literals
            # (`/agent/jobs/${id}`) already normalise to a concrete segment, so
            # a wildcard pattern that nothing calls stays visible as unused.
            if not any(self.routed(path, [pattern]) for path in called):
                unused.append(pattern)
        self.assertEqual(unused, [], "agent routes exist that no pane ever calls")

    def test_the_agent_routes_are_only_present_when_an_agent_is_attached(self):
        self.assertEqual([pattern for _, pattern, _ in self.plain.routes
                          if pattern.startswith("/agent")], [],
                         "a build without an agent grew agent routes")
        self.assertLess(len(self.plain.routes), len(self.api.routes))


class ServerWiringTest(TempDirCase):
    """server.py's wiring, which nothing else in the suite exercises.

    `build_api` is where the agent, the engine and the notifier are tied
    together; a renamed keyword argument there would only show up at container
    startup, in front of the operator.
    """

    def build(self, *extra):
        from automation import server as srv
        web_root = os.path.join(self.tmp, "web")
        os.makedirs(web_root, exist_ok=True)
        args = srv.parse_args(["--web-root", web_root, "--data-dir", self.tmp,
                               "--public-base", PUBLIC_BASE] + list(extra))
        api = srv.build_api(args)
        if api.agent is not None:
            self.addCleanup(api.agent.stop)
        return api

    def test_an_agent_is_attached_and_hands_its_notifier_to_the_engine(self):
        api = self.build()
        self.assertIsNotNone(api.agent)
        self.assertIs(api.engine._notifier, api.agent.notifier,
                      "the engine cannot announce a handoff without this")
        self.assertEqual(api.agent.public_base, PUBLIC_BASE)
        self.assertEqual(api.agent.ai_port, 9223)
        self.assertEqual(api.agent.ai_display, ":2")
        # The agent's own browser is reachable through CDP, not through the
        # automation display - and it is not running here, which must be a
        # report rather than a crash.
        status = api.agent.ai_status()
        self.assertIn("available", status)

    def test_the_agent_can_be_started_and_stopped_with_its_scheduler(self):
        api = self.build()
        api.agent.start()
        self.assertTrue(api.agent.scheduler.alive)
        api.agent.stop()
        self.assertFalse(api.agent.scheduler.alive)

    def test_the_agent_switches_everything_off(self):
        api = self.build("--no-agent", "--ai-cdp-port", "9333")
        self.assertIsNone(api.agent)
        self.assertIsNone(api.engine._notifier)
        self.assertEqual([pattern for _, pattern, _ in api.routes
                          if pattern.startswith("/agent")], [],
                         "--no-agent still exposed agent routes")

    def test_the_flags_the_hostim_entrypoint_passes_are_all_accepted(self):
        # hostim/docker-entrypoint.sh builds exactly this command line.
        api = self.build("--ai-cdp-port", "9223", "--ai-display", ":2",
                         "--public-base", PUBLIC_BASE)
        self.assertEqual(api.agent.ai_port, 9223)
        with open(os.path.join(REPO_ROOT, "hostim", "docker-entrypoint.sh"),
                  encoding="utf-8") as handle:
            entrypoint = handle.read()
        for flag in ("--ai-cdp-port", "--ai-display", "--public-base"):
            self.assertIn(flag, entrypoint,
                          "%s is accepted by the server but never passed" % flag)


if __name__ == "__main__":
    unittest.main(verbosity=2)


# ---------------------------------------------------------------------------
# Phase 6: the operations library, the agent key, the chat tab, both Telegram
# channels, the visible pointer, and the manual primitives behind them.
# ---------------------------------------------------------------------------
class FakeEngine:
    """An engine that records instead of driving a display."""

    def __init__(self, busy=False, page="Start game\nBattle Arena"):
        self.busy_flag = busy
        self.started = []
        self.page = page
        self.backend = FakeBackend()

    def busy(self):
        return self.busy_flag

    def start(self, flow, **kwargs):
        self.started.append(flow)
        return {"runId": "run-1", "status": "running"}

    def status(self):
        return {"status": "busy" if self.busy_flag else "idle", "runId": "run-1"}

    def page_text(self, limit=2000):
        return self.page[:limit]


class FakeCursorFx:
    def __init__(self):
        self.configs = []
        self.installed = 0

    def configure(self, config=None):
        self.configs.append(dict(config or {}))
        self.installed += 1
        return {"ok": True, "size": (config or {}).get("size")}


class HubCase(TempDirCase):
    """A hub with fakes for everything that would touch a display or a socket."""

    def build(self, llm=None, engine=None, flows=None):
        backend = FakeBackend()
        flows = flows if flows is not None else FlowStore(self.tmp)
        hub = AgentHub(self.tmp, engine=None, flows=flows, backend=backend,
                       token=TOKEN, public_base=PUBLIC_BASE, clock=self.clock)
        hub.llm = llm if llm is not None else FakeLlm("سلام")
        hub.captcha.llm = hub.llm
        hub.engine = engine if engine is not None else FakeEngine()
        self.addCleanup(hub.store.close)
        return hub, backend, flows

    def api_for(self, hub, backend, flows, busy=False):
        engine = AutomationEngine(backend, self.tmp, notifier=hub.notifier)
        if busy:
            engine.busy = lambda: True
        return AutomationApi(engine, flows, None, data_dir=self.tmp, token=TOKEN,
                             agent=hub)

    def call(self, api, method, path, body=None, headers=None, query=None):
        raw = json.dumps(body).encode("utf-8") if body is not None else b""
        status, _, data = api.handle(
            method, path, body=raw, query=query or {},
            headers={"x-automation-token": TOKEN} if headers is None else headers)
        try:
            payload = json.loads(data.decode("utf-8"))
        except ValueError:
            payload = data[:200]
        return status, payload

    def join_chat(self, hub, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not [row for row in hub.store.list_chat(50)
                    if row["status"] == "thinking"]:
                return True
            for thread in list(hub._chat_threads):
                thread.join(0.05)
        return False


class OperationsLibraryTest(HubCase):
    def test_the_factory_operations_are_seeded_once_and_survive_a_restart(self):
        hub, _, _ = self.build()
        first = hub.operation_list()
        ids = [row["id"] for row in first]
        self.assertEqual(len(ids), len(set(ids)), "a built-in was seeded twice")
        self.assertEqual(len(first), 5)
        names = " ".join(row["name"] for row in first)
        for needle in ("lmarena", "کپچا", "دیپ‌سیک"):
            self.assertIn(needle, names)
        hub.store.close()

        # A second hub on the same directory is what a restart looks like.
        again = AgentHub(self.tmp, engine=None, flows=None, backend=None,
                         token=TOKEN, clock=self.clock)
        self.addCleanup(again.store.close)
        self.assertEqual([row["id"] for row in again.operation_list()], ids)

    def test_every_factory_operation_is_a_valid_flow(self):
        hub, _, _ = self.build()
        for operation in hub.operation_list():
            with self.subTest(operation=operation["id"]):
                flow = hub.library.as_flow(operation)
                _, errors = schema.validate_flow(flow)
                self.assertEqual(errors, [])
                self.assertTrue(flow["steps"])

    def test_the_deepseek_operation_opens_the_site_and_then_asks(self):
        hub, _, _ = self.build()
        by_key = {row["builtinKey"]: row for row in hub.operation_list()}
        opened = [step["type"] for step in by_key["deepseek-open"]["steps"]]
        asked = [step["type"] for step in by_key["deepseek-ask"]["steps"]]
        self.assertIn("agent_open", opened)
        self.assertIn("agent_ask", asked)
        provider = [step.get("provider") for step in by_key["deepseek-open"]["steps"]
                    if step["type"] == "agent_open"][0]
        self.assertEqual(provider, "deepseek")
        self.assertIn("chat.deepseek.com", DEFAULT_PROVIDERS[provider]["url"],
                      "the profile is what carries the address")

    def test_an_operation_can_be_created_edited_and_deleted(self):
        hub, _, _ = self.build()
        created = hub.operation_save({"name": "ورود به سایت",
                                      "description": "دو گام",
                                      "tags": ["login", "web"],
                                      "steps": [{"type": "goto_url", "url": "https://x.test/"},
                                                {"type": "wait", "ms": 500}]})
        self.assertTrue(created["id"])
        self.assertFalse(created["builtin"])
        self.assertEqual(len(created["steps"]), 2)

        edited = hub.operation_save({"name": "ورود به سایت (ویرایش)",
                                     "steps": [{"type": "wait", "ms": 10}]},
                                    operation_id=created["id"])
        self.assertEqual(edited["id"], created["id"])
        self.assertEqual(len(edited["steps"]), 1)
        self.assertEqual(len(hub.operation_list()), 6)

        self.assertEqual(hub.operation_delete(created["id"])["deleted"], created["id"])
        with self.assertRaises(AgentError) as caught:
            hub.operation_delete(created["id"])
        self.assertEqual(caught.exception.status, 404)

    def test_an_invalid_step_list_is_refused_before_it_is_stored(self):
        hub, _, _ = self.build()
        for bad, why in (([], "empty"), ([{"type": "nope"}], "unknown type"),
                         ([{"type": "click"}], "click without coordinates"),
                         ("not a list", "not a list")):
            with self.subTest(why=why):
                with self.assertRaises(AgentError) as caught:
                    hub.operation_save({"name": "bad", "steps": bad})
                self.assertEqual(caught.exception.status, 400)
        self.assertEqual(len(hub.operation_list()), 5, "a refused operation was stored")

    def test_a_builtin_can_be_reset_after_an_edit(self):
        hub, _, _ = self.build()
        target = [row for row in hub.operation_list() if row["builtinKey"] == "captcha"][0]
        hub.operation_save({"name": "کپچای من", "steps": [{"type": "wait", "ms": 5}]},
                           operation_id=target["id"])
        edited = hub.library.get(target["id"])
        self.assertEqual(edited["name"], "کپچای من")

        restored = hub.operation_reset(target["id"])
        self.assertEqual(restored["id"], target["id"])
        self.assertNotEqual(restored["name"], "کپچای من")
        self.assertEqual([step["type"] for step in restored["steps"]],
                         [step["type"] for step in target["steps"]])
        with self.assertRaises(AgentError):
            hub.operation_reset("op-does-not-exist")

    def test_running_an_operation_starts_its_flow_and_is_recorded(self):
        engine = FakeEngine()
        hub, _, _ = self.build(engine=engine)
        target = hub.operation_list()[0]
        result = hub.operation_run(target["id"])
        self.assertEqual(result["status"], "started")
        self.assertEqual(len(engine.started), 1)
        self.assertEqual(engine.started[0]["name"], target["name"])
        self.assertEqual(engine.started[0]["steps"][0]["type"],
                         target["steps"][0]["type"])
        stored = hub.library.get(target["id"])
        self.assertEqual(stored["run_count"], 1)
        self.assertEqual(stored["last_status"], "started")

    def test_a_busy_desktop_refuses_an_operation_run(self):
        hub, _, _ = self.build(engine=FakeEngine(busy=True))
        with self.assertRaises(AgentError) as caught:
            hub.operation_run(hub.operation_list()[0]["id"])
        self.assertEqual(caught.exception.status, 409)

    def test_the_kill_switch_stops_the_library_too(self):
        hub, _, _ = self.build()
        hub.set_kill_switch(True)
        with self.assertRaises(AgentError) as caught:
            hub.operation_run(hub.operation_list()[0]["id"])
        self.assertEqual(caught.exception.status, 409)


class AgentKeyTest(HubCase):
    def test_a_key_is_generated_once_and_never_leaks_into_the_overview(self):
        hub, _, _ = self.build()
        info = hub.agent_key_info()
        self.assertTrue(info["set"])
        self.assertTrue(info["enabled"])
        self.assertEqual(info["length"], 43)
        self.assertIn("…", info["prefix"])
        self.assertEqual(hub.agent_key_info()["createdAt"], info["createdAt"])

        secret = hub.store.get_secret(hub.store.AGENT_KEY)
        blob = json.dumps(hub.overview(), ensure_ascii=False)
        self.assertNotIn(secret, blob)
        self.assertNotIn(secret, json.dumps(hub.agent_key_info()))
        # It is not in the database either, only in the 0600 secrets file.
        with sqlite3.connect(hub.store.db_path) as db:
            dump = json.dumps(db.execute("SELECT key, value FROM settings").fetchall(),
                              ensure_ascii=False)
        self.assertNotIn(secret, dump)
        with open(hub.store.secrets_path, encoding="utf-8") as handle:
            self.assertIn(secret, handle.read(), "the key lives in the secrets file")

    def test_revealing_the_key_is_audited(self):
        hub, _, _ = self.build()
        secret = hub.store.get_secret(hub.store.AGENT_KEY)
        self.assertEqual(hub.agent_key_reveal()["key"], secret)
        actions = [row["action"] for row in hub.store.list_audit(limit=50)]
        self.assertIn("agentKey.revealed", actions)

    def test_rotating_invalidates_the_old_key(self):
        hub, _, _ = self.build()
        old = hub.store.get_secret(hub.store.AGENT_KEY)
        new = hub.agent_key_rotate()["key"]
        self.assertNotEqual(old, new)
        self.assertFalse(hub.check_agent_access(old))
        self.assertTrue(hub.check_agent_access(new))

    def test_disabling_cuts_every_route_but_not_the_platform_token(self):
        hub, _, _ = self.build()
        key = hub.store.get_secret(hub.store.AGENT_KEY)
        self.assertTrue(hub.check_agent_access(key))
        self.assertFalse(hub.agent_key_set_enabled(False)["enabled"])
        self.assertFalse(hub.check_agent_access(key),
                         "a disabled key must not work even with the right value")
        self.assertTrue(hub.agent_key_set_enabled(True)["enabled"])
        self.assertTrue(hub.check_agent_access(key))

    def test_a_wrong_key_is_simply_wrong(self):
        hub, _, _ = self.build()
        self.assertFalse(hub.check_agent_access(""))
        self.assertFalse(hub.check_agent_access("mas_not_the_key"))

    def test_the_api_accepts_the_key_in_three_places(self):
        hub, backend, flows = self.build()
        engine = AutomationEngine(backend, self.tmp, notifier=hub.notifier)
        api = AutomationApi(engine, flows, None, data_dir=self.tmp, token=TOKEN,
                            agent=hub)
        key = hub.store.get_secret(hub.store.AGENT_KEY)
        for headers in ({"authorization": "Bearer " + key}, {"x-agent-key": key}):
            with self.subTest(headers=list(headers)):
                self.assertEqual(api.authenticate("/agent", headers, {}), "agent")
                self.assertEqual(self.call(api, "GET", "/agent", headers=headers)[0], 200)
        self.assertEqual(api.authenticate("/agent", {}, {"k": [key]}), "agent")
        self.assertEqual(self.call(api, "GET", "/agent", query={"k": [key]})[0], 200)
        # And the key is remembered as used.
        self.assertTrue(hub.agent_key_info()["lastUsedAt"])

    def test_a_cut_key_answers_403_while_the_platform_token_still_works(self):
        hub, backend, flows = self.build()
        engine = AutomationEngine(backend, self.tmp, notifier=hub.notifier)
        api = AutomationApi(engine, flows, None, data_dir=self.tmp, token=TOKEN,
                            agent=hub)
        key = hub.store.get_secret(hub.store.AGENT_KEY)
        hub.agent_key_set_enabled(False)
        status, payload = self.call(api, "GET", "/agent", headers={"x-agent-key": key})
        self.assertEqual(status, 403)
        self.assertIn("error", payload)
        self.assertEqual(self.call(api, "GET", "/agent")[0], 200,
                         "the operator's own token must keep working")

    def test_the_kill_switch_cuts_the_key_too(self):
        hub, backend, flows = self.build()
        engine = AutomationEngine(backend, self.tmp, notifier=hub.notifier)
        api = AutomationApi(engine, flows, None, data_dir=self.tmp, token=TOKEN,
                            agent=hub)
        key = hub.store.get_secret(hub.store.AGENT_KEY)
        hub.set_kill_switch(True)
        self.assertEqual(api.authenticate("/agent", {"x-agent-key": key}, {}), "cut")
        self.assertEqual(self.call(api, "GET", "/agent",
                                   headers={"x-agent-key": key})[0], 403)

    def test_an_unknown_key_is_a_401_not_a_403(self):
        hub, backend, flows = self.build()
        engine = AutomationEngine(backend, self.tmp, notifier=hub.notifier)
        api = AutomationApi(engine, flows, None, data_dir=self.tmp, token=TOKEN,
                            agent=hub)
        status, payload = self.call(api, "GET", "/agent",
                                    headers={"x-agent-key": "nope"})
        self.assertEqual(status, 401)
        self.assertIn("error", payload)

    def test_the_index_documents_exactly_the_routes_the_server_has(self):
        hub, backend, flows = self.build()
        api = self.api_for(hub, backend, flows)
        routes = {(method, path) for method, path, _ in api.routes}
        indexed = {(method, re.sub(r"<[^>]+>", "*", path))
                   for method, path, _, _ in API_INDEX}
        self.assertEqual(sorted(routes - indexed), [],
                         "routes an agent may call but the index never mentions")
        self.assertEqual(sorted(indexed - routes), [],
                         "documented routes that do not exist")

    def test_the_endpoint_index_is_absolute_and_covers_the_agent_surface(self):
        hub, _, _ = self.build()
        endpoints = hub.api_index()
        self.assertGreaterEqual(len(endpoints), 30)
        for item in endpoints:
            with self.subTest(path=item["path"]):
                self.assertTrue(item["path"].startswith(PUBLIC_BASE + "/automation/api/"),
                                item["path"])
                self.assertIn(item["method"], ("GET", "POST", "PUT", "DELETE"))
                self.assertTrue(item["fa"] and item["en"])
        paths = [item["path"] for item in endpoints]
        for needle in ("/agent/key", "/agent/chat/send", "/agent/operations",
                       "/agent/cursor", "/run", "/detect"):
            self.assertTrue(any(path.endswith(needle) for path in paths), needle)


class ChatTabTest(HubCase):
    def test_a_turn_is_queued_and_answered_in_the_background(self):
        llm = FakeLlm("سلام! چطور می‌توانم کمک کنم؟")
        hub, _, _ = self.build(llm=llm)
        result = hub.chat_send("سلام", provider="deepseek")
        self.assertTrue(result["queued"])
        self.assertEqual([row["role"] for row in result["messages"]],
                         ["user", "assistant"])
        self.assertEqual(result["messages"][1]["status"], "thinking")
        self.assertIn("deepseek", result["providers"])

        self.assertTrue(self.join_chat(hub), "the answer never arrived")
        rows = hub.chat_messages()
        self.assertEqual([row["role"] for row in rows], ["user", "assistant"],
                         "a conversation reads oldest first")
        self.assertEqual(rows[1]["status"], "done")
        self.assertIn("چطور", rows[1]["body"])
        self.assertEqual(llm.calls[0]["chat_name"], "deepseek")
        self.assertIn("سلام", llm.calls[0]["prompt"])

    def test_the_history_is_handed_back_on_the_next_turn(self):
        llm = FakeLlm("جواب اول")
        hub, _, _ = self.build(llm=llm)
        hub.chat_send("پرسش اول")
        self.join_chat(hub)
        hub.chat_send("پرسش دوم")
        self.join_chat(hub)
        self.assertIn("پرسش اول", llm.calls[1]["prompt"])
        self.assertIn("جواب اول", llm.calls[1]["prompt"])

    def test_an_empty_message_is_refused(self):
        hub, _, _ = self.build()
        with self.assertRaises(AgentError):
            hub.chat_send("   ")

    def test_the_automation_context_is_read_only_and_optional(self):
        llm = FakeLlm("باشه")
        engine = FakeEngine(page="Start game")
        hub, _, flows = self.build(llm=llm, engine=engine)
        flows.save("Arena", {"name": "Arena", "steps": [{"type": "wait", "ms": 5}]})
        hub.chat_send("چی روی صفحه است؟", with_context=True)
        self.join_chat(hub)
        prompt = llm.calls[0]["prompt"]
        self.assertIn("Arena", prompt)
        self.assertIn("Start game", prompt)
        self.assertIn("عملیات کتابخانه", prompt)

        llm.calls.clear()
        hub.chat_send("بدون زمینه", with_context=False)
        self.join_chat(hub)
        self.assertNotIn("Start game", llm.calls[0]["prompt"])

    def test_a_flow_inside_an_answer_can_be_applied(self):
        reply = ("حتماً.\n```json\n{\"name\": \"پیشنهاد مدل\", \"steps\": "
                 "[{\"type\": \"goto_url\", \"url\": \"https://x.test/\"},"
                 "{\"type\": \"wait\", \"ms\": 300}]}\n```")
        hub, _, flows = self.build(llm=FakeLlm(reply))
        hub.chat_send("یک جریان بساز")
        self.join_chat(hub)
        answer = hub.chat_messages()[-1]
        self.assertIn("flow", answer["meta"])

        saved = hub.chat_apply_flow(answer["id"])
        self.assertEqual(saved["name"], "پیشنهاد مدل")
        self.assertEqual(len(saved["steps"]), 2)
        self.assertIsNotNone(flows.get("پیشنهاد مدل"))

    def test_applying_an_answer_without_a_flow_is_a_409(self):
        hub, _, _ = self.build(llm=FakeLlm("فقط یک جملهٔ معمولی."))
        hub.chat_send("سلام")
        self.join_chat(hub)
        answer = hub.chat_messages()[-1]
        with self.assertRaises(AgentError) as caught:
            hub.chat_apply_flow(answer["id"])
        self.assertEqual(caught.exception.status, 409)
        with self.assertRaises(AgentError) as caught:
            hub.chat_apply_flow("chat-nope")
        self.assertEqual(caught.exception.status, 404)

    def test_a_broken_model_is_reported_in_the_pane_not_as_a_500(self):
        class Broken(FakeLlm):
            def complete(self, *args, **kwargs):
                raise LlmError("the chat page never answered")

        hub, _, _ = self.build(llm=Broken("x"))
        hub.chat_send("سلام")
        self.assertTrue(self.join_chat(hub))
        row = hub.chat_messages()[-1]
        self.assertEqual(row["status"], "failed")
        self.assertIn("never answered", row["error"])

    def test_clearing_the_conversation_empties_it(self):
        hub, _, _ = self.build()
        hub.chat_send("سلام")
        self.join_chat(hub)
        self.assertTrue(hub.chat_messages())
        result = hub.chat_clear()
        self.assertGreaterEqual(result["cleared"], 2)
        self.assertEqual(hub.chat_messages(), [])


class TelegramChannelsTest(HubCase):
    def test_both_channels_can_be_on_at_once(self):
        hub, _, _ = self.build()
        hub.save_settings({"telegram.mode": "both"})
        self.assertEqual(hub.notifier.channels("handoff"), ["bot", "account"])
        self.assertEqual(hub.telegram_status()["mode"], "both")

    def test_each_kind_of_message_can_pick_its_own_channel(self):
        hub, _, _ = self.build()
        hub.save_settings({"telegram.mode": "both",
                           "telegram.channel.handoff": "account",
                           "telegram.channel.captcha": "bot"})
        self.assertEqual(hub.notifier.channels("handoff"), ["account"])
        self.assertEqual(hub.notifier.channels("captcha"), ["bot"])
        self.assertEqual(hub.notifier.channels("jobs"), ["bot", "account"],
                         "an unrouted purpose follows the global mode")
        status = hub.telegram_status()
        self.assertEqual(status["routing"]["handoff"], "account")
        self.assertEqual(status["routing"]["jobs"], "")

    def test_an_unknown_routing_value_is_refused(self):
        hub, _, _ = self.build()
        with self.assertRaises(AgentError):
            hub.save_settings({"telegram.channel.jobs": "carrier-pigeon"})

    def test_off_still_means_off(self):
        hub, _, _ = self.build()
        hub.save_settings({"telegram.mode": "off", "telegram.channel.jobs": "bot"})
        self.assertEqual(hub.notifier.channels("jobs"), [])

    def test_the_channel_status_says_what_each_side_needs(self):
        hub, _, _ = self.build()
        status = hub.telegram_status()["channels"]
        self.assertIn("BotFather", status["bot"]["needs"])
        self.assertIn("my.telegram.org", status["account"]["needs"])
        self.assertFalse(status["bot"]["configured"])
        self.assertFalse(status["account"]["sessionExists"])
        hub.save_settings({"telegram.botToken": "12345:abc"})
        self.assertTrue(hub.telegram_status()["channels"]["bot"]["configured"])

    def test_sending_names_a_target_and_a_channel(self):
        hub, _, _ = self.build()
        hub.save_settings({"telegram.mode": "bot",
                           "telegram.targets": [{"id": -100123, "title": "گروه من",
                                                 "type": "supergroup"},
                                                {"id": 42, "title": "من",
                                                 "type": "private"}]})
        sent = []
        hub.notifier.send = lambda text, targets=None, purpose="manual", channel=None: (
            sent.append({"text": text, "targets": targets, "purpose": purpose,
                         "channel": channel})
            or {"sent": 1, "failed": 0, "channels": [channel or "bot"]})

        hub.telegram_send("سلام", target=-100123, channel="account", purpose="manual")
        self.assertEqual(sent[0]["targets"], [{"id": -100123, "title": "گروه من",
                                               "type": "supergroup"}])
        self.assertEqual(sent[0]["channel"], "account")

        hub.telegram_send("به همه")
        self.assertIsNone(sent[1]["targets"], "no target means every saved target")

        with self.assertRaises(AgentError) as caught:
            hub.telegram_send("سلام", target=999)
        self.assertEqual(caught.exception.status, 404)
        with self.assertRaises(AgentError):
            hub.telegram_send("")
        with self.assertRaises(AgentError):
            hub.telegram_send("سلام", channel="pigeon")
        with self.assertRaises(AgentError):
            hub.telegram_send("سلام", purpose="nonsense")

    def test_a_captcha_escalation_is_routed_as_a_captcha_message(self):
        hub, _, _ = self.build()
        seen = []
        hub.notifier.notify_handoff = lambda payload: seen.append(payload)
        hub.captcha.escalate({"kind": "image_select", "shot": "a.png",
                              "summary": "سه تصویر"})
        self.assertEqual(seen[0]["purpose"], "captcha")


class CursorTest(HubCase):
    def test_the_defaults_are_a_big_yellow_pointer_with_a_ripple(self):
        hub, _, _ = self.build()
        info = hub.cursor_info()
        self.assertTrue(info["enabled"])
        self.assertEqual(info["size"], 44)
        self.assertEqual(info["color"], "#ffd400")
        self.assertTrue(info["ripple"])

    def test_the_size_and_the_colours_are_validated(self):
        hub, _, _ = self.build()
        saved = hub.cursor_set({"size": 64, "color": "#ff0000", "ripple": False})
        self.assertEqual(saved["size"], 64)
        self.assertFalse(saved["ripple"])
        for bad in ({"size": 4}, {"size": 400}, {"color": "yellow"},
                    {"outline": "#gggggg"}, {"nonsense": 1}):
            with self.subTest(bad=bad):
                with self.assertRaises(AgentError):
                    hub.cursor_set(bad)

    def test_saving_pushes_the_settings_into_the_live_browser(self):
        hub, _, _ = self.build()
        effect = FakeCursorFx()
        hub.engine.backend.cursor_fx = effect
        hub.cursor_set({"size": 72, "color": "#00ff00"})
        self.assertEqual(effect.installed, 1)
        self.assertEqual(effect.configs[0]["size"], 72)
        self.assertEqual(effect.configs[0]["color"], "#00ff00")

    def test_a_missing_browser_is_not_an_error(self):
        hub, _, _ = self.build()
        result = hub.cursor_apply()
        self.assertFalse(result["applied"])
        self.assertIn("reason", result)

    def test_the_settings_are_plain_and_persisted(self):
        hub, _, _ = self.build()
        hub.save_settings({"cursor.size": 50, "cursor.enabled": False})
        self.assertEqual(hub.store.get_setting("cursor.size"), 50)
        self.assertIs(hub.store.get_setting("cursor.enabled"), False)
        self.assertFalse(hub.cursor_info()["enabled"])


class ManualControlTest(HubCase):
    def test_a_click_reaches_the_pointer(self):
        hub, backend, flows = self.build()
        api = self.api_for(hub, backend, flows)
        status, payload = self.call(api, "POST", "/control",
                                    {"action": "click", "x": 512, "y": 384})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertIn(("click", 512, 384, "left", 1), backend.calls)

    def test_the_other_primitives_are_available_too(self):
        hub, backend, flows = self.build()
        api = self.api_for(hub, backend, flows)
        for body, expected in (
                ({"action": "double_click", "x": 10, "y": 20},
                 ("click", 10, 20, "left", 2)),
                ({"action": "move", "x": 1, "y": 2}, ("move", 1, 2)),
                ({"action": "type", "text": "سلام"}, ("type", "سلام")),
                ({"action": "key", "keys": ["ctrl", "a"]}, ("key", ("ctrl", "a"))),
                ({"action": "goto_url", "url": "https://x.test/"},
                 ("goto_url", "https://x.test/"))):
            with self.subTest(action=body["action"]):
                status, _ = self.call(api, "POST", "/control", body)
                self.assertEqual(status, 200)
                self.assertIn(expected, backend.calls)

    def test_a_run_that_owns_the_mouse_refuses_a_manual_click(self):
        hub, backend, flows = self.build()
        api = self.api_for(hub, backend, flows, busy=True)
        status, _ = self.call(api, "POST", "/control",
                              {"action": "click", "x": 1, "y": 1})
        self.assertEqual(status, 409)
        self.assertEqual(backend.calls, [], "a refused click must not move anything")

    def test_bad_input_is_a_400_and_a_failed_click_is_a_502(self):
        hub, backend, flows = self.build()
        api = self.api_for(hub, backend, flows)
        for body in ({"action": "fly"}, {"action": "click", "x": "soon", "y": 1},
                     {"action": "key", "keys": []}):
            with self.subTest(body=body):
                status, _ = self.call(api, "POST", "/control", body)
                self.assertEqual(status, 400)

        backend.click = lambda *args, **kwargs: (1, "", "xdotool: no display")
        status, payload = self.call(api, "POST", "/control",
                                    {"action": "click", "x": 1, "y": 1})
        self.assertEqual(status, 502)
        self.assertIn("xdotool", payload["error"])

    def test_the_pause_resume_vocabulary_still_works(self):
        hub, backend, flows = self.build()
        api = self.api_for(hub, backend, flows)
        for action in ("pause", "resume", "stop"):
            with self.subTest(action=action):
                status, _ = self.call(api, "POST", "/control", {"action": action})
                self.assertIn(status, (200, 409))


class TelegramTestRouteTest(HubCase):
    def test_a_test_message_needs_a_target(self):
        hub, backend, flows = self.build()
        engine = AutomationEngine(backend, self.tmp, notifier=hub.notifier)
        api = AutomationApi(engine, flows, None, data_dir=self.tmp, token=TOKEN,
                            agent=hub)
        hub.save_settings({"telegram.mode": "bot",
                           "telegram.targets": [{"id": 42, "title": "من",
                                                 "type": "private"}]})
        status, payload = self.call(api, "POST", "/agent/telegram/test", {})
        self.assertEqual(status, 400)
        self.assertIn("target", payload["error"])

        sent = []
        hub.notifier.send = lambda text, targets=None, purpose="manual", channel=None: (
            sent.append({"targets": targets, "purpose": purpose, "channel": channel})
            or {"sent": 1, "failed": 0, "channels": ["bot"]})
        status, payload = self.call(api, "POST", "/agent/telegram/test",
                                    {"target": 42, "text": "سلام", "channel": "bot"})
        self.assertEqual(status, 200)
        self.assertEqual(sent[0]["purpose"], "manual")
        self.assertEqual(sent[0]["channel"], "bot")
        self.assertEqual(payload["sent"], 1)


class StepCatalogTest(unittest.TestCase):
    def test_every_step_type_the_server_accepts_is_documented(self):
        catalog = schema.step_catalog()
        self.assertEqual(set(catalog), set(schema.STEP_TYPES))
        for kind, entry in catalog.items():
            with self.subTest(kind=kind):
                self.assertEqual(entry["required"],
                                 list(schema.STEP_TYPES[kind]["required"]))
                self.assertTrue(entry["fa"], "a Persian line is the point of the catalog")
                self.assertTrue(entry["en"])
                self.assertIn("id", entry["common"])

    def test_the_agent_steps_are_part_of_the_same_catalog(self):
        catalog = schema.step_catalog()
        for kind in ("agent_open", "agent_ask", "captcha_solve"):
            self.assertIn(kind, catalog)
        self.assertEqual(catalog["agent_ask"]["required"], ["prompt"])


class EngineCursorTest(TempDirCase):
    """The pointer effect is decoration: it must run, and must never break a run.

    These use the real ControlBackend with `control()` stubbed, because the
    ripple and the reinstall live in the backend's own primitives - a fake
    backend would prove nothing.
    """

    class Recorder:
        def __init__(self, fail=False):
            self.calls = []
            self.fail = fail

        def click_effect(self, x, y):
            self.calls.append(("ripple", x, y))
            if self.fail:
                raise RuntimeError("CDP went away")

        def install(self):
            self.calls.append(("install",))
            if self.fail:
                raise RuntimeError("CDP went away")

    def backend(self, effect=None, rc=0):
        from automation.engine import ControlBackend
        backend = ControlBackend("/nonexistent/browser_control.sh")
        backend.commands = []
        backend.control = lambda args, timeout=None: (
            backend.commands.append(tuple(args)) or (rc, "", "" if rc == 0 else "failed"))
        backend.cursor_fx = effect
        return backend

    def test_a_click_draws_the_ripple_before_the_click(self):
        effect = self.Recorder()
        backend = self.backend(effect)
        AutomationEngine(backend, self.tmp)
        rc, _, _ = backend.click(120, 240)
        self.assertEqual(rc, 0)
        self.assertEqual(effect.calls, [("ripple", 120, 240)])
        self.assertEqual(backend.commands, [("click", 120, 240, "left")])

    def test_a_double_click_ripples_once_at_the_same_point(self):
        effect = self.Recorder()
        backend = self.backend(effect)
        AutomationEngine(backend, self.tmp)
        backend.click(10, 20, "left", 2)
        self.assertEqual(effect.calls, [("ripple", 10, 20)])
        self.assertEqual(backend.commands, [("doubleclick", 10, 20)])

    def test_navigating_puts_the_pointer_back(self):
        effect = self.Recorder()
        backend = self.backend(effect)
        AutomationEngine(backend, self.tmp)
        backend.goto_url("https://lmarena.ai/")
        self.assertEqual(effect.calls, [("install",)])

    def test_a_failed_navigation_does_not_reinstall(self):
        effect = self.Recorder()
        backend = self.backend(effect, rc=1)
        AutomationEngine(backend, self.tmp)
        rc, _, _ = backend.goto_url("https://lmarena.ai/")
        self.assertEqual(rc, 1)
        self.assertEqual(effect.calls, [])

    def test_a_broken_effect_cannot_break_a_click(self):
        effect = self.Recorder(fail=True)
        backend = self.backend(effect)
        AutomationEngine(backend, self.tmp)
        rc, _, _ = backend.click(10, 20)
        self.assertEqual(rc, 0, "a cosmetic failure must not fail the step")
        rc, _, _ = backend.goto_url("https://x.test/")
        self.assertEqual(rc, 0)

    def test_the_engine_hands_its_effect_to_a_backend_without_one(self):
        effect = self.Recorder()
        backend = self.backend(None)
        engine = AutomationEngine(backend, self.tmp, cursor_fx=effect)
        self.assertIs(backend.cursor_fx, effect)
        backend.click(1, 2)
        self.assertEqual(effect.calls, [("ripple", 1, 2)])

    def test_page_text_is_a_read_only_window_for_the_chat(self):
        engine = AutomationEngine(self.backend(None), self.tmp)
        engine._page_text = lambda: (0, "Battle Arena\nStart game\n" + "x" * 500, "")
        self.assertEqual(len(engine.page_text(20)), 20)
        self.assertIn("Battle Arena", engine.page_text())
        engine._page_text = lambda: (1, "", "cdp refused")
        self.assertEqual(engine.page_text(), "")
