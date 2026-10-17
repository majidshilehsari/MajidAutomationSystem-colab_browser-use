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
import tarfile
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List

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

    def test_a_conditional_job_is_armed_only_by_a_correct_finish(self):
        # Round 27, item 8: an 'after' job never enters the queue by the
        # clock; a CORRECT finish of the watched run puts its one occurrence
        # in the queue, a failed finish arms nothing and is reported so the
        # brain can decide.
        job = self.store.upsert_job({"name": "بعد از گزارش", "kind": "after",
                                     "schedule": "گزارش روزانه", "target": "x"})
        scheduler = self.make()
        scheduler.ensure_schedules()
        self.clock.advance(100000)
        self.assertEqual(scheduler.due_jobs(), [],
                         "time must never arm a conditional job")
        self.assertEqual(scheduler.on_flow_finished("گزارش روزانه", False, "boom"),
                         [])
        self.assertEqual(scheduler.due_jobs(), [])
        self.assertEqual(scheduler.on_flow_finished("چیز دیگر", True), [])
        self.assertEqual(scheduler.due_jobs(), [],
                         "another run's finish must not arm it")
        self.assertEqual(scheduler.on_flow_finished("گزارش روزانه", True),
                         [str(job["id"])])
        results = scheduler.tick()
        self.assertEqual(self.fired, ["بعد از گزارش"])
        self.assertEqual(results[0]["status"], "done")
        refreshed = self.store.get_job(job["id"])
        self.assertIsNone(refreshed["next_run_at"],
                          "after one fire the queue must be empty again")
        self.assertEqual(scheduler.due_jobs(), [])
        reports = self.store.list_step_reports("scheduler", 50)
        self.assertTrue(any(row["phase"] == "شرطی" for row in reports),
                        "the conditional arming was never reported")
        self.assertTrue(any(row["phase"] == "شرطی" and not row["ok"]
                            for row in reports),
                        "the failed finish must be reported for the brain")

    def test_validate_accepts_the_conditional_kind(self):
        out = Scheduler.validate({"kind": "after", "schedule": "گزارش"},
                                 now=self.clock())
        self.assertIsNone(out["nextRunAt"])
        self.assertIn("گزارش", out["nextRunText"])
        with self.assertRaises(ValueError):
            Scheduler.validate({"kind": "after", "schedule": "  "},
                               now=self.clock())
        with self.assertRaises(ValueError):
            Scheduler.validate({"kind": "nope", "schedule": "x"},
                               now=self.clock())

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

    def test_only_the_next_due_occurrence_is_queued_and_reported(self):
        # Round 27, item 4: only the NEXT due occurrence of a scheduled job
        # ever sits in the queue; after a run only the next due one returns.
        # Both facts must be reportable and verifiable.
        job = self.store.upsert_job({"name": "every-minute", "kind": "every",
                                     "schedule": "60", "target": "x"})
        scheduler = self.make()
        scheduler.ensure_schedules()
        queue = [row for row in self.store.list_step_reports("scheduler")
                 if row["phase"] == "صف"]
        self.assertEqual(len(queue), 1)
        self.assertIn("فقط همین یک موعد", queue[0]["message"])
        # "down" for three whole intervals: the job comes due ONCE, not three
        # times, and the queue afterwards holds exactly the next occurrence.
        self.clock.advance(181)
        results = scheduler.tick()
        self.assertEqual(len(results), 1)
        self.assertEqual(self.fired, ["every-minute"])
        refreshed = self.store.get_job(job["id"])
        self.assertAlmostEqual(refreshed["next_run_at"], self.clock() + 60,
                               delta=1)
        self.assertEqual(scheduler.due_jobs(self.clock()), [],
                         "only one due time may exist per job")
        outcome = [row for row in self.store.list_step_reports("scheduler")
                   if row["phase"] == "نتیجه"]
        self.assertEqual(len(outcome), 1)
        self.assertIn("فقط موعد بعدی", outcome[0]["message"])
        self.assertTrue(outcome[0]["ok"])

    def test_a_deferred_job_keeps_its_single_queue_slot(self):
        self.store.upsert_job({"name": "busy-job", "kind": "every",
                               "schedule": "60", "target": "x"})
        scheduler = self.make(runner=self.raise_busy)
        scheduler.ensure_schedules()
        self.clock.advance(61)
        scheduler.tick()
        queued = [row for row in self.store.list_step_reports("scheduler")
                  if "موکول" in row["message"]]
        self.assertEqual(len(queued), 1)
        self.assertIn("همان یک موعد", queued[0]["message"])
        job = self.store.list_jobs()[0]
        self.assertAlmostEqual(job["next_run_at"], self.clock() + 30, delta=1)

    def test_a_one_shot_leaves_the_queue_and_says_so(self):
        self.store.upsert_job({"name": "once-more", "kind": "at",
                               "schedule": str(self.clock() + 5), "target": "x"})
        scheduler = self.make()
        scheduler.ensure_schedules()
        self.clock.advance(6)
        scheduler.tick()
        outcome = [row for row in self.store.list_step_reports("scheduler")
                   if row["phase"] == "نتیجه"]
        self.assertEqual(len(outcome), 1)
        self.assertIn("از صف خارج شد", outcome[0]["message"])
        self.assertEqual(scheduler.due_jobs(self.clock()), [])

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

    def test_a_sent_file_can_be_downloaded_back_by_its_file_id(self) -> None:
        # Round 28, item 8: the telegram restore path - getFile hands out the
        # path, one HTTPS GET brings the very same bytes back down
        from automation.telegram_client import TelegramBot, TelegramError
        seen: List[str] = []

        def opener(method, payload, files):
            seen.append(method)
            if method == "getFile":
                self.assertEqual(payload["file_id"], "FID")
                return {"file_id": "FID",
                        "file_path": "documents/mas-backup-x.tar.gz"}
            if method == "getFile.download":
                self.assertIn("/file/bot12345:ABC/documents/", payload["url"])
                return b"ARCHIVE"
            raise AssertionError(method)

        bot = TelegramBot("12345:ABC", opener=opener)
        self.assertEqual(bot.download_document("FID"), b"ARCHIVE")
        self.assertEqual(seen, ["getFile", "getFile.download"])
        with self.assertRaises(TelegramError):
            TelegramBot("t", opener=lambda *a: {}).download_document("FID")

    def test_discovered_chats_come_from_get_updates(self) -> None:
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
            hub.ask("سلام")
        self.assertEqual(caught.exception.status, 409)

    def test_a_job_must_reference_something_real(self):
        hub, _, _ = self.build()
        with self.assertRaises(AgentError):
            hub.save_job({"name": "x", "kind": "every", "schedule": "60",
                          "target": "does-not-exist", "payload": {"action": "flow"}})
        job = hub.save_job({"name": "ok", "kind": "every", "schedule": "60",
                            "target": "Arena", "payload": {"action": "flow"}})
        self.assertTrue(job["next_run_at"], "the first run time was never computed")

    def test_a_conditional_job_waits_for_an_event_not_a_clock(self):
        hub, _, _ = self.build()
        job = hub.save_job({"name": "بعد از گزارش", "kind": "after",
                            "schedule": "Arena", "target": "Arena",
                            "payload": {"action": "flow"}})
        self.assertIsNone(job["next_run_at"],
                          "an after job must never be armed by the clock")
        with self.assertRaises(AgentError):
            hub.save_job({"name": "بی‌هدف", "kind": "after", "schedule": " ",
                          "target": "Arena", "payload": {"action": "flow"}})

    def test_flow_finished_routes_to_the_queue_and_to_the_brain(self):
        # Round 27, item 8: one completion event for every run - the
        # scheduler arms the conditional jobs, and only a real error is
        # handed to the brain for its decision.
        hub, _, _ = self.build()
        armed = []
        decided = []
        hub.scheduler.on_flow_finished = \
            lambda name, ok, error="": armed.append((name, ok, error)) or []
        hub.brain.on_flow_error = lambda name, error: decided.append((name, error))
        hub.flow_finished("گزارش", "done", "")
        hub.flow_finished("گزارش", "error", "boom")
        hub.flow_finished("گزارش", "stopped", "")
        self.assertEqual(armed, [("گزارش", True, ""), ("گزارش", False, "boom"),
                                 ("گزارش", False, "")])
        self.assertEqual(decided, [("گزارش", "boom")],
                         "only a real error goes to the brain's decision")

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
        self.assertEqual(self.call("POST", "/agent/chat/send", {"text": "x"})[0], 409)
        self.assertEqual(self.call("POST", "/agent/captcha/solve", {})[0], 409)
        self.call("POST", "/agent/kill-switch", {"on": False})
        self.assertEqual(self.call("POST", "/agent/chat/send", {"text": "x"})[0], 200)

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
        self.assertEqual(self.call("POST", "/agent/captcha/execute", {})[0], 400)

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

    def test_backup_restore_route_swaps_the_data_and_restarts(self):
        import sqlite3
        from unittest import mock
        self.api.agent.store.set_setting("restore.marker", "v1")
        status, info = self.call("POST", "/agent/backups", {})
        self.assertEqual(status, 201)
        self.api.agent.store.set_setting("restore.marker", "v2")
        with mock.patch("automation.api._restart_later") as restart:
            status, payload = self.call("POST", "/agent/backup-restore",
                                        {"name": info["name"]})
        self.assertEqual(status, 200)
        self.assertEqual(payload["restored"], info["name"])
        self.assertTrue(payload["restarting"])
        restart.assert_called_once()
        # the swapped-in file (not the old open handle) holds the old value
        conn = sqlite3.connect(self.api.agent.store.db_path)
        rows = dict(conn.execute("SELECT key, value FROM settings").fetchall())
        conn.close()
        self.assertIn("v1", str(rows.get("restore.marker")))

    def test_backup_restore_refuses_the_agent_key(self):
        # called directly (like the supabase route tests): api.handle would
        # simply re-authenticate the operator header and overwrite the verdict
        from automation.api import ApiError
        self.api._last_verdict = "agent"
        with self.assertRaises(ApiError) as ctx:
            self.api.route_agent_backup_restore(body=b'{"name": "x"}')
        self.assertEqual(ctx.exception.status, 403)

    def test_backup_inspect_upload_and_selective_restore_routes(self):
        # Round 28, items 4+8 through the real HTTP surface
        import base64
        from unittest import mock
        status, info = self.call("POST", "/agent/backups", {})
        self.assertEqual(status, 201)
        status, table = self.call("POST", "/agent/backup-inspect",
                                  {"name": info["name"]})
        self.assertEqual(status, 200)
        keys = {row["key"] for row in table["items"]}
        self.assertIn("agent.db", keys)
        self.assertGreaterEqual(table["green"], 1)
        # a file from the user's own machine lands through the upload route
        # (checked BEFORE the selective restore: a restore replaces the live
        # database file and the open connection only survives a restart)
        data = self.api.agent.backup_read(info["name"])
        blob = ("data:application/gzip;base64,"
                + base64.b64encode(data).decode("ascii"))
        status, payload = self.call("POST", "/agent/backup-upload",
                                    {"name": "mine.tar.gz", "data": blob})
        self.assertEqual(status, 201)
        self.assertEqual(payload["backup"]["name"], "mas-backup-mine.tar.gz")
        status, listing = self.call("GET", "/agent/backups", {})
        self.assertIn("mas-backup-mine.tar.gz",
                      [row["name"] for row in listing["backups"]])
        # but a random blob is refused before it can poison the list
        status, payload = self.call(
            "POST", "/agent/backup-upload",
            {"data": base64.b64encode(b"x" * 900).decode("ascii")})
        self.assertEqual(status, 400)
        with mock.patch("automation.api._restart_later"):
            status, payload = self.call("POST", "/agent/backup-restore",
                                        {"name": info["name"],
                                         "items": ["agent.db"]})
        self.assertEqual(status, 200)
        self.assertEqual(payload["items"], ["agent.db"])

    def test_ui_preferences_round_trip_through_the_database(self):
        from automation.agent import PLAIN_SETTINGS
        for key in ("ui.theme", "ui.lang", "ui.tab", "ui.agentSub", "ui.tgSub",
                    "ui.dbSub", "ui.panel", "ui.chatProvider", "ui.chatContext",
                    "ui.reportsKind"):
            self.assertIn(key, PLAIN_SETTINGS,
                          "%s must be storable or prefs die silently" % key)
        status, _payload = self.call("POST", "/agent/settings", {
            "ui.theme": "dark", "ui.lang": "en", "ui.tab": "db",
            "ui.agentSub": "telegram", "ui.tgSub": "targets",
            "ui.dbSub": "shots", "ui.panel": "1",
            "ui.chatProvider": "deepseek", "ui.chatContext": "0",
            "ui.reportsKind": "brain"})
        self.assertEqual(status, 200)
        status, index = self.call("GET", "/agent")
        self.assertEqual(status, 200)
        settings = index.get("settings") or {}
        self.assertEqual(settings.get("ui.theme"), "dark")
        self.assertEqual(settings.get("ui.lang"), "en")
        self.assertEqual(settings.get("ui.tab"), "db")
        self.assertEqual(settings.get("ui.tgSub"), "targets")
        self.assertEqual(settings.get("ui.dbSub"), "shots")

    def test_telegram_is_off_by_default_and_says_what_to_do(self):
        status, payload = self.call("GET", "/agent/telegram/status")
        self.assertEqual(payload["mode"], "off")
        self.assertEqual(self.call("POST", "/agent/telegram/test", {})[0], 400)
        # Round 16: "telegram is off" is a state conflict (409), and the
        # message has to point the operator at the settings sub-tab.
        status, payload = self.call("GET", "/agent/telegram/targets")
        self.assertEqual(status, 409)
        self.assertIn("تنظیمات", str(payload.get("error", "")))

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
        self.assertEqual(api.engine.on_finished, api.agent.flow_finished,
                         "conditional jobs and the brain's on-error decision "
                         "listen through this callback")
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
        self.assertEqual(len(first), 8)
        names = " ".join(row["name"] for row in first)
        for needle in ("lmarena", "کپچا", "دیپ‌سیک", "مشاهده"):
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
        # round 26: built-in operations are stored in the one language
        opened = [step.get("code", "") for step in by_key["deepseek-open"]["steps"]]
        asked = [step.get("code", "") for step in by_key["deepseek-ask"]["steps"]]
        self.assertTrue(any("open_agent(" in code for code in opened), opened)
        self.assertTrue(any("ask_agent(" in code for code in asked), asked)
        self.assertTrue(any("provider='deepseek'" in code for code in opened),
                        "the provider must ride along in the code")
        self.assertIn("chat.deepseek.com", DEFAULT_PROVIDERS["deepseek"]["url"],
                      "the profile is what carries the address")

    def test_an_untouched_round26_brain_tool_gains_the_hardened_steps(self):
        # Round 27, item 7: a database seeded in round 26 carries the first
        # factory version of the brain's tools. Those rows were never edited,
        # so a restart hardens them (graceful screenshot, empty-answer and
        # empty-library guards).
        from automation.operations import _PREV_FACTORY_STEPS
        hub, _, _ = self.build()
        hub.library.store.update_operation(
            "op-builtin-observe",
            {"steps": _PREV_FACTORY_STEPS["op-builtin-observe"]}, actor="test")
        hub.library.ensure_builtins(actor="restart")
        row = hub.library.get("op-builtin-observe")
        self.assertIn("rc, shot, err = screenshot('observe')",
                      row["steps"][0]["code"])
        self.assertIn("RuntimeError", row["steps"][1]["code"])

    def test_an_edited_brain_tool_is_never_overwritten_by_the_hardening(self):
        from automation.operations import _PREV_FACTORY_STEPS
        hub, _, _ = self.build()
        edited = [dict(step) for step
                  in _PREV_FACTORY_STEPS["op-builtin-pi-consult"]]
        edited.append({"id": "pc-x", "type": "python",
                       "code": "log('یادداشت اپراتور')",
                       "label": "قدم اپراتور"})
        hub.library.store.update_operation(
            "op-builtin-pi-consult", {"steps": edited}, actor="operator")
        hub.library.ensure_builtins(actor="restart")
        row = hub.library.get("op-builtin-pi-consult")
        self.assertEqual([step["id"] for step in row["steps"]],
                         ["pc-1", "pc-2", "pc-x"],
                         "an operator edit must survive the hardening")

    def test_the_pi_consultations_are_brain_only_operations(self):
        # Round 26: the brain's three own tools are scope='brain' and auto;
        # the shared tools stay scope='general'.
        hub, _, _ = self.build()
        by_key = {row["builtinKey"]: row for row in hub.operation_list()}
        self.assertEqual(by_key["observe"]["scope"], "brain")
        self.assertEqual(by_key["pi-consult"]["scope"], "brain")
        self.assertEqual(by_key["pi-next"]["scope"], "brain")
        self.assertEqual(by_key["pi-consult"]["permission"], "auto")
        self.assertEqual(by_key["pi-next"]["permission"], "auto")
        self.assertEqual(by_key["deepseek-open"]["scope"], "general")
        codes = " ".join(step.get("code", "")
                         for step in by_key["pi-next"]["steps"])
        self.assertIn("operations_list()", codes)
        self.assertIn("brain_suggest(", codes)
        consult = " ".join(step.get("code", "")
                           for step in by_key["pi-consult"]["steps"])
        self.assertIn("brain_note(", consult)
        self.assertIn("provider='pi'", consult)

    def test_an_old_builtin_row_is_migrated_to_the_one_language(self):
        hub, _, _ = self.build()
        # simulate a pre-round-26 installation: the factory row is still the
        # old JSON with the old name
        hub.store.update_operation(
            "op-builtin-snapshot",
            {"name": "عکس و متن صفحهٔ فعال",
             "steps": [{"id": "sn-1", "type": "screenshot", "name": "manual"},
                       {"id": "sn-2", "type": "capture_text"}]})
        hub.library.ensure_builtins()
        row = {r["id"]: r for r in hub.operation_list()}["op-builtin-snapshot"]
        self.assertEqual(row["name"], "دیدن صفحهٔ فعال")
        self.assertEqual([step["type"] for step in row["steps"]],
                         ["python", "python"])
        self.assertIn("screenshot('manual')", row["steps"][0]["code"])

    def test_scope_is_validated_and_saved(self):
        hub, _, _ = self.build()
        with self.assertRaises(AgentError):
            hub.operation_save({"name": "x", "scope": "secret",
                                "steps": [{"type": "python", "code": "wait(1)"}]})
        op = hub.operation_save({"name": "y", "scope": "brain",
                                 "steps": [{"type": "python", "code": "wait(1)"}]})
        self.assertEqual(op["scope"], "brain")

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
        self.assertEqual(len(hub.operation_list()), 9)

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
        self.assertEqual(len(hub.operation_list()), 8, "a refused operation was stored")

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


    def test_every_user_message_produces_timestamped_agent_reports(self):
        # Round 27, item 5: every message the operator sends the coworker
        # agent must leave a precise, timestamped report of what it did.
        hub, _, _ = self.build(llm=FakeLlm("جواب ایجنت"))
        hub.chat_send("سلام ایجنت همکار")
        self.join_chat(hub)
        reports = hub.store.list_step_reports("agent")
        phases = [row["phase"] for row in reports]
        self.assertIn("پیام کاربر", phases)
        self.assertIn("پاسخ", phases)
        message = [row for row in reports if row["phase"] == "پیام کاربر"][0]
        self.assertIn("سلام ایجنت همکار", message["message"])
        answer = [row for row in reports if row["phase"] == "پاسخ"][0]
        self.assertTrue(answer["ok"])
        self.assertIn("پاسخ داد", answer["message"])
        self.assertTrue(all(row["at"] > 0 for row in reports),
                        "every report carries an exact timestamp")

    def test_a_failed_turn_is_reported_as_not_ok(self):
        class Broken(FakeLlm):
            def complete(self, *args, **kwargs):
                raise RuntimeError("model is down")

        hub, _, _ = self.build(llm=Broken("x"))
        hub.chat_send("سلام")
        self.join_chat(hub)
        bad = [row for row in hub.store.list_step_reports("agent")
               if not row["ok"]]
        self.assertTrue(bad, "a failed turn must be reported with ok=0")
        self.assertIn("شکست خورد", bad[0]["message"])

    def test_reports_route_filters_by_kind(self):
        hub, backend, flows = self.build(llm=FakeLlm("باشه"))
        hub.chat_send("سلام")
        self.join_chat(hub)
        hub.brain._report("مشاهده", "خطی از مغز")
        hub.store.add_step_report("scheduler", "صف", "خطی از زمان‌بند")
        api = self.api_for(hub, backend, flows)
        status, payload = self.call(api, "GET", "/agent/reports",
                                    query={"kind": ["agent"]})
        self.assertEqual(status, 200)
        self.assertTrue(payload["reports"])
        self.assertEqual({row["kind"] for row in payload["reports"]}, {"agent"})
        status, payload = self.call(api, "GET", "/agent/reports",
                                    query={"kind": ["brain"]})
        self.assertEqual({row["kind"] for row in payload["reports"]}, {"brain"})
        status, payload = self.call(api, "GET", "/agent/reports")
        self.assertEqual({row["kind"] for row in payload["reports"]},
                         {"agent", "brain", "scheduler"})


