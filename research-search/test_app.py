"""Unit tests for the parsing helpers (run on host: python3 test_app.py)."""

import json
import http.client
import os
import socket
import sys
import threading
import urllib.error
from http.server import ThreadingHTTPServer
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app  # noqa: E402


def _serp_entry(url="https://example.com/a", rank=0, desc="<strong>Hi</strong> there"):
    return {
        "url": url,
        "title": "Example A",
        "description": desc,
        "snippets": ["first snippet", "second snippet"],
        "rank": rank,
        "date": None,
        "source": "brave",
        "can_open": True,
    }


class SearchParsing(unittest.TestCase):
    def test_dict_of_ids_string(self):
        raw = json.dumps({"oPZinQd8": _serp_entry(), "SkP7WwZ5": _serp_entry("https://example.com/b", 1)})
        entries = app.parse_search_entries(raw)
        self.assertEqual(len(entries), 2)
        self.assertEqual({e["url"] for e in entries}, {"https://example.com/a", "https://example.com/b"})

    def test_plain_list_and_results_wrapper(self):
        self.assertEqual(len(app.parse_search_entries([_serp_entry()])), 1)
        self.assertEqual(len(app.parse_search_entries({"results": [_serp_entry()]})), 1)

    def test_garbage(self):
        self.assertEqual(app.parse_search_entries(None), [])
        self.assertEqual(app.parse_search_entries("not json"), [])
        self.assertEqual(app.parse_search_entries(42), [])

    def test_harvest_dedupes_and_ranks(self):
        e1 = _serp_entry(rank=3)
        e2 = dict(e1, rank=0, title="Better")  # same url, better rank
        e3 = _serp_entry("https://example.com/b", rank=1)
        conv = {"outputs": [
            {"type": "tool.execution", "info": {"result": json.dumps({"x": e1, "y": e3})}},
            {"type": "tool.execution", "info": {"result": json.dumps({"z": e2})}},
            {"type": "message.output", "content": []},
        ]}
        got = app.harvest_search_entries(conv)
        self.assertEqual([e["url"] for e in got], ["https://example.com/a", "https://example.com/b"])
        self.assertEqual(got[0]["title"], "Better")

    def test_serp_snippet(self):
        snippet = app._serp_snippet(_serp_entry())
        self.assertEqual(snippet, "Hi there\nfirst snippet\nsecond snippet")


class PageExtraction(unittest.TestCase):
    def _outer(self, inner: str, key="truncated_response"):
        return json.dumps({key: inner, "info": "the tool response has been truncated to 32768 characters"})

    def test_full_chain(self):
        inner = json.dumps({"url": "https://example.com/page", "content": "Full page text"})
        block = json.dumps({"type": "text", "text": self._outer(inner, key="response")})
        page = app.extract_page(block)
        self.assertEqual(page["url"], "https://example.com/page")
        self.assertEqual(page["content"], "Full page text")

    def test_truncated_inner_json_is_salvaged(self):
        inner = json.dumps({"url": "https://example.com/long", "content": "A" * 50})[:-20]
        block = json.dumps({"type": "text", "text": self._outer(inner)})
        page = app.extract_page(block)
        self.assertEqual(page["url"], "https://example.com/long")
        self.assertTrue(page["content"].startswith("A"))
        self.assertGreater(len(page["content"]), 10)

    def test_direct_page_dict(self):
        page = app.extract_page(json.dumps({"url": "https://x.de", "content": "text", "title": "X"}))
        self.assertEqual(page["content"], "text")
        self.assertEqual(page["title"], "X")

    def test_no_content(self):
        self.assertIsNone(app.extract_page(json.dumps({"url": "https://x.de"})))
        self.assertIsNone(app.extract_page("not json at all"))


