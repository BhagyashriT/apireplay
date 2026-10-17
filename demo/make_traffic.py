#!/usr/bin/env python3
"""Send a fixed set of requests through the APIReplay capture proxy.

Uses raw http.client connections with absolute-URI request lines (proxy
style), so traffic deterministically flows through the proxy regardless of
environment proxy settings.

Env: PROXY_PORT (default 18888), TARGET_PORT (default 18080).
"""

import http.client
import json
import os

PROXY_PORT = int(os.environ.get("PROXY_PORT", "18888"))
TARGET_PORT = int(os.environ.get("TARGET_PORT", "18080"))
TARGET = f"http://127.0.0.1:{TARGET_PORT}"

AUTH_HEADERS = {
    "Authorization": "Bearer demo-token-abc123",  # redacted later by `sanitize`
    "X-Request-Id": "demo-run-1",
}


def send(method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", PROXY_PORT, timeout=15)
    hdrs = dict(AUTH_HEADERS)
    hdrs.update(headers or {})
    payload = None
    if body is not None:
        payload = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    try:
        conn.request(method, TARGET + path, body=payload, headers=hdrs)
        resp = conn.getresponse()
        data = resp.read()
        print(f"{method} {path} -> {resp.status} ({len(data)} bytes)")
    finally:
        conn.close()


def main():
    send("GET", "/api/users/1")
    send("GET", "/api/users/2")
    send("POST", "/api/orders", {"item_id": "widget-1", "quantity": 2})
    send("POST", "/api/orders", {"item_id": "widget-1"})  # bad input -> 422 on v1
    send("GET", "/api/search?q=laptop")
    send("GET", "/api/search?q=keyboard")
    send("GET", "/api/health")
    print("traffic complete")


if __name__ == "__main__":
    main()
