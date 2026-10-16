"""Server side flow runner.

The engine executes a validated flow by driving ``browser_control.sh`` (and so
xdotool / wmctrl / scrot) against the virtual display. It owns its worker
thread, which means a flow keeps running after the human closes the noVNC tab
in their own browser: the only requirement is that the Colab runtime stays
alive.

No third party packages, no X server needed to import this module, which keeps
it unit-testable with a fake backend.
"""

from __future__ import annotations

import json
import os
import shlex
import signal
import struct
import subprocess
import threading
import time
import traceback
import uuid
from typing import Any, Dict, List, Optional, Tuple

import hashlib
import re as _re
import urllib.error
import urllib.request
from urllib.parse import urlsplit as _urlsplit

from .challenge import CHALLENGE_SCRIPT, assess_page, safe_origin
from .convert import convert_flow
from .vault import MAX_BODY_BYTES, SecretVault, VaultError

DEFAULT_DISPLAY = ":1"
# Fixed, deterministic delays chosen for input reliability only. These are not
# randomized and must never be presented as a way to imitate people or bypass
# a site's anti-abuse controls.
TYPING_DELAYS_MS = {"low": 50, "normal": 15, "fast": 0}

TERMINAL_STATES = ("done", "error", "stopped", "cancelled")


class ControlBackend:
    """Runs the primitives exposed by browser_control.sh."""

    def __init__(self, script_path: str, display: str = DEFAULT_DISPLAY,
                 screenshot_dir: Optional[str] = None, timeout: float = 60.0):
        self.script_path = script_path
        self.display = display
        self.screenshot_dir = screenshot_dir
        self.timeout = timeout
        # Optional visual feedback: the yellow pointer and the click ripple
        # from automation.cursor. Purely cosmetic, failures are swallowed
        # inside CursorFx, and None means "leave the desktop alone".
        self.cursor_fx: Any = None
        self._proc_lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None

    def env(self) -> Dict[str, str]:
        env = dict(os.environ)
        env["DISPLAY"] = self.display
        if self.screenshot_dir:
            env["BROWSER_SCREENSHOT_DIR"] = self.screenshot_dir
        return env

    def control(self, args: List[str], timeout: Optional[float] = None) -> Tuple[int, str, str]:
        command = [self.script_path] + [str(a) for a in args]
        limit = timeout or self.timeout
        try:
            # stdin is DEVNULL so a helper that reads it cannot block forever,
            # and the child is kept so terminate() can kill a hung step.
            proc = subprocess.Popen(
                command,
                env=self.env(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                # Its own process group, so terminate() can take the script and
                # every helper it spawned (xdotool, xclip, sleep) with it.
                start_new_session=True,
            )
        except FileNotFoundError:
            return 127, "", "control script not found: %s" % self.script_path
        with self._proc_lock:
            self._proc = proc
        try:
            out, err = proc.communicate(timeout=limit)
        except subprocess.TimeoutExpired:
            self._kill(proc)
            return 124, "", "timed out after %.0fs: %s" % (limit, " ".join(command))
        finally:
            with self._proc_lock:
                if self._proc is proc:
                    self._proc = None
        return proc.returncode, (out or "").strip(), (err or "").strip()

    def terminate(self) -> None:
        """Kill the control script that is running right now, if any.

        Without this, Stop only takes effect between steps: a step blocked in a
        60 second subprocess would ignore the request until it timed out.
        """
        with self._proc_lock:
            proc = self._proc
        if proc is not None:
            self._kill(proc)

    @staticmethod
    def _kill(proc: "subprocess.Popen") -> None:
        """Stop the script and its whole process group.

        Killing only the shell leaves its children holding the pipes, so the
        caller would still block until the step timeout.
        """
        if proc.poll() is not None:
            return
        for signo in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(os.getpgid(proc.pid), signo)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    proc.send_signal(signo)
                except (ProcessLookupError, OSError):
                    return
            try:
                proc.wait(timeout=2)
                return
            except subprocess.TimeoutExpired:
                continue

    # -- primitives -----------------------------------------------------
    def click(self, x: int, y: int, button: str = "left", clicks: int = 1) -> Tuple[int, str, str]:
        # The ring is drawn *before* the click so the operator sees where the
        # pointer was when it happened, not where it ended up. Wrapped because
        # the contract of a cosmetic effect is that it can never break a run,
        # and this is the place that has to enforce it.
        if self.cursor_fx is not None:
            try:
                self.cursor_fx.click_effect(int(x), int(y))
            except Exception:  # noqa: BLE001 - cosmetic, never fatal
                pass
        if clicks == 2:
            return self.control(["doubleclick", x, y])
        if clicks > 2:
            return self._repeat_click(x, y, button, clicks)
        return self.control(["click", x, y, button])

    def _repeat_click(self, x: int, y: int, button: str, clicks: int) -> Tuple[int, str, str]:
        rc, out, err = self.control(["move", x, y])
        if rc != 0:
            return rc, out, err
        last = (0, "", "")
        for _ in range(clicks):
            last = self.control(["click", x, y, button])
            if last[0] != 0:
                return last
            time.sleep(0.08)
        return last

    def move(self, x: int, y: int) -> Tuple[int, str, str]:
        return self.control(["move", x, y])

    def drag(self, x1: int, y1: int, x2: int, y2: int, button: str = "left") -> Tuple[int, str, str]:
        return self.control(["drag", x1, y1, x2, y2, button])

    def type_text(self, text: str, delay_ms: int = 15) -> Tuple[int, str, str]:
        return self.control(["type", "--delay-ms", delay_ms, text])

    def paste(self, text: str) -> Tuple[int, str, str]:
        return self.control(["paste", text])

    def key(self, keys: List[str]) -> Tuple[int, str, str]:
        return self.control(["key"] + list(keys))

    def scroll(self, amount: int, x: Optional[int] = None, y: Optional[int] = None) -> Tuple[int, str, str]:
        if x is not None and y is not None:
            rc, out, err = self.control(["move", x, y])
            if rc != 0:
                return rc, out, err
        return self.control(["scroll", amount])

    def goto_url(self, url: str) -> Tuple[int, str, str]:
        result = self.control(["url", url])
        if result[0] == 0 and self.cursor_fx is not None:
            # A navigation replaces the page, and with it the injected cursor
            # style, so put the pointer back on the new document.
            try:
                self.cursor_fx.install()
            except Exception:  # noqa: BLE001 - cosmetic, never fatal
                pass
        return result

    def focus_window(self, title: str) -> Tuple[int, str, str]:
        return self.control(["focus", title])

    def screenshot(self, name: str) -> Tuple[int, str, str]:
        return self.control(["screenshot", name])

    def active_window(self) -> Tuple[int, str, str]:
        return self.control(["active"])

    def windows(self) -> Tuple[int, str, str]:
        return self.control(["windows"])

    def position(self) -> Tuple[int, str, str]:
        return self.control(["position"])

    def shell(self, command: str, timeout: float = 30.0) -> Tuple[int, str, str]:
        try:
            proc = subprocess.run(
                command,
                shell=True,
                env=self.env(),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return 124, "", "shell step timed out after %.0fs" % timeout
        return proc.returncode, proc.stdout[-4000:], proc.stderr[-4000:]

    def page_text(self) -> Tuple[int, str, str]:
        """Copy the focused page's selection to the clipboard and read it back.

        Used as the detection fallback when Chrome's debugging port is not
        reachable, so text based waits still work without CDP.
        """
        rc, out, err = self.control(["key", "ctrl+a"])
        if rc != 0:
            return rc, out, err
        time.sleep(0.15)
        rc, out, err = self.control(["key", "ctrl+c"])
        if rc != 0:
            return rc, out, err
        time.sleep(0.15)
        # Chrome refuses the legacy STRING target for non-ASCII text, so ask
        # for UTF8_STRING first (same order as the clipboard API bridge).
        rc, text, err = 1, "", ""
        for extra in (["-t", "UTF8_STRING"], ["-t", "STRING"]):
            try:
                proc = subprocess.run(
                    ["xclip", "-selection", "clipboard", "-o"] + extra,
                    env=self.env(), capture_output=True, text=True,
                    timeout=10, check=False)
            except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
                return 1, "", "clipboard read failed: %s" % exc
            rc, text, err = proc.returncode, proc.stdout, proc.stderr
            if rc == 0:
                break
        self.control(["key", "Escape"])
        return rc, text, err


class RunState:
    def __init__(self, run_id: str, flow: Dict[str, Any]):
        # ${form.namespace.field} values collected by interactive_form steps;
        # run-scoped on purpose, and never able to hold a secret (those live
        # in the vault behind opaque handles).
        self.form_outputs: Dict[str, Any] = {}
        self.run_id = run_id
        self.flow = flow
        self.status = "running"
        self.index = 0
        self.pass_index = 1
        self.entries: List[Dict[str, Any]] = []
        # One row per executed step, so a report can be built without parsing
        # the human-readable log. This is what the AI reads to see where a run
        # stopped and why.
        self.results: List[Dict[str, Any]] = []
        # Human verification pauses (automatic challenge detection or an
        # explicit pause_for_human_verification step), without page contents.
        self.handoffs: List[Dict[str, Any]] = []
        self.started_at = time.time()
        self.finished_at: Optional[float] = None
        self.error: Optional[str] = None
        self.awaiting_confirmation: Optional[Dict[str, Any]] = None

    def snapshot(self) -> Dict[str, Any]:
        steps = self.flow.get("steps", [])
        return {
            "runId": self.run_id,
            "status": self.status,
            "index": self.index,
            "passIndex": self.pass_index,
            "totalPasses": self.flow.get("settings", {}).get("repeat", 1),
            "stepCount": len(steps),
            "currentStep": steps[self.index] if 0 <= self.index < len(steps) else None,
            "awaitingConfirmation": self.awaiting_confirmation,
            "startedAt": self.started_at,
            "finishedAt": self.finished_at,
            "elapsedMs": int(((self.finished_at or time.time()) - self.started_at) * 1000),
            "error": self.error,
            "entries": self.entries[-200:],
            "results": self.results[-300:],
            "handoffs": self.handoffs[-50:],
        }


class AutomationEngine:
    """Single-flow runner with pause/stop/confirmation support."""

    def __init__(self, backend: ControlBackend, data_dir: str, sleep=time.sleep,
                 clock=time.time, cdp: Any = None, cdp_port: int = 9222,
                 shots: Any = None, texts: Any = None, notifier: Any = None,
                 agent: Any = None, cursor_fx: Any = None):
        self.backend = backend
        self.data_dir = data_dir
        self._cdp = cdp
        self._cdp_port = cdp_port
        # The coworker agent, when attached: it serves the agent_open,
        # agent_ask and captcha_solve step types and receives the handoff
        # notifications through `notifier`. Assigned after construction by
        # server.build_api, which builds the hub first for exactly this reason.
        self._agent = agent
        self.cursor_fx = cursor_fx
        if cursor_fx is not None and getattr(self.backend, "cursor_fx", None) is None:
            self.backend.cursor_fx = cursor_fx
        # Optional Telegram (or other) notifier, told whenever a run stops for a
        # human. Detached by default, so nothing about a plain run changes.
        self._notifier = notifier
        # Round 27, item 8: called as on_finished(flowName, status, error)
        # whenever a run reaches its end - the hub routes it to the
        # conditional jobs and, on error, to the brain's decision.
        self.on_finished = None
        # Indexes of what this session produced, so the panel can list every
        # screenshot and every extracted text without rescanning the disk.
        self._shots = shots
        self._texts = texts
        self._cdp_warned = False
        self._challenge_probe_unavailable = False
        self._challenge_probe_warned = False
        # Fingerprints of evidence we already handed to a human (fp -> clock)
        # and of text-only wording we already announced, so an unchanged page
        # can never build a handoff loop (round-12 false-pause fix).
        self._challenge_handoffs: Dict[Any, float] = {}
        self._challenge_noticed: Set[Any] = set()
        # Memory-only secret vault (automation.vault) and the values a human
        # just submitted through a pending form card.
        self._form_values: Optional[Dict[str, Any]] = None
        # Screenshots are copied here so a run report can link them without a
        # token, which is what lets the model actually look at the page.
        self.public_dir = os.path.join(data_dir, "public")
        self._sleep = sleep
        self._clock = clock
        # Created after the clock: the vault timestamps every handle with it.
        self._vault = SecretVault(clock=self._clock)
        self._lock = threading.RLock()
        # Round 26: the python-step wrappers need to know where in the run
        # they are (index, shot dir, label) to archive like the old types did.
        self._current_index = 0
        self._current_shot_dir = ""
        self._current_step_label = ""
        self._current_shots: Optional[Dict[str, str]] = None
        self._wakeup = threading.Event()
        self._state: Optional[RunState] = None
        self._thread: Optional[threading.Thread] = None
        self._paused = False
        self._stop_requested = False
        self._confirmed: Optional[bool] = None
        self.history: List[Dict[str, Any]] = []

    # -- public API -----------------------------------------------------
    def busy(self) -> bool:
        """True while a flow owns the single mouse/keyboard of the desktop.

        A run that is paused or waiting for a human still owns the desktop, so
        it must keep reporting busy or a second flow could start on top of it.
        """
        with self._lock:
            return self._state is not None and self._state.status not in TERMINAL_STATES

    def start(self, flow: Dict[str, Any], run_id: Optional[str] = None) -> Dict[str, Any]:
        # The brain's logging policy: mandatory before/after screenshots on
        # every run, whatever the flow itself asked for. Best effort - a
        # failing policy must never block a run.
        brain = getattr(self._agent, "brain", None) if self._agent else None
        policy = getattr(brain, "enforce_logging", None)
        if policy is not None:
            try:
                flow = policy(flow) or flow
            except Exception:
                pass
        # Round 26: one language. Anything still written in the old JSON step
        # types is translated to python here (idempotent for python flows).
        flow = convert_flow(flow)
        with self._lock:
            if self.busy():
                raise RuntimeError("a flow is already running (run %s)" % self._state.run_id)
            run_id = run_id or "run-%d" % int(self._clock() * 1000)
            self._state = RunState(run_id, flow)
            self._paused = False
            self._stop_requested = False
            self._confirmed = None
            self._challenge_probe_unavailable = False
            self._challenge_probe_warned = False
            self._challenge_handoffs = {}
            self._challenge_noticed = set()
            self._wakeup.set()
            self._thread = threading.Thread(target=self._run, name="automation-%s" % run_id,
                                            daemon=True)
            self._thread.start()
            return self._state.snapshot()

    def pause(self) -> Dict[str, Any]:
        with self._lock:
            if self._state is None or self._state.status != "running":
                raise RuntimeError("no flow is running")
            self._paused = True
            self._state.status = "paused"
            return self._state.snapshot()

    def resume(self) -> Dict[str, Any]:
        with self._lock:
            if self._state is None or self._state.status != "paused":
                raise RuntimeError("no flow is paused")
            self._paused = False
            self._state.status = "running"
            self._wakeup.set()
            return self._state.snapshot()

    def stop(self) -> Dict[str, Any]:
        with self._lock:
            if self._state is None:
                raise RuntimeError("no flow is running")
            self._stop_requested = True
            self._wakeup.set()
            snapshot = self._state.snapshot()
        # Outside the lock: the running step may be blocked in a subprocess.
        kill = getattr(self.backend, "terminate", None)
        if callable(kill):
            kill()
        return snapshot

    def confirm(self, approve: bool) -> Dict[str, Any]:
        with self._lock:
            if self._state is None or not self._state.awaiting_confirmation:
                raise RuntimeError("nothing is waiting for confirmation")
            self._confirmed = approve
            self._wakeup.set()
            return self._state.snapshot()

    def status(self) -> Dict[str, Any]:
        with self._lock:
            if self._state is None:
                return {"runId": None, "status": "idle", "entries": [], "history": self.history[-20:]}
            data = self._state.snapshot()
            data["history"] = self.history[-20:]
            return data

    # -- worker ---------------------------------------------------------
    def _log(self, level: str, message: str, **extra: Any) -> None:
        entry = {"t": round(self._clock(), 3), "level": level, "message": message}
        entry.update(extra)
        with self._lock:
            assert self._state is not None
            self._state.entries.append(entry)
            if len(self._state.entries) > 2000:
                self._state.entries = self._state.entries[-1500:]

    @property
    def vault(self) -> SecretVault:
        return self._vault

    # -- interactive forms and secrets ----------------------------------
    def _interpolate(self, value: Any, state: RunState) -> Any:
        """Resolve ${form.ns.field} from this run's form outputs.

        Secret values can never appear here: form outputs only ever hold
        plain form values and masked handle metadata.
        """
        if not isinstance(value, str) or "${form." not in value:
            return value

        def repl(match: "Any") -> str:
            path = match.group(1).split(".")
            if path and path[0] == "form":
                path = path[1:]
            node: Any = state.form_outputs
            for part in path:
                if isinstance(node, dict) and part in node:
                    node = node[part]
                else:
                    return ""
            return node if isinstance(node, str) else json.dumps(node, ensure_ascii=False)

        return _re.sub(r"\$\{form\.([^}]+)\}", repl, value)

    def _interpolate_step(self, step: Dict[str, Any], state: RunState) -> Dict[str, Any]:
        out = dict(step)
        for key, value in list(out.items()):
            if isinstance(value, str):
                out[key] = self._interpolate(value, state)
            elif isinstance(value, dict):
                out[key] = {k: self._interpolate(v, state) if isinstance(v, str) else v
                            for k, v in value.items()}
        return out

    def _set_output(self, state: RunState, namespace: str, value: Any) -> None:
        path = [part for part in str(namespace or "").split(".") if part]
        if path and path[0] == "form":
            path = path[1:]
        if not path:
            return
        node = state.form_outputs
        for part in path[:-1]:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):
                return
        node[path[-1]] = value

    def _form_node(self, state: Optional[RunState], output: Any,
                   index: int, secure: bool = False) -> Any:
        """The dict form_collect()/secure_collect() just filled (round 26)."""
        if isinstance(output, dict):
            namespace = output.get("handle" if secure else "namespace")
        else:
            namespace = output
        if not namespace:
            namespace = (("form.sec%03d" if secure else "form.step%03d")
                         % index)
        node: Any = state.form_outputs if state is not None else {}
        for part in str(namespace).split("."):
            if not part or part == "form":
                continue
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return {}
        return node

    @staticmethod
    def _validate_form_values(schema: Dict[str, Any], raw: Any) -> Dict[str, Any]:
        """Check one submission against the schema the human actually saw.

        Required, maxLength, options and numeric bounds are enforced here (not
        only in the panel), values are coerced to the field type, and anything
        the schema does not declare is refused outright - a submit call must
        never smuggle keys into the run's namespace.
        """
        if not isinstance(raw, dict):
            raise ValueError("values must be an object of field id to value")
        fields = [f for f in (schema.get("fields") or []) if isinstance(f, dict)]
        known = {str(f.get("id")): f for f in fields if f.get("id")}
        unknown = [key for key in raw if key not in known]
        if unknown:
            raise ValueError("unknown field id(s): %s" % ", ".join(sorted(unknown)))
        values: Dict[str, Any] = {}
        for field_id, field in known.items():
            ftype = str(field.get("type") or "text")
            value = raw.get(field_id, field.get("default"))
            if ftype == "checkbox":
                value = bool(value)
            elif ftype in ("radio", "select"):
                allowed = [str(o.get("value")) for o in (field.get("options") or [])
                           if isinstance(o, dict)]
                value = "" if value is None else str(value)
                if value and allowed and value not in allowed:
                    raise ValueError("field %r: %r is not one of the options"
                                     % (field_id, value))
            elif ftype == "number":
                if value not in ("", None):
                    try:
                        value = float(value) if "." in str(value) else int(value)
                    except (TypeError, ValueError):
                        raise ValueError("field %r must be a number" % field_id)
                    if field.get("min") is not None and value < field["min"]:
                        raise ValueError("field %r is below the minimum" % field_id)
                    if field.get("max") is not None and value > field["max"]:
                        raise ValueError("field %r is above the maximum" % field_id)
                else:
                    value = ""
            else:
                value = "" if value is None else str(value)
                limit = field.get("maxLength")
                if limit and len(value) > int(limit):
                    raise ValueError("field %r is longer than %s characters"
                                     % (field_id, limit))
            required = bool(field.get("required"))
            if required and value in ("", None, False):
                raise ValueError("field %r is required" % field_id)
            values[field_id] = value
        return values

    def submit_form(self, form_id: str, values: Dict[str, Any]) -> None:
        """A human submitted a pending interactive form."""
        with self._lock:
            pending = self._state.awaiting_confirmation if self._state else None
            if not pending or pending.get("formId") != form_id:
                raise RuntimeError("no pending form with id %r" % form_id)
            if pending.get("consumed"):
                raise RuntimeError("form %r was already submitted" % form_id)
            clean = self._validate_form_values(pending.get("formSchema") or {},
                                               values)
            self._form_values = clean
            # Marked under the same lock as the wakeup, so a second submit in
            # the polling race gets a clear error instead of silently queuing.
            pending["consumed"] = True
            self._confirmed = True
            self._wakeup.set()

    def submit_secret(self, form_id: str, secrets: Dict[str, str],
                      actor: str = "operator") -> Dict[str, Any]:
        """A human typed secrets into a pending secure_input card.

        The raw values go straight into the vault; the pending step only ever
        sees the masked handle metadata returned here.
        """
        with self._lock:
            pending = self._state.awaiting_confirmation if self._state else None
            if not pending or pending.get("formId") != form_id:
                raise RuntimeError("no pending form with id %r" % form_id)
            schema = pending.get("formSchema") or {}
            policy = schema.get("policy") or {}
            if pending.get("consumed"):
                raise RuntimeError("form %r was already submitted" % form_id)
            filled = {key: val for key, val in (secrets or {}).items() if val}
            if not filled:
                # Round-14 letter, problem 2: refuse BEFORE resuming, so the
                # card stays waiting and the human can type the value. A
                # confirmed-but-empty submit is what produced rc=0 stdout={}.
                raise ValueError("no secret value was entered; nothing reached "
                                 "the vault and the card stays open")
            handles: Dict[str, Any] = {}
            for field_id, raw in filled.items():
                handles[field_id] = self._vault.create(str(raw), policy, actor=actor)
            self._form_values = handles
            pending["consumed"] = True
            self._confirmed = True
            self._wakeup.set()
            return handles

    def cancel_form(self, form_id: str) -> None:
        with self._lock:
            pending = self._state.awaiting_confirmation if self._state else None
            if not pending or pending.get("formId") != form_id:
                raise RuntimeError("no pending form with id %r" % form_id)
            if pending.get("consumed"):
                raise RuntimeError("form %r was already submitted" % form_id)
            pending["consumed"] = True
            self._confirmed = False
            self._wakeup.set()

    def form_status(self, form_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            pending = self._state.awaiting_confirmation if self._state else None
            if not pending or pending.get("formId") != form_id:
                return None
            return {"formId": form_id, "kind": pending.get("kind"),
                    "schema": pending.get("formSchema"),
                    "status": "waiting"}

    def _take_form_values(self) -> Dict[str, Any]:
        with self._lock:
            values = self._form_values or {}
            self._form_values = None
            return values

    def _run_progress_notice(self, step: Dict[str, Any]) -> "tuple[int, str, str]":
        level = str(step.get("level") or "info")
        if level not in ("info", "success", "warning", "error"):
            level = "info"
        title = str(step.get("title") or "")
        message = str(step.get("message") or "")
        # Non-blocking by definition: a notice never waits for anyone.
        self._log(level, "progress notice: %s%s" % (title, (" - " + message) if message else ""),
                  durationMs=int(step.get("durationMs") or 0),
                  dismissible=bool(step.get("dismissible", True)))
        return 0, title, ""

    def _pending_form(self, state: RunState, index: int, step: Dict[str, Any],
                      kind: str) -> Dict[str, Any]:
        form_id = "form-%s-%03d" % (state.run_id, index)
        schema = {key: step[key] for key in
                  ("title", "message", "fields", "submitLabel", "cancelLabel",
                   "position", "timeoutMs") if key in step}
        if kind == "secret":
            schema["policy"] = step.get("policy") or {}
        return {"formId": form_id, "kind": kind, "formSchema": schema}

    def _run_interactive_form(self, state: RunState, index: int,
                              step: Dict[str, Any]) -> "tuple[int, str, str]":
        pending = self._pending_form(state, index, step, "form")
        label = step.get("label") or step.get("title") or "form"
        self._log("warn", "interactive form is waiting for a human",
                  stepIndex=index, formId=pending["formId"],
                  fields=[f.get("id") for f in (step.get("fields") or [])
                          if isinstance(f, dict)])
        self._wait_for_human(state, label, dict(
            pending, index=index, label=label, type="interactive_form",
            message=step.get("message") or step.get("title") or ""))
        values = self._take_form_values()
        out = step.get("output")
        namespace = ((out.get("namespace") if isinstance(out, dict) else out)
                     or ("form.step%03d" % index))
        self._set_output(state, namespace, values)
        summary = json.dumps(values, ensure_ascii=False)[:600]
        return 0, summary, ""

    def _run_secure_input(self, state: RunState, index: int,
                          step: Dict[str, Any]) -> "tuple[int, str, str]":
        pending = self._pending_form(state, index, step, "secret")
        label = step.get("label") or step.get("title") or "secret"
        self._log("warn", "secure input is waiting for a human; the value will only "
                          "enter the memory vault",
                  stepIndex=index, formId=pending["formId"],
                  scope=((step.get("policy") or {}).get("scope")),
                  allowedHosts=((step.get("policy") or {}).get("allowedHosts")))
        self._wait_for_human(state, label, dict(
            pending, index=index, label=label, type="secure_input",
            message=step.get("message") or step.get("title") or ""))
        handles = self._take_form_values()
        if not handles:
            # No value, no handle, no success: a step that quietly returned {}
            # is what broke the chain to secure_http_request in run
            # run-1791705532781 (rc=0, stdout={}).
            return 2, "", ("no secret reached the vault: every submitted value was "
                           "empty, so no handle was created")
        out = step.get("output")
        namespace = ((out.get("handle") if isinstance(out, dict) else out)
                     or ("form.sec%03d" % index))
        self._set_output(state, namespace, handles)
        # Only masked metadata is ever returned or logged.
        return 0, json.dumps(handles, ensure_ascii=False)[:600], ""

    @staticmethod
    def _resolve_secret_ref(ref: str, state: RunState) -> str:
        """Resolve ``creds.api_key`` (run-scoped) to a live vault handle.

        The reference walks this run's form outputs, which hold only masked
        handle metadata, and lifts the fullHandle out of it.  A ref that misses
        yields an empty string; the vault then refuses with a clear error
        instead of the step silently picking something else up.
        """
        node: Any = state.form_outputs
        for part in str(ref or "").split("."):
            if not part or part == "form":
                continue
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return ""
        if isinstance(node, dict):
            return str(node.get("fullHandle") or "")
        return str(node or "")

    def _run_secure_http(self, state: RunState, step: Dict[str, Any]) -> "tuple[int, str, str]":
        handle = str(step.get("handle") or "")
        if not handle and step.get("secretRef"):
            handle = self._resolve_secret_ref(str(step["secretRef"]), state)
            if not handle:
                return 2, "", ("secretRef %r did not resolve: the secure_input step "
                               "must run before this one" % step["secretRef"])
        if not handle:
            return 2, "", "the step needs either a handle or a secretRef"
        method = str(step.get("method") or "GET").upper()
        url = str(step.get("url") or "")
        try:
            secret, policy = self._vault.acquire(handle, method, url)
        except VaultError as exc:
            return 2, "", "vault refused: %s" % exc
        header_name, _, header_value = policy["headerTemplate"].partition(":")
        headers = {header_name.strip(): (header_value or "{{secret}}").strip()
                   .replace("{{secret}}", secret)}
        data = json.dumps(step["json"], ensure_ascii=False).encode("utf-8")             if step.get("json") is not None else None
        request = urllib.request.Request(url, data=data, headers=headers, method=method)

        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args: Any, **kwargs: Any) -> Any:
                return None

        opener = (urllib.request.build_opener(_NoRedirect())
                  if not policy["allowRedirect"] else urllib.request.build_opener())
        timeout = float(step.get("timeout") or 30)
        try:
            with opener.open(request, timeout=timeout) as response:
                raw = response.read(MAX_BODY_BYTES + 1)
                code = int(getattr(response, "status", 200) or 200)
        except urllib.error.HTTPError as exc:
            code = exc.code
            raw = exc.read(MAX_BODY_BYTES + 1)
        except Exception as exc:  # noqa: BLE001 - never leak the secret in errors
            return 2, "", "connector request failed: %s" % type(exc).__name__
        if len(raw) > MAX_BODY_BYTES:
            return 2, "", "response larger than the %d byte cap" % MAX_BODY_BYTES
        digest = hashlib.sha256(raw).hexdigest()[:12]
        parsed: Any = raw.decode("utf-8", "replace")[:20000]
        if raw[:1] in (b"{", b"["):
            try:
                parsed = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                pass
        if step.get("saveAs"):
            self._set_output(state, str(step["saveAs"]), parsed)
        safe_url = _urlsplit(url)
        self._log("ok", "secure request %s %s%s -> %d (%d bytes, sha256:%s)"
                  % (method, safe_url.hostname, safe_url.path, code, len(raw), digest),
                  handle=handle[:8], scope=policy["scope"], uses=None)
        return (0 if 200 <= code < 300 else 2), "status %d sha256:%s" % (code, digest), ""

    @staticmethod
    def _step_params(step: Dict[str, Any]) -> str:
        """One short line of *what moved*: coordinates, keys, url, text.

        Reports must show every motion of the mouse and keyboard, so each
        result row carries the parameters the step actually used, truncated
        hard so a pasted text can never bloat the report.
        """
        parts = []
        for key in ("x", "y", "x1", "y1", "x2", "y2", "amount", "clicks", "button"):
            if step.get(key) is not None:
                parts.append("%s=%s" % (key, step[key]))
        if step.get("keys"):
            parts.append("keys=%s" % str(step["keys"])[:40])
        if step.get("url"):
            parts.append("url=%s" % str(step["url"])[:80])
        if step.get("text"):
            parts.append("text=%r" % str(step["text"])[:40])
        if step.get("ms") is not None:
            parts.append("ms=%s" % step["ms"])
        return " ".join(parts)[:200]

    def _record(self, index: int, step: Dict[str, Any], status: str,
                started_at: Optional[float] = None, duration_ms: int = 0,
                rc: Optional[int] = None, error: Optional[str] = None,
                screenshot: Optional[str] = None, text: Optional[str] = None,
                shots: Optional[Dict[str, str]] = None,
                image: Optional[Dict[str, int]] = None,
                typing_mode: Optional[str] = None,
                key_delay_ms: Optional[int] = None) -> None:
        """Append one machine-readable row about a step's outcome."""
        with self._lock:
            assert self._state is not None
            self._state.results.append({
                "index": index,
                "passIndex": self._state.pass_index,
                "type": step.get("type"),
                "label": step.get("label") or step.get("type"),
                "stepId": step.get("id"),
                "params": self._step_params(step),
                "status": status,
                "startedAt": round(started_at, 3) if started_at else None,
                "durationMs": duration_ms,
                "rc": rc,
                "error": (error or None),
                "screenshot": screenshot,
                # The page text a capture_text step read, and the published
                # screenshots around the step, so a report can show the model
                # what the page actually said and looked like.
                "text": text,
                "shots": shots or None,
                "image": image,
                "typingMode": typing_mode,
                "keyDelayMs": key_delay_ms,
            })
            if len(self._state.results) > 2000:
                self._state.results = self._state.results[-1500:]

    def _page_text(self) -> Tuple[int, str, str]:
        """The visible text of the page.

        CDP reads the real DOM, so it works no matter which element has focus.
        The clipboard fallback presses ctrl+a / ctrl+c, which selects the
        focused field instead of the page whenever the caret sits in an input,
        so a wait_for_text on a page label could never match. CDP first,
        clipboard only if the debugging port is unreachable.
        """
        if self._cdp is not None:
            try:
                result = self._cdp.evaluate("document.body.innerText", port=self._cdp_port)
                value = (result or {}).get("value")
                if isinstance(value, str) and value:
                    return 0, value, ""
            except Exception as exc:
                if not self._cdp_warned:
                    self._cdp_warned = True
                    self._log("warn", "page text via CDP failed, falling back to the "
                                      "clipboard: %s" % exc)
        return self.backend.page_text()

    def _publish(self, path: Optional[str]) -> Optional[str]:
        """Copy a screenshot into the public share dir; returns its file name."""
        if not path:
            return None
        target = path.strip()
        if not os.path.isfile(target):
            return None
        name = os.path.basename(target)
        try:
            os.makedirs(self.public_dir, exist_ok=True)
            with open(target, "rb") as src:
                data = src.read()
            tmp = os.path.join(self.public_dir, name + ".tmp")
            with open(tmp, "wb") as dst:
                dst.write(data)
            os.replace(tmp, os.path.join(self.public_dir, name))
            return name
        except OSError as exc:
            self._log("warn", "could not publish %s: %s" % (name, exc))
            return None

    def _interruptible_sleep(self, seconds: float) -> bool:
        """Sleep in small slices; returns False if a stop was requested."""
        deadline = self._clock() + seconds
        while True:
            with self._lock:
                if self._stop_requested:
                    return False
                paused = self._paused
            if paused:
                self._wakeup.wait(0.2)
                self._wakeup.clear()
                continue
            remaining = deadline - self._clock()
            if remaining <= 0:
                return True
            self._sleep(min(remaining, 0.1))

    def _run(self) -> None:
        state = self._state
        assert state is not None
        flow = state.flow
        settings = flow.get("settings", {})
        steps = flow.get("steps", [])
        passes = int(settings.get("repeat", 1))
        shot_dir = os.path.join(self.data_dir, "runs", state.run_id)
        os.makedirs(shot_dir, exist_ok=True)

        self._log("info", "run started: %s (%d steps, %d pass)"
                  % (flow.get("name"), len(steps), passes))

        try:
            for pass_index in range(1, passes + 1):
                with self._lock:
                    state.pass_index = pass_index
                if passes > 1:
                    self._log("info", "pass %d/%d" % (pass_index, passes))
                for index, step in enumerate(steps):
                    with self._lock:
                        state.index = index
                    if not self._interruptible_sleep(0):
                        raise _Stopped()
                    if not step.get("enabled", True):
                        self._log("skip", "step %d disabled: %s" % (index, step.get("label")))
                        self._record(index, step, "skipped")
                        continue
                    if step.get("type") != "pause_for_human_verification":
                        self._pause_if_challenge(state, index, step, shot_dir)
                    self._execute_step(state, index, step, shot_dir, settings)
                    with self._lock:
                        if state.status == "error":
                            raise _Failed(state.error or "step failed")
            if not self._interruptible_sleep(0):
                raise _Stopped()
            if steps:
                self._pause_if_challenge(state, len(steps) - 1, steps[-1], shot_dir)
        except _Stopped:
            self._finish("stopped")
            return
        except _Failed:
            self._finish("error")
            return
        except Exception as exc:  # pragma: no cover - defensive
            with self._lock:
                state.error = "unexpected error: %s" % exc
            self._log("error", state.error)
            self._finish("error")
            return

        self._finish("done")

    #: Steps allowed to run while a challenge is merely suspected: they only
    #: look at the page or leave it, never touching the challenge itself.
    SAFE_RECOVERY_TYPES = ("focus_window", "screenshot", "capture_text", "wait")
    # Round 26: the one language needs its own recovery rule - a python step
    # that only looks around, or only travels to an allowed origin, is the
    # same safe recovery the old typed steps were.
    SAFE_PY_CALLS = frozenset({
        "check", "focus_window", "screenshot", "capture_text", "wait",
        "sleep", "log", "press_key", "goto"})
    SAFE_RECOVERY_ORIGINS = frozenset({
        "https://arena.ai", "https://www.google.com", "https://lmarena.ai",
        "https://chat.deepseek.com", "https://deepseek.com",
    })
    #: Identical evidence within this window never raises a second handoff.
    CHALLENGE_COOLDOWN = 120.0

    def _is_safe_recovery(self, step: Dict[str, Any]) -> bool:
        step_type = step.get("type")
        if step_type in self.SAFE_RECOVERY_TYPES:
            return True
        if step_type == "key":
            keys = str(step.get("keys") or "").lower()
            return "ctrl+l" in keys or "control+l" in keys
        if step_type == "goto_url":
            return safe_origin(step.get("url")) in self.SAFE_RECOVERY_ORIGINS
        if step_type == "python":
            return self._python_is_safe_recovery(str(step.get("code") or ""))
        return False

    def _python_is_safe_recovery(self, code: str) -> bool:
        """True when every executed line only looks around or travels to an
        allowed origin - the round-12 recovery rule, spoken in python."""
        lines = [line.strip() for line in code.splitlines()
                 if line.strip() and not line.strip().startswith("#")]
        if not lines:
            return False
        for line in lines:
            names = set(_re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(", line))
            if not names or not names <= self.SAFE_PY_CALLS:
                return False
            if "goto" in names:
                match = _re.search(r"goto\(\s*(['\"])(.*?)\1", line)
                if (not match or safe_origin(match.group(2))
                        not in self.SAFE_RECOVERY_ORIGINS):
                    return False
            if "press_key" in names:
                low = line.lower()
                if "ctrl+l" not in low and "control+l" not in low:
                    return False
        return True

    def _challenge_fingerprint(self, assessment: Dict[str, Any]) -> Any:
        return (assessment.get("pageOrigin"),
                tuple(assessment.get("signals") or ()),
                assessment.get("textHash"))

    def _challenge_notice(self, index: int, assessment: Dict[str, Any],
                          fingerprint: Any) -> None:
        """Words without a widget: inform, never block (progress notice)."""
        with self._lock:
            if fingerprint in self._challenge_noticed:
                return
            self._challenge_noticed.add(fingerprint)
        self._log("info", "progress notice: wording that mentions CAPTCHA or a security "
                          "check was seen, but no challenge widget is visible; the run "
                          "continues without a pause",
                  stepIndex=index, signals=assessment.get("signals"),
                  evidence=assessment.get("evidence"),
                  pageOrigin=assessment.get("pageOrigin"))

    def _pause_if_challenge(self, state: RunState, index: int, step: Dict[str, Any],
                            shot_dir: str) -> None:
        """Pause before further automation when *structural* cues say challenge.

        Round-12 rules, after a real false-pause loop on a chat page whose old
        text merely mentioned CAPTCHA:

        * recovery steps (focus, address-bar, allowed goto_url, screenshot,
          capture_text, wait) never wait on the detector - they are how a flow
          leaves a stale page;
        * wording-only signals become a non-blocking progress notice;
        * identical evidence (origin+signals+text hash) inside the cooldown
          never raises a second handoff, so Continue on an unchanged page
          cannot loop.
        """
        if self._is_safe_recovery(step):
            return
        while True:
            if self._cdp is None or self._challenge_probe_unavailable:
                return
            try:
                result = self._cdp.evaluate(CHALLENGE_SCRIPT, port=self._cdp_port, timeout=1.5)
                payload = result.get("value") if isinstance(result, dict) and "value" in result else result
                assessment = assess_page(payload)
                if not assessment.get("pageOrigin") and isinstance(result, dict):
                    assessment["pageOrigin"] = safe_origin(result.get("url"))
            except Exception:
                self._challenge_probe_unavailable = True
                if not self._challenge_probe_warned:
                    self._challenge_probe_warned = True
                    self._log("warn", "automatic CAPTCHA/security-check detection is unavailable "
                                      "(page inspection through CDP failed); inspect the page manually")
                return
            fingerprint = self._challenge_fingerprint(assessment)
            if not assessment.get("detected"):
                if assessment.get("signals"):
                    self._challenge_notice(index, assessment, fingerprint)
                return
            now = self._clock()
            with self._lock:
                last = self._challenge_handoffs.get(fingerprint)
            if last is not None and (now - last) < self.CHALLENGE_COOLDOWN:
                self._log("warn", "challenge evidence is unchanged since the last handoff "
                                  "(cooldown %ds); continuing without a new pause"
                                  % self.CHALLENGE_COOLDOWN,
                          stepIndex=index, signals=assessment.get("signals"),
                          evidence=assessment.get("evidence"),
                          pageOrigin=assessment.get("pageOrigin"))
                return
            message = (
                "سامانه نشانه‌های ساختاری چالش (ویجت/iframe/title) دید و اجرای خودکار را "
                "متوقف کرد. خودت صفحه را بررسی و در صورت مجاز بودن، مرحله را در مرورگر انجام "
                "بده؛ کد یا پاسخ را برای هوش مصنوعی نفرست. فقط وقتی چالش رفع شده و صفحه عادی "
                "است ادامه بده."
            )
            with self._lock:
                self._challenge_handoffs[fingerprint] = now
            self._create_handoff(state, index, step, shot_dir, "challenge", message, assessment)
            return

    def _create_handoff(self, state: RunState, index: int, step: Dict[str, Any],
                        shot_dir: str, kind: str, message: str,
                        assessment: Optional[Dict[str, Any]] = None) -> None:
        label = step.get("label") or step.get("type") or "human verification"
        step_id = step.get("id", "s")
        name = "step-%03d-handoff-p%03d-%s-%s.png" % (
            index,
            state.pass_index,
            step_id,
            uuid.uuid4().hex[:8],
        )
        path = self._capture(shot_dir, name)
        public_name = self._publish(path)
        image = png_size(path) if path else None
        signals = list((assessment or {}).get("signals") or [])
        evidence = list((assessment or {}).get("evidence") or [])
        page_origin = (assessment or {}).get("pageOrigin") or ""
        role = "challenge" if kind == "challenge" else "handoff"
        event = {
            "kind": kind,
            "detectedAt": round(self._clock(), 3),
            "index": index,
            "label": label,
            "signals": signals,
            "evidence": evidence,
            "structural": bool((assessment or {}).get("structural")),
            "textHash": (assessment or {}).get("textHash"),
            "pageOrigin": page_origin,
            "publicShot": public_name,
            "decision": "pending",
            "resolvedAt": None,
        }
        with self._lock:
            state.handoffs.append(event)
            if len(state.handoffs) > 50:
                state.handoffs = state.handoffs[-40:]

        if public_name and self._shots is not None:
            try:
                self._shots.add({
                    "name": public_name, "role": role, "runId": state.run_id,
                    "stepIndex": index, "stepLabel": label,
                    "stepType": step.get("type"), "image": image,
                    "status": "waiting",
                })
            except OSError as exc:
                self._log("warn", "could not index human-handoff screenshot: %s" % exc)

        log_message = ("possible CAPTCHA/security challenge detected; pausing for human review"
                       if kind == "challenge" else "pausing for the requested human verification")
        self._log("warn", log_message, stepIndex=index, signals=signals,
                  evidence=evidence, pageOrigin=page_origin,
                  publicShot=public_name)
        pending = {
            "index": index, "label": label, "type": step.get("type"),
            "kind": kind, "message": message, "signals": signals,
            "evidence": evidence, "structural": bool((assessment or {}).get("structural")),
            "pageOrigin": page_origin, "publicShot": public_name,
        }
        # The notifier is told before the wait begins, so a slow or dead
        # Telegram cannot delay the pause itself.
        if self._notifier is not None:
            try:
                self._notifier.notify_handoff(dict(pending, runId=state.run_id))
            except Exception as exc:  # noqa: BLE001 - a broken notifier must not break a run
                self._log("warn", "handoff notification failed: %s" % exc)
        self._wait_for_human(state, label, pending, event)

    def _wait_for_human(self, state: RunState, label: str,
                        pending: Dict[str, Any], event: Optional[Dict[str, Any]] = None) -> None:
        with self._lock:
            self._confirmed = None
            self._wakeup.clear()
            state.awaiting_confirmation = pending
            state.status = "waiting"
        if pending.get("kind") == "confirmation":
            self._log("warn", "waiting for human confirmation before: %s" % label)
        while True:
            approved: Optional[bool] = None
            stopped = False
            with self._lock:
                if self._stop_requested:
                    state.awaiting_confirmation = None
                    state.status = "running"
                    stopped = True
                elif self._confirmed is not None:
                    approved = self._confirmed
                    self._confirmed = None
                    state.awaiting_confirmation = None
                    state.status = "running"
                    if event is not None:
                        event["decision"] = "continued" if approved else "stopped"
                        event["resolvedAt"] = round(self._clock(), 3)
            if stopped:
                self._log("warn", "human handoff stopped by user before: %s" % label)
                raise _Stopped()
            if approved is not None:
                if approved:
                    self._log("info", "human confirmed the handoff; continuing after: %s" % label)
                    return
                self._log("warn", "human declined the handoff; stopping before: %s" % label)
                raise _Stopped()
            self._wakeup.wait(0.2)
            self._wakeup.clear()

    def _execute_step(self, state: RunState, index: int, step: Dict[str, Any],
                      shot_dir: str, settings: Dict[str, Any]) -> None:
        label = step.get("label") or step.get("type")
        kind = step.get("type")
        self._current_index = index
        self._current_shot_dir = shot_dir
        self._current_step_label = str(label or "")
        self._log("step", "#%d %s" % (index, label), stepId=step.get("id"), type=kind)

        if step.get("requiresConfirmation"):
            self._wait_for_human(state, label, {
                "index": index, "label": label, "type": kind,
                "kind": "confirmation",
                "message": "گام بعدی به تأیید تو نیاز دارد.",
            })

        shots: Dict[str, str] = {}
        self._current_shots = shots
        image = None
        step_id = step.get("id", "s")

        if settings.get("screenshotBeforeEachStep"):
            before = self._capture(shot_dir, "step-%03d-before-%s.png" % (index, step_id))
            published = self._publish(before)
            if published:
                shots["before"] = published

        typing_mode = None
        key_delay_ms = None

        step = self._interpolate_step(step, state)
        kind = step.get("type")

        started = self._clock()
        rc, out, err = self._dispatch(step, settings)
        duration_ms = int((self._clock() - started) * 1000)

        ok = rc == 0
        captured = None
        if kind in ("python", "run_operation"):
            # Round 25: what the code printed and the nested operation's trace
            # belong in the step report like captured text - but not in the
            # texts archive, which stays page text only.
            captured = ((out or "")[:6000] or None)
        keep = 6000 if captured is not None else 800
        self._log("ok" if ok else "error",
                  "%s -> rc=%d in %dms" % (label, rc, duration_ms),
                  rc=rc, stdout=(out or "")[:keep], stderr=(err or "")[:800],
                  durationMs=duration_ms,
                  **({"typingMode": typing_mode, "keyDelayMs": key_delay_ms}
                     if typing_mode is not None else {}))

        if settings.get("screenshotAfterEachStep"):
            after = self._capture(shot_dir, "step-%03d-after-%s.png" % (index, step_id))
            published = self._publish(after)
            if published:
                shots["after"] = published
                image = png_size(os.path.join(shot_dir, published))
        if image is None and shots.get("after"):
            # a python screenshot() published into the row; size it like the
            # old screenshot step did
            image = png_size(os.path.join(shot_dir, shots["after"]))

        if not ok and settings.get("screenshotOnError", True) and not self._stop_requested:
            # The page at the moment of failure is the most useful image there
            # is, and stopOnError would otherwise end the run without one. A
            # stop the human asked for is not a failure, and photographing it
            # would delay the very thing they pressed the button for.
            failure = self._capture(shot_dir, "step-%03d-error-%s.png" % (index, step_id))
            published = self._publish(failure)
            if published:
                shots["error"] = published
                image = image or png_size(os.path.join(shot_dir, published))

        self._record(index, step, "ok" if ok else "error",
                     started_at=started, duration_ms=duration_ms, rc=rc,
                     error=None if ok else (err or out or "rc=%d" % rc)[:400],
                     screenshot=shots.get("after"), text=captured,
                     shots=shots or None, image=image,
                     typing_mode=typing_mode, key_delay_ms=key_delay_ms)

        if self._shots is not None:
            for role, name in shots.items():
                self._shots.add({
                    "name": name, "role": role, "runId": state.run_id,
                    "stepIndex": index, "stepLabel": label, "stepType": kind,
                    "image": image, "status": "ok" if ok else "error",
                })
        if captured is not None and kind == "capture_text" and self._texts is not None:
            self._texts.add({
                "text": captured, "source": "capture_text", "runId": state.run_id,
                "stepIndex": index, "stepLabel": label, "chars": len(captured),
            })

        if not ok and str(settings.get("errorOperation") or "").strip():
            # Round 25, phase 2: the failing step hands the desk to the error
            # protocol operation; when that succeeds and retryAfterProtocol is
            # set, the step is retried exactly once.
            protocol_ok = self._run_error_protocol(settings, index)
            if protocol_ok and settings.get("retryAfterProtocol"):
                self._log("info", "retrying step #%d after the error protocol" % index)
                rc, out, err = self._dispatch(step, settings)
                ok = rc == 0
                if ok:
                    self._log("info", "step #%d succeeded on the retry" % index)

        if not ok:
            if step.get("continueOnError"):
                self._log("warn", "continueOnError is set, ignoring failure of #%d" % index)
                self._record(index, step, "ignored", started_at=started,
                             duration_ms=duration_ms, rc=rc,
                             error=(err or out or "rc=%d" % rc)[:400])
            elif not settings.get("stopOnError", True):
                self._log("warn", "stopOnError is false, continuing after #%d" % index)
            else:
                with self._lock:
                    state.error = "step #%d (%s) failed with rc=%d: %s" % (index, label, rc, err or out)
                    state.status = "error"
                return

        delay = step.get("delayAfterMs", settings.get("defaultDelayAfterMs", 350))
        if delay:
            self._interruptible_sleep(delay / 1000.0)

    def page_text(self, limit: int = 2000) -> str:
        """The visible page text, for callers that only want to look.

        Used by the chat tab when the operator asks for context, and by
        nothing that can change anything.
        """
        rc, out, err = self._page_text()
        if rc != 0:
            return ""
        return (out or "")[:int(limit)]

    def _capture(self, shot_dir: str, name: str) -> Optional[str]:
        previous = getattr(self.backend, "screenshot_dir", None)
        try:
            self.backend.screenshot_dir = shot_dir
            rc, out, err = self.backend.screenshot(name)
        finally:
            self.backend.screenshot_dir = previous
        if rc != 0:
            self._log("warn", "screenshot failed: %s" % (err or out))
            return None
        return out.strip() or os.path.join(shot_dir, name)

    def _dispatch(self, step: Dict[str, Any], settings: Optional[Dict[str, Any]] = None) -> Tuple[int, str, str]:
        kind = step["type"]
        if kind == "python":
            return self._run_python(step)
        if kind == "run_operation":
            # Kept for structured operation steps inside old library rows;
            # flows themselves only ever arrive here as python (round 26).
            return self._run_operation_step(step, settings)
        return 1, "", ("unsupported step type: %s (round 26: every step "
                       "speaks python)" % kind)

    # -- round 25: python steps and nested operations -----------------------
    def _python_namespace(self) -> Tuple[Dict[str, Any], List[str]]:
        """The desk API handed to a python step.

        Every helper is a thin wrapper over the same backend calls the
        ordinary step types make, so a python step can do anything a flow can
        do - plus conditions, loops and variables. The ``vars`` dict survives
        to the next python step of the same run.
        """
        state = self._state
        py_vars = getattr(state, "py_vars", None)
        if py_vars is None:
            py_vars = {}
            if state is not None:
                state.py_vars = py_vars
        outputs: List[str] = []

        def log(message: Any = "") -> None:
            outputs.append(str(message))

        def screenshot(name: str = "") -> Tuple[int, str, str]:
            shot_dir = self._current_shot_dir or os.path.join(
                self.data_dir, "runs", state.run_id if state else "misc")
            shot_name = str(name or "").strip()
            if not shot_name:
                shot_name = "py-%d.png" % int(self._clock() * 1000)
            elif not shot_name.lower().endswith(".png"):
                shot_name = "step-%03d-%s.png" % (self._current_index,
                                                  shot_name)
            path = self._capture(shot_dir, shot_name)
            if not path:
                return 1, "", "screenshot failed"
            published = self._publish(path) or path
            if self._current_shots is not None:
                self._current_shots["after"] = published
            if self._shots is not None and state is not None:
                self._shots.add({
                    "name": os.path.basename(path), "role": "shot",
                    "runId": state.run_id, "stepIndex": self._current_index,
                    "stepLabel": self._current_step_label,
                    "stepType": "python", "image": png_size(path),
                    "status": "ok",
                })
            return 0, published, ""

        def capture_text(limit: int = 4000) -> Tuple[int, str, str]:
            rc, out, err = self._page_text()
            if rc != 0:
                return rc, "", err or "page text failed"
            text = (out or "")[:int(limit)]
            if self._texts is not None and state is not None:
                self._texts.add({
                    "text": text, "source": "capture_text",
                    "runId": state.run_id, "stepIndex": self._current_index,
                    "stepLabel": self._current_step_label, "chars": len(text),
                })
            return 0, text, ""

        def type_text(text: Any, mode: Optional[str] = None,
                      delay_ms: Optional[int] = None) -> Tuple[int, str, str]:
            settings_now: Dict[str, Any] = {}
            if state is not None:
                settings_now = (state.flow or {}).get("settings") or {}
            if delay_ms is None:
                resolved = str(mode or settings_now.get("typingMode")
                               or "normal")
                delay_ms = TYPING_DELAYS_MS.get(
                    resolved, TYPING_DELAYS_MS["normal"])
            return self.backend.type_text(str(text), int(delay_ms))

        def wait(seconds: float) -> None:
            if not self._interruptible_sleep(max(0.0, float(seconds))):
                raise _Stopped()

        def wait_for_text(text: str, timeout_ms: int = 15000,
                          absent: bool = False) -> Tuple[int, str, str]:
            return self._wait_for_text({
                "text": str(text), "timeoutMs": int(timeout_ms),
                "absent": bool(absent)})

        def human_verify(prompt: str) -> Tuple[int, str, str]:
            if state is None:
                return 1, "", "no run is active"
            handoff_step = {
                "type": "pause_for_human_verification", "id": "py",
                "prompt": str(prompt),
                "label": self._current_step_label or "human verification"}
            self._create_handoff(state, self._current_index, handoff_step,
                                 self._current_shot_dir or "",
                                 "manual_verification", str(prompt))
            return 0, "human verification confirmed", ""

        def form_collect(title: str, fields: Any, message: str = "",
                         submit_label: Optional[str] = None,
                         cancel_label: Optional[str] = None,
                         position: Optional[str] = None,
                         timeout_ms: Optional[int] = None,
                         output: Any = None) -> Tuple[int, Any, str]:
            form_step: Dict[str, Any] = {
                "type": "interactive_form", "title": str(title),
                "fields": list(fields or []),
                "label": self._current_step_label or str(title)}
            if message:
                form_step["message"] = str(message)
            if submit_label:
                form_step["submitLabel"] = str(submit_label)
            if cancel_label:
                form_step["cancelLabel"] = str(cancel_label)
            if position:
                form_step["position"] = str(position)
            if timeout_ms:
                form_step["timeoutMs"] = int(timeout_ms)
            if output is not None:
                form_step["output"] = output
            rc, out, err = self._run_interactive_form(
                state, self._current_index, form_step)
            if rc != 0:
                return rc, None, err or out or "the form was never completed"
            return 0, self._form_node(state, form_step.get("output"),
                                      self._current_index), ""

        def secure_collect(title: str, fields: Any, policy: Any,
                           message: str = "",
                           position: Optional[str] = None,
                           timeout_ms: Optional[int] = None,
                           output: Any = None) -> Tuple[int, Any, str]:
            sec_step: Dict[str, Any] = {
                "type": "secure_input", "title": str(title),
                "fields": list(fields or []), "policy": policy or {},
                "label": self._current_step_label or str(title)}
            if message:
                sec_step["message"] = str(message)
            if position:
                sec_step["position"] = str(position)
            if timeout_ms:
                sec_step["timeoutMs"] = int(timeout_ms)
            if output is not None:
                sec_step["output"] = output
            rc, out, err = self._run_secure_input(
                state, self._current_index, sec_step)
            if rc != 0:
                return rc, None, err or out or "no secret reached the vault"
            return 0, self._form_node(state, sec_step.get("output"),
                                      self._current_index, secure=True), ""

        def http_request(method: str, url: str,
                         handle: Optional[str] = None,
                         secret_ref: Optional[str] = None,
                         json_body: Any = None,
                         save_as: Optional[str] = None,
                         timeout: Optional[float] = None
                         ) -> Tuple[int, str, str]:
            req_step: Dict[str, Any] = {
                "type": "secure_http_request", "method": str(method),
                "url": str(url)}
            if handle:
                req_step["handle"] = str(handle)
            if secret_ref:
                req_step["secretRef"] = str(secret_ref)
            if json_body is not None:
                req_step["json"] = json_body
            if save_as:
                req_step["saveAs"] = save_as
            if timeout:
                req_step["timeout"] = timeout
            return self._run_secure_http(state, req_step)

        def notify_progress(title: str, message: str = "",
                            level: str = "info", duration_ms: int = 0,
                            dismissible: bool = True,
                            position: Optional[str] = None
                            ) -> Tuple[int, str, str]:
            notice_step: Dict[str, Any] = {
                "type": "progress_notice", "title": str(title),
                "message": str(message), "level": str(level),
                "durationMs": int(duration_ms or 0),
                "dismissible": bool(dismissible)}
            if position:
                notice_step["position"] = str(position)
            return self._run_progress_notice(notice_step)

        def open_agent(provider: Optional[str] = None,
                       url: Optional[str] = None) -> Tuple[int, Any, str]:
            rc, out, err = self._agent_open(
                {"type": "agent_open", "provider": provider, "url": url})
            if rc == 0:
                try:
                    return 0, json.loads(out), ""
                except ValueError:
                    pass
            return rc, out, err

        def ask_agent(prompt: str, provider: Optional[str] = None,
                      timeout: Optional[float] = None,
                      fresh_chat: Optional[bool] = None,
                      save_as: Any = False) -> Tuple[int, str, str]:
            return self._agent_ask({
                "type": "agent_ask", "prompt": str(prompt),
                "provider": provider, "timeout": timeout,
                "freshChat": fresh_chat, "saveAs": save_as})

        def solve_captcha(label: str = "", auto_click: Optional[bool] = None,
                          notify: Optional[bool] = None,
                          shot: str = "") -> Tuple[int, Any, str]:
            cap_step: Dict[str, Any] = {
                "type": "captcha_solve", "label": str(label or "captcha step")}
            if auto_click is not None:
                cap_step["autoClick"] = bool(auto_click)
            if notify is not None:
                cap_step["notify"] = bool(notify)
            if shot:
                cap_step["shot"] = str(shot)
            rc, out, err = self._captcha_solve(cap_step)
            if rc == 0:
                try:
                    return 0, json.loads(out), ""
                except ValueError:
                    pass
            return rc, out, err

        def shell(command: str) -> Tuple[int, str, str]:
            settings_now = (((state.flow or {}).get("settings")) or {}) \
                if state is not None else {}
            if not settings_now.get("allowShellSteps"):
                return 1, "", ("shell steps are disabled; set "
                               "settings.allowShellSteps to enable them")
            return self.backend.shell(str(command))

        def operations_list() -> Tuple[int, List[str], str]:
            """operations_list(): نام همهٔ عملیات‌های کتابخانه، برای انتخاب مغز."""
            library = getattr(self._agent, "library", None) \
                if self._agent is not None else None
            if library is None:
                return 1, [], "no agent library"
            try:
                names = [str(row.get("name") or "") for row in library.list()]
            except Exception as exc:
                return 1, [], str(exc)
            return 0, names, ""

        def brain_note(text: Any) -> Tuple[int, str, str]:
            """brain_note('...'): یک خط در آرشیو «تعامل با مغز» می‌نویسد
            (جایی که پاسخ مشورت‌ها برای استفادهٔ بعدی ثبت می‌شود)."""
            brain = getattr(self._agent, "brain", None) \
                if self._agent is not None else None
            if brain is None:
                return 1, "", "the brain is not attached to this engine"
            try:
                brain._speak(str(text))
            except Exception as exc:
                return 1, "", str(exc)
            return 0, "", ""

        def brain_suggest(answer: Any, why: str = "") -> Tuple[int, str, str]:
            """brain_suggest(پاسخ مشاوره): نام عملیات را در متن پیدا می‌کند و
            آن را در «پیشنهاد جاری» مغز می‌گذارد."""
            brain = getattr(self._agent, "brain", None) \
                if self._agent is not None else None
            if brain is None:
                return 1, "", "the brain is not attached to this engine"
            try:
                match = brain._match_operation(str(answer))
                if match is None:
                    brain._speak("عملیات بعدی در کتابخانه پیدا نشد؛ پاسخ مشاوره: %s"
                                 % str(answer)[:200])
                    return 0, "", "no operation matched the consultation answer"
                brain._suggest(match, self._clock())
            except Exception as exc:
                return 1, "", str(exc)
            return 0, str(match.get("name") or ""), ""

        def run_operation(operation: str) -> Tuple[int, str, str]:
            return self._run_operation_step({"operation": str(operation)},
                                            None)

        def check(result: Any) -> Any:
            """check(click(1, 2)): fail this step when the call failed."""
            if isinstance(result, tuple) and len(result) == 3:
                rc, out, err = result
                if int(rc or 0) != 0:
                    detail = str(err or "")
                    if err and out:
                        # keep the nested cause (the operation trace, the
                        # depth guard, ...) visible next to the refusal
                        detail = "%s | %s" % (detail,
                                              str(out).replace("\n", " / "))
                    raise RuntimeError(detail or str(out or ("rc=%s" % rc)))
                return out
            return result

        namespace: Dict[str, Any] = {
            "click": lambda x, y, button="left", clicks=1:
                self.backend.click(int(x), int(y), button, int(clicks)),
            "double_click": lambda x, y:
                self.backend.click(int(x), int(y), "left", 2),
            "move": lambda x, y: self.backend.move(int(x), int(y)),
            "drag": lambda x1, y1, x2, y2, button="left":
                self.backend.drag(int(x1), int(y1), int(x2), int(y2), button),
            "scroll": lambda amount, x=None, y=None:
                self.backend.scroll(int(amount), x, y),
            "type_text": type_text,
            "paste": lambda text: self.backend.paste(str(text)),
            "press_key": lambda keys: self.backend.key(
                keys if isinstance(keys, list) else [str(keys)]),
            "wait": wait,
            "sleep": wait,
            "goto": lambda url: self.backend.goto_url(str(url)),
            "focus_window": lambda title: self.backend.focus_window(str(title)),
            "screenshot": screenshot,
            "page_text": lambda limit=20000: self.page_text(int(limit)),
            "shell": shell,
            "ask_agent": ask_agent,
            "operations_list": operations_list,
            "brain_note": brain_note,
            "brain_suggest": brain_suggest,
            "run_operation": run_operation,
            "check": check,
            "wait_for_text": wait_for_text,
            "human_verify": human_verify,
            "form_collect": form_collect,
            "secure_collect": secure_collect,
            "http_request": http_request,
            "notify_progress": notify_progress,
            "open_agent": open_agent,
            "solve_captcha": solve_captcha,
            "capture_text": capture_text,
            "forms": state.form_outputs if state is not None else {},
            "log": log,
            "print": lambda *args: outputs.append(" ".join(str(a) for a in args)),
            "vars": py_vars,
            "stop_requested": lambda: bool(self._stop_requested),
        }
        return namespace, outputs

    def _run_python(self, step: Dict[str, Any]) -> Tuple[int, str, str]:
        """One python step: real code, on a worker thread, output captured."""
        code = str(step.get("code") or "")
        if not code.strip():
            return 1, "", "a python step needs code"
        try:
            timeout = float(step.get("timeout") or 0)
        except (TypeError, ValueError):
            timeout = 0.0
        namespace, outputs = self._python_namespace()
        holder: Dict[str, Any] = {}

        def target() -> None:
            # No redirect_stdout on purpose: it swaps sys.stdout for the whole
            # process, and an abandoned timeout worker would keep the real
            # stdout hostage. The namespace's own print/log capture instead.
            try:
                exec(compile(code, "<python step>", "exec"), namespace)
            except BaseException as exc:
                holder["exc"] = exc
                holder["error"] = "%s: %s\n%s" % (
                    type(exc).__name__, exc,
                    "".join(traceback.format_tb(exc.__traceback__))[-800:])

        worker = threading.Thread(target=target, name="python-step", daemon=True)
        worker.start()
        worker.join(timeout if timeout > 0 else None)
        if worker.is_alive():
            # Revoke the abandoned worker's world: with the namespace cleared
            # its next helper lookup dies instead of touching the desk.
            namespace.clear()
            return 1, "\n".join(outputs), (
                "python step timed out after %gs; the worker was abandoned "
                "and its helpers were revoked" % timeout)
        if isinstance(holder.get("exc"), _Stopped):
            # A stop (or a declined human handoff) inside a python step must
            # still stop the whole run, exactly like it did for old types.
            raise holder["exc"]
        if "error" in holder:
            return 1, "\n".join(outputs), holder["error"]
        return 0, "\n".join(outputs), ""

    def _run_operation_step(self, step: Dict[str, Any],
                            settings: Optional[Dict[str, Any]]) -> Tuple[int, str, str]:
        """Nest one library operation inside the running flow."""
        if self._agent is None:
            return self._no_agent()
        library = getattr(self._agent, "library", None)
        if library is None:
            return self._no_agent()
        wanted = str(step.get("operation") or "").strip()
        if not wanted:
            return 1, "", "run_operation needs an operation id or name"
        depth = getattr(self, "_op_depth", 0)
        if depth >= 3:
            return 1, "", "run_operation nested too deep (the limit is 3)"
        operation = None
        try:
            operation = library.get(wanted)
        except Exception:
            operation = None
        if operation is None:
            for row in library.list():
                if str(row.get("name") or "") == wanted:
                    operation = row
                    break
        if operation is None:
            return 1, "", "no operation with id or name %r" % wanted
        if str(operation.get("scope") or "general") == "brain":
            # round 26: brain-only tools never run inside an ordinary flow;
            # the brain itself starts them directly, not through run_operation
            return 1, "", ("عملیات «%s» اختصاصی مغز است (دامنه: مغز) و داخل "
                           "جریان‌های معمولی اجرا نمی‌شود"
                           % operation.get("name"))
        try:
            flow = convert_flow(library.as_flow(operation))
        except Exception as exc:
            return 1, "", str(exc)
        sub_steps = list(flow.get("steps") or [])
        self._log("info", "run_operation: «%s» (%d step(s))"
                  % (operation.get("name"), len(sub_steps)))
        self._op_depth = depth + 1
        lines: List[str] = []
        rc_final = 0
        try:
            for sub in sub_steps:
                if self._stop_requested:
                    rc_final = 1
                    lines.append("stopped before %s" % sub.get("type"))
                    break
                rc, out, err = self._dispatch(sub, settings)
                detail = err or out or ""
                if rc != 0 and err and out:
                    # Keep the nested cause visible: "operation failed" alone
                    # would hide why (the depth guard, a protocol refusal...).
                    detail = "%s | %s" % (err, out.replace("\n", " / "))
                lines.append("%s → rc=%d %s"
                             % (sub.get("label") or sub.get("type"), rc,
                                detail[:400]))
                if rc != 0:
                    rc_final = rc
                    break
        finally:
            self._op_depth = depth
        return rc_final, "\n".join(lines), (
            "" if rc_final == 0 else "operation %r failed" % wanted)

    def _run_error_protocol(self, settings: Dict[str, Any], index: int) -> bool:
        """The dedicated error protocol: one library operation as reaction."""
        name = str(settings.get("errorOperation") or "").strip()
        rc, out, err = self._run_operation_step({"operation": name}, settings)
        self._log("info" if rc == 0 else "warn",
                  "error protocol «%s» after step #%d: rc=%d %s"
                  % (name, index, rc, (err or out or "")[:200]))
        return rc == 0

    # -- steps that reach the coworker agent -------------------------------
    def _no_agent(self) -> Tuple[int, str, str]:
        return (1, "", "this deployment has no coworker agent attached"
                       " (it was started with --no-agent)")

    def _agent_open(self, step: Dict[str, Any]) -> Tuple[int, str, str]:
        if self._agent is None:
            return self._no_agent()
        try:
            result = self._agent.ai_open(provider=step.get("provider") or None,
                                         url=step.get("url") or None)
        except Exception as exc:  # noqa: BLE001 - one step failing is a step failing
            return 1, "", str(exc)
        return 0, json.dumps(result, ensure_ascii=False), ""

    def _agent_ask(self, step: Dict[str, Any]) -> Tuple[int, str, str]:
        if self._agent is None:
            return self._no_agent()
        try:
            result = self._agent.ai_ask(
                str(step.get("prompt") or ""),
                provider=step.get("provider") or None,
                timeout=float(step.get("timeout") or 0) or None,
                fresh_chat=None if step.get("freshChat") is None
                else bool(step.get("freshChat")))
        except Exception as exc:  # noqa: BLE001
            return 1, "", str(exc)
        text_out = str(result.get("text") or "")
        if step.get("saveAs"):
            # The answer becomes a note, so later steps and the operator can
            # both find it again.
            self._agent.add_note("%s\n%s" % (step.get("prompt") or "", text_out),
                                 kind="agent-answer", actor="flow")
        return 0, text_out, ""

    def _captcha_solve(self, step: Dict[str, Any]) -> Tuple[int, str, str]:
        if self._agent is None:
            return self._no_agent()
        context = {"label": step.get("label") or "captcha step"}
        if step.get("autoClick") is not None:
            context["autoClick"] = bool(step.get("autoClick"))
        if step.get("notify") is not None:
            context["notify"] = bool(step.get("notify"))
        try:
            result = self._agent.captcha_solve(shot_name=step.get("shot") or "",
                                               context=context)
        except Exception as exc:  # noqa: BLE001
            return 1, "", str(exc)
        return 0, json.dumps(result, ensure_ascii=False), 

    def _wait_for_text(self, step: Dict[str, Any]) -> Tuple[int, str, str]:
        needle = step["text"]
        want_absent = bool(step.get("absent", False))
        timeout_ms = int(step.get("timeoutMs", 15000))
        deadline = self._clock() + timeout_ms / 1000.0
        attempts = 0
        while True:
            attempts += 1
            rc, out, err = self._page_text()
            found = rc == 0 and needle.lower() in (out or "").lower()
            if found != want_absent:
                return 0, "matched after %d attempt(s)" % attempts, ""
            if self._clock() >= deadline:
                return 1, "", "text %r %s after %dms" % (
                    needle, "still present" if want_absent else "not found", timeout_ms)
            if not self._interruptible_sleep(0.5):
                return 1, "", "stopped while waiting for text"

    def _finish(self, status: str) -> None:
        with self._lock:
            assert self._state is not None
            self._state.status = status
            self._state.finished_at = self._clock()
            summary = {
                "runId": self._state.run_id,
                "flow": self._state.flow.get("name"),
                "status": status,
                "startedAt": self._state.started_at,
                "finishedAt": self._state.finished_at,
                "error": self._state.error,
                "steps": len(self._state.flow.get("steps", [])),
                "executed": len(self._state.results),
                "failed": sum(1 for r in self._state.results if r["status"] == "error"),
                "results": self._state.results,
            }
            self.history.append(summary)
            if len(self.history) > 100:
                self.history = self.history[-100:]
            self._log("info", "run finished: %s" % status)
            log_dir = os.path.join(self.data_dir, "runs", self._state.run_id)
            try:
                os.makedirs(log_dir, exist_ok=True)
                with open(os.path.join(log_dir, "log.jsonl"), "w", encoding="utf-8") as handle:
                    for entry in self._state.entries:
                        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
                with open(os.path.join(log_dir, "summary.json"), "w", encoding="utf-8") as handle:
                    json.dump(summary, handle, ensure_ascii=False, indent=2)
            except OSError:
                pass
        callback = getattr(self, "on_finished", None)
        if callback is not None:
            try:
                callback(str(summary.get("flow") or ""), str(status),
                         str(summary.get("error") or ""))
            except Exception:  # noqa: BLE001 - routing must never break a run
                pass


def png_size(path: str) -> Optional[Dict[str, int]]:
    """Width and height of a PNG, read from IHDR so no image library is needed."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(26)
    except OSError:
        return None
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    width, height = struct.unpack(">II", head[16:24])
    return {"width": int(width), "height": int(height)}


class _Stopped(Exception):
    pass


class _Failed(Exception):
    pass


def quote_command(args: List[str]) -> str:
    return " ".join(shlex.quote(str(a)) for a in args)