class TruncationDetection(unittest.TestCase):
    def test_truncated_wrapper_dict(self):
        payload = {"truncated_response": '{"url": "https://x.de", "content": "text"',
                   "info": "the tool response has been truncated to 32768 characters"}
        self.assertTrue(app._is_truncated_payload(payload))

    def test_untruncated_response_wrapper(self):
        payload = {"response": json.dumps({"url": "https://x.de", "content": "text"})}
        self.assertFalse(app._is_truncated_payload(payload))

    def test_nested_content_block(self):
        inner = json.dumps({"truncated_response": '{"url": "https://x.de", "content": "te'})
        block = json.dumps({"type": "text", "text": inner})
        self.assertTrue(app._is_truncated_payload(block))

    def test_plain_page_text_is_not_truncated(self):
        # A page legitimately mentioning the key name must not false-positive.
        page = json.dumps({"url": "https://x.de", "content": 'talks about "truncated_response" keys'})
        self.assertFalse(app._is_truncated_payload(page))

    def test_truncation_note_text(self):
        self.assertIn("truncated this page at 32768 characters", app.TRUNCATION_NOTE)


class SerpFallback(unittest.TestCase):
    """fetch_page_via_agent fallback: only the page's OWN SERP entry."""

    def _conv_with(self, entries):
        return {"outputs": [
            {"type": "tool.execution",
             "info": {"result": json.dumps({"x": e for x, e in zip("abcdefghij", entries)})}},
        ]}

    def test_falls_back_to_own_serp_entry(self):
        target = _serp_entry("https://example.com/want", desc="Wanted snippet")
        other = _serp_entry("https://example.com/other", rank=0, desc="Unrelated")
        self.addCleanup(setattr, app, "call_scout", app.call_scout)
        app.call_scout = lambda content: self._conv_with([other, target])
        page = app.fetch_page_via_agent("https://example.com/want")
        self.assertEqual(page["url"], "https://example.com/want")
        self.assertEqual(page["content"], "Wanted snippet\nfirst snippet\nsecond snippet")
        self.assertEqual(page["fetch_mode"], "serp")

    def test_no_unrelated_entry_when_url_absent(self):
        # A rank-0 entry for a DIFFERENT url must never stand in.
        other = _serp_entry("https://example.com/other", rank=0)
        self.addCleanup(setattr, app, "call_scout", app.call_scout)
        app.call_scout = lambda content: self._conv_with([other])
        self.assertIsNone(app.fetch_page_via_agent("https://example.com/want"))


class HTTPIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.server.shutdown()
        finally:
            cls.server.server_close()
            cls.thread.join(timeout=2)

    def setUp(self):
        self.original_token = app.RESEARCH_TOKEN
        self.original_authorization = app._EXPECTED_AUTHORIZATION
        app.RESEARCH_TOKEN = "integration-test-token"
        app._EXPECTED_AUTHORIZATION = b"Bearer integration-test-token"
        self.addCleanup(setattr, app, "RESEARCH_TOKEN", self.original_token)
        self.addCleanup(setattr, app, "_EXPECTED_AUTHORIZATION", self.original_authorization)

    def _post(self, body, *, path="/search", headers=None):
        request_headers = {
            "Authorization": "Bearer integration-test-token",
            "Content-Type": "application/json",
        }
        request_headers.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            conn.request("POST", path, body=body, headers=request_headers)
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def test_non_dict_json_is_rejected(self):
        status, body = self._post(b"[]")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "invalid_json")

    def test_malformed_json_is_rejected(self):
        status, body = self._post(b"not json")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "invalid_json")

    def test_oversized_content_length_is_rejected(self):
        status, body = self._post(
            b"", headers={"Content-Length": str(app.MAX_BODY_BYTES + 1)}
        )
        self.assertEqual(status, 413)

    def test_negative_content_length_is_rejected(self):
        with socket.create_connection(("127.0.0.1", self.server.server_port), timeout=3) as sock:
            sock.sendall(
                b"POST /search HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"Authorization: Bearer integration-test-token\r\n"
                b"Content-Length: -1\r\nConnection: close\r\n\r\n"
            )
            stream = sock.makefile("rb")
            status = int(stream.readline().split()[1])
            headers = {}
            while line := stream.readline():
                if line == b"\r\n":
                    break
                key, value = line.decode("latin-1").split(":", 1)
                headers[key.lower()] = value.strip()
            body = json.loads(stream.read(int(headers["content-length"])))
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "invalid Content-Length")

    def test_upstream_http_error_is_sanitized(self):
        def fail(_content):
            raise urllib.error.HTTPError("https://mistral.invalid", 503, "failure", {}, None)

        self.addCleanup(setattr, app, "call_scout", app.call_scout)
        app.call_scout = fail
        status, body = self._post(json.dumps({"query": "test"}).encode())
        self.assertEqual(status, 502)
        self.assertEqual(body["error"], "upstream_error")
        self.assertNotIn("detail", body)
        self.assertEqual(body["status"], 503)

    def test_generic_exception_is_sanitized(self):
        def fail(_content):
            raise RuntimeError("SENSITIVE-marker-text")

        self.addCleanup(setattr, app, "call_scout", app.call_scout)
        app.call_scout = fail
        with self.assertLogs(app.log, level="ERROR") as captured:
            status, body = self._post(json.dumps({"query": "test"}).encode())
        self.assertEqual(status, 502)
        self.assertEqual(body, {"error": "internal_error"})
        self.assertNotIn("SENSITIVE-marker-text", "\n".join(captured.output))

    def test_search_success_returns_results(self):
        conversation = {
            "outputs": [{
                "type": "tool.execution",
                "info": {"result": json.dumps({"hit": _serp_entry()})},
            }]
        }
        self.addCleanup(setattr, app, "call_scout", app.call_scout)
        app.call_scout = lambda _content: conversation
        status, body = self._post(json.dumps({"query": "test"}).encode())
        self.assertEqual(status, 200)
        self.assertEqual(body[0]["link"], "https://example.com/a")


class ResultShape(unittest.TestCase):
    def test_result_item_shape(self):
        item = app._result_item(_serp_entry())
        self.assertEqual(item["link"], "https://example.com/a")
        self.assertEqual(item["title"], "Example A")
        self.assertIn("snippet", item)
        self.assertNotIn("--- page excerpt ---", item["snippet"])


class UpstreamResponseSize(unittest.TestCase):
    def test_call_scout_enforces_upstream_size_boundary(self):
        original_urlopen = app.urllib.request.urlopen
        self.addCleanup(setattr, app.urllib.request, "urlopen", original_urlopen)

        doc = {"padding": ""}
        empty_doc = json.dumps(doc, separators=(",", ":")).encode("utf-8")
        if app.MAX_UPSTREAM_BYTES < len(empty_doc):
            self.skipTest("MAX_UPSTREAM_BYTES is too small for a JSON object")
        doc["padding"] = "x" * (app.MAX_UPSTREAM_BYTES - len(empty_doc))
        valid = json.dumps(doc, separators=(",", ":")).encode("utf-8")
        self.assertEqual(len(valid), app.MAX_UPSTREAM_BYTES)

        class FakeResponse:
            def __init__(self, body):
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def read(self, amount):
                if amount != app.MAX_UPSTREAM_BYTES + 1:
                    raise AssertionError("unexpected upstream read size")
                return self.body[:amount]

        responses = iter((valid, valid + b" "))
        app.urllib.request.urlopen = lambda _req, timeout: FakeResponse(next(responses))

        result = app.call_scout("x")
        self.assertIsInstance(result, dict)
        self.assertEqual(len(result["padding"]), app.MAX_UPSTREAM_BYTES - len(empty_doc))
        with self.assertRaises(ValueError):
            app.call_scout("x")


if __name__ == "__main__":
    unittest.main(verbosity=2)
