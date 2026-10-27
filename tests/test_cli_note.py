"""Single-note publish from the command line (S1.2, S2.1, S2.2, S2.3, S2.6), in-process.

Every run goes through :class:`~confluence_publisher.cli.PublishCommand` with
the real :class:`~confluence_publisher.plan.Planner` and
:class:`~confluence_publisher.executor.PlanExecutor` over the in-memory
Confluence (``FakeConfluence``), so the request log proves what was and was not
written. The note and the file system are faked through the constructor;
nothing is patched and no file is read or written. Run from the repo root::

    python3.12 -B -m unittest discover -s tests
"""
import io
import types
import unittest

from _support import cli, config, docset, plan, storage
from _fake_confluence import FakeConfluence, BASE

TOKEN = "s3cr3t-token-value"
CREDS = config.Credentials(BASE, "x@example.invalid", TOKEN)
NOTE_MD = "---\ntitle: Fancy Title\n---\n# Hi\n\nbody text\n"
URL_20 = f"{BASE}/spaces/S1/pages/20/Hand+written"


class FakeStdin:
    def __init__(self, tty):
        self.tty = tty

    def isatty(self):
        return self.tty


class NoteRun(unittest.TestCase):
    def setUp(self):
        self.f = FakeConfluence()
        self.f.add("1", "Home")                          # the space homepage
        self.f.add("10", "TEST", parent="1")
        self.f.add("20", "Hand written", parent="10", version=7)   # unmanaged
        self.planner_args = []

    def run_cli(self, *argv, tty=False, answer=None, markdown=NOTE_MD, app_dirs=None):
        """Run one command; return (exit message or None, output, prompts)."""
        out, prompts = io.StringIO(), []

        class Loader:
            def load(self, env_file=None):
                return CREDS

        def ask(prompt):
            prompts.append(prompt)
            return answer

        def planner(client, args):
            self.planner_args.append(args)
            return plan.Planner(client, storage.StorageConverter(args.prefix, None, None))

        args = cli.build_parser().parse_args(list(argv))
        cmd = cli.PublishCommand.from_args(
            args, out, credential_loader=Loader(), client_factory=self.f.client,
            mode_resolver=cli.ModeResolver(is_file=lambda p: p.endswith(".md"),
                                           is_dir=lambda p: p == "docs"),
            note_loader=lambda path, title: docset.SingleNote(path, title=title,
                                                              markdown=markdown),
            docset_loader=lambda path: docset.DocSet((docset.SourceDoc("01 - A", "a\n"),)),
            planner_factory=planner,
            app_dirs=app_dirs or types.SimpleNamespace(note_img_dir="/cache/cp/img"),
            prompt=cli.HandoverPrompt(out, ask=ask, stdin=FakeStdin(tty)))
        try:
            cmd.run()
            return None, out.getvalue(), prompts
        except SystemExit as e:
            return e.code, out.getvalue(), prompts

    def assert_friendly(self, msg):
        lines = msg.splitlines()
        self.assertTrue(lines[0].startswith("Error: "), msg)
        self.assertTrue(lines[1].startswith("Fix: "), msg)
        self.assertNotIn(TOKEN, msg)


class ReplaceWithTo(NoteRun):
    def test_dry_run_shows_the_replace_preview_and_writes_nothing(self):
        msg, out, prompts = self.run_cli("My Note.md", "--to", URL_20, "--dry-run", tty=True)
        self.assertIsNone(msg, msg)
        self.assertEqual((self.f.writes, prompts), ([], []))
        self.assertIn("UPDATE  0        Hand written", out)        # title kept
        self.assertIn("Replace preview:\n  Page:     Hand written  (space ", out)
        self.assertIn("  Current:  version 7, last edited 2026-10-01 10:00 UTC", out)
        self.assertRegex(out, r"  Content:  \d+ bytes -> \d+ bytes")
        self.assertIn("1 page needs a one-time hand-over", out)
        self.assertIn("1 page DRY-RUN (nothing written)", out)
        self.assertIn("Page: ", out)

    def test_unmanaged_page_needs_the_typed_count_then_republish_is_silent(self):
        msg, out, prompts = self.run_cli("My Note.md", "--to", "20", tty=True, answer="1")
        self.assertIsNone(msg, msg)
        self.assertEqual(prompts, ["Take over and replace 1 page(s) you did not publish? "
                                   "Type the number 1 to confirm: "])
        self.assertIn("This page was not published by this tool from this source:", out)
        self.assertIn("1 page published", out)
        self.assertIn(f"History: {BASE}/pages/viewpreviousversions.action?pageId=20", out)
        self.assertEqual(self.f.pages["20"]["parentId"], "10")    # never moved
        self.assertEqual(self.f.pages["20"]["title"], "Hand written")
        msg, out, prompts = self.run_cli("My Note.md", "--to", "20", tty=True)
        self.assertIsNone(msg, msg)
        self.assertEqual(prompts, [])                             # now managed: no prompt

    def test_declined_or_non_interactive_writes_nothing(self):
        for tty, answer, needle in ((True, "y", "Hand-over not confirmed"),
                                    (False, None, "not an interactive terminal")):
            with self.subTest(tty=tty):
                msg, _, _ = self.run_cli("My Note.md", "--to", "20", tty=tty, answer=answer)
                self.assert_friendly(msg)
                self.assertIn(needle, msg)
                self.assertEqual(self.f.writes, [])

    def test_wording_fits_note_mode(self):
        # F1: a declined --to suggests --under/--title, not --prefix
        msg, _, _ = self.run_cli("My Note.md", "--to", "20", tty=True, answer="2")
        self.assertIn('use --under PAGE --title "New title" to publish a new page', msg)
        self.assertNotIn("--prefix", msg)
        # F3: one page is "it", not "them"
        msg, _, _ = self.run_cli("My Note.md", "--to", "20")
        self.assertIn("add --adopt to take it over", msg)
        _, out, _ = self.run_cli("My Note.md", "--to", "20", "--dry-run")
        self.assertIn("to hand it over", out)

    def test_link_after_a_rename_uses_the_id(self):
        # F4: the planned webui slug carries the old title
        _, out, _ = self.run_cli("My Note.md", "--to", "20", "--title", "Renamed", "--adopt")
        self.assertIn(f"Page: {BASE}/pages/viewpage.action?pageId=20", out)

    def test_adopt_replaces_without_a_prompt(self):
        msg, out, prompts = self.run_cli("My Note.md", "--to", "20", "--adopt", tty=True)
        self.assertIsNone(msg, msg)
        self.assertEqual(prompts, [])
        self.assertIn("Taken over (1):\n  - Hand written", out)

    def test_title_flag_renames(self):
        msg, out, _ = self.run_cli("My Note.md", "--to", "20", "--title", "Renamed", "--adopt")
        self.assertIsNone(msg, msg)
        self.assertEqual(self.f.pages["20"]["title"], "Renamed")

    def test_space_homepage_is_refused(self):
        msg, _, _ = self.run_cli("My Note.md", "--to", "1", "--adopt")
        self.assert_friendly(msg)
        self.assertIn("homepage", msg)
        self.assertEqual(self.f.writes, [])


