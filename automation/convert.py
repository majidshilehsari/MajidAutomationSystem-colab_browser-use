"""Round 26: one language for every step.

Flows and operations used to be written in a JSON step language (click,
type, wait_for_text, interactive_form, ...). The operator's verdict: it
ties the coworker agent's hands. Everything the old language could say,
this module says in python instead - one honest line per step, calling the
same desk helpers the old engine dispatched to - so a single step language
remains: python.

The JSON envelope (files, the clipboard, the API) stays; it only ever
carries python steps now. ``${form.x.y}`` references become real python
lookups into ``forms``, the live namespace a form_collect() call fills.

Every converted call is wrapped in ``check(...)``: the old step types
failed the run when a primitive returned a non-zero rc, and the converted
line must keep that promise.
"""

import re
from typing import Any, Callable, Dict, List, Optional, Tuple

_FORM_TOKEN = re.compile(r"\$\{form\.([^}]+)\}")

# Step types that are already the target language.
NATIVE_TYPES = ("python",)

__all__ = [
    "convert_flow", "convert_step", "needs_conversion", "step_to_code",
    "label_for", "NATIVE_TYPES",
]


def _py(value: Any) -> str:
    """A python literal for a JSON value; Persian text stays readable."""
    return repr(value)


def _path_expr(path: str) -> str:
    parts = [part for part in str(path).split(".") if part and part != "form"]
    return "forms" + "".join("[%s]" % _py(part) for part in parts)


def _f_literal(text: str) -> str:
    return (text.replace("\\", "\\\\").replace('"', '\\"')
            .replace("{", "{{").replace("}", "}}")
            .replace("\n", "\\n"))


def _str_expr(value: Any) -> str:
    """A python expression for a string that may carry ${form.*} tokens.

    A value that is exactly one token becomes the bare lookup (so numbers
    and dicts keep their type); anything mixed becomes an f-string, which
    is what the old runtime interpolation produced as text.
    """
    if not isinstance(value, str) or "${form." not in value:
        return _py(value)
    matches = list(_FORM_TOKEN.finditer(value))
    if not matches:
        return _py(value)
    if len(matches) == 1 and matches[0].start() == 0 \
            and matches[0].end() == len(value):
        return _path_expr(matches[0].group(1))
    out: List[str] = []
    pos = 0
    for match in matches:
        out.append(_f_literal(value[pos:match.start()]))
        out.append("{%s}" % _path_expr(match.group(1)))
        pos = match.end()
    out.append(_f_literal(value[pos:]))
    return 'f"%s"' % "".join(out)


def _kwargs(pairs: List[Tuple[str, Optional[str]]]) -> str:
    return "".join(", %s=%s" % (key, value) for key, value in pairs
                   if value is not None)


def _num(value: Any) -> str:
    """0.35 instead of 0.35000000000000003; 2 instead of 2.0."""
    number = float(value)
    if number == int(number):
        return str(int(number))
    return ("%.6f" % number).rstrip("0").rstrip(".")


# --------------------------------------------------------------------------
# one converter per old step type: (code, Persian label)
# --------------------------------------------------------------------------

def _click(step: Dict[str, Any]) -> Tuple[str, str]:
    extra = ""
    if str(step.get("button") or "left") != "left":
        extra += ", button=%s" % _py(step["button"])
    if int(step.get("clicks") or 1) != 1:
        extra += ", clicks=%d" % int(step["clicks"])
    code = "check(click(%d, %d%s))" % (int(step["x"]), int(step["y"]), extra)
    if step.get("modifiers"):
        code = "# modifiers %s were declared but never reached the backend\n%s" % (
            _py(step["modifiers"]), code)
    return code, "کلیک در (%d، %d)" % (int(step["x"]), int(step["y"]))


def _double_click(step: Dict[str, Any]) -> Tuple[str, str]:
    return ("check(double_click(%d, %d))" % (int(step["x"]), int(step["y"])),
            "دوبار کلیک در (%d، %d)" % (int(step["x"]), int(step["y"])))


def _move(step: Dict[str, Any]) -> Tuple[str, str]:
    return ("check(move(%d, %d))" % (int(step["x"]), int(step["y"])),
            "حرکت نشانگر به (%d، %d)" % (int(step["x"]), int(step["y"])))


