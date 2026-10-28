"""Plain-text section references (H2, AC-H2.1-H2.2), in-process and offline.

A bare ``§N`` that names a numbered heading of the same note is reported in one
warning line after the plan; it is never a refusal and never changes a body or a
request. Detection is :class:`~confluence_publisher.storage.SectionRefFinder`
(pure); the wording is :meth:`~confluence_publisher.cli_output.PlanPrinter.section_refs`.
Run from the repo root::

    python3.12 -B -m unittest discover -s tests
"""
import io
import types
import unittest

from _support import cli_output, docset, plan, storage
from _fake_confluence import FakeConfluence
from test_cli_note import NoteRun

ROLES = ("---\ntitle: Roles\n---\n"          # lines 1-3: frontmatter, counted
         "# Roles\n"                          # 4
         "\n"                                 # 5
         "Scopes are in §3; who acts is in §6.\n"           # 6
         "## 3. How the two scopes connect\n"               # 7
         "text\n"                                           # 8
         "## 6. Who may act on whom — inside a workspace\n"  # 9
         "See §3 again.\n")                                 # 10
LINE = ("3 section references are plain text: §3 (line 6), §6 (line 6), §3 (line 10). "
        "Link them as [[#Heading|§N]].\n")


def find(md):
    return storage.SectionRefFinder().find(md)


class Finder(unittest.TestCase):
    def test_bare_refs_to_numbered_headings_with_file_line_numbers(self):
        self.assertEqual(find(ROLES), [("§3", 6), ("§6", 6), ("§3", 10)])

    def test_sub_sections_and_a_space_after_the_sign(self):
        md = "## 2. Top\n### 2.1 Sub\n#### 2.1.3. Deeper\nSee § 2.1 and §2.1.3, then §2.\n"
        self.assertEqual(find(md), [("§2.1", 4), ("§2.1.3", 4), ("§2", 4)])

    def test_a_ref_that_matches_no_heading_is_not_reported(self):
        md = ("## 3. Three\nSee § *OBX-maind*, §4, §30, §3.1 and the other doc's §12.\n"
              "## 2026 plans\n## 7 Seven without a dot\nSee §2026 and §7.\n")
        self.assertEqual(find(md), [])

    def test_already_linked_refs_are_not_reported(self):
        md = "## 3. Three\nSee [[#3. Three|§3]], [[Other#3. X|§3]] and [§3](#three).\n"
        self.assertEqual(find(md), [])

    def test_code_is_skipped_with_the_extractors_rules(self):
        md = "## 3. Three\n```\nsee §3\n```\nInline `§3` stays; this §3 counts.\n"
        self.assertEqual(find(md), [("§3", 5)])

    def test_headings_inside_a_fence_or_the_frontmatter_do_not_count(self):
        md = "---\nnote: §3\n---\n```\n## 3. Not a heading\n```\nSee §3.\n"
        self.assertEqual(find(md), [])

    def test_the_same_ref_twice_on_a_line_is_listed_once(self):
        self.assertEqual(find("## 1. One\n§1 and §1, then §1\n§1\n"), [("§1", 2), ("§1", 3)])

    def test_a_trailing_full_stop_is_not_part_of_the_number(self):
        self.assertEqual(find("## 5. Five\nThe end is §5.\n"), [("§5", 2)])


class Headings(unittest.TestCase):
    """The one heading parser (shared with H3's same-note anchors)."""

    def test_text_level_line_and_number(self):
        hs = storage.note_headings(ROLES + "```\n## 9. In code\n```\n### Closing ##\n")
        self.assertEqual([(h.level, h.text, h.line, h.number) for h in hs], [
            (1, "Roles", 4, None),
            (2, "3. How the two scopes connect", 7, "3"),
            (2, "6. Who may act on whom — inside a workspace", 9, "6"),
            (3, "Closing", 14, None)])

    def test_numbers(self):
        def number(text):
            return storage.Heading(2, text, 1).number
        self.assertEqual([number(t) for t in ("3. A", "3.2 B", "3.2. C", "7 D", "2026 plans",
                                              "§ Sección con acentos ñ", "3.")],
                         ["3", "3.2", "3.2", None, None, None, "3"])

    def test_not_headings(self):
        self.assertEqual(storage.note_headings("#tag\n    # indented code\n#\n"), [])


