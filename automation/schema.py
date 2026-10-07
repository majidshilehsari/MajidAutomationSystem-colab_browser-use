"""Flow and step schema for the Colab browser automation stack.

A *flow* is a JSON document describing a list of automation steps. The same
document is produced by the in-browser sidebar, consumed by the server side
engine, and exchanged with an AI assistant, so it is validated in one place.

Everything here is standard library only: the Colab runtime must not need extra
packages for the core automation path to work.
"""

from __future__ import annotations

import re
import uuid
from typing import Any, Dict, Iterable, List, Tuple

SCHEMA_VERSION = 1

#: Desktop geometry used by the virtual display (see start_colab_browser.sh).
DEFAULT_VIEWPORT = {"width": 1366, "height": 768}

BUTTONS = {"left": "1", "middle": "2", "right": "3", "1": "1", "2": "2", "3": "3", "4": "4", "5": "5"}

_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
# Plain text: reject control characters, path separators and angle brackets,
# allow everything else. Double quotes must pass because generated labels look
# like: paste "سلام" and Persian names/labels are ordinary user input.
_NAME_RE = re.compile(r"^[^\x00-\x1f\x7f/\\<>]{0,120}$", re.UNICODE)

# Every step type, its required keys and the optional keys it accepts.
STEP_TYPES: Dict[str, Dict[str, List[str]]] = {
    "click": {"required": ["x", "y"], "optional": ["button", "modifiers", "clicks"]},
    "double_click": {"required": ["x", "y"], "optional": []},
    "drag": {"required": ["x1", "y1", "x2", "y2"], "optional": ["button"]},
    "move": {"required": ["x", "y"], "optional": []},
    "type": {"required": ["text"], "optional": []},
    "paste": {"required": ["text"], "optional": []},
    "key": {"required": ["keys"], "optional": []},
    "scroll": {"required": ["amount"], "optional": ["x", "y"]},
    "wait": {"required": ["ms"], "optional": []},
    "wait_for_text": {"required": ["text"], "optional": ["timeoutMs", "absent"]},
    "goto_url": {"required": ["url"], "optional": []},
    "focus_window": {"required": ["title"], "optional": []},
    "screenshot": {"required": [], "optional": ["name"]},
    "shell": {"required": ["command"], "optional": []},
}

#: Fields accepted on every step regardless of type.
COMMON_STEP_FIELDS = [
    "id",
    "type",
    "label",
    "note",
    "enabled",
    "delayAfterMs",
    "requiresConfirmation",
    "continueOnError",
]

_INT_COORD_FIELDS = ("x", "y", "x1", "y1", "x2", "y2")


class SchemaError(Exception):
    """Raised for a structurally invalid flow."""


def new_id(prefix: str = "s") -> str:
    return "%s-%s" % (prefix, uuid.uuid4().hex[:8])


def _err(errors: List[str], message: str) -> None:
    errors.append(message)


def _validate_int(step: Dict[str, Any], field: str, where: str, errors: List[str],
                  minimum: int | None = None, maximum: int | None = None) -> None:
    value = step.get(field)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _err(errors, "%s: '%s' must be a number" % (where, field))
        return
    if float(value) != int(value):
        _err(errors, "%s: '%s' must be a whole number" % (where, field))
        return
    number = int(value)
    if minimum is not None and number < minimum:
        _err(errors, "%s: '%s' must be >= %s" % (where, field, minimum))
    if maximum is not None and number > maximum:
        _err(errors, "%s: '%s' must be <= %s" % (where, field, maximum))


