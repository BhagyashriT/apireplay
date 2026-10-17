"""Comparison engine: recorded (expected) vs replayed (actual) per request.

Checks, per request:

* **status** — exact match on the HTTP status code;
* **body** — semantic JSON diff when both bodies parse as JSON (object key
  order ignored, numeric ``1`` == ``1.0``, arrays ordered unless
  ``unordered_lists`` is set), exact string match otherwise;
* **headers** — compared only for a small allowlist (default
  ``content-type``); mismatches fail, unlisted headers never fail;
* **latency** — flagged as a regression only when the replayed latency
  exceeds the recorded latency × ``latency_ratio`` **and** recorded +
  ``latency_threshold_ms``. Requiring both keeps sub-millisecond replay
  jitter from tripping the ratio bound.

``--ignore`` takes dot-paths with ``*`` wildcards (e.g.
``items[*].updated_at``); list indices are rendered as ``[*]`` so a single
pattern covers every element.
"""

import json
from collections import Counter

DIFF_FORMAT = "apireplay/diff-v1"

PASS = "PASS"
FAIL = "FAIL"

DEFAULT_HEADER_ALLOWLIST = ("content-type",)


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _join(path: str, segment: str) -> str:
    return f"{path}.{segment}" if path else segment


def _path_ignored(path: str, ignore_patterns) -> bool:
    if not path:
        return False
    segments = path.split(".")
    for pattern in ignore_patterns:
        psegs = pattern.split(".")
        if len(psegs) != len(segments):
            continue
        if all(p == "*" or p == s for p, s in zip(psegs, segments)):
            return True
    return False


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def json_diffs(expected, actual, ignore=(), unordered_lists=False, path="") -> list:
    """Recursive semantic JSON diff; returns a list of human-readable diffs."""
    if _path_ignored(path, ignore):
        return []
    if isinstance(expected, dict) and isinstance(actual, dict):
        diffs = []
        for key in expected:
            if key not in actual:
                diffs.append(f"{_join(path, key)}: missing in replayed body")
        for key in actual:
            if key not in expected:
                diffs.append(f"{_join(path, key)}: unexpected in replayed body")
        for key in expected:
            if key in actual:
                diffs.extend(
                    json_diffs(expected[key], actual[key], ignore,
                               unordered_lists, _join(path, key))
                )
        return diffs
    if isinstance(expected, list) and isinstance(actual, list):
        if unordered_lists:
            exp = Counter(_canonical(v) for v in expected)
            act = Counter(_canonical(v) for v in actual)
            diffs = []
            for canon in sorted(set(exp) | set(act)):
                if exp[canon] != act[canon]:
                    diffs.append(
                        f"{path or '<root>'}: list element counts differ "
                        f"(recorded {exp[canon]}, replayed {act[canon]}): {canon}"
                    )
            return diffs
        diffs = []
        if len(expected) != len(actual):
            diffs.append(
                f"{path or '<root>'}: list length differs "
                f"(recorded {len(expected)}, replayed {len(actual)})"
            )
        for i, (e_item, a_item) in enumerate(zip(expected, actual)):
            _ = i
            diffs.extend(
                json_diffs(e_item, a_item, ignore, unordered_lists,
                           f"{path}[*]" if path else "[*]")
            )
        return diffs
    if _is_number(expected) and _is_number(actual):
        if float(expected) != float(actual):
            return [f"{path or '<root>'}: {expected!r} != {actual!r}"]
        return []
    if type(expected) is not type(actual) or expected != actual:
        return [f"{path or '<root>'}: {expected!r} != {actual!r}"]
    return []


def _try_parse_json(text):
    try:
        return json.loads(text), True
    except (json.JSONDecodeError, ValueError, TypeError):
        return None, False


def _body_text(body, encoding):
    """Decode a pack body to text; return None for non-UTF-8 binary."""
    from .pack import decode_body

    data = decode_body(body or "", encoding)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def compare_bodies(exp_body, exp_enc, act_body, act_enc, ignore, unordered_lists):
    """Return ``(passed, details)`` for the body check."""
    exp_text = _body_text(exp_body, exp_enc)
    act_text = _body_text(act_body, act_enc)
    if exp_text is None or act_text is None:
        # Binary bodies: compare raw bytes.
        from .pack import decode_body

        same = decode_body(exp_body or "", exp_enc) == decode_body(act_body or "", act_enc)
        return same, [] if same else ["binary bodies differ"]
    exp_json, exp_ok = _try_parse_json(exp_text)
    act_json, act_ok = _try_parse_json(act_text)
    if exp_ok and act_ok:
        diffs = json_diffs(exp_json, act_json, ignore, unordered_lists)
        return not diffs, diffs
    same = exp_text == act_text
    return same, [] if same else ["bodies differ (non-JSON, exact match failed)"]


def _ci_get(headers: dict, name: str):
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


def compare_headers(exp_headers, act_headers, allowlist=DEFAULT_HEADER_ALLOWLIST):
    """Return ``(passed, details)``; only allowlisted headers are compared."""
    diffs = []
    for name in allowlist:
        expected = _ci_get(exp_headers or {}, name)
        actual = _ci_get(act_headers or {}, name)
        if expected != actual:
            diffs.append(
                f"header {name!r}: recorded {expected!r} != replayed {actual!r}"
            )
    return not diffs, diffs


