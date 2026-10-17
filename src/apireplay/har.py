"""Import HTTP Archive (HAR 1.2) files into replay packs.

Reads ``log.entries[]`` from a HAR file exported by a browser's devtools
(or any HAR 1.2 producer) and converts each request/response pair into a
pack request. Response latency comes from the HAR ``timings`` block.
"""

import json

from .pack import encode_body, new_pack


def _headers_to_dict(pairs) -> dict:
    headers = {}
    for item in pairs or []:
        name = item.get("name")
        if name is None:
            continue
        headers[name] = item.get("value", "")
    return headers


def _query_to_dict(pairs) -> dict:
    query = {}
    for item in pairs or []:
        name = item.get("name")
        if name is None:
            continue
        query[name] = item.get("value", "")
    return query


def _timings_to_ms(timings) -> float | None:
    """Sum non-negative HAR timings into milliseconds; None when unknown."""
    if not isinstance(timings, dict):
        return None
    total = 0.0
    seen = False
    for value in timings.values():
        if isinstance(value, (int, float)) and value >= 0:
            total += value
            seen = True
    return round(total, 2) if seen else None


def _content_to_body(content) -> tuple:
    """Return ``(text, encoding)`` from a HAR ``content`` object."""
    content = content or {}
    text = content.get("text") or ""
    if content.get("encoding") == "base64":
        import base64

        try:
            raw = base64.b64decode(text)
        except Exception:
            raw = text.encode("utf-8", "replace")
        return encode_body(raw)
    return text, None


def _split_url(url: str) -> tuple:
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    path = parts.path or "/"
    return url, path


def har_to_pack(har: dict, source: str = "har") -> dict:
    try:
        entries = har["log"]["entries"]
    except (KeyError, TypeError) as exc:
        raise ValueError(f"not a HAR 1.2 document (missing log.entries): {exc}") from exc

    pack = new_pack(source)
    for i, entry in enumerate(entries):
        hreq = entry.get("request", {})
        hresp = entry.get("response", {})
        url = hreq.get("url", "")
        _full_url, path = _split_url(url)

        post_data = hreq.get("postData") or {}
        req_text, req_enc = _content_to_body(
            {"text": post_data.get("text", ""), "encoding": post_data.get("encoding")}
        )

        resp_content = hresp.get("content") or {}
        resp_text, resp_enc = _content_to_body(resp_content)

        request = {
            "id": f"req-{i + 1:04d}",
            "method": (hreq.get("method") or "GET").upper(),
            "url": url,
            "path": path,
            "query": _query_to_dict(hreq.get("queryString")),
            "headers": _headers_to_dict(hreq.get("headers")),
        }
        if req_enc:
            request["body_encoding"] = req_enc
        request["body"] = req_text

        recorded = {
            "status": int(hresp.get("status", 0)),
            "headers": _headers_to_dict(hresp.get("headers")),
            "body": resp_text,
            "latency_ms": _timings_to_ms(hresp.get("timings")),
        }
        if resp_enc:
            recorded["body_encoding"] = resp_enc
        request["recorded_response"] = recorded
        pack["requests"].append(request)
    return pack


def load_har_file(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        har = json.load(fh)
    return har_to_pack(har)
