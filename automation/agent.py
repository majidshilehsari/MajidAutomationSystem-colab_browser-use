"""The coworker agent: one facade over the store, the model, Telegram, the
scheduler, the script gate and the captcha chain.

`AutomationApi` stays thin: it parses a request, calls a method here, and turns
the result into a response. Everything that has to be wired together - and
everything that has to be replaceable by a fake in a test - lives in this class.

Design decisions worth knowing before changing anything:

* **Nothing is constructed eagerly that costs a process.** The agent's browser
  is attached to a CDP port, not spawned; the scheduler thread only starts when
  `start()` is called. A deployment that never opens the agent tab runs no extra
  work at all.
* **The kill switch is checked at every entry point that acts** (chat, jobs,
  scripts, captcha clicks), not just in the UI, because the UI is not the only
  caller.
* **Secrets never travel through the database or the audit log.** They live in a
  0600 JSON file next to it; see `agent_store.py`.
* **Read-only SQL is offered, destructive SQL is not.** "Full access to the
  database" for an agent that reads untrusted web pages is a loaded phrase, so
  the query endpoint opens a `mode=ro` connection and accepts one statement.
  Writing goes through the typed CRUD methods, which validate and audit.
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

from automation import schema
from automation.agent_store import AgentStore, SCRIPT_APPROVED
from automation.ai_browser import AiBrowser, AiBrowserError
from automation.captcha import CaptchaError, CaptchaSolver
from automation.llm import LlmClient, LlmError, extract_json
from automation.scheduler import Scheduler, SchedulerBusy, next_run
from automation.scripts import ScriptError, ScriptRunner
from automation.telegram_client import (MODE_ACCOUNT, MODE_BOT, MODE_OFF, MODES,
                                        Notifier, TelegramError)


# Settings the sidebar may write. Anything not listed here is rejected, so a
# typo cannot create a key that silently does nothing, and a hostile page cannot
# invent one.
PLAIN_SETTINGS = {
    "ai.provider": str,
    "ai.chatProvider": str,
    "ai.baseUrl": str,
    "ai.model": str,
    "ai.temperature": float,
    "ai.timeout": float,
    "ai.freshChat": bool,
    "ai.providers": dict,
    "telegram.mode": str,
    "telegram.targets": list,
    "telegram.handoffTemplate": str,
    "telegram.resultTemplate": str,
    "telegram.notifyHandoff": bool,
    "telegram.notifyJobFailed": bool,
    "telegram.notifyJobDone": bool,
    "telegram.sessionPath": str,
    "captcha.enabled": bool,
    "captcha.strategies": list,
    "captcha.autoClick": bool,
    "captcha.maxAttempts": int,
    "captcha.minConfidence": float,
    "captcha.extension": str,
    "captcha.notifyOnEscalation": bool,
    "scripts.passToken": bool,
    "scripts.timeout": float,
    "public.base": str,
}

SECRET_SETTINGS = {
    "ai.apiKey", "telegram.botToken", "telegram.apiId", "telegram.apiHash",
}

# A SELECT, and only a SELECT: one statement, no trailing semicolon trickery.
_READ_ONLY_SQL = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)
_FORBIDDEN_SQL = re.compile(
    r"\b(insert|update|delete|drop|alter|create|attach|pragma|vacuum|replace)\b",
    re.IGNORECASE)


class AgentError(Exception):
    """Something the operator should read, with a status to go with it."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class AgentHub:
    def __init__(self, data_dir: str, engine: Any = None, flows: Any = None,
                 backend: Any = None, cdp: Any = None, token: str = "",
                 public_base: str = "", ai_port: int = 9223,
                 ai_display: str = ":2", clock: Any = time.time,
                 sleep: Any = time.sleep, store: Optional[AgentStore] = None,
                 llm: Optional[LlmClient] = None,
                 notifier: Optional[Notifier] = None,
                 ai_browser: Any = None, scheduler: Optional[Scheduler] = None,
                 scripts: Optional[ScriptRunner] = None,
                 captcha: Optional[CaptchaSolver] = None) -> None:
        self.data_dir = data_dir
        self.engine = engine
        self.flows = flows
        self.backend = backend
        self.cdp = cdp
        self.token = token
        self.ai_port = ai_port
        self.ai_display = ai_display
        self._clock = clock
        self._sleep = sleep
        # Held for the length of one manual "run now", so two operators
        # cannot start two runs against the same single mouse.
        self._job_lock = threading.Lock()

        self.store = store if store is not None else AgentStore(data_dir, clock=clock)
        if public_base:
            self.store.set_setting("public.base", public_base, actor="startup")
        self.public_base = str(self.store.get_setting("public.base", public_base)
                               or public_base or "")

        if ai_browser is None and cdp is not None:
            ai_browser = AiBrowser(cdp, port=ai_port, display=ai_display,
                                   providers=self.store.get_setting("ai.providers") or None,
                                   clock=clock, sleep=sleep, runner=backend)
        self.ai_browser = ai_browser

        self.llm = llm if llm is not None else LlmClient(self.store,
                                                         ai_browser=self.ai_browser,
                                                         clock=clock)
        self.notifier = notifier if notifier is not None else Notifier(
            self.store, public_base=self.public_base, clock=clock,
            store_path=self.public_shot_path)
        self.scripts = scripts if scripts is not None else ScriptRunner(
            self.store, clock=clock, data_dir=data_dir,
            api_base="%s/automation/api" % self.public_base if self.public_base else "",
            display=os.environ.get("DISPLAY", ":1"))
        self.captcha = captcha if captcha is not None else CaptchaSolver(
            self.store, self.llm, notifier=self.notifier, control=backend,
            clock=clock, image_size=_png_size)
        self.scheduler = scheduler if scheduler is not None else Scheduler(
            self.store, self._run_job, clock=clock, sleep=sleep)

    # -- lifecycle ------------------------------------------------------
    def start(self) -> None:
        self.scheduler.ensure_schedules()
        self.scheduler.start()

    def stop(self) -> None:
        self.scheduler.stop()
        self.store.close()

    # -- small shared helpers -------------------------------------------
    def public_shot_path(self, name: str) -> str:
        """Absolute path of a published screenshot, or '' when it is not there."""
        if not name or not re.match(r"^[A-Za-z0-9._-]+\.png$", name):
            return ""
        path = os.path.join(self.data_dir, "public", name)
        return path if os.path.isfile(path) else ""

    def _guard(self) -> None:
        if self.store.kill_switch_engaged():
            raise AgentError("the kill switch is on: the agent may not act", 409)

    # -- settings -------------------------------------------------------
    def overview(self) -> Dict[str, Any]:
        """Everything the agent tab needs to render, without any secret value."""
        return {
            "settings": self.store.list_settings(),
            "llm": self.llm.describe(),
            "telegram": self.telegram_status(),
            "captcha": self.captcha.config(),
            "captchaExtension": self.captcha.extension_status(),
            "scripts": self.scripts.config(),
            "scheduler": {"alive": self.scheduler.alive,
                          "runningJob": self.scheduler.running_job,
                          "lastTick": self.scheduler.last_tick,
                          "jobs": len(self.store.list_jobs())},
            "killSwitch": self.store.kill_switch_engaged(),
            "counts": {"jobs": len(self.store.list_jobs()),
                       "scripts": len(self.store.list_scripts()),
                       "pendingScripts": len(self.scripts.pending()),
                       "notes": len(self.store.list_notes(limit=1000))},
        }

    def save_settings(self, data: Dict[str, Any], actor: str = "operator") -> Dict[str, Any]:
        for key, value in data.items():
            if key in SECRET_SETTINGS:
                # An empty value clears a secret instead of storing one.
                if value in ("", None):
                    self.store.delete_setting(key, actor=actor)
                else:
                    self.store.set_setting(key, str(value), secret=True, actor=actor)
                continue
            cast = PLAIN_SETTINGS.get(key)
            if cast is None:
                raise AgentError("unknown setting: %s" % key)
            if cast is bool:
                value = bool(value)
            elif cast is float:
                value = float(value)
            elif cast is int:
                value = int(value)
            elif cast in (list, dict) and not isinstance(value, cast):
                raise AgentError("%s must be a %s" % (key, cast.__name__))
            elif cast is str:
                value = str(value)
            if key == "telegram.mode" and value not in MODES:
                raise AgentError("telegram.mode must be one of: %s" % ", ".join(MODES))
            if key == "ai.provider" and value not in ("ai-browser", "http-api"):
                raise AgentError("ai.provider must be ai-browser or http-api")
            self.store.set_setting(key, value, actor=actor)
        if "ai.providers" in data and self.ai_browser is not None:
            # Selector overrides take effect immediately, no restart needed.
            merged = dict(self.ai_browser.providers)
            for name, profile in (data["ai.providers"] or {}).items():
                base = dict(merged.get(name) or {})
                base.update(profile or {})
                merged[name] = base
            self.ai_browser.providers = merged
        return self.store.list_settings()

    def set_kill_switch(self, engaged: bool, actor: str = "operator") -> Dict[str, Any]:
        self.store.set_kill_switch(bool(engaged), actor=actor)
        if engaged:
            self.scripts.kill_running(actor=actor)
        return {"killSwitch": self.store.kill_switch_engaged()}

    def query(self, sql: str, limit: int = 200) -> Dict[str, Any]:
        """Read-only SQL over the agent's own database."""
        statement = (sql or "").strip().rstrip(";").strip()
        if not statement:
            raise AgentError("a statement is required")
        if ";" in statement:
            raise AgentError("exactly one statement is allowed")
        if not _READ_ONLY_SQL.match(statement):
            raise AgentError("only SELECT (or WITH ... SELECT) is allowed")
        if _FORBIDDEN_SQL.search(statement):
            raise AgentError("this statement writes; use the CRUD endpoints")
        limit = max(1, min(int(limit), 1000))
        uri = "file:%s?mode=ro" % self.store.db_path
        connection = sqlite3.connect(uri, uri=True, timeout=5.0)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(statement + " LIMIT %d" % limit).fetchall() \
                if " limit " not in statement.lower() else \
                connection.execute(statement).fetchall()
        except sqlite3.Error as exc:
            raise AgentError("query failed: %s" % exc)
        finally:
            connection.close()
        self.store.audit("operator", "db.query", statement[:300])
        return {"columns": list(rows[0].keys()) if rows else [],
                "rows": [dict(row) for row in rows], "count": len(rows)}

    # -- chatting with the agent ----------------------------------------
    def ask(self, prompt: str, system: str = "", images: Optional[List[str]] = None,
            want_json: bool = False, actor: str = "operator") -> Dict[str, Any]:
        self._guard()
        if not prompt.strip():
            raise AgentError("a prompt is required")
        try:
            reply = self.llm.complete(prompt, system=system, images=images,
                                      want_json=want_json,
                                      timeout=float(self.store.get_setting(
                                          "ai.timeout", 180)))
        except (LlmError, AiBrowserError) as exc:
            self.store.audit(actor, "agent.ask.failed", str(exc)[:300])
            raise AgentError(str(exc), 502)
        self.store.audit(actor, "agent.ask", "%d chars via %s"
                         % (len(prompt), reply.get("provider")))
        return reply

    def chat(self, prompt: str, actor: str = "operator") -> Dict[str, Any]:
        """Ask, and if the answer contains a flow, validate it for the operator."""
        reply = self.ask(prompt, actor=actor)
        out: Dict[str, Any] = {"text": reply.get("text", ""),
                               "provider": reply.get("provider"),
                               "elapsed": reply.get("elapsed"),
                               "source": reply.get("source") or {}}
        parsed = extract_json(reply.get("text", ""))
        if isinstance(parsed, dict) and isinstance(parsed.get("steps"), list):
            normalized, errors = schema.validate_flow(parsed)
            out["flow"] = normalized if not errors else parsed
            out["flowErrors"] = errors
            out["flowValid"] = not errors
        return out

    # -- Telegram -------------------------------------------------------
    def telegram_status(self, probe: bool = False) -> Dict[str, Any]:
        """Local configuration only, unless `probe` asks for a round trip.

        The sidebar calls this on every tab switch, so it must stay cheap and
        must not fail when Telegram is unreachable; `probe=True` (the "is it
        really working?" button) is the only thing that touches the network.
        """
        mode = self.notifier.mode()
        info: Dict[str, Any] = {"mode": mode, "targets": self.notifier.targets(),
                                "configured": self.notifier.configured(),
                                "accountAuthorized": None, "error": "",
                                "botTokenSet": bool(self.store.get_secret("telegram.botToken", "")),
                                "apiCredentialsSet": bool(self.store.get_secret("telegram.apiId", "")),
                                "phone": self.store.get_setting("telegram.phone", "")}
        if mode == MODE_OFF or not probe:
            return info
        try:
            client = self.notifier.transport()
        except TelegramError as exc:
            info["error"] = str(exc)
            return info
        try:
            info["identity"] = client.whoami()
            if mode == MODE_ACCOUNT:
                info["accountAuthorized"] = True
        except TelegramError as exc:
            info["error"] = str(exc)
        return info

    def telegram_targets(self) -> List[Dict[str, Any]]:
        """Chats the operator can pick from, so nobody hunts for a numeric id."""
        mode = self.notifier.mode()
        if mode == MODE_OFF:
            raise AgentError("telegram is off; set telegram.mode first")
        try:
            return self.notifier.transport().list_targets()
        except TelegramError as exc:
            raise AgentError(str(exc), 502)

    def telegram_test(self, text: str = "") -> Dict[str, Any]:
        mode = self.notifier.mode()
        if mode == MODE_OFF:
            raise AgentError("telegram is off")
        message = text or "✅ پیام آزمایشی از Majid Automation System"
        try:
            result = self.notifier.send(message)
        except TelegramError as exc:
            raise AgentError(str(exc), 502)
        if not result.get("sent"):
            raise AgentError("nothing was delivered: %s" % result.get("reason"), 502)
        return result

    def telegram_login_start(self, phone: str) -> Dict[str, Any]:
        """Step one of logging in as the operator's own account."""
        try:
            client = self.notifier.transport(MODE_ACCOUNT)
        except TelegramError as exc:
            raise AgentError(str(exc), 400)
        try:
            result = client.request_code(str(phone))
        except TelegramError as exc:
            raise AgentError(str(exc), 502)
        # The hash is not a credential on its own, but it is only useful with
        # the phone number, so it goes to the secret file rather than settings.
        self.store.set_secret("telegram.phoneCodeHash", result.get("phoneCodeHash", ""))
        self.store.set_setting("telegram.phone", str(phone), actor="operator")
        self.store.audit("operator", "telegram.login.started", str(phone)[:20])
        return {"sent": True, "phone": str(phone)}

    def telegram_login_finish(self, code: str, password: str = "") -> Dict[str, Any]:
        phone = str(self.store.get_setting("telegram.phone", ""))
        if not phone:
            raise AgentError("start the login first")
        try:
            client = self.notifier.transport(MODE_ACCOUNT)
            result = client.sign_in(phone, str(code),
                                    self.store.get_secret("telegram.phoneCodeHash", ""),
                                    password)
        except TelegramError as exc:
            raise AgentError(str(exc), 502)
        self.store.delete_setting("telegram.phoneCodeHash", actor="operator")
        self.store.audit("operator", "telegram.login.finished", phone[:20])
        return result

    # -- jobs -----------------------------------------------------------
    def jobs(self) -> List[Dict[str, Any]]:
        return self.store.list_jobs()

    def save_job(self, data: Dict[str, Any], job_id: Optional[str] = None,
                 actor: str = "operator") -> Dict[str, Any]:
        if job_id:
            existing = self.store.get_job(job_id)
            if existing is None:
                raise AgentError("no job with id %s" % job_id, 404)
            # Pausing a job sends only {"enabled": false}; validating that on
            # its own would reject every partial update, so the stored job is
            # the base and the request only overrides what it mentions.
            merged = dict(existing)
            merged.update({key: value for key, value in data.items()
                           if key != "payload"})
            payload = dict(existing.get("payload") or {})
            payload.update(data.get("payload") or {})
            merged["payload"] = payload
            data = merged
        action = str((data.get("payload") or {}).get("action") or "flow")
        if action not in ("flow", "script", "prompt"):
            raise AgentError("payload.action must be flow, script or prompt")
        target = str(data.get("target") or "")
        if action != "prompt" and not target:
            raise AgentError("this job needs a target")
        if action == "flow" and self.flows is not None and target:
            if self.flows.get(target) is None:
                raise AgentError("no flow named %r" % target, 404)
        if action == "script":
            script = self.store.get_script(target)
            if script is None:
                raise AgentError("no script with id %s" % target, 404)
            if str(script.get("status")) != SCRIPT_APPROVED:
                raise AgentError("script %s is %s; a scheduled job needs an "
                                 "approved script" % (target, script.get("status")), 409)
        try:
            probe = {"kind": data.get("kind"), "schedule": data.get("schedule")}
            moment = next_run(probe, self._clock())
        except ValueError as exc:
            raise AgentError(str(exc))
        data = dict(data)
        data.setdefault("payload", {})["action"] = action
        job = self.store.upsert_job(data, job_id=job_id, actor=actor)
        if job.get("next_run_at") is None and job.get("enabled"):
            self.store.set_job_next_run(job["id"], moment)
            # Re-read: the row just changed, and the caller renders this answer.
            job = self.store.get_job(job["id"]) or job
        return job

    def delete_job(self, job_id: str, actor: str = "operator") -> Dict[str, Any]:
        if not self.store.delete_job(job_id, actor=actor):
            raise AgentError("no job with id %s" % job_id, 404)
        return {"deleted": job_id}

    def run_job_now(self, job_id: str, actor: str = "operator",
                    wait: bool = False) -> Dict[str, Any]:
        """Start a job on demand.

        A flow can legitimately take minutes, and this is reached over HTTP
        through an ingress whose idle timeout nobody has measured, so by
        default the run is handed to a worker thread and the answer says so.
        The outcome still lands in the job row and the audit log, which is
        what the jobs pane renders. `wait=True` keeps the old synchronous
        behaviour for callers that are already off the request path.
        """
        job = self.store.get_job(job_id)
        if job is None:
            raise AgentError("no job with id %s" % job_id, 404)
        self._guard()
        self._raise_if_busy(job)
        self.store.audit(actor, "job.runNow", job_id)
        if wait:
            return self._run_job_guarded(job)
        if not self._job_lock.acquire(blocking=False):
            raise AgentError("another manual job run is still going", 409)
        thread = threading.Thread(target=self._run_job_worker,
                                 args=(dict(job), self._job_lock),
                                 name="agent-job-%s" % job_id, daemon=True)
        thread.start()
        return {"status": "queued", "id": job_id,
                "note": "the run started in the background; the jobs table "
                        "shows its result"}

    def _raise_if_busy(self, job: Dict[str, Any]) -> None:
        """Answer 409 up front instead of queueing work that cannot run."""
        action = str((job.get("payload") or {}).get("action") or "flow")
        if action != "flow":
            return
        if self.engine is None or self.flows is None:
            raise AgentError("no automation engine is attached", 409)
        if self.engine.busy():
            raise AgentError("the desktop is busy with another run", 409)

    def _run_job_guarded(self, job: Dict[str, Any]) -> Dict[str, Any]:
        try:
            return self._run_job(job)
        except SchedulerBusy as exc:
            raise AgentError(str(exc), 409)
        except ScriptError as exc:
            raise AgentError(str(exc), getattr(exc, "status", 400))
        except ValueError as exc:
            raise AgentError(str(exc))

    def _run_job_worker(self, job: Dict[str, Any], lock: Any) -> None:
        """Background half of run_job_now: run it, then record the outcome."""
        try:
            try:
                outcome = self._run_job_guarded(job)
            except AgentError as exc:
                outcome = {"status": "failed", "error": str(exc)}
            except Exception as exc:  # noqa: BLE001 - a thread must not die silently
                outcome = {"status": "failed", "error": str(exc)}
            status = str(outcome.get("status") or "done")
            # A manual run must not move the schedule: keep the stored next
            # run, and only clear it for a one-shot job that has now fired.
            moment = None if str(job.get("kind")) == "at" else job.get("next_run_at")
            self.store.record_job_run(job["id"], status,
                                      str(outcome.get("error") or ""), moment)
        finally:
            lock.release()

    def _run_job(self, job: Dict[str, Any]) -> Dict[str, Any]:
        payload = job.get("payload") or {}
        action = str(payload.get("action") or "flow")
        target = str(job.get("target") or "")
        name = str(job.get("name") or target or job["id"])

        if action == "script":
            result = self.scripts.run(target, actor="scheduler")
            status = "done" if result["exitCode"] == 0 else "failed"
            return {"status": status,
                    "error": "" if status == "done" else result["output"][-800:]}

        if action == "prompt":
            prompt = str(payload.get("text") or target)
            reply = self.ask(prompt, actor="scheduler")
            self.store.add_note(reply.get("text", "")[:20000], kind="job-answer",
                                actor="scheduler")
            return {"status": "done", "answer": (reply.get("text") or "")[:2000]}

        if self.engine is None or self.flows is None:
            raise SchedulerBusy("no automation engine is attached")
        if self.engine.busy():
            raise SchedulerBusy("the desktop is busy with another run")
        flow = self.flows.get(target)
        if flow is None:
            raise ValueError("no flow named %r" % target)
        self.engine.start(flow)
        status = self._wait_for_engine(float(job.get("timeout") or 600))
        if status in ("waiting", "running"):
            return {"status": "waiting",
                    "error": "the run is waiting for a human and the job timeout expired"}
        self.notifier.notify_result("اجرای زمان‌بندی‌شده: %s" % name, target, status)
        return {"status": status}

    def _wait_for_engine(self, timeout: float) -> str:
        deadline = self._clock() + timeout
        status = "running"
        while self._clock() < deadline:
            state = self.engine.status()
            status = str(state.get("status") or "running")
            if status not in ("running",):
                return status
            self._sleep(1.0)
        return status

    # -- scripts --------------------------------------------------------
    def script_list(self) -> List[Dict[str, Any]]:
        return self.store.list_scripts()

    def script_save(self, data: Dict[str, Any], script_id: Optional[str] = None,
                    actor: str = "agent") -> Dict[str, Any]:
        if not str(data.get("code") or "").strip():
            raise AgentError("a script needs code")
        language = str(data.get("language") or "python3")
        if language not in ("python3", "bash"):
            raise AgentError("language must be python3 or bash")
        if script_id:
            updated = self.store.update_script(script_id, data, actor=actor)
            if updated is None:
                raise AgentError("no script with id %s" % script_id, 404)
            return updated
        return self.store.create_script(data, actor=actor)

    def script_delete(self, script_id: str, actor: str = "operator") -> Dict[str, Any]:
        if not self.store.delete_script(script_id, actor=actor):
            raise AgentError("no script with id %s" % script_id, 404)
        return {"deleted": script_id}

    def script_decision(self, script_id: str, approve: bool,
                        actor: str = "operator") -> Dict[str, Any]:
        try:
            updated = (self.scripts.approve(script_id, actor=actor) if approve
                       else self.scripts.reject(script_id, actor=actor))
        except ScriptError as exc:
            raise AgentError(str(exc), 404)
        return updated

    def script_run(self, script_id: str, actor: str = "operator") -> Dict[str, Any]:
        self._guard()
        try:
            return self.scripts.run(script_id, actor=actor)
        except ScriptError as exc:
            raise AgentError(str(exc), getattr(exc, "status", 400))

    def script_kill(self, actor: str = "operator") -> Dict[str, Any]:
        return self.scripts.kill_running(actor=actor)

    # -- captcha --------------------------------------------------------
    def captcha_solve(self, shot_name: str = "", context: Optional[Dict[str, Any]] = None,
                      actor: str = "operator") -> Dict[str, Any]:
        self._guard()
        context = dict(context or {})
        path = self.public_shot_path(shot_name)
        if not path:
            # Nothing usable was given: take a fresh screenshot of the desktop.
            if self.backend is None:
                raise AgentError("no screenshot was supplied and no control backend "
                                 "is attached")
            name = "agent-captcha-%d.png" % int(self._clock())
            self.backend.screenshot(name)
            published = self._publish_shot(name)
            path = self.public_shot_path(published or name)
            if not path:
                raise AgentError("could not capture a screenshot to analyse", 500)
            shot_name = published or name
        public_url = ""
        if self.public_base and shot_name:
            public_url = "%s/automation/api/public/shot/%s" % (self.public_base,
                                                               shot_name)
        context.setdefault("publicShot", shot_name)
        context.setdefault("runId", (self.engine.status().get("runId")
                                     if self.engine is not None else "") or "")
        return self.captcha.solve(path, context, public_url)

    def _publish_shot(self, name: str) -> Optional[str]:
        """Copy a captured screenshot into the public dir, like the engine does."""
        source = os.path.join(self.data_dir, "shots", name)
        if not os.path.isfile(source):
            for candidate in (os.path.join(self.data_dir, name), name):
                if os.path.isfile(candidate):
                    source = candidate
                    break
            else:
                return None
        public_dir = os.path.join(self.data_dir, "public")
        os.makedirs(public_dir, exist_ok=True)
        target = os.path.join(public_dir, os.path.basename(source))
        try:
            with open(source, "rb") as handle:
                blob = handle.read()
            with open(target, "wb") as handle:
                handle.write(blob)
        except OSError:
            return None
        return os.path.basename(target)

    def captcha_set_config(self, data: Dict[str, Any],
                           actor: str = "operator") -> Dict[str, Any]:
        """Validate and store the captcha settings.

        Wrapped here rather than called on the solver directly so a refused
        value reaches the operator as a 400 with the reason, not as a 500.
        """
        try:
            return self.captcha.set_config(data, actor=actor)
        except CaptchaError as exc:
            raise AgentError(str(exc))

    def captcha_execute(self, actions: List[Dict[str, Any]],
                        actor: str = "operator") -> Dict[str, Any]:
        """The operator approving a proposal: this is the click that acts."""
        self._guard()
        if self.backend is None:
            raise AgentError("no control backend is attached")
        bounds = _png_size(str((actions[0] or {}).get("_shot", ""))) \
            if actions and isinstance(actions[0], dict) and actions[0].get("_shot") \
            else None
        validated = self.captcha.validate_actions(actions, bounds)
        if validated["rejected"]:
            raise AgentError("some actions were refused: %s"
                             % "; ".join(str(item.get("reason"))
                                         for item in validated["rejected"]))
        try:
            result = self.captcha.execute(validated["actions"])
        except CaptchaError as exc:
            raise AgentError(str(exc), 409)
        self.store.audit(actor, "captcha.executed",
                         "%d action(s)" % len(result["executed"]))
        return result

    # -- the agent's browser --------------------------------------------
    def ai_status(self) -> Dict[str, Any]:
        if self.ai_browser is None:
            return {"available": False, "reason": "this deployment has no agent browser"}
        try:
            return self.ai_browser.status(self.llm.chat_provider())
        except AiBrowserError as exc:
            return {"available": False, "error": str(exc)}

    def ai_open(self) -> Dict[str, Any]:
        if self.ai_browser is None:
            raise AgentError("this deployment has no agent browser")
        try:
            return self.ai_browser.open_provider(self.llm.chat_provider())
        except AiBrowserError as exc:
            raise AgentError(str(exc), 502)

    def ai_screenshot(self) -> str:
        """PNG bytes of the agent's own display, so a one-time login is possible."""
        if self.ai_browser is None:
            raise AgentError("this deployment has no agent browser")
        path = os.path.join(self.data_dir, "public",
                            "agent-browser-%d.png" % int(self._clock()))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            self.ai_browser.screenshot(path)
        except AiBrowserError as exc:
            raise AgentError(str(exc), 502)
        return path

    # -- notes ----------------------------------------------------------
    def add_note(self, body: str, kind: str = "note",
                 actor: str = "operator") -> Dict[str, Any]:
        if not body.strip():
            raise AgentError("a note needs a body")
        return self.store.add_note(body, kind=kind, actor=actor)

    def notes(self, kind: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
        return self.store.list_notes(kind=kind, limit=limit)

    def delete_note(self, note_id: str, actor: str = "operator") -> Dict[str, Any]:
        if not self.store.delete_note(note_id, actor=actor):
            raise AgentError("no note with id %s" % note_id, 404)
        return {"deleted": note_id}

    def audit(self, limit: int = 200) -> List[Dict[str, Any]]:
        return self.store.list_audit(limit=limit)


def _png_size(path: str) -> Optional[Dict[str, int]]:
    """Width and height from a PNG header, without any image library."""
    try:
        with open(path, "rb") as handle:
            header = handle.read(26)
    except OSError:
        return None
    if len(header) < 24 or not header.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    return {"width": int.from_bytes(header[16:20], "big"),
            "height": int.from_bytes(header[20:24], "big")}
