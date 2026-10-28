"""S2.5: in note mode, links to notes with no page in the space become plain text.

Offline: the title lookup is ``FakeConfluence``'s space listing (or a plain
function for the storage-level tests). Run from the repo root::

    python3.12 -B -m unittest discover -s tests
"""
import pathlib
import unittest

from _support import docset, plan, storage
from _fake_confluence import FakeConfluence

NOTE = ("# Note\n\nSee [[Existing]], [[Missing|the missing one]], [[Missing#Part]],\n"
        "[[A and B]] and [[My note]].\n\n```\n[[Fenced]]\n```\n")


def link(title, display, anchor=None):
    return storage.Wikilink(title, display, anchor=anchor).macro


def tree():
    f = FakeConfluence()
    f.add("1", "Home")
    f.add("10", "TEST", parent="1")
    f.add("11", "Existing", parent="1")          # anywhere in the space counts
    f.add("40", "Their page", parent="10")
    return f


def note(md=NOTE, name="My note.md"):
    return docset.SingleNote(pathlib.Path("/vault") / name, None, md)


class Restore(unittest.TestCase):
    """AC-2.5.3: only the xLINKNx restore step changes."""

    def test_convert_alone_is_unchanged_every_link_kept(self):
        doc = storage.StorageConverter().convert(NOTE)
        for title, display in (("Existing", "Existing"), ("Missing", "the missing one"),
                               ("A and B", "A and B")):
            self.assertIn(link(title, display), doc.html)
        self.assertIn(link("Missing", "Missing", "Part"), doc.html)      # H3: anchor kept
        self.assertIn("[[Fenced]]", doc.html)                     # fence rule unchanged
        self.assertEqual(doc.relink(lambda t: True).html, doc.html)

    def test_missing_targets_become_emphasised_escaped_text(self):
        doc = storage.StorageConverter().convert(NOTE)
        exists = {"Existing"}.__contains__
        html = doc.relink(exists).html
        self.assertIn(link("Existing", "Existing"), html)
        self.assertIn("<em>the missing one</em>", html)
        self.assertIn("<em>Missing</em>", html)                   # anchor dropped as before
        self.assertEqual(storage.Wikilink("x", "A & <B>").text, "<em>A &amp; &lt;B&gt;</em>")
        self.assertNotIn('ri:content-title="Missing"', html)
        self.assertEqual(doc.unresolved(exists), ["Missing", "A and B", "My note"])
        self.assertEqual(doc.html, storage.StorageConverter().convert(NOTE).html)  # untouched

    def test_prefixed_targets_are_looked_up_prefixed(self):
        doc = storage.StorageConverter(prefix="R").convert("[[A]] [[B]]\n")
        self.assertEqual(doc.unresolved({"R - A"}.__contains__), ["R - B"])  # the missing title
        self.assertIn("<em>B</em>", doc.relink({"R - A"}.__contains__).html)


    def test_names_are_the_missing_titles_without_anchor_or_alias(self):
        doc = storage.StorageConverter(prefix="R").convert(
            "[[Other note#Part|label]] [[Other note]] [[#Only anchor]]\n")
        self.assertEqual(doc.unresolved(lambda t: False), ["R - Other note", "#Only anchor"])
        plain = storage.StorageConverter().convert("[[Other note#Part|label]]\n")
        self.assertEqual(plain.unresolved(lambda t: False), ["Other note"])  # no prefix


class InlineCode(unittest.TestCase):
    """B11: [[…]] in an inline code span stays literal, like in a fenced block."""

    def convert(self, md):
        return storage.StorageConverter().convert(md)

    def test_single_and_double_backtick_spans_stay_literal(self):
        for md in ("Write `[[X]]` to link.\n", "Write ``[[X]]`` to link.\n"):
            with self.subTest(md=md):
                doc = self.convert(md)
                self.assertEqual(doc.links, {})
                self.assertIn("<code>[[X]]</code>", doc.html)
                self.assertNotIn("ac:link", doc.html)

    def test_a_span_next_to_a_real_link(self):
        doc = self.convert("Type `[[Note]]` to get [[Note]]; also `a` [[B|b]] `c`.\n")
        self.assertEqual([(l.target, l.display) for l in doc.links.values()],
                         [("Note", "Note"), ("B", "b")])
        self.assertIn("<code>[[Note]]</code>", doc.html)
        self.assertIn(link("Note", "Note"), doc.html)
        self.assertEqual(doc.unresolved(lambda t: False), ["Note", "B"])

    def test_an_unclosed_backtick_is_not_a_span(self):
        doc = self.convert("A stray ` then [[X]]\n")
        self.assertIn(link("X", "X"), doc.html)

    def test_toc_and_callouts_are_line_based_and_unaffected(self):
        md = "[[TOC]]\n\n> [!note] See `code`\n> body\n"
        self.assertEqual(self.convert(md).html,
                         storage.StorageConverter().convert(md).html)
        self.assertIn('ac:name="toc"', self.convert(md).html)
        self.assertIn('ac:name="info"', self.convert(md).html)


