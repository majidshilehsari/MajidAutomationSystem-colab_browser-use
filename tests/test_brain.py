"""The awake brain, phase 1: the supervisor that never lets the desk idle.

The brain is duty-bound, not clever: it runs the always-allowed operations
round-robin, proposes the ask-first ones with a deadline, enforces the
per-step logging policy on every engine start and prunes old screenshots
while the text reports stay forever. Everything here runs against a real
store and fake engines, so the tests exercise the decisions, not threads.
"""

import json
import os
import sqlite3
import tempfile
import time
import unittest

from automation.agent import PLAIN_SETTINGS, AgentError, AgentHub
from automation.api import AutomationApi, FlowStore
from automation.archive import shot_archive
from automation.brain import DEFAULTS, Brain
from automation.engine import AutomationEngine

TOKEN = "t"


class FakeEngine:
    """Records what the brain started; busy only when the test says so."""

    def __init__(self):
        self.started = []
        self.busy_flag = False
        self.history = []
        self.stop_calls = 0

    def busy(self):
        return self.busy_flag

    def status(self):
        return {"runId": "run-last", "status": "done", "history": self.history}

    def start(self, flow, run_id=None):
        self.started.append(flow)
        return {"runId": "run-new", "status": "running"}

    def stop(self):
        self.stop_calls += 1
        self.busy_flag = False


class QuietBackend:
    """Just enough backend for a real engine run of a single wait step."""

    def __init__(self):
        self.cursor_fx = None

    def control(self, *args, **kwargs):
        return (0, "", "")

    def screenshot(self, *args, **kwargs):
        return (1, "no display here", "")


class BrainCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mas-brain-")
        self.now = [1760000000.0]
        self.engine = FakeEngine()
        self.shots = shot_archive(self.tmp)
        self.hub = AgentHub(self.tmp, engine=self.engine,
                            flows=FlowStore(self.tmp), token=TOKEN,
                            clock=lambda: self.now[0], sleep=lambda seconds: None,
                            shots=self.shots)
        self.brain = self.hub.brain
        self.told = []
        # The brain talks to Telegram through the hub; capture instead of send.
        self.hub.telegram_send = lambda text, **kwargs: self.told.append(text)
        self.addCleanup(self.hub.store.close)

    # -- helpers ---------------------------------------------------------
    def enable(self):
        self.hub.brain_toggle(True, actor="tester")

    def ops_by_id(self):
        # library.list(), not operation_list(): the hub reseeds deleted
        # built-ins on every operation_list call, which would undo the
        # deletions these tests rely on.
        return {op["id"]: op for op in self.hub.library.list()}

    def brain_log(self):
        return [row["title"] for row in self.hub.devlog_list(200)["entries"]
                if row.get("actor") == "brain"]

    def only_observe_left(self):
        for op_id in list(self.ops_by_id()):
            if op_id != "op-builtin-observe":
                self.hub.operation_delete(op_id)

    def set_permission(self, op_id, permission):
        op = self.ops_by_id()[op_id]
        self.hub.operation_save({"name": op["name"], "steps": op["steps"],
                                 "permission": permission}, operation_id=op_id)


