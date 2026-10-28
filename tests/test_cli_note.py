"""Single-note publish from the command line (S1.2, S2.1-S2.4, S2.6), in-process.

Every run goes through :class:`~confluence_publisher.cli.PublishCommand` with
the real :class:`~confluence_publisher.plan.Planner` and
:class:`~confluence_publisher.executor.PlanExecutor` over the in-memory
Confluence (``FakeConfluence``), so the request log proves what was and was not
written. The note and the file system are faked through the constructor;
nothing is patched and no file is read or written. Run from the repo root::

    python3.12 -B -m unittest discover -s tests
"""
import io
import json
import urllib.parse
import types
import unittest

from _support import cli, cli_output, config, docset, markdown_html, plan, storage
from _support import mermaid as mmd
from _fake_confluence import FakeConfluence, BASE
from _fake_log import memory_log

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
        self.log = memory_log()                  # never the real audit log
        self.converter = None                    # a test may inject one (e.g. without npx)

    def run_cli(self, *argv, tty=False, answer=None, markdown=NOTE_MD, app_dirs=None,
                docs_md="a\n"):
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
            return plan.Planner(client, self.converter
                                or storage.StorageConverter(args.prefix, None, None))

        args = cli.build_parser().parse_args(list(argv))
        cmd = cli.PublishCommand.from_args(
            args, out, credential_loader=Loader(), client_factory=self.f.client,
            mode_resolver=cli.ModeResolver(
                is_file=lambda p: p.endswith(".md"), is_dir=lambda p: p == "docs",
                targets_of=lambda p: docset.SingleNote(p, markdown=markdown).targets()),
            note_loader=lambda path, title: docset.SingleNote(path, title=title,
                                                              markdown=markdown),
            docset_loader=lambda path: docset.DocSet((docset.SourceDoc("01 - A", docs_md),)),
            planner_factory=planner,
            app_dirs=app_dirs or types.SimpleNamespace(note_img_dir="/cache/cp/img"),
            prompt=cli.HandoverPrompt(out, ask=ask, stdin=FakeStdin(tty)), run_log=self.log)
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


class FrontmatterTarget(NoteRun):
    """S2.4: the note remembers its target; the tool never edits the note."""
    PASTE = "To publish this note again without flags, paste this line into its frontmatter"

    def test_confluence_key_replaces_that_page_with_no_flag(self):
        md = f"---\nconfluence: {URL_20}\n---\nbody\n"
        msg, out, _ = self.run_cli("My Note.md", "--adopt", markdown=md)
        self.assertIsNone(msg, msg)
        self.assertIn("UPDATE  0        Hand written", out)
        self.assertEqual(self.f.pages["20"]["parentId"], "10")
        self.assertNotIn(self.PASTE, out)                  # the note already has its key

    def test_confluence_parent_key_creates_a_child(self):
        msg, out, _ = self.run_cli("My Note.md", markdown="---\nconfluence_parent: 10\n---\nx\n")
        self.assertIsNone(msg, msg)
        self.assertIn("CREATE  0        My Note", out)
        self.assertNotIn(self.PASTE, out)

    def test_a_flag_wins_over_the_key(self):
        md = "---\nconfluence: 1\n---\nbody\n"             # 1 is the homepage: refused
        msg, out, _ = self.run_cli("My Note.md", "--to", "20", "--adopt", markdown=md)
        self.assertIsNone(msg, msg)
        self.assertIn("UPDATE  0        Hand written", out)

    def test_a_bad_value_gets_the_flag_error_before_any_request(self):
        md = f"---\nconfluence: {BASE}/x/AbCd\n---\nbody\n"
        msg, _, _ = self.run_cli("My Note.md", markdown=md)
        self.assert_friendly(msg)
        self.assertIn("short", msg.lower())
        self.assertEqual(self.f.calls, [])

    def test_both_keys_are_refused_before_any_request(self):
        md = "---\nconfluence: 20\nconfluence_parent: 10\n---\nbody\n"
        msg, _, _ = self.run_cli("My Note.md", markdown=md)
        self.assert_friendly(msg)
        self.assertIn("both confluence: and confluence_parent:", msg)
        self.assertEqual(self.f.calls, [])

    def test_publish_with_to_prints_the_line_to_paste(self):
        msg, out, _ = self.run_cli("My Note.md", "--to", URL_20, "--adopt")
        self.assertIsNone(msg, msg)
        self.assertIn(f"{self.PASTE}\n(the --- block at the top of the note; add one if it has "
                      f"none):\n  confluence: {BASE}/pages/viewpage.action?pageId=20\n", out)

    def test_publish_with_under_prints_the_parent_line(self):
        msg, out, _ = self.run_cli("My Note.md", "--under", "10")
        self.assertIsNone(msg, msg)
        self.assertIn(f"  confluence_parent: {BASE}/pages/viewpage.action?pageId=10\n", out)

    def test_no_line_on_a_dry_run(self):
        _, out, _ = self.run_cli("My Note.md", "--to", "20", "--dry-run")
        self.assertNotIn(self.PASTE, out)


