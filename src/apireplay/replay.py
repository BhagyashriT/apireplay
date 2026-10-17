"""Replay engine: fire a pack's requests at a target service concurrently.

Each request is sent with :class:`http.client.HTTPConnection` (or
``HTTPSConnection`` for https targets) inside a ``ThreadPoolExecutor``.
Per request we record status, headers, body, and wall-clock latency; network
failures and timeouts are recorded as errors instead of raising.
"""

import http.client
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode, urlsplit

from .pack import decode_body, encode_body, utcnow_iso

RESULTS_FORMAT = "apireplay/results-v1"

_FORWARD_SKIP = frozenset({"host", "content-length", "connection", "transfer-encoding"})


def _build_path(req: dict) -> str:
    path = req.get("path") or "/"
    query = req.get("query") or {}
    if query:
        return path + "?" + urlencode(query)
    return path


def _replay_one(req: dict, host: str, port: int, use_https: bool, timeout: float) -> dict:
    result = {
        "id": req.get("id"),
        "method": req.get("method"),
        "path": req.get("path"),
        "status": None,
        "headers": {},
        "body": "",
        "latency_ms": None,
        "error": None,
    }
    headers = {
        name: value
        for name, value in (req.get("headers") or {}).items()
        if name.lower() not in _FORWARD_SKIP
    }
    body = decode_body(req.get("body") or "", req.get("body_encoding"))
    conn_cls = http.client.HTTPSConnection if use_https else http.client.HTTPConnection
    started = time.perf_counter()
    try:
        conn = conn_cls(host, port, timeout=timeout)
        try:
            conn.request(req.get("method") or "GET", _build_path(req),
                         body=body or None, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            text, encoding = encode_body(data)
            result.update(
                status=resp.status,
                headers={name: value for name, value in resp.getheaders()},
                body=text,
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
            )
            if encoding:
                result["body_encoding"] = encoding
        finally:
            conn.close()
    except Exception as exc:  # network failure / timeout: record, don't raise
        result["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def replay_pack(pack: dict, target: str, workers: int = 8, timeout: float = 10.0) -> dict:
    """Replay every request in ``pack`` against ``target``; return results dict."""
    parts = urlsplit(target)
    if parts.scheme not in ("http", "https"):
        raise ValueError(f"target must be an http(s) URL, got {target!r}")
    host = parts.hostname or "127.0.0.1"
    port = parts.port or (443 if parts.scheme == "https" else 80)
    use_https = parts.scheme == "https"
    workers = max(1, int(workers))

    requests = pack.get("requests", [])
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(
            pool.map(lambda req: _replay_one(req, host, port, use_https, timeout), requests)
        )
    return {
        "format": RESULTS_FORMAT,
        "target": target,
        "replayed_at": utcnow_iso(),
        "results": results,
    }


def save_results(results: dict, path: str) -> None:
    import json

    with open(path, "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2)
        fh.write("\n")


def load_results(path: str) -> dict:
    import json

    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict) or data.get("format") != RESULTS_FORMAT:
        raise ValueError(f"{path!r} is not an apireplay results file")
    return data


def count_errors(results: dict) -> int:
    return sum(1 for r in results.get("results", []) if r.get("error"))
