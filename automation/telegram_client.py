"""Telegram delivery for human handoffs, job results and captcha escalations.

Two independent ways to send, chosen per message target by the operator:

* **bot** - the Bot API over plain HTTPS. Free, no extra dependency (the
  standard library's urllib is enough), and the bot must already be in the
  group it posts to. This is the default and the only mode that never touches
  a personal account.
* **account** - the operator's own Telegram account through Telethon, so the
  message appears to come from them. This needs an api_id/api_hash pair from
  my.telegram.org plus a one-time phone login whose session file grants full
  access to that account. Telethon is imported lazily: when it is not
  installed the account mode reports that clearly instead of breaking the bot
  mode next to it.

Nothing here raises into a run. A Telegram outage must cost a notification, not
an automation, so every public method returns a result dict and delivery of a
handoff happens on a daemon thread.
"""

from __future__ import annotations

import json
import mimetypes
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Callable, Dict, List, Optional


BOT_API_BASE = "https://api.telegram.org"
TIMEOUT = 20.0

MODE_OFF = "off"
MODE_BOT = "bot"
MODE_ACCOUNT = "account"
MODE_BOTH = "both"
MODES = (MODE_OFF, MODE_BOT, MODE_ACCOUNT, MODE_BOTH)

#: The two transports a message can travel on.
CHANNELS = (MODE_BOT, MODE_ACCOUNT)
#: Notification kinds an operator can route to a different transport.
PURPOSES = ("handoff", "captcha", "jobs", "manual", "backup")

DEFAULT_HANDOFF_TEMPLATE = (
    "🛑 نیاز به تأیید انسانی\n"
    "برچسب: {label}\n"
    "پیام: {message}\n"
    "نشانه‌ها: {signals}\n"
    "صفحه: {origin}\n"
    "تصویر: {shot}\n"
    "دسکتاپ زنده: {desktop}\n"
    "اجرا: {run}"
)

DEFAULT_RESULT_TEMPLATE = (
    "{emoji} {title}\n"
    "جریان: {flow}\n"
    "وضعیت: {status}\n"
    "زمان: {when}\n"
    "خطا: {error}"
)


class TelegramError(Exception):
    """A delivery failed. The message is safe to show to the operator."""


def _post_json(url: str, payload: Dict[str, Any], timeout: float = TIMEOUT) -> Dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError:
        raise TelegramError("Telegram replied with something that is not JSON")
    if not data.get("ok"):
        raise TelegramError("Telegram refused the call: %s" % data.get("description"))
    return data.get("result") or {}


def _post_multipart(url: str, fields: Dict[str, Any], files: Dict[str, str],
                    timeout: float = TIMEOUT) -> Dict[str, Any]:
    """Minimal multipart/form-data POST; file attachments need it."""
    boundary = "----mas%s" % uuid.uuid4().hex
    parts: List[bytes] = []
    for name, value in fields.items():
        parts.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                      % (boundary, name, value)).encode("utf-8"))
    for name, path in files.items():
        filename = os.path.basename(path)
        ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        with open(path, "rb") as handle:
            blob = handle.read()
        parts.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\";"
                      " filename=\"%s\"\r\nContent-Type: %s\r\n\r\n"
                      % (boundary, name, filename, ctype)).encode("utf-8"))
        parts.append(blob)
        parts.append(b"\r\n")
    parts.append(("--%s--\r\n" % boundary).encode("utf-8"))
    request = urllib.request.Request(
        url, data=b"".join(parts), method="POST",
        headers={"Content-Type": "multipart/form-data; boundary=%s" % boundary})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8"))
    if not data.get("ok"):
        raise TelegramError("Telegram refused the upload: %s" % data.get("description"))
    return data.get("result") or {}


