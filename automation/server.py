"""Serves noVNC, the automation sidebar and the JSON API on one port.

Why this file exists
--------------------
The Colab stack exposes exactly one public URL: a Cloudflare tunnel pointing at
127.0.0.1:6080. Everything the sidebar needs must therefore come from that same
port, so this module replaces the plain ``websockify`` launch with a server that
does three things at once:

1. serves the noVNC static files (plus our sidebar assets) from the web root,
2. answers ``/automation/api/...`` JSON routes,
3. proxies the ``/websockify`` WebSocket to x11vnc, exactly as before.

WebSocket framing and the VNC proxy are delegated to the installed websockify
library rather than reimplemented. websockify's own accept loop forks a process
per connection, which would hide the engine's state from later requests, so this
module drives ``top_new_client`` from a threaded accept loop instead.
"""

from __future__ import annotations

import argparse
import logging
import hashlib
import os
import re
import shutil
import signal
import socket
import sys
import threading
import time
import urllib.parse
from typing import Any, Dict, Optional, Tuple, Type

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from automation import schema  # noqa: E402
from automation.api import API_PREFIX, AutomationApi, FlowStore  # noqa: E402
from automation.detect import Detector  # noqa: E402
from automation.engine import AutomationEngine, ControlBackend  # noqa: E402

LOG = logging.getLogger("automation.server")
MARKER = "<!-- automation-ui -->"
END_MARKER = "<!-- /automation-ui -->"
INJECT_BLOCK = re.compile(
    re.escape(MARKER) + r".*?" + re.escape(END_MARKER) + r"\n?", re.DOTALL)
# Asset references in the standalone panel, with any version we wrote before.
ASSET_REF = re.compile(r"(automation\.(?:css|js))(?:\?v=[0-9a-f]+)?")


def cache_query(static_dir: str) -> str:
    """?v=<hash of the main script>, so a new build cannot be served stale."""
    main_script = os.path.join(static_dir, "automation.js")
    if not os.path.exists(main_script):
        return ""
    with open(main_script, "rb") as handle:
        digest = hashlib.sha256(handle.read()).hexdigest()[:10]
    return "?v=%s" % digest


def inject_block(static_dir: str) -> str:
    """The sidebar tags, with a content hash so a new build cannot be served
    from the browser's cache. Rewritten on every start, so it never goes stale.
    """
    query = cache_query(static_dir)
    return (
        MARKER + "\n"
        '<link rel="stylesheet" href="automation/automation.css%s">\n'
        '<script type="module" src="automation/automation.js%s"></script>\n'
        "%s\n" % (query, query, END_MARKER)
    )


class WebsockifyMissing(RuntimeError):
    pass


def load_websockify() -> Tuple[Any, Type]:
    """Import websockify and find its request handler class."""
    try:
        from websockify import websocketproxy as module  # websockify >= 0.9
    except ImportError:
        try:
            from websockify import proxy as module  # very old layouts
        except ImportError as exc:
            raise WebsockifyMissing(
                "the websockify Python module is not importable (%s). "
                "Run install.sh, or: pip install websockify" % exc)
    handler = getattr(module, "ProxyRequestHandler", None) or \
        getattr(module, "WebSocketRequestHandler", None)
    if handler is None:
        raise WebsockifyMissing("websockify has no request handler class we recognise")
    return module.WebSocketProxy, handler


