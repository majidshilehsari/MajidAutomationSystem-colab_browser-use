"""Running code the agent wrote, behind an explicit human approval.

The operator asked for the agent to be able to write code and take part in the
automation, and separately chose that execution needs their approval. Both are
honoured here, and the gate is not decorative:

* A script starts life ``pending``. It runs only when its status is
  ``approved``, and `AgentStore.update_script` drops an approved script back to
  ``pending`` the moment its code changes - otherwise the agent could get one
  body signed off and then swap in another.
* The kill switch stops every execution, including one already queued.
* Only ``python3`` and ``bash`` are accepted, so a job cannot ask for an
  arbitrary binary by name.
* The child process gets a **sanitised** environment. The platform's own secrets
  (`AUTOMATION_TOKEN`, `VNC_PASSWORD`) are removed, because code written by a
  model that has been reading untrusted web pages should not inherit credentials
  by accident. Opting back in is a deliberate setting, off by default.
* Output is capped, and the exit code plus a truncated transcript are stored and
  audited, so "what did it actually do" always has an answer.

This is not a sandbox. It runs in the same container, as the same user, with
network access. The real protections are the approval gate, the audit trail and
the kill switch; anyone needing isolation should run scripts in a separate
container.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional


LANGUAGES = {
    "python3": ["python3", "-I", "-u", "-c"],
    "bash": ["bash", "--noprofile", "--norc", "-c"],
}

# Everything the container was given that a script has no business inheriting.
STRIPPED_ENV = ("AUTOMATION_TOKEN", "VNC_PASSWORD", "HOSTIM_TOKEN",
                "TELEGRAM_BOT_TOKEN", "AI_API_KEY")

MAX_OUTPUT = 40000


class ScriptError(Exception):
    """A refused script action, with the HTTP status it should produce.

    The status is carried by the exception on purpose: deriving it from the
    wording of the message means a reworded sentence silently changes the
    response code, which is how the approval gate ended up answering 400
    instead of 409.
    """

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def build_env(base: Optional[Dict[str, str]] = None, *, data_dir: str = "",
              api_base: str = "", script_id: str = "", display: str = ":1",
              pass_token: bool = False, token: str = "") -> Dict[str, str]:
    """The environment a script actually gets."""
    env = dict(base if base is not None else os.environ)
    for name in STRIPPED_ENV:
        env.pop(name, None)
    env["AUTOMATION_DATA_DIR"] = data_dir
    env["AUTOMATION_SCRIPT_ID"] = script_id
    env["DISPLAY"] = display
    if api_base:
        # Lets a script drive the automation through the same API the sidebar
        # uses, without ever seeing a credential it was not given.
        env["AUTOMATION_API_BASE"] = api_base
    if pass_token and token:
        env["AUTOMATION_TOKEN"] = token
    return env


class ScriptRunner:
    """Executes approved scripts, one at a time, with a transcript."""

    def __init__(self, store: Any, clock: Any = time.time,
                 env_base: Optional[Dict[str, str]] = None,
                 data_dir: str = "", api_base: str = "", display: str = ":1",
                 runner: Optional[Any] = None) -> None:
        self.store = store
        self._clock = clock
        self._env_base = env_base
        self.data_dir = data_dir or getattr(store, "data_dir", "")
        self.api_base = api_base
        self.display = display
        self.runner = runner
        self._lock = threading.Lock()
        self._process: Optional[subprocess.Popen] = None
        self.running_id: Optional[str] = None

    # -- configuration --------------------------------------------------
    def config(self) -> Dict[str, Any]:
        return {
            "passToken": bool(self.store.get_setting("scripts.passToken", False)),
            "defaultTimeout": float(self.store.get_setting("scripts.timeout", 60)),
            "languages": sorted(LANGUAGES),
            "strippedEnv": list(STRIPPED_ENV),
            "running": self.running_id,
        }

    # -- execution ------------------------------------------------------
    def run(self, script_id: str, actor: str = "operator",
            timeout: Optional[float] = None) -> Dict[str, Any]:
        script = self.store.get_script(script_id)
        if script is None:
            raise ScriptError("no script with id %s" % script_id, 404)
        if self.store.kill_switch_engaged():
            raise ScriptError("the kill switch is on: scripts may not run", 409)
        if str(script.get("status")) != "approved":
            # This is the gate. The message names what has to happen next.
            raise ScriptError(
                "script %s is %s; approve it before running it"
                % (script_id, script.get("status")), 409)
        language = str(script.get("language") or "python3")
        if language not in LANGUAGES:
            raise ScriptError("unsupported language: %s (allowed: %s)"
                              % (language, ", ".join(sorted(LANGUAGES))))
        code = str(script.get("code") or "")
        if not code.strip():
            raise ScriptError("the script is empty")

        if not self._lock.acquire(blocking=False):
            raise ScriptError("another script is already running (%s)"
                              % self.running_id, 409)
        limit = float(timeout if timeout is not None
                      else (script.get("timeout") or 60))
        limit = max(1.0, min(limit, 3600.0))
        env = build_env(self._env_base, data_dir=self.data_dir,
                        api_base=self.api_base, script_id=script_id,
                        display=self.display,
                        pass_token=bool(self.store.get_setting("scripts.passToken",
                                                               False)),
                        token=self.store.get_secret("automation.token", ""))
        argv = list(LANGUAGES[language]) + [code]
        self.store.audit(actor, "script.run.started",
                         "%s (%s, timeout=%.0fs)" % (script_id, language, limit))
        started = self._clock()
        self.running_id = script_id
        try:
            process = subprocess.Popen(argv, env=env, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True,
                                       cwd=self.data_dir or None)
            self._process = process
            try:
                output, _ = process.communicate(timeout=limit)
                exit_code = process.returncode
            except subprocess.TimeoutExpired:
                self._kill(process)
                output, _ = process.communicate(timeout=5)
                output = "%s\n[killed after %.0fs: timeout]" % (output or "", limit)
                exit_code = 124
        except OSError as exc:
            output, exit_code = "could not start %s: %s" % (language, exc), 127
        finally:
            self._process = None
            self.running_id = None
            self._lock.release()

        transcript = (output or "")[-MAX_OUTPUT:]
        elapsed = round(self._clock() - started, 2)
        self.store.record_script_run(script_id, exit_code, transcript)
        self.store.audit(actor, "script.run.finished",
                         "%s exit=%s elapsed=%.2fs" % (script_id, exit_code, elapsed))
        return {"id": script_id, "exitCode": exit_code, "output": transcript,
                "elapsed": elapsed, "language": language,
                "status": "ok" if exit_code == 0 else "failed"}

    @staticmethod
    def _kill(process: subprocess.Popen) -> None:
        try:
            process.kill()
        except OSError:
            pass

    def kill_running(self, actor: str = "operator") -> Dict[str, Any]:
        """The kill switch for whatever is executing right now."""
        process = self._process
        script_id = self.running_id
        if process is None:
            return {"killed": False, "reason": "nothing is running"}
        self._kill(process)
        self.store.audit(actor, "script.run.killed", str(script_id))
        return {"killed": True, "id": script_id}

    # -- the approval workflow ------------------------------------------
    def propose(self, name: str, code: str, language: str = "python3",
                timeout: float = 60, actor: str = "agent") -> Dict[str, Any]:
        """What the agent calls. Always lands in `pending`, never approved."""
        if language not in LANGUAGES:
            raise ScriptError("unsupported language: %s" % language, 400)
        return self.store.create_script({"name": name, "code": code,
                                         "language": language, "timeout": timeout},
                                        actor=actor)

    def approve(self, script_id: str, actor: str = "operator") -> Dict[str, Any]:
        updated = self.store.set_script_status(script_id, "approved", actor=actor)
        if updated is None:
            raise ScriptError("no script with id %s" % script_id, 404)
        return updated

    def reject(self, script_id: str, actor: str = "operator") -> Dict[str, Any]:
        updated = self.store.set_script_status(script_id, "rejected", actor=actor)
        if updated is None:
            raise ScriptError("no script with id %s" % script_id, 404)
        return updated

    def pending(self) -> List[Dict[str, Any]]:
        return [script for script in self.store.list_scripts()
                if str(script.get("status")) == "pending"]