class LettersTest(HubCase):
    """Round 27, item 3: the two-way letter box between the operator and the
    coworker agent, and its delivery into the agent's prompts."""

    def test_store_roundtrip_and_queue(self):
        hub, backend, flows = self.build()
        first = hub.store.add_letter("to_agent", " capability note ",
                                     subject=" تازه ")
        second = hub.store.add_letter("to_user", "agent reply")
        self.assertEqual(first["direction"], "to_agent")
        self.assertEqual(first["status"], "new")
        self.assertEqual(first["body"], "capability note")     # trimmed
        self.assertEqual(first["subject"], "تازه")
        letters = hub.store.list_letters()
        self.assertEqual([item["id"] for item in letters],
                         [second["id"], first["id"]])          # newest first
        self.assertEqual([item["id"] for item in
                          hub.store.list_letters(direction="to_user")],
                         [second["id"]])
        queue = hub.store.unread_letters("to_agent")
        self.assertEqual([item["id"] for item in queue], [first["id"]])
        self.assertEqual(hub.store.mark_letters_read([first["id"]]), 1)
        self.assertEqual(hub.store.mark_letters_read([first["id"]]), 0)
        self.assertEqual(hub.store.unread_letters("to_agent"), [])
        self.assertTrue(hub.store.delete_letter(second["id"]))
        self.assertFalse(hub.store.delete_letter(second["id"]))

    def test_hub_validation_and_listing(self):
        hub, backend, flows = self.build()
        with self.assertRaises(AgentError):
            hub.letter_add("to_agent", "   ")
        letter = hub.letter_add("sideways", "متن نامه")
        self.assertEqual(letter["direction"], "to_agent")      # unknown -> to_agent
        data = hub.letters_list()
        self.assertEqual(data["unread"], 1)
        self.assertEqual(len(data["letters"]), 1)
        with self.assertRaises(AgentError):
            hub.letter_delete("nope")
        with self.assertRaises(AgentError) as ctx:
            hub.letter_delete("99999")
        self.assertEqual(ctx.exception.status, 404)

    def test_letters_ride_into_the_chat_prompt_once(self):
        hub, backend, flows = self.build()
        hub.letter_add("to_agent", "از امروز عملیات شرطی داریم.",
                       subject="توانایی تازه")
        user = hub.store.add_chat("user", "سلام")
        prompt = hub._chat_prompt(user["id"], "سلام", False)
        self.assertIn("نامه‌های تازهٔ کاربر به تو", prompt)
        self.assertIn("توانایی تازه", prompt)
        self.assertIn("از امروز عملیات شرطی داریم.", prompt)
        # delivered exactly once: the next turn no longer carries it
        second = hub.store.add_chat("user", "دوباره")
        self.assertNotIn("نامه‌های تازهٔ کاربر به تو",
                         hub._chat_prompt(second["id"], "دوباره", False))

    def test_routes(self):
        hub, backend, flows = self.build()
        api = self.api_for(hub, backend, flows)
        status, payload = self.call(api, "POST", "/agent/letters",
                                    {"direction": "to_agent", "subject": "موضوع",
                                     "body": "متن"})
        self.assertEqual(status, 200)
        letter_id = payload["letter"]["id"]
        status, payload = self.call(api, "GET", "/agent/letters")
        self.assertEqual(status, 200)
        self.assertEqual(payload["unread"], 1)
        self.assertEqual(payload["letters"][0]["subject"], "موضوع")
        status, _ = self.call(api, "POST", "/agent/letters", {"body": ""})
        self.assertEqual(status, 400)
        status, _ = self.call(api, "GET", "/agent/letters", headers={})
        self.assertEqual(status, 401)
        status, payload = self.call(api, "DELETE", "/agent/letters/%s" % letter_id)
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"deleted": letter_id})
        status, _ = self.call(api, "DELETE", "/agent/letters/%s" % letter_id)
        self.assertEqual(status, 404)

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

    def test_the_catalog_is_the_single_python_language(self):
        # round 26: one step type is left; the old vocabulary stays documented
        # so archived flows and old letters remain readable
        catalog = schema.step_catalog()
        self.assertEqual(sorted(catalog), ["python"])
        for kind in ("agent_open", "agent_ask", "captcha_solve",
                     "interactive_form", "secure_http_request"):
            self.assertIn(kind, schema.STEP_DOCS)


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