class BuiltinAndSchemaTest(BrainCase):
    def test_the_observe_cycle_is_builtin_and_always_allowed(self):
        ops = self.hub.operation_list()   # the panel path, seeds built-ins
        ops = {op["id"]: op for op in ops}
        self.assertIn("op-builtin-observe", ops)
        # round 26: the brain's three thinking tools (observe, consult pi,
        # choose the next operation) run on auto; everything else still asks.
        thinking = ("op-builtin-observe", "op-builtin-pi-consult",
                    "op-builtin-pi-next")
        for op_id in thinking:
            self.assertEqual(ops[op_id]["permission"], "auto",
                             "%s is part of the brain's thinking" % op_id)
        for op_id, op in ops.items():
            if op_id not in thinking:
                self.assertEqual(op["permission"], "ask",
                                 "%s must default to ask-first" % op_id)
        # The fixed thinking is one screenshot plus one text capture -
        # stored, like everything else, in the one language (round 26).
        steps = ops["op-builtin-observe"]["steps"]
        self.assertEqual([step["type"] for step in steps],
                         ["python", "python"])
        self.assertIn("screenshot('observe')", steps[0]["code"])
        self.assertIn("capture_text(limit=20000)", steps[1]["code"])

    def test_an_old_database_gains_the_permission_column(self):
        # A database from before round 24: an operations table with no
        # permission column and one row in it. Opening it must migrate.
        root = os.path.join(self.tmp, "old")
        base = os.path.join(root, "automation")
        os.makedirs(base)
        db = sqlite3.connect(os.path.join(base, "agent.db"))
        db.executescript(
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);"
            "CREATE TABLE operations ("
            " id TEXT PRIMARY KEY,"
            " name TEXT NOT NULL DEFAULT '',"
            " description TEXT NOT NULL DEFAULT '',"
            " kind TEXT NOT NULL DEFAULT '',"
            " builtin TEXT NOT NULL DEFAULT '',"
            " tags TEXT NOT NULL DEFAULT '[]',"
            " steps TEXT NOT NULL DEFAULT '[]',"
            " created_at REAL NOT NULL DEFAULT 0,"
            " updated_at REAL NOT NULL DEFAULT 0,"
            " last_run_at REAL NOT NULL DEFAULT 0,"
            " last_status TEXT NOT NULL DEFAULT '',"
            " run_count INTEGER NOT NULL DEFAULT 0);"
            "INSERT INTO operations (id, name) VALUES ('op-old', 'قدیمی');")
        db.commit()
        db.close()

        from automation.agent_store import AgentStore
        store = AgentStore(root)
        self.addCleanup(store.close)
        row = store.get_operation("op-old")
        self.assertIsNotNone(row, "the migrated table must keep its rows")
        self.assertEqual(row["permission"], "ask",
                         "existing operations must default to ask-first")
        # Opening twice is a no-op, not a crash.
        store2 = AgentStore(root)
        self.addCleanup(store2.close)
        self.assertEqual(store2.get_operation("op-old")["permission"], "ask")

    def test_a_nonsense_permission_is_refused(self):
        with self.assertRaises(AgentError) as caught:
            self.set_permission("op-builtin-observe", "yolo")
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(self.ops_by_id()["op-builtin-observe"]["permission"],
                         "auto", "a refused edit must not change anything")

    def test_the_policy_settings_are_registered(self):
        for key in DEFAULTS:
            self.assertIn(key, PLAIN_SETTINGS,
                          "%s must be writable from the panel" % key)
        self.assertIn("telegram.channel.brain", PLAIN_SETTINGS)
        # The hub casts what the panel sends, like every other setting.
        self.hub.save_settings({"brain.runGapSec": "45",
                                "log.forceStepShots": False})
        self.assertEqual(self.hub.store.get_setting("brain.runGapSec"), 45)
        self.assertIs(self.hub.store.get_setting("log.forceStepShots"), False)


