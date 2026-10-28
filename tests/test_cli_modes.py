"""Note vs folder mode and the flags that go with each (S2.1, S2.2, S2.4, AC-2.1.1, AC-2.1.7).

:class:`~confluence_publisher.cli.ModeResolver` gets fake ``is_file`` /
``is_dir`` functions, so nothing touches the file system. Every failure must
be one sentence plus a ``Fix:`` line. Run from the repo root::

    python3.12 -B -m unittest discover -s tests
"""
import unittest

from _support import cli as cp
from _support import errors, docset

FILES = {"note.md", "Notes/Deep Note.MD", "picture.png", "to.md", "under.md", "both.md"}
DIRS = {"docs"}
#: The frontmatter of each fake note, read by the real reader (S2.4).
FRONTMATTER = {
    "to.md": "---\nconfluence: https://x/wiki/spaces/K/pages/77/Slug\n---\nbody\n",
    "under.md": "---\ntitle: T\nconfluence_parent: '88'\n---\nbody\n",
    "both.md": "---\nconfluence: 77\nconfluence_parent: 88\n---\n",
}


def targets_of(path):
    return docset.SingleNote(path, markdown=FRONTMATTER.get(path, "# no frontmatter\n")
                             ).targets()


SHIM = "python3.12 confluence_publish.py"


def resolve(*argv):
    """Parse ``argv`` and resolve the mode against the fake file system; the
    ``Fix:`` lines name the clone's shim command."""
    args = cp.build_parser(SHIM).parse_args(list(argv))
    resolver = cp.ModeResolver(is_file=FILES.__contains__, is_dir=DIRS.__contains__,
                               targets_of=targets_of)
    return resolver.resolve(args, cp.ErrorTranslator(args, command=SHIM))


class NoteMode(unittest.TestCase):
    def test_to_replaces_that_page(self):
        self.assertEqual(resolve("note.md", "--to", "123"),
                         cp.RunMode("note", "note.md", "123", replace=True))

    def test_under_creates_a_child(self):
        self.assertEqual(resolve("note.md", "--under", "https://x/wiki/spaces/K/pages/9"),
                         cp.RunMode("note", "note.md", "https://x/wiki/spaces/K/pages/9"))

    def test_parent_works_as_under_for_a_note(self):
        self.assertEqual(resolve("note.md", "--parent", "9").page, "9")

    def test_md_extension_is_case_insensitive(self):
        self.assertEqual(resolve("Notes/Deep Note.MD", "--to", "1").kind, "note")

    def test_title_is_accepted(self):
        args = cp.build_parser().parse_args(["note.md", "--under", "9", "--title", "My page"])
        self.assertEqual(args.title, "My page")

    def test_space_is_optional(self):
        args = cp.build_parser().parse_args(["note.md", "--to", "1"])
        self.assertIsNone(args.space)


class FrontmatterTarget(unittest.TestCase):
    """AC-2.4.1: ``confluence:`` acts as --to, ``confluence_parent:`` as --under; flags win."""

    def test_confluence_key_replaces_that_page(self):
        self.assertEqual(resolve("to.md"),
                         cp.RunMode("note", "to.md", "https://x/wiki/spaces/K/pages/77/Slug",
                                    replace=True, from_frontmatter=True))

    def test_confluence_parent_key_creates_a_child(self):
        self.assertEqual(resolve("under.md"),
                         cp.RunMode("note", "under.md", "88", from_frontmatter=True))

    def test_flags_win_over_the_frontmatter(self):
        for flag in ("--to", "--under", "--parent"):
            with self.subTest(flag=flag):
                mode = resolve("to.md", flag, "5")
                self.assertEqual((mode.page, mode.replace, mode.from_frontmatter),
                                 ("5", flag == "--to", False))

    def test_flags_win_even_over_both_keys(self):
        self.assertEqual(resolve("both.md", "--to", "5").page, "5")

    def test_frontmatter_is_not_read_when_a_flag_is_given(self):
        def boom(path):
            raise AssertionError("frontmatter read")
        for argv in (("note.md", "--to", "1"), ("docs", "--parent", "1")):
            with self.subTest(argv=argv):
                args = cp.build_parser(SHIM).parse_args(list(argv))
                cp.ModeResolver(is_file=FILES.__contains__, is_dir=DIRS.__contains__,
                                targets_of=boom).resolve(args)