def _drag(step: Dict[str, Any]) -> Tuple[str, str]:
    extra = ""
    if str(step.get("button") or "left") != "left":
        extra = ", button=%s" % _py(step["button"])
    return ("check(drag(%d, %d, %d, %d%s))" % (
        int(step["x1"]), int(step["y1"]), int(step["x2"]), int(step["y2"]),
        extra),
        "کشیدن از (%d، %d) به (%d، %d)" % (
            int(step["x1"]), int(step["y1"]), int(step["x2"]), int(step["y2"])))


def _scroll(step: Dict[str, Any]) -> Tuple[str, str]:
    pairs: List[Tuple[str, Optional[str]]] = []
    if step.get("x") is not None:
        pairs.append(("x", str(int(step["x"]))))
    if step.get("y") is not None:
        pairs.append(("y", str(int(step["y"]))))
    return ("check(scroll(%d%s))" % (int(step["amount"]), _kwargs(pairs)),
            "اسکرول به مقدار %d" % int(step["amount"]))


def _type(step: Dict[str, Any]) -> Tuple[str, str]:
    pairs: List[Tuple[str, Optional[str]]] = []
    if step.get("typingMode"):
        pairs.append(("mode", _py(step["typingMode"])))
    text = str(step.get("text") or "")
    return ("check(type_text(%s%s))" % (_str_expr(text), _kwargs(pairs)),
            "تایپ «%s»" % (text[:24] + ("…" if len(text) > 24 else "")))


def _paste(step: Dict[str, Any]) -> Tuple[str, str]:
    text = str(step.get("text") or "")
    return ("check(paste(%s))" % _str_expr(text),
            "چسباندن «%s»" % (text[:24] + ("…" if len(text) > 24 else "")))


def _key(step: Dict[str, Any]) -> Tuple[str, str]:
    keys = list(step.get("keys") or [])
    return ("check(press_key(%s))" % _py(keys),
            "کلید «%s»" % "+".join(str(key) for key in keys))


def _wait(step: Dict[str, Any]) -> Tuple[str, str]:
    seconds = float(step.get("ms") or 0) / 1000.0
    return ("wait(%s)" % _num(seconds),
            "صبر %s ثانیه" % _num(seconds))


def _wait_for_text(step: Dict[str, Any]) -> Tuple[str, str]:
    text = str(step.get("text") or "")
    pairs: List[Tuple[str, Optional[str]]] = []
    if int(step.get("timeoutMs") or 15000) != 15000:
        pairs.append(("timeout_ms", str(int(step["timeoutMs"]))))
    if bool(step.get("absent")):
        pairs.append(("absent", "True"))
    label = ("انتظار برای نبودِ «%s»" % text[:24]) if step.get("absent") \
        else ("انتظار برای دیدن «%s»" % text[:24])
    return ("check(wait_for_text(%s%s))" % (_str_expr(text), _kwargs(pairs)),
            label)


def _goto(step: Dict[str, Any]) -> Tuple[str, str]:
    url = str(step.get("url") or "")
    return "check(goto(%s))" % _str_expr(url), "رفتن به %s" % url[:40]


def _focus_window(step: Dict[str, Any]) -> Tuple[str, str]:
    title = str(step.get("title") or "")
    return ("check(focus_window(%s))" % _str_expr(title),
            "پنجرهٔ «%s»" % title[:30])


def _screenshot(step: Dict[str, Any]) -> Tuple[str, str]:
    name = str(step.get("name") or "")
    if name:
        return "check(screenshot(%s))" % _py(name), "اسکرین‌شات «%s»" % name
    return "check(screenshot())", "اسکرین‌شات"


def _capture_text(step: Dict[str, Any]) -> Tuple[str, str]:
    limit = int(step.get("limit") or 0)
    if limit and limit != 4000:
        return ("log(check(capture_text(limit=%d)))" % limit,
                "ضبط متن صفحه (تا %d نویسه)" % limit)
    return "log(check(capture_text()))", "ضبط متن صفحه"


def _human_verify(step: Dict[str, Any]) -> Tuple[str, str]:
    prompt = str(step.get("prompt") or "")
    return ("check(human_verify(%s))" % _str_expr(prompt),
            "تأیید انسانی: %s" % prompt[:30])


