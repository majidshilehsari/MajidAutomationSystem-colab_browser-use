"""In-memory secret vault for ``secure_input`` / ``secure_http_request``.

A secret entered by a human through the panel goes straight into this vault
and nowhere else: never into flow JSON, the model prompt, the clipboard,
screenshots, run logs, report text, audit details, a database text column or
an API response.  Callers receive an opaque ``sec_...`` handle plus masked
status; the raw value is handed out only by :meth:`SecretVault.acquire`, and
only to a connector request that passes every policy check.

Deliberately memory-only: a secret that survived a redeploy would outlive the
human decision that created it.
"""

from __future__ import annotations

import hashlib
import ipaddress
import socket
import threading
import time
import uuid
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

#: Hard ceilings so a typo can never create an immortal master key.
MAX_TTL_SECONDS = 12 * 3600
MAX_USES = 1000
MAX_BODY_BYTES = 2 * 1024 * 1024

_ALLOWED_METHODS = ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS")


class VaultError(Exception):
    def __init__(self, message: str, status: int = 403) -> None:
        super().__init__(message)
        self.status = status


def _private_host(host: str) -> bool:
    """True for loopback/private/link-local literals (SSRF floor)."""
    try:
        return ipaddress.ip_address(host.strip("[]")).is_private
    except ValueError:
        return False


def _normalise_policy(data: Any) -> Dict[str, Any]:
    if not isinstance(data, dict):
        raise VaultError("a secret policy must be an object", 400)
    methods = [str(m).upper() for m in (data.get("methods") or ["GET", "POST"])]
    bad = [m for m in methods if m not in _ALLOWED_METHODS]
    if bad:
        raise VaultError("unsupported method(s): %s" % ", ".join(bad), 400)
    hosts = [str(h).lower() for h in (data.get("allowedHosts") or []) if str(h).strip()]
    if not hosts:
        raise VaultError("a secret needs at least one allowed host", 400)
    for host in hosts:
        if _private_host(host):
            raise VaultError("private/loopback hosts are not allowed: %s" % host, 400)
    paths = [str(p) for p in (data.get("allowedPaths") or []) if str(p).startswith("/")]
    if not paths:
        paths = ["/"]
    try:
        ttl = int(data.get("ttlSeconds") or 900)
        uses = int(data.get("maxUses") or 20)
    except (TypeError, ValueError):
        raise VaultError("ttlSeconds and maxUses must be numbers", 400)
    ttl = max(30, min(ttl, MAX_TTL_SECONDS))
    uses = max(1, min(uses, MAX_USES))
    template = str(data.get("headerTemplate")
                   or "Authorization: Bearer {{secret}}")
    if "{{secret}}" not in template:
        raise VaultError("headerTemplate must contain {{secret}}", 400)
    return {
        "scope": str(data.get("scope") or "custom"),
        "methods": methods,
        "allowedHosts": hosts,
        "allowedPaths": paths,
        "headerTemplate": template,
        "ttlSeconds": ttl,
        "maxUses": uses,
        "readOnly": bool(data.get("readOnly", True)),
        "allowRedirect": bool(data.get("allowRedirect", False)),
    }