class TelegramBot:
    """Bot API client. Stateless: every call is one HTTPS request."""

    kind = MODE_BOT

    def __init__(self, token: str, opener: Optional[Callable[..., Any]] = None) -> None:
        if not token:
            raise TelegramError("no bot token is configured")
        self.token = token
        self._opener = opener

    def _url(self, method: str) -> str:
        return "%s/bot%s/%s" % (BOT_API_BASE, self.token, method)

    def _call(self, method: str, payload: Dict[str, Any],
              files: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        if self._opener is not None:
            # Tests inject a fake transport instead of reaching the network.
            return self._opener(method, payload, files)
        try:
            if files:
                return _post_multipart(self._url(method), payload, files)
            return _post_json(self._url(method), payload)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("description", "")
            except Exception:  # noqa: BLE001 - any parse failure is not the point
                pass
            raise TelegramError("Telegram returned HTTP %s: %s" % (exc.code, detail or exc.reason))
        except urllib.error.URLError as exc:
            raise TelegramError("could not reach Telegram: %s" % exc.reason)

    def whoami(self) -> Dict[str, Any]:
        return self._call("getMe", {})

    def list_targets(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Chats that have recently spoken to the bot, newest last message first.

        Finding a numeric chat_id by hand is the single most annoying part of
        setting a bot up, so the sidebar shows this list and the operator picks.
        Only chats that messaged the bot appear - Telegram does not let a bot
        enumerate the groups it sits in silently.
        """
        result = self._call("getUpdates", {"limit": int(limit), "allowed_updates":
                                           ["message", "channel_post"]})
        updates = result if isinstance(result, list) else []
        found: Dict[Any, Dict[str, Any]] = {}
        for update in updates:
            message = update.get("message") or update.get("channel_post") or {}
            chat = message.get("chat") or {}
            chat_id = chat.get("id")
            if chat_id is None:
                continue
            found[chat_id] = {
                "id": chat_id,
                "title": chat.get("title") or chat.get("username")
                or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")]))
                or str(chat_id),
                "type": chat.get("type") or "private",
                "username": chat.get("username") or "",
                "lastText": (message.get("text") or "")[:120],
                "seenAt": message.get("date"),
            }
        return sorted(found.values(), key=lambda item: item.get("seenAt") or 0,
                      reverse=True)

    def send_text(self, chat_id: Any, text: str) -> Dict[str, Any]:
        return self._call("sendMessage", {
            "chat_id": chat_id, "text": text[:4096],
            "disable_web_page_preview": True,
        })

    def send_photo(self, chat_id: Any, path: str, caption: str = "") -> Dict[str, Any]:
        if not os.path.exists(path):
            raise TelegramError("screenshot is gone: %s" % path)
        return self._call("sendPhoto",
                          {"chat_id": chat_id, "caption": caption[:1024]},
                          files={"photo": path})

    def send_document(self, chat_id: Any, path: str,
                      caption: str = "") -> Dict[str, Any]:
        """Upload a file as a document (used for database backups)."""
        if not os.path.exists(path):
            raise TelegramError("the file is gone: %s" % path)
        return self._call("sendDocument",
                          {"chat_id": chat_id, "caption": caption[:1024]},
                          files={"document": path})


class TelegramAccount:
    """The operator's own account through Telethon, imported lazily.

    Telethon is not part of the base image (it is installed best-effort at build
    time), and its session file is a full-access credential, so both the import
    and the login state are reported explicitly rather than guessed at.
    """

    kind = MODE_ACCOUNT

    def __init__(self, api_id: str, api_hash: str, session_path: str) -> None:
        try:
            from telethon import TelegramClient  # noqa: PLC0415 - optional dependency
        except ImportError as exc:
            raise TelegramError(
                "account mode needs the 'telethon' package, which is not installed "
                "in this image; use bot mode instead") from exc
        if not api_id or not api_hash:
            raise TelegramError("account mode needs apiId and apiHash from my.telegram.org")
        self._TelegramClient = TelegramClient
        self.api_id = int(api_id)
        self.api_hash = api_hash
        self.session_path = session_path
        self._lock = threading.Lock()

    def _client(self) -> Any:
        client = self._TelegramClient(self.session_path, self.api_id, self.api_hash)
        client.connect()
        return client

    def authorized(self) -> bool:
        with self._lock:
            client = self._client()
            try:
                return bool(client.is_user_authorized())
            finally:
                client.disconnect()

    def request_code(self, phone: str) -> Dict[str, Any]:
        with self._lock:
            client = self._client()
            try:
                sent = client.send_code_request(phone)
                return {"phoneCodeHash": getattr(sent, "phone_code_hash", ""),
                        "phone": phone}
            finally:
                client.disconnect()

    def sign_in(self, phone: str, code: str, phone_code_hash: str,
                password: str = "") -> Dict[str, Any]:
        with self._lock:
            client = self._client()
            try:
                client.sign_in(phone=phone, code=code,
                               phone_code_hash=phone_code_hash)
                return {"authorized": True}
            except Exception as exc:  # noqa: BLE001 - surface Telethon's own words
                if password and "SESSION_PASSWORD_NEEDED" in str(exc):
                    client.sign_in(password=password)
                    return {"authorized": True}
                raise TelegramError("login failed: %s" % exc) from exc
            finally:
                client.disconnect()

    def whoami(self) -> Dict[str, Any]:
        with self._lock:
            client = self._client()
            try:
                me = client.get_me()
                return {"id": me.id, "title": getattr(me, "first_name", "") or str(me.id)}
            finally:
                client.disconnect()

    def list_targets(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            client = self._client()
            try:
                out = []
                for dialog in client.iter_dialogs(limit=limit):
                    entity = dialog.entity
                    out.append({
                        "id": dialog.id,
                        "title": dialog.title or str(dialog.id),
                        "type": getattr(entity, "broadcast", False) and "channel"
                        or (getattr(entity, "megagroup", None) and "supergroup"
                            or ("private" if not hasattr(entity, "participants_count")
                                else "group")),
                    })
                return out
            finally:
                client.disconnect()

    def send_text(self, chat_id: Any, text: str) -> Dict[str, Any]:
        with self._lock:
            client = self._client()
            try:
                message = client.send_message(chat_id, text[:4096])
                return {"messageId": getattr(message, "id", None)}
            finally:
                client.disconnect()

    def send_photo(self, chat_id: Any, path: str, caption: str = "") -> Dict[str, Any]:
        if not os.path.exists(path):
            raise TelegramError("screenshot is gone: %s" % path)
        with self._lock:
            client = self._client()
            try:
                message = client.send_file(chat_id, path, caption=caption[:1024])
                return {"messageId": getattr(message, "id", None)}
            finally:
                client.disconnect()

    def send_document(self, chat_id: Any, path: str,
                      caption: str = "") -> Dict[str, Any]:
        """Telethon picks the right media type from the file itself."""
        return self.send_photo(chat_id, path, caption)


class Notifier:
    """Picks a transport from settings and formats operator-facing messages.

    Kept separate from both transports so the engine, the scheduler and the
    captcha chain can all notify without knowing whether they are talking to a
    bot or to a personal account.
    """

    def __init__(self, store: Any, public_base: str = "", clock: Any = time.time,
                 store_path: Optional[Callable[[str], str]] = None) -> None:
        self.store = store
        self._clock = clock
        self.public_base = public_base.rstrip("/")
        # Resolves a public screenshot name to a URL the operator can open.
        self._store_path = store_path
        self._pending: List[threading.Thread] = []

    # -- configuration --------------------------------------------------
    def mode(self) -> str:
        value = str(self.store.get_setting("telegram.mode", MODE_OFF) or MODE_OFF)
        return value if value in MODES else MODE_OFF

    def targets(self) -> List[Dict[str, Any]]:
        value = self.store.get_setting("telegram.targets", [])
        return value if isinstance(value, list) else []

    def channels(self, purpose: Optional[str] = None) -> List[str]:
        """Which transports a message of this kind travels on.

        The global mode decides by default; a per-purpose override
        (`telegram.channel.handoff` and friends) may narrow or widen it, so
        for example captcha handoffs can go through the personal account
        while job results stay on the bot. `both` means every configured
        transport gets the message, which is the operator's spare wheel.
        """
        mode = self.mode()
        if mode == MODE_OFF:
            return []
        if purpose:
            override = str(self.store.get_setting(
                "telegram.channel.%s" % purpose, "") or "")
            if override in CHANNELS or override == MODE_BOTH:
                mode = override
        if mode == MODE_BOTH:
            return list(CHANNELS)
        return [mode] if mode in CHANNELS else []

    def channel_status(self) -> Dict[str, Any]:
        """What each transport needs and whether it has it, no network."""
        session = self.store.get_setting(
            "telegram.sessionPath",
            os.path.join(self.store.data_dir, "automation", "telegram.session"))
        return {
            MODE_BOT: {
                "configured": bool(self.store.get_secret("telegram.botToken", "")),
                "needs": "توکن ربات از @BotFather",
            },
            MODE_ACCOUNT: {
                "configured": bool(self.store.get_secret("telegram.apiId", ""))
                              and bool(self.store.get_secret("telegram.apiHash", "")),
                "sessionPath": session,
                "sessionExists": bool(session) and os.path.exists(session),
                "needs": "apiId و apiHash از my.telegram.org و یک بار لاگین",
            },
        }

    def transport(self, mode: Optional[str] = None) -> Any:
        chosen = mode or self.mode()
        if chosen == MODE_BOTH:
            chosen = MODE_BOT
        if chosen == MODE_BOT:
            return TelegramBot(self.store.get_secret("telegram.botToken", ""))
        if chosen == MODE_ACCOUNT:
            session = self.store.get_setting(
                "telegram.sessionPath",
                os.path.join(self.store.data_dir, "automation", "telegram.session"))
            return TelegramAccount(self.store.get_secret("telegram.apiId", ""),
                                   self.store.get_secret("telegram.apiHash", ""),
                                   session)
        raise TelegramError("notifications are off (telegram.mode=%s)" % self.mode())

    def configured(self) -> bool:
        return self.mode() != MODE_OFF and bool(self.targets())

    # -- message building ----------------------------------------------
    def desktop_url(self) -> str:
        base = self.public_base
        if not base:
            builtin = os.environ.get("BUILTIN_DOMAIN", "")
            base = ("https://%s" % builtin) if builtin else ""
        if not base:
            return ""
        return "%s/vnc.html?autoconnect=true&resize=scale&path=websockify" % base

    def shot_url(self, public_name: Optional[str]) -> str:
        if not public_name:
            return ""
        base = self.public_base
        if not base:
            builtin = os.environ.get("BUILTIN_DOMAIN", "")
            base = ("https://%s" % builtin) if builtin else ""
        if not base:
            return public_name
        return "%s/automation/api/public/shot/%s" % (base, urllib.parse.quote(public_name))

    def shot_path(self, public_name: Optional[str]) -> str:
        if not public_name or self._store_path is None:
            return ""
        try:
            path = self._store_path(public_name)
        except Exception:  # noqa: BLE001 - a missing file must not break a run
            return ""
        return path if path and os.path.exists(path) else ""

    def format_handoff(self, payload: Dict[str, Any]) -> str:
        template = self.store.get_setting("telegram.handoffTemplate") \
            or DEFAULT_HANDOFF_TEMPLATE
        signals = payload.get("signals") or []
        return template.format(
            label=payload.get("label") or "-",
            message=payload.get("message") or "-",
            signals=", ".join(signals) if signals else "-",
            origin=payload.get("pageOrigin") or "-",
            shot=self.shot_url(payload.get("publicShot")),
            desktop=self.desktop_url(),
            run=payload.get("runId") or "-",
        )

    def format_result(self, title: str, flow: str, status: str, error: str = "") -> str:
        template = self.store.get_setting("telegram.resultTemplate") \
            or DEFAULT_RESULT_TEMPLATE
        emoji = {"done": "✅", "failed": "❌", "stopped": "⏹",
                 "waiting": "🛑"}.get(status, "ℹ️")
        return template.format(
            emoji=emoji, title=title, flow=flow or "-", status=status or "-",
            when=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self._clock())),
            error=(error or "-")[:600],
        )

    # -- delivery -------------------------------------------------------
    def _deliver(self, text: str, photo_path: str = "",
                 targets: Optional[List[Dict[str, Any]]] = None,
                 purpose: Optional[str] = None,
                 channel: Optional[str] = None,
                 document_path: str = "") -> Dict[str, Any]:
        chosen = targets if targets is not None else self.targets()
        if not chosen:
            return {"sent": 0, "failed": 0, "reason": "no telegram targets configured"}
        channels = [channel] if channel in CHANNELS else self.channels(purpose)
        if not channels:
            return {"sent": 0, "failed": 0, "reason": "notifications are off"}
        sent = 0
        errors: List[str] = []
        used: List[str] = []
        # One log row per destination, so «لیست پیام‌ها» reads like a mailbox:
        # a row appears as "queued" the moment delivery starts and ends up
        # sent / partial / failed with the reason attached.
        per_target: Dict[Any, Dict[str, Any]] = {}
        for target in chosen:
            chat_id = target.get("id")
            if chat_id is None:
                continue
            log_id = 0
            try:
                log_id = self.store.log_telegram(
                    "queued", "", str(purpose or "manual"),
                    target.get("title") or chat_id, text)
            except Exception:  # a logging problem must never block delivery
                log_id = 0
            per_target[chat_id] = {"log": log_id, "ok": False, "errors": [],
                                   "channel": ""}
        for name in channels:
            try:
                client = self.transport(name)
            except TelegramError as exc:
                errors.append("%s: %s" % (name, exc))
                for state in per_target.values():
                    state["errors"].append(str(exc))
                continue
            used.append(name)
            for target in chosen:
                chat_id = target.get("id")
                if chat_id is None or chat_id not in per_target:
                    continue
                state = per_target[chat_id]
                try:
                    if document_path:
                        client.send_document(chat_id, document_path, text)
                    elif photo_path:
                        client.send_photo(chat_id, photo_path, text)
                    else:
                        client.send_text(chat_id, text)
                    sent += 1
                    state["ok"] = True
                    state["channel"] = name
                except TelegramError as exc:
                    errors.append("%s/%s: %s"
                                % (name, target.get("title") or chat_id, exc))
                    state["errors"].append(str(exc))
        for chat_id, state in per_target.items():
            if state["ok"] and not state["errors"]:
                status = "sent"
            elif state["ok"]:
                status = "partial"
            else:
                status = "failed"
            if state["log"]:
                try:
                    self.store.update_telegram_log(
                        state["log"], status,
                        error="; ".join(state["errors"])[:500],
                        channel=state["channel"])
                except Exception:  # keep delivery independent of logging
                    pass
        result = {"sent": sent, "failed": len(errors),
                  "channels": used, "reason": "; ".join(errors)}
        self.store.audit("telegram", "notify.sent" if sent else "notify.failed",
                         "channels=%s targets=%d sent=%d %s"
                         % (",".join(used) or "-", len(chosen), sent,
                            result["reason"][:200]))
        return result

    def send(self, text: str, photo_path: str = "",
             targets: Optional[List[Dict[str, Any]]] = None,
             purpose: Optional[str] = None,
             channel: Optional[str] = None,
             document_path: str = "") -> Dict[str, Any]:
        """Deliver now, in the calling thread. Used by the test endpoint and
        by the agent, which may pick the channel itself."""
        if self.mode() == MODE_OFF:
            return {"sent": 0, "failed": 0, "reason": "notifications are off"}
        return self._deliver(text, photo_path, targets, purpose=purpose,
                             channel=channel, document_path=document_path)

    def send_document(self, path: str, caption: str = "",
                      targets: Optional[List[Dict[str, Any]]] = None,
                      purpose: Optional[str] = None,
                      channel: Optional[str] = None) -> Dict[str, Any]:
        """Upload a file (a backup archive) to Telegram right now."""
        if not os.path.exists(path):
            raise TelegramError("the file is gone: %s" % path)
        return self.send(caption or "📦 فایل پشتیبان", targets=targets,
                         purpose=purpose, channel=channel, document_path=path)

    def notify_handoff(self, payload: Dict[str, Any]) -> None:
        """Called by the engine when a run stops for a human.

        Deliberately fire-and-forget on a daemon thread: Telegram being slow or
        down must never delay the pause itself, and must never propagate an
        exception into the run.
        """
        if self.mode() == MODE_OFF:
            return
        if not self.store.get_setting("telegram.notifyHandoff", True):
            return
        text = self.format_handoff(payload)
        photo = self.shot_path(payload.get("publicShot"))

        def worker() -> None:
            try:
                self._deliver(text, photo, purpose=str(payload.get("purpose")
                                                           or "handoff"))
            except Exception as exc:  # noqa: BLE001 - last resort, never raise
                self.store.audit("telegram", "notify.exception", str(exc)[:200])

        thread = threading.Thread(target=worker, name="telegram-handoff",
                                  daemon=True)
        self._pending = [item for item in self._pending if item.is_alive()]
        self._pending.append(thread)
        thread.start()

    def notify_result(self, title: str, flow: str, status: str,
                      error: str = "") -> None:
        if self.mode() == MODE_OFF:
            return
        key = {"failed": "telegram.notifyJobFailed",
               "done": "telegram.notifyJobDone"}.get(status)
        if key and not self.store.get_setting(key, status == "failed"):
            return
        text = self.format_result(title, flow, status, error)

        def worker() -> None:
            try:
                self._deliver(text, purpose="jobs")
            except Exception as exc:  # noqa: BLE001 - never raise into a scheduler
                self.store.audit("telegram", "notify.exception", str(exc)[:200])

        thread = threading.Thread(target=worker, name="telegram-result",
                                  daemon=True)
        thread.start()
