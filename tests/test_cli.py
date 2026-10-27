"""CLI tests for confluence_publish.py, run in-process.

No child processes, no temporary folders, no network: arguments go through
``build_parser()`` / ``main(argv)`` directly. Run from the repo root::

    python3.12 -B -m unittest discover -s tests
"""
import contextlib
import io
import os
import unittest

from _support import cp
import _support

ARGS = ["docs", "--space", "X", "--parent", "1", "--dry-run"]
NO_CREDS = {k: v for k, v in os.environ.items() if not k.startswith("CONFLUENCE_")}
DUMMY_CREDS = {**NO_CREDS, "CONFLUENCE_BASE_URL": "https://example.invalid/wiki",
               "CONFLUENCE_EMAIL": "x@example.invalid", "CONFLUENCE_API_TOKEN": "t"}


@contextlib.contextmanager
def environ(values):
    """Temporarily replace ``os.environ`` with ``values`` (lighter than unittest.mock,
    which drags in asyncio and doubles the suite's import time)."""
    saved = dict(os.environ)
    os.environ.clear(); os.environ.update(values)
    try:
        yield
    finally:
        os.environ.clear(); os.environ.update(saved)


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


class MainPreflight(unittest.TestCase):
    """main() keeps its early exits; both happen before any network call."""

    def test_missing_credentials(self):
        with environ(NO_CREDS):
            with self.assertRaises(SystemExit) as ctx:
                cp.main([*ARGS, "--img-width", "700"])
        self.assertIn("Missing environment variable", str(ctx.exception.code))

    def test_not_a_folder(self):
        with environ(DUMMY_CREDS):
            with self.assertRaises(SystemExit) as ctx:
                cp.main(["no/such/dir/for/tests", "--space", "X", "--parent", "1", "--dry-run"])
        self.assertIn("Not a folder", str(ctx.exception.code))


class PythonVersionGuard(unittest.TestCase):
    """The 3.12+ guard, exercised with fake versions instead of another interpreter."""

    def test_rejects_older(self):
        for v in ((3, 9, 6), (3, 11, 9)):
            with self.subTest(version=v):
                with self.assertRaises(SystemExit) as ctx:
                    cp._check_python(v)
                msg = str(ctx.exception.code)
                self.assertIn("Python 3.12+", msg)
                self.assertIn("found %d.%d.%d" % v, msg)
                self.assertNotIn("\n", msg)

    def test_accepts_supported(self):
        for v in ((3, 12, 0), (3, 13, 0)):
            with self.subTest(version=v):
                self.assertIsNone(cp._check_python(v))


class SingleLoad(unittest.TestCase):
    def test_module_executed_once(self):
        self.assertEqual(_support.LOADS, 1)


if __name__ == "__main__":
    unittest.main()
