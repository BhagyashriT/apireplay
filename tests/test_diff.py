"""Tests for the comparison engine (diff.py)."""

import json
import unittest

from apireplay.diff import FAIL, PASS, diff_pack_vs_results


def _pack_response(body="", status=200, headers=None, latency_ms=10.0):
    return {
        "status": status,
        "headers": headers or {"content-type": "application/json"},
        "body": body,
        "latency_ms": latency_ms,
    }


def _pack_and_results(requests_spec):
    """Build (pack, results) from [(request_overrides, result_overrides)]."""
    pack = {"format": "apireplay/v1", "created_at": "t", "source": "proxy", "requests": []}
    results = {"format": "apireplay/results-v1", "target": "t", "replayed_at": "t",
               "results": []}
    for i, (req_ov, res_ov) in enumerate(requests_spec):
        req = {
            "id": f"req-{i + 1:04d}",
            "method": "GET",
            "url": "http://x/api",
            "path": "/api",
            "query": {},
            "headers": {},
            "body": "",
            "recorded_response": _pack_response(),
        }
        req.update(req_ov)
        pack["requests"].append(req)
        res = {
            "id": req["id"],
            "method": req["method"],
            "path": req["path"],
            "status": req["recorded_response"]["status"],
            "headers": dict(req["recorded_response"]["headers"]),
            "body": req["recorded_response"]["body"],
            "latency_ms": req["recorded_response"]["latency_ms"],
            "error": None,
        }
        res.update(res_ov)
        results["results"].append(res)
    return pack, results


def _diff(pack, results, **kwargs):
    return diff_pack_vs_results(pack, results, **kwargs)


class StatusTest(unittest.TestCase):
    def test_identical_passes(self):
        pack, results = _pack_and_results([({}, {})])
        d = _diff(pack, results)
        self.assertEqual(d["requests"][0]["verdict"], PASS)
        self.assertEqual(d["summary"], {**d["summary"], "total": 1, "passed": 1, "failed": 0})

    def test_status_mismatch_fails(self):
        pack, results = _pack_and_results([({}, {"status": 500})])
        d = _diff(pack, results)
        req = d["requests"][0]
        self.assertEqual(req["verdict"], FAIL)
        self.assertFalse(req["checks"]["status"]["pass"])
        self.assertIn("500", req["details"][0])

    def test_replay_error_fails(self):
        pack, results = _pack_and_results([({}, {"status": None, "error": "Timeout: boom"})])
        d = _diff(pack, results)
        req = d["requests"][0]
        self.assertEqual(req["verdict"], FAIL)
        self.assertIn("Timeout", req["details"][0])


class BodyTest(unittest.TestCase):
    def _body_case(self, recorded, replayed, **kwargs):
        pack, results = _pack_and_results([(
            {"recorded_response": _pack_response(body=recorded)},
            {"body": replayed},
        )])
        return _diff(pack, results, **kwargs)["requests"][0]

    def test_json_key_order_ignored(self):
        req = self._body_case('{"b": 2, "a": 1}', '{"a": 1, "b": 2}')
        self.assertEqual(req["verdict"], PASS)

    def test_numeric_equality(self):
        req = self._body_case('{"n": 1}', '{"n": 1.0}')
        self.assertEqual(req["verdict"], PASS)

    def test_json_value_change_fails(self):
        req = self._body_case('{"a": 1}', '{"a": 2}')
        self.assertEqual(req["verdict"], FAIL)
        self.assertTrue(any("a" in detail for detail in req["details"]))

    def test_missing_key_fails(self):
        req = self._body_case('{"a": 1, "b": 2}', '{"a": 1}')
        self.assertEqual(req["verdict"], FAIL)

    def test_ignore_paths(self):
        req = self._body_case(
            '{"id": 1, "created_at": "2020-01-01"}',
            '{"id": 1, "created_at": "2026-10-16"}',
            ignore=["created_at"],
        )
        self.assertEqual(req["verdict"], PASS)

    def test_ignore_wildcard_paths(self):
        recorded = '{"items": [{"ts": "a", "v": 1}, {"ts": "b", "v": 2}]}'
        replayed = '{"items": [{"ts": "x", "v": 1}, {"ts": "y", "v": 2}]}'
        req = self._body_case(recorded, replayed, ignore=["items[*].ts"])
        self.assertEqual(req["verdict"], PASS)
        # without the ignore, the same bodies must fail
        req2 = self._body_case(recorded, replayed)
        self.assertEqual(req2["verdict"], FAIL)

    def test_ordered_lists_by_default(self):
        req = self._body_case('{"l": [1, 2]}', '{"l": [2, 1]}')
        self.assertEqual(req["verdict"], FAIL)

    def test_unordered_lists_flag(self):
        req = self._body_case('{"l": [1, 2, 2]}', '{"l": [2, 1, 2]}', unordered_lists=True)
        self.assertEqual(req["verdict"], PASS)
        req2 = self._body_case('{"l": [1, 2]}', '{"l": [1, 2, 2]}', unordered_lists=True)
        self.assertEqual(req2["verdict"], FAIL)

    def test_non_json_exact_match(self):
        req = self._body_case("hello", "hello")
        self.assertEqual(req["verdict"], PASS)
        req2 = self._body_case("hello", "bye")
        self.assertEqual(req2["verdict"], FAIL)

    def test_type_mismatch_fails(self):
        req = self._body_case('{"a": 1}', '{"a": "1"}')
        self.assertEqual(req["verdict"], FAIL)


