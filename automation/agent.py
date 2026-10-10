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

import json
import os
import re
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

from automation import schema
from automation.agent_store import AgentStore, SCRIPT_APPROVED
from automation import browser_info
from automation.ai_browser import AiBrowser, AiBrowserError
from automation.backup import BackupError, BackupManager
from automation.captcha import CaptchaError, CaptchaSolver
from automation.llm import (LlmClient, LlmError, PROVIDER_BROWSER,
                            extract_json)
from automation.operations import OperationError, OperationLibrary
from automation.scheduler import Scheduler, SchedulerBusy, next_run
from automation.scripts import ScriptError, ScriptRunner
from automation.telegram_client import (CHANNELS, MODE_ACCOUNT, MODE_BOT, MODE_OFF,
                                        MODES, Notifier, PURPOSES, TelegramError)


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
    # The visible pointer: a big yellow cursor and a ripple on every click.
    "cursor.enabled": bool,
    "cursor.size": int,
    "cursor.color": str,
    "cursor.outline": str,
    "cursor.ripple": bool,
    # Which transport each kind of message uses; "" follows the global mode.
    "telegram.channel.handoff": str,
    "telegram.channel.captcha": str,
    "telegram.channel.jobs": str,
    "telegram.channel.manual": str,
}

SECRET_SETTINGS = {
    "ai.apiKey", "telegram.botToken", "telegram.apiId", "telegram.apiHash",
}

# A SELECT, and only a SELECT: one statement, no trailing semicolon trickery.
_READ_ONLY_SQL = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)
_FORBIDDEN_SQL = re.compile(
    r"\b(insert|update|delete|drop|alter|create|attach|pragma|vacuum|replace)\b",
    re.IGNORECASE)

