"""Note library: page refs, AppDirs, SingleNote (+ S2.4 targets), --under and --to.

Offline and in-process: notes are in-memory ``SingleNote`` objects and
Confluence is the injected ``FakeConfluence`` transport. Run from the repo root::

    python3.12 -B -m unittest discover -s tests
"""
import json
import pathlib
import unittest

from _support import config, docset, errors, executor, pageref, plan, storage
from _fake_confluence import FakeConfluence

BASE = "https://acme.atlassian.net/wiki"
TODAY = "2026-10-27"
NOTE_MD = "---\ntitle: Release notes\n---\n# Hi\n\nBody & more.\n"


def tree():
    """Home(1) ─┬─ TEST(10) ── Area(11)
                └─ Other team(20)"""
    f = FakeConfluence()
    f.add("1", "Home")
    f.add("10", "TEST", parent="1")
    f.add("11", "Area", parent="10")
    f.add("20", "Other team", parent="1")
    return f


def note(md=NOTE_MD, title=None, name="release.md"):
    return docset.SingleNote(pathlib.Path("/vault") / name, title, md)


def managed(source):
    return {"source": source, "tool": plan.MARKER_TOOL, "since": "2026-01-01"}


def planner(f, prefix=""):
    return plan.Planner(f.client(), storage.StorageConverter(prefix=prefix))


def run(f, p, **kw):
    return executor.PlanExecutor(f.client(), today=TODAY).execute(p, **kw)


class PageRefParse(unittest.TestCase):
    def test_accepted_forms(self):
        cases = {
            "123456": ("123456", None),
            f"{BASE}/spaces/ENG/pages/42": ("42", "ENG"),
            f"{BASE}/spaces/ENG/pages/42/Some+Title?focused=1#h": ("42", "ENG"),
            f"{BASE}/spaces/~me/pages/edit-v2/77": ("77", "~me"),
            f"{BASE}/pages/viewpage.action?pageId=99&x=1": ("99", None),
            "  123  ": ("123", None),
        }
        for text, (pid, key) in cases.items():
            with self.subTest(text=text):
                ref = pageref.PageRef.parse(text, BASE)
                self.assertEqual((ref.page_id, ref.space_key), (pid, key))

    def test_folder_url_rejected(self):
        with self.assertRaises(errors.FolderPageRef) as cm:
            pageref.PageRef.parse(f"{BASE}/spaces/ENG/folder/5", BASE)
        self.assertEqual(str(cm.exception), "That URL is a folder; publish under a page instead.")
        self.assertTrue(cm.exception.fix)

    def test_short_link_and_unknown_list_the_accepted_forms(self):
        for text, exc in ((f"{BASE}/x/AbCd", errors.ShortLinkPageRef),
                          (f"{BASE}/spaces/ENG/overview", errors.UnrecognisedPageRef),
                          ("not a page", errors.UnrecognisedPageRef)):
            with self.subTest(text=text), self.assertRaises(exc) as cm:
                pageref.PageRef.parse(text, BASE)
            self.assertIn("viewpage.action?pageId=ID", cm.exception.fix)
            self.assertIsInstance(cm.exception, errors.PublisherError)

    def test_foreign_host_rejected_naming_both(self):
        with self.assertRaises(errors.ForeignHostPageRef) as cm:
            pageref.PageRef.parse("https://evil.example/wiki/spaces/ENG/pages/42", BASE)
        self.assertIn("evil.example", str(cm.exception))
        self.assertIn("acme.atlassian.net", str(cm.exception))


class Dirs(unittest.TestCase):
    def test_cache_and_config_dirs(self):
        d = config.AppDirs({"HOME": "/home/u"})
        self.assertEqual(d.note_img_dir, pathlib.Path("/home/u/.cache/confluence-publisher/img"))
        self.assertEqual(d.config_dir, pathlib.Path("/home/u/.config/confluence-publisher"))
        x = config.AppDirs({"HOME": "/h", "XDG_CACHE_HOME": "/c", "XDG_CONFIG_HOME": "/g"})
        self.assertEqual((x.cache_dir, x.config_dir),
                         (pathlib.Path("/c/confluence-publisher"),
                          pathlib.Path("/g/confluence-publisher")))