class HeadersTest(unittest.TestCase):
    def test_allowlisted_header_mismatch_fails(self):
        pack, results = _pack_and_results([(
            {"recorded_response": _pack_response(headers={"content-type": "application/json"})},
            {"headers": {"content-type": "text/plain"}},
        )])
        req = _diff(pack, results)["requests"][0]
        self.assertEqual(req["verdict"], FAIL)
        self.assertFalse(req["checks"]["headers"]["pass"])

    def test_unlisted_headers_ignored(self):
        pack, results = _pack_and_results([(
            {"recorded_response": _pack_response(
                headers={"content-type": "application/json", "x-request-id": "aaa"})},
            {"headers": {"content-type": "application/json", "x-request-id": "bbb",
                         "x-extra": "1"}},
        )])
        req = _diff(pack, results)["requests"][0]
        self.assertEqual(req["verdict"], PASS)

    def test_header_name_case_insensitive(self):
        pack, results = _pack_and_results([(
            {"recorded_response": _pack_response(headers={"Content-Type": "application/json"})},
            {"headers": {"content-type": "application/json"}},
        )])
        req = _diff(pack, results)["requests"][0]
        self.assertEqual(req["verdict"], PASS)


class LatencyTest(unittest.TestCase):
    def _lat_case(self, recorded_ms, replayed_ms, **kwargs):
        pack, results = _pack_and_results([(
            {"recorded_response": _pack_response(latency_ms=recorded_ms)},
            {"latency_ms": replayed_ms},
        )])
        return _diff(pack, results, **kwargs)["requests"][0]

    def test_no_regression_passes(self):
        req = self._lat_case(100.0, 120.0)
        self.assertEqual(req["verdict"], PASS)
        self.assertTrue(req["checks"]["latency"]["pass"])

    def test_clear_regression_fails(self):
        # 350 > 100*2.0 AND 350 > 100+200 -> both bounds exceeded
        req = self._lat_case(100.0, 350.0)
        self.assertEqual(req["verdict"], FAIL)
        self.assertTrue(req["checks"]["latency"]["regression"])

    def test_ratio_alone_does_not_flag(self):
        # 250 > 100*2.0 but 250 < 100+200: small absolute change, no flag
        req = self._lat_case(100.0, 250.0)
        self.assertEqual(req["verdict"], PASS)

    def test_submillisecond_noise_rejected(self):
        # Same-machine replay jitter (1ms -> 20ms) must not trip the ratio.
        req = self._lat_case(1.0, 20.0)
        self.assertEqual(req["verdict"], PASS)

    def test_threshold_bound_with_small_threshold(self):
        # 700 > 300+200 AND 700 > 300*2.0 -> flagged
        req = self._lat_case(300.0, 700.0)
        self.assertEqual(req["verdict"], FAIL)

    def test_latency_summary_percentiles(self):
        specs = [(
            {"recorded_response": _pack_response(latency_ms=float(ms))},
            {"latency_ms": float(ms * 2)},
        ) for ms in (10, 20, 30, 40)]
        pack, results = _pack_and_results(specs)
        summary = _diff(pack, results)["summary"]["latency_ms"]
        self.assertIsNotNone(summary["recorded"]["p50"])
        self.assertIsNotNone(summary["replayed"]["p95"])
        self.assertGreater(summary["replayed"]["p50"], summary["recorded"]["p50"])


class DiffArtifactsTest(unittest.TestCase):
    def test_failing_request_embeds_bodies(self):
        pack, results = _pack_and_results([({}, {"status": 500})])
        req = _diff(pack, results)["requests"][0]
        self.assertIn("_recorded", req)
        self.assertIn("_replayed", req)
        self.assertEqual(req["_replayed"]["status"], 500)

    def test_passing_request_has_no_snapshots(self):
        pack, results = _pack_and_results([({}, {})])
        req = _diff(pack, results)["requests"][0]
        self.assertNotIn("_recorded", req)


if __name__ == "__main__":
    unittest.main()