#: What the agent key opens, in the order an outsider should read it: the
#: reference first, then the state, then the things that act. The sidebar turns
#: this into a copyable list, so nobody has to guess a path - and a 404 stops
#: being a puzzle.
# 3, 6 or 8 hex digits after the hash: what a CSS colour, and an <input
# type="color">, can actually produce. Length alone would let "#gggggg" through
# and the pointer would silently stay the browser default.
_HEX_COLOR_RE = re.compile(
    r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")

API_INDEX = [
    ("GET", "/agent", "📇 نقشهٔ کامل: وضعیت ایجنت، تنظیمات ماسک‌شده، شمارش‌ها و "
                      "همین فهرست مسیرها.",
     "The full map: agent status, masked settings, counts and this index."),
    ("GET", "/agent/chat/messages", "💬 تاریخچهٔ گفت‌وگو (تب چت) با وضعیت هر پیام.",
     "Chat history with each message's status."),
    ("POST", "/agent/chat/send", "💬 فرستادن پیام به چت؛ جواب در پس‌زمینه می‌آید و "
                                 "با polling خوانده می‌شود.",
     "Send a chat message; the answer arrives in the background."),
    ("GET", "/agent/operations", "📚 کتابخانهٔ عملیات: هر عملیات و مراحلش.",
     "The operations library: every operation and its steps."),
    ("POST", "/agent/operations", "📚 ساخت یا ویرایش یک عملیات (مراحل با schema "
                                  "اعتبارسنجی می‌شوند).",
     "Create or edit an operation; steps are schema-validated."),
    ("POST", "/agent/operation-run", "📚 اجرای یک عملیات، همین حالا.",
     "Run an operation right now."),
    ("GET", "/status", "🖥 وضعیت اجرای فعلی اتوماسیون (قدم چندم از چند، منتظر "
                       "انسان یا نه).",
     "Current automation run state."),
    ("GET", "/flows", "🧩 فهرست جریان‌های ذخیره‌شده.", "Saved flows."),
    ("PUT", "/flows/<name>", "🧩 ذخیرهٔ یک جریان (با اعتبارسنجی کامل schema).",
     "Save a flow, fully validated."),
    ("POST", "/run", "▶ شروع یک اجرا از روی جریان داده‌شده.",
     "Start a run from the supplied flow."),
    ("POST", "/control", "🎮 pause / resume / stop / confirm و کلیک دستی.",
     "pause / resume / stop / confirm and manual clicks."),
    ("POST", "/detect", "🔍 شناسایی صفحهٔ فعال: عنوان، URL، عناصر با مختصات "
                        "دسکتاپ و اسکرین‌شات عمومی.",
     "Detect the active page: title, URL, elements with desktop coordinates."),
    ("GET", "/pages", "🖼 صفحه‌های شناسایی‌شدهٔ قبلی.", "Previously detected pages."),
    ("GET", "/shots", "📷 همهٔ اسکرین‌شات‌ها با نقش و لینک عمومی.",
     "Every screenshot, with its role and public link."),
    ("GET", "/texts", "📄 متن‌های استخراج‌شده از صفحه‌ها.", "Extracted page texts."),
    ("GET", "/agent/key", "🔑 وضعیت کلید ایجنت (فعال/قطع، زمان ساخت) بدون خودِ کلید.",
     "Agent key status (on/off, created at) without the key itself."),
    ("POST", "/agent/key/enable", "⏸ قطع یا وصل کردن دسترسی ایجنت، آنی و بدون deploy.",
     "Cut or restore agent access, instantly, no redeploy."),
    ("POST", "/agent/settings", "⚙️ تغییر تنظیمات.", "Change settings."),
    ("GET", "/agent/jobs", "⏰ وظایف زمان‌بندی‌شده و آخرین نتیجهٔ هر کدام.",
     "Scheduled jobs and each one's last result."),
    ("POST", "/agent/jobs", "⏰ ساخت وظیفهٔ تازه (at / every / cron).",
     "Create a job (at / every / cron)."),
    ("POST", "/agent/job-run", "⏰ اجرای فوری یک وظیفه (در پس‌زمینه).",
     "Run a job now, in the background."),
    ("GET", "/agent/scripts", "📜 اسکریپت‌های ایجنت و وضعیت تأیید هر کدام.",
     "The agent's scripts and each approval state."),
    ("POST", "/agent/script-decision", "✅ تأیید یا رد یک اسکریپت؛ بدون تأیید اجرا "
                                       "نمی‌شود.",
     "Approve or reject a script; unapproved code never runs."),
    ("POST", "/agent/script-run", "📜 اجرای یک اسکریپت **تأییدشده**.",
     "Run an approved script."),
    ("POST", "/agent/captcha/config", "🧩 تغییر تنظیمات زنجیرهٔ حل کپچا.",
     "Change the captcha chain settings."),
    ("GET", "/agent/captcha/config", "🧩 تنظیمات زنجیرهٔ حل کپچا و وضعیت افزونه.",
     "Captcha chain settings and extension status."),
    ("POST", "/agent/captcha/solve", "🧩 اسکرین‌شات + اجرای زنجیرهٔ حل؛ خروجی یک "
                                     "پیشنهاد است.",
     "Screenshot + run the solve chain; the outcome is a proposal."),
    ("POST", "/agent/captcha/execute", "🧩 اجرای اقداماتی که تأیید کرده‌اید.",
     "Perform actions you approved."),
    ("GET", "/agent/telegram/status", "🤖 وضعیت ربات و حساب: چه چیزی تنظیم شده، "
                                      "جلسه هست یا نه.",
     "Bot and account status: what is configured, whether a session exists."),
    ("GET", "/agent/telegram/targets", "🤖 کشف گفتگوها (شخص/گروه) از getUpdates.",
     "Discover chats (person/group) from getUpdates."),
    ("POST", "/agent/telegram/test", "🤖 پیام آزمایشی — باید یک target داشته باشد.",
     "A test message - it must name a target."),
    ("POST", "/agent/telegram/send", "🤖 فرستادن پیام دلخواه به یک target با کانال "
                                     "دلخواه (bot/account).",
     "Send any message to a target over a chosen channel."),
    ("GET", "/agent/ai/status", "🧠 مرورگر ایجنت: در دسترس است، روی چه صفحه‌ای.",
     "The agent browser: reachable, on which page."),
    ("POST", "/agent/ai/open", "🧠 باز کردن سایت چت در مرورگر ایجنت.",
     "Open the chat site in the agent browser."),
    ("GET", "/agent/ai/shot", "🧠 اسکرین‌شات نمایش ایجنت (برای لاگین دستی).",
     "A screenshot of the agent display, for the one-time login."),
    ("GET", "/agent/notes", "🗒 یادداشت‌ها و پاسخ‌های ثبت‌شده.", "Notes and saved answers."),
    ("GET", "/agent/audit", "🧾 لاگ audit: چه کسی، چه چیزی، کی.",
     "The audit log: who did what, when."),
    ("POST", "/agent/query", "🗄 کوئری SQL **فقط‌خواندنی** روی دیتابیس ایجنت.",
     "A read-only SQL query against the agent database."),
    ("GET", "/agent/api-index", "📇 همین فهرست مسیرها، به‌صورت JSON با آدرس کامل.",
     "This very index, as JSON with absolute URLs."),
    ("POST", "/agent/chat", "💬 پرسش مستقیم از ایجنت با زمینهٔ اتوماسیون؛ جواب "
                            "همزمان برمی‌گردد (برخلاف chat/send).",
     "Ask the agent directly with automation context; answers synchronously."),
    ("POST", "/agent/chat/apply-flow", "💬 ذخیرهٔ جریانی که مدل داخل پاسخ چت "
                                       "پیشنهاد داده است.",
     "Save a flow the model proposed inside a chat answer."),
    ("POST", "/agent/chat/clear", "💬 پاک کردن تاریخچهٔ گفت‌وگو.",
     "Clear the conversation history."),
    ("POST", "/agent/key/reveal", "🔑 نمایش خودِ کلید — این کار در audit ثبت می‌شود.",
     "Reveal the key itself; this is written to the audit log."),
    ("POST", "/agent/key/rotate", "🔑 ساخت کلید تازه؛ کلید قبلی همان لحظه از کار "
                                  "می‌افتد.",
     "Generate a new key; the old one dies at that moment."),
    ("POST", "/agent/kill-switch", "⏹ کلید قطع اضطراری: تا خاموش شدنش هیچ اجرا، "
                                   "اسکریپت یا عملیاتی شروع نمی‌شود.",
     "The emergency kill switch: nothing runs while it is on."),
    ("GET", "/agent/cursor", "🖱 تنظیمات نشانگر: اندازه، رنگ، لبه و موج کلیک.",
     "Pointer settings: size, colour, outline and the click ripple."),
    ("POST", "/agent/cursor", "🖱 تغییر نشانگر و اعمال فوری‌اش روی مرورگر.",
     "Change the pointer and apply it to the browser immediately."),
    ("GET", "/agent/captcha/history", "🧩 تاریخچهٔ تلاش‌های کپچا و نتیجهٔ هر کدام.",
     "The captcha attempts and what each one concluded."),
    ("PUT", "/agent/jobs/<id>", "⏰ ویرایش یک وظیفهٔ زمان‌بندی‌شده.",
     "Edit a scheduled job."),
    ("DELETE", "/agent/jobs/<id>", "⏰ حذف یک وظیفهٔ زمان‌بندی‌شده.",
     "Delete a scheduled job."),
    ("POST", "/agent/notes", "🗒 ثبت یک یادداشت یا پاسخ مدل.",
     "Store a note or a model answer."),
    ("DELETE", "/agent/notes/<id>", "🗒 حذف یک یادداشت.", "Delete a note."),
    ("POST", "/agent/scripts", "📜 ثبت یک اسکریپت تازه (وضعیتش pending می‌ماند).",
     "Store a new script; it starts out pending."),
    ("PUT", "/agent/scripts/<id>", "📜 ویرایش اسکریپت؛ تأیید قبلی باطل می‌شود.",
     "Edit a script; any earlier approval is voided."),
    ("DELETE", "/agent/scripts/<id>", "📜 حذف یک اسکریپت.", "Delete a script."),
    ("POST", "/agent/script-kill", "📜 کشتن اسکریپت در حال اجرا، همین لحظه.",
     "Kill the running script right now."),
    ("PUT", "/agent/operations/<id>", "📚 ویرایش یک عملیات موجود.",
     "Edit an existing operation."),
    ("DELETE", "/agent/operations/<id>", "📚 حذف یک عملیات (عملیات‌های آماده با "
                                         "restart برمی‌گردند).",
     "Delete an operation; built-ins come back on restart."),
    ("POST", "/agent/operation-reset", "📚 برگرداندن یک عملیات آماده به نسخهٔ "
                                       "کارخانه.",
     "Reset a built-in operation to its factory version."),
    ("POST", "/agent/telegram/login", "🤖 شروع لاگین اکانت شخصی: ارسال کد به شماره.",
     "Start the personal-account login: send the code to the phone."),
    ("POST", "/agent/telegram/login-finish", "🤖 پایان لاگین با کد دریافتی؛ جلسه "
                                             "روی /data ذخیره می‌شود.",
     "Finish the login with the code; the session is stored on /data."),
    ("GET", "/info", "ℹ️ آیا توکن لازم است، اندازهٔ صفحه، و اینکه موتور آزاد است.",
     "Whether a token is needed, the viewport, and whether the engine is idle."),
    ("GET", "/guide", "📖 راهنمای پرامپت‌نویسی برای مدل (متن ساده).",
     "The prompt-writing guide, as plain text."),
    ("POST", "/prompt", "📖 ساخت پرامپت آماده برای یک مدل بیرونی، با لینک عمومی "
                        "اسکرین‌شات.",
     "Build a prompt for an outside model, public screenshot link included."),
    ("GET", "/report", "🧾 گزارش اجرای آخر به‌صورت متن.", "The last run report as text."),
    ("GET", "/runs", "🧾 فهرست اجراهای گذشته.", "Past runs."),
    ("GET", "/runs/<id>/log", "🧾 لاگ کامل یک اجرا.", "The full log of one run."),
    ("POST", "/screenshot", "📷 اسکرین‌شات دستی از دسکتاپ.", "A manual desktop screenshot."),
    ("POST", "/texts", "📄 ثبت دستی یک متن استخراج‌شده.", "Store an extracted text by hand."),
    ("GET", "/artifact", "📦 خواندن یک فایل داخلی (اسکرین‌شات) با مسیر نسبی.",
     "Read one internal file (a screenshot) by relative path."),
    ("GET", "/pages/<id>", "🖼 جزئیات یک صفحهٔ شناسایی‌شده با عناصرش.",
     "One detected page, elements included."),
    ("GET", "/flows/<name>", "🧩 خواندن یک جریان ذخیره‌شده.", "Read one saved flow."),
    ("DELETE", "/flows/<name>", "🧩 حذف یک جریان ذخیره‌شده.", "Delete one saved flow."),
    ("DELETE", "/shots/<id>", "📷 حذف یک اسکرین‌شات.", "Delete one screenshot."),
    ("DELETE", "/texts/<id>", "📄 حذف یک متن ثبت‌شده.", "Delete one stored text."),
    ("GET", "/public/shot/<name>", "🌐 لینک عمومی یک اسکرین‌شات؛ بدون توکن باز "
                                   "می‌شود تا بتوانی در تلگرام یا به مدل دیگری بدهی.",
     "The public link to one screenshot; opens without a token, so it can be "
     "pasted into Telegram or handed to another model."),
    # the coworker agent's eyes on Chrome
    ("GET", "/agent/browser", "🌐 همه‌چیز کروم در یک پاسخ: تب‌های باز، نسخه، "
                              "سابقهٔ کامل پروفایل و تب‌های بسته‌شدهٔ اخیر.",
     "Everything about Chrome in one answer: open tabs, version, the full "
     "profile history and recently closed tabs."),
    ("GET", "/agent/browser/tabs", "🌐 فهرست تب‌های باز همین لحظه (از پورت دیباگ).",
     "The tabs open right now, from the debugging port."),
    ("GET", "/agent/browser/history", "🕘 سابقهٔ کروم: بازدیدها، جست‌وجوها و "
                                      "دانلودها با فیلتر متن و بازهٔ زمانی.",
     "Chrome history: visits, searches and downloads, filterable."),
    ("POST", "/agent/browser/tab", "🌐 باز کردن/فعال کردن/بستن یک تب کروم.",
     "Open, activate or close one Chrome tab."),
    # continuous database backups
    ("GET", "/agent/backups", "🗄 فهرست پشتیبان‌ها + آمار دیتابیس + تنظیمات "
                              "پشتیبان‌گیری.",
     "Backup list plus database stats and backup settings."),
    ("POST", "/agent/backups", "🗄 گرفتن یک پشتیبان همین حالا (tar.gz روی volume).",
     "Take one backup right now (a tar.gz on the volume)."),
    ("GET", "/agent/backup-download", "📥 دانلود یک پشتیبان برای نگهداری بیرون.",
     "Download one backup archive."),
    ("POST", "/agent/backup-delete", "🗄 حذف یک پشتیبان.", "Delete one backup."),
    ("POST", "/agent/backup-send", "🗄 فرستادن یک پشتیبان به تلگرام (کانال/چت "
                                   "دلخواه).",
     "Upload one backup to a Telegram chat or channel."),
    ("POST", "/agent/backup-config", "🗄 تنظیم پشتیبان‌گیری: زمان‌بندی خودکار، "
                                     "تعداد نگه‌داشته، مقصد تلگرام.",
     "Configure backups: automatic schedule, retention, Telegram target."),
    # the trustworthy telegram test
    ("GET", "/agent/telegram/log", "✉️ صندوق پیام‌های تلگرام: هر تلاش ارسال با "
                                   "وضعیت، مقصد، زمان و متن + شمارش کل/رفته/"
                                   "ناموفق.",
     "The Telegram mailbox: every delivery attempt with status, target, time "
     "and text, plus the counters."),
    ("POST", "/agent/telegram/diagnose", "🩺 تست مرحله‌به‌مرحلهٔ تلگرام: حالت، "
                                         "اتصال ربات، نشست حساب، مقصد و یک "
                                         "ارسال واقعی.",
     "Step-by-step Telegram test: mode, bot login, account session, target "
     "and one real send."),
]


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
        self._chat_threads: List[threading.Thread] = []

        self.store = store if store is not None else AgentStore(data_dir, clock=clock)
        if public_base:
            self.store.set_setting("public.base", public_base, actor="startup")
        self.public_base = str(self.store.get_setting("public.base", public_base)
                               or public_base or "")
        # The operations library seeds its built-ins on first start and
        # never overwrites a row the operator edited afterwards.
        self.library = OperationLibrary(self.store)
        self.library.ensure_builtins()
        # The agent key exists from the first start, so an operator who reads
        # the guide finds a key waiting instead of an empty pane. Generating is
        # idempotent: an existing key is returned untouched.
        self.store.agent_key(actor="startup")

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
        # Continuous database backups live on the same volume as the database
        # itself, and reuse the scheduler for the automatic part.
        self.backups = BackupManager(
            self.store, data_dir=data_dir, notifier=self.notifier, clock=clock,
            flows_dir=getattr(flows, "dir", None), hub=self)

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
            "key": self.agent_key_info(),
            "cursor": self.cursor_info(),
            "chat": {"pending": len(self.store.pending_chats()),
                     "messages": len(self.store.list_chat(limit=1000)),
                     "providers": self.chat_providers()},
            "counts": {"jobs": len(self.store.list_jobs()),
                       "scripts": len(self.store.list_scripts()),
                       "pendingScripts": len(self.scripts.pending()),
                       "notes": len(self.store.list_notes(limit=1000)),
                       "operations": len(self.store.list_operations())},
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
            if key.startswith("telegram.channel.") and value not in (
                    "", "bot", "account", "both"):
                raise AgentError("%s must be one of: (empty), bot, account, both"
                                 % key)
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

    # -- the chat tab ----------------------------------------------------
    def chat_providers(self) -> List[str]:
        """The names the chat dropdown offers: browser profiles today, more
        models later without touching the UI.

        Tolerates an llm object that predates `chat_names` - the tests swap in
        a fake, and a stale deployment should offer one provider rather than
        fail the whole overview with a 500.
        """
        names = getattr(self.llm, "chat_names", None)
        if callable(names):
            try:
                return [str(name) for name in names()]
            except Exception:
                pass
        single = getattr(self.llm, "chat_provider", None)
        return [str(single())] if callable(single) else []

    def chat_messages(self, limit: int = 200) -> List[Dict[str, Any]]:
        """Oldest first, the way a conversation reads."""
        return list(reversed(self.store.list_chat(limit)))

    def chat_pending(self) -> List[Dict[str, Any]]:
        return self.store.pending_chats()

    def chat_send(self, text: str, provider: str = "", with_context: bool = False,
                  actor: str = "operator") -> Dict[str, Any]:
        """Start one turn of the conversation.

        The model can take minutes (a web chat is being driven, not an API),
        and this is reached over HTTP, so the answer is not the response: the
        assistant row is created as `thinking` and a worker fills it in, while
        the pane polls `/agent/chat/messages`. A refresh mid-answer therefore
        loses nothing.
        """
        self._guard()
        if not str(text).strip():
            raise AgentError("a message is required")
        name = str(provider or self.llm.chat_provider())
        user = self.store.add_chat("user", str(text), provider=name, actor=actor)
        assistant = self.store.add_chat("assistant", "", provider=name,
                                        status="thinking", actor=actor)
        thread = threading.Thread(
            target=self._chat_worker,
            args=(user["id"], assistant["id"], str(text), name,
                  bool(with_context), actor),
            name="chat-turn-%s" % assistant["id"], daemon=True)
        self._chat_threads = [item for item in self._chat_threads if item.is_alive()]
        self._chat_threads.append(thread)
        thread.start()
        return {"queued": True, "messages": [user, assistant],
                "providers": self.chat_providers()}

    def _chat_prompt(self, user_id: str, text: str, with_context: bool) -> str:
        """History + the new message, as an ordinary conversation the model
        can continue. Deliberately plain: this is a chat page, not a tool."""
        lines = ["تو دستیار گفت‌وگوی این سامانهٔ اتوماسیون مرورگر هستی. روان، "
                 "کوتاه و به زبان کاربر جواب بده. اگر کاربر جریان اتوماسیون "
                 "خواست، همان JSON جریان را با کلید steps برگردان."]
        if with_context:
            lines.append("\n--- context فعلی اتوماسیون (فقط خواندنی) ---")
            lines.append(self._chat_context())
        lines.append("\n--- گفت‌وگو ---")
        for message in self.chat_messages(16):
            if message["id"] == user_id:
                break
            if message["status"] != "done" or not message["body"]:
                continue
            who = "کاربر" if message["role"] == "user" else "دستیار"
            lines.append("%s: %s" % (who, message["body"][:1500]))
        lines.append("کاربر: %s" % text)
        return "\n".join(lines)

    def _chat_context(self) -> str:
        parts = []
        if self.engine is not None:
            status = self.engine.status()
            parts.append("اجرای فعلی: %s" % (status.get("status") or "idle"))
        if self.flows is not None:
            names = [flow.get("name") for flow in self.flows.list()]
            parts.append("جریان‌های ذخیره‌شده: %s" % (", ".join(names) or "-"))
        operations = self.store.list_operations()
        parts.append("عملیات کتابخانه: %s"
                     % (", ".join(op.get("name", "") for op in operations) or "-"))
        if self.engine is not None:
            try:
                page = self.engine.page_text(1500)
            except Exception:  # noqa: BLE001 - context is best effort
                page = ""
            if page:
                parts.append("متن صفحهٔ فعال:\n%s" % page)
        return "\n".join(parts)

    def _chat_worker(self, user_id: str, assistant_id: str, text: str,
                     provider: str, with_context: bool, actor: str) -> None:
        try:
            prompt = self._chat_prompt(user_id, text, with_context)
            # `chat_name` only means something to the browser provider; an HTTP
            # provider ignores it, so there is one code path here instead of a
            # branch that has to be kept in step with LlmClient.
            reply = self.llm.complete(
                prompt,
                timeout=float(self.store.get_setting("ai.timeout", 240)),
                chat_name=provider or None)
            body = str(reply.get("text") or "").strip()
            meta: Dict[str, Any] = {"source": reply.get("source") or {},
                                    "elapsed": reply.get("elapsed"),
                                    "provider": reply.get("provider")}
            parsed = extract_json(body)
            if isinstance(parsed, dict) and isinstance(parsed.get("steps"), list):
                flow, errors = schema.validate_flow(parsed)
                meta["flow"] = flow if not errors else parsed
                meta["flowErrors"] = errors
            self.store.update_chat(assistant_id, body=body or "(پاسخ خالی)",
                                   status="done", meta=meta)
            self.store.audit(actor, "chat.answer",
                             "%d chars via %s" % (len(body), provider))
        except Exception as exc:  # noqa: BLE001 - the pane shows this, not a 500
            self.store.update_chat(assistant_id, status="failed",
                                   error=str(exc)[:500])
            self.store.audit(actor, "chat.failed", str(exc)[:300])

    def chat_apply_flow(self, message_id: str, actor: str = "operator") -> Dict[str, Any]:
        """Save a flow the model proposed inside a chat answer, under a name
        the operator chooses in the pane."""
        message = self.store.get_chat(message_id)
        if message is None:
            raise AgentError("no chat message with id %s" % message_id, 404)
        flow = (message.get("meta") or {}).get("flow")
        if not isinstance(flow, dict) or not flow.get("steps"):
            raise AgentError("that message holds no usable flow", 409)
        normalized, errors = schema.validate_flow(flow)
        if errors:
            raise AgentError("the proposed flow is invalid: %s" % "; ".join(errors))
        name = str(flow.get("name") or "از چت %s" % message_id[-4:])
        if self.flows is None:
            raise AgentError("no flow store is attached")
        return self.flows.save(name, normalized)

    def chat_clear(self, actor: str = "operator") -> Dict[str, Any]:
        return self.store.clear_chat(actor)

    # -- the agent's own API key ------------------------------------------
    def agent_key_info(self) -> Dict[str, Any]:
        """Status only. The key itself needs an explicit reveal, which is
        audited, so a glance at the pane leaves no trace in a shoulder-surfed
        screenshot."""
        if not self.store.get_secret(self.store.AGENT_KEY, ""):
            self.store.agent_key(actor="startup")
        return self.store.agent_key_info()

    def agent_key_reveal(self, actor: str = "operator") -> Dict[str, Any]:
        self.store.audit(actor, "agentKey.revealed",
                         "the key was shown in the sidebar")
        return {"key": self.store.agent_key(actor=actor)}

    def agent_key_rotate(self, actor: str = "operator") -> Dict[str, Any]:
        return {"key": self.store.rotate_agent_key(actor=actor),
                "info": self.store.agent_key_info()}

    def agent_key_set_enabled(self, enabled: bool,
                              actor: str = "operator") -> Dict[str, Any]:
        return {"enabled": self.store.set_agent_key_enabled(bool(enabled),
                                                            actor=actor),
                "info": self.store.agent_key_info()}

    def check_agent_access(self, candidate: str) -> bool:
        """Does this candidate key open the API? Disabled means no, whatever
        the value: cutting access has to win over a leaked key."""
        if not self.store.agent_key_enabled():
            return False
        if not self.store.check_agent_key(candidate):
            return False
        self.store.touch_agent_key()
        return True

    # -- the operations library --------------------------------------------
    def operation_list(self) -> List[Dict[str, Any]]:
        self.library.ensure_builtins()
        return self.library.list()

    def operation_save(self, data: Dict[str, Any], operation_id: Optional[str] = None,
                       actor: str = "operator") -> Dict[str, Any]:
        try:
            return self.library.save(data, operation_id, actor=actor)
        except OperationError as exc:
            raise AgentError(str(exc), getattr(exc, "status", 400))

    def operation_delete(self, operation_id: str,
                         actor: str = "operator") -> Dict[str, Any]:
        try:
            return self.library.delete(operation_id, actor=actor)
        except OperationError as exc:
            raise AgentError(str(exc), getattr(exc, "status", 400))

    def operation_reset(self, operation_id: str,
                        actor: str = "operator") -> Dict[str, Any]:
        try:
            return self.library.reset_builtin(operation_id, actor=actor)
        except OperationError as exc:
            raise AgentError(str(exc), getattr(exc, "status", 400))

    def operation_run(self, operation_id: str,
                      actor: str = "operator") -> Dict[str, Any]:
        """Start an operation's steps as a run, right now."""
        self._guard()
        operation = self.library.get(operation_id)
        if self.engine is None:
            raise AgentError("no automation engine is attached", 409)
        if self.engine.busy():
            raise AgentError("the desktop is busy with another run", 409)
        try:
            flow = self.library.as_flow(operation)
        except OperationError as exc:
            raise AgentError(str(exc), getattr(exc, "status", 409))
        self.engine.start(flow)
        self.store.record_operation_run(operation_id, "started")
        self.store.audit(actor, "operation.run", operation_id)
        return {"status": "started", "name": operation.get("name"),
                "steps": len(operation.get("steps") or [])}

    # -- Telegram: sending on purpose --------------------------------------
    def telegram_send(self, text: str, target: Optional[Any] = None,
                      channel: Optional[str] = None, purpose: str = "manual",
                      actor: str = "operator") -> Dict[str, Any]:
        """Send a message now, to one target or to all of them.

        The agent gets this too, which is what "the agent can decide" means in
        practice: it can tell you something without waiting for a handoff.
        """
        if not str(text).strip():
            raise AgentError("the message is empty")
        if purpose not in PURPOSES:
            raise AgentError("purpose must be one of: %s" % ", ".join(PURPOSES))
        if channel not in (None, "") and channel not in CHANNELS:
            raise AgentError("channel must be bot or account")
        targets = None
        if target not in (None, ""):
            wanted = target
            try:
                wanted = int(target)
            except (TypeError, ValueError):
                pass
            targets = [item for item in self.notifier.targets()
                       if item.get("id") == wanted]
            if not targets:
                raise AgentError("target %s is not in your saved targets" % target, 404)
        result = self.notifier.send(str(text), targets=targets, purpose=purpose,
                                    channel=channel or None)
        if not result.get("sent") and not result.get("failed"):
            raise AgentError(str(result.get("reason") or "nothing was sent"), 400)
        self.store.audit(actor, "telegram.send",
                         "%d char(s), channels=%s" % (len(str(text)),
                                                      ",".join(result.get("channels")
                                                                 or [])))
        return result

    def _run_telegram_job(self, job: Dict[str, Any]) -> Dict[str, Any]:
        """A scheduled Telegram message («برنامه‌ها» in the Telegram pane).

        ``until`` (unix seconds) and ``maxRuns`` live in the payload; when one
        of them is spent the job disables itself instead of firing forever.
        """
        payload = dict(job.get("payload") or {})
        until = float(payload.get("until") or 0)
        max_runs = int(payload.get("maxRuns") or 0)
        runs = int(payload.get("runs") or 0)
        if until and self._clock() > until:
            self._expire_job(job, "the until-date has passed")
            return {"status": "done", "note": "expired"}
        if max_runs and runs >= max_runs:
            self._expire_job(job, "the run limit was reached")
            return {"status": "done", "note": "limit reached"}
        text = str(payload.get("text") or job.get("target") or "").strip()
        if not text:
            raise AgentError("this program has no message text", 400)
        result = self.notifier.send(text, purpose="jobs")
        payload["runs"] = runs + 1
        try:
            self.store.upsert_job({"payload": payload}, job_id=job["id"],
                                  actor="scheduler")
        except Exception:  # the run itself already happened; keep its outcome
            pass
        if not result.get("sent"):
            return {"status": "failed",
                    "error": str(result.get("reason") or "nothing was sent")}
        if max_runs and runs + 1 >= max_runs:
            self._expire_job(job, "the run limit was reached")
        return {"status": "done", "sent": result.get("sent", 0)}

    def _expire_job(self, job: Dict[str, Any], why: str) -> None:
        try:
            self.store.upsert_job({"enabled": False}, job_id=job["id"],
                                  actor="scheduler")
            self.store.audit("scheduler", "job.expired",
                             "%s: %s" % (job.get("id"), why))
        except Exception:
            pass

    def telegram_log(self, limit: int = 100) -> Dict[str, Any]:
        return {"messages": self.store.list_telegram_log(limit),
                "counts": self.store.telegram_counts()}

    # -- continuous database backups ---------------------------------------
    def _run_backup_job(self, job: Dict[str, Any]) -> Dict[str, Any]:
        """The scheduler's `backup` action: snapshot, prune, maybe ship."""
        info = self.backup_create(actor="scheduler", label="زمان‌بندی‌شده")
        pruned = self.backups.prune(actor="scheduler")
        shipped: Dict[str, Any] = {}
        if str(self.backups.config().get("telegramTarget") or ""):
            try:
                shipped = self.backups.send(name=info["name"], actor="scheduler")
            except BackupError as exc:
                shipped = {"sent": 0, "error": str(exc)}
        if self.backups.config().get("notifyAfterBackup"):
            try:
                self.notifier.notify_result(
                    "پشتیبان‌گیری خودکار دیتابیس", info["name"],
                    "done" if not shipped.get("error") else "failed",
                    str(shipped.get("error") or ""))
            except Exception:  # a notification problem must not fail the job
                pass
        return {"status": "done", "backup": info["name"],
                "sizeText": info["sizeText"], "pruned": pruned, "telegram": shipped}

    def backup_stats(self) -> Dict[str, Any]:
        return self.backups.stats()

    def backup_list(self) -> Dict[str, Any]:
        return {"backups": self.backups.list(), "stats": self.backups.stats()}

    def backup_create(self, include_secrets: Optional[bool] = None,
                      label: str = "", actor: str = "operator") -> Dict[str, Any]:
        info = self.backups.create(include_secrets=include_secrets, actor=actor,
                                   label=label)
        self.backups.prune(actor=actor)
        return info

    def backup_delete(self, name: str, actor: str = "operator") -> Dict[str, Any]:
        try:
            return self.backups.delete(name, actor=actor)
        except BackupError as exc:
            raise AgentError(str(exc), exc.status)

    def backup_read(self, name: str) -> bytes:
        try:
            return self.backups.read(name)
        except BackupError as exc:
            raise AgentError(str(exc), exc.status)

    def backup_send(self, name: str = "", target: str = "", channel: str = "",
                    actor: str = "operator") -> Dict[str, Any]:
        try:
            return self.backups.send(name=name, target=target, channel=channel,
                                     actor=actor)
        except BackupError as exc:
            raise AgentError(str(exc), exc.status)

    def backup_configure(self, values: Dict[str, Any],
                         actor: str = "operator") -> Dict[str, Any]:
        try:
            return self.backups.configure(values, actor=actor)
        except BackupError as exc:
            raise AgentError(str(exc), exc.status)

    # -- everything Chrome -------------------------------------------------
    def browser_overview(self, profile: str = "", port: Optional[int] = None,
                         limit: int = 200, query: str = "",
                         days: float = 0.0) -> Dict[str, Any]:
        return browser_info.summary(profile=profile, port=port, limit=limit,
                                    query=query, days=days)

    def browser_tabs(self, port: Optional[int] = None) -> Dict[str, Any]:
        return browser_info.open_tabs(int(port or self.ai_port or 9223))

    def browser_history(self, profile: str = "", limit: int = 200, query: str = "",
                        days: float = 0.0) -> Dict[str, Any]:
        return browser_info.read_history(profile, limit=limit, query=query,
                                          days=days)

    def browser_tab(self, action: str, target_id: str = "", url: str = "",
                    port: Optional[int] = None) -> Dict[str, Any]:
        try:
            return browser_info.tab_action(action, target_id=target_id, url=url,
                                           port=int(port or self.ai_port or 9223))
        except browser_info.BrowserError as exc:
            raise AgentError(str(exc), exc.status)

    # -- the step-by-step Telegram test -------------------------------------
    def telegram_diagnose(self, target: Any = "", channel: str = "",
                          text: str = "") -> Dict[str, Any]:
        """Walk the whole delivery path one step at a time.

        The user asked for a test they can actually trust, so this answers
        "where exactly would it break" instead of a single yes/no: mode ->
        bot login -> account session -> destination -> a real send.
        """
        steps: List[Dict[str, Any]] = []
        mode = self.notifier.mode()
        steps.append({
            "step": "mode", "fa": "حالت تلگرام",
            "ok": mode != MODE_OFF,
            "detail": ("حالت فعلی: %s" % mode) if mode != MODE_OFF
                      else "تلگرام خاموش است؛ از جعبهٔ «حالت» یکی از ربات/حساب/هر دو "
                           "را انتخاب و ذخیره کن.",
        })
        channels = [channel] if channel in CHANNELS else self.notifier.channels("manual")
        steps.append({
            "step": "channels", "fa": "مسیرهای فعال",
            "ok": bool(channels),
            "detail": ("پیام از مسیرهای: %s رد می‌شود." % "، ".join(channels))
                      if channels else "هیچ مسیری فعال نیست (حالت یا مسیر هدف).",
        })
        if MODE_BOT in channels:
            detail, ok = "", False
            try:
                me = self.notifier.transport(MODE_BOT).whoami()
                ok = True
                detail = "ربات وصل است: @%s" % (me.get("username") or "?")
            except TelegramError as exc:
                detail = "ربات جواب نداد: %s" % exc
            steps.append({"step": "bot", "fa": "اتصال ربات (getMe)", "ok": ok,
                          "detail": detail})
        if MODE_ACCOUNT in channels:
            ok, detail = False, ""
            try:
                account = self.notifier.transport(MODE_ACCOUNT)
                if account.authorized():
                    me = account.whoami()
                    ok = True
                    detail = "حساب کاربری وارد شده: %s" % (
                        me.get("username") or me.get("phone") or "?")
                else:
                    detail = ("حساب کاربری هنوز وارد نشده؛ از بخش «ورود با حساب» "
                              "شماره و کد را بده.")
            except TelegramError as exc:
                detail = "حساب کاربری در دسترس نیست: %s" % exc
            steps.append({"step": "account", "fa": "نشست حساب کاربری", "ok": ok,
                          "detail": detail})
        wanted: Any = target
        if target not in (None, ""):
            try:
                wanted = int(target)
            except (TypeError, ValueError):
                pass
        saved = self.notifier.targets()
        chosen = ([item for item in saved if item.get("id") == wanted]
                  if target not in (None, "") else list(saved))
        steps.append({
            "step": "targets", "fa": "مقصد پیام",
            "ok": bool(chosen),
            "detail": ("مقصد: %s" % ", ".join(str(i.get("title") or i.get("id"))
                                              for i in chosen[:3]))
                      if chosen else "هیچ مقصدی انتخاب نشده؛ اول «پیدا کردن چت‌ها» "
                                     "یا یک آی‌دی/نام کانال بده.",
        })
        sent_result: Dict[str, Any] = {}
        if channels and chosen:
            message = str(text or "🩺 تست سلامت تلگرام از سامانهٔ اتوماسیون")
            try:
                sent_result = self.notifier.send(message, targets=chosen,
                                                 purpose="manual",
                                                 channel=channel or None)
                ok = bool(sent_result.get("sent"))
                detail = ("%d پیام فرستاده شد%s"
                          % (sent_result.get("sent", 0),
                             ("؛ خطا: %s" % sent_result["reason"])
                             if sent_result.get("reason") else ""))
            except TelegramError as exc:
                ok, detail = False, "فرستادن ناموفق: %s" % exc
            steps.append({"step": "send", "fa": "فرستادن پیام واقعی", "ok": ok,
                          "detail": detail})
        else:
            steps.append({"step": "send", "fa": "فرستادن پیام واقعی", "ok": False,
                          "skipped": True,
                          "detail": "به دلیل مشکل مرحله‌های قبل انجام نشد."})
        all_ok = all(bool(s.get("ok")) for s in steps)
        self.store.audit("operator", "telegram.diagnose",
                         "ok=%s steps=%d" % (all_ok, len(steps)))
        return {"ok": all_ok, "steps": steps, "send": sent_result,
                "mode": mode, "channels": channels}

    # -- the pointer and the click ripple -----------------------------------
    def cursor_info(self) -> Dict[str, Any]:
        return {
            "enabled": bool(self.store.get_setting("cursor.enabled", True)),
            "size": int(self.store.get_setting("cursor.size", 44) or 44),
            "color": str(self.store.get_setting("cursor.color", "#ffd400")),
            "outline": str(self.store.get_setting("cursor.outline", "#1b1b1b")),
            "ripple": bool(self.store.get_setting("cursor.ripple", True)),
        }

    def cursor_set(self, data: Dict[str, Any],
                   actor: str = "operator") -> Dict[str, Any]:
        from .cursor import MAX_SIZE, MIN_SIZE
        allowed = {"enabled": bool, "size": int, "color": str, "outline": str,
                   "ripple": bool}
        for key in data:
            if key not in allowed:
                raise AgentError("unknown cursor setting: %s" % key)
        for key, cast in allowed.items():
            if key in data:
                value = cast(data[key])
                if key == "size" and not MIN_SIZE <= int(value) <= MAX_SIZE:
                    raise AgentError("cursor size must be %d..%d" % (MIN_SIZE, MAX_SIZE))
                if key in ("color", "outline"):
                    text = str(value)
                    if not _HEX_COLOR_RE.match(text):
                        raise AgentError("%s must be a hex colour like #ffd400" % key)
                self.store.set_setting("cursor.%s" % key, value, actor=actor)
        self.store.audit(actor, "cursor.updated", json.dumps(self.cursor_info()))
        self.cursor_apply()
        return self.cursor_info()

    def cursor_apply(self) -> Dict[str, Any]:
        """Hand the saved settings to the running browser.

        Returns what the effect layer now reports; a missing CDP or a closed
        page is not an error here, because the settings are still saved and
        the next run picks them up.
        """
        backend = self.engine.backend if self.engine is not None else None
        effect = getattr(backend, "cursor_fx", None)
        if effect is None:
            return {"applied": False, "reason": "no cursor effect is attached"}
        try:
            report = effect.configure(self.cursor_config_for_backend())
        except Exception as exc:  # noqa: BLE001 - cosmetic, never fatal
            return {"applied": False, "reason": str(exc)[:200]}
        return {"applied": True, "report": report or {}}

    def cursor_config_for_backend(self) -> Dict[str, Any]:
        info = self.cursor_info()
        return {"enabled": info["enabled"], "size": info["size"],
                "color": info["color"], "outline": info["outline"],
                "ripple": info["ripple"]}

    # -- the route list the sidebar shows ------------------------------------
    def api_index(self) -> List[Dict[str, Any]]:
        """Every route the agent key opens, with the base URL filled in, so
        the operator can hand the whole thing to another AI by copy-paste."""
        base = self.public_base.rstrip("/") if self.public_base else ""
        prefix = "%s/automation/api" % base if base else "/automation/api"
        return [{"method": method, "path": "%s%s" % (prefix, path),
                 "fa": fa, "en": en} for method, path, fa, en in API_INDEX]

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
                                "channels": self.notifier.channel_status(),
                                "routing": {name: str(self.store.get_setting(
                                    "telegram.channel.%s" % name, ""))
                                    for name in PURPOSES},
                                "purposes": list(PURPOSES),
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
        if action not in ("flow", "script", "prompt", "backup", "telegram"):
            raise AgentError("payload.action must be flow, script, prompt, "
                             "backup or telegram")
        target = str(data.get("target") or "")
        if action not in ("prompt", "backup", "telegram") and not target:
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

        if action == "backup":
            return self._run_backup_job(job)

        if action == "telegram":
            return self._run_telegram_job(job)

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

    def ai_open(self, provider: Optional[str] = None,
                url: Optional[str] = None) -> Dict[str, Any]:
        """Open a chat site in the agent browser.

        `provider` picks a stored profile; `url` overrides it entirely, so
        an operation can open any site without touching the settings.
        """
        if self.ai_browser is None:
            raise AgentError("this deployment has no agent browser")
        name = provider or self.llm.chat_provider()
        if url and not provider:
            name = self.llm.chat_provider()
        try:
            return self.ai_browser.open_provider(name, url=url or None)
        except AiBrowserError as exc:
            raise AgentError(str(exc), 502)

    def ai_ask(self, prompt: str, provider: Optional[str] = None,
               timeout: Optional[float] = None,
               fresh_chat: Optional[bool] = None) -> Dict[str, Any]:
        """One question to the agent browser, answer included.

        This is the step-level twin of `ask`: no automation context, no
        JSON insistence - just the prompt, in, and the chat answer, out.
        """
        self._guard()
        if not str(prompt).strip():
            raise AgentError("a prompt is required")
        if self.ai_browser is None:
            raise AgentError("this deployment has no agent browser", 502)
        name = provider or self.llm.chat_provider()
        try:
            kwargs: Dict[str, Any] = {}
            if timeout:
                kwargs["timeout"] = float(timeout)
            if fresh_chat is not None:
                kwargs["fresh_chat"] = bool(fresh_chat)
            result = self.ai_browser.ask(str(prompt), name=name, **kwargs)
        except AiBrowserError as exc:
            raise AgentError(str(exc), 502)
        self.store.audit("agent", "ai.ask",
                         "%d chars to %s" % (len(str(prompt)), name))
        return {"text": str(result.get("text") or ""), "provider": name,
                "source": {"url": result.get("url", ""),
                           "title": result.get("title", "")},
                "elapsed": result.get("elapsed"),
                "timedOut": bool(result.get("timedOut"))}

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
