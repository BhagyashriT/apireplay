"""A minimal HTTP capture proxy for recording traffic into a pack.

The proxy listens on ``--port`` and forwards every request to ``--target``
(an origin server base URL such as ``http://localhost:8080``), recording the
request/response pair — including observed latency — into memory. On
shutdown (Ctrl-C / SIGINT / SIGTERM) the recorded pairs are written to the
pack file.

v1 is HTTP only: the proxy forwards plain HTTP. It does not terminate TLS
and cannot capture HTTPS traffic.
"""

import http.client
import http.server
import signal
import socket
import threading
import time
from urllib.parse import parse_qsl, urlsplit

from .pack import encode_body, new_pack, save_pack

# Hop-by-hop headers (RFC 9110 §7.6.1) plus proxy framing headers are never
# forwarded; the forwarding connection manages its own framing and Host.
_HOP_BY_HOP = frozenset({
    "connection",
    "proxy-connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
})

_RESPONSE_HOP_BY_HOP = frozenset({
    "connection",
    "proxy-connection",
    "keep-alive",
    "proxy-authenticate",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "content-length",
})


class _CaptureHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "APIReplayProxy/0.1"

    def log_message(self, fmt, *args):  # keep the proxy quiet
        pass

    def _forward(self):
        method = self.command
        raw_target = self.path  # absolute URI when used as a proxy, origin-form otherwise

        if raw_target.startswith("http://") or raw_target.startswith("https://"):
            parts = urlsplit(raw_target)
            path_query = parts.path or "/"
            if parts.query:
                path_query += "?" + parts.query
            original_url = raw_target
        else:
            path_query = raw_target
            original_url = self.server.target_base.rstrip("/") + raw_target

        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length > 0 else b""

        fwd_headers = {
            name: value
            for name, value in self.headers.items()
            if name.lower() not in _HOP_BY_HOP
        }

        target = urlsplit(self.server.target_base)
        conn_cls = (
            http.client.HTTPSConnection
            if target.scheme == "https"
            else http.client.HTTPConnection
        )
        started = time.perf_counter()
        status, resp_headers, resp_body = 502, {}, b""
        try:
            conn = conn_cls(target.hostname, target.port or (443 if target.scheme == "https" else 80),
                            timeout=30)
            try:
                conn.request(method, path_query, body=body or None, headers=fwd_headers)
                resp = conn.getresponse()
                status = resp.status
                resp_headers = {name: value for name, value in resp.getheaders()}
                resp_body = resp.read()
            finally:
                conn.close()
        except (OSError, http.client.HTTPException, socket.timeout):
            status, resp_headers, resp_body = 502, {}, b""
        latency_ms = round((time.perf_counter() - started) * 1000, 2)

        # Record the pair.
        query = dict(parse_qsl(urlsplit(original_url).query, keep_blank_values=True))
        req_text, req_enc = encode_body(body)
        resp_text, resp_enc = encode_body(resp_body)
        with self.server.records_lock:
            idx = len(self.server.records) + 1
            record = {
                "id": f"req-{idx:04d}",
                "method": method,
                "url": original_url,
                "path": urlsplit(path_query).path or "/",
                "query": query,
                "headers": dict(self.headers.items()),
                "body": req_text,
                "recorded_response": {
                    "status": status,
                    "headers": resp_headers,
                    "body": resp_text,
                    "latency_ms": latency_ms,
                },
            }
            if req_enc:
                record["body_encoding"] = req_enc
            if resp_enc:
                record["recorded_response"]["body_encoding"] = resp_enc
            self.server.records.append(record)

        # Relay the response to the client.
        out_headers = {
            name: value
            for name, value in resp_headers.items()
            if name.lower() not in _RESPONSE_HOP_BY_HOP
        }
        try:
            self.send_response(status)
            for name, value in out_headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(resp_body)))
            self.send_header("Connection", "close")
            self.end_headers()
            if method != "HEAD":
                self.wfile.write(resp_body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # All common methods funnel through the same forwarder.
    do_GET = _forward
    do_POST = _forward
    do_PUT = _forward
    do_DELETE = _forward
    do_PATCH = _forward
    do_HEAD = _forward
    do_OPTIONS = _forward


def _records_to_pack(records) -> dict:
    pack = new_pack("proxy")
    pack["requests"] = records
    return pack


def run_proxy(port: int, target: str, out: str) -> str:
    """Run the capture proxy until SIGINT/SIGTERM; then write the pack."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), _CaptureHandler)
    server.daemon_threads = True
    server.target_base = target.rstrip("/")
    server.records = []
    server.records_lock = threading.Lock()

    stopping = threading.Event()

    def _on_signal(signum, frame):
        stopping.set()

    old_int = signal.signal(signal.SIGINT, _on_signal)
    old_term = signal.signal(signal.SIGTERM, _on_signal)
    try:
        worker = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True
        )
        worker.start()
        print(f"APIReplay capture proxy listening on 127.0.0.1:{port} -> {target}")
        print("Send traffic through the proxy, then press Ctrl-C to save the pack.")
        stopping.wait()
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
    finally:
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)

    pack = _records_to_pack(server.records)
    save_pack(pack, out)
    print(f"Saved {len(server.records)} captured request(s) to {out}")
    return out
