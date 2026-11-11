import json
from pathlib import Path
import unittest
from unittest.mock import patch

from pydantic import ValidationError
import requests

import searxng_image_search as extension


def image(number=1, **extra):
    return {"img_src": f"https://images.example/{number}.jpg", "url": "https://example.org/page",
            "title": "Example image", "engine": "example", **extra}


class Response:
    def __init__(self, data=None, *, raw=None, status=200):
        self.raw = raw if raw is not None else json.dumps(data).encode()
        self.status_code = status
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def iter_content(self, chunk_size):
        for start in range(0, len(self.raw), chunk_size):
            yield self.raw[start:start + chunk_size]


class ImageSearchTests(unittest.TestCase):
    def setUp(self):
        self.tool = extension.Tools()

    def search(self, results, **kwargs):
        response = Response({"results": results})
        with patch.object(extension.requests, "get", return_value=response) as get:
            output = self.tool.image_search("test", **kwargs)
        self.assertTrue(response.closed)
        return output, get.call_args

    def test_adapter_imports_tools_without_duplicate_implementation(self):
        code = (Path(__file__).parent / "tool.py").read_text()
        namespace = {}
        exec(compile(code, "tool.py", "exec"), namespace)
        self.assertIs(namespace["Tools"], extension.Tools)

    def test_empty_query_does_not_send_http(self):
        with patch.object(extension.requests, "get") as get:
            self.assertIn("provide a search query", self.tool.image_search(" \n"))
        get.assert_not_called()

    def test_request_defaults_and_parameter_encoding(self):
        output, call = self.search([image(n) for n in range(10)])
        self.assertEqual(output.count("!["), 4)
        self.assertEqual(call.args, ("http://searxng:8080/search",))
        self.assertEqual(call.kwargs["params"],
                         {"q": "test", "format": "json", "categories": "images", "safesearch": 1})
        self.assertEqual(call.kwargs["timeout"], 20)
        self.assertIs(call.kwargs["allow_redirects"], False)
        self.assertIs(call.kwargs["stream"], True)

    def test_query_is_not_interpolated_into_url(self):
        query = 'cats & "dogs"? # fragment'
        with patch.object(extension.requests, "get", return_value=Response({"results": []})) as get:
            self.tool.image_search(query)
        self.assertEqual(get.call_args.kwargs["params"]["q"], query)
        self.assertNotIn(query, get.call_args.args[0])

    def test_count_clamping_and_custom_defaults(self):
        self.tool.valves.DEFAULT_COUNT = 2
        for value, expected in [(0, 2), (-1, 2), (1, 1), (100, 8)]:
            with self.subTest(count=value):
                out, _ = self.search([image(n) for n in range(12)], count=value)
                self.assertEqual(out.count("!["), expected)

    def test_safesearch_clamping_and_time_ranges(self):
        self.tool.valves.DEFAULT_SAFE_SEARCH = 2
        for value, expected in [(-1, 2), (0, 0), (1, 1), (5, 2)]:
            _, call = self.search([], safesearch=value)
            self.assertEqual(call.kwargs["params"]["safesearch"], expected)
        for value in ["day", "week", "month", "year"]:
            _, call = self.search([], time_range=value)
            self.assertEqual(call.kwargs["params"]["time_range"], value)
        _, call = self.search([], time_range="invalid")
        self.assertNotIn("time_range", call.kwargs["params"])

    def test_deduplicates_skips_malformed_results_and_requires_image_url(self):
        out, _ = self.search([None, 3, "bad", {}, {"url": "https://page.example/"},
                              image(), image(), image(2)])
        self.assertEqual(out.count("!["), 2)
        self.assertIn("[source](<https://example.org/page>)", out)

    def test_unsafe_image_urls_are_rejected(self):
        for url in [None, 5, "", "javascript:alert(1)", "data:image/png;base64,x", "//host/pic",
                    "file:///etc/passwd", "https://", "https://user:pass@host/pic",
                    "https://host/pic\nmore", "https://host:99999/pic", "https://bad host/pic",
                    "https://host\\@other/pic"]:
            with self.subTest(url=url):
                out, _ = self.search([image(img_src=url)])
                self.assertIn("No usable image results", out)

    def test_unsafe_source_omitted_and_text_types_tolerated(self):
        out, _ = self.search([image(url="javascript:alert(1)", title={}, source=[], engine=None)])
        self.assertIn("![image]", out)
        self.assertNotIn("[source]", out)

    def test_markdown_injection_and_newlines_are_escaped(self):
        out, _ = self.search([image(img_src='https://host/a)![injected](https://evil/x "a".jpg',
                                    title="]\n![fake](bad)<script>", source="evil\n# heading")])
        self.assertNotIn(")![injected]", out)
        self.assertNotIn("<script>", out)
        self.assertIn("%29", out)
        self.assertIn("%20", out)
        self.assertIn("%22", out)
        self.assertNotIn("\n# heading", out)

    def test_truncation_does_not_leave_dangling_escape(self):
        text = extension._escape_md("[" * 200)
        self.assertTrue(text.endswith("..."))
        self.assertEqual(text.count("\\["), 117)

    def test_non_json_and_invalid_response_shapes(self):
        for raw in [b"<html>bad</html>", b"\xff", b"[]", b"null", b"{}", b'{"results":{}}']:
            with self.subTest(raw=raw), patch.object(extension.requests, "get", return_value=Response(raw=raw)):
                self.assertEqual(self.tool.image_search("test"), extension._FORMAT_ERROR)

    def test_http_failure_redirect_and_network_errors_are_sanitized(self):
        for status in [301, 403, 429, 500]:
            with patch.object(extension.requests, "get", return_value=Response(status=status)):
                self.assertEqual(self.tool.image_search("test"), extension._FETCH_ERROR)
        for error in [requests.Timeout("secret details"), requests.ConnectionError("private URL")]:
            with patch.object(extension.requests, "get", side_effect=error):
                self.assertEqual(self.tool.image_search("test"), extension._FETCH_ERROR)

    def test_stream_failure_is_sanitized(self):
        response = Response({"results": []})
        with patch.object(response, "iter_content", side_effect=requests.ConnectionError("private")), \
                patch.object(extension.requests, "get", return_value=response):
            self.assertEqual(self.tool.image_search("test"), extension._FETCH_ERROR)
        self.assertTrue(response.closed)

    def test_response_size_is_bounded(self):
        response = Response(raw=b"x" * 128)
        with patch.object(extension, "_MAX_RESPONSE_BYTES", 64), \
                patch.object(extension.requests, "get", return_value=response):
            self.assertEqual(self.tool.image_search("test"), extension._FORMAT_ERROR)
        self.assertTrue(response.closed)

    def test_empty_results(self):
        output, _ = self.search([])
        self.assertIn("No image results found", output)

    def test_valve_validation_and_proxy_subpath(self):
        for field, value in [("TIMEOUT", 0), ("TIMEOUT", 121), ("DEFAULT_COUNT", 0),
                             ("DEFAULT_COUNT", 9), ("DEFAULT_SAFE_SEARCH", 3),
                             ("SEARXNG_URL", "file:///etc"), ("SEARXNG_URL", "https://user:pw@host"),
                             ("SEARXNG_URL", "https://host/?secret=x")]:
            with self.subTest(field=field, value=value), self.assertRaises(ValidationError):
                extension.Tools.Valves(**{field: value})
        self.tool.valves = self.tool.Valves(SEARXNG_URL="https://host/searx/")
        _, call = self.search([])
        self.assertEqual(call.args[0], "https://host/searx/search")