MERMAID_MD = "# Hi\n\n```mermaid\ngraph TD\n  A --> B\n```\n"


class NoRunner:
    def __call__(self, argv, **kw):
        raise AssertionError("npx must not be run when it was not found")


class DiagramsWithoutNpx(NoteRun):
    """PM #108: under Obsidian's bare PATH a note never trades its diagrams for code."""

    def setUp(self):
        super().setUp()
        mermaid = mmd.MermaidRenderer("/nonexistent/cpub-img", npx=None,
                                      runner=NoRunner())
        self.converter = storage.StorageConverter(
            renderer=markdown_html.MarkdownRenderer(mermaid))

    def test_a_note_with_mermaid_is_refused_before_any_write(self):
        for flag, page in (("--to", "20"), ("--under", "10")):
            with self.subTest(flag=flag):
                msg, out, _ = self.run_cli("My Note.md", flag, page, "--adopt",
                                           markdown=MERMAID_MD)
                self.assert_friendly(msg)
                self.assertEqual(msg.splitlines()[:2], [
                    "Error: Diagrams in My Note.md cannot be rendered (npx not found).",
                    "Fix: Install Node.js, or add its folder to PATH in the Shell commands "
                    "plugin."])
                self.assertEqual(self.f.writes, [])
                self.assertNotIn("Replace preview", out)

    def test_a_note_without_mermaid_still_publishes(self):
        msg, _, _ = self.run_cli("My Note.md", "--to", "20", "--adopt")
        self.assertIsNone(msg, msg)

    def test_a_folder_publishes_with_a_one_line_warning(self):
        msg, out, _ = self.run_cli("docs", "--parent", "10", "--dry-run", docs_md=MERMAID_MD)
        self.assertIsNone(msg, msg)
        self.assertIn("Warning: npx not found, so the Mermaid diagrams in 1 page are "
                      "published as code blocks: 01 - A\n", out)
        self.assertLess(out.index("Warning: npx"), out.index("ACTION"))

    def test_no_warning_when_nothing_is_skipped(self):
        _, out, _ = self.run_cli("docs", "--parent", "10", "--dry-run")
        self.assertNotIn("Warning: npx", out)


class AuditLog(NoteRun):
    """S0.5 (AC-0.5.1): a real publish appends one line per page; a dry run none."""

    def test_publish_logs_the_write_without_body_token_or_email(self):
        msg, _, _ = self.run_cli("My Note.md", "--to", "20", "--adopt")
        self.assertIsNone(msg, msg)
        (line,) = [json.loads(x) for x in self.log.files.text.splitlines()]
        self.assertEqual((line["kind"], line["page_id"], line["action"], line["title"],
                          line["version_before"], line["version_after"], line["host"]),
                         ("publish", "20", "UPDATE", "Hand written", 7, 8,
                          urllib.parse.urlsplit(BASE).hostname))
        for secret in (TOKEN, CREDS.email, "body text"):
            self.assertNotIn(secret, self.log.files.text)

    def test_dry_run_and_refusals_log_nothing(self):
        self.run_cli("My Note.md", "--to", "20", "--dry-run")
        self.run_cli("My Note.md", "--to", "1", "--adopt")            # homepage: refused
        self.assertEqual(self.log.files.text, "")


