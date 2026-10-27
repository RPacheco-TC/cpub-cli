"""Shared test fixture: load ``confluence_publish.py`` exactly once per run.

Every test module imports ``cp`` from here. Python caches this module in
``sys.modules``, so the CLI module (and the ``md_to_confluence.py`` it loads)
is executed a single time however many test modules use it.
"""
import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
LOADS = 0  # how many times confluence_publish.py was executed; asserted to be 1


def _load():
    global LOADS
    spec = importlib.util.spec_from_file_location("confluence_publish", ROOT / "confluence_publish.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    LOADS += 1
    return module


cp = _load()
