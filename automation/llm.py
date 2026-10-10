"""One interface over two very different ways of asking a model something.

* **ai-browser** (the default, and the one the operator asked for): drive the
  agent's own Chrome to a chat website and read the answer off the page. No API
  key, no paid account, no cost per call - the price is that it is slow and that
  a markup change can break it.
* **http-api**: any OpenAI-compatible endpoint, key stored 0600 outside the
  database. Faster, scriptable, and the only mode that reliably carries an image
  as data rather than as a clipboard paste.

Both return plain text plus a little metadata, and both go through the same
`complete()` so the captcha chain and the assistant never need to know which one
is configured. `extract_json()` is shared too, because every model wraps JSON in
markdown fences some of the time.
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional


PROVIDER_BROWSER = "ai-browser"
PROVIDER_HTTP = "http-api"
PROVIDERS = (PROVIDER_BROWSER, PROVIDER_HTTP)

# A reply that has to be machine-readable gets this appended, because "return
# JSON" alone is not enough for most models.
JSON_SUFFIX = (
    "\n\nفقط یک آبجکت JSON معتبر برگردان، بدون هیچ توضیح اضافه و بدون "
    "حصار markdown (```). هیچ کلیدی جز آنچه خواسته شده اضافه نکن."
)

DEFAULT_TIMEOUT = 180.0


class LlmError(Exception):
    """No answer could be obtained. The message is safe to show the operator."""


def extract_json(text: str) -> Optional[Any]:
    """Pull the first JSON object or array out of a model's reply.

    Models wrap JSON in ```json fences, add a sentence before it, or both. This
    tries progressively more forgiving strategies rather than failing on the
    first cosmetic difference.
    """
    if not text:
        return None
    candidate = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.+?)```", candidate, re.DOTALL)
    if fenced:
        candidate = fenced.group(1).strip()
    for attempt in (candidate, text.strip()):
        try:
            return json.loads(attempt)
        except ValueError:
            pass
    # Fall back to the outermost braces/brackets in the reply.
    for open_char, close_char in (("{", "}"), ("[", "]")):
        start = text.find(open_char)
        end = text.rfind(close_char)
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except ValueError:
                continue
    return None


