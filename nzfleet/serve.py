"""`nzfleet serve`: status.html over HTTP, read-only, on the Tailscale address and nowhere else."""

from __future__ import annotations

import http.server
import ipaddress
import os
import subprocess

from . import config

PAGE = os.path.join(config.HOME, "status.html")
TAILNET_V4 = ipaddress.ip_network("100.64.0.0/10")
TAILNET_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")
HEADERS = {
    "Cache-Control": "no-store",
    "Content-Type": "text/html; charset=utf-8",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'",
}


def tailnet_address(output: str) -> str | None:
    """The first line of `tailscale ip -4` that is a tailnet address; None for anything else, so a wildcard or a LAN address never binds."""
    for line in output.split():
        try:
            ip = ipaddress.ip_address(line)
        except ValueError:
            continue
        if ip in TAILNET_V4 or ip in TAILNET_V6:
            return str(ip)
    return None


def find_address() -> str | None:
    try:
        done = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return tailnet_address(done.stdout) if done.returncode == 0 else None


def handler(path: str) -> type:
    class Page(http.server.BaseHTTPRequestHandler):
        def _send(self, status: int, body: bytes, head: bool) -> None:
            self.send_response(status)
            for k, v in HEADERS.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if not head:
                self.wfile.write(body)

        def _answer(self, head: bool) -> None:
            if self.path.split("?")[0] not in ("/", "/status.html"):
                return self._send(404, b"not found", head)
            try:
                with open(path, "rb") as f:
                    self._send(200, f.read(), head)
            except OSError:
                self._send(503, b"no page yet; the daemon writes one every tick", head)

        def do_GET(self) -> None:  # noqa: N802 - http.server's name
            self._answer(False)

        def do_HEAD(self) -> None:  # noqa: N802
            self._answer(True)

        def _refuse(self) -> None:
            self._send(405, b"read-only", False)

        do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _refuse

        def log_message(self, *_args) -> None:
            return

    return Page


def run(port: int, path: str = PAGE, address: str | None = None) -> int:
    address = address or find_address()
    if address is None:
        print("no Tailscale address: this page is for the tailnet only, so it does not start. Is tailscale up?")
        return 1
    server = http.server.ThreadingHTTPServer((address, port), handler(path))
    print(f"serving {path} at http://{address}:{port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0