class LinksToMissingPages(NoteRun):
    """S2.5 end to end: a link to a missing page becomes text and is listed once."""
    MD = "---\ntitle: Linker\n---\nSee [[Hand written]] and [[Missing note|the plan]].\n"

    def test_dry_run_lists_the_missing_target_once(self):
        msg, out, _ = self.run_cli("My Note.md", "--under", "10", "--dry-run", markdown=self.MD)
        self.assertIsNone(msg, msg)
        self.assertEqual(out.count("1 link left as text: Missing note\n"), 1)
        self.assertEqual(self.f.writes, [])

    def test_real_run_writes_text_for_the_missing_link_and_a_link_for_the_other(self):
        msg, out, _ = self.run_cli("My Note.md", "--under", "10", markdown=self.MD)
        self.assertIsNone(msg, msg)
        self.assertEqual(out.count("1 link left as text: Missing note\n"), 1)
        (page,) = [p for p in self.f.pages.values() if p["title"] == "Linker"]
        body = page["body"]
        self.assertIn('ri:content-title="Hand written"', body)
        self.assertIn("<em>the plan</em>", body)
        self.assertNotIn('ri:content-title="Missing note"', body)

    def test_with_a_prefix_the_line_names_the_title_that_was_looked_up(self):
        md = "See [[Missing note#Part|the plan]].\n"
        msg, out, _ = self.run_cli("My Note.md", "--under", "10", "--prefix", "R", "--dry-run",
                                   markdown=md)
        self.assertIsNone(msg, msg)
        self.assertEqual(out.count("1 link left as text: R - Missing note\n"), 1)

    def test_no_line_when_every_link_resolves(self):
        md = "See [[Hand written]].\n"
        _, out, _ = self.run_cli("My Note.md", "--to", "20", "--dry-run", markdown=md)
        self.assertNotIn("left as text", out)


class UnresolvedLinksLine(unittest.TestCase):
    """S2.5 (AC-2.5.2): one line naming every link left as text; nothing when none."""

    def line(self, targets):
        out = io.StringIO()
        cli_output.PlanPrinter(out).unresolved_links(targets)
        return out.getvalue()

    def test_wording(self):
        self.assertEqual(self.line(["Other note"]), "1 link left as text: Other note\n")
        self.assertEqual(self.line(["A", "B", "C"]), "3 links left as text: A, B, C\n")
        self.assertEqual(self.line([]), "")


class DefaultWiring(unittest.TestCase):
    def test_the_real_planner_gets_the_npx_that_app_dirs_found(self):
        class Dirs:
            note_img_dir = "/cache/cp/img"
            log_path = "/state/confluence-publisher/log.jsonl"   # never written here
            def __init__(self):
                self.asked = []
            def find_executable(self, name):
                self.asked.append(name)
                return ("/opt/homebrew/bin/npx", {"PATH": "/opt/homebrew/bin"})
        dirs = Dirs()
        args = cli.build_parser("cpub").parse_args(["docs", "--parent", "1"])
        cmd = cli.PublishCommand.from_args(args, io.StringIO(), app_dirs=dirs)
        planner = cmd.planner_factory(object(), args)
        self.assertEqual(dirs.asked, ["npx"])
        self.assertEqual(planner.converter.renderer.mermaid.npx,
                         ("/opt/homebrew/bin/npx", {"PATH": "/opt/homebrew/bin"}))


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