class LlmClient:
    """Asks a model, by browser or by HTTP, and hands back text."""

    def __init__(self, store: Any, ai_browser: Any = None,
                 opener: Optional[Any] = None, clock: Any = time.time) -> None:
        self.store = store
        self.ai_browser = ai_browser
        self._opener = opener or urllib.request.urlopen
        self._clock = clock

    # -- configuration --------------------------------------------------
    def provider(self) -> str:
        value = str(self.store.get_setting("ai.provider", PROVIDER_BROWSER) or "")
        return value if value in PROVIDERS else PROVIDER_BROWSER

    def chat_provider(self) -> str:
        """Which chat website the browser mode drives."""
        return str(self.store.get_setting("ai.chatProvider", "deepseek") or "deepseek")

    def configured(self) -> bool:
        if self.provider() == PROVIDER_HTTP:
            return bool(self.store.get_secret("ai.apiKey", ""))
        return self.ai_browser is not None and self.ai_browser.available()

    def describe(self) -> Dict[str, Any]:
        """What the sidebar shows: enough to diagnose, never a key."""
        provider = self.provider()
        info: Dict[str, Any] = {
            "provider": provider,
            "chatProvider": self.chat_provider(),
            "configured": self.configured(),
            "model": self.store.get_setting("ai.model", ""),
            "baseUrl": self.store.get_setting("ai.baseUrl", ""),
        }
        if provider == PROVIDER_HTTP:
            info["keySet"] = bool(self.store.get_secret("ai.apiKey", ""))
        elif self.ai_browser is not None:
            # Deliberately shallow: `overview()` is called on every tab switch,
            # so it must not open a WebSocket to the agent's browser. The full
            # status lives behind GET /agent/ai/status.
            info["browser"] = {"available": self.ai_browser.available(),
                               "port": getattr(self.ai_browser, "port", None),
                               "display": getattr(self.ai_browser, "display", None)}
        return info

    # -- asking ---------------------------------------------------------
    def chat_names(self) -> List[str]:
        """The choices the chat tab offers.

        In browser mode these are the provider profiles (deepseek today,
        anything added to `ai.providers` tomorrow); in HTTP mode there is
        one entry, the configured model.
        """
        if self.provider() == PROVIDER_HTTP:
            model = str(self.store.get_setting("ai.model", "") or "http-api")
            return [model]
        if self.ai_browser is not None:
            return list(self.ai_browser.providers.keys())
        return [self.chat_provider()]

    def complete(self, prompt: str, system: str = "", images: Optional[List[str]] = None,
                 want_json: bool = False, timeout: Optional[float] = None,
                 chat_name: Optional[str] = None) -> Dict[str, Any]:
        text = prompt if not want_json else prompt + JSON_SUFFIX
        if system:
            text = "%s\n\n%s" % (system, text)
        provider = self.provider()
        if provider == PROVIDER_HTTP:
            return self._complete_http(text, images or [], want_json,
                                       timeout or DEFAULT_TIMEOUT)
        return self._complete_browser(text, images or [], timeout or DEFAULT_TIMEOUT,
                                      chat_name)

    def _complete_browser(self, text: str, images: List[str],
                          timeout: float,
                          chat_name: Optional[str] = None) -> Dict[str, Any]:
        if self.ai_browser is None:
            raise LlmError("the agent browser is not available in this deployment")
        name = chat_name or self.chat_provider()
        started = self._clock()
        image_path = images[0] if images else ""
        # A public URL travels with the prompt as well: a chat site that can
        # fetch images gets a second chance when the clipboard paste fails.
        if image_path and not os.path.exists(image_path):
            image_path = ""
        result = self.ai_browser.ask(text, name=name, timeout=timeout,
                                     image_path=image_path)
        return {
            "text": (result.get("text") or "").strip(),
            "provider": PROVIDER_BROWSER,
            "chatProvider": name,
            "elapsed": round(self._clock() - started, 1),
            "source": {"url": result.get("url", ""), "title": result.get("title", "")},
            "timedOut": bool(result.get("timedOut")),
        }

    def _complete_http(self, text: str, images: List[str], want_json: bool,
                       timeout: float) -> Dict[str, Any]:
        key = self.store.get_secret("ai.apiKey", "")
        if not key:
            raise LlmError("provider is http-api but no API key is stored")
        base = str(self.store.get_setting("ai.baseUrl", "")
                   or "https://api.openai.com/v1").rstrip("/")
        model = str(self.store.get_setting("ai.model", "") or "gpt-4o-mini")
        content: Any = text
        if images:
            parts: List[Dict[str, Any]] = [{"type": "text", "text": text}]
            for path in images:
                if not os.path.exists(path):
                    continue
                with open(path, "rb") as handle:
                    blob = base64.b64encode(handle.read()).decode("ascii")
                parts.append({"type": "image_url",
                              "image_url": {"url": "data:image/png;base64,%s" % blob}})
            content = parts
        payload: Dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": content}],
            "temperature": float(self.store.get_setting("ai.temperature", 0.1)),
        }
        if want_json:
            payload["response_format"] = {"type": "json_object"}
        request = urllib.request.Request(
            "%s/chat/completions" % base,
            data=json.dumps(payload).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer %s" % key})
        started = self._clock()
        try:
            with self._opener(request, timeout=timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:400]
            except Exception:  # noqa: BLE001 - diagnostics only
                pass
            raise LlmError("the model API returned HTTP %s: %s" % (exc.code, detail))
        except (urllib.error.URLError, OSError) as exc:
            raise LlmError("could not reach the model API: %s" % exc)
        try:
            data = json.loads(raw.decode("utf-8"))
            answer = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LlmError("the model API replied with something unexpected: %s" % exc)
        return {
            "text": (answer or "").strip(),
            "provider": PROVIDER_HTTP,
            "model": model,
            "elapsed": round(self._clock() - started, 1),
            "usage": data.get("usage") or {},
        }
