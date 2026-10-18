"""The CAPTCHA chain: look, ask, escalate to a human.

Three routes were requested, and they are not equally real:

1. **vision** - send a screenshot to a model and ask where to click. Works for
   the "select every image with a traffic light" family and simple puzzles,
   because those are a vision problem. It does **not** work for reCAPTCHA v2's
   checkbox or hCaptcha, which score behaviour rather than pixels; pretending
   otherwise would just burn attempts.
2. **human** - stop the run and tell somebody on Telegram, with a link to the
   live desktop. This is the reliable one, and the project already had the
   machinery for it (`_create_handoff` / `_wait_for_human` in engine.py, added
   as the "safe human verification handoff"). This module only adds the
   notification and the record.
3. **extension** - a ready-made solver. The honest state of "free" here is thin:
   Buster is free and open source but only solves reCAPTCHA *audio* challenges
   and its own author warns that frequent use risks a temporary block; NopeCHA
   has a free tier of 100 recognitions a day and has been closed-source since
   2023. Everything else with a real success rate is paid. So this route is a
   configuration slot plus a status report, not a promise.

Order matters: the chain tries what is cheap and automatic first and escalates
to a human when confidence is low or attempts run out.

By default the solver **proposes** clicks instead of performing them. A web
page is untrusted input, and a page that can steer the model can steer the
mouse; the operator turns `captcha.autoClick` on once they have watched it work.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from automation.llm import extract_json


VISION_SYSTEM = (
    "تو یک تحلیل‌گر کپچا هستی. فقط بر اساس تصویر داده‌شود تصمیم می‌گیری و "
    "هیچ حدس بیرونی نمی‌زنی. مختصات را در همان مقیاس پیکسلی تصویر می‌دهی، "
    "نسبت به گوشهٔ بالا-چپ تصویر. اگر مطمئن نیستی، confidence را پایین بده."
)

VISION_PROMPT = """این تصویر یک اسکرین‌شات از صفحهٔ مرورگر است که یک چالش امنیتی/کپچا نشان می‌دهد.

{context}

۱) نوع چالش را تشخیص بده.
۲) اقدام‌های لازم را به ترتیب بده.

قالب پاسخ (فقط JSON):
{{
  "kind": "image_select" | "click" | "text" | "slider" | "checkbox" | "audio" | "unknown",
  "confidence": 0.0,
  "summary": "توضیح یک‌خطی فارسی از چیزی که می‌بینی",
  "actions": [
    {{"type": "click", "x": 0, "y": 0, "note": ""}},
    {{"type": "type", "text": ""}},
    {{"type": "key", "keys": ["Return"]}}
  ],
  "solvableByVision": true,
  "reason": ""
}}

قواعد سخت:
- اگر چالش رفتاری است (مثل تیک «من ربات نیستم» در reCAPTCHA یا hCaptcha) مقدار
  "solvableByVision" را false بگذار و در "reason" بنویس که به انسان نیاز دارد.