class Escaping(unittest.TestCase):
    """B10: link titles with & < > (were invalid XML) now publish; others unchanged."""
    CASES = ("[[A & B]]", "[[Q&A|alias & more]]", "[[a < b]]", "[[Tom's note]]",
             "[[x > y|y]]", '[[Say "hi"]]', "[[odd ]]> text]]")

    def test_special_characters_give_valid_storage_in_both_modes(self):
        for md in self.CASES:
            for exists in (None, lambda t: False):
                with self.subTest(md=md, exists=exists):
                    doc = storage.StorageConverter(prefix="R & D").convert(md + "\n")
                    if exists is not None:
                        doc = doc.relink(exists)            # validates
                    doc.validate()

    def test_title_is_escaped_and_cdata_text_is_not(self):
        html = storage.StorageConverter().convert("[[Q&A|a < b & c]]\n").html
        self.assertIn('ri:content-title="Q&amp;A"', html)
        self.assertIn("<![CDATA[a < b & c]]>", html)            # CDATA is never decoded
        self.assertIn("<em>a &lt; b &amp; c</em>",
                      storage.StorageConverter().convert("[[Q&A|a < b & c]]\n")
                      .relink(lambda t: False).html)

    def test_plain_and_apostrophe_titles_are_byte_identical_to_before(self):
        def old_macro(title, display):                # ab158ec's _wikilink_macro
            t = title.replace('"', "&quot;")
            return (f'<ac:link><ri:page ri:content-title="{t}" />'
                    f"<ac:plain-text-link-body><![CDATA[{display}]]>"
                    "</ac:plain-text-link-body></ac:link>")
        for title, display in (("01 - Foo", "01 - Foo"), ("Tom's note", "alias"),
                               ('Say "hi"', 'Say "hi"'), ("R - In Situ", "In Situ")):
            self.assertEqual(storage.Wikilink(title, display).macro, old_macro(title, display))


class NoteMode(unittest.TestCase):
    def planner(self, f):
        return plan.Planner(f.client(), storage.StorageConverter())

    def test_under_resolves_against_the_space_and_keeps_a_self_link(self):
        f = tree()
        p = self.planner(f).build(note(), "S1", "10")
        html = p.items[0].storage
        self.assertEqual(p.unresolved_links, ["Missing", "A and B"])
        self.assertIn(link("Existing", "Existing"), html)
        self.assertIn(link("My note", "My note"), html)          # the page being created
        self.assertIn("<em>the missing one</em>", html)
        self.assertEqual(p.items[0].new_size, len(html.encode()))
        self.assertEqual(f.calls.count(("GET", "/api/v2/spaces/S1/pages")), 1)   # read once

    def test_to_reads_the_titles_once_and_only_when_there_are_links(self):
        f = tree()
        p = self.planner(f).build_replace(note(), "40")
        self.assertEqual(p.unresolved_links, ["Missing", "A and B", "My note"])
        self.assertIn(link("Their page", "Their page"), self.planner(f).build_replace(
            note("[[Their page]]\n"), "40").items[0].storage)     # --to keeps the page title
        self.assertEqual(f.calls.count(("GET", "/api/v2/spaces/S1/pages")), 2)
        g = tree()
        q = self.planner(g).build_replace(note("# No links\n"), "40")
        self.assertEqual(q.unresolved_links, [])
        self.assertNotIn(("GET", "/api/v2/spaces/S1/pages"), g.calls)

    def test_nothing_unresolved_means_the_v11_body(self):
        f = tree()
        p = self.planner(f).build(note("[[Existing]]\n"), "S1", "10")
        self.assertEqual(p.unresolved_links, [])
        self.assertEqual(p.items[0].storage,
                         storage.StorageConverter().convert("[[Existing]]\n").html)


class SetModeUnchanged(unittest.TestCase):
    def test_set_mode_keeps_every_link(self):
        """AC-2.5.4: pages of a set link to each other before they exist."""
        f = tree()
        ds = docset.DocSet((docset.SourceDoc("01 - A", "[[02 - B]] [[Nowhere]]\n"),
                            docset.SourceDoc("02 - B", "x\n")))
        p = plan.Planner(f.client(), storage.StorageConverter()).build(ds, "S1", "10")
        self.assertEqual(p.unresolved_links, [])
        self.assertEqual(p.items[0].storage, storage.StorageConverter().convert(
            "[[02 - B]] [[Nowhere]]\n").html)
        self.assertIn(link("Nowhere", "Nowhere"), p.items[0].storage)


if __name__ == "__main__":
    unittest.main()
