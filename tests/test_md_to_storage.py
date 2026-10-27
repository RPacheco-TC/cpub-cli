"""Regression tests for the Markdown -> Confluence storage conversion.

Run from the repo root (offline: no network, no npx, no Confluence env vars)::

    python3.12 -B -m unittest discover -s tests
"""
import unittest

from _support import cp


def storage(md: str, **kw) -> str:
    """Convert ``md`` with diagram rendering off and return the storage body."""
    body, _atts = cp.md_to_storage(md, None, **kw)
    return body


def code_bodies(html: str) -> list[str]:
    """Return the CDATA text of every code macro in ``html``."""
    opener = "<ac:plain-text-body><![CDATA["   # code macros only, not ac:link bodies
    out, start = [], 0
    while True:
        i = html.find(opener, start)
        if i < 0:
            return out
        j = html.find("]]></ac:plain-text-body>", i)
        out.append(html[i + len(opener):j])
        start = j


class NoTokenLeakMixin:
    def assertNoTokens(self, html):
        for tok in ("xCALLOUT", "xTOC", "xLINK"):
            self.assertNotIn(tok, html, f"placeholder {tok}… leaked into output")


class FencedContentIsLiteral(NoTokenLeakMixin, unittest.TestCase):
    """AC1-AC3: structure-looking lines inside ``` fences stay verbatim."""

    def test_callout_inside_fence(self):
        html = storage("Intro\n\n```\n> [!info] Hi\n> body\n```\n")
        self.assertNoTokens(html)
        self.assertEqual(code_bodies(html), ["> [!info] Hi\n> body"])
        self.assertNotIn('ac:name="info"', html)

    def test_toc_inside_fence(self):
        html = storage("Intro\n\n```\n[[TOC]]\n```\n")
        self.assertNoTokens(html)
        self.assertEqual(code_bodies(html), ["[[TOC]]"])
        self.assertNotIn('ac:name="toc"', html)

    def test_wikilink_inside_fence(self):
        html = storage("Intro\n\n```\nsee [[01 - Foo]] here\n```\n", prefix="REPO")
        self.assertNoTokens(html)
        self.assertEqual(code_bodies(html), ["see [[01 - Foo]] here"])
        self.assertNotIn("<ac:link", html)


class OutsideFencesStillConverts(NoTokenLeakMixin, unittest.TestCase):
    """AC4: the normal conversions are unchanged."""

    def test_callout(self):
        html = storage("> [!warning] Careful\n> body text\n")
        self.assertIn('<ac:structured-macro ac:name="warning">', html)
        self.assertIn("Careful", html)
        self.assertNoTokens(html)

    def test_toc(self):
        html = storage("# T\n\n[[TOC]]\n\n## A\n")
        self.assertIn('<ac:structured-macro ac:name="toc"', html)
        self.assertNoTokens(html)

    def test_wikilink(self):
        html = storage("See [[01 - Foo]].\n", prefix="REPO")
        self.assertIn("<ac:link>", html)
        self.assertIn('ri:content-title="REPO - 01 - Foo"', html)
        self.assertNoTokens(html)

    def test_wikilink_anchor_dropped_from_target_and_text(self):
        html = storage("See [[03 - Edge Cases#Code fences]].\n", prefix="REPO")
        self.assertIn('ri:content-title="REPO - 03 - Edge Cases"', html)
        self.assertIn("<![CDATA[03 - Edge Cases]]>", html)
        self.assertNotIn("Code fences", html)

    def test_wikilink_alias_wins_over_anchor(self):
        html = storage("See [[03 - Edge Cases#Code fences|the fences]].\n", prefix="REPO")
        self.assertIn("<![CDATA[the fences]]>", html)
        self.assertNotIn("Code fences", html)

    def test_plain_blockquote_untouched(self):
        # Edge case 6: a normal quote is not a callout.
        html = storage("> just a quote\n")
        self.assertIn("<blockquote>", html)
        self.assertNotIn("ac:structured-macro", html)