def _shell(step: Dict[str, Any]) -> Tuple[str, str]:
    command = str(step.get("command") or "")
    return ("check(shell(%s))" % _str_expr(command),
            "فرمان پوسته: %s" % command[:36])


def _agent_open(step: Dict[str, Any]) -> Tuple[str, str]:
    pairs: List[Tuple[str, Optional[str]]] = [
        ("provider", _py(step["provider"]) if step.get("provider") else None),
        ("url", _str_expr(step["url"]) if step.get("url") else None),
    ]
    return ("check(open_agent(%s))" % _kwargs(pairs).lstrip(", "),
            "باز کردن ایجنت%s" % (
                " (%s)" % step["provider"] if step.get("provider") else ""))


def _agent_ask(step: Dict[str, Any]) -> Tuple[str, str]:
    prompt = str(step.get("prompt") or "")
    pairs: List[Tuple[str, Optional[str]]] = [
        ("provider", _py(step["provider"]) if step.get("provider") else None),
        ("timeout", str(float(step["timeout"])) if step.get("timeout") else None),
        ("fresh_chat", ("True" if step.get("freshChat") else "False")
         if step.get("freshChat") is not None else None),
        ("save_as", "True" if step.get("saveAs") else None),
    ]
    return ("check(ask_agent(%s%s))" % (_str_expr(prompt), _kwargs(pairs)),
            "پرسش از ایجنت: «%s»" % prompt[:30])


def _captcha_solve(step: Dict[str, Any]) -> Tuple[str, str]:
    pairs: List[Tuple[str, Optional[str]]] = [
        ("label", _py(step["label"]) if step.get("label") else None),
        ("auto_click", ("True" if step.get("autoClick") else "False")
         if step.get("autoClick") is not None else None),
        ("notify", ("True" if step.get("notify") else "False")
         if step.get("notify") is not None else None),
        ("shot", _py(step["shot"]) if step.get("shot") else None),
    ]
    return ("check(solve_captcha(%s))" % _kwargs(pairs).lstrip(", "),
            "حل کپچا%s" % (" (%s)" % step["label"] if step.get("label")
                          else ""))


def _progress_notice(step: Dict[str, Any]) -> Tuple[str, str]:
    title = str(step.get("title") or "")
    pairs: List[Tuple[str, Optional[str]]] = [
        ("message", _str_expr(step["message"]) if step.get("message") else None),
        ("level", _py(step["level"]) if step.get("level") else None),
        ("duration_ms", str(int(step["durationMs"]))
         if step.get("durationMs") else None),
        ("dismissible", "False" if step.get("dismissible") is False else None),
        ("position", _py(step["position"]) if step.get("position") else None),
    ]
    return ("check(notify_progress(%s%s))" % (_str_expr(title),
                                              _kwargs(pairs)),
            "اعلان: %s" % title[:30])


def _form_kwargs(step: Dict[str, Any], extra_first: List[str]) -> str:
    parts: List[str] = list(extra_first)
    if step.get("message"):
        parts.append("message=%s" % _str_expr(step["message"]))
    if step.get("submitLabel"):
        parts.append("submit_label=%s" % _py(step["submitLabel"]))
    if step.get("cancelLabel"):
        parts.append("cancel_label=%s" % _py(step["cancelLabel"]))
    if step.get("position"):
        parts.append("position=%s" % _py(step["position"]))
    if step.get("timeoutMs"):
        parts.append("timeout_ms=%d" % int(step["timeoutMs"]))
    out = step.get("output")
    if out is not None:
        parts.append("output=%s" % _py(
            out.get("namespace") if isinstance(out, dict) else out))
    return ",\n    ".join(parts)


def _interactive_form(step: Dict[str, Any]) -> Tuple[str, str]:
    title = str(step.get("title") or "")
    body = _form_kwargs(step, [
        "title=%s" % _str_expr(title),
        "fields=%s" % _py(step.get("fields") or []),
    ])
    return ("answers = check(form_collect(\n    %s,\n))" % body,
            "فرم «%s»" % title[:30])


