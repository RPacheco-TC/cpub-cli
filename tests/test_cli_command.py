"""The command name follows how the tool was started (AC-1.3.5, AC-1.3.6, D26), in-process.

:class:`~confluence_publisher.cli.CommandName` is a pure function of
``sys.argv[0]``; the name it returns is injected into the parser (usage,
examples, ``--version``) and into every ``Fix:`` line. Nothing is installed or
run as a subprocess. Run from the repo root::

    python3.12 -B -m unittest discover -s tests
"""
import contextlib
import io
import unittest

from _support import cli as cp, pkg


class Resolve(unittest.TestCase):
    CASES = [
        ("cpub", "cpub"),
        ("/home/u/.local/bin/cpub", "cpub"),
        ("/home/u/.local/bin/confluence-publish", "confluence-publish"),
        (r"C:\Users\u\.local\bin\cpub.exe", "cpub"),
        ("CPUB.EXE", "cpub"),
        ("confluence_publish.py", "python3.12 confluence_publish.py"),
        ("/repo/confluence_publish.py", "python3.12 confluence_publish.py"),
        ("/repo/confluence_publisher/__main__.py", "python3.12 -m confluence_publisher"),
        ("/usr/lib/python3.12/unittest/__main__.py", "cpub"),   # someone else's -m
        ("", "cpub"),
        (None, "cpub"),
    ]

    def test_names(self):
        for argv0, expected in self.CASES:
            with self.subTest(argv0=argv0):
                self.assertEqual(cp.CommandName.resolve(argv0), expected)


class Injected(unittest.TestCase):
    def test_usage_and_examples_use_the_command(self):
        for command in ("cpub", "confluence-publish", "python3.12 -m confluence_publisher"):
            with self.subTest(command=command):
                text = cp.build_parser(command).format_help()
                self.assertTrue(text.startswith(f"usage: {command} "), text[:80])
                self.assertIn(f"examples:\n  {command} note.md --to ", text)
                self.assertIn(f"  {command} docs/ --parent ", text)

    def test_version(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as ctx:
            cp.build_parser("cpub").parse_args(["--version"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertEqual(out.getvalue(), f"cpub {pkg.__version__}\n")
        self.assertEqual(pkg.__version__, "1.4.0")

    def test_fix_lines_use_the_command(self):
        args = cp.build_parser("cpub").parse_args(["note.md"])
        cmd = cp.PublishCommand.from_args(
            args, io.StringIO(), command="cpub",
            mode_resolver=cp.ModeResolver(is_file=lambda p: True, is_dir=lambda p: False,
                                          targets_of=lambda p: {}))
        with self.assertRaises(SystemExit) as ctx:
            cmd.run()
        msg = str(ctx.exception.code)
        self.assertIn("Fix: cpub note.md --to <page URL or id>   or   cpub note.md --under", msg)
        self.assertNotIn("python3.12", msg)

    def test_no_hard_coded_command_outside_the_resolver(self):
        """AC-1.3.6: the command strings live only in CommandName."""
        import pathlib
        root = pathlib.Path(pkg.__file__).parent
        for path in sorted(root.glob("*.py")):
            text = path.read_text()
            for literal in ("python3.12 confluence_publish.py",
                            "python3.12 -m confluence_publisher"):
                count = text.count(f'"{literal}"')
                expected = 1 if path.name == "cli.py" else 0
                with self.subTest(file=path.name, literal=literal):
                    self.assertEqual(count, expected)


class NothingPointsIntoTheClone(unittest.TestCase):
    """An installed cpub has no repo files: help and messages must not send users there."""

    CLONE_ONLY = (".env.example", "docs/GUIDE", "docs/KNOWN", "README", "GUIDE.md",
                  "template.yaml")

    def test_help_text(self):
        text = cp.build_parser("cpub").format_help()
        self.assertIn("    CONFLUENCE_API_TOKEN=<your API token>", text)
        for name in self.CLONE_ONLY:
            with self.subTest(name=name):
                self.assertNotIn(name, text)

    def test_package_strings(self):
        """No string literal in the package names a clone-only file (URLs are fine)."""
        import ast
        import pathlib
        root = pathlib.Path(pkg.__file__).parent
        for path in sorted(root.glob("*.py")):
            tree = ast.parse(path.read_text())
            docstrings = {id(n.body[0].value) for n in ast.walk(tree)
                          if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef))
                          and n.body and isinstance(n.body[0], ast.Expr)}
            for node in ast.walk(tree):
                if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                        and id(node) not in docstrings and "://" not in node.value):
                    for name in (".env.example", "KNOWN_ISSUES", "GUIDE.md", "README"):
                        with self.subTest(file=path.name, name=name):
                            self.assertNotIn(name, node.value)


if __name__ == "__main__":
    unittest.main()