class CreateWithUnder(NoteRun):
    def test_creates_a_child_titled_from_frontmatter_without_prompting(self):
        msg, out, prompts = self.run_cli("My Note.md", "--under", "10", tty=True)
        self.assertIsNone(msg, msg)
        self.assertEqual(prompts, [])
        self.assertIn("CREATE  0        Fancy Title", out)       # no prefix in note mode
        self.assertIn("New page: Fancy Title  (space ", out)
        created = [p for p in self.f.pages.values() if p["title"] == "Fancy Title"]
        self.assertEqual([p["parentId"] for p in created], ["10"])
        msg, out, prompts = self.run_cli("My Note.md", "--under", "10", tty=True)
        self.assertIsNone(msg, msg)
        self.assertIn("UPDATE  0        Fancy Title", out)       # re-run updates it
        self.assertEqual(prompts, [])

    def test_title_flag_beats_frontmatter(self):
        msg, out, _ = self.run_cli("My Note.md", "--under", "10", "--title", "Chosen")
        self.assertIsNone(msg, msg)
        self.assertIn("CREATE  0        Chosen", out)

    def test_same_title_outside_the_parent_is_refused(self):
        self.f.add("30", "Elsewhere", parent="1")
        self.f.add("31", "Fancy Title", parent="30")
        msg, out, _ = self.run_cli("My Note.md", "--under", "10")
        self.assert_friendly(msg)
        self.assertIn("already exists outside", msg)
        self.assertEqual(self.f.writes, [])
        self.assertNotIn("Replace preview", out)                  # F2: no preview if refused


class PageReferences(NoteRun):
    def test_bad_urls_are_refused_before_any_request(self):
        for ref, needle in ((f"{BASE}/spaces/S1/folder/55", "folder"),
                            ("https://other.example/wiki/spaces/S1/pages/20", "other.example"),
                            (f"{BASE}/x/AbCd", "short"),
                            ("not a page", "")):
            with self.subTest(ref=ref):
                self.f.calls.clear()
                msg, _, _ = self.run_cli("My Note.md", "--to", ref)
                self.assert_friendly(msg)
                self.assertIn(needle, msg.lower())
                self.assertEqual(self.f.calls, [])

    def test_folder_parent_accepts_a_url(self):
        msg, out, _ = self.run_cli("docs", "--parent", f"{BASE}/spaces/S1/pages/10/TEST",
                                   "--dry-run")
        self.assertIsNone(msg, msg)
        self.assertEqual(self.planner_args[0].parent, "10")
        self.assertIn("CREATE  0        01 - A", out)
        self.assertIn("1 pages DRY-RUN (nothing written) — space ENG, parent 10", out)


class ImageFolder(NoteRun):
    def test_note_mode_renders_into_the_user_cache(self):
        self.run_cli("My Note.md", "--to", "20", "--dry-run")
        self.assertEqual(self.planner_args[0].img_dir, "/cache/cp/img")

    def test_img_dir_still_overrides(self):
        self.run_cli("My Note.md", "--to", "20", "--dry-run", "--img-dir", "/mine")
        self.assertEqual(self.planner_args[0].img_dir, "/mine")

    def test_folder_mode_default_is_unchanged(self):
        self.run_cli("docs", "--parent", "10", "--dry-run")
        self.assertIsNone(self.planner_args[0].img_dir)            # -> <folder>/.img later


if __name__ == "__main__":
    unittest.main()