# ---------------------------------------------------------------------------
# web root
# ---------------------------------------------------------------------------
def prepare_web_root(web_root: str, novnc_dir: str, static_dir: str) -> Dict[str, Any]:
    """Build the served tree: a copy of noVNC plus our sidebar assets."""
    report = {"novncCopied": False, "staticCopied": [], "patched": False, "vncHtml": False}
    os.makedirs(web_root, exist_ok=True)

    target_vnc = os.path.join(web_root, "vnc.html")
    if not os.path.exists(target_vnc):
        if not os.path.isdir(novnc_dir):
            raise FileNotFoundError("noVNC is not installed at %s" % novnc_dir)
        shutil.copytree(novnc_dir, web_root, dirs_exist_ok=True)
        report["novncCopied"] = True

    os.makedirs(os.path.join(web_root, "automation"), exist_ok=True)
    if os.path.isdir(static_dir):
        for name in sorted(os.listdir(static_dir)):
            src = os.path.join(static_dir, name)
            if os.path.isfile(src):
                shutil.copy2(src, os.path.join(web_root, "automation", name))
                report["staticCopied"].append(name)

    guide = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ai_guide.md")
    if os.path.exists(guide):
        shutil.copy2(guide, os.path.join(web_root, "automation", "ai_guide.md"))
        report["staticCopied"].append("ai_guide.md")

    # The standalone panel is a plain static file, so its asset links are
    # versioned here rather than injected.
    panel = os.path.join(web_root, "automation", "panel.html")
    if os.path.exists(panel):
        with open(panel, encoding="utf-8") as handle:
            html = handle.read()
        query = cache_query(static_dir)
        updated = ASSET_REF.sub(lambda m: m.group(1) + query, html)
        if updated != html:
            with open(panel, "w", encoding="utf-8") as handle:
                handle.write(updated)
            report["panelPatched"] = True
        report["panel"] = True

    if os.path.exists(target_vnc):
        with open(target_vnc, encoding="utf-8") as handle:
            html = handle.read()
        block = inject_block(static_dir)
        if INJECT_BLOCK.search(html):
            # Replace rather than skip: the hash may have changed since the last
            # start, and a stale hash means the browser keeps the old sidebar.
            updated = INJECT_BLOCK.sub(lambda _: block, html, count=1)
        elif "</body>" in html:
            updated = html.replace("</body>", block + "</body>", 1)
        else:
            updated = html + "\n" + block
        if updated != html:
            with open(target_vnc, "w", encoding="utf-8") as handle:
                handle.write(updated)
            report["patched"] = True
        report["vncHtml"] = True
        report["cacheKey"] = block.split("?v=")[1].split('"')[0] if "?v=" in block else ""
    return report


# ---------------------------------------------------------------------------
# transport
# ---------------------------------------------------------------------------
def make_handler_class(api: AutomationApi, base_handler: Type) -> Type:
    """Subclass websockify's handler so API paths never reach the static server."""

    class AutomationRequestHandler(base_handler):
        def _api_request(self, method: str) -> bool:
            parsed = urllib.parse.urlparse(self.path)
            if not parsed.path.startswith(API_PREFIX):
                return False
            sub_path = parsed.path[len(API_PREFIX):] or "/"
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            body = self.rfile.read(length) if length > 0 else b""
            query = urllib.parse.parse_qs(parsed.query)
            headers = {key.lower(): value for key, value in self.headers.items()}
            status, response_headers, payload = api.handle(
                method, sub_path, query=query, body=body, headers=headers)
            self.send_response(status)
            for key, value in response_headers.items():
                self.send_header(key, value)
            # protocol_version is HTTP/1.1, so a missing Content-Length would
            # leave the browser waiting for a body that never ends.
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if method != "HEAD" and payload:
                self.wfile.write(payload)
            return True

        def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler naming
            if self._api_request("GET"):
                return
            super().do_GET()

        def do_HEAD(self):  # noqa: N802
            if self._api_request("HEAD"):
                return
            super().do_HEAD()

        def do_POST(self):  # noqa: N802
            if self._api_request("POST"):
                return
            self.send_error(405, "POST is only available under %s" % API_PREFIX)

        def do_PUT(self):  # noqa: N802
            if self._api_request("PUT"):
                return
            self.send_error(405, "PUT is only available under %s" % API_PREFIX)

        def do_DELETE(self):  # noqa: N802
            if self._api_request("DELETE"):
                return
            self.send_error(405, "DELETE is only available under %s" % API_PREFIX)

    return AutomationRequestHandler


