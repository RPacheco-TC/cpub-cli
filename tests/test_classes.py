"""R2 class seams: no shared state, one fence rule, typed errors (offline, in-process).

Run from the repo root::

    python3.12 -B -m unittest discover -s tests
"""
import io
import unittest
import urllib.error

from _support import api, docset, errors, markdown_html, storage
from _fake_confluence import FakeConfluence

MERMAID = "```mermaid\ngraph TD; A-->B\n```\n"


class StubMermaid:
    """A MermaidRenderer stand-in: no npx, no files."""

    def __init__(self, name):
        self.name, self.seen = name, []

    def render(self, code):
        self.seen.append(code)
        return f'<img src="img/{self.name}.png" alt="Mermaid diagram" />'


class RendererState(unittest.TestCase):
    def test_two_renderers_do_not_share_an_image_folder(self):
        a = markdown_html.MarkdownRenderer(StubMermaid("a"))
        b = markdown_html.MarkdownRenderer(StubMermaid("b"))
        plain = markdown_html.MarkdownRenderer()
        self.assertIn("img/a.png", a.render(MERMAID))
        self.assertIn("img/b.png", b.render(MERMAID))
        self.assertIn("Diagram (Mermaid)", plain.render(MERMAID))   # fallback, untouched

    def test_no_module_level_image_state(self):
        self.assertFalse(hasattr(markdown_html, "_current_img_dir"))
        self.assertFalse(hasattr(markdown_html, "_current_img_rel"))

    def test_callout_bodies_never_render_mermaid(self):
        stub = StubMermaid("x")
        conv = storage.StorageConverter(renderer=markdown_html.MarkdownRenderer(stub))
        html = conv.convert("> [!note] N\n> ```mermaid\n> graph TD; A-->B\n> ```\n").html
        self.assertEqual(stub.seen, [])                 # v1.1: fallback block inside callouts
        self.assertIn("Diagram (Mermaid)", html)


class FenceRule(unittest.TestCase):
    DOC = "```\n[[TOC]]\n[[Link]]\n> [!note] t\n```\n"

    def test_every_extractor_leaves_fenced_lines_alone(self):
        for ex in (storage.TocExtractor(), storage.WikilinkExtractor("p"),
                   storage.CalloutExtractor(markdown_html.MarkdownRenderer())):
            with self.subTest(extractor=type(ex).__name__):
                md, macros = ex.extract(self.DOC)
                self.assertEqual((md, macros), (self.DOC, {}))
                self.assertIsInstance(ex, storage.FenceAwareExtractor)

    def test_one_fence_rule(self):
        for cls in (storage.TocExtractor, storage.WikilinkExtractor, storage.CalloutExtractor):
            self.assertNotIn("is_fence", vars(cls))
            self.assertNotIn("rewrite", vars(cls))


class Transport(unittest.TestCase):
    def test_http_error_keeps_confluence_detail(self):
        def urlopen(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 400, "Bad Request", {},
                                         io.BytesIO(b'{"message": "parent is a folder"}'))
        t = api.HttpTransport(urlopen=urlopen)
        with self.assertRaises(urllib.error.HTTPError) as cm:
            t.request("GET", "https://example.invalid/wiki/api/v2/pages/1")
        self.assertEqual(cm.exception.detail, '{"message": "parent is a folder"}')

    def test_transport_refuses_plain_http_before_any_request(self):
        """B8 defence in depth: even a URL built elsewhere never goes out over http."""
        def urlopen(request, timeout):
            raise AssertionError("a request was sent")
        t = api.HttpTransport(urlopen=urlopen)
        for url in ("http://example.invalid/wiki/x", "http://localhost/wiki/x",
                    "http://127.0.0.1.evil.com/wiki/x"):
            with self.subTest(url=url), self.assertRaises(errors.InsecureBaseUrl):
                t.request("GET", url, headers={"Authorization": "Basic c2VjcmV0"})

    def test_transport_allows_http_to_a_loopback_ip_literal(self):
        """D29: QA's local recorder on http://127.0.0.1 keeps working."""
        sent = []

        class Resp(io.BytesIO):
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def urlopen(request, timeout):
            sent.append(request.full_url)
            return Resp(b"{}")
        t = api.HttpTransport(urlopen=urlopen)
        for url in ("http://127.0.0.1:8080/wiki/x", "http://[::1]/wiki/x"):
            self.assertEqual(t.request("GET", url), (200, {}))
        self.assertEqual(len(sent), 2)

    def test_client_sends_auth_and_accept_headers(self):
        seen = []

        class Recorder:
            def request(self, method, url, data=None, headers=None):
                seen.append(headers)
                return 200, {"results": [{"id": "7"}]}

        client = api.ConfluenceClient(FakeConfluence().client().credentials, Recorder())
        self.assertEqual(client.resolve_space_id("ENG"), "7")
        self.assertEqual(set(seen[0]), {"Authorization", "Accept"})
        self.assertTrue(seen[0]["Authorization"].startswith("Basic "))


class TypedErrors(unittest.TestCase):
    def test_unknown_space_is_space_not_found(self):
        f = FakeConfluence()

        def no_space(method, url, data=None, headers=None):
            return 200, {"results": []}

        f.request = no_space
        with self.assertRaises(errors.SpaceNotFound) as cm:
            f.client().resolve_space_id("NOPE")
        self.assertEqual((cm.exception.key, str(cm.exception)),
                         ("NOPE", "Space not found for key: NOPE"))
        self.assertIsInstance(cm.exception, errors.PublisherError)
        self.assertNotIsInstance(cm.exception, SystemExit)

    def test_doc_set_path_must_be_a_folder(self):
        with self.assertRaises(errors.NotAFolder) as cm:
            docset.DocSet.from_folder("no/such/dir/for/tests")
        self.assertEqual(cm.exception.path, "no/such/dir/for/tests")

    def test_docset_entries_and_duplicates(self):
        ds = docset.DocSet((docset.SourceDoc("01 - A", "x"), docset.SourceDoc("In Situ", "y")),
                           (docset.SourceDoc("Deep", "z"),))
        self.assertEqual([(e.title, e.placement) for e in ds.entries()],
                         [("01 - A", "top"), ("In Situ", "top"),
                          ("In Situ", "container"), ("Deep", "in-situ")])
        self.assertEqual(ds.duplicate_titles(), ["In Situ"])


if __name__ == "__main__":
    unittest.main()
