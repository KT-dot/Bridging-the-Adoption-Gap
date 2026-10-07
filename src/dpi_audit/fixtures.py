"""Serve the bundled fixture websites on deterministic local ports (pilot / tests).

Each site listed in tests/fixtures/sites.json is served by its own HTTP server so that every
fixture has its own origin (and its own robots.txt), on port ``port_base + port_offset``.
"""
from __future__ import annotations

import functools
import http.server
import threading
from pathlib import Path

from .util import repo_root

SITES_DIR = repo_root() / "tests" / "fixtures" / "sites"


class _Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quiet
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self):
        site = Path(self.directory)
        rules = site / "_status.txt"   # lines: "/path STATUS"
        if rules.exists():
            for line in rules.read_text("utf-8").splitlines():
                parts = line.split()
                if len(parts) == 2 and self.path.split("?")[0] == parts[0]:
                    code = int(parts[1])
                    self.send_response(code)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(f"<!doctype html><html lang=en><title>{code}</title><h1>{code}</h1></html>".encode())
                    return
        return super().do_GET()

    def do_POST(self):  # fixtures record that a POST reached them (should never happen: requests are blocked)
        (Path(self.directory) / "_POST_RECEIVED").write_text(self.path)
        self.send_response(500)
        self.end_headers()


def site_map(fixtures_dir: Path | None = None) -> list[dict]:
    """Read tests/fixtures/sites.json: [{"name", "path" (relative to fixtures dir), "port_offset"}]."""
    import json
    base = fixtures_dir or SITES_DIR.parent
    return json.loads((base / "sites.json").read_text("utf-8"))


def serve(port_base: int = 18700, host: str = "127.0.0.1", fixtures_dir: Path | None = None) -> list[tuple[str, int, http.server.ThreadingHTTPServer]]:
    base = fixtures_dir or SITES_DIR.parent
    servers = []
    for site in site_map(base):
        port = port_base + int(site["port_offset"])
        handler = functools.partial(_Handler, directory=str((base / site["path"]).resolve()))
        srv = http.server.ThreadingHTTPServer((host, port), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append((site["name"], port, srv))
    return servers
