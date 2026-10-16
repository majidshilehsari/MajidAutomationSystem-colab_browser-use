"""The awake brain: a supervisor that never lets the desktop stand idle.

The operator's definition of the brain is duty-based, not a free-wheeling LLM:

* the system must never be left idle - when a run ends, stops or errors, the
  brain immediately looks for the next piece of work;
* work that is always allowed (operations with ``permission='auto'``) is run
  by the brain itself, round-robin: the one resting longest goes first;
* everything else (``permission='ask'``) becomes a *suggestion*: the brain
  asks in the panel and on Telegram, waits a bounded time for the operator,
  and falls back to the always-allowed work when nobody answers;
* logging is mandatory: the engine asks the brain for the logging policy on
  every start (before/after screenshots per step), and old screenshots are
  pruned on a cycle while text reports are kept forever.

The variable half of thinking (error protocols, LLM consultation through the
coworker browser, the feedback ledger) grows on top of this in later phases;
this module is the deterministic core they can rely on.
"""

from __future__ import annotations

import os
import shutil
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

#: The policy defaults. Every one lives in the ordinary settings table, so
#: the panel edits them and Supabase snapshots carry them between machines.
DEFAULTS: Dict[str, Any] = {
    "brain.enabled": False,
    "brain.notify": True,
    "brain.runGapSec": 120,
    "brain.opCooldownMin": 10,
    "brain.suggestTtlMin": 15,
    "brain.askCooldownMin": 30,
    "brain.reportGapSec": 300,
    "log.forceStepShots": True,
    "log.shotKeepDays": 7,
    "log.errShotKeepDays": 30,
}

#: How often the screenshot pruning pass may run at most.
PRUNE_EVERY_SEC = 6 * 3600