class FolderMode(unittest.TestCase):
    def test_parent(self):
        self.assertEqual(resolve("docs", "--parent", "9"), cp.RunMode("set", "docs", "9"))

    def test_under_is_an_alias_of_parent(self):
        self.assertEqual(resolve("docs", "--under", "9"), cp.RunMode("set", "docs", "9"))

    def test_same_page_in_both_is_fine(self):
        self.assertEqual(resolve("docs", "--parent", "9", "--under", "9").page, "9")

    def test_v11_command_line_is_folder_mode(self):
        mode = resolve("docs", "--space", "ENG", "--parent", "9", "--prefix", "p", "--dry-run")
        self.assertEqual((mode.kind, mode.page), ("set", "9"))


class Refused(unittest.TestCase):
    def fails(self, *argv):
        with self.assertRaises(errors.CliError) as ctx:
            resolve(*argv)
        msg = str(ctx.exception)
        lines = msg.splitlines()
        self.assertTrue(lines[0].startswith("Error: "), msg)
        self.assertTrue(lines[1].startswith("Fix: "), msg)
        return msg

    def test_to_and_under_together(self):
        msg = self.fails("note.md", "--to", "1", "--under", "2")
        self.assertIn("--to and --under can't be used together", msg)

    def test_note_without_target_shows_both_forms(self):
        msg = self.fails("note.md")
        self.assertIn("python3.12 confluence_publish.py note.md --to <page URL or id>", msg)
        self.assertIn("python3.12 confluence_publish.py note.md --under <page URL or id>", msg)

    def test_note_without_target_mentions_the_frontmatter_keys(self):
        msg = self.fails("note.md")
        self.assertIn("python3.12 confluence_publish.py note.md with no flag", msg)
        self.assertIn("  confluence:        <page URL or id>   (replace that page)", msg)
        self.assertIn("  confluence_parent: <page URL or id>   (create a child page)", msg)

    def test_fix_lines_quote_a_path_with_spaces(self):
        """QA: vault notes often have spaces; the Fix line must survive a copy-paste."""
        args = cp.build_parser("cpub").parse_args(["x"])
        err = cp.ErrorTranslator(args, command="cpub")
        note = str(err.note_without_target("/v/notes/FM Tricky.md"))
        self.assertIn("Fix: cpub '/v/notes/FM Tricky.md' --to <page URL or id>   or   "
                      "cpub '/v/notes/FM Tricky.md' --under", note)
        self.assertIn("run cpub '/v/notes/FM Tricky.md' with no flag", note)
        self.assertIn("Fix: cpub 'My Docs' --parent <page URL or id>",
                      str(err.folder_without_parent("My Docs")))
        self.assertIn("Fix: cpub note.md --to", str(err.note_without_target("note.md")))

    def test_both_frontmatter_keys(self):
        msg = self.fails("both.md")
        self.assertIn("has both confluence: and confluence_parent:", msg)
        self.assertIn("Keep only one of them", msg)

    def test_folder_without_parent(self):
        msg = self.fails("docs")
        self.assertIn("python3.12 confluence_publish.py docs --parent <page URL or id>", msg)

    def test_to_with_a_folder(self):
        self.assertIn("takes a single note, not a folder", self.fails("docs", "--to", "1"))

    def test_title_with_a_folder(self):
        self.assertIn("--title names one page", self.fails("docs", "--parent", "1", "--title", "x"))

    def test_parent_and_under_disagree(self):
        self.assertIn("name different pages", self.fails("docs", "--parent", "1", "--under", "2"))

    def test_missing_path(self):
        self.assertIn("No such file or folder: gone.md", self.fails("gone.md", "--to", "1"))

    def test_not_markdown(self):
        self.assertIn("Not a Markdown note: picture.png", self.fails("picture.png", "--to", "1"))

    def test_flag_errors_come_before_any_credential_or_network_step(self):
        class Boom:
            def load(self, env_file=None):
                raise AssertionError("credentials must not be read")
        args = cp.build_parser().parse_args(["no/such/note.md", "--to", "1"])
        cmd = cp.PublishCommand.from_args(args, credential_loader=Boom())
        with self.assertRaises(SystemExit) as ctx:
            cmd.run()
        self.assertIn("No such file or folder", str(ctx.exception.code))


if __name__ == "__main__":
    unittest.main()