class DecisionCycleTest(BrainCase):
    def test_a_disabled_brain_stays_quiet(self):
        self.brain.tick()
        self.assertEqual(self.engine.started, [])
        self.assertEqual(self.brain_log(), [])
        self.assertEqual(self.told, [])

    def test_the_resting_auto_operation_runs_and_cooldowns_hold(self):
        self.enable()
        # Only the auto operation exists: an ask-first sibling would turn
        # the cooldown wait into a suggestion instead (which is correct
        # brain behaviour, but a different test).
        self.only_observe_left()
        self.brain.tick()
        self.assertEqual(len(self.engine.started), 1)
        self.assertEqual(self.engine.started[0]["name"], "چرخهٔ مشاهده")
        self.assertIn("چرخهٔ مشاهده", " ".join(self.brain_log()))
        # Right away again: the run gap keeps the desk still.
        self.brain.tick()
        self.assertEqual(len(self.engine.started), 1)
        # Past the gap but inside the per-operation cooldown: still nothing.
        self.now[0] += 121
        self.brain.tick()
        self.assertEqual(len(self.engine.started), 1,
                         "the cooldown must outlast the run gap")
        # Past both: the same operation may run again.
        self.now[0] += 600
        self.brain.tick()
        self.assertEqual(len(self.engine.started), 2)

    def test_a_busy_desk_blocks_and_an_ending_is_reported(self):
        self.enable()
        self.engine.busy_flag = True
        self.brain.tick()
        self.assertEqual(self.engine.started, [])
        # The run ends in error: the brain must see the transition, log it,
        # tell Telegram, and hold the gap before proposing anything new.
        self.engine.history = [{"runId": "run-9", "status": "error"}]
        self.engine.busy_flag = False
        self.brain.tick()
        log = " ".join(self.brain_log())
        self.assertIn("run-9", log)
        self.assertIn("error", log)
        self.assertTrue(self.told, "the ending should reach Telegram")
        self.assertEqual(self.engine.started, [],
                         "the gap after an ending blocks new runs")
        # With notify off, the ending is logged but not sent.
        self.told.clear()
        self.hub.save_settings({"brain.notify": False})
        self.engine.busy_flag = True
        self.brain.tick()
        self.engine.busy_flag = False
        self.brain.tick()
        self.assertEqual(self.told, [], "notify=false must silence Telegram")

    def test_ask_operations_become_suggestions_with_a_deadline(self):
        self.enable()
        self.only_observe_left()
        self.set_permission("op-builtin-observe", "ask")
        self.brain.tick()
        status = self.hub.brain_status()
        suggestion = status["suggestion"]
        self.assertIsNotNone(suggestion, "an ask operation must be proposed")
        self.assertEqual(suggestion["operationId"], "op-builtin-observe")
        self.assertEqual(self.engine.started, [],
                         "a proposal must never start the engine")
        self.assertTrue(any("پیشنهاد" in text for text in self.told),
                        "the proposal should reach Telegram")
        # Before the deadline the question owns the desk.
        self.now[0] += 60
        self.brain.tick()
        self.assertIsNotNone(self.hub.brain_status()["suggestion"])
        # Past the deadline it expires, is logged, and the brain falls back.
        self.now[0] += 15 * 60
        self.brain.tick()
        self.assertIsNone(self.hub.brain_status()["suggestion"])
        self.assertIn("بی‌پاسخ", " ".join(self.brain_log()))
        # The fallback: observe is allowed again, so the brain runs it itself.
        self.set_permission("op-builtin-observe", "auto")
        self.now[0] += 31 * 60   # past the ask cooldown as well
        self.brain.tick()
        self.assertEqual(len(self.engine.started), 1,
                         "after an ignored proposal the auto work resumes")

    def test_decisions_run_or_reject_the_open_suggestion(self):
        self.enable()
        self.only_observe_left()
        self.set_permission("op-builtin-observe", "ask")
        self.brain.tick()
        suggestion = self.hub.brain_status()["suggestion"]
        self.assertIsNotNone(suggestion)
        # A wrong id is refused and must not consume the suggestion.
        with self.assertRaises(AgentError) as caught:
            self.hub.brain_decide("nope", "run")
        self.assertEqual(caught.exception.status, 409)
        self.assertIsNotNone(self.hub.brain_status()["suggestion"])
        # Reject clears it without running anything.
        out = self.hub.brain_decide(suggestion["id"], "reject")
        self.assertEqual(out["decision"], "reject")
        self.assertIsNone(self.hub.brain_status()["suggestion"])
        self.assertEqual(self.engine.started, [])
        # Past the ask cooldown a new proposal appears; run starts the engine.
        self.now[0] += 31 * 60
        self.brain.tick()
        suggestion = self.hub.brain_status()["suggestion"]
        self.assertIsNotNone(suggestion)
        out = self.hub.brain_decide(suggestion["id"], "run")
        self.assertEqual(out["decision"], "run")
        self.assertEqual(len(self.engine.started), 1)
        # Deciding twice on one suggestion is refused.
        with self.assertRaises(AgentError):
            self.hub.brain_decide(suggestion["id"], "run")

    def test_the_kill_switch_stops_the_brain_too(self):
        self.enable()
        self.hub.store.set_kill_switch(True, actor="tester")
        with self.assertRaises(AgentError):
            self.hub.brain_toggle(True, actor="tester")


