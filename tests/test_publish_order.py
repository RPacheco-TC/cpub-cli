"""PlanExecutor per-page write order, with a recording fake client (offline, in-process).

Run from the repo root::

    python3.12 -B -m unittest discover -s tests
"""
import unittest

from _support import docset, executor, plan, storage

ATTS = [("mermaid_aaa.png", "/x/aaa.png"), ("mermaid_bbb.png", "/x/bbb.png")]


class FakeConverter:
    """Stands in for StorageConverter: every doc converts to one body with ATTS."""
    prefix = ""

    def convert(self, md):
        return storage.StorageDocument("<p>body</p>", list(ATTS))


class RecordingClient:
    """The ConfluenceClient methods the planner and executor use; writes are recorded."""

    def __init__(self, page_id=None, existing=()):
        self.page_id, self.existing, self.calls = page_id, set(existing), []

    # reads (planner)
    def get_page_info(self, pid):
        return {"id": pid, "title": "x", "spaceId": "S", "parentId": None,
                "status": "current", "version": 1, "last_edited": None, "webui": None}

    def space_title_index(self, space):
        return {"02 - Diagrams": self.page_id} if self.page_id else {}

    def is_descendant(self, pid, anc):
        return True

    def get_marker(self, pid):
        return {"source": "02 - Diagrams", "tool": plan.MARKER_TOOL}, "p1", 1

    def existing_attachment_names(self, pid):
        return set(self.existing)

    # writes (executor only)
    def set_marker(self, pid, source, **kw):
        self.calls.append(("marker", pid))

    def create_page(self, *a):
        self.calls.append(("create",))
        return {"id": "new"}

    def update_page(self, pid, title, html, msg="", expected_version=None):
        self.calls.append(("update", pid))

    def upload_attachment(self, pid, fp, fn):
        self.calls.append(("upload", pid, fn))


def publish(client, dry_run=False):
    """Plan + execute one managed page; return v1.1's ``(action, page_id, attachments)``."""
    source = docset.DocSet((docset.SourceDoc("02 - Diagrams", "md"),))
    p = plan.Planner(client, FakeConverter()).build(source, "S", "P")
    item = p.items[0]
    if dry_run:
        return item.action, item.page_id, item.attachments
    (_, pid), = executor.PlanExecutor(client).execute(p).written
    return item.action, pid, item.attachments


class AttachmentsBeforeBody(unittest.TestCase):
    """The body is saved only after the attachments it references exist, so
    Confluence links them (the editor otherwise shows "Preview not available")."""

    def test_update_uploads_new_diagrams_before_saving_body(self):
        client = RecordingClient(page_id="42", existing={"mermaid_aaa.png"})
        action, pid, _ = publish(client)
        self.assertEqual((action, pid), ("UPDATE", "42"))
        self.assertEqual(client.calls, [("upload", "42", "mermaid_bbb.png"), ("update", "42")])

    def test_create_with_diagrams_resaves_body_after_upload(self):
        client = RecordingClient()
        action, pid, _ = publish(client)
        self.assertEqual((action, pid), ("CREATE", "new"))
        self.assertEqual(client.calls, [("create",), ("marker", "new"),
                                        ("upload", "new", "mermaid_aaa.png"),
                                        ("upload", "new", "mermaid_bbb.png"),
                                        ("update", "new")])

    def test_create_without_new_uploads_saves_once(self):
        client = RecordingClient(existing={"mermaid_aaa.png", "mermaid_bbb.png"})
        publish(client)
        self.assertEqual(client.calls, [("create",), ("marker", "new")])

    def test_dry_run_writes_nothing(self):
        client = RecordingClient(page_id="42")
        action, _, atts = publish(client, dry_run=True)
        self.assertEqual((action, client.calls, len(atts)), ("UPDATE", [], 2))


if __name__ == "__main__":
    unittest.main()
