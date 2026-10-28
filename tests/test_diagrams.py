"""Mermaid without npx (story A): find npx off PATH, refuse a note, warn for a set.

Offline and in-process: the renderer is injected (no npx, no disk writes) and
Confluence is the ``FakeConfluence`` transport. Run from the repo root::

    python3.12 -B -m unittest discover -s tests
"""
import pathlib
import types
import unittest

from _support import config, docset, errors, markdown_html, plan, storage
from _fake_confluence import FakeConfluence

MD = "# Doc\n\n```mermaid\ngraph TD\n  A --> B\n```\n\ntext\n\n```mermaid\ngraph LR\n  C\n```\n"
NO_IMG = "/nonexistent/cpub-test-img"         # never created: no render is attempted


def converter(npx=None):
    """A converter whose Mermaid renderer has (or lacks) npx; no img_dir, no disk."""
    mermaid = markdown_html.MermaidRenderer(NO_IMG, npx=npx, runner=FailRunner())
    return storage.StorageConverter(renderer=markdown_html.MarkdownRenderer(mermaid))


class FailRunner:
    """A ``subprocess.run`` stand-in that records calls and renders nothing."""
    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        return types.SimpleNamespace(returncode=1, stderr="boom")


def tree():
    f = FakeConfluence()
    f.add("1", "Home")
    f.add("10", "TEST", parent="1")
    f.add("40", "Their page", parent="10")
    return f


class FindExecutable(unittest.TestCase):
    def dirs(self, path, present):
        d = config.AppDirs({"HOME": "/h", "PATH": path, "LANG": "C"})
        return d.find_executable("npx", is_exe=lambda p: str(p) in present)

    def test_path_hit_inherits_the_environment(self):
        self.assertEqual(self.dirs("/usr/bin:/opt/n/bin", {"/opt/n/bin/npx"}),
                         ("/opt/n/bin/npx", None))

    def test_fallback_hit_puts_its_folder_first_on_path(self):
        found = self.dirs("/usr/bin:/bin", {"/opt/homebrew/bin/npx", "/usr/local/bin/npx"})
        self.assertEqual(found[0], "/opt/homebrew/bin/npx")
        self.assertEqual(found[1], {"HOME": "/h", "LANG": "C",
                                    "PATH": "/opt/homebrew/bin:/usr/bin:/bin"})
        found = self.dirs("", {"/usr/local/bin/npx"})            # empty PATH
        self.assertEqual(found, ("/usr/local/bin/npx", {"HOME": "/h", "LANG": "C",
                                                        "PATH": "/usr/local/bin"}))

    def test_unreadable_candidate_is_skipped_not_a_traceback(self):
        def is_exe(p):                    # B6: Path.is_file() raises EPERM in a sandbox
            if str(p) == "/opt/homebrew/bin/npx":
                raise PermissionError(1, "Operation not permitted", str(p))
            return str(p) == "/usr/local/bin/npx"
        d = config.AppDirs({"PATH": "/usr/bin"})
        self.assertEqual(d.find_executable("npx", is_exe=is_exe)[0], "/usr/local/bin/npx")
        self.assertIsNone(d.find_executable("npx", fallback_dirs=("/opt/homebrew/bin",),
                                            is_exe=is_exe))

    def test_not_found_and_injected_search_list(self):
        self.assertIsNone(self.dirs("/usr/bin:/bin", set()))
        d = config.AppDirs({"PATH": "/usr/bin"})
        self.assertEqual(d.find_executable("npx", fallback_dirs=("/x",),
                                           is_exe=lambda p: str(p) == "/x/npx")[0], "/x/npx")
        self.assertIsNone(d.find_executable("npx", fallback_dirs=(),
                                            is_exe=lambda p: str(p) == "/x/npx"))


class Renderer(unittest.TestCase):
    def test_without_npx_nothing_is_tried_and_the_skip_is_counted(self):
        runner = FailRunner()
        m = markdown_html.MermaidRenderer(NO_IMG, npx=None, runner=runner)
        self.assertIsNone(m.render("graph TD\n A"))
        self.assertEqual((m.skipped, runner.calls), (1, []))
        self.assertFalse(pathlib.Path(NO_IMG).exists())

    def test_found_npx_is_run_by_its_path_with_its_environment(self):
        env = {"PATH": "/opt/homebrew/bin:/usr/bin"}
        m = markdown_html.MermaidRenderer(NO_IMG, npx=("/opt/homebrew/bin/npx", env))
        argv, got = m._command("/t/a.mmd", "/i/a.png")
        self.assertEqual(argv, ["/opt/homebrew/bin/npx", "-y", "-p", "@mermaid-js/mermaid-cli",
                                "mmdc", "-i", "/t/a.mmd", "-o", "/i/a.png",
                                "-b", "white", "-s", "2", "-q"])
        self.assertIs(got, env)

    def test_default_is_npx_on_path_as_before(self):
        argv, env = markdown_html.MermaidRenderer(NO_IMG)._command("a.mmd", "a.png")
        self.assertEqual((argv[0], env), ("npx", None))


class Converter(unittest.TestCase):
    def test_unrendered_count_and_unchanged_fallback_body(self):
        doc = converter(npx=None).convert(MD)
        self.assertEqual((doc.unrendered, doc.attachments), (2, []))
        self.assertEqual(doc.html, storage.StorageConverter().convert(MD).html)
        self.assertEqual(converter(npx=None).convert("# no diagrams\n").unrendered, 0)

    def test_no_renderer_at_all_is_not_counted(self):
        self.assertEqual(storage.StorageConverter().convert(MD).unrendered, 0)


class NoteIsRefused(unittest.TestCase):
    def note(self):
        return docset.SingleNote(pathlib.Path("/vault/Arch.md"), None, MD)

    def assert_refused_before_any_request(self, build):
        f = tree()
        with self.assertRaises(errors.DiagramsUnrenderable) as cm:
            build(plan.Planner(f.client(), converter(npx=None)), self.note())
        self.assertEqual((cm.exception.note, cm.exception.count), ("Arch.md", 2))
        self.assertEqual(str(cm.exception),
                         "Diagrams in Arch.md cannot be rendered (npx not found).")
        self.assertIsInstance(cm.exception, errors.PublisherError)
        self.assertEqual((f.calls, f.writes), ([], []))

    def test_under(self):
        self.assert_refused_before_any_request(lambda p, n: p.build(n, None, "10"))

    def test_to(self):
        self.assert_refused_before_any_request(lambda p, n: p.build_replace(n, "40"))


class SetModeWarns(unittest.TestCase):
    def test_set_plan_lists_the_docs_and_keeps_the_fallback(self):
        f = tree()
        ds = docset.DocSet((docset.SourceDoc("01 - A", MD), docset.SourceDoc("02 - B", "x\n")))
        p = plan.Planner(f.client(), converter(npx=None)).build(ds, "S1", "10")
        self.assertEqual([(it.title, it.unrendered) for it in p.unrendered_items()],
                         [("01 - A", 2)])
        self.assertEqual(p.items[0].storage, storage.StorageConverter().convert(MD).html)
        self.assertEqual(p.refusals(), [])


if __name__ == "__main__":
    unittest.main()
