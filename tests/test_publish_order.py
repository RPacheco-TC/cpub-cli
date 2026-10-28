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

    def convert(self, md, headings_of=None):
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


class SetClient(RecordingClient):
    """Pages get ids 1, 2, …; ``titles`` already exist (UPDATE targets)."""

    def __init__(self, titles=None):
        super().__init__()
        self.titles, self.ids = dict(titles or {}), iter("123456789")

    def space_title_index(self, space):
        return dict(self.titles)

    def create_page(self, space, parent, title, html):
        self.calls.append(("create", title))
        return {"id": next(self.ids), "version": {"number": 1}}

    def update_page(self, pid, title, html, msg="", expected_version=None):
        self.calls.append(("update", pid, expected_version, msg))
        return {"version": {"number": expected_version + 1}}


def run_set(client, docs):
    source = docset.DocSet(tuple(docset.SourceDoc(t, md) for t, md in docs))
    p = plan.Planner(client, storage.StorageConverter()).build(source, "S", "P")
    events = []
    executor.PlanExecutor(client).execute(p, adopt=True, log=events.append)
    return p, events


class ForwardAnchors(unittest.TestCase):
    """D37: a new page linking a heading of a page created later is saved once more,
    after every page exists (Confluence links an anchor only to an existing page)."""
    DOCS = (("01 - A", "[[02 - B#Notes]] [[03 - C]]\n"), ("02 - B", "## Notes\n[[01 - A#Top]]\n"),
            ("03 - C", "[[01 - A]]\n"))

    def test_only_the_forward_anchor_page_is_resaved_last(self):
        client = SetClient()
        p, events = run_set(client, self.DOCS)
        self.assertEqual([it.relink for it in p.items], [True, False, False])
        writes = [c for c in client.calls if c[0] != "marker"]
        self.assertEqual(writes, [("create", "01 - A"), ("create", "02 - B"),
                                  ("create", "03 - C"),
                                  ("update", "1", 1, "link heading anchors")])
        self.assertEqual([(e["title"], e["action"], e["version_after"]) for e in events],
                         [("02 - B", "CREATE", 1), ("03 - C", "CREATE", 1),
                          ("01 - A", "CREATE", 2)])          # one line per page, last version

    def test_existing_targets_and_no_anchor_sets_add_no_write(self):
        client = SetClient({"02 - B": "7"})              # B already exists: an UPDATE
        p, _ = run_set(client, self.DOCS)
        self.assertEqual([it.relink for it in p.items], [False, False, False])
        self.assertNotIn("link heading anchors", [c[-1] for c in client.calls])
        plain = SetClient()
        run_set(plain, (("01 - A", "[[02 - B]]\n"), ("02 - B", "x\n")))
        self.assertEqual([c[0] for c in plain.calls if c[0] != "marker"],
                         ["create", "create"])

    def test_an_update_linking_forward_to_a_new_page_is_resaved_last(self):
        client = SetClient({"01 - A": "7"})              # A exists: UPDATE, B is new
        p, events = run_set(client, (("01 - A", "[[02 - B#Notes]]\n"), ("02 - B", "## Notes\n")))
        self.assertEqual([(it.action, it.relink) for it in p.items],
                         [("UPDATE", True), ("CREATE", False)])
        writes = [c for c in client.calls if c[0] in ("create", "update")]
        self.assertEqual(writes, [("update", "7", 1, "republish"), ("create", "02 - B"),
                                  ("update", "7", 2, "link heading anchors")])
        self.assertEqual([(e["title"], e["action"], e["version_before"], e["version_after"])
                          for e in events],
                         [("02 - B", "CREATE", None, 1), ("01 - A", "UPDATE", 1, 3)])
        self.assertEqual(p.refusals(1), [])              # one page: one update counted

    def test_a_failed_resave_still_logs_the_created_page(self):
        class Failing(SetClient):
            def update_page(self, *a, **kw):
                raise OSError("boom")
        events = []
        source = docset.DocSet(tuple(docset.SourceDoc(t, md) for t, md in self.DOCS))
        client = Failing()
        p = plan.Planner(client, storage.StorageConverter()).build(source, "S", "P")
        with self.assertRaises(executor.PlanExecutionError):
            executor.PlanExecutor(client).execute(p, log=events.append)
        self.assertEqual([(e["title"], e["version_after"]) for e in events],
                         [("02 - B", 1), ("03 - C", 1), ("01 - A", 1)])


if __name__ == "__main__":
    unittest.main()