def _validate_step(step: Any, index: int, viewport: Dict[str, int], errors: List[str]) -> Dict[str, Any]:
    where = "steps[%d]" % index
    if not isinstance(step, dict):
        _err(errors, "%s: must be an object" % where)
        return {}

    kind = step.get("type")
    if not isinstance(kind, str) or kind not in STEP_TYPES:
        _err(errors, "%s: unknown type %r (expected one of %s)"
             % (where, kind, ", ".join(sorted(STEP_TYPES))))
        return {}

    spec = STEP_TYPES[kind]
    for field in spec["required"]:
        if field not in step or step[field] in (None, ""):
            _err(errors, "%s (%s): missing required field '%s'" % (where, kind, field))

    allowed = set(spec["required"]) | set(spec["optional"]) | set(COMMON_STEP_FIELDS)
    for field in step:
        if field not in allowed:
            _err(errors, "%s (%s): unexpected field '%s'" % (where, kind, field))

    for field in _INT_COORD_FIELDS:
        if field in step:
            _validate_int(step, field, "%s (%s)" % (where, kind), errors)

    if "button" in step and str(step["button"]) not in BUTTONS:
        _err(errors, "%s (%s): button must be one of left/middle/right/1-5" % (where, kind))

    if "clicks" in step:
        _validate_int(step, "clicks", "%s (%s)" % (where, kind), errors, 1, 10)

    if "text" in step and not isinstance(step["text"], str):
        _err(errors, "%s (%s): 'text' must be a string" % (where, kind))

    if "keys" in step:
        keys = step["keys"]
        if isinstance(keys, str):
            keys = [keys]
        if not isinstance(keys, list) or not keys or not all(isinstance(k, str) and k for k in keys):
            _err(errors, "%s (%s): 'keys' must be a non-empty list of strings" % (where, kind))

    if "amount" in step:
        _validate_int(step, "amount", "%s (%s)" % (where, kind), errors, -1000, 1000)

    for field, limit in (("ms", 10 * 60 * 1000), ("delayAfterMs", 10 * 60 * 1000),
                         ("delayMs", 10 * 60 * 1000), ("timeoutMs", 10 * 60 * 1000)):
        if field in step:
            _validate_int(step, field, "%s (%s)" % (where, kind), errors, 0, limit)

    if "url" in step and not (isinstance(step["url"], str) and re.match(r"^[a-zA-Z][\w+.-]*://", step["url"])):
        _err(errors, "%s (%s): 'url' must start with a scheme such as https://" % (where, kind))

    for field in ("enabled", "requiresConfirmation", "continueOnError"):
        if field in step and not isinstance(step[field], bool):
            _err(errors, "%s (%s): '%s' must be true or false" % (where, kind, field))

    if "label" in step and (not isinstance(step["label"], str) or not _NAME_RE.match(step["label"])):
        _err(errors, "%s (%s): 'label' must be plain text up to 120 characters" % (where, kind))

    step_id = step.get("id")
    if step_id is not None and (not isinstance(step_id, str) or not _ID_RE.match(step_id)):
        _err(errors, "%s (%s): 'id' may only contain letters, digits and _ . : -" % (where, kind))

    # Coordinates must fit the virtual desktop; a click outside it is a bug in
    # whatever produced the flow and would silently do nothing.
    if kind in ("click", "double_click", "move"):
        _check_bounds(step, ("x", "y"), viewport, "%s (%s)" % (where, kind), errors)
    if kind == "drag":
        _check_bounds(step, ("x1", "y1", "x2", "y2"), viewport, "%s (%s)" % (where, kind), errors)
    if kind == "scroll" and "x" in step:
        _check_bounds(step, ("x", "y"), viewport, "%s (%s)" % (where, kind), errors)

    return step


def _check_bounds(step: Dict[str, Any], fields: Iterable[str], viewport: Dict[str, int],
                  where: str, errors: List[str]) -> None:
    for field in fields:
        value = step.get(field)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        limit = viewport["width"] if field.startswith("x") else viewport["height"]
        if not 0 <= int(value) < limit:
            _err(errors, "%s: %s=%s is outside the %dx%d desktop"
                 % (where, field, int(value), viewport["width"], viewport["height"]))


def _validate_settings(settings: Any, errors: List[str]) -> Dict[str, Any]:
    defaults = {
        "defaultDelayAfterMs": 350,
        "screenshotAfterEachStep": False,
        "stopOnError": True,
        "repeat": 1,
        "allowShellSteps": False,
        "stepTimeoutMs": 60000,
    }
    if settings is None:
        return defaults
    if not isinstance(settings, dict):
        _err(errors, "settings: must be an object")
        return defaults
    for field in settings:
        if field not in defaults:
            _err(errors, "settings: unexpected field '%s'" % field)
    for field in ("defaultDelayAfterMs", "stepTimeoutMs"):
        if field in settings:
            _validate_int(settings, field, "settings", errors, 0, 10 * 60 * 1000)
    if "repeat" in settings:
        _validate_int(settings, "repeat", "settings", errors, 1, 1000)
    for field in ("screenshotAfterEachStep", "stopOnError", "allowShellSteps"):
        if field in settings and not isinstance(settings[field], bool):
            _err(errors, "settings: '%s' must be true or false" % field)
    merged = dict(defaults)
    merged.update({k: v for k, v in settings.items() if k in defaults})
    return merged


def normalize_step(step: Dict[str, Any], index: int) -> Dict[str, Any]:
    """Fill in defaults so downstream code never has to use .get() guards."""
    out = dict(step)
    out.setdefault("id", new_id())
    out.setdefault("enabled", True)
    out.setdefault("requiresConfirmation", False)
    out.setdefault("continueOnError", False)
    if "button" in out:
        out["button"] = str(out["button"])
    if out.get("type") == "key" and isinstance(out.get("keys"), str):
        out["keys"] = [out["keys"]]
    if not out.get("label"):
        out["label"] = default_label(out)
    out["_index"] = index
    return out