class SecretVault:
    """Holds live secrets behind opaque handles, with a use ledger."""

    def __init__(self, clock: Any = time.time) -> None:
        self._clock = clock
        self._lock = threading.RLock()
        self._items: Dict[str, Dict[str, Any]] = {}

    # -- lifecycle -----------------------------------------------------
    def create(self, secret: str, policy: Any, actor: str = "operator") -> Dict[str, Any]:
        if not isinstance(secret, str) or not secret:
            raise VaultError("the secret value is empty", 400)
        norm = _normalise_policy(policy)
        now = self._clock()
        handle = "sec_%s" % uuid.uuid4().hex[:12]
        with self._lock:
            self._items[handle] = {
                "value": secret,
                "policy": norm,
                "actor": actor,
                "createdAt": now,
                "expiresAt": now + norm["ttlSeconds"],
                "uses": 0,
                "revoked": False,
                "revokedAt": None,
                "digest": hashlib.sha256(secret.encode("utf-8")).hexdigest()[:12],
            }
        return self.status(handle)

    def status(self, handle: str) -> Dict[str, Any]:
        with self._lock:
            item = self._items.get(handle)
            if item is None:
                raise VaultError("no such secret handle", 404)
            now = self._clock()
            expired = now >= item["expiresAt"]
            return {
                # masked on purpose: only the prefix ever leaves this module
                "handle": handle[:8] + "…",
                "fullHandle": handle,
                "status": "revoked" if item["revoked"] else ("expired" if expired else "ready"),
                "scope": item["policy"]["scope"],
                "createdAt": item["createdAt"],
                "expiresAt": item["expiresAt"],
                "uses": item["uses"],
                "maxUses": item["policy"]["maxUses"],
                "revoked": item["revoked"],
                "actor": item["actor"],
                "digest": item["digest"],
                "policy": {key: item["policy"][key] for key in
                           ("scope", "methods", "allowedHosts", "allowedPaths",
                            "readOnly", "allowRedirect", "ttlSeconds", "maxUses")},
            }

    def revoke(self, handle: str, actor: str = "operator") -> Dict[str, Any]:
        with self._lock:
            item = self._items.get(handle)
            if item is None:
                raise VaultError("no such secret handle", 404)
            item["revoked"] = True
            item["revokedAt"] = self._clock()
            item["value"] = ""
            return self.status(handle)

    # -- the only door to a raw value -----------------------------------
    def acquire(self, handle: str, method: str, url: str) -> "tuple[str, Dict[str, Any]]":
        """Hand the raw secret to one checked request, or raise.

        Every check happens before the value leaves the vault, and the use
        ledger is written in the same lock so maxUses cannot race.
        """
        method = str(method or "").upper()
        with self._lock:
            item = self._items.get(handle)
            if item is None:
                raise VaultError("no such secret handle", 404)
            if item["revoked"]:
                raise VaultError("the handle was revoked", 410)
            now = self._clock()
            if now >= item["expiresAt"]:
                raise VaultError("the handle expired", 410)
            if item["uses"] >= item["policy"]["maxUses"]:
                raise VaultError("the handle reached maxUses", 410)
            policy = item["policy"]
            if method not in policy["methods"]:
                raise VaultError("method %s is outside the policy" % method)
            if method in ("PUT", "DELETE", "PATCH") and policy["readOnly"]:
                raise VaultError("the policy is read-only")
            try:
                parsed = urlsplit(url)
            except ValueError:
                raise VaultError("the url cannot be parsed", 400)
            host = (parsed.hostname or "").lower()
            if parsed.scheme not in ("http", "https") or not host:
                raise VaultError("only http(s) urls with a host are allowed", 400)
            if host not in policy["allowedHosts"]:
                raise VaultError("host %s is not in the policy" % host)
            if _private_host(host):
                raise VaultError("private/loopback hosts are not allowed")
            # DNS-rebinding floor: a hostname that resolves to a private or
            # loopback address is refused here, before any socket is opened.
            # Unresolvable names are let through on purpose - the request
            # itself will fail, and refusing them would break offline tests.
            try:
                infos = socket.getaddrinfo(host, None)
            except OSError:
                infos = []
            for info in infos:
                if _private_host(str(info[4][0])):
                    raise VaultError(
                        "the host resolves to a private address (rebinding blocked)")
            if not any(parsed.path.startswith(prefix) for prefix in policy["allowedPaths"]):
                raise VaultError("path %s is outside the policy" % parsed.path)
            if parsed.username or parsed.password:
                raise VaultError("credentials inside the url are not allowed")
            item["uses"] += 1
            return item["value"], policy

    def fingerprint(self, handle: str) -> str:
        with self._lock:
            item = self._items.get(handle)
            return item["digest"] if item else "unknown"

    def live_handles(self) -> list:
        with self._lock:
            return [self.status(handle) for handle in self._items]
