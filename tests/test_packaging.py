"""pyproject.toml contract (S1.3), checked with stdlib ``tomllib``: no build, no install.

Run from the repo root::

    python3.12 -B -m unittest discover -s tests
"""
import ast
import importlib
import inspect
import re
import tomllib
import unittest

from _support import ROOT, pkg

with open(ROOT / "pyproject.toml", "rb") as fh:
    PYPROJECT = tomllib.load(fh)
PROJECT = PYPROJECT["project"]


class Pyproject(unittest.TestCase):
    def test_setuptools_backend(self):
        self.assertEqual(PYPROJECT["build-system"]["build-backend"], "setuptools.build_meta")

    def test_requires_python_312(self):
        self.assertEqual(PROJECT["requires-python"], ">=3.12")

    def test_no_dependencies(self):
        self.assertEqual(PROJECT.get("dependencies", []), [])
        self.assertNotIn("optional-dependencies", PROJECT)

    def test_two_console_scripts_share_one_entry_point(self):
        main = "confluence_publisher.cli:main"
        self.assertEqual(PROJECT["scripts"], {"cpub": main, "confluence-publish": main})

    def test_entry_point_is_callable_without_arguments(self):
        for target in set(PROJECT["scripts"].values()):
            module, func = target.split(":")
            main = getattr(importlib.import_module(module), func)
            params = inspect.signature(main).parameters.values()
            required = [p for p in params if p.default is inspect.Parameter.empty]
            self.assertEqual(required, [], target)   # a console script calls main()

    def test_mit_license_declared(self):
        self.assertEqual(PROJECT["license"], "MIT")              # SPDX expression
        self.assertEqual(PROJECT["license-files"], ["LICENSE"])
        self.assertTrue((ROOT / "LICENSE").read_text().startswith("MIT License"))
        self.assertEqual(PYPROJECT["build-system"]["requires"], ["setuptools>=77"])

    def test_wheel_holds_only_the_package(self):
        self.assertEqual(PYPROJECT["tool"]["setuptools"]["packages"], ["confluence_publisher"])


class SingleVersionSource(unittest.TestCase):
    def test_version_is_dynamic_from_the_package(self):
        self.assertNotIn("version", PROJECT)
        self.assertEqual(PROJECT["dynamic"], ["version"])
        attr = PYPROJECT["tool"]["setuptools"]["dynamic"]["version"]["attr"]
        self.assertEqual(attr, "confluence_publisher.__version__")

    def test_version_is_a_literal_setuptools_can_read_statically(self):
        tree = ast.parse((ROOT / "confluence_publisher" / "__init__.py").read_text())
        literals = [n.value.value for n in tree.body
                    if isinstance(n, ast.Assign)
                    and [getattr(t, "id", None) for t in n.targets] == ["__version__"]
                    and isinstance(n.value, ast.Constant)]
        self.assertEqual(literals, [pkg.__version__])
        self.assertRegex(pkg.__version__, r"^\d+\.\d+\.\d+$")

    def test_no_other_version_string_in_the_package(self):
        hits = [p.name for p in (ROOT / "confluence_publisher").glob("*.py")
                if p.name != "__init__.py"
                and re.search(r'__version__\s*=', p.read_text())]
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