- هیچ اقدامی خارج از کادر تصویر نده.
- حداکثر ۹ اقدام.
"""

MAX_ACTIONS = 9


class CaptchaError(Exception):
    pass


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


class CaptchaSolver:
    """Runs the configured strategies in order and records what happened."""

    def __init__(self, store: Any, llm: Any, notifier: Any = None,
                 control: Any = None, clock: Any = time.time,
                 image_size: Optional[Any] = None) -> None:
        self.store = store
        self.llm = llm
        self.notifier = notifier
        self.control = control
        self._clock = clock
        # png_size from engine.py; injected so tests do not need real images.
        self._image_size = image_size

    # -- configuration --------------------------------------------------
    def config(self) -> Dict[str, Any]:
        return {
            "enabled": bool(self.store.get_setting("captcha.enabled", True)),
            "strategies": self.store.get_setting("captcha.strategies",
                                                 ["vision", "human"]) or ["human"],
            "autoClick": bool(self.store.get_setting("captcha.autoClick", False)),
            "maxAttempts": int(self.store.get_setting("captcha.maxAttempts", 3)),
            "minConfidence": float(self.store.get_setting("captcha.minConfidence", 0.6)),
            "extension": self.store.get_setting("captcha.extension", "none"),
            "notifyOnEscalation": bool(
                self.store.get_setting("captcha.notifyOnEscalation", True)),
        }

    # config() answers with these short names, and so does the UI; the full
    # setting names are accepted as well. An unlisted key is an error rather
    # than a silent no-op - a checkbox that saves nothing is worse than one
    # that complains.
    CONFIG_BOOLS = {
        "enabled": "captcha.enabled",
        "autoClick": "captcha.autoClick",
        "notifyOnEscalation": "captcha.notifyOnEscalation",
    }
    CONFIG_OTHER = ("strategies", "maxAttempts", "minConfidence", "extension")

    def set_config(self, data: Dict[str, Any], actor: str = "operator") -> Dict[str, Any]:
        known = set(self.CONFIG_BOOLS) | set(self.CONFIG_BOOLS.values()) \
            | set(self.CONFIG_OTHER)
        for key in data:
            if key not in known:
                raise CaptchaError("unknown captcha setting: %s" % key)
        for short, full in self.CONFIG_BOOLS.items():
            value = data[short] if short in data else data.get(full)
            if value is not None:
                self.store.set_setting(full, bool(value), actor=actor)
        if "strategies" in data:
            strategies = [str(item) for item in (data["strategies"] or [])
                          if str(item) in ("vision", "human", "extension")]
            if not strategies:
                raise CaptchaError("at least one strategy is required")
            self.store.set_setting("captcha.strategies", strategies, actor=actor)
        if "maxAttempts" in data:
            attempts = int(data["maxAttempts"])
            if not 1 <= attempts <= 10:
                raise CaptchaError("maxAttempts must be between 1 and 10")
            self.store.set_setting("captcha.maxAttempts", attempts, actor=actor)
        if "minConfidence" in data:
            confidence = float(data["minConfidence"])
            if not 0.0 <= confidence <= 1.0:
                raise CaptchaError("minConfidence must be between 0 and 1")
            self.store.set_setting("captcha.minConfidence", confidence, actor=actor)
        if "extension" in data:
            if str(data["extension"]) not in ("none", "buster", "nopecha"):
                raise CaptchaError("unknown extension: %s" % data["extension"])
            self.store.set_setting("captcha.extension", str(data["extension"]),
                                   actor=actor)
        self.store.audit(actor, "captcha.config.updated",
                         json.dumps(self.config(), ensure_ascii=False)[:400])
        return self.config()

    def extension_status(self) -> Dict[str, Any]:
        """What we can honestly say about the ready-made solver slot."""
        name = str(self.store.get_setting("captcha.extension", "none"))
        known = {
            "buster": {
                "free": True, "openSource": True,
                "covers": "فقط چالش صوتی reCAPTCHA",
                "warning": "نیاز به نصب افزونه در image دارد؛ تشخیص گفتار در کرومِ "
                           "بدون صدا تأیید نشده؛ استفادهٔ پرتکرار در یک روز ریسک "
                           "بلاک موقت دارد (به گفتهٔ خود سازنده).",
                "verified": False,
            },
            "nopecha": {
                "free": True, "openSource": False,
                "covers": "reCAPTCHA، hCaptcha، FunCAPTCHA، AWS WAF، متنی",
                "warning": "لایهٔ رایگان ۱۰۰ تشخیص در ۲۴ ساعت و از ۲۰۲۳ بسته‌متن؛ "
                           "برای بیش از آن اکانت لازم است.",
                "verified": False,
            },
        }
        info = dict(known.get(name) or {"free": True, "covers": "-",
                                        "warning": "هیچ حل‌کنندهٔ آماده‌ای انتخاب نشده",
                                        "verified": False})
        info["name"] = name
        return info

    # -- history --------------------------------------------------------
    def _record(self, entry: Dict[str, Any]) -> None:
        entry["at"] = round(self._clock(), 3)
        self.store.add_note(json.dumps(entry, ensure_ascii=False), kind="captcha",
                            actor="captcha")

    def history(self, limit: int = 50) -> List[Dict[str, Any]]:
        out = []
        for note in self.store.list_notes(kind="captcha", limit=limit):
            try:
                out.append(json.loads(note["body"]))
            except (ValueError, KeyError):
                continue
        return out

    # -- validation -----------------------------------------------------
    def _bounds(self, shot_path: str) -> Optional[Dict[str, int]]:
        if self._image_size is None or not shot_path:
            return None
        try:
            return self._image_size(shot_path)
        except Exception:  # noqa: BLE001 - an unreadable image is not fatal here
            return None

    def validate_actions(self, actions: Any, bounds: Optional[Dict[str, int]]) -> Dict[str, Any]:
        """Reject anything malformed or outside the image.

        The model's reply is untrusted input: it may be wrong, or steered by the
        page it is looking at. Coordinates outside the screenshot are dropped,
        unknown action types are dropped, and the operator sees exactly what was
        dropped and why.
        """
        accepted: List[Dict[str, Any]] = []
        rejected: List[Dict[str, Any]] = []
        if not isinstance(actions, list):
            return {"actions": [], "rejected": [{"reason": "actions is not a list"}]}
        for raw in actions[:MAX_ACTIONS]:
            if not isinstance(raw, dict):
                rejected.append({"action": raw, "reason": "not an object"})
                continue
            kind = str(raw.get("type") or "")
            if kind == "click":
                x = _as_int(raw.get("x"))
                y = _as_int(raw.get("y"))
                if x is None or y is None:
                    rejected.append({"action": raw, "reason": "missing x/y"})
                    continue
                if bounds:
                    width = int(bounds.get("width") or 0)
                    height = int(bounds.get("height") or 0)
                    if not (0 <= x < width and 0 <= y < height):
                        rejected.append({"action": raw,
                                         "reason": "outside the %dx%d image"
                                                   % (width, height)})
                        continue
                accepted.append({"type": "click", "x": x, "y": y,
                                 "note": str(raw.get("note") or "")[:200]})
            elif kind == "type":
                text = raw.get("text")
                if not isinstance(text, str) or not text:
                    rejected.append({"action": raw, "reason": "empty text"})
                    continue
                accepted.append({"type": "type", "text": text[:500]})
            elif kind == "key":
                keys = raw.get("keys")
                if not isinstance(keys, list) or not keys:
                    rejected.append({"action": raw, "reason": "no keys"})
                    continue
                accepted.append({"type": "key",
                                 "keys": [str(item)[:40] for item in keys[:5]]})
            else:
                rejected.append({"action": raw, "reason": "unknown type %r" % kind})
        if len(actions) > MAX_ACTIONS:
            rejected.append({"reason": "more than %d actions were proposed"
                                      % MAX_ACTIONS})
        return {"actions": accepted, "rejected": rejected}

    # -- strategies -----------------------------------------------------
    def vision(self, shot_path: str, context: str = "",
               public_url: str = "") -> Dict[str, Any]:
        config = self.config()
        bounds = self._bounds(shot_path)
        where = ""
        if bounds:
            where = "اندازهٔ تصویر %s×%s پیکسل است." % (bounds.get("width"),
                                                        bounds.get("height"))
        if public_url:
            where += "\nاگر می‌توانی تصویر را از این نشانی بخوان: %s" % public_url
        prompt = VISION_PROMPT.format(context="%s\n%s" % (context or "", where))
        try:
            reply = self.llm.complete(prompt, system=VISION_SYSTEM,
                                      images=[shot_path] if shot_path else [],
                                      want_json=True,
                                      timeout=float(self.store.get_setting(
                                          "ai.timeout", 180)))
        except Exception as exc:  # noqa: BLE001 - reported as a failed strategy
            return {"strategy": "vision", "ok": False, "error": str(exc),
                    "actions": [], "rejected": []}
        parsed = extract_json(reply.get("text", ""))
        if not isinstance(parsed, dict):
            return {"strategy": "vision", "ok": False,
                    "error": "the model did not return JSON",
                    "raw": (reply.get("text") or "")[:1000],
                    "actions": [], "rejected": []}
        confidence = float(parsed.get("confidence") or 0.0)
        validated = self.validate_actions(parsed.get("actions"), bounds)
        result = {
            "strategy": "vision",
            "ok": True,
            "kind": str(parsed.get("kind") or "unknown"),
            "confidence": confidence,
            "summary": str(parsed.get("summary") or "")[:500],
            "solvableByVision": bool(parsed.get("solvableByVision", True)),
            "reason": str(parsed.get("reason") or "")[:500],
            "actions": validated["actions"],
            "rejected": validated["rejected"],
            "raw": (reply.get("text") or "")[:2000],
            "elapsed": reply.get("elapsed"),
            "confident": confidence >= config["minConfidence"],
        }
        # A behavioural challenge is not a vision problem; say so loudly.
        if not result["solvableByVision"]:
            result["ok"] = False
            result["error"] = result["reason"] or "needs a human"
        return result

    def execute(self, actions: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Perform validated actions. Refuses when the kill switch is on."""
        if self.control is None:
            raise CaptchaError("no control backend is attached")
        if self.store.kill_switch_engaged():
            raise CaptchaError("the kill switch is on: the agent may not act")
        done: List[Dict[str, Any]] = []
        for action in actions:
            kind = action.get("type")
            if kind == "click":
                self.control.click(int(action["x"]), int(action["y"]))
            elif kind == "type":
                self.control.type_text(str(action["text"]))
            elif kind == "key":
                self.control.key(list(action["keys"]))
            done.append(action)
            time.sleep(0.35)
        return {"executed": done}

    def escalate(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Tell a human on Telegram. The engine's own pause does the waiting."""
        payload = {
            "label": context.get("label") or "captcha",
            "message": context.get("message") or "یک چالش امنیتی نیاز به انسان دارد",
            "signals": context.get("signals") or [],
            "pageOrigin": context.get("pageOrigin") or "",
            "publicShot": context.get("publicShot") or "",
            "runId": context.get("runId") or "",
        }
        sent: Dict[str, Any] = {"sent": 0, "failed": 0, "reason": "notifier detached"}
        if self.notifier is not None and self.config()["notifyOnEscalation"]:
            try:
                # Purpose "captcha" so the operator can route captcha
                # handoffs through a different channel than other pauses.
                payload["purpose"] = "captcha"
                self.notifier.notify_handoff(payload)
                sent = {"queued": True}
            except Exception as exc:  # noqa: BLE001 - escalation must not explode
                sent = {"sent": 0, "failed": 1, "reason": str(exc)}
        self._record({"event": "escalated", "label": payload["label"],
                      "origin": payload["pageOrigin"], "notify": sent})
        return {"strategy": "human", "ok": True, "notify": sent, "payload": payload}

    # -- the chain ------------------------------------------------------
    def solve(self, shot_path: str, context: Optional[Dict[str, Any]] = None,
              public_url: str = "") -> Dict[str, Any]:
        """Try each enabled strategy in order and report what came out."""
        context = context or {}
        config = self.config()
        if not config["enabled"]:
            return {"ok": False, "skipped": "captcha handling is disabled",
                    "attempts": []}
        attempts: List[Dict[str, Any]] = []
        proposal: Optional[Dict[str, Any]] = None

        for strategy in config["strategies"]:
            if strategy == "vision":
                for attempt in range(1, config["maxAttempts"] + 1):
                    result = self.vision(shot_path, context.get("message", ""),
                                         public_url)
                    result["attempt"] = attempt
                    attempts.append(result)
                    self._record({"event": "vision", "attempt": attempt,
                                  "ok": result.get("ok"),
                                  "kind": result.get("kind"),
                                  "confidence": result.get("confidence"),
                                  "summary": result.get("summary"),
                                  "actions": result.get("actions"),
                                  "error": result.get("error", "")})
                    if not result.get("ok"):
                        break
                    if not result.get("confident") or not result.get("actions"):
                        continue
                    if config["autoClick"] and not self.store.kill_switch_engaged():
                        executed = self.execute(result["actions"])
                        result["executed"] = executed["executed"]
                        return {"ok": True, "strategy": "vision", "attempts": attempts,
                                "result": result}
                    proposal = result
                    break
                if proposal is not None:
                    break
            elif strategy == "extension":
                attempts.append({"strategy": "extension", "ok": False,
                                 "status": self.extension_status()})

        if proposal is not None:
            self._record({"event": "proposal", "actions": proposal.get("actions"),
                          "confidence": proposal.get("confidence")})
            return {"ok": True, "strategy": "vision", "needsApproval": True,
                    "attempts": attempts, "result": proposal}

        # Nothing automatic worked: a human is the last strategy, and also the
        # default when the operator only configured "human".
        if "human" in config["strategies"] or not config["strategies"]:
            escalation = self.escalate(context)
            attempts.append(escalation)
            return {"ok": True, "strategy": "human", "needsHuman": True,
                    "attempts": attempts, "result": escalation}
        return {"ok": False, "attempts": attempts,
                "error": "no strategy produced a usable result"}