class Note(unittest.TestCase):
    def test_title_precedence(self):
        self.assertEqual(note(title="Explicit").resolved_title(), "Explicit")
        self.assertEqual(note().resolved_title(), "Release notes")
        self.assertEqual(note(md="# no frontmatter\n").resolved_title(), "release")

    def test_entry_carries_the_filename_as_marker_source(self):
        (e,) = note().entries()
        self.assertEqual((e.title, e.source_id, e.placement),
                         ("Release notes", "release.md", "top"))

    def test_frontmatter_reader(self):
        md = "---\ntitle: 'Q: a'\nconfluence: \"https://x\"\nlist:\n  - a\n---\nbody"
        self.assertEqual(docset.read_frontmatter(md),
                         {"title": "Q: a", "confluence": "https://x", "list": ""})
        self.assertEqual(docset.read_frontmatter("no frontmatter"), {})
        self.assertEqual(docset.read_frontmatter("---\ntitle: open\n"), {})

    def test_non_markdown_path_is_refused(self):
        for path in ("tests/test_note.py", "no/such/note.md", "tests"):
            with self.subTest(path=path), self.assertRaises(errors.NotAMarkdownFile):
                docset.SingleNote.from_file(path)


class FrontmatterTarget(unittest.TestCase):
    """S2.4: ``confluence:`` acts as --to, ``confluence_parent:`` as --under."""

    NONE = {"to": None, "under": None}

    def targets(self, fm):
        return note(md=f"---\n{fm}\n---\n# Body\n").targets()

    def test_each_key_alone(self):
        url = f"{BASE}/spaces/ENG/pages/42/Title"
        self.assertEqual(self.targets(f"confluence: {url}"), {"to": url, "under": None})
        self.assertEqual(self.targets("confluence_parent: '10'"), {"to": None, "under": "10"})
        self.assertEqual((docset.TO_KEY, docset.UNDER_KEY), ("confluence", "confluence_parent"))

    def test_both_keys_are_returned_to_the_caller_to_refuse(self):
        got = self.targets("confluence_parent: 10\ntitle: T\nconfluence: \"42\"")
        self.assertEqual(got, {"to": "42", "under": "10"})

    def test_absent_empty_list_or_no_frontmatter_means_no_target(self):
        for md in ("# no frontmatter\nconfluence: 42\n",
                   "---\nconfluence:\nconfluence_parent: ''\n---\n",
                   "---\nconfluence:\n  - 42\n---\n",
                   "---\nconfluence: 42\n"):                 # unclosed: not frontmatter
            with self.subTest(md=md):
                self.assertEqual(note(md=md).targets(), self.NONE)

    def test_crlf_and_key_case(self):
        md = "---\r\nconfluence: 42\r\nConfluence_Parent: 10\r\n---\r\nx"
        self.assertEqual(note(md=md).targets(), {"to": "42", "under": None})

    def test_value_parses_like_a_flag(self):
        url = f"{BASE}/spaces/ENG/pages/42/Title?focusedCommentId=1#c"
        ref = self.targets(f"confluence: {url}")["to"]
        self.assertEqual(pageref.PageRef.parse(ref, BASE).page_id, "42")
        ref = self.targets(f"confluence: {BASE}/spaces/ENG/folder/5")["to"]
        with self.assertRaises(errors.FolderPageRef):
            pageref.PageRef.parse(ref, BASE)

    def test_reading_the_target_never_touches_the_file(self):
        n = note(md="---\nconfluence: 42\n---\nx", name="no/such/dir/n.md")
        self.assertEqual(n.targets()["to"], "42")           # in memory: no file I/O


class Under(unittest.TestCase):
    def test_create_under_parent_marks_with_filename(self):
        f = tree()
        p = planner(f).build(note(), None, "10")          # --space omitted
        self.assertEqual((p.mode, p.space_id, p.space_info["key"]), ("under", "S1", "ENG"))
        self.assertEqual([(i.action, i.title) for i in p.items], [("CREATE", "Release notes")])
        self.assertEqual(p.handover_items(), [])           # creating loses nothing: no prompt
        (it, pid), = run(f, p).written
        self.assertEqual(f.pages[pid]["parentId"], "10")
        self.assertEqual(f.pages[pid]["props"]["cpub-managed"]["value"],
                         {"source": "release.md", "tool": plan.MARKER_TOOL, "since": TODAY})

    def test_rerun_updates_with_preview_and_obsidian_message(self):
        f = tree(); f.add("40", "Release notes", parent="10", marker=managed("release.md"))
        p = planner(f).build(note(), "S1", "10")
        it = p.items[0]
        self.assertEqual((it.action, it.managed, it.needs_handover), ("UPDATE", "same", False))
        self.assertEqual((it.version, it.old_size), (3, len(b"<p>old</p>")))
        self.assertEqual(it.new_size, len(it.storage.encode()))
        puts = []
        real = f.request

        def spy(m, u, d=None, h=None):
            if m == "PUT":
                puts.append(json.loads(d))
            return real(m, u, d, h)

        f.request = spy
        run(f, p)
        self.assertEqual(puts[0]["version"]["message"], "Published from Obsidian: release.md")

    def test_create_resave_carries_the_obsidian_message(self):
        f = tree()
        p = planner(f).build(note(), "S1", "10")
        p.items[0].attachments = [("mermaid_a.png", "/x/a.png")]
        puts = []
        real = f.request

        def spy(m, u, d=None, h=None):
            if m == "PUT" and "/pages/" in u and "properties" not in u:
                puts.append(json.loads(d)["version"]["message"])
            return real(m, u, d, h)

        f.request = spy
        run(f, p)
        self.assertEqual(puts, ["Published from Obsidian: release.md"])

    def test_same_title_outside_the_parent_is_refused_with_note_fix(self):
        f = tree(); f.add("30", "Release notes", parent="20")
        p = planner(f).build(note(), "S1", "10")
        (r,) = p.refusals()
        self.assertEqual(r.code, "outside-subtree")
        self.assertIn("--to", r.fix)
        self.assertIn("--title", r.fix)
        with self.assertRaises(errors.PlanRefused):
            run(f, p, adopt=True)
        self.assertEqual(f.writes, [])

    def test_prefix_applies_to_the_title(self):
        p = planner(tree(), prefix="R").build(note(), "S1", "10")
        self.assertEqual(p.items[0].title, "R - Release notes")


