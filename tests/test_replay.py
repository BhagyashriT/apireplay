"""Tests for the replay engine (replay.py) against a live local server."""

import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from apireplay.pack import new_pack
from apireplay.replay import replay_pack


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, status, payload: bytes):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path == "/fast":
            self._send(200, b'{"ok": true}')
        elif self.path == "/slow":
            time.sleep(1.5)
            self._send(200, b'{"ok": true}')
        elif self.path.startswith("/inspect"):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            payload = json.dumps({
                "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": body.decode("utf-8", "replace"),
            }).encode()
            self._send(200, payload)
        else:
            self._send(404, b'{"error": "not_found"}')

    def do_POST(self):
        if self.path == "/echo":
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            self._send(201, body)
        else:
            self._send(404, b'{"error": "not_found"}')


def _serve_forever(server):
    server.serve_forever(poll_interval=0.1)


class ReplayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.server.daemon_threads = True
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=_serve_forever, args=(cls.server,),
                                      daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _pack(self, specs):
        pack = new_pack("proxy")
        for i, (method, path, body, headers, query) in enumerate(specs):
            pack["requests"].append({
                "id": f"req-{i + 1:04d}",
                "method": method,
                "url": self.base + path,
                "path": path.split("?")[0],
                "query": query,
                "headers": headers,
                "body": body,
                "recorded_response": {"status": 200, "headers": {},
                                     "body": "", "latency_ms": 1.0},
            })
        return pack

    def test_replay_records_structure_and_latency(self):
        pack = self._pack([
            ("GET", "/fast", "", {}, {}),
            ("POST", "/echo", '{"a": 1}', {"content-type": "application/json"}, {}),
            ("GET", "/inspect?x=1&y=2", "", {"X-Custom": "yes"}, {"x": "1", "y": "2"}),
        ])
        results = replay_pack(pack, self.base, workers=4, timeout=5.0)
        self.assertEqual(results["format"], "apireplay/results-v1")
        self.assertEqual(len(results["results"]), 3)

        fast, echo, inspect = results["results"]
        self.assertEqual(fast["status"], 200)
        self.assertIsNone(fast["error"])
        self.assertGreaterEqual(fast["latency_ms"], 0)
        self.assertIn("ok", fast["body"])

        self.assertEqual(echo["status"], 201)
        self.assertEqual(json.loads(echo["body"]), {"a": 1})

        seen = json.loads(inspect["body"])
        self.assertEqual(seen["path"], "/inspect?x=1&y=2")
        self.assertEqual(seen["headers"].get("x-custom"), "yes")

    def test_timeout_recorded_as_error(self):
        pack = self._pack([("GET", "/slow", "", {}, {})])
        results = replay_pack(pack, self.base, workers=1, timeout=0.5)
        res = results["results"][0]
        self.assertIsNone(res["status"])
        self.assertIsNotNone(res["error"])
        self.assertIn("Timeout", res["error"])

    def test_connection_refused_recorded_as_error(self):
        pack = self._pack([("GET", "/fast", "", {}, {})])
        results = replay_pack(pack, "http://127.0.0.1:1", workers=1, timeout=2.0)
        res = results["results"][0]
        self.assertIsNone(res["status"])
        self.assertIsNotNone(res["error"])

    def test_bad_target_rejected(self):
        pack = self._pack([])
        with self.assertRaises(ValueError):
            replay_pack(pack, "gopher://example.com", workers=1)


if __name__ == "__main__":
    unittest.main()
