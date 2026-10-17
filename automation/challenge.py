"""Best-effort detection of CAPTCHA and human-verification pages.

This module intentionally only classifies page text and visible DOM hints. It
never clicks, enters, solves, bypasses, or submits a challenge. The engine uses
it as a safety interlock: when cues are found, it pauses for a human.

Round-12 hardening (the false-pause loop): a *word* on the page is not a
challenge.  Chat histories, model answers, code blocks, prompt boxes and our
own UI are stripped before the text is read, and a pause now requires
**structural** evidence - a visible widget/iframe/selector from a known
provider, or a challenge-shaped title.  Text-only wording is still reported
(as ``signals`` plus ``evidence``) so logs and reports stay informative, but it
downgrades to a non-blocking progress notice instead of stopping the run.
Every assessment also carries a small ``textHash`` fingerprint so the engine
can refuse to raise the very same handoff twice on an unchanged page.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, Iterable, List
from urllib.parse import urlsplit


# Keep the probe read-only and small. Raw page text is inspected in memory, but
# the classifier returns only coarse signal names plus short evidence snippets;
# it does not persist full body text, form values, iframe URLs, or queries.
#
# The text is read from a *clone* of the body with the non-credible regions
# removed first: code, prompts, chat messages and hidden material mention
# "captcha"/"security check" all the time without any challenge being present.
CHALLENGE_SCRIPT = r"""
(() => {
  const visible = (el) => {
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return rect.width > 1 && rect.height > 1 && style.display !== 'none' &&
           style.visibility !== 'hidden' && style.opacity !== '0';
  };
  const selectors = [
    'iframe', '[data-sitekey]', '[id*="captcha" i]', '[class*="captcha" i]',
    '[aria-label*="captcha" i]', '[title*="captcha" i]',
    '[id*="challenge" i]', '[class*="challenge" i]',
    '[aria-label*="challenge" i]', '[title*="challenge" i]'
  ].join(',');
  const hints = Array.from(document.querySelectorAll(selectors))
    .filter(visible).slice(0, 80).map((el) => [
      el.tagName || '', el.id || '',
      typeof el.className === 'string' ? el.className : '',
      el.getAttribute('title') || '', el.getAttribute('aria-label') || '',
      el.getAttribute('src') || '',
      el.hasAttribute('data-sitekey') ? 'data-sitekey' : ''
    ].join(' ').slice(0, 400));
  // Regions whose words must never stop a run on their own.
  const EXCLUDE = [
    'script', 'style', 'noscript', 'template', 'pre', 'code', 'textarea',
    '[contenteditable="true"]', '[role="textbox"]',
    '[data-message-author]', '[data-role="message"]',
    '[class*="markdown" i]', '[class*="message-body" i]',
    '[class*="message-content" i]', '[class*="chat-history" i]',
    '[class*="conversation" i]', '#mas-root', '#mas-toast'
  ].join(',');
  let text = '';
  try {
    const clone = document.body ? document.body.cloneNode(true) : null;
    if (clone) {
      clone.querySelectorAll(EXCLUDE).forEach((el) => el.remove());
      text = (clone.innerText || clone.textContent || '');
    }
  } catch (err) {
    text = (document.body && document.body.innerText) || '';
  }
  return {
    url: location.href,
    title: document.title || '',
    text: text.slice(0, 30000),
    hints
  };
})()
"""

_PROVIDER_HINT = re.compile(
    r"(?:recaptcha|hcaptcha|turnstile|captcha|cf-challenge|challenges\.cloudflare\.com)",
    re.IGNORECASE,
)
_HUMAN_PHRASE = re.compile(
    r"\b(?:verify|prove|confirm)\s+(?:that\s+)?you(?:\s+are|'re)\s+human\b"
    r"|\bi(?:\s+am|'m)\s+not\s+a\s+robot\b"
    r"|\bverify\s+you(?:\s+are|'re)\s+not\s+a\s+robot\b"
    r"|\bverify\s+your\s+humanity\b",
    re.IGNORECASE,
)
_CAPTCHA_TERM = re.compile(r"\b(?:captcha|recaptcha|hcaptcha|turnstile)\b", re.IGNORECASE)
_SECURITY_PHRASE = re.compile(
    r"\b(?:security|identity)\s+(?:verification|check|challenge)\b"
    r"|\bcomplete\s+the\s+security\s+check\b"
    r"|\bsecurity\s+verification\b",
    re.IGNORECASE,
)
_BROWSER_CHALLENGE = re.compile(
    r"\bchecking\s+your\s+browser\b"
    r"|\bchecking\s+if\s+the\s+site\s+connection\s+is\s+secure\b"
    r"|\bperforming\s+security\s+verification\b"
    r"|\bautomated\s+(?:requests?|queries)\s+from\s+your\s+computer\b",
    re.IGNORECASE,
)
_PERSIAN_HUMAN_PHRASE = re.compile(
    r"(?:تأیید|تایید|ثابت)\s+(?:کنید\s+که\s+)?(?:شما\s+)?انسان\s+هستید"
    r"|(?:من|شما)\s+ربات\s+نیستم"
    r"|بررسی\s+امنیتی\s+(?:لازم|موردنیاز|درحال)"
    r"|کپچا.{0,80}(?:وارد|انتخاب|تکمیل|حل)"
    r"|(?:وارد|انتخاب|تکمیل)\s+.{0,80}کپچا",
    re.IGNORECASE,
)

#: Signals that mean "a real widget/page shape is in front of the browser".
STRUCTURAL_SIGNALS = ("visible-captcha-or-challenge-widget", "browser-challenge-title")


def safe_origin(url: Any) -> str:
    """Return only an HTTP(S) origin; omit credentials, path, query and hash."""
    if not isinstance(url, str) or not url:
        return ""
    try:
        parsed = urlsplit(url)
        scheme = parsed.scheme.lower()
        host = parsed.hostname
        if scheme not in ("http", "https") or not host:
            return ""
        host = host.lower()
        if ":" in host and not host.startswith("["):
            host = "[%s]" % host
        port = parsed.port
        default_port = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
        netloc = host if not port or default_port else "%s:%d" % (host, port)
        return "%s://%s" % (scheme, netloc)
    except (TypeError, ValueError):
        return ""


def _snippet(match: "re.Match[str]", limit: int = 90) -> str:
    # Only the matched phrase itself: no surrounding words, so the evidence
    # never leaks neighbouring page text into logs and reports.
    return " ".join(match.group(0).split())[:limit]


def assess_page(payload: Any) -> Dict[str, Any]:
    """Classify a read-only page probe or a plain page-text string.

    The result deliberately contains no raw body text. ``detected`` now means
    "structural evidence says pause and ask the human"; wording-only pages keep
    their ``signals`` and ``evidence`` but report ``detected=False`` so the
    engine can log them as a non-blocking notice instead of stopping.
    """
    if isinstance(payload, str):
        text = payload
        title = ""
        hints: Iterable[Any] = ()
        url = ""
    elif isinstance(payload, dict):
        value = payload.get("value")
        if isinstance(value, (dict, str)):
            payload = value
        text = payload if isinstance(payload, str) else ""
        if isinstance(payload, dict):
            text = payload.get("text") or payload.get("bodyText") or ""
            title = payload.get("title") or ""
            hints = payload.get("hints") or payload.get("challengeHints") or []
            url = payload.get("url") or ""
        else:
            title, hints, url = "", (), ""
    else:
        text, title, hints, url = "", "", (), ""

    if not isinstance(text, str):
        text = ""
    if not isinstance(title, str):
        title = ""
    if isinstance(hints, str):
        hint_text = hints
        hint_items: List[str] = [hints]
    else:
        hint_items = [str(item) for item in hints if isinstance(item, (str, int, float))]
        hint_text = " ".join(hint_items)

    # Collapse whitespace for phrase matching but do not expose this text in
    # the assessment returned to callers.
    body = re.sub(r"\s+", " ", (title + " " + text)[:30000])
    signals: List[str] = []
    evidence: List[Dict[str, Any]] = []

    def note(signal: str, kind: str, snippet: str, region: str, visible: bool) -> None:
        signals.append(signal)
        evidence.append({
            "signal": signal, "kind": kind, "snippet": snippet[:120],
            "region": region, "visible": visible,
        })

    provider = _PROVIDER_HINT.search(hint_text)
    if provider:
        where = next((item for item in hint_items if _PROVIDER_HINT.search(item)), hint_text)
        note("visible-captcha-or-challenge-widget", "structural", where[:120],
             "visible-widget", True)
    for pattern, signal in (
        (_HUMAN_PHRASE, "human-verification-wording"),
        (_PERSIAN_HUMAN_PHRASE, "localized-human-verification-wording"),
        (_SECURITY_PHRASE, "security-verification-wording"),
        (_BROWSER_CHALLENGE, "browser-challenge-wording"),
    ):
        match = pattern.search(body)
        if match:
            note(signal, "text", _snippet(match), "page-text", True)

    if _CAPTCHA_TERM.search(body):
        # A bare mention in help/documentation is not enough. Require a nearby
        # instruction such as entering a code, choosing images, or completing
        # a challenge before raising this text-only signal.
        nearby = re.search(
            r"(?:captcha|recaptcha|hcaptcha|turnstile).{0,120}(?:enter|type|solve|complete|"
            r"select|click|choose|verify|continue|submit|code|images?|pictures?|challenge)"
            r"|(?:enter|type|solve|complete|select|click|choose|verify|continue|submit|"
            r"code|images?|pictures?|challenge).{0,120}(?:captcha|recaptcha|hcaptcha|turnstile)",
            body, re.IGNORECASE,
        )
        if nearby:
            note("captcha-instructions", "text", _snippet(nearby), "page-text", True)

    # Cloudflare's interstitial commonly relies on a browser-check phrase and a
    # title rather than naming CAPTCHA. Require both cues to avoid flagging any
    # ordinary page whose title happens to say "Just a moment".
    if ("just a moment" in title.lower()
            and ("cloudflare" in hint_text.lower() or _BROWSER_CHALLENGE.search(body))):
        note("browser-challenge-title", "structural", title[:120], "title", True)

    signals = list(dict.fromkeys(signals))
    seen: set = set()
    unique_evidence: List[Dict[str, Any]] = []
    for item in evidence:
        key = (item["signal"], item["snippet"])
        if key not in seen:
            seen.add(key)
            unique_evidence.append(item)
    structural = any(item["signal"] in STRUCTURAL_SIGNALS for item in unique_evidence)
    digest = hashlib.sha256()
    digest.update(body[:8000].encode("utf-8", "replace"))
    return {
        "detected": structural,
        "structural": structural,
        "signals": signals,
        "evidence": unique_evidence[:12],
        "textHash": digest.hexdigest()[:16],
        "pageOrigin": safe_origin(url),
    }