def latency_regression(recorded_ms, replayed_ms, threshold_ms, ratio) -> bool:
    if recorded_ms is None or replayed_ms is None:
        return False
    # Both bounds must be exceeded. A pure ratio is meaningless near zero
    # (sub-millisecond recordings make replay jitter look like a 20x
    # regression), and a pure absolute bound ignores the endpoint's scale.
    return replayed_ms > recorded_ms * ratio and replayed_ms > recorded_ms + threshold_ms


def _percentile(sorted_values, pct):
    if not sorted_values:
        return None
    idx = min(len(sorted_values) - 1, max(0, int(-(-pct * len(sorted_values) // 1)) - 1))
    return sorted_values[idx]


def diff_pack_vs_results(pack, results, ignore=(), unordered_lists=False,
                         latency_threshold_ms=200.0, latency_ratio=2.0) -> dict:
    """Compare a pack against replay results; return a diff document."""
    by_id = {r.get("id"): r for r in results.get("results", [])}
    req_diffs = []
    recorded_lat, replayed_lat = [], []

    for req in pack.get("requests", []):
        recorded = req.get("recorded_response", {})
        actual = by_id.get(req.get("id"))
        checks = {}
        details = []

        # --- status ---
        if actual is None:
            checks["status"] = {"pass": False, "expected": recorded.get("status"),
                                "actual": None}
            details.append("no replay result for this request")
        elif actual.get("error"):
            checks["status"] = {"pass": False, "expected": recorded.get("status"),
                                "actual": None, "error": actual["error"]}
            details.append(f"replay error: {actual['error']}")
        else:
            ok = recorded.get("status") == actual.get("status")
            checks["status"] = {"pass": ok, "expected": recorded.get("status"),
                                "actual": actual.get("status")}
            if not ok:
                details.append(
                    f"status: recorded {recorded.get('status')} != "
                    f"replayed {actual.get('status')}"
                )

        # --- body ---
        if actual is not None and not actual.get("error"):
            ok, diffs = compare_bodies(
                recorded.get("body"), recorded.get("body_encoding"),
                actual.get("body"), actual.get("body_encoding"),
                ignore, unordered_lists,
            )
            checks["body"] = {"pass": ok, "diffs": diffs}
            details.extend(diffs)
        else:
            checks["body"] = {"pass": actual is not None and not actual.get("error"),
                              "diffs": [], "skipped": True}

        # --- headers ---
        if actual is not None and not actual.get("error"):
            ok, hdiffs = compare_headers(recorded.get("headers"), actual.get("headers"))
            checks["headers"] = {"pass": ok, "diffs": hdiffs}
            details.extend(hdiffs)
        else:
            checks["headers"] = {"pass": True, "diffs": [], "skipped": True}

        # --- latency ---
        rec_ms = recorded.get("latency_ms")
        act_ms = actual.get("latency_ms") if actual else None
        if rec_ms is not None:
            recorded_lat.append(rec_ms)
        if act_ms is not None:
            replayed_lat.append(act_ms)
        regressed = (
            actual is not None
            and not actual.get("error")
            and latency_regression(rec_ms, act_ms, latency_threshold_ms, latency_ratio)
        )
        checks["latency"] = {
            "pass": not regressed,
            "recorded_ms": rec_ms,
            "replayed_ms": act_ms,
            "regression": regressed,
        }
        if regressed:
            details.append(
                f"latency regression: recorded {rec_ms} ms, replayed {act_ms} ms "
                f"(threshold +{latency_threshold_ms} ms / x{latency_ratio})"
            )

        verdict = PASS if all(c["pass"] for c in checks.values()) else FAIL
        entry = {
            "id": req.get("id"),
            "method": req.get("method"),
            "path": req.get("path"),
            "verdict": verdict,
            "checks": checks,
            "details": details,
        }
        if verdict == FAIL:
            # Embed response snapshots so diff.json is a self-contained
            # artifact: `apireplay report` can render unified body diffs
            # from it without the original pack/results files.
            entry["_recorded"] = recorded
            entry["_replayed"] = (
                {"error": actual["error"]}
                if actual is not None and actual.get("error")
                else {
                    "status": actual.get("status") if actual else None,
                    "headers": actual.get("headers", {}) if actual else {},
                    "body": actual.get("body", "") if actual else "",
                    "body_encoding": actual.get("body_encoding") if actual else None,
                    "latency_ms": actual.get("latency_ms") if actual else None,
                }
            )
        req_diffs.append(entry)

    passed = sum(1 for r in req_diffs if r["verdict"] == PASS)
    recorded_lat.sort()
    replayed_lat.sort()
    return {
        "format": DIFF_FORMAT,
        "summary": {
            "total": len(req_diffs),
            "passed": passed,
            "failed": len(req_diffs) - passed,
            "latency_ms": {
                "recorded": {"p50": _percentile(recorded_lat, 0.50),
                             "p95": _percentile(recorded_lat, 0.95)},
                "replayed": {"p50": _percentile(replayed_lat, 0.50),
                             "p95": _percentile(replayed_lat, 0.95)},
            },
        },
        "requests": req_diffs,
    }


def save_diff(diff: dict, path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(diff, fh, indent=2)
        fh.write("\n")


def load_diff(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict) or data.get("format") != DIFF_FORMAT:
        raise ValueError(f"{path!r} is not an apireplay diff file")
    return data
