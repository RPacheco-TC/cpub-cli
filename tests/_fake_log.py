"""An in-memory audit log for CLI tests (S0.5): the real :class:`RunLog` on fake files.

Every test that lets a publish or an undo write pages injects one of these, so
no test ever appends to the real ``~/.local/state/confluence-publisher/log.jsonl``.
"""
import importlib

import _support  # noqa: F401  (loads the package from this checkout first)

runlog = importlib.import_module("confluence_publisher.runlog")


class MemoryLogFiles:
    """The :class:`~confluence_publisher.runlog.LogFiles` operations, in memory."""

    def __init__(self, text=""):
        self.text = text

    def append_line(self, path, line):
        self.text += line + "\n"

    def read_text(self, path):
        return self.text or None


def memory_log(text="", **kw):
    """A real ``RunLog`` whose file is a string (``.files.text``)."""
    return runlog.RunLog("/state/confluence-publisher/log.jsonl",
                         files=MemoryLogFiles(text), **kw)
