"""confluence_publisher: publish Obsidian-style Markdown to Confluence Cloud.

Stdlib only. Importing the package runs the Python version guard first, so the
user of an old interpreter gets one line instead of a traceback.

Keep this file parseable by old Pythons (no 3.10+ syntax, no annotations) and
import no submodule from it: the submodules use ``X | None`` annotations that
are evaluated at definition time and would fail before the guard could run.
"""
import sys

__version__ = "1.4.2"

MIN_PYTHON = (3, 12)


def _check_python(version_info=None):
    """Exit with a one-line message when the interpreter is older than :data:`MIN_PYTHON`.

    :param version_info: version to check (``sys.version_info`` when ``None``);
        injectable so tests can exercise the guard without another interpreter.
    :raises SystemExit: if the version is below the minimum.
    """
    v = sys.version_info if version_info is None else version_info
    if tuple(v[:2]) < MIN_PYTHON:
        raise SystemExit(
            "confluence_publish requires Python %d.%d+ (found %d.%d.%d)"
            % (MIN_PYTHON + tuple(v[:3])))


_check_python()