class Wording(unittest.TestCase):
    def line(self, items, per_page=False):
        out = io.StringIO()
        cli_output.PlanPrinter(out).section_refs(items, per_page=per_page)
        return out.getvalue()

    def item(self, title, refs):
        return types.SimpleNamespace(title=title, section_refs=refs)

    def test_one_and_many(self):
        self.assertEqual(self.line([self.item("N", [("§3", 48)])]),
                         "1 section reference is plain text: §3 (line 48). "
                         "Link it as [[#Heading|§N]].\n")
        self.assertEqual(self.line([self.item("N", [("§3", 48), ("§6", 222)])]),
                         "2 section references are plain text: §3 (line 48), §6 (line 222). "
                         "Link them as [[#Heading|§N]].\n")

    def test_set_mode_names_each_page_and_skips_pages_without_refs(self):
        items = [self.item("R - 01 - A", [("§2", 9)]), self.item("R - 02 - B", []),
                 self.item("In Situ", None)]
        self.assertEqual(self.line(items, per_page=True),
                         "In R - 01 - A: 1 section reference is plain text: §2 (line 9). "
                         "Link it as [[#Heading|§N]].\n")

    def test_nothing_when_there_is_none(self):
        self.assertEqual(self.line([self.item("N", [])]), "")


class EndToEnd(NoteRun):
    """AC-H2.1: one line on a real run and on --dry-run, in both modes."""

    def test_note_dry_run_and_real_run(self):
        for extra in (("--dry-run",), ()):
            with self.subTest(extra=extra):
                msg, out, _ = self.run_cli("My Note.md", "--under", "10", *extra,
                                           markdown=ROLES)
                self.assertIsNone(msg, msg)
                self.assertEqual(out.count(LINE), 1)

    def test_note_to_mode(self):
        msg, out, _ = self.run_cli("My Note.md", "--to", "20", "--adopt", markdown=ROLES)
        self.assertIsNone(msg, msg)
        self.assertEqual(out.count(LINE), 1)

    def test_set_mode_line_names_the_page(self):
        md = "## 1. One\nSee §1.\n"
        for extra in (("--dry-run",), ()):
            with self.subTest(extra=extra):
                msg, out, _ = self.run_cli("docs", "--parent", "10", *extra, docs_md=md)
                self.assertIsNone(msg, msg)
                self.assertIn("In 01 - A: 1 section reference is plain text: §1 (line 2). "
                              "Link it as [[#Heading|§N]].\n", out)

    def test_no_line_without_refs(self):
        _, out, _ = self.run_cli("My Note.md", "--under", "10", "--dry-run")
        self.assertNotIn("section reference", out)

    def test_a_refused_run_still_warns_and_still_writes_nothing(self):
        msg, out, _ = self.run_cli("My Note.md", "--to", "1", "--adopt", markdown=ROLES)
        self.assertTrue(msg.startswith("Error: "), msg)        # the homepage refusal
        self.assertEqual(out.count(LINE), 1)
        self.assertEqual(self.f.writes, [])


class WarningOnly(unittest.TestCase):
    """AC-H2.2: the same requests and the same body as with no detection at all."""

    class NoFinder:
        def find(self, md):
            return []

    def plan_with(self, sections, replace):
        f = FakeConfluence()
        f.add("1", "Home")
        f.add("10", "TEST", parent="1")
        f.add("20", "Hand written", parent="10", version=7)
        planner = plan.Planner(f.client(), storage.StorageConverter("", None, None),
                               sections=sections)
        note = docset.SingleNote("My Note.md", markdown=ROLES)
        p = planner.build_replace(note, "20") if replace else planner.build(note, None, "10")
        return p, f.calls

    def test_body_and_requests_are_unchanged(self):
        for replace in (True, False):
            with self.subTest(replace=replace):
                found, calls = self.plan_with(None, replace)
                plain, plain_calls = self.plan_with(self.NoFinder(), replace)
                self.assertEqual(len(found.items[0].section_refs), 3)
                self.assertEqual(found.items[0].storage, plain.items[0].storage)
                self.assertEqual(calls, plain_calls)


if __name__ == "__main__":
    unittest.main()
