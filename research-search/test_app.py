"""Unit tests for the parsing helpers (run on host: python3 test_app.py)."""

import json
import os
import sys
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


class HtmlText(unittest.TestCase):
    def test_extraction(self):
        html = ("<html><head><title>My Title</title><style>.x{}</style></head>"
                "<body><script>bad()</script><h1>Hello</h1><p>World &amp; more</p>"
                "<div>Second line</div></body></html>")
        ex = app._TextExtractor()
        ex.feed(html)
        text = ex.text()
        self.assertEqual(ex.title.strip(), "My Title")
        self.assertIn("Hello", text)
        self.assertIn("World & more", text)
        self.assertNotIn("bad()", text)
        self.assertNotIn(".x", text)

    def test_result_item_shape(self):
        entries = [_serp_entry()]
        results = [{"link": e["url"], "title": e["title"], "snippet": app._serp_snippet(e)} for e in entries]
        self.assertEqual(results[0]["link"], "https://example.com/a")
        self.assertIn("snippet", results[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