class LoggingPolicyTest(BrainCase):
    def test_the_policy_forces_before_and_after_shots(self):
        flow = {"name": "t", "steps": [{"id": "s1", "type": "wait", "ms": 1}],
                "settings": {"screenshotBeforeEachStep": False,
                             "screenshotAfterEachStep": False}}
        forced = self.brain.enforce_logging(flow)
        self.assertTrue(forced["settings"]["screenshotBeforeEachStep"])
        self.assertTrue(forced["settings"]["screenshotAfterEachStep"])
        self.assertTrue(forced["settings"]["screenshotOnError"])
        self.assertFalse(flow["settings"]["screenshotBeforeEachStep"],
                         "the original flow must not be mutated")
        # With the policy off the flow passes through untouched.
        self.hub.save_settings({"log.forceStepShots": False})
        self.assertIs(self.brain.enforce_logging(flow), flow)

    def test_the_engine_asks_the_brain_before_every_run(self):
        engine = AutomationEngine(QuietBackend(), self.tmp,
                                  sleep=lambda seconds: None,
                                  clock=lambda: self.now[0], agent=self.hub)
        engine.start({"name": "policy",
                      "steps": [{"id": "w1", "type": "wait", "ms": 1}],
                      "settings": {"screenshotBeforeEachStep": False}})
        deadline = time.time() + 10
        while engine.busy() and time.time() < deadline:
            time.sleep(0.02)
        settings = engine._state.flow.get("settings") or {}
        self.assertTrue(settings.get("screenshotBeforeEachStep"),
                        "engine.start must apply the brain's logging policy")
        self.assertTrue(settings.get("screenshotAfterEachStep"))

    def test_old_shots_are_pruned_and_texts_stay_forever(self):
        now = self.now[0]
        day = 86400
        # Three archived shots and two run folders, backdated in the file.
        for name, role in (("fresh.png", "before"), ("old.png", "after"),
                           ("old-error.png", "error")):
            self.shots.add({"name": name, "role": role, "runId": "run-x"})
        path = os.path.join(self.tmp, "archive", "shots.json")
        with open(path, encoding="utf-8") as handle:
            rows = json.load(handle)
        ages = {"fresh.png": now - day, "old.png": now - 10 * day,
                "old-error.png": now - 10 * day}
        for row in rows:
            row["createdAt"] = ages[row["name"]]
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(rows, handle)
        public = os.path.join(self.tmp, "public")
        os.makedirs(public, exist_ok=True)
        for name in ("fresh.png", "old.png"):
            with open(os.path.join(public, name), "w") as handle:
                handle.write("x")
        for name, mtime in (("run-old", now - 40 * day), ("run-new", now - day)):
            folder = os.path.join(self.tmp, "runs", name)
            os.makedirs(folder, exist_ok=True)
            os.utime(folder, (mtime, mtime))
        # The texts archive is the permanent half of the log.
        texts_path = os.path.join(self.tmp, "archive", "texts.json")
        with open(texts_path, "w", encoding="utf-8") as handle:
            json.dump([{"id": "txt1", "name": "ancient", "text": "سلام",
                        "createdAt": now - 400 * day}], handle)

        self.brain._maybe_prune(now)

        names = sorted(row["name"] for row in self.shots.list())
        self.assertEqual(names, ["fresh.png", "old-error.png"],
                         "only the ordinary shot past its horizon goes")
        self.assertFalse(os.path.exists(os.path.join(public, "old.png")),
                         "the published file goes with the entry")
        self.assertTrue(os.path.exists(os.path.join(public, "fresh.png")))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "runs", "run-old")))
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "runs", "run-new")))
        self.assertIn("هرزسازی", " ".join(self.brain_log()))
        with open(texts_path, encoding="utf-8") as handle:
            self.assertEqual(len(json.load(handle)), 1,
                             "texts stay forever")
        # The pruning pass respects its own cadence: nothing happens again
        # one second later.
        self.brain._maybe_prune(now + 1)
        self.assertEqual(len([row for row in self.hub.devlog_list(200)["entries"]
                              if row.get("actor") == "brain"
                              and "هرزسازی" in row["title"]]), 1)