def default_label(step: Dict[str, Any]) -> str:
    kind = step.get("type", "?")
    if kind in ("click", "double_click", "move"):
        return "%s %s,%s" % (kind, step.get("x"), step.get("y"))
    if kind == "drag":
        return "drag %s,%s -> %s,%s" % (step.get("x1"), step.get("y1"), step.get("x2"), step.get("y2"))
    if kind in ("type", "paste"):
        text = str(step.get("text", ""))
        return "%s %r" % (kind, text[:24])
    if kind == "key":
        keys = step.get("keys")
        return "key %s" % ("+".join(keys) if isinstance(keys, list) else keys)
    if kind == "scroll":
        return "scroll %s" % step.get("amount")
    if kind == "wait":
        return "wait %sms" % step.get("ms")
    if kind == "wait_for_text":
        return "wait_for_text %r" % str(step.get("text"))[:24]
    if kind == "goto_url":
        return "goto %s" % str(step.get("url"))[:40]
    if kind == "focus_window":
        return "focus %s" % str(step.get("title"))[:30]
    if kind == "screenshot":
        return "screenshot"
    if kind == "shell":
        return "shell %s" % str(step.get("command"))[:40]
    return kind


def validate_flow(flow: Any) -> Tuple[Dict[str, Any], List[str]]:
    """Validate a flow document.

    Returns ``(normalized_flow, errors)``. When ``errors`` is non-empty the flow
    must not be executed; the normalized copy is still returned so callers can
    show partial information in the UI.
    """
    errors: List[str] = []
    if not isinstance(flow, dict):
        return {}, ["flow: must be a JSON object"]

    version = flow.get("schema", SCHEMA_VERSION)
    if version != SCHEMA_VERSION:
        _err(errors, "schema: unsupported version %r (this build understands %d)"
             % (version, SCHEMA_VERSION))

    name = flow.get("name", "Untitled flow")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        _err(errors, "name: must be plain text up to 120 characters")
        name = "Untitled flow"

    viewport = flow.get("viewport") or {}
    if not isinstance(viewport, dict):
        _err(errors, "viewport: must be an object with width and height")
        viewport = {}
    resolved_viewport = {
        "width": viewport.get("width", DEFAULT_VIEWPORT["width"]),
        "height": viewport.get("height", DEFAULT_VIEWPORT["height"]),
    }
    for field in ("width", "height"):
        value = resolved_viewport[field]
        if isinstance(value, bool) or not isinstance(value, int) or not 100 <= value <= 8192:
            _err(errors, "viewport.%s: must be an integer between 100 and 8192" % field)
            resolved_viewport[field] = DEFAULT_VIEWPORT[field]

    steps = flow.get("steps", [])
    if not isinstance(steps, list):
        _err(errors, "steps: must be a list")
        steps = []

    normalized_steps: List[Dict[str, Any]] = []
    seen_ids = set()
    for index, raw in enumerate(steps):
        validated = _validate_step(raw, index, resolved_viewport, errors)
        if not validated:
            continue
        step = normalize_step(validated, index)
        if step["id"] in seen_ids:
            step["id"] = new_id()
        seen_ids.add(step["id"])
        normalized_steps.append(step)

    settings = _validate_settings(flow.get("settings"), errors)

    if not settings["allowShellSteps"]:
        for index, step in enumerate(normalized_steps):
            if step["type"] == "shell":
                _err(errors, "steps[%d]: shell steps are disabled; set settings.allowShellSteps to enable them" % index)

    normalized = {
        "schema": SCHEMA_VERSION,
        "name": name,
        "description": flow.get("description", "") if isinstance(flow.get("description"), str) else "",
        "viewport": resolved_viewport,
        "settings": settings,
        "steps": [{k: v for k, v in s.items() if k != "_index"} for s in normalized_steps],
    }
    for field in ("createdAt", "updatedAt", "origin"):
        if isinstance(flow.get(field), str):
            normalized[field] = flow[field]
    return normalized, errors


def flow_to_prompt_steps(flow: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Strip UI-only noise so an AI sees a compact, editable step list."""
    defaults = {"enabled": True, "requiresConfirmation": False, "continueOnError": False}
    out = []
    for step in flow.get("steps", []):
        compact = {}
        for key, value in step.items():
            if key == "_index" or value in (None, ""):
                continue
            if key in defaults and value == defaults[key]:
                continue  # the value is already implied by the schema
            compact[key] = value
        out.append(compact)
    return out
