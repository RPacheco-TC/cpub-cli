"""execute_plan() per-page write order, with the REST helpers stubbed (offline, in-process).

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
        "space_title_index": lambda space: {"02 - Diagrams": page_id} if page_id else {},
        "get_page_info": lambda pid: {"id": pid, "title": "x", "spaceId": "S", "parentId": None,
                                      "status": "current", "version": 1, "last_edited": None, "webui": None},
        "is_descendant": lambda pid, anc: True,
        "get_marker": lambda pid: ({"source": "02 - Diagrams", "tool": cp.MARKER_TOOL}, "p1", 1),
        "set_marker": lambda pid, source, **kw: calls.append(("marker", pid)),
        "create_page": lambda *a: calls.append(("create",)) or {"id": "new"},
        "update_page": lambda pid, title, html, msg="", expected_version=None: calls.append(("update", pid)),
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


def publish(dry_run=False):
    """Plan + execute one managed page; return v1.1's ``(action, page_id, attachments)``."""
    plan = cp.build_plan("unused", "S", "P", "/img", docs=([("02 - Diagrams", "md")], None))
    item = plan.items[0]
    if dry_run:
        return item.action, item.page_id, item.attachments
    (_, pid), = cp.execute_plan(plan).written
    return item.action, pid, item.attachments


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
        self.assertEqual(calls, [("create",), ("marker", "new"),
                                 ("upload", "new", "mermaid_aaa.png"),
                                 ("upload", "new", "mermaid_bbb.png"),
                                 ("update", "new")])

    def test_create_without_new_uploads_saves_once(self):
        with stubbed(existing={"mermaid_aaa.png", "mermaid_bbb.png"}) as calls:
            publish()
        self.assertEqual(calls, [("create",), ("marker", "new")])

    def test_dry_run_writes_nothing(self):
        with stubbed(page_id="42") as calls:
            action, _, atts = publish(dry_run=True)
        self.assertEqual((action, calls, len(atts)), ("UPDATE", [], 2))


if __name__ == "__main__":
    unittest.main()
