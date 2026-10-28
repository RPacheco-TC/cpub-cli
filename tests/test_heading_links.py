"""H3 (D36): heading links keep Confluence's native anchor (offline, in-process).

The anchor rule was measured on Confluence Cloud (H3.0 spike): the heading's shown
text, trimmed, every whitespace character -> ``-``, repeats ``.1``, ``.2``. Run
from the repo root::

    python3.12 -B -m unittest discover -s tests
"""
import pathlib
import unittest

from _support import anchors, docset, plan, storage
from _fake_confluence import FakeConfluence

NOTE = ("---\ntitle: T\n---\n# Spike\n\n## 3. How the two scopes connect\n\n"
        "## 6. Who may act on whom — inside a workspace\n\n## Notes: final\n\n"
        "## Repeated\n\n## Repeated\n\n"
        "See [[#3. How the two scopes connect]], [[#6. Who may act on whom — inside a "
        "workspace|§6]], [[#notes final]], [[#Repeated]] and [[#Gone]].\n")


def body(link_markup, text):
    return f"{link_markup}<ac:plain-text-link-body><![CDATA[{text}]]></ac:plain-text-link-body>" \
           "</ac:link>"


def convert(md, **kw):
    return storage.StorageConverter(**kw).convert(md)


class AnchorRule(unittest.TestCase):
    """AC-H3: the pure text -> anchor functions, on the spike's measured cases."""

    def test_measured_cases(self):
        for text, anchor in (
                ("3. How the two scopes connect", "3.-How-the-two-scopes-connect"),
                ("6. Who may act on whom — inside a workspace",
                 "6.-Who-may-act-on-whom-—-inside-a-workspace"),
                ("§ Sección con acentos ñ", "§-Sección-con-acentos-ñ"),
                ('Sub: colon, comma & "quotes" (parens) / slash?',
                 'Sub:-colon,-comma-&-"quotes"-(parens)-/-slash?'),
                ("Two  spaces here", "Two--spaces-here"),
                ("Tab\ttab and trailing ", "Tab-tab-and-trailing"),
                ("Already-hyphen - spaced", "Already-hyphen---spaced"),
                ("Hash # in C#", "Hash-#-in-C#"),
                ("Ünïcode 日本語 emoji 🚀", "Ünïcode-日本語-emoji-🚀")):
            with self.subTest(text=text):
                self.assertEqual(anchors.heading_anchor(anchors.heading_plain(text)), anchor)

    def test_inline_markup_keeps_only_its_words(self):
        self.assertEqual(anchors.heading_plain("With `code` and **bold** & <tags>"),
                         "With code and bold & <tags>")
        self.assertEqual(anchors.heading_plain("See [[Other note|the other]] and [[X#Y]]"),
                         "See the other and X")

    def test_repeats_are_numbered_across_levels_first_bare(self):
        heads = storage.note_headings("## Repeated\n## Repeated\n## Repeated\n### Repeated\n")
        found = anchors.HeadingAnchors(heads)
        self.assertEqual([e[2] for e in found.entries],
                         ["Repeated", "Repeated.1", "Repeated.2", "Repeated.3"])
        self.assertEqual(found.find("Repeated"), "Repeated")      # Obsidian: the first

    def test_a_heading_named_like_a_suffix_is_suffixed_again(self):
        """B2, measured live: Repeated / ### Repeated / Repeated.1 / Repeated."""
        found = anchors.HeadingAnchors(storage.note_headings(
            "## Repeated\n### Repeated\n## Repeated.1\n## Repeated\n"))
        self.assertEqual([e[2] for e in found.entries],
                         ["Repeated", "Repeated.1", "Repeated.1.1", "Repeated.2"])
        self.assertEqual(found.find("Repeated.1"), "Repeated.1.1")  # the literal heading
        taken = anchors.HeadingAnchors(storage.note_headings("## A.1\n## A\n## A\n"))
        self.assertEqual([e[2] for e in taken.entries], ["A.1", "A", "A.2"])

    def test_lenient_match_then_none(self):
        found = anchors.HeadingAnchors(storage.note_headings(
            "## Notes: final\n## With `code`\n"))
        self.assertEqual(found.find("Notes final"), "Notes:-final")
        self.assertEqual(found.find("notes: FINAL"), "Notes:-final")
        self.assertEqual(found.find("With code"), "With-code")      # the shown text
        self.assertIsNone(found.find("Elsewhere"))
        self.assertIsNone(found.find("—"))                          # no empty-key match


class CalloutHeadings(unittest.TestCase):
    """B3: exactly the <hN> of the body count, callout panels included (measured live)."""
    MD = ("> [!note] Title ## not this\n> ## Inside\n> text\n\n"
          "> [!tip]- Folded\n> ### Fold\n> ```\n> ## fenced\n> ```\n\n"
          "> ## Plain quote\n> > ## Nested\n\n## Inside\n## Fold\n")

    def test_callouts_count_quotes_do_not(self):
        heads = storage.note_headings(self.MD)
        self.assertEqual([(h.text, h.line, h.in_callout) for h in heads],
                         [("Inside", 2, True), ("Fold", 6, True),
                          ("Inside", 14, False), ("Fold", 15, False)])
        html = convert(self.MD).html                    # exactly the rendered headings
        self.assertEqual([html.count(f"<h{n}>") for n in range(1, 7)],
                         [[h.level for h in heads].count(n) for n in range(1, 7)])

    def test_repeats_span_the_callout_and_links_prefer_the_body_heading(self):
        found = anchors.HeadingAnchors(storage.note_headings(self.MD))
        self.assertEqual([e[2] for e in found.entries], ["Inside", "Fold", "Inside.1", "Fold.1"])
        self.assertEqual(found.find("Inside"), "Inside.1")
        self.assertEqual(found.find("fold"), "Fold.1")
        only = anchors.HeadingAnchors(storage.note_headings("> [!note]\n> ## Only here\n"))
        self.assertEqual(only.find("Only here"), "Only-here")     # no longer <em>
        self.assertEqual(convert("> [!note]\n> ## Only here\n\n[[#Only here]]\n")
                         .unresolved(lambda t: True), [])

    def test_a_numbered_callout_heading_counts_for_section_refs(self):
        md = "> [!note]\n> ## 3. In a panel\n\nSee §3.\n"
        self.assertEqual(storage.SectionRefFinder().find(md), [("§3", 4)])