class ThreadedFrontServer:
    """Threaded accept loop in front of websockify's per-connection handler."""

    def __init__(self, proxy: Any, host: str, port: int):
        self.proxy = proxy
        self.host = host
        self.port = port
        self._stop = threading.Event()
        self._listener: Optional[socket.socket] = None
        self.connections = 0

    def _handle(self, conn: socket.socket, address: Any) -> None:
        try:
            self.proxy.top_new_client(conn, address)
        except Exception as exc:
            LOG.debug("connection from %s ended: %s", address, exc)
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def serve_forever(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((self.host, self.port))
        listener.listen(128)
        listener.settimeout(0.5)
        self._listener = listener
        self.port = listener.getsockname()[1]  # resolves port 0 for tests
        LOG.info("listening on %s:%s", self.host, self.port)
        while not self._stop.is_set():
            try:
                conn, address = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self.connections += 1
            threading.Thread(target=self._handle, args=(conn, address),
                             name="conn-%d" % self.connections, daemon=True).start()
        listener.close()

    def stop(self) -> None:
        self._stop.set()
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# wiring
# ---------------------------------------------------------------------------
def build_api(args: argparse.Namespace) -> AutomationApi:
    os.makedirs(args.data_dir, exist_ok=True)
    token = args.token or os.environ.get("AUTOMATION_TOKEN", "")
    if not token and args.token_file:
        if os.path.exists(args.token_file):
            with open(args.token_file, encoding="utf-8") as handle:
                token = handle.read().strip()
    backend = ControlBackend(script_path=args.control_script, display=args.display,
                             screenshot_dir=os.path.join(args.data_dir, "shots"))
    engine = AutomationEngine(backend, args.data_dir)
    detector = Detector(backend, args.data_dir, viewport=dict(schema.DEFAULT_VIEWPORT),
                        cdp_port=args.cdp_port)
    guide_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ai_guide.md")
    return AutomationApi(
        engine, FlowStore(args.data_dir), detector,
        data_dir=args.data_dir, token=token, control_script=args.control_script,
        viewport=dict(schema.DEFAULT_VIEWPORT), guide_path=guide_path)


def parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    here = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(here)
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, default=6080)
    parser.add_argument("--vnc-host", default="127.0.0.1")
    parser.add_argument("--vnc-port", type=int, default=5901)
    parser.add_argument("--web-root", required=True,
                        help="directory to serve; a copy of noVNC plus our assets")
    parser.add_argument("--novnc-dir", default="/usr/share/novnc")
    parser.add_argument("--data-dir", required=True,
                        help="private directory for flows, runs, pages and shots")
    parser.add_argument("--control-script",
                        default=os.path.join(repo_root, "browser_control.sh"))
    parser.add_argument("--display", default=":1")
    parser.add_argument("--cdp-port", type=int, default=9222)
    parser.add_argument("--token", default="")
    parser.add_argument("--token-file", default="")
    parser.add_argument("--prepare-only", action="store_true",
                        help="build the web root and exit (no server)")
    parser.add_argument("--no-prepare", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[list] = None) -> int:
    args = parse_args(argv)
    # The proxy chdir's into the web root, so resolve everything up front.
    args.web_root = os.path.abspath(args.web_root)
    args.data_dir = os.path.abspath(args.data_dir)
    args.control_script = os.path.abspath(args.control_script)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if not args.no_prepare:
        static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
        report = prepare_web_root(os.path.abspath(args.web_root), args.novnc_dir, static_dir)
        LOG.info("web root ready at %s (%s)", args.web_root, report)
    if args.prepare_only:
        return 0

    try:
        proxy_class, base_handler = load_websockify()
    except WebsockifyMissing as exc:
        LOG.error("%s", exc)
        return 3

    api = build_api(args)
    if not api.token:
        LOG.warning("no automation token was supplied: the API is open to anyone "
                    "who can reach this port. Set AUTOMATION_TOKEN.")

    web_root = os.path.abspath(args.web_root)
    proxy = proxy_class(
        RequestHandlerClass=make_handler_class(api, base_handler),
        listen_host=args.listen_host,
        listen_port=args.listen_port,
        target_host=args.vnc_host,
        target_port=args.vnc_port,
        web=web_root,
        file_only=True,
        verbose=args.verbose,
        timeout=0,
        idle_timeout=0,
    )
    # Constructing the proxy chdir'd the process into the web root: websockify's
    # request handler resolves its document root from the cwd at connection
    # time, so we must not chdir back. Every path this server owns was resolved
    # to an absolute path in main() before this point.
    server = ThreadedFrontServer(proxy, args.listen_host, args.listen_port)

    def shutdown(*_: Any) -> None:
        LOG.info("shutting down")
        server.stop()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    LOG.info("automation API on %s, VNC proxy -> %s:%s",
             API_PREFIX, args.vnc_host, args.vnc_port)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
