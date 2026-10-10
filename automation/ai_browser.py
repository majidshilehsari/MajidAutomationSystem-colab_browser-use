"""A second browser the agent drives to talk to a web-based AI, no API key.

The operator asked for the agent to get its answers from a chat website
(DeepSeek) instead of a paid API. That means the agent needs a browser of its
own, and it must not be the browser the automation is working in:

* The automation's Chrome owns the single mouse and keyboard of display `:1`,
  and `browser_control.sh url` activates windows by the title "Google Chrome".
  A second window with that title on the same display would make the wrong
  window come to the front and silently corrupt the run.
* So the agent's browser lives on its **own display** (`:2` by default) with
  its **own profile directory** and its **own debugging port** (9223). The main
  automation cannot see it and it cannot see the main automation.

Everything is driven over the DevTools protocol with the same dependency-free
WebSocket client the project already uses for the main browser
(`automation/cdp.py`), so no new package is needed.

Honest limitation, stated up front: a chat website's markup is not an API
contract. It changes without notice, and every provider here is described by a
list of *candidate* selectors that the operator can edit from the sidebar
without touching code. When nothing matches, the failure message includes what
the page actually looked like, because that is the only thing that makes it
fixable from the outside.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from typing import Any, Dict, List, Optional


class AiBrowserError(Exception):
    """The agent's browser could not do what was asked."""


# Candidate selectors, tried in order. Editing these in the sidebar
# (ai.provider setting) is the intended way to repair a markup change.
DEFAULT_PROVIDERS: Dict[str, Dict[str, Any]] = {
    "deepseek": {
        "url": "https://chat.deepseek.com/",
        "inputs": ["#chat-input", "textarea", "[contenteditable='true']"],
        "submitWithEnter": True,
        "sendButtons": ["button[type=submit]", "button[aria-label*=send i]",
                        "div[role=button][aria-label*=send i]"],
        "answers": [".ds-markdown", "[class*=markdown]", "[class*=message-content]"],
        "newChatButtons": ["a[href='/']", "button[aria-label*=new i]",
                           "[class*=new-chat]"],
    },
    "generic": {
        "url": "",
        "inputs": ["textarea", "[contenteditable='true']"],
        "submitWithEnter": True,
        "sendButtons": ["button[type=submit]"],
        "answers": ["[class*=markdown]", "[class*=message]"],
        "newChatButtons": [],
    },
}

# React and friends ignore a plain `value =` assignment; the native setter has
# to be used and an `input` event dispatched, or the send button stays disabled.
_INJECT_JS = r"""
(function () {
  var inputs = %(inputs)s;
  var buttons = %(buttons)s;
  var withEnter = %(with_enter)s;
  var text = %(text)s;
  var field = null;
  var used = '';
  for (var i = 0; i < inputs.length; i++) {
    field = document.querySelector(inputs[i]);
    if (field) { used = inputs[i]; break; }
  }
  if (!field) {
    return JSON.stringify({
      ok: false,
      reason: 'no input element matched',
      tried: inputs,
      title: document.title,
      url: location.href,
      bodyStart: (document.body ? document.body.innerText : '').slice(0, 400)
    });
  }
  field.focus();
  if (field.tagName === 'TEXTAREA' || field.tagName === 'INPUT') {
    var proto = field.tagName === 'TEXTAREA'
      ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
    var setter = Object.getOwnPropertyDescriptor(proto, 'value');
    if (setter && setter.set) { setter.set.call(field, text); }
    else { field.value = text; }
  } else {
    field.textContent = text;
  }
  field.dispatchEvent(new Event('input', { bubbles: true }));
  field.dispatchEvent(new Event('change', { bubbles: true }));
  var how = '';
  if (withEnter) {
    ['keydown', 'keypress', 'keyup'].forEach(function (type) {
      field.dispatchEvent(new KeyboardEvent(type, {
        key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true
      }));
    });
    how = 'enter';
  }
  var clicked = false;
  for (var b = 0; b < buttons.length; b++) {
    var node = document.querySelector(buttons[b]);
    if (node && !node.disabled) { node.click(); clicked = true; how = how || 'button'; break; }
  }
  if (!withEnter && !clicked) {
    return JSON.stringify({ ok: false, reason: 'nothing to submit with', tried: buttons });
  }
  return JSON.stringify({ ok: true, selector: used, how: how,
                          url: location.href, title: document.title });
})()
"""