class FenceEdgeCases(NoTokenLeakMixin, unittest.TestCase):
    """Edge cases 1-5 from the spec."""

    def test_language_tagged_fence(self):
        html = storage("```python\n> [!info] x\n[[TOC]]\n[[01 - Foo]]\n```\n")
        self.assertNoTokens(html)
        self.assertEqual(code_bodies(html), ["> [!info] x\n[[TOC]]\n[[01 - Foo]]"])

    def test_indented_opening_fence(self):
        # Both the extractors and the converter accept a fence with leading spaces.
        html = storage("Intro\n\n  ```\n> [!info] x\n[[TOC]]\n  ```\n")
        self.assertNoTokens(html)
        self.assertEqual(code_bodies(html), ["> [!info] x\n[[TOC]]"])

    def test_unclosed_fence_runs_to_eof(self):
        html = storage("Intro\n\n```\n> [!info] x\n[[TOC]]\n[[01 - Foo]]\n")
        self.assertNoTokens(html)
        self.assertNotIn("<ac:link", html)
        self.assertNotIn('ac:name="toc"', html)
        self.assertNotIn('ac:name="info"', html)

    def test_structure_right_after_closing_fence_converts(self):
        html = storage("```\ncode\n```\n> [!tip] After\n> ok\n\n[[TOC]]\n\nSee [[01 - Foo]].\n")
        self.assertIn('ac:name="tip"', html)
        self.assertIn('ac:name="toc"', html)
        self.assertIn("<ac:link>", html)
        self.assertNoTokens(html)

    def test_multiple_fences_alternating(self):
        md = (
            "> [!info] One\n> a\n\n"
            "```\n> [!info] literal\n```\n\n"
            "[[TOC]]\n\n"
            "```\n[[TOC]]\n```\n\n"
            "See [[01 - Foo]].\n\n"
            "```\n[[02 - Bar]]\n```\n"
        )
        html = storage(md)
        self.assertNoTokens(html)
        self.assertEqual(html.count('ac:name="info"'), 1)
        self.assertEqual(html.count('ac:name="toc"'), 1)
        self.assertEqual(html.count("<ac:link>"), 1)
        self.assertEqual(code_bodies(html), ["> [!info] literal", "[[TOC]]", "[[02 - Bar]]"])

    def test_callout_continuation_lines_inside_fence_not_swallowed(self):
        html = storage("```\n> [!note] T\n> line 2\n> line 3\nafter\n```\n")
        self.assertEqual(code_bodies(html), ["> [!note] T\n> line 2\n> line 3\nafter"])
        self.assertNotIn("<ac:rich-text-body>", html)


class ImageWidth(unittest.TestCase):
    """AC7-AC8: optional ac:width on Mermaid image macros."""

    IMG = '<p><img src="img/abc123.png" alt="diagram" /></p>'

    def test_default_emits_no_width(self):
        html, atts = cp._images_to_attachments(self.IMG, "/tmp/x")
        self.assertIn('<ac:image><ri:attachment ri:filename="abc123.png" /></ac:image>', html)
        self.assertNotIn("ac:width", html)
        self.assertEqual([a[0] for a in atts], ["abc123.png"])

    def test_width_is_emitted_and_valid(self):
        html, atts = cp._images_to_attachments(self.IMG, "/tmp/x", 700)
        self.assertIn('<ac:image ac:width="700"><ri:attachment ri:filename="abc123.png" /></ac:image>', html)
        self.assertEqual([a[0] for a in atts], ["abc123.png"])  # filenames unchanged
        cp.validate_storage(html)

    def test_md_to_storage_accepts_img_width_without_images(self):
        md = "# T\n\nNo diagrams here.\n"
        self.assertEqual(storage(md), storage(md, img_width=700))


if __name__ == "__main__":
    unittest.main()
