"""publish_page() call order, with the REST helpers stubbed (offline, in-process).

Run from the repo root::

    python3.12 -B -m unittest discover -s tests
"""
import contextlib
import unittest

from _support import cp

ATTS = [("mermaid_aaa.png", "/x/aaa.png"), ("mermaid_bbb.png", "/x/bbb.png")]


@contextlib.contextmanager
def stubbed(page_id=None, existing=()):
    """Replace the REST helpers and md_to_storage; yield the recorded calls."""
    calls = []
    fakes = {
        "md_to_storage": lambda md, img_dir, prefix, img_width: ("<p>body</p>", list(ATTS)),
        "find_page_id_by_title": lambda space, title: page_id,
        "create_page": lambda *a: calls.append(("create",)) or {"id": "new"},
        "update_page": lambda pid, title, html, msg="": calls.append(("update", pid)),
        "existing_attachment_names": lambda pid: set(existing),
        "upload_attachment": lambda pid, fp, fn: calls.append(("upload", pid, fn)),
    }
    saved = {name: getattr(cp, name) for name in fakes}
    for name, fn in fakes.items():
        setattr(cp, name, fn)
    try:
        yield calls
    finally:
        for name, fn in saved.items():
            setattr(cp, name, fn)


def publish():
    return cp.publish_page("S", "P", "02 - Diagrams", "md", "/img")


class AttachmentsBeforeBody(unittest.TestCase):
    """The body is saved only after the attachments it references exist, so
    Confluence links them (the editor otherwise shows "Preview not available")."""

    def test_update_uploads_new_diagrams_before_saving_body(self):
        with stubbed(page_id="42", existing={"mermaid_aaa.png"}) as calls:
            action, pid, _ = publish()
        self.assertEqual((action, pid), ("UPDATE", "42"))
        self.assertEqual(calls, [("upload", "42", "mermaid_bbb.png"), ("update", "42")])

    def test_create_with_diagrams_resaves_body_after_upload(self):
        with stubbed() as calls:
            action, pid, _ = publish()
        self.assertEqual((action, pid), ("CREATE", "new"))
        self.assertEqual(calls, [("create",),
                                 ("upload", "new", "mermaid_aaa.png"),
                                 ("upload", "new", "mermaid_bbb.png"),
                                 ("update", "new")])

    def test_create_without_new_uploads_saves_once(self):
        with stubbed(existing={"mermaid_aaa.png", "mermaid_bbb.png"}) as calls:
            publish()
        self.assertEqual(calls, [("create",)])

    def test_dry_run_writes_nothing(self):
        with stubbed(page_id="42") as calls:
            action, _, atts = cp.publish_page("S", "P", "T", "md", "/img", dry_run=True)
        self.assertEqual((action, calls, len(atts)), ("UPDATE", [], 2))


if __name__ == "__main__":
    unittest.main()
