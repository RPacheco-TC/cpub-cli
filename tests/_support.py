"""Shared test fixture: import the ``confluence_publisher`` package exactly once per run.

Test modules import the submodule they exercise from here (``from _support
import plan``). Python caches the package in ``sys.modules``, so it is
executed a single time however many test modules use it. Fakes are injected
through constructors (e.g. ``ConfluenceClient(creds, FakeConfluence())``);
no test patches package code with ``setattr``.
"""
import importlib
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
LOADS = 0  # how many times the package was executed; asserted to be 1


def _load():
    global LOADS
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    fresh = "confluence_publisher" not in sys.modules
    import confluence_publisher
    LOADS += fresh
    return confluence_publisher


pkg = _load()
from confluence_publisher import (api, config, docset, errors, executor,  # noqa: E402
                                  markdown_html, plan, storage)


def __getattr__(name):
    """Load ``cli`` (the composition root) only when a test asks for it."""
    if name == "cli":
        return importlib.import_module("confluence_publisher.cli")
    raise AttributeError(name)
