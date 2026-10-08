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
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_DISPLAY = ":1"

TERMINAL_STATES = ("done", "error", "stopped", "cancelled")


class ControlBackend:
    """Runs the primitives exposed by browser_control.sh."""

    def __init__(self, script_path: str, display: str = DEFAULT_DISPLAY,
                 screenshot_dir: Optional[str] = None, timeout: float = 60.0):
        self.script_path = script_path
        self.display = display
        self.screenshot_dir = screenshot_dir
        self.timeout = timeout
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

    def type_text(self, text: str) -> Tuple[int, str, str]:
        return self.control(["type", text])

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
        return self.control(["url", url])

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
        try:
            proc = subprocess.run(["xclip", "-selection", "clipboard", "-o"],
                                  env=self.env(), capture_output=True, text=True,
                                  timeout=10, check=False)
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            return 1, "", "clipboard read failed: %s" % exc
        self.control(["key", "Escape"])
        return proc.returncode, proc.stdout, proc.stderr


class RunState:
    def __init__(self, run_id: str, flow: Dict[str, Any]):
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
        }


class AutomationEngine:
    """Single-flow runner with pause/stop/confirmation support."""

    def __init__(self, backend: ControlBackend, data_dir: str, sleep=time.sleep,
                 clock=time.time, cdp: Any = None, cdp_port: int = 9222):
        self.backend = backend
        self.data_dir = data_dir
        self._cdp = cdp
        self._cdp_port = cdp_port
        self._cdp_warned = False
        # Screenshots are copied here so a run report can link them without a
        # token, which is what lets the model actually look at the page.
        self.public_dir = os.path.join(data_dir, "public")
        self._sleep = sleep
        self._clock = clock
        self._lock = threading.RLock()
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
        with self._lock:
            if self.busy():
                raise RuntimeError("a flow is already running (run %s)" % self._state.run_id)
            run_id = run_id or "run-%d" % int(self._clock() * 1000)
            self._state = RunState(run_id, flow)
            self._paused = False
            self._stop_requested = False
            self._confirmed = None
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

    def _record(self, index: int, step: Dict[str, Any], status: str,
                started_at: Optional[float] = None, duration_ms: int = 0,
                rc: Optional[int] = None, error: Optional[str] = None,
                screenshot: Optional[str] = None, text: Optional[str] = None,
                shots: Optional[Dict[str, str]] = None,
                image: Optional[Dict[str, int]] = None) -> None:
        """Append one machine-readable row about a step's outcome."""
        with self._lock:
            assert self._state is not None
            self._state.results.append({
                "index": index,
                "passIndex": self._state.pass_index,
                "type": step.get("type"),
                "label": step.get("label") or step.get("type"),
                "stepId": step.get("id"),
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
                    self._execute_step(state, index, step, shot_dir, settings)
                    with self._lock:
                        if state.status == "error":
                            raise _Failed(state.error or "step failed")
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

    def _execute_step(self, state: RunState, index: int, step: Dict[str, Any],
                      shot_dir: str, settings: Dict[str, Any]) -> None:
        label = step.get("label") or step.get("type")
        self._log("step", "#%d %s" % (index, label), stepId=step.get("id"), type=step.get("type"))

        if step.get("requiresConfirmation"):
            with self._lock:
                self._confirmed = None
                self._wakeup.clear()
                state.awaiting_confirmation = {"index": index, "label": label, "type": step.get("type")}
                state.status = "waiting"
            self._log("warn", "waiting for human confirmation before: %s" % label)
            while True:
                with self._lock:
                    if self._stop_requested:
                        state.awaiting_confirmation = None
                        state.status = "running"
                        raise _Stopped()
                    if self._confirmed is not None:
                        approved = self._confirmed
                        state.awaiting_confirmation = None
                        state.status = "running"
                        if not approved:
                            self._log("warn", "confirmation refused; stopping before: %s" % label)
                            raise _Stopped()
                        break
                self._wakeup.wait(0.2)
                self._wakeup.clear()

        shots: Dict[str, str] = {}
        image = None
        step_id = step.get("id", "s")
        kind = step.get("type")

        if settings.get("screenshotBeforeEachStep"):
            before = self._capture(shot_dir, "step-%03d-before-%s.png" % (index, step_id))
            published = self._publish(before)
            if published:
                shots["before"] = published

        started = self._clock()
        rc, out, err = self._dispatch(step)
        duration_ms = int((self._clock() - started) * 1000)

        ok = rc == 0
        captured = None
        if kind == "capture_text":
            limit = int(step.get("limit") or 0) or 4000
            captured = (out or "")[:limit]
        keep = 6000 if captured is not None else 800
        self._log("ok" if ok else "error",
                  "%s -> rc=%d in %dms" % (label, rc, duration_ms),
                  rc=rc, stdout=(out or "")[:keep], stderr=(err or "")[:800],
                  durationMs=duration_ms)

        if kind == "screenshot" and ok and out:
            published = self._publish(out)
            if published:
                shots["after"] = published
                image = png_size(os.path.join(shot_dir, published))
        elif settings.get("screenshotAfterEachStep"):
            after = self._capture(shot_dir, "step-%03d-after-%s.png" % (index, step_id))
            published = self._publish(after)
            if published:
                shots["after"] = published
                image = png_size(os.path.join(shot_dir, published))

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
                     shots=shots or None, image=image)

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

    def _dispatch(self, step: Dict[str, Any]) -> Tuple[int, str, str]:
        kind = step["type"]
        if kind == "click":
            return self.backend.click(int(step["x"]), int(step["y"]),
                                      step.get("button", "left"), int(step.get("clicks", 1)))
        if kind == "double_click":
            return self.backend.click(int(step["x"]), int(step["y"]), "left", 2)
        if kind == "move":
            return self.backend.move(int(step["x"]), int(step["y"]))
        if kind == "drag":
            return self.backend.drag(int(step["x1"]), int(step["y1"]),
                                     int(step["x2"]), int(step["y2"]), step.get("button", "left"))
        if kind == "type":
            return self.backend.type_text(step["text"])
        if kind == "paste":
            return self.backend.paste(step["text"])
        if kind == "key":
            return self.backend.key(step["keys"])
        if kind == "scroll":
            return self.backend.scroll(int(step["amount"]), step.get("x"), step.get("y"))
        if kind == "wait":
            self._interruptible_sleep(int(step["ms"]) / 1000.0)
            return 0, "", ""
        if kind == "wait_for_text":
            return self._wait_for_text(step)
        if kind == "capture_text":
            rc, out, err = self._page_text()
            if rc != 0:
                return rc, "", err or "could not read the page text"
            return 0, out or "", ""
        if kind == "goto_url":
            return self.backend.goto_url(step["url"])
        if kind == "focus_window":
            return self.backend.focus_window(step["title"])
        if kind == "screenshot":
            name = step.get("name") or "shot-%d.png" % int(self._clock())
            path = self._capture(os.path.join(self.data_dir, "runs",
                                              self._state.run_id if self._state else "misc"), name)
            return (0, path or "", "") if path else (1, "", "screenshot failed")
        if kind == "shell":
            return self.backend.shell(step["command"])
        return 1, "", "unsupported step type: %s" % kind

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
