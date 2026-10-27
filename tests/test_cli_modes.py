"""Note vs folder mode and the flags that go with each (S2.1, S2.2, AC-2.1.1, AC-2.1.7).

:class:`~confluence_publisher.cli.ModeResolver` gets fake ``is_file`` /
``is_dir`` functions, so nothing touches the file system. Every failure must
be one sentence plus a ``Fix:`` line. Run from the repo root::

    python3.12 -B -m unittest discover -s tests
"""
import unittest

from _support import cli as cp
from _support import errors

FILES = {"note.md", "Notes/Deep Note.MD", "picture.png"}
DIRS = {"docs"}


def resolve(*argv):
    """Parse ``argv`` and resolve the mode against the fake file system."""
    args = cp.build_parser().parse_args(list(argv))
    resolver = cp.ModeResolver(is_file=FILES.__contains__, is_dir=DIRS.__contains__)
    return resolver.resolve(args)


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
