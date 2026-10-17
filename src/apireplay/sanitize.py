"""Redaction engine: strip secrets and PII from a pack before it is shared.

Default rules cover three surfaces:

* **Headers** (case-insensitive): ``authorization``, ``proxy-authorization``,
  ``cookie``, ``set-cookie``, ``x-api-key``, ``x-auth-token``.
* **Query params** (case-insensitive): ``token``, ``api_key``, ``apikey``,
  ``secret``, ``password``, ``access_token``, ``client_secret``.
* **Body values**: email addresses, credit-card-like numbers (13–19 digits),
  SSN-like ``NNN-NN-NNNN``, ``Bearer`` tokens, ``AKIA…`` AWS keys and
  ``sk-…`` secrets — applied recursively to parsed JSON strings, with a
  regex fallback over raw text.

Custom rules extend the defaults via a JSON file::

    {
        "headers": ["x-internal-token"],
        "query_params": ["session_id"],
        "body_patterns": [{"name": "internal_id", "regex": "INT-[0-9]{6}"}]
    }

The summary reports redaction *counts* per category only — never values.
"""

import copy
import json
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

REDACTED = "***REDACTED***"

DEFAULT_HEADER_NAMES = frozenset({
    "authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "x-auth-token",
})

DEFAULT_QUERY_PARAMS = frozenset({
    "token",
    "api_key",
    "apikey",
    "secret",
    "password",
    "access_token",
    "client_secret",
})

DEFAULT_BODY_PATTERNS = (
    ("email", r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    ("credit_card", r"\b(?:\d[ -]?){13,19}\b"),
    ("ssn", r"\b\d{3}-\d{2}-\d{4}\b"),
    ("bearer_token", r"\bBearer\s+[A-Za-z0-9\-._~+/=]{8,}\b"),
    ("aws_key", r"\bAKIA[0-9A-Z]{16}\b"),
    ("sk_secret", r"\bsk-[A-Za-z0-9]{8,}\b"),
)


class Rules:
    """Compiled redaction rules: defaults plus any custom additions."""

    def __init__(self, header_names=(), query_params=(), body_patterns=()):
        self.header_names = set(DEFAULT_HEADER_NAMES) | {h.lower() for h in header_names}
        self.query_params = set(DEFAULT_QUERY_PARAMS) | {q.lower() for q in query_params}
        self.body_patterns = [(name, re.compile(rx)) for name, rx in DEFAULT_BODY_PATTERNS]
        for item in body_patterns:
            name, rx = item["name"], item["regex"]
            self.body_patterns.append((name, re.compile(rx)))

    @classmethod
    def from_file(cls, path):
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("rules file must contain a JSON object")
        return cls(
            header_names=data.get("headers", []),
            query_params=data.get("query_params", []),
            body_patterns=data.get("body_patterns", []),
        )


def _redact_text(text: str, rules: Rules) -> tuple:
    """Apply body patterns to raw text; return ``(new_text, sub_count)``."""
    count = 0
    for _name, pattern in rules.body_patterns:
        text, n = pattern.subn(REDACTED, text)
        count += n
    return text, count


def _redact_json_value(value, rules: Rules) -> tuple:
    """Recursively redact strings inside parsed JSON; return ``(value, count)``."""
    if isinstance(value, dict):
        count = 0
        out = {}
        for key, item in value.items():
            new_item, n = _redact_json_value(item, rules)
            out[key] = new_item
            count += n
        return out, count
    if isinstance(value, list):
        count = 0
        out = []
        for item in value:
            new_item, n = _redact_json_value(item, rules)
            out.append(new_item)
            count += n
        return out, count
    if isinstance(value, str):
        return _redact_text(value, rules)
    return value, 0


def sanitize_body_text(text: str, rules: Rules) -> tuple:
    """Redact a UTF-8 body string; return ``(new_text, redaction_count)``."""
    if not text:
        return text, 0
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return _redact_text(text, rules)
    redacted, count = _redact_json_value(parsed, rules)
    if count == 0:
        return text, 0
    return json.dumps(redacted), count


def _sanitize_headers(headers: dict, rules: Rules) -> int:
    count = 0
    for name in list(headers.keys()):
        if name.lower() in rules.header_names and headers[name] != REDACTED:
            headers[name] = REDACTED
            count += 1
    return count


def _sanitize_query(query: dict, rules: Rules) -> int:
    count = 0
    for name in list(query.keys()):
        if name.lower() in rules.query_params and query[name] != REDACTED:
            query[name] = REDACTED
            count += 1
    return count


def _sanitize_url(url: str, rules: Rules) -> str:
    """Redact sensitive query params embedded in the URL string itself."""
    parts = urlsplit(url)
    if not parts.query:
        return url
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    changed = False
    new_pairs = []
    for name, value in pairs:
        if name.lower() in rules.query_params and value != REDACTED:
            new_pairs.append((name, REDACTED))
            changed = True
        else:
            new_pairs.append((name, value))
    if not changed:
        return url
    return urlunsplit(parts._replace(query=urlencode(new_pairs)))


def _sanitize_body_field(holder: dict, key: str, enc_key: str, rules: Rules) -> int:
    """Redact a body stored as text (possibly base64); return count.

    Non-UTF-8 binary bodies are left untouched — regex redaction over
    arbitrary bytes is unsafe.
    """
    text = holder.get(key) or ""
    if not text:
        return 0
    if holder.get(enc_key) == "base64":
        import base64

        try:
            raw = base64.b64decode(text.encode("ascii"))
            decoded = raw.decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return 0
        new_text, count = sanitize_body_text(decoded, rules)
        if count:
            holder[key] = base64.b64encode(new_text.encode("utf-8")).decode("ascii")
        return count
    new_text, count = sanitize_body_text(text, rules)
    if count:
        holder[key] = new_text
    return count


def sanitize_pack(pack: dict, rules: Rules | None = None) -> tuple:
    """Return ``(sanitized_pack, summary_counts)``; the input is not mutated.

    Summary counts are ``{"headers": n, "query_params": n, "body_values": n}`` —
    counts only, never values.
    """
    rules = rules or Rules()
    clean = copy.deepcopy(pack)
    summary = {"headers": 0, "query_params": 0, "body_values": 0}
    for req in clean.get("requests", []):
        summary["headers"] += _sanitize_headers(req.get("headers", {}), rules)
        summary["query_params"] += _sanitize_query(req.get("query", {}), rules)
        req["url"] = _sanitize_url(req.get("url", ""), rules)
        summary["body_values"] += _sanitize_body_field(req, "body", "body_encoding", rules)
        resp = req.get("recorded_response", {})
        summary["headers"] += _sanitize_headers(resp.get("headers", {}), rules)
        summary["body_values"] += _sanitize_body_field(resp, "body", "body_encoding", rules)
    return clean, summary
