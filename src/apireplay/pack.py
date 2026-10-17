"""Versioned replay-pack model: create, save, load, and validate.

A pack is a plain JSON document::

    {
        "format": "apireplay/v1",
        "created_at": "2026-10-16T19:00:00+00:00",
        "source": "har",            # or "proxy"
        "requests": [
            {
                "id": "req-0001",
                "method": "GET",
                "url": "http://localhost:8080/api/users/1",
                "path": "/api/users/1",
                "query": {"verbose": "true"},
                "headers": {"accept": "application/json"},
                "body": "",
                "recorded_response": {
                    "status": 200,
                    "headers": {"content-type": "application/json"},
                    "body": "{\\"id\\": 1}",
                    "latency_ms": 12.4
                }
            }
        ]
    }

Bodies are UTF-8 strings. Non-UTF-8 bytes are stored base64-encoded with
``"body_encoding": "base64"`` on the request or the recorded response.
"""

import base64
import copy
import datetime
import json

FORMAT = "apireplay/v1"
RESULTS_FORMAT = "apireplay/results-v1"
DIFF_FORMAT = "apireplay/diff-v1"

_VALID_SOURCES = ("har", "proxy")


class PackError(ValueError):
    """Raised when a pack file is malformed or fails validation."""


def utcnow_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def new_pack(source: str) -> dict:
    if source not in _VALID_SOURCES:
        raise PackError(f"unknown pack source {source!r}; expected one of {_VALID_SOURCES}")
    return {
        "format": FORMAT,
        "created_at": utcnow_iso(),
        "source": source,
        "requests": [],
    }


def encode_body(data: bytes) -> tuple:
    """Return ``(text, encoding)``; encoding is ``"base64"`` or ``None``."""
    if data is None:
        return "", None
    try:
        return data.decode("utf-8"), None
    except UnicodeDecodeError:
        return base64.b64encode(data).decode("ascii"), "base64"


def decode_body(body, encoding) -> bytes:
    """Inverse of :func:`encode_body`."""
    if not body:
        return b""
    if encoding == "base64":
        return base64.b64decode(body.encode("ascii"))
    return body.encode("utf-8")


def _check(cond, errors, msg):
    if not cond:
        errors.append(msg)


def validate_pack(pack: dict) -> list:
    """Return a list of validation error strings (empty when valid)."""
    errors = []
    if not isinstance(pack, dict):
        return ["pack must be a JSON object"]
    _check(pack.get("format") == FORMAT, errors,
           f"unsupported format {pack.get('format')!r}; expected {FORMAT!r}")
    _check(isinstance(pack.get("created_at"), str), errors, "'created_at' must be a string")
    _check(pack.get("source") in _VALID_SOURCES, errors,
           f"'source' must be one of {_VALID_SOURCES}")
    requests = pack.get("requests")
    _check(isinstance(requests, list), errors, "'requests' must be a list")
    if isinstance(requests, list):
        for i, req in enumerate(requests):
            _validate_request(req, i, errors)
    return errors


def _validate_request(req, i, errors):
    where = f"requests[{i}]"
    if not isinstance(req, dict):
        errors.append(f"{where} must be an object")
        return
    for key in ("id", "method", "url", "path"):
        if not isinstance(req.get(key), str):
            errors.append(f"{where}.{key} must be a string")
    for key in ("query", "headers"):
        if not isinstance(req.get(key), dict):
            errors.append(f"{where}.{key} must be an object")
    if not isinstance(req.get("body"), str):
        errors.append(f"{where}.body must be a string")
    enc = req.get("body_encoding")
    if enc is not None and enc != "base64":
        errors.append(f"{where}.body_encoding must be 'base64' or absent")
    resp = req.get("recorded_response")
    if not isinstance(resp, dict):
        errors.append(f"{where}.recorded_response must be an object")
        return
    if not isinstance(resp.get("status"), int):
        errors.append(f"{where}.recorded_response.status must be an integer")
    if not isinstance(resp.get("headers"), dict):
        errors.append(f"{where}.recorded_response.headers must be an object")
    if not isinstance(resp.get("body"), str):
        errors.append(f"{where}.recorded_response.body must be a string")
    renc = resp.get("body_encoding")
    if renc is not None and renc != "base64":
        errors.append(f"{where}.recorded_response.body_encoding must be 'base64' or absent")
    lat = resp.get("latency_ms")
    if lat is not None and not isinstance(lat, (int, float)):
        errors.append(f"{where}.recorded_response.latency_ms must be a number or null")


def save_pack(pack: dict, path: str) -> None:
    errors = validate_pack(pack)
    if errors:
        raise PackError("cannot save invalid pack: " + "; ".join(errors))
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(pack, fh, indent=2)
        fh.write("\n")


def load_pack(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            pack = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise PackError(f"cannot read pack {path!r}: {exc}") from exc
    errors = validate_pack(pack)
    if errors:
        raise PackError("invalid pack " + "; ".join(errors))
    return pack


def copy_pack(pack: dict) -> dict:
    return copy.deepcopy(pack)
