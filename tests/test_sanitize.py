"""Tests for the redaction engine (sanitize.py)."""

import json
import os
import tempfile
import unittest

from apireplay.pack import new_pack
from apireplay.sanitize import REDACTED, Rules, sanitize_pack


def _pack_with_request(**overrides):
    pack = new_pack("proxy")
    req = {
        "id": "req-0001",
        "method": "GET",
        "url": "http://localhost:8080/api/users/1",
        "path": "/api/users/1",
        "query": {},
        "headers": {},
        "body": "",
        "recorded_response": {
            "status": 200,
            "headers": {},
            "body": "",
            "latency_ms": 1.0,
        },
    }
    req.update(overrides)
    pack["requests"].append(req)
    return pack


class SanitizeHeadersTest(unittest.TestCase):
    def test_sensitive_headers_redacted_case_insensitive(self):
        pack = _pack_with_request(headers={
            "Authorization": "Bearer abc123",
            "X-API-KEY": "key-xyz",
            "Content-Type": "application/json",
        })
        clean, summary = sanitize_pack(pack)
        headers = clean["requests"][0]["headers"]
        self.assertEqual(headers["Authorization"], REDACTED)
        self.assertEqual(headers["X-API-KEY"], REDACTED)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(summary["headers"], 2)

    def test_recorded_response_headers_redacted(self):
        pack = _pack_with_request()
        pack["requests"][0]["recorded_response"]["headers"] = {"Set-Cookie": "sess=1"}
        clean, summary = sanitize_pack(pack)
        self.assertEqual(
            clean["requests"][0]["recorded_response"]["headers"]["Set-Cookie"], REDACTED)
        self.assertEqual(summary["headers"], 1)


class SanitizeQueryTest(unittest.TestCase):
    def test_query_params_redacted(self):
        pack = _pack_with_request(
            url="http://x/api?token=abc&password=hunter2&page=2",
            query={"token": "abc", "password": "hunter2", "page": "2"},
        )
        clean, summary = sanitize_pack(pack)
        query = clean["requests"][0]["query"]
        self.assertEqual(query["token"], REDACTED)
        self.assertEqual(query["password"], REDACTED)
        self.assertEqual(query["page"], "2")
        from urllib.parse import parse_qsl, urlsplit

        url_query = dict(parse_qsl(urlsplit(clean["requests"][0]["url"]).query))
        self.assertEqual(url_query["token"], REDACTED)
        self.assertEqual(url_query["password"], REDACTED)
        self.assertEqual(url_query["page"], "2")
        self.assertEqual(summary["query_params"], 2)


class SanitizeBodyTest(unittest.TestCase):
    def test_json_nested_redaction(self):
        body = json.dumps({
            "user": {"email": "ada@example.com", "name": "Ada"},
            "cards": [{"number": "4111 1111 1111 1111"}],
            "ssn": "123-45-6789",
        })
        pack = _pack_with_request(body=body)
        clean, summary = sanitize_pack(pack)
        parsed = json.loads(clean["requests"][0]["body"])
        self.assertEqual(parsed["user"]["email"], REDACTED)
        self.assertEqual(parsed["user"]["name"], "Ada")
        self.assertEqual(parsed["cards"][0]["number"], REDACTED)
        self.assertEqual(parsed["ssn"], REDACTED)
        self.assertEqual(summary["body_values"], 3)

    def test_raw_text_fallback(self):
        body = "call me at ada@example.com or use Bearer abcdefgh12345678"
        pack = _pack_with_request(body=body)
        clean, summary = sanitize_pack(pack)
        out = clean["requests"][0]["body"]
        self.assertNotIn("ada@example.com", out)
        self.assertNotIn("Bearer abcdefgh12345678", out)
        self.assertIn("call me at", out)
        self.assertEqual(summary["body_values"], 2)

    def test_aws_key_and_sk_secret(self):
        body = json.dumps({
            "aws": "AKIAIOSFODNN7EXAMPLE",
            "openai": "sk-abcdefghijklmnop123456",
        })
        clean, summary = sanitize_pack(_pack_with_request(body=body))
        parsed = json.loads(clean["requests"][0]["body"])
        self.assertEqual(parsed["aws"], REDACTED)
        self.assertEqual(parsed["openai"], REDACTED)
        self.assertEqual(summary["body_values"], 2)

    def test_recorded_response_body_redacted(self):
        pack = _pack_with_request()
        pack["requests"][0]["recorded_response"]["body"] = json.dumps(
            {"email": "ada@example.com"})
        clean, summary = sanitize_pack(pack)
        parsed = json.loads(clean["requests"][0]["recorded_response"]["body"])
        self.assertEqual(parsed["email"], REDACTED)
        self.assertEqual(summary["body_values"], 1)

    def test_base64_body_redacted(self):
        import base64

        raw = json.dumps({"email": "ada@example.com"}).encode()
        pack = _pack_with_request(
            body=base64.b64encode(raw).decode(), body_encoding="base64")
        clean, summary = sanitize_pack(pack)
        decoded = base64.b64decode(clean["requests"][0]["body"]).decode()
        self.assertEqual(json.loads(decoded)["email"], REDACTED)
        self.assertEqual(clean["requests"][0]["body_encoding"], "base64")
        self.assertEqual(summary["body_values"], 1)


class CustomRulesTest(unittest.TestCase):
    def test_custom_rules_extend_defaults(self):
        rules_data = {
            "headers": ["x-internal-token"],
            "query_params": ["session_id"],
            "body_patterns": [{"name": "internal_id", "regex": r"INT-[0-9]{6}"}],
        }
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(rules_data, fh)
            path = fh.name
        try:
            rules = Rules.from_file(path)
        finally:
            os.unlink(path)
        pack = _pack_with_request(
            headers={"X-Internal-Token": "s3cr3t", "Authorization": "Bearer x"},
            query={"session_id": "abc"},
            body='order INT-123456 for ada@example.com',
        )
        clean, summary = sanitize_pack(pack, rules)
        req = clean["requests"][0]
        self.assertEqual(req["headers"]["X-Internal-Token"], REDACTED)
        self.assertEqual(req["headers"]["Authorization"], REDACTED)  # default kept
        self.assertEqual(req["query"]["session_id"], REDACTED)
        self.assertNotIn("INT-123456", req["body"])
        self.assertNotIn("ada@example.com", req["body"])
        self.assertEqual(summary["headers"], 2)
        self.assertEqual(summary["query_params"], 1)
        self.assertEqual(summary["body_values"], 2)

    def test_input_pack_not_mutated(self):
        pack = _pack_with_request(headers={"Authorization": "Bearer x"})
        sanitize_pack(pack)
        self.assertEqual(pack["requests"][0]["headers"]["Authorization"], "Bearer x")


if __name__ == "__main__":
    unittest.main()
