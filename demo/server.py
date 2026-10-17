#!/usr/bin/env python3
"""Demo service for APIReplay: a tiny JSON API with two behavior versions.

Run:  python3 demo/server.py --version v1 --port 18080

Endpoints:
    GET  /api/users/{id}   user profile
    POST /api/orders        create an order (expects JSON {item_id, quantity})
    GET  /api/search?q=...  search (artificially slow)
    GET  /api/health        liveness probe

v2 seeds exactly three regressions relative to v1:

1. POST /api/orders with invalid input returns 400 instead of v1's 422.
2. GET /api/users/{id} renames the response field ``full_name`` -> ``name``.
3. GET /api/search sleeps ~0.6s instead of ~0.05s (latency regression).
"""

import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlsplit

USERS = {
    1: "Ada Lovelace",
    2: "Alan Turing",
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "APIReplayDemo/0.1"
    version = "v1"  # overridden per process

    def log_message(self, *args):
        pass

    # -- helpers ------------------------------------------------------
    def _send(self, status, obj):
        payload = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw.decode("utf-8")) if raw else None
        except (ValueError, UnicodeDecodeError):
            return None

    # -- routes -------------------------------------------------------
    def do_GET(self):
        parts = urlsplit(self.path)
        path, query = parts.path, dict(parse_qsl(parts.query))

        if path == "/api/health":
            self._send(200, {"status": "ok"})
        elif path.startswith("/api/users/"):
            try:
                user_id = int(path.rsplit("/", 1)[1])
            except ValueError:
                self._send(404, {"error": "not_found"})
                return
            name = USERS.get(user_id)
            if name is None:
                self._send(404, {"error": "not_found"})
                return
            if self.version == "v2":
                # REGRESSION 2: field renamed full_name -> name
                self._send(200, {"id": user_id, "name": name, "tier": "pro"})
            else:
                self._send(200, {"id": user_id, "full_name": name, "tier": "pro"})
        elif path == "/api/search":
            q = query.get("q", "")
            if self.version == "v2":
                time.sleep(0.6)   # REGRESSION 3: much slower than v1
            else:
                time.sleep(0.05)
            self._send(200, {
                "query": q,
                "results": [{"id": 1, "title": f"{q} widget"}],
                "count": 1,
            })
        else:
            self._send(404, {"error": "not_found"})

    def do_POST(self):
        parts = urlsplit(self.path)
        if parts.path != "/api/orders":
            self._send(404, {"error": "not_found"})
            return
        data = self._read_json()
        valid = (
            isinstance(data, dict)
            and isinstance(data.get("item_id"), str)
            and isinstance(data.get("quantity"), int)
            and data["quantity"] > 0
        )
        if not valid:
            if self.version == "v2":
                # REGRESSION 1: 400 instead of v1's 422
                self._send(400, {"error": "bad_request",
                                 "detail": "item_id and quantity are required"})
            else:
                self._send(422, {"error": "unprocessable_entity",
                                 "detail": "item_id (string) and quantity (positive int) required"})
            return
        self._send(201, {"order_id": "ord-1001", "item_id": data["item_id"],
                         "quantity": data["quantity"]})


def main():
    parser = argparse.ArgumentParser(description="APIReplay demo service")
    parser.add_argument("--version", choices=("v1", "v2"), required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    Handler.version = args.version
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.daemon_threads = True
    print(f"demo service {args.version} on 127.0.0.1:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