class ApiAndThreadTest(BrainCase):
    def call(self, api, method, path, body=None, headers=None):
        raw = json.dumps(body).encode("utf-8") if body is not None else b""
        status, _, data = api.handle(
            method, path, body=raw, query={},
            headers={"x-automation-token": TOKEN} if headers is None else headers)
        try:
            payload = json.loads(data.decode("utf-8"))
        except ValueError:
            payload = data[:200]
        return status, payload

    def test_the_brain_routes_are_gated_and_shaped(self):
        api = AutomationApi(None, FlowStore(self.tmp), None, data_dir=self.tmp,
                            token=TOKEN, agent=self.hub)
        status, payload = self.call(api, "GET", "/agent/brain")
        self.assertEqual(status, 200)
        for key in ("enabled", "alive", "busy", "suggestion", "policy",
                    "autoOps", "askOps", "log"):
            self.assertIn(key, payload)
        self.assertEqual(payload["enabled"], False)
        self.assertEqual(sorted(op["id"] for op in payload["autoOps"]),
                         ["op-builtin-observe", "op-builtin-pi-consult",
                          "op-builtin-pi-next"])
        self.assertEqual(self.call(api, "GET", "/agent/brain", headers={})[0],
                         401)
        self.assertEqual(self.call(api, "POST", "/agent/brain/toggle", {})[0],
                         400)
        status, payload = self.call(api, "POST", "/agent/brain/toggle",
                                    {"enabled": True})
        self.assertEqual(status, 200)
        self.assertTrue(payload["enabled"])
        self.assertEqual(
            self.call(api, "POST", "/agent/brain/suggestion",
                      {"id": "x", "action": "run"})[0], 409,
            "a decision without an open suggestion is a conflict")
        self.assertEqual(
            self.call(api, "POST", "/agent/brain/suggestion",
                      {"id": "x", "action": "dance"})[0], 400)

    def test_the_loop_starts_and_stops_like_the_scheduler(self):
        brain = Brain(self.hub, store=self.hub.store, shots=self.shots,
                      clock=lambda: self.now[0], sleep=lambda seconds: None,
                      interval=0.01)
        brain.start()
        self.assertTrue(brain.alive())
        brain.start()   # a second start is a no-op, not a second thread
        self.assertTrue(brain.alive())
        brain.stop(timeout=3)
        self.assertFalse(brain.alive())

    def test_hub_start_and_stop_carry_the_brain(self):
        self.hub.start()
        self.assertTrue(self.hub.brain.alive())
        self.hub.stop()
        self.assertFalse(self.hub.brain.alive())


class RespondRulesTest(BrainCase):
    """Round 25: the brain answers the operator by its honest rulebook."""

    def test_help_lists_the_rulebook(self):
        reply = self.brain.respond("کمک")["reply"]
        self.assertEqual(reply, self.brain.HELP_TEXT)
        texts = [row["text"] for row in self.brain.status()["messages"]]
        self.assertIn("کمک", texts)
        self.assertIn(self.brain.HELP_TEXT, texts)

    def test_stop_when_idle_says_so(self):
        self.assertIn("در جریان نیست", self.brain.respond("توقف")["reply"])
        self.assertEqual(self.engine.stop_calls, 0)

    def test_stop_when_busy_requests_the_stop(self):
        self.engine.busy_flag = True
        self.assertIn("درخواست توقف زدم", self.brain.respond("توقف")["reply"])
        self.assertEqual(self.engine.stop_calls, 1)

    def test_remember_and_recall(self):
        self.assertIn("به حافظهٔ مغز سپرده شد",
                      self.brain.respond("یادت باشه فردا ساعت ۸")["reply"])
        reply = self.brain.respond("حافظه")["reply"]
        self.assertIn("یادداشت‌های مغز", reply)
        self.assertIn("فردا ساعت ۸", reply)
        memory = [row["text"] for row in self.brain.status()["memory"]]
        self.assertIn("فردا ساعت ۸", memory)

    def test_status_reports_desk_and_brain(self):
        reply = self.brain.respond("وضعیت")["reply"]
        self.assertIn("مغز خاموش است", reply)
        self.assertIn("آزاد است", reply)

    def test_run_an_auto_operation_starts_it_right_away(self):
        self.hub.operation_list()   # seeds the built-ins
        reply = self.brain.respond("اجرا: چرخهٔ مشاهده")["reply"]
        self.assertIn("اجرا را شروع کردم", reply)
        self.assertEqual(len(self.engine.started), 1)

    def test_run_an_ask_operation_becomes_a_card_suggestion(self):
        self.hub.operation_list()
        self.set_permission("op-builtin-observe", "ask")
        reply = self.brain.respond("اجرا: چرخهٔ مشاهده")["reply"]
        self.assertIn("روی کارت", reply)
        self.assertIsNotNone(self.brain.status()["suggestion"])

    def test_anything_else_becomes_a_memory_note(self):
        reply = self.brain.respond("سلام مغز")["reply"]
        self.assertIn("دریافت شد", reply)
        self.assertIn("سلام مغز",
                      [row["text"] for row in self.brain.status()["memory"]])

    def test_an_empty_message_is_refused(self):
        self.assertRaises(ValueError, self.brain.respond, "   ")
        with self.assertRaises(AgentError) as caught:
            self.hub.brain_message("")
        self.assertEqual(caught.exception.status, 400)