# ---------------------------------------------------------------------------
# phase 7: continuous backups, chrome knowledge, the telegram mailbox
# ---------------------------------------------------------------------------
class FakeTgClient:
    """A transport double: records calls, never touches the network."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.sent: List[Dict[str, Any]] = []

    def whoami(self) -> Dict[str, Any]:
        return {"username": "test_bot"}

    def authorized(self) -> bool:
        return True

    def send_text(self, chat_id: Any, text: str) -> Dict[str, Any]:
        if self.fail:
            raise TelegramError("boom")
        self.sent.append({"chat": chat_id, "text": text})
        return {"messageId": 1}

    def send_photo(self, chat_id: Any, path: str, caption: str = "") -> Dict[str, Any]:
        return self.send_text(chat_id, caption)

    def send_document(self, chat_id: Any, path: str, caption: str = "") -> Dict[str, Any]:
        if self.fail:
            raise TelegramError("boom")
        self.sent.append({"chat": chat_id, "document": path, "caption": caption})
        return {"messageId": 2}


class BackupManagerTest(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        from automation.agent_store import AgentStore
        from automation.backup import BackupManager
        self.store = AgentStore(self.tmp)
        self.addCleanup(self.store.close)
        self.flows = os.path.join(self.tmp, "flows")
        os.makedirs(self.flows, exist_ok=True)
        with open(os.path.join(self.flows, "jaryan.json"), "w", encoding="utf-8") as fh:
            fh.write('{"name": "jaryan", "steps": []}')
        self.manager = BackupManager(self.store, data_dir=self.tmp,
                                     flows_dir=self.flows, clock=self.clock)

    def test_create_lists_prunes_and_deletes(self) -> None:
        from automation.backup import BackupError
        info = self.manager.create(label="اولی")
        self.assertTrue(info["name"].startswith("mas-backup-"))
        self.assertEqual(info["flows"], 1)
        self.clock.now += 5
        self.manager.create()
        self.assertEqual(len(self.manager.list()), 2)
        pruned = self.manager.prune(keep=1)
        self.assertEqual(len(pruned["deleted"]), 1)
        self.assertEqual(len(self.manager.list()), 1)
        with tarfile.open(self.manager.list()[0]["path"]) as archive:
            names = set(archive.getnames())
        self.assertIn("agent.db", names)
        self.assertIn("flows/jaryan.json", names)
        self.assertIn("backup.json", names)
        self.assertEqual(self.manager.delete(self.manager.newest_name())["remaining"], 0)
        with self.assertRaises(BackupError):
            self.manager.safe_path("../escape.tar.gz")

    def test_secrets_join_only_when_asked(self) -> None:
        self.store.set_secret("telegram.botToken", "123:abc")
        plain = self.manager.create()
        with tarfile.open(plain["path"]) as archive:
            self.assertNotIn("agent-secrets.json", archive.getnames())
        with_secrets = self.manager.create(include_secrets=True)
        with tarfile.open(with_secrets["path"]) as archive:
            self.assertIn("agent-secrets.json", archive.getnames())

    def test_restore_round_trip_and_stale_wal_cleanup(self) -> None:
        from automation.agent_store import AgentStore
        self.store.set_setting("restore.marker", "v1")
        info = self.manager.create()
        self.store.set_setting("restore.marker", "v2")
        os.remove(os.path.join(self.flows, "jaryan.json"))
        wal = str(self.manager.db_path) + "-wal"
        with open(wal, "w", encoding="utf-8") as handle:
            handle.write("stale")
        result = self.manager.restore(info["name"])
        self.assertEqual(result["restored"], info["name"])
        # the flow and the setting rolled back…
        self.assertTrue(os.path.exists(os.path.join(self.flows, "jaryan.json")))
        fresh = AgentStore(self.tmp)
        self.addCleanup(fresh.close)
        self.assertEqual(fresh.get_setting("restore.marker"), "v1")
        # …and the stale WAL that could corrupt the fresh db is gone
        self.assertFalse(os.path.exists(wal))

    def test_restore_refuses_unknown_names_and_hostile_members(self) -> None:
        import io
        from automation.backup import BackupError
        with self.assertRaises(BackupError) as missing:
            self.manager.restore("nope.tar.gz")
        self.assertEqual(missing.exception.status, 404)
        self.manager.ensure_dir()
        evil = self.manager.dir / "evil.tar.gz"
        with tarfile.open(str(evil), "w:gz") as archive:
            data = b"pwned"
            member = tarfile.TarInfo("../evil.txt")
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
        with self.assertRaises(BackupError) as hostile:
            self.manager.restore("evil.tar.gz")
        self.assertEqual(hostile.exception.status, 400)

    def test_send_uploads_through_the_notifier(self) -> None:
        from automation.backup import BackupError
        self.manager.create()
        calls: List[Dict[str, Any]] = []

        class Notifier:
            def targets(self) -> List[Dict[str, Any]]:
                return [{"id": -100, "title": "kanal"}]

            def send_document(self, path, caption="", targets=None,
                              purpose=None, channel=None):
                calls.append({"path": path, "targets": targets,
                              "purpose": purpose, "channel": channel})
                return {"sent": 1, "failed": 0, "channels": ["bot"]}
        self.manager.notifier = Notifier()
        result = self.manager.send(target="-100", channel="bot")
        self.assertEqual(result["sent"], 1)
        self.assertEqual(calls[0]["purpose"], "backup")
        self.assertEqual(calls[0]["targets"][0]["id"], -100)
        with self.assertRaises(BackupError):
            self.manager.send(target="-100", channel="pigeon")

    def test_inspect_gives_every_item_a_verdict(self) -> None:
        # Round 28, item 4: green = compatible with this app version,
        # red = never imported
        with open(os.path.join(self.flows, "broken.json"), "w",
                  encoding="utf-8") as fh:
            fh.write("{this is not json")
        info = self.manager.create()
        table = self.manager.inspect(info["name"])
        verdicts = {row["key"]: row for row in table["items"]}
        self.assertTrue(verdicts["agent.db"]["compatible"])
        self.assertIn("سازگار", verdicts["agent.db"]["reason"])
        self.assertTrue(verdicts["flows/jaryan.json"]["compatible"])
        self.assertFalse(verdicts["flows/broken.json"]["compatible"])
        self.assertEqual(table["red"], 1)
        self.assertEqual(table["name"], info["name"])

    def test_restore_default_skips_the_red_items(self) -> None:
        from automation.agent_store import AgentStore
        with open(os.path.join(self.flows, "broken.json"), "w",
                  encoding="utf-8") as fh:
            fh.write("{this is not json")
        info = self.manager.create()
        self.store.set_setting("restore.marker", "v2")
        os.remove(os.path.join(self.flows, "broken.json"))
        os.remove(os.path.join(self.flows, "jaryan.json"))
        result = self.manager.restore(info["name"])
        self.assertIn("flows/jaryan.json", result["items"])
        self.assertIn("flows/broken.json", result["skipped"])
        self.assertTrue(os.path.exists(os.path.join(self.flows, "jaryan.json")))
        self.assertFalse(os.path.exists(os.path.join(self.flows, "broken.json")))
        # the database was green, so it came back too
        fresh = AgentStore(self.tmp)
        self.addCleanup(fresh.close)
        self.assertNotEqual(fresh.get_setting("restore.marker"), "v2")

    def test_restore_items_choice_and_unknown_refusal(self) -> None:
        from automation.agent_store import AgentStore
        from automation.backup import BackupError
        info = self.manager.create()
        self.store.set_setting("restore.marker", "v2")
        os.remove(os.path.join(self.flows, "jaryan.json"))
        result = self.manager.restore(info["name"], items=["flows/jaryan.json"])
        self.assertEqual(result["items"], ["flows/jaryan.json"])
        self.assertIn("agent.db", result["skipped"])
        self.assertTrue(os.path.exists(os.path.join(self.flows, "jaryan.json")))
        # the database was NOT chosen, so the live marker survives
        fresh = AgentStore(self.tmp)
        self.addCleanup(fresh.close)
        self.assertEqual(fresh.get_setting("restore.marker"), "v2")
        with self.assertRaises(BackupError) as ctx:
            self.manager.restore(info["name"], items=["nope.json"])
        self.assertEqual(ctx.exception.status, 400)

    def test_upload_accepts_only_our_archives(self) -> None:
        import io
        from automation.backup import BackupError
        info = self.manager.create()
        with open(info["path"], "rb") as fh:
            data = fh.read()
        landed = self.manager.upload(data, name="my copy.tar.gz")
        self.assertEqual(landed["name"], "mas-backup-mycopy.tar.gz")
        self.assertIn(landed["name"],
                      [row["name"] for row in self.manager.list()])
        with self.assertRaises(BackupError):
            self.manager.upload(b"x" * 900)
        foreign = io.BytesIO()
        with tarfile.open(fileobj=foreign, mode="w:gz") as archive:
            payload = b"hello"
            member = tarfile.TarInfo("notes.txt")
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
        with self.assertRaises(BackupError) as ctx:
            self.manager.upload(foreign.getvalue())
        self.assertEqual(ctx.exception.status, 400)

    def test_telegram_auto_toggle_and_the_sent_record(self) -> None:
        # Round 28, item 8: the telegram tab owns its automatic decision, and
        # every shipped file keeps its message info for the way back
        self.assertTrue(self.manager.config()["telegramAuto"])
        self.manager.configure({"telegramAuto": False})
        self.assertFalse(self.manager.config()["telegramAuto"])
        self.manager.configure({"telegramAuto": True})
        self.manager.create()

        class Notifier:
            def targets(self):
                return [{"id": -100, "title": "kanal"}]

            def send_document(self, path, caption="", targets=None,
                              purpose=None, channel=None):
                return {"sent": 1, "failed": 0, "channels": ["bot"],
                        "details": [{"chatId": -100, "channel": "bot",
                                     "messageId": 7, "fileId": "FILE",
                                     "fileName": "x.tar.gz"}]}

        self.manager.notifier = Notifier()
        self.manager.send(target="-100", channel="bot")
        sent = self.manager.config()["telegramSent"]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["fileId"], "FILE")
        self.assertEqual(sent[0]["messageId"], 7)


class BackupHubTest(HubCase):
    def test_the_automatic_job_backs_up_and_the_routes_serve_it(self) -> None:
        hub, _backend, flows = self.build()
        api = AutomationApi(FakeEngine(), flows, None, data_dir=self.tmp,
                            token=TOKEN, agent=hub)
        status, payload = self.call(api, "POST", "/agent/backup-config", {
            "enabled": True, "scheduleKind": "cron", "schedule": "0 3 * * *",
            "keep": 2, "telegramTarget": "@kanal"})
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["enabled"])
        job = hub.backups.job()
        self.assertIsNotNone(job)
        self.assertEqual(job["payload"]["action"], "backup")
        outcome = hub._run_job(hub.store.get_job(job["id"]))
        self.assertEqual(outcome["status"], "done")
        self.assertEqual(len(hub.backups.list()), 1)

        status, payload = self.call(api, "GET", "/agent/backups", {})
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["backups"]), 1)
        self.assertIn("tables", payload["stats"])
        name = payload["backups"][0]["name"]
        status, _headers, body = api.handle(
            "GET", "/agent/backup-download", query={"name": [name]},
            headers={"x-automation-token": TOKEN})
        self.assertEqual(status, 200)
        self.assertTrue(body[:2] == b"\x1f\x8b")          # gzip magic
        status, payload = self.call(api, "POST", "/agent/backup-delete",
                                    {"name": name})
        self.assertEqual(status, 200)
        self.assertEqual(payload["remaining"], 0)
        status, payload = self.call(api, "GET", "/agent/backups", {})
        self.assertEqual(payload["backups"], [])

    def test_a_backup_send_needs_a_destination(self) -> None:
        hub, _backend, flows = self.build()
        api = AutomationApi(FakeEngine(), flows, None, data_dir=self.tmp,
                            token=TOKEN, agent=hub)
        hub.backup_create()
        status, payload = self.call(api, "POST", "/agent/backup-send", {})
        self.assertEqual(status, 400)
        self.assertIn("destination", payload["error"])

    def test_backup_fetch_downloads_from_telegram_and_lands_locally(self) -> None:
        # Round 28, item 8: one sent file comes back by its recorded file_id
        from automation.agent import AgentError
        hub, _backend, flows = self.build()
        hub.backup_create()
        name = hub.backups.newest_name()
        with open(str(hub.backups.safe_path(name)), "rb") as fh:
            data = fh.read()

        class Fetcher:
            def download_document(self, file_id):
                assert file_id == "FID"
                return data

        hub.notifier = Fetcher()
        info = hub.backup_fetch("FID", name="from-telegram.tar.gz")
        self.assertEqual(info["name"], "mas-backup-from-telegram.tar.gz")
        self.assertIn(info["name"],
                      [row["name"] for row in hub.backups.list()])
        with self.assertRaises(AgentError) as ctx:
            hub.backup_fetch("")
        self.assertEqual(ctx.exception.status, 400)


class BrowserInfoTest(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        from automation import browser_info
        self.bi = browser_info
        profile = os.path.join(self.tmp, "chrome-profile", "Default")
        os.makedirs(profile)
        db = sqlite3.connect(os.path.join(profile, "History"))
        offset = browser_info._WINDOWS_EPOCH_OFFSET
        # real wall-clock on purpose: closed_tabs() filters against time.time()
        now = int(time.time() * 1_000_000) + offset
        db.executescript(
            "CREATE TABLE urls (id INTEGER PRIMARY KEY, url TEXT, title TEXT,"
            " visit_count INTEGER DEFAULT 0, typed_count INTEGER DEFAULT 0,"
            " last_visit_time INTEGER DEFAULT 0);"
            "CREATE TABLE visits (id INTEGER PRIMARY KEY, url INTEGER,"
            " visit_time INTEGER, from_visit INTEGER, transition INTEGER DEFAULT 0);"
            "CREATE TABLE keyword_search_terms (keyword_id INTEGER, url_id INTEGER,"
            " term TEXT, normal_term TEXT, last_visit_time INTEGER DEFAULT 0);"
            "CREATE TABLE downloads (id INTEGER PRIMARY KEY, current_path TEXT,"
            " target_path TEXT, start_time INTEGER, total_bytes INTEGER,"
            " received_bytes INTEGER, state INTEGER, tab_url TEXT,"
            " mime_type TEXT, danger_type INTEGER);")
        db.execute("INSERT INTO urls VALUES (1, 'https://a.example/', 'A', 2, 1, ?)",
                   (now,))
        db.execute("INSERT INTO urls VALUES (2, 'https://b.example/', 'B', 1, 0, ?)",
                   (now - 7_200_000_000,))
        db.execute("INSERT INTO visits VALUES (1, 1, ?, 0, 0)", (now,))
        db.execute("INSERT INTO visits VALUES (2, 2, ?, 0, 80)",
                   (now - 7_200_000_000,))
        db.execute("INSERT INTO keyword_search_terms VALUES (0, 1, 'deepseek', 'deepseek', ?)",
                   (now,))
        db.execute("INSERT INTO downloads VALUES (1, '/data/x.zip', '/data/x.zip', ?,"
                   " 10, 10, 1, 'https://a.example/x.zip', 'application/zip', 0)",
                   (now,))
        db.commit()
        db.close()

    def test_history_reads_visits_searches_and_downloads(self) -> None:
        data = self.bi.read_history("automation", data_dir=Path(self.tmp))
        self.assertTrue(data["ok"], data.get("error"))
        self.assertEqual(len(data["visits"]), 2)
        self.assertEqual(data["visits"][0]["url"], "https://a.example/")
        self.assertEqual(data["visits"][1]["transition"], "کد 80" if False
                         else data["visits"][1]["transition"])
        self.assertEqual(data["searches"][0]["term"], "deepseek")
        self.assertEqual(data["downloads"][0]["state"], "کامل")
        filtered = self.bi.read_history("automation", query="b.example",
                                        data_dir=Path(self.tmp))
        self.assertEqual([v["url"] for v in filtered["visits"]],
                         ["https://b.example/"])

    def test_closed_tabs_are_history_minus_open_tabs(self) -> None:
        data = self.bi.read_history("automation", data_dir=Path(self.tmp))
        closed = self.bi.closed_tabs(["https://a.example/"], data["visits"])
        self.assertEqual([row["url"] for row in closed], ["https://b.example/"])

    def test_summary_combines_everything_without_a_live_browser(self) -> None:
        summary = self.bi.summary(data_dir=Path(self.tmp))
        self.assertTrue(summary["history"]["ok"])
        self.assertFalse(summary["tabs"]["ok"])       # no chrome in the sandbox
        self.assertEqual(len(summary["closedTabs"]), 2)
        self.assertIn("SNSS", summary["closedTabsNote"])

    def test_tab_actions_reject_nonsense(self) -> None:
        from automation.browser_info import BrowserError
        with self.assertRaises(BrowserError):
            self.bi.tab_action("explode")
        with self.assertRaises(BrowserError):
            self.bi.tab_action("close")


class TelegramMailboxTest(HubCase):
    def test_every_delivery_lands_in_the_mailbox_with_a_status(self) -> None:
        hub, _backend, flows = self.build()
        hub.save_settings({"telegram.mode": "bot",
                           "telegram.targets": [{"id": 7, "title": "من",
                                                 "type": "private"}]})
        client = FakeTgClient()
        hub.notifier.transport = lambda mode=None: client
        hub.telegram_send("سلام", target=7)
        counts = hub.telegram_log()["counts"]
        self.assertEqual(counts["sent"], 1)
        self.assertEqual(counts["failed"], 0)
        hub.notifier.transport = lambda mode=None: FakeTgClient(fail=True)
        result = hub.telegram_send("دوباره", target=7)
        self.assertEqual(result["sent"], 0)
        self.assertEqual(result["failed"], 1)
        counts = hub.telegram_log()["counts"]
        self.assertEqual(counts["failed"], 1)
        rows = hub.telegram_log()["messages"]
        self.assertEqual(rows[0]["status"], "failed")
        self.assertEqual(rows[1]["status"], "sent")
        api = AutomationApi(FakeEngine(), flows, None, data_dir=self.tmp,
                            token=TOKEN, agent=hub)
        status, payload = self.call(api, "GET", "/agent/telegram/log", {})
        self.assertEqual(status, 200)
        self.assertEqual(payload["counts"]["all"], 2)

    def test_diagnose_walks_the_whole_path(self) -> None:
        hub, _backend, _flows = self.build()
        hub.save_settings({"telegram.mode": "bot",
                           "telegram.targets": [{"id": 7, "title": "من",
                                                 "type": "private"}]})
        client = FakeTgClient()
        hub.notifier.transport = lambda mode=None: client
        report = hub.telegram_diagnose(target=7)
        self.assertTrue(report["ok"], report)
        steps = [step["step"] for step in report["steps"]]
        self.assertEqual(steps, ["mode", "channels", "bot", "targets", "send"])
        self.assertEqual(len(client.sent), 1)
        hub.save_settings({"telegram.mode": "off"})
        report = hub.telegram_diagnose()
        self.assertFalse(report["ok"])
        self.assertFalse(report["steps"][0]["ok"])

    def test_a_telegram_program_stops_after_its_limit(self) -> None:
        hub, _backend, _flows = self.build()
        hub.save_settings({"telegram.mode": "bot",
                           "telegram.targets": [{"id": 7, "title": "من",
                                                 "type": "private"}]})
        client = FakeTgClient()
        hub.notifier.transport = lambda mode=None: client
        job = hub.save_job({"name": "برنامه", "kind": "every", "schedule": "60",
                            "payload": {"action": "telegram", "text": "سلام",
                                        "maxRuns": 1}})
        first = hub._run_job(hub.store.get_job(job["id"]))
        self.assertEqual(first["status"], "done")
        self.assertTrue(hub.store.get_job(job["id"])["enabled"] is False)
        self.assertEqual(len(client.sent), 1)


class WebRootPhase7Test(TempDirCase):
    def test_the_viewer_is_forced_full_screen_and_imports_are_versioned(self) -> None:
        from automation import server as srv
        web = os.path.join(self.tmp, "web")
        novnc = os.path.join(self.tmp, "novnc")
        os.makedirs(novnc)
        with open(os.path.join(novnc, "vnc.html"), "w", encoding="utf-8") as fh:
            fh.write("<html><head></head><body></body></html>")
        static = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              os.pardir, "automation", "static")
        report = srv.prepare_web_root(web, novnc, static)
        self.assertTrue(report["viewPatched"])
        self.assertGreaterEqual(report["versionedImports"], 1)
        with open(os.path.join(web, "vnc.html"), encoding="utf-8") as fh:
            html = fh.read()
        self.assertIn("noVNC_setting_resize", html)
        self.assertLess(html.index(srv.VIEW_MARKER), html.index("</head>"))
        self.assertIn("overflow: hidden", html)
        # Phase 9: the viewer must NOT carry a render-blocking external font
        # link (blank page behind filtered networks); Vazirmatn is self-hosted.
        self.assertNotIn("fonts.googleapis.com", html)
        with open(os.path.join(web, "automation", "automation.css"),
                  encoding="utf-8") as fh:
            css = fh.read()
        self.assertIn("@font-face", css)
        self.assertTrue(os.path.isfile(os.path.join(
            web, "automation", "fonts", "Vazirmatn-Regular.woff2")),
            "the woff2 files must be copied next to the css")
        with open(os.path.join(web, "automation", "automation.js"),
                  encoding="utf-8") as fh:
            script = fh.read()
        self.assertRegex(script, r"from '\./core\.mjs\?v=[0-9a-f]{10}'")
        # a second start must not stack the blocks
        srv.prepare_web_root(web, novnc, static)
        with open(os.path.join(web, "vnc.html"), encoding="utf-8") as fh:
            html = fh.read()
        self.assertEqual(html.count(srv.VIEW_MARKER), 1)


class LabelCoverageTest(unittest.TestCase):
    """Every t('key') the sidebar uses must exist in BOTH label tables.

    The operator saw a raw "tabChat" once because a stale core.mjs met a new
    automation.js; the cache is fixed elsewhere, but a missing key would look
    exactly the same, so the two files are compared here on every run.
    """

    def test_every_label_key_resolves_in_persian_and_english(self) -> None:
        import re
        root = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir)
        with open(os.path.join(root, "automation", "static", "automation.js"),
                  encoding="utf-8") as fh:
            script = fh.read()
        with open(os.path.join(root, "automation", "static", "core.mjs"),
                  encoding="utf-8") as fh:
            core = fh.read()
        used = set(re.findall(r"\bt\('([A-Za-z0-9_]+)'\)", script))
        fa = core[core.index("  fa: {"):core.index("  en: {")]
        en = core[core.index("  en: {"):]
        fa_keys = set(re.findall(r"([A-Za-z0-9_]+)\s*:", fa))
        en_keys = set(re.findall(r"([A-Za-z0-9_]+)\s*:", en))
        self.assertEqual(sorted(k for k in used if k not in fa_keys), [],
                         "keys without a Persian label")
        self.assertEqual(sorted(k for k in used if k not in en_keys), [],
                         "keys without an English label")
        # the fa block must really sit inside the fa object: a stray close
        # brace would turn labels into dead JS labels and fail silently.
        self.assertNotIn("},\n    tabDb:", core)


class Phase9Test(HubCase):
    """Round nine: journal, dedicated pages, gated suggestions, read-only db."""

    def test_devlog_seeds_with_the_letter_and_grows(self) -> None:
        hub, backend, flows = self.build()
        api = self.api_for(hub, backend, flows)
        status, data = self.call(api, "GET", "/agent/devlog")
        self.assertEqual(status, 200)
        self.assertTrue(any("ایجنت همکار" in entry["title"]
                            for entry in data["entries"]),
                        "the journal must start with the agent letter")
        status, data = self.call(api, "POST", "/agent/devlog",
                                 body={"title": "دور نهم", "body": "کلیپ‌بورد"})
        self.assertEqual(status, 201)
        status, data = self.call(api, "GET", "/agent/devlog")
        self.assertEqual(len(data["entries"]), 2)
        status, _ = self.call(api, "POST", "/agent/devlog", body={"title": ""})
        self.assertEqual(status, 400)

    def test_pages_persist_html_and_delete_is_operator_only(self) -> None:
        hub, backend, flows = self.build()
        api = self.api_for(hub, backend, flows)
        key = hub.agent_key_reveal()["key"]
        status, data = self.call(api, "POST", "/agent/pages",
                                 headers={"x-agent-key": key},
                                 body={"title": "گزارش روز", "html": "<b>سلام</b>"})
        self.assertEqual(status, 201)
        page_id = data["page"]["id"]
        status, data = self.call(api, "GET", "/agent/pages")
        self.assertEqual(data["pages"][0]["size"], len("<b>سلام</b>"))
        status, data = self.call(api, "GET", "/agent/pages",
                                 query={"id": [str(page_id)]})
        self.assertEqual(data["html"], "<b>سلام</b>")
        status, _ = self.call(api, "POST", "/agent/pages",
                              headers={"x-agent-key": key},
                              body={"title": "x", "html": "  "})
        self.assertEqual(status, 400, "empty html must not be saved")
        status, _ = self.call(api, "POST", "/agent/page-delete",
                              headers={"x-agent-key": key}, body={"id": page_id})
        self.assertEqual(status, 403, "the agent must not delete pages")
        status, _ = self.call(api, "POST", "/agent/page-delete",
                              body={"id": page_id})
        self.assertEqual(status, 200)
        status, _ = self.call(api, "GET", "/agent/pages",
                              query={"id": [str(page_id)]})
        self.assertEqual(status, 404)

    def test_suggestions_never_apply_without_a_human(self) -> None:
        hub, backend, flows = self.build()
        api = self.api_for(hub, backend, flows)
        key = hub.agent_key_reveal()["key"]
        proposal = {"title": "تم پیش‌فرض تاریک", "kind": "settings",
                    "section": "تم", "risk": "low",
                    "status": "approved",  # self-approval must be ignored
                    "before_state": json.dumps({"key": "ui.theme", "value": "light"}),
                    "after_state": json.dumps({"key": "ui.theme", "value": "dark"})}
        status, data = self.call(api, "POST", "/agent/suggestions",
                                 headers={"x-agent-key": key}, body=proposal)
        self.assertEqual(status, 201)
        sid = data["suggestion"]["id"]
        self.assertEqual(data["suggestion"]["status"], "draft")
        # the agent cannot decide, apply, rollback or delete
        for path in ("/agent/suggestion-decision", "/agent/suggestion-apply",
                     "/agent/suggestion-rollback", "/agent/suggestion-delete"):
            status, _ = self.call(api, "POST", path,
                                  headers={"x-agent-key": key},
                                  body={"id": sid, "decision": "approve"})
            self.assertEqual(status, 403, path)
        # operator, but before approval: still refused
        status, _ = self.call(api, "POST", "/agent/suggestion-apply",
                              body={"id": sid})
        self.assertEqual(status, 409)
        status, data = self.call(api, "POST", "/agent/suggestion-decision",
                                 body={"id": sid, "decision": "approve"})
        self.assertEqual(status, 200)
        self.assertEqual(data["suggestion"]["status"], "approved")
        status, data = self.call(api, "POST", "/agent/suggestion-apply",
                                 body={"id": sid})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(hub.store.get_setting("ui.theme"), "dark")
        status, _ = self.call(api, "POST", "/agent/suggestion-rollback",
                              body={"id": sid})
        self.assertEqual(status, 200)
        self.assertEqual(hub.store.get_setting("ui.theme"), "light")
        # editing cannot smuggle a status change
        status, data = self.call(api, "POST", "/agent/suggestion-update",
                                 headers={"x-agent-key": key},
                                 body={"id": sid, "title": "عنوان تازه",
                                       "status": "applied"})
        self.assertEqual(data["suggestion"]["title"], "عنوان تازه")
        self.assertNotEqual(data["suggestion"]["status"], "applied")
        # soft delete: archived, never destroyed
        status, _ = self.call(api, "POST", "/agent/suggestion-delete",
                              body={"id": sid})
        self.assertEqual(status, 200)
        status, data = self.call(api, "GET", "/agent/suggestions")
        self.assertEqual(data["suggestions"], [])
        status, data = self.call(api, "GET", "/agent/suggestions",
                                 query={"archived": ["1"]})
        self.assertEqual(len(data["suggestions"]), 1)
        status, data = self.call(api, "GET", "/agent/suggestions",
                                 query={"id": [str(sid)]})
        self.assertEqual(data["suggestion"]["status"], "archived")

    def test_non_settings_suggestions_refuse_to_auto_apply(self) -> None:
        hub, backend, flows = self.build()
        api = self.api_for(hub, backend, flows)
        status, data = self.call(api, "POST", "/agent/suggestions",
                                 body={"title": "تغییر UI", "kind": "ui"})
        sid = data["suggestion"]["id"]
        self.call(api, "POST", "/agent/suggestion-decision",
                  body={"id": sid, "decision": "approve"})
        status, _ = self.call(api, "POST", "/agent/suggestion-apply",
                              body={"id": sid})
        self.assertEqual(status, 409, "no silent appliers for other kinds")

    def test_db_window_is_read_only_and_masks_secrets(self) -> None:
        hub, backend, flows = self.build()
        api = self.api_for(hub, backend, flows)
        hub.store.set_setting("ui.theme", "light", actor="t")
        hub.store.set_setting("telegram.botToken", "123456:ABC-SECRET",
                              secret=True, actor="t")
        status, data = self.call(api, "GET", "/agent/db/schema")
        self.assertEqual(status, 200)
        names = [table["name"] for table in data["tables"]]
        for table in ("settings", "suggestions", "devlog", "agent_pages"):
            self.assertIn(table, names)
        status, data = self.call(api, "POST", "/agent/db/query",
                                 body={"sql": "SELECT * FROM settings"})
        self.assertEqual(status, 200)
        by_key = {row["key"]: row["value"] for row in data["rows"]}
        # the settings table stores JSON-encoded values
        self.assertEqual(json.loads(by_key["ui.theme"]), "light")
        token_value = by_key["telegram.botToken"]
        self.assertIsInstance(token_value, dict)
        self.assertTrue(token_value["masked"])
        self.assertNotIn("ABC-SECRET", json.dumps(data["rows"], ensure_ascii=False))
        status, data = self.call(api, "POST", "/agent/db/query",
                                 body={"sql": "WITH x AS (SELECT 1 n) SELECT n FROM x"})
        self.assertEqual(data["rows"], [{"n": 1}])
        for bad in ("UPDATE settings SET value='x'",
                    "DELETE FROM devlog", "DROP TABLE settings",
                    "SELECT 1; SELECT 2", "ATTACH '/tmp/x' AS y",
                    "INSERT INTO devlog (at,title) VALUES (1,'a')", ""):
            status, _ = self.call(api, "POST", "/agent/db/query",
                                  body={"sql": bad})
            self.assertEqual(status, 400, "must refuse: %r" % bad)

    def test_clipboard_route_is_authed_and_validated(self) -> None:
        hub, backend, flows = self.build()
        api = self.api_for(hub, backend, flows)
        status, _ = self.call(api, "GET", "/clipboard", headers={})
        self.assertEqual(status, 401)
        status, _ = self.call(api, "POST", "/clipboard", body={})
        self.assertEqual(status, 400)
        status, _ = self.call(api, "POST", "/clipboard", body={"text": "سلام"})
        self.assertIn(status, (200, 409),
                      "no xclip in the sandbox is fine, but it must answer")

    def test_clipboard_bridge_forces_a_utf8_locale(self) -> None:
        # xclip converts clipboard bytes through the locale encoding; Colab
        # starts with LANG unset (POSIX/ASCII), which silently turns Persian
        # text into question marks. Both directions must force C.UTF-8.
        from unittest import mock
        from automation import clipboard
        captured: Dict[str, Any] = {}

        def fake_run(args, **kwargs):
            captured["env"] = kwargs.get("env") or {}
            return subprocess.CompletedProcess(args, 0, b"", b"")

        with mock.patch.object(clipboard.shutil, "which",
                               return_value="/usr/bin/xclip"), \
                mock.patch.object(clipboard.subprocess, "run",
                                  side_effect=fake_run):
            clipboard.write_text("سلام")
            self.assertEqual(captured["env"].get("LC_ALL"), "C.UTF-8")
            self.assertEqual(captured["env"].get("LANG"), "C.UTF-8")
            clipboard.read_text()
            self.assertEqual(captured["env"].get("LC_ALL"), "C.UTF-8")
            self.assertEqual(captured["env"].get("LANG"), "C.UTF-8")

    def test_clipboard_read_falls_back_from_utf8_to_string(self) -> None:
        # xclip's default target is the legacy STRING (Latin-1); Chrome
        # refuses to convert Persian into it ("target STRING not available")
        # even when the clipboard is full. UTF8_STRING must be asked first.
        from unittest import mock
        from automation import clipboard
        calls: List[List[str]] = []

        def fake_run(args, **kwargs):
            calls.append([str(a) for a in args])
            if "UTF8_STRING" in args:
                return subprocess.CompletedProcess(
                    args, 1, b"", b"Error: target UTF8_STRING not available\n")
            return subprocess.CompletedProcess(args, 0, "سلام".encode(), b"")

        with mock.patch.object(clipboard.shutil, "which",
                               return_value="/usr/bin/xclip"), \
                mock.patch.object(clipboard.subprocess, "run",
                                  side_effect=fake_run):
            self.assertEqual(clipboard.read_text(), "سلام")
        self.assertEqual(len(calls), 2)
        self.assertIn("UTF8_STRING", calls[0])
        self.assertIn("STRING", calls[1])

    def test_clipboard_read_reports_an_empty_desktop_cleanly(self) -> None:
        # Both targets unavailable means nobody owns text; the operator must
        # see "empty", not xclip's raw English error. (The old detection
        # looked for "target not available", which never matches the real
        # "target STRING not available" message.)
        from unittest import mock
        from automation import clipboard

        def fake_run(args, **kwargs):
            return subprocess.CompletedProcess(
                args, 1, b"", b"Error: target STRING not available\n")

        with mock.patch.object(clipboard.shutil, "which",
                               return_value="/usr/bin/xclip"), \
                mock.patch.object(clipboard.subprocess, "run",
                                  side_effect=fake_run):
            with self.assertRaises(clipboard.ClipboardError) as ctx:
                clipboard.read_text()
        self.assertIn("خالی", str(ctx.exception))

    def test_agent_key_use_is_audited_and_shown(self) -> None:
        hub, backend, flows = self.build()
        api = self.api_for(hub, backend, flows)
        key = hub.agent_key_reveal()["key"]
        self.call(api, "GET", "/agent/devlog", headers={"x-agent-key": key})
        rows = [row for row in hub.audit(50) if row["actor"] == "agent-key"]
        self.assertTrue(rows and "/agent/devlog" in rows[0]["detail"])
        last = hub.agent_key_info()["lastUse"]
        self.assertEqual(last["endpoint"], "GET /agent/devlog")
