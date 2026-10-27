"""Shared test fixture: import the ``confluence_publisher`` package exactly once per run.

Test modules import the submodule they exercise from here (``from _support
import plan as cp``). Python caches the package in ``sys.modules``, so it is
executed a single time however many test modules use it. Patch a function in
the module that *looks it up* (e.g. ``plan.create_page``, ``cli.load_env``,
``api._req``): each module imports the names it calls.
"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
LOADS = 0  # how many times the package was executed; asserted to be 1


def _load():
    global LOADS
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    fresh = "confluence_publisher" not in sys.modules
    import confluence_publisher.cli  # loads every submodule
    LOADS += fresh
    return confluence_publisher


pkg = _load()
from confluence_publisher import api, cli, config, errors, markdown_html, plan, storage  # noqa: E402