class FakeDocLlm:
    """A coworker agent that writes whatever the test tells it to."""

    def __init__(self, reply):
        self.reply = reply
        self.prompts = []

    def describe(self):
        return {"provider": "fake", "configured": True}

    def complete(self, prompt, system="", **kwargs):
        self.prompts.append(prompt)
        return {"text": self.reply}


class BoomLlm(FakeDocLlm):
    def __init__(self):
        super().__init__("")

    def complete(self, *args, **kwargs):
        raise RuntimeError("the browser chat is down")


class MessageAndDocRoutesTest(BrainCase):
    """Round 25: POST /agent/brain/message and POST /agent/step-doc."""

    def call(self, api, method, path, body=None, headers=None):
        raw = json.dumps(body).encode("utf-8") if body is not None else b""
        status, _, data = api.handle(
            method, path, body=raw, query={},
            headers={"x-automation-token": TOKEN} if headers is None else headers)
        try:
            payload = json.loads(data.decode("utf-8"))
        except ValueError:
            payload = data[:200]
        return status, payload

    def api(self):
        return AutomationApi(None, FlowStore(self.tmp), None, data_dir=self.tmp,
                             token=TOKEN, agent=self.hub)

    def test_the_message_route_is_gated_shaped_and_answered(self):
        api = self.api()
        self.assertEqual(
            self.call(api, "POST", "/agent/brain/message", {}, headers={})[0], 401)
        self.assertEqual(self.call(api, "POST", "/agent/brain/message", {})[0], 400)
        status, payload = self.call(api, "POST", "/agent/brain/message",
                                    {"text": "وضعیت"})
        self.assertEqual(status, 200)
        self.assertIn("مغز", payload["reply"])

    def test_the_step_doc_route_refuses_then_writes_then_reports_failure(self):
        api = self.api()
        self.assertEqual(self.call(api, "POST", "/agent/step-doc", {})[0], 400)
        step = {"type": "click", "x": 1, "y": 2}
        # no coworker agent configured yet: an honest 409, not a fake text
        self.assertEqual(
            self.call(api, "POST", "/agent/step-doc", {"step": step})[0], 409)
        self.hub.llm = FakeDocLlm("روی دکمهٔ ورود کلیک می‌کند.")
        status, payload = self.call(api, "POST", "/agent/step-doc", {"step": step})
        self.assertEqual(status, 200)
        self.assertEqual(payload["text"], "روی دکمهٔ ورود کلیک می‌کند.")
        # a failing agent is a 502 with the reason, never an empty note
        self.hub.llm = BoomLlm()
        self.assertEqual(
            self.call(api, "POST", "/agent/step-doc", {"step": step})[0], 502)


if __name__ == "__main__":
    unittest.main()