class Brain:
    """The duty-bound supervisor thread, mirroring the Scheduler's shape."""

    def __init__(self, hub: Any, store: Any = None, shots: Any = None,
                 clock: Any = time.time, sleep: Any = time.sleep,
                 interval: float = 5.0) -> None:
        self._hub = hub
        self._store = store if store is not None else getattr(hub, "store", None)
        self._shots = shots
        self._clock = clock
        self._sleep = sleep
        self._interval = float(interval)
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._suggestion: Optional[Dict[str, Any]] = None
        self._next_allowed_at = 0.0
        self._last_ask_at = 0.0
        self._last_busy = False
        self._last_prune = 0.0
        self._last_heartbeat = 0.0

    # -- lifecycle ------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="agent-brain",
                                        daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)
        self._thread = None

    def alive(self) -> bool:
        return bool(self._thread is not None and self._thread.is_alive())

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:   # a crashing brain must not kill the server
                self._say("خطای داخلی مغز: %s" % exc)
                self._report("خطا", "چرخهٔ فکر با خطا مواجه شد: %s" % exc, ok=False)
            self._sleep(self._interval)

    # -- settings -------------------------------------------------------
    def setting(self, key: str) -> Any:
        value = self._store.get_setting(key, DEFAULTS.get(key))
        return DEFAULTS[key] if value is None else value

    # -- the thinking cycle ----------------------------------------------
    def tick(self) -> None:
        now = self._clock()
        self._maybe_prune(now)
        if not bool(self.setting("brain.enabled")):
            return
        engine = getattr(self._hub, "engine", None)
        busy = bool(engine is not None and engine.busy())
        self._heartbeat(now, busy)
        if self._last_busy and not busy:
            self._run_just_ended(engine)
        self._last_busy = busy
        if busy:
            return
        if now < self._next_allowed_at:
            return
        with self._lock:
            suggestion = self._suggestion
        if suggestion is not None:
            if now >= float(suggestion.get("expiresAt") or 0):
                line = ("پیشنهاد «%s» بی‌پاسخ ماند؛ به عملیات‌های همیشه‌مجاز "
                        "برمی‌گردم." % suggestion.get("name"))
                self._say(line)
                self._speak(line)
                self._report("پیشنهاد", line)
                with self._lock:
                    self._suggestion = None
            else:
                return   # an open question owns the desk
        operation = self._next_auto(now)
        if operation is not None:
            self._execute(operation, now)
            return
        candidate = self._next_ask(now)
        if candidate is not None:
            self._suggest(candidate, now)

    def _run_just_ended(self, engine: Any) -> None:
        latest: Dict[str, Any] = {}
        try:
            history = (engine.status() or {}).get("history") or []
            if history:
                latest = history[-1]
        except Exception:
            latest = {}
        status = str(latest.get("status") or "done")
        run_id = str(latest.get("runId") or "?")
        line = "اجرای %s تمام شد (وضعیت: %s)." % (run_id, status)
        self._say(line)
        self._speak(line)
        self._report("پایان اجرا", line, ok=status in ("done", "stopped"))
        if bool(self.setting("brain.notify")):
            self._tell(line)
        self._next_allowed_at = self._clock() + float(self.setting("brain.runGapSec"))

    def on_flow_error(self, name: str, error: str) -> None:
        """Round 27, item 8: a run ended in error - the brain decides next.

        The failure is recorded with its exact timestamp (reports, archive
        and the brain conversation), and the brain's own waiting time is
        dropped so the very next thinking cycle looks at the situation and
        picks the next move from the always-allowed operations.
        """
        line = ("اجرای «%s» با خطا تمام شد: %s — تصمیم دربارهٔ اقدام بعدی با "
                "مغز است؛ چرخهٔ فکر بلافاصله بیدار شد."
                % (name or "بی‌نام", error or "بدون پیام"))
        self._say(line)
        self._speak(line)
        self._report("تصمیم در خطا", line, ok=False)
        self._next_allowed_at = 0.0

    def _operations(self) -> List[Dict[str, Any]]:
        library = getattr(self._hub, "library", None)
        if library is None:
            return []
        try:
            return list(library.list())
        except Exception:
            return []

    def _next_auto(self, now: float) -> Optional[Dict[str, Any]]:
        cooldown = float(self.setting("brain.opCooldownMin")) * 60.0
        ready = [op for op in self._operations()
                 if str(op.get("permission") or "ask") == "auto"
                 and (not op.get("last_run_at")
                      or float(op["last_run_at"]) + cooldown <= now)]
        if not ready:
            return None
        ready.sort(key=lambda op: (float(op.get("last_run_at") or 0.0),
                                   str(op.get("name") or "")))
        return ready[0]

    def _next_ask(self, now: float) -> Optional[Dict[str, Any]]:
        ask_cooldown = float(self.setting("brain.askCooldownMin")) * 60.0
        if self._last_ask_at and now - self._last_ask_at < ask_cooldown:
            return None
        candidates = [op for op in self._operations()
                      if str(op.get("permission") or "ask") != "auto"]
        if not candidates:
            return None
        candidates.sort(key=lambda op: (float(op.get("last_run_at") or 0.0),
                                        str(op.get("name") or "")))
        return candidates[0]

    def _execute(self, operation: Dict[str, Any], now: float) -> None:
        name = str(operation.get("name") or operation.get("id"))
        self._report("اجرا", "عملیات «%s» را برای اجرا به صف سپردم." % name)
        try:
            self._hub.operation_run(str(operation.get("id")), actor="brain")
        except Exception as exc:
            self._say("اجرای «%s» ناموفق بود: %s" % (name, exc))
            self._speak("خواستم «%s» را اجرا کنم اما نشد: %s" % (name, exc))
            self._report("اجرا", "اجرای «%s» ناموفق بود: %s" % (name, exc),
                         ok=False)
            self._next_allowed_at = now + max(
                30.0, float(self.setting("brain.runGapSec")))
            return
        self._say("مغز «%s» را اجرا کرد." % name)
        self._speak("«%s» را اجرا کردم." % name)
        self._report("اجرا", "عملیات «%s» اجرا شد." % name)
        self._next_allowed_at = now + float(self.setting("brain.runGapSec"))

    def _suggest(self, operation: Dict[str, Any], now: float) -> None:
        ttl = float(self.setting("brain.suggestTtlMin")) * 60.0
        suggestion = {"id": uuid.uuid4().hex[:10],
                      "operationId": str(operation.get("id")),
                      "name": str(operation.get("name") or operation.get("id")),
                      "createdAt": now, "expiresAt": now + ttl}
        with self._lock:
            self._suggestion = suggestion
        self._last_ask_at = now
        line = ("پیشنهاد: «%s» (تا %d دقیقه منتظر تصمیم می‌ماند)."
                % (suggestion["name"], max(1, int(ttl // 60))))
        self._say(line)
        self._speak(line + " در تب مغز «اجرا کن» یا «رد کن» را بزن.")
        self._report("پیشنهاد", line)
        if bool(self.setting("brain.notify")):
            self._tell("پیشنهاد مغز: «%s» — در پنل تأیید یا رد کنید."
                       % suggestion["name"])

    def decide(self, suggestion_id: str, action: str) -> Dict[str, Any]:
        with self._lock:
            suggestion = self._suggestion
        if suggestion is None or str(suggestion.get("id")) != str(suggestion_id):
            raise ValueError("این پیشنهاد دیگر معتبر نیست (شاید قبلاً تصمیم "
                             "گرفته شده).")
        if action not in ("run", "reject"):
            raise ValueError("action must be run or reject")
        with self._lock:
            self._suggestion = None
        now = self._clock()
        if action == "reject":
            self._last_ask_at = now
            self._say("پیشنهاد «%s» رد شد." % suggestion.get("name"))
            self._report("تصمیم", "کاربر پیشنهاد «%s» را رد کرد؛ به سراغ "
                         "عملیات‌های همیشه‌مجاز می‌روم." % suggestion.get("name"))
            return {"decision": "reject", "name": suggestion.get("name")}
        operation = None
        for candidate in self._operations():
            if str(candidate.get("id")) == str(suggestion.get("operationId")):
                operation = candidate
                break
        if operation is None:
            raise ValueError("عملیاتِ پیشنهادشده دیگر وجود ندارد.")
        self._execute(operation, now)
        return {"decision": "run", "name": suggestion.get("name")}

    # -- the mandatory logging policy -------------------------------------
    def enforce_logging(self, flow: Dict[str, Any]) -> Dict[str, Any]:
        """The global logging policy: before/after shots on every step.

        The engine asks this at every start, so a flow can no longer keep the
        desk silent; the text side of the log is permanent and only images
        are pruned (see _maybe_prune).
        """
        if not bool(self.setting("log.forceStepShots")):
            return flow
        updated = dict(flow or {})
        settings = dict(updated.get("settings") or {})
        settings["screenshotBeforeEachStep"] = True
        settings["screenshotAfterEachStep"] = True
        settings.setdefault("screenshotOnError", True)
        updated["settings"] = settings
        return updated

    def _maybe_prune(self, now: float) -> None:
        if self._last_prune and now - self._last_prune < PRUNE_EVERY_SEC:
            return
        self._last_prune = now
        keep = float(self.setting("log.shotKeepDays")) * 86400.0
        err_keep = float(self.setting("log.errShotKeepDays")) * 86400.0
        data_dir = str(getattr(self._hub, "data_dir", "") or "")
        removed = 0
        if self._shots is not None and data_dir:
            try:
                rows = self._shots.list()
            except Exception:
                rows = []
            public_dir = os.path.join(data_dir, "public")
            for row in rows:
                horizon = err_keep if str(row.get("role") or "") == "error" else keep
                try:
                    created = float(row.get("createdAt") or now)
                except (TypeError, ValueError):
                    created = now
                if now - created <= horizon:
                    continue
                try:
                    if self._shots.delete(str(row.get("id"))):
                        removed += 1
                except Exception:
                    continue
                name = str(row.get("name") or "")
                if name:
                    try:
                        os.unlink(os.path.join(public_dir, name))
                    except OSError:
                        pass
        # Whole run folders disappear after the longer (error) horizon; the
        # text reports (the texts archive and the database) are never touched.
        runs_dir = os.path.join(data_dir, "runs") if data_dir else ""
        if runs_dir:
            try:
                entries = os.listdir(runs_dir)
            except OSError:
                entries = []
            for entry in entries:
                path = os.path.join(runs_dir, entry)
                try:
                    if os.path.isdir(path) and now - os.path.getmtime(path) > err_keep:
                        shutil.rmtree(path, ignore_errors=True)
                        removed += 1
                except OSError:
                    continue
        if removed:
            self._say("هرزسازی گزارش‌ها: %d مورد قدیمی پاک شد (متن‌ها همیشگی‌اند)."
                      % removed)
            self._report("پاک‌سازی", "%d تصویر/پوشهٔ قدیمی پاک شد؛ متن‌ها و "
                         "گزارش‌ها همیشگی‌اند." % removed)

    # -- the panel surface --------------------------------------------------
    def status(self) -> Dict[str, Any]:
        now = self._clock()
        with self._lock:
            suggestion = dict(self._suggestion) if self._suggestion else None
        if suggestion is not None and now >= float(suggestion.get("expiresAt") or 0):
            suggestion["expired"] = True
        engine = getattr(self._hub, "engine", None)
        try:
            busy = bool(engine is not None and engine.busy())
        except Exception:
            busy = False
        operations = self._operations()
        try:
            entries = self._hub.devlog_list(60).get("entries") or []
        except Exception:
            entries = []
        brain_log = [row for row in entries if row.get("actor") == "brain"][:15]
        try:
            messages = self._store.list_brain_msgs(60)
        except Exception:
            messages = []
        try:
            memory = self._store.list_brain_memory(10)
        except Exception:
            memory = []
        try:
            steps = self._store.list_step_reports("brain", 30)
        except Exception:
            steps = []
        return {
            "messages": messages,
            "memory": memory,
            "steps": steps,
            "enabled": bool(self.setting("brain.enabled")),
            "alive": self.alive(),
            "busy": busy,
            "suggestion": suggestion,
            "nextAllowedAt": self._next_allowed_at,
            "policy": {
                "runGapSec": int(self.setting("brain.runGapSec")),
                "opCooldownMin": int(self.setting("brain.opCooldownMin")),
                "suggestTtlMin": int(self.setting("brain.suggestTtlMin")),
                "reportGapSec": int(self.setting("brain.reportGapSec")),
                "notify": bool(self.setting("brain.notify")),
                "forceStepShots": bool(self.setting("log.forceStepShots")),
                "shotKeepDays": int(self.setting("log.shotKeepDays")),
                "errShotKeepDays": int(self.setting("log.errShotKeepDays")),
            },
            "autoOps": [{"id": op.get("id"), "name": op.get("name"),
                         "permission": "auto",
                         "last_run_at": op.get("last_run_at"),
                         "last_status": op.get("last_status")}
                        for op in operations
                        if str(op.get("permission") or "ask") == "auto"],
            "askOps": [{"id": op.get("id"), "name": op.get("name"),
                        "permission": "ask",
                        "last_run_at": op.get("last_run_at")}
                       for op in operations
                       if str(op.get("permission") or "ask") != "auto"],
            "log": brain_log,
        }

    # -- the conversation with the operator ---------------------------------
    HELP_TEXT = (
        "من مغزم — سرپرست بیدار. کارهایی که از دستم برمی‌آید:\n"
        "• «اجرا: <نام عملیات>» → همیشه‌مجازها را فوراً اجرا می‌کنم، بقیه را "
        "پیشنهاد می‌دهم.\n"
        "• «توقف» → اجرای جاری را در اولین نقطهٔ امن متوقف می‌کنم.\n"
        "• «یادت باشه <متن>» → در حافظهٔ مغز ثبت می‌کنم.\n"
        "• «حافظه» → یادداشت‌هایم را می‌خوانم.\n"
        "• «وضعیت» → وضعیت میز و مغز را می‌گویم.\n"
        "• «کمک» → همین راهنما.\n"
        "هر چیز دیگری بنویسی، به‌عنوان یادداشت در حافظه‌ام می‌ماند.")

    def respond(self, text: str, actor: str = "operator") -> Dict[str, Any]:
        """One inbound message from the operator, answered by the rulebook.

        The brain is honest about what it is: a duty-bound supervisor. What it
        does not understand becomes a memory note - the personal memory the
        operator asked for - instead of a made-up answer.
        """
        text = str(text or "").strip()
        if not text:
            raise ValueError("the message must not be empty")
        self._store.add_brain_msg("in", text, actor=actor)
        reply = self._reply(text)
        self._store.add_brain_msg("out", reply)
        self._say("پیام کاربر: %s" % text[:120])
        self._report("گفت‌وگو", "پیام کاربر «%s» را پاسخ دادم." % text[:120])
        return {"reply": reply}

    def _reply(self, text: str) -> str:
        low = text.lower()
        if low in ("/help", "help", "کمک", "راهنما"):
            return self.HELP_TEXT
        if any(word in low for word in ("توقف", "stop", "/stop")):
            engine = getattr(self._hub, "engine", None)
            stop = getattr(engine, "stop", None)
            busy = getattr(engine, "busy", None)
            if stop is None or busy is None:
                return "موتوری وصل نیست که متوقفش کنم."
            try:
                if not busy():
                    return "الان اجرایی در جریان نیست."
                stop()
                return "درخواست توقف زدم؛ اجرا در اولین نقطهٔ امن می‌ایستد."
            except Exception as exc:
                return "توقف ناموفق بود: %s" % exc
        if low.startswith(("یادت باشه", "به خاطر بسپار", "remember", "/remember")):
            note = text
            for prefix in ("یادت باشه", "به خاطر بسپار", "remember", "/remember"):
                if text.lower().startswith(prefix):
                    note = text[len(prefix):].strip(" :،-")
                    break
            note = note or text
            self._store.add_brain_memory(note)
            return "به حافظهٔ مغز سپرده شد: «%s»" % note[:200]
        if any(word in low for word in ("حافظه", "چه یادته", "memory")):
            rows = self._store.list_brain_memory(8)
            if not rows:
                return "حافظهٔ مغز هنوز خالی است."
            return "یادداشت‌های مغز:\n" + "\n".join(
                "• %s" % str(row.get("text"))[:200] for row in rows)
        if low in ("وضعیت", "status", "/status"):
            snapshot = self.status()
            suggestion = snapshot.get("suggestion")
            return ("مغز %s است؛ میز %s؛ %s"
                    % ("روشن" if snapshot.get("enabled") else "خاموش",
                       "درگیر اجراست" if snapshot.get("busy") else "آزاد است",
                       ("پیشنهاد جاری: «%s»" % suggestion.get("name"))
                       if suggestion else "پیشنهادی در انتظار نیست."))
        if "اجرا" in low or low.startswith("/run"):
            match = self._match_operation(text)
            if match is not None:
                if str(match.get("permission") or "ask") == "auto":
                    self._execute(match, self._clock())
                    return "«%s» همیشه‌مجاز است؛ اجرا را شروع کردم." % match.get("name")
                self._suggest(match, self._clock())
                return ("«%s» با پرسش است؛ پیشنهادش را روی کارت گذاشتم — "
                        "«اجرا کن» یا «رد کن»." % match.get("name"))
            return "عملیاتی با این نام پیدا نکردم؛ فهرست کامل در تب کتابخانه است."
        # Anything else is a note for the brain's own memory.
        self._store.add_brain_memory(text[:500])
        return ("دریافت شد و به حافظهٔ مغز رفت. برای کار: «اجرا: <نام عملیات>»، "
                "«توقف»، «یادت باشه …»، «حافظه»، «وضعیت» یا «کمک».")

    def _match_operation(self, text: str) -> Optional[Dict[str, Any]]:
        """The first operation whose id or name appears in the message."""
        low = text.lower()
        for op in self._operations():
            op_id = str(op.get("id") or "")
            if op_id and op_id in text:
                return op
        for op in self._operations():
            name = str(op.get("name") or "")
            if name and (name in text or name.lower() in low):
                return op
        return None

    # -- the continuous step report -----------------------------------------
    def _report(self, phase: str, message: str, ok: bool = True) -> None:
        """One line of the brain's step report (round 27, item 4).

        A precise, timestamped stream of everything the brain does; the
        operator reads it in the brain tab and the coworker agent can read it
        later through GET /agent/reports?kind=brain, so it always understands
        what happened while it was away.
        """
        try:
            self._store.add_step_report("brain", phase, message, ok=ok)
        except Exception:
            pass   # reporting must never break the thinking cycle

    def _heartbeat(self, now: float, busy: bool) -> None:
        """The observation cycle, reported continuously but throttled: at
        most one line per brain.reportGapSec seconds, so the stream stays
        readable instead of flooding the database every five seconds."""
        gap = max(30.0, float(self.setting("brain.reportGapSec")))
        if now - self._last_heartbeat < gap:
            return
        self._last_heartbeat = now
        with self._lock:
            suggestion = self._suggestion
        desk = "میز مشغول اجراست" if busy else "میز آزاد است"
        waiting = ("؛ پیشنهاد «%s» منتظر تصمیم انسانی است"
                   % suggestion.get("name")) if suggestion else ""
        self._report("مشاهده", "چرخهٔ مشاهده: %s%s. در نبودِ کارِ واجب، آمادهٔ "
                     "انتخاب عملیات بعدی می‌مانم." % (desk, waiting))

    # -- the voice ------------------------------------------------------------
    def _speak(self, text: str) -> None:
        """One line in the brain's conversation with the operator."""
        try:
            self._store.add_brain_msg("out", text)
        except Exception:
            pass

    def _say(self, title: str, body: str = "") -> None:
        try:
            self._store.add_devlog(title, body, actor="brain")
        except Exception:
            pass

    def _tell(self, text: str) -> None:
        try:
            self._hub.telegram_send(text, purpose="brain")
        except Exception:
            pass   # Telegram being off must never gag the brain locally