class SameNote(unittest.TestCase):
    """[[#Heading]]: a same-page link, no ri:page (the spike's markup)."""

    def test_links_jump_to_the_real_heading(self):
        html = convert(NOTE).html
        self.assertIn(body('<ac:link ac:anchor="3.-How-the-two-scopes-connect">',
                           "3. How the two scopes connect"), html)
        self.assertIn(body('<ac:link ac:anchor="6.-Who-may-act-on-whom-—-inside-a-workspace">',
                           "§6"), html)                                # the alias shows
        self.assertIn(body('<ac:link ac:anchor="Notes:-final">', "notes final"), html)
        self.assertIn(body('<ac:link ac:anchor="Repeated">', "Repeated"), html)
        self.assertNotIn("<h2 id", html)                              # headings untouched

    def test_missing_heading_note_mode_text_set_mode_link(self):
        doc = convert(NOTE)
        self.assertIn(body('<ac:link ac:anchor="Gone">', "Gone"), doc.html)  # convert: kept
        html = doc.relink(lambda title: True).html                    # note mode (S2.5)
        self.assertIn("<em>Gone</em>", html)
        self.assertIn('ac:anchor="Notes:-final"', html)
        self.assertEqual(doc.unresolved(lambda title: True), ["#Gone"])

    def test_block_reference_has_nothing_to_jump_to(self):
        doc = convert("Para ^abc\n\nSee [[#^abc]] and [[#^abc|that]].\n")
        self.assertIn("<em>#^abc</em>", doc.html)
        self.assertIn("<em>that</em>", doc.html)
        self.assertNotIn("ac:link", doc.html)

    def test_anchor_attribute_is_xml_escaped_as_confluence_writes_it(self):
        doc = convert('## Sub: & "q" <t>\n\nSee [[#Sub: & "q" <t>]].\n')
        self.assertIn('ac:anchor="Sub:-&amp;-&quot;q&quot;-&lt;t&gt;"', doc.html)
        doc.validate()

    def test_frontmatter_and_fences_are_not_headings(self):
        doc = convert("---\n# not: a heading\n---\n```\n## Fenced\n```\nSee [[#Fenced]].\n")
        self.assertEqual(doc.unresolved(lambda t: True), ["#Fenced"])


class OtherPage(unittest.TestCase):
    """[[Page#Heading]]: ri:page plus the anchor; display unchanged."""

    def test_note_mode_anchor_from_the_link_text(self):
        html = convert("[[Other note#Some Heading]] [[Other note#A#B|x]]\n", prefix="R").html
        self.assertIn(body('<ac:link ac:anchor="Some-Heading">'
                           '<ri:page ri:content-title="R - Other note" />', "Other note"), html)
        self.assertIn(body('<ac:link ac:anchor="B"><ri:page ri:content-title="R - Other note" />',
                           "x"), html)                                 # nested: last heading

    def test_block_reference_and_plain_links_are_v14_bytes(self):
        for md in ("[[Other#^abc]]\n", "[[Other]]\n"):
            with self.subTest(md=md):
                self.assertIn(storage.Wikilink("Other", "Other").macro, convert(md).html)
                self.assertNotIn("ac:anchor", convert(md).html)

    def test_set_mode_resolves_against_that_documents_headings(self):
        ds = docset.DocSet((
            docset.SourceDoc("01 - A", "See [[02 - B#notes final]] and [[02 - B#Unknown x]].\n"),
            docset.SourceDoc("02 - B", "## Notes: final\n\nSee [[#Nope]].\n")))
        f = FakeConfluence()
        f.add("1", "Home")
        f.add("10", "TEST", parent="1")
        p = plan.Planner(f.client(), storage.StorageConverter(prefix="R")).build(ds, "S1", "10")
        a, b = (it.storage for it in p.items)
        self.assertIn('<ac:link ac:anchor="Notes:-final"><ri:page ri:content-title="R - 02 - B" />',
                      a)
        self.assertIn('ac:anchor="Unknown-x"', a)                     # not found: link text
        self.assertIn(body('<ac:link ac:anchor="Nope">', "Nope"), b)  # set mode keeps it
        self.assertEqual(p.unresolved_links, [])                       # no new output


class Unchanged(unittest.TestCase):
    """Bodies with no # link are byte-identical to 1.4.2."""

    def test_headings_without_anchor_links_change_nothing(self):
        md = "## 3. Three\n\nSee [[Page]] and [[Other|alias]].\n"
        doc = convert(md)
        self.assertNotIn("ac:anchor", doc.html)
        self.assertEqual(
            doc.html, "<h2>3. Three</h2>\n\n<p>See " + storage.Wikilink("Page", "Page").macro +
            " and " + storage.Wikilink("Other", "alias").macro + ".</p>\n")


if __name__ == "__main__":
    unittest.main()
