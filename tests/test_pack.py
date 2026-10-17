"""Tests for the pack model (pack.py): save/load round-trip and validation."""

import base64
import json
import os
import tempfile
import unittest

from apireplay.pack import (
    PackError,
    decode_body,
    encode_body,
    load_pack,
    new_pack,
    save_pack,
    validate_pack,
)


def _valid_request():
    return {
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
            "body": '{"id": 1}',
            "latency_ms": 12.4,
        },
    }


class RoundTripTest(unittest.TestCase):
    def test_save_load_round_trip(self):
        pack = new_pack("har")
        pack["requests"].append(_valid_request())
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            path = fh.name
        try:
            save_pack(pack, path)
            loaded = load_pack(path)
        finally:
            os.unlink(path)
        self.assertEqual(loaded, pack)

    def test_binary_body_base64_round_trip(self):
        raw = bytes(range(256))  # not valid UTF-8
        text, encoding = encode_body(raw)
        self.assertEqual(encoding, "base64")
        self.assertEqual(decode_body(text, encoding), raw)

        text2, encoding2 = encode_body(b'{"a": 1}')
        self.assertIsNone(encoding2)
        self.assertEqual(decode_body(text2, encoding2), b'{"a": 1}')

    def test_binary_body_in_pack(self):
        pack = new_pack("proxy")
        req = _valid_request()
        text, encoding = encode_body(b"\xff\xfe binary")
        req["body"] = text
        req["body_encoding"] = encoding
        pack["requests"].append(req)
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            path = fh.name
        try:
            save_pack(pack, path)
            loaded = load_pack(path)
        finally:
            os.unlink(path)
        self.assertEqual(loaded["requests"][0]["body_encoding"], "base64")
        self.assertEqual(
            decode_body(loaded["requests"][0]["body"], "base64"), b"\xff\xfe binary")


class ValidationTest(unittest.TestCase):
    def _write_and_load(self, obj):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(obj, fh)
            path = fh.name
        try:
            return load_pack(path)
        finally:
            os.unlink(path)

    def test_rejects_wrong_format(self):
        pack = new_pack("har")
        pack["format"] = "apireplay/v9"
        with self.assertRaises(PackError):
            self._write_and_load(pack)

    def test_rejects_missing_requests(self):
        pack = {"format": "apireplay/v1", "created_at": "t", "source": "har"}
        with self.assertRaises(PackError):
            self._write_and_load(pack)

    def test_rejects_bad_source(self):
        pack = new_pack("har")
        pack["source"] = "telegraph"
        with self.assertRaises(PackError):
            self._write_and_load(pack)

    def test_rejects_request_missing_method(self):
        pack = new_pack("har")
        req = _valid_request()
        del req["method"]
        pack["requests"].append(req)
        with self.assertRaises(PackError):
            self._write_and_load(pack)

    def test_rejects_non_integer_status(self):
        pack = new_pack("har")
        req = _valid_request()
        req["recorded_response"]["status"] = "200"
        pack["requests"].append(req)
        with self.assertRaises(PackError):
            self._write_and_load(pack)

    def test_rejects_unreadable_file(self):
        with self.assertRaises(PackError):
            load_pack("/nonexistent/path/pack.json")

    def test_validate_returns_errors_without_raising(self):
        errors = validate_pack({"format": "nope"})
        self.assertTrue(errors)

    def test_new_pack_rejects_unknown_source(self):
        with self.assertRaises(PackError):
            new_pack("pigeon")


if __name__ == "__main__":
    unittest.main()