def _secure_input(step: Dict[str, Any]) -> Tuple[str, str]:
    title = str(step.get("title") or "")
    body = _form_kwargs(step, [
        "title=%s" % _str_expr(title),
        "fields=%s" % _py(step.get("fields") or []),
        "policy=%s" % _py(step.get("policy") or {}),
    ])
    return ("handles = check(secure_collect(\n    %s,\n))" % body,
            "ورود امن «%s»" % title[:30])


def _secure_http(step: Dict[str, Any]) -> Tuple[str, str]:
    method = str(step.get("method") or "GET")
    url = str(step.get("url") or "")
    pairs: List[Tuple[str, Optional[str]]] = [
        ("handle", _py(step["handle"]) if step.get("handle") else None),
        ("secret_ref", _py(step["secretRef"]) if step.get("secretRef")
         else None),
        ("json_body", _py(step["json"]) if step.get("json") is not None
         else None),
        ("save_as", _py(step["saveAs"]) if step.get("saveAs") else None),
        ("timeout", _num(step["timeout"]) if step.get("timeout") else None),
    ]
    return ("check(http_request(method=%s, url=%s%s))"
            % (_py(method), _str_expr(url), _kwargs(pairs)),
            "درخواست امن %s %s" % (method, url[:36]))


def _run_operation(step: Dict[str, Any]) -> Tuple[str, str]:
    wanted = str(step.get("operation") or "")
    return ("check(run_operation(%s))" % _str_expr(wanted),
            "اجرای عملیات «%s»" % wanted[:36])


CONVERTERS: Dict[str, Callable[[Dict[str, Any]], Tuple[str, str]]] = {
    "click": _click,
    "double_click": _double_click,
    "move": _move,
    "drag": _drag,
    "scroll": _scroll,
    "type": _type,
    "paste": _paste,
    "key": _key,
    "wait": _wait,
    "wait_for_text": _wait_for_text,
    "goto_url": _goto,
    "focus_window": _focus_window,
    "screenshot": _screenshot,
    "capture_text": _capture_text,
    "pause_for_human_verification": _human_verify,
    "shell": _shell,
    "agent_open": _agent_open,
    "agent_ask": _agent_ask,
    "captcha_solve": _captcha_solve,
    "progress_notice": _progress_notice,
    "interactive_form": _interactive_form,
    "secure_input": _secure_input,
    "secure_http_request": _secure_http,
    "run_operation": _run_operation,
}

# The keys every step carries beside its type-specific fields.
COMMON_KEYS = ("id", "enabled", "requiresConfirmation", "continueOnError",
               "delayAfterMs", "note")


def step_to_code(step: Dict[str, Any]) -> Tuple[str, str]:
    """(code, Persian label) for one old-JSON step; python passes through."""
    kind = str(step.get("type") or "")
    if kind == "python":
        return str(step.get("code") or ""), str(step.get("label") or "")
    converter = CONVERTERS.get(kind)
    if converter is None:
        raise ValueError("no converter for step type %r" % kind)
    return converter(step)


def label_for(step: Dict[str, Any]) -> str:
    """The uniform Persian label of any step (python: its own or «کد پایتون»)."""
    kind = str(step.get("type") or "")
    if kind == "python":
        return str(step.get("label") or "کد پایتون")
    return step_to_code(step)[1]


def convert_step(step: Dict[str, Any]) -> Dict[str, Any]:
    """One step of any old type becomes one python step (idempotent)."""
    if not isinstance(step, dict):
        raise ValueError("a step must be an object")
    kind = str(step.get("type") or "")
    if kind == "python":
        return dict(step)
    code, label = step_to_code(step)
    converted: Dict[str, Any] = {"type": "python", "code": code,
                                 "label": label}
    for key in COMMON_KEYS:
        if key in step:
            converted[key] = step[key]
    if step.get("type") == "python" and step.get("timeout"):
        converted["timeout"] = step["timeout"]
    return converted


def needs_conversion(flow: Optional[Dict[str, Any]]) -> bool:
    """True when any step of the flow is still written in the old language."""
    for step in (flow or {}).get("steps") or []:
        if not isinstance(step, dict) or str(step.get("type") or "") != "python":
            return True
    return False


def convert_flow(flow: Dict[str, Any]) -> Dict[str, Any]:
    """The whole flow, with every step spoken in python (idempotent)."""
    converted = dict(flow)
    converted["steps"] = [convert_step(step)
                          for step in flow.get("steps") or []]
    return converted