# Reads the answers currently on the page as a JSON array of strings.
_READ_JS = r"""
(function () {
  var selectors = %(answers)s;
  var nodes = [];
  for (var i = 0; i < selectors.length; i++) {
    nodes = document.querySelectorAll(selectors[i]);
    if (nodes.length) { break; }
  }
  var out = [];
  for (var n = 0; n < nodes.length; n++) {
    out.push((nodes[n].innerText || '').trim());
  }
  return JSON.stringify({ count: out.length, texts: out,
                          url: location.href, title: document.title,
                          used: selectors.length ? selectors[0] : '' });
})()
"""


def _js_list(values: List[str]) -> str:
    return json.dumps(list(values))


class AiBrowser:
    """Drives the agent's own Chrome through CDP on a private display."""

    def __init__(self, cdp: Any, port: int = 9223, display: str = ":2",
                 providers: Optional[Dict[str, Dict[str, Any]]] = None,
                 clock: Any = time.time, sleep: Any = time.sleep,
                 runner: Optional[Any] = None) -> None:
        self.cdp = cdp
        self.port = port
        self.display = display
        self.providers = dict(DEFAULT_PROVIDERS)
        if providers:
            for name, profile in providers.items():
                merged = dict(DEFAULT_PROVIDERS.get(name)
                              or DEFAULT_PROVIDERS["generic"])
                merged.update(profile or {})
                self.providers[name] = merged
        self._clock = clock
        self._sleep = sleep
        # Optional ControlBackend bound to this display, so the operator can
        # log in to the chat site once by looking at a screenshot and clicking.
        self.runner = runner

    # -- provider profiles ---------------------------------------------
    def profile(self, name: str) -> Dict[str, Any]:
        return self.providers.get(name) or self.providers["generic"]

    # -- availability ---------------------------------------------------
    def available(self) -> bool:
        try:
            self.cdp.list_targets(port=self.port)
        except Exception:  # noqa: BLE001 - any failure means "not running"
            return False
        return True

    def status(self, name: str = "deepseek") -> Dict[str, Any]:
        profile = self.profile(name)
        info: Dict[str, Any] = {
            "provider": name, "port": self.port, "display": self.display,
            "url": profile.get("url", ""), "available": False,
            "pageUrl": "", "pageTitle": "", "answers": 0,
        }
        try:
            read = self._read(profile)
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            info["error"] = str(exc)
            return info
        info.update({"available": True, "pageUrl": read.get("url", ""),
                     "pageTitle": read.get("title", ""),
                     "answers": read.get("count", 0)})
        return info

    def _evaluate(self, expression: str, timeout: float = 10.0) -> Any:
        try:
            result = self.cdp.evaluate(expression, port=self.port, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - CdpError and socket errors alike
            raise AiBrowserError(
                "cannot talk to the agent browser on port %d (%s). Start it with "
                "AI_BROWSER=1, or switch the provider to an HTTP API." % (self.port, exc))
        return result.get("value")

    def _read(self, profile: Dict[str, Any]) -> Dict[str, Any]:
        raw = self._evaluate(_READ_JS % {
            "answers": _js_list(profile.get("answers") or [])})
        try:
            return json.loads(raw) if isinstance(raw, str) else (raw or {})
        except ValueError:
            return {}

    # -- navigation -----------------------------------------------------
    def open_provider(self, name: str = "deepseek",
                        url: Optional[str] = None) -> Dict[str, Any]:
        profile = self.profile(name)
        url = url or profile.get("url") or ""
        if not url:
            raise AiBrowserError("provider %r has no url configured" % name)
        if self.runner is not None:
            self.runner.goto_url(url)
        else:
            self._evaluate("location.href = %s" % json.dumps(url))
        return {"opened": url}

    def new_chat(self, name: str = "deepseek") -> bool:
        """Best-effort: a fresh conversation keeps the context from growing."""
        profile = self.profile(name)
        buttons = profile.get("newChatButtons") or []
        if not buttons:
            return False
        raw = self._evaluate(r"""
        (function () {
          var selectors = %s;
          for (var i = 0; i < selectors.length; i++) {
            var node = document.querySelector(selectors[i]);
            if (node) { node.click(); return JSON.stringify({ok:true, used:selectors[i]}); }
          }
          return JSON.stringify({ok:false});
        })()
        """ % _js_list(buttons))
        try:
            return bool(json.loads(raw).get("ok")) if isinstance(raw, str) else False
        except ValueError:
            return False

    # -- asking ---------------------------------------------------------
    def attach_image(self, path: str) -> bool:
        """Put a PNG on the clipboard of this display and paste it into the chat.

        Web chats accept an image from the clipboard, and the DevTools protocol
        cannot set a file input without DOM.setFileInputFiles, so the clipboard
        is the route that works with the tools already installed (xclip and
        xdotool). Returns False rather than raising when the paste could not be
        confirmed - the caller still sends the public URL as text.
        """
        if not path or not os.path.exists(path):
            return False
        env = dict(os.environ)
        env["DISPLAY"] = self.display
        try:
            subprocess.run(["xclip", "-selection", "clipboard", "-t", "image/png",
                            "-i", path], env=env, check=False, timeout=15,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["xdotool", "key", "--clearmodifiers", "ctrl+v"],
                           env=env, check=False, timeout=15,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            return False
        return True

    def ask(self, prompt: str, name: str = "deepseek", timeout: float = 120.0,
            fresh_chat: bool = True, image_path: str = "") -> Dict[str, Any]:
        """Send a prompt and wait for the answer to finish streaming.

        Completion is detected without any provider-specific signal: the last
        answer is polled until its text stops changing for two reads in a row,
        which works for any chat UI that renders the reply incrementally.
        """
        if not self.available():
            raise AiBrowserError(
                "the agent browser is not reachable on port %d. Start it with "
                "AI_BROWSER=1 (the Hostim entrypoint does this by default), or "
                "switch ai.provider to http-api and set an API key."
                % self.port)
        profile = self.profile(name)
        started = self._clock()

        if fresh_chat:
            self.new_chat(name)
            deadline = started + 8.0
            while self._clock() < deadline:
                if not self._read(profile).get("count"):
                    break
                self._sleep(0.4)

        baseline = self._read(profile)
        base_count = int(baseline.get("count") or 0)
        base_last = (baseline.get("texts") or [""])[-1] if base_count else ""

        if image_path:
            self.attach_image(image_path)

        raw = self._evaluate(_INJECT_JS % {
            "inputs": _js_list(profile.get("inputs") or []),
            "buttons": _js_list(profile.get("sendButtons") or []),
            "with_enter": "true" if profile.get("submitWithEnter", True) else "false",
            "text": json.dumps(prompt),
        })
        try:
            injected = json.loads(raw) if isinstance(raw, str) else {}
        except ValueError:
            injected = {}
        if not injected.get("ok"):
            raise AiBrowserError("could not submit the prompt: %s" % json.dumps(
                injected, ensure_ascii=False)[:800])

        deadline = started + timeout
        last_text = ""
        stable = 0
        answer = ""
        while self._clock() < deadline:
            self._sleep(1.0)
            state = self._read(profile)
            count = int(state.get("count") or 0)
            texts = state.get("texts") or []
            current = texts[-1] if texts else ""
            # A new answer is the one that appeared after the prompt was sent.
            if count > base_count:
                answer = current
            elif count == base_count and current and current != base_last:
                answer = current
            if answer and answer == last_text:
                stable += 1
                if stable >= 2:
                    return {"text": answer, "url": state.get("url", ""),
                            "title": state.get("title", ""),
                            "elapsed": round(self._clock() - started, 1),
                            "answers": count}
            else:
                stable = 0
            last_text = answer or current
        if answer:
            return {"text": answer, "url": "", "title": "",
                    "elapsed": round(self._clock() - started, 1),
                    "answers": base_count, "timedOut": True}
        raise AiBrowserError(
            "the prompt was submitted but no answer appeared within %.0fs "
            "(page: %s)" % (timeout, baseline.get("url", "?")))

    # -- operator view --------------------------------------------------
    def screenshot(self, path: str) -> str:
        """Grab the agent's display so the operator can log in once, by hand."""
        if self.runner is not None:
            self.runner.control(["screenshot", path])
            return path
        env = dict(os.environ)
        env["DISPLAY"] = self.display
        try:
            subprocess.run(["scrot", "-o", path], env=env, check=True, timeout=20,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as exc:
            raise AiBrowserError("could not capture the agent display: %s" % exc)
        return path