class To(unittest.TestCase):
    def setUp(self):
        self.f = tree()
        self.f.add("40", "Their title", parent="11")      # unmanaged, hand-written

    def test_replaces_exactly_that_page_keeping_title_and_parent(self):
        p = planner(self.f).build_replace(note(), "40")
        it = p.items[0]
        self.assertEqual((p.mode, it.action, it.page_id, it.title), ("to", "UPDATE", "40",
                                                                     "Their title"))
        self.assertEqual((it.managed, it.needs_handover), ("none", True))
        self.assertNotIn(("GET", "/api/v2/spaces/S1/pages"), self.f.calls)   # no title search
        with self.assertRaises(errors.HandoverRequired):
            run(self.f, p)
        self.assertEqual(self.f.writes, [])
        run(self.f, p, adopt=True)
        page = self.f.pages["40"]
        self.assertEqual((page["title"], page["parentId"], page["version"]),
                         ("Their title", "11", 4))
        self.assertEqual(page["props"]["cpub-managed"]["value"]["source"], "release.md")
        again = planner(self.f).build_replace(note(), "40")
        self.assertEqual((again.items[0].managed, again.handover_items()), ("same", []))

    def test_explicit_title_renames(self):
        p = planner(self.f, prefix="R").build_replace(note(title="New"), "40")
        self.assertEqual(p.items[0].title, "R - New")

    def test_space_homepage_is_refused(self):
        p = planner(self.f).build_replace(note(), "1")
        self.assertEqual([r.code for r in p.refusals()], ["homepage"])
        with self.assertRaises(errors.PlanRefused):
            run(self.f, p, adopt=True)
        self.assertEqual(self.f.writes, [])

    def test_space_mismatch_is_refused(self):
        p = planner(self.f).build_replace(note(), "40", space_id="S9")
        self.assertEqual([r.code for r in p.refusals()], ["target-space"])
        self.assertEqual(planner(self.f).build_replace(note(), "40", "S1").refusals(), [])

    def test_archived_target_is_refused(self):
        self.f.pages["40"]["status"] = "archived"
        p = planner(self.f).build_replace(note(), "40")
        self.assertEqual([r.code for r in p.refusals()], ["not-current"])

    def test_other_source_needs_hand_over(self):
        self.f.pages["40"]["props"]["cpub-managed"] = {
            "id": "prop40", "value": managed("other.md"), "version": 1}
        p = planner(self.f).build_replace(note(), "40")
        self.assertEqual(p.items[0].managed, "other")
        with self.assertRaises(errors.HandoverDeclined):
            run(self.f, p, confirm=lambda items: False)
        self.assertEqual(self.f.writes, [])


class SetModeUnchanged(unittest.TestCase):
    def test_derived_space_is_described_for_the_footer(self):
        f = tree()
        ds = docset.DocSet((docset.SourceDoc("01 - A", "x\n"),))
        p = planner(f).build(ds, None, "10")
        self.assertEqual((p.space_id, p.space_info["key"]), ("S1", "ENG"))
        self.assertIsNone(p.requested_space_id)

    def test_set_mode_reads_no_space_and_no_bodies(self):
        f = tree(); f.add("40", "01 - A", parent="10")
        ds = docset.DocSet((docset.SourceDoc("01 - A", "x\n"),))
        p = planner(f).build(ds, "S1", "10")
        self.assertEqual(p.mode, "set")
        self.assertNotIn(("GET", "/api/v2/spaces/S1"), f.calls)
        self.assertIsNone(p.items[0].old_size)
        self.assertEqual(p.items[0].version_message, "republish")


if __name__ == "__main__":
    unittest.main()
