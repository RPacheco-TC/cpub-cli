"""CLI tests for confluence_publish.py, run in-process.

No child processes, no temporary folders, no network: arguments go through
``build_parser()`` / ``main(argv)`` directly. Run from the repo root::

    python3.12 -B -m unittest discover -s tests
"""
import contextlib
import io
import os
import unittest

from _support import cli as cp, config, pkg
import _support

ARGS = ["docs", "--space", "X", "--parent", "1", "--dry-run"]
NO_CREDS = {k: v for k, v in os.environ.items() if not k.startswith("CONFLUENCE_")}
DUMMY_CREDS = {**NO_CREDS, "CONFLUENCE_BASE_URL": "https://example.invalid/wiki",
               "CONFLUENCE_EMAIL": "x@example.invalid", "CONFLUENCE_API_TOKEN": "t"}


def parse(*extra):
    """Parse ``ARGS + extra``; return ``(args, None)`` or ``(None, (exit_code, stderr))``."""
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        try:
            return cp.build_parser().parse_args([*ARGS, *extra]), None
        except SystemExit as e:
            return None, (e.code, err.getvalue())


class ImgWidthArgument(unittest.TestCase):
    """AC9 (cycle 1): invalid --img-width fails in argparse, before any env/network check."""

    def test_invalid_values_rejected_by_argparse(self):
        for bad in ("0", "-5", "abc", "1.5", ""):
            with self.subTest(value=bad):
                args, failure = parse(f"--img-width={bad}")
                self.assertIsNone(args)
                code, stderr = failure
                self.assertEqual(code, 2, stderr)
                self.assertIn("--img-width", stderr)
                self.assertIn("positive integer", stderr)

    def test_valid_value_accepted(self):
        args, failure = parse("--img-width", "700")
        self.assertIsNone(failure)
        self.assertEqual(args.img_width, 700)

    def test_default_is_unset(self):
        args, _ = parse()
        self.assertIsNone(args.img_width)


def run_main(argv, environ):
    """Run the command with the real credential loader on ``environ`` and no files,
    so a developer's real ./.env is never read (nothing is patched)."""
    loader = config.CredentialLoader(environ=environ, cwd="/nonexistent",
                                     config_dir="/nonexistent", read_file=lambda p: None)
    args = cp.build_parser().parse_args(argv)
    cp.PublishCommand.from_args(args, io.StringIO(), credential_loader=loader).run()


class MainPreflight(unittest.TestCase):
    """The command keeps its early exits; both happen before any network call."""

    def test_missing_credentials(self):
        with self.assertRaises(SystemExit) as ctx:
            run_main([*ARGS, "--img-width", "700"], NO_CREDS)
        self.assertIn("Missing Confluence credential(s)", str(ctx.exception.code))

    def test_not_a_folder(self):
        with self.assertRaises(SystemExit) as ctx:
            run_main(["no/such/dir/for/tests", "--space", "X", "--parent", "1", "--dry-run"],
                     DUMMY_CREDS)
        self.assertIn("Not a folder", str(ctx.exception.code))


class PythonVersionGuard(unittest.TestCase):
    """The 3.12+ guard, exercised with fake versions instead of another interpreter."""

    def test_rejects_older(self):
        for v in ((3, 9, 6), (3, 11, 9)):
            with self.subTest(version=v):
                with self.assertRaises(SystemExit) as ctx:
                    pkg._check_python(v)
                msg = str(ctx.exception.code)
                self.assertIn("Python 3.12+", msg)
                self.assertIn("found %d.%d.%d" % v, msg)
                self.assertNotIn("\n", msg)

    def test_accepts_supported(self):
        for v in ((3, 12, 0), (3, 13, 0)):
            with self.subTest(version=v):
                self.assertIsNone(pkg._check_python(v))


class SingleLoad(unittest.TestCase):
    def test_module_executed_once(self):
        self.assertEqual(_support.LOADS, 1)


if __name__ == "__main__":
    unittest.main()
