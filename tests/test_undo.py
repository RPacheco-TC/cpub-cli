"""Story D (S0.5): the audit log and ``cpub undo``, on the real planner/executor.

Offline and in-process: Confluence is ``FakeConfluence``, the log file is an
in-memory fake (no file is written). Run from the repo root::

    python3.12 -B -m unittest discover -s tests
"""
import datetime
import itertools
import json
import pathlib
import unittest

from _support import config, docset, errors, executor, plan, runlog, storage, undo
from _fake_confluence import FakeConfluence

HOST = "example.invalid"
LOG = pathlib.Path("/home/u/.local/state/confluence-publisher/log.jsonl")
CLOCK = lambda: datetime.datetime(2026, 10, 28, 9, 30, tzinfo=datetime.timezone.utc)


class MemFiles:
    def __init__(self, text=""):
        self.text, self.fail = text, False

    def append_line(self, path, line):
        if self.fail:
            raise PermissionError(13, "Permission denied")
        self.text += line + "\n"

    def read_text(self, path):
        return self.text or None


def new_log(text=""):
    ids = (f"run{n}" for n in itertools.count(1))
    warnings = []
    log = runlog.RunLog(LOG, MemFiles(text), CLOCK, lambda: next(ids), warnings.append)
    return log, warnings


def tree():
    f = FakeConfluence()
    f.add("1", "Home")
    f.add("10", "TEST", parent="1")
    return f


def publish(f, log, md="# v2\n", name="Note.md"):
    """A real --under publish of one note, logged; returns the RunReport."""
    note = docset.SingleNote(pathlib.Path("/vault") / name, None, md)
    p = plan.Planner(f.client(), storage.StorageConverter()).build(note, "S1", "10")
    rec = log.recorder(runlog.PUBLISH, HOST, p.space_id)
    return executor.PlanExecutor(f.client(), today="2026-10-28").execute(
        p, adopt=True, log=rec)


def managed(f, pid, src):
    f.pages[pid]["props"]["cpub-managed"] = {
        "id": f"prop{pid}", "version": 1,
        "value": {"source": src, "tool": plan.MARKER_TOOL, "since": "2026-01-01"}}


def undo_last(f, log, confirm=lambda items: True, run_id=None):
    run = undo.UndoPlanner.select(log, HOST, run_id)
    up = undo.UndoPlanner(f.client(), log).build(run)
    rec = log.recorder(runlog.UNDO, HOST, run.space_id, undoes=run.run_id)
    return up, executor.PlanExecutor(f.client()).undo(up, confirm=confirm, log=rec)


class Dirs(unittest.TestCase):
    def test_state_dir_and_log_path(self):
        self.assertEqual(config.AppDirs({"HOME": "/home/u"}).log_path, LOG)
        self.assertEqual(config.AppDirs({"HOME": "/h", "XDG_STATE_HOME": "/s"}).log_path,
                         pathlib.Path("/s/confluence-publisher/log.jsonl"))


class Log(unittest.TestCase):
    def test_every_write_is_one_line_without_body_or_secrets(self):
        f, (log, _) = tree(), new_log()
        f.add("40", "Note", parent="10")
        managed(f, "40", "Note.md")
        publish(f, log, name="Note.md")                      # UPDATE 3 -> 4
        publish(f, log, md="# new\n", name="Other.md")       # CREATE v1
        lines = [json.loads(x) for x in log.files.text.splitlines()]
        self.assertEqual([(l["run_id"], l["action"], l["version_before"], l["version_after"])
                          for l in lines], [("run1", "UPDATE", 3, 4), ("run2", "CREATE", None, 1)])
        self.assertEqual(lines[0], {
            "run_id": "run1", "kind": "publish", "time": "2026-10-28T09:30:00Z",
            "host": HOST, "space_id": "S1", "page_id": "40", "title": "Note",
            "action": "UPDATE", "version_before": 3, "version_after": 4, "source": "Note.md"})
        text = log.files.text
        for secret in ("token", "x@example.invalid", "<p>", "v2"):
            self.assertNotIn(secret, text)

    def test_create_with_a_resave_logs_the_final_version(self):
        f, (log, _) = tree(), new_log()
        note = docset.SingleNote(pathlib.Path("/vault/D.md"), None, "# d\n")
        p = plan.Planner(f.client(), storage.StorageConverter()).build(note, "S1", "10")
        p.items[0].attachments = [("mermaid_a.png", "/x/a.png")]
        executor.PlanExecutor(f.client()).execute(p, log=log.recorder(runlog.PUBLISH, HOST))
        self.assertEqual(log.runs()[0].pages[0]["version_after"], 2)

    def test_a_failed_log_write_warns_once_and_never_aborts(self):
        f, (log, warnings) = tree(), new_log()
        log.files.fail = True
        report = publish(f, log)
        publish(f, log, name="B.md")
        self.assertEqual(len(report.written), 1)
        self.assertEqual(len(warnings), 2)                   # once per run
        self.assertIn(str(LOG), warnings[0])

    def test_runs_newest_first_damaged_lines_skipped(self):
        log, _ = new_log('{"run_id": "a", "kind": "publish", "action": "UPDATE"}\n'
                         "not json\n[1]\n"
                         '{"run_id": "b", "kind": "publish", "action": "CREATE"}\n')
        self.assertEqual([r.run_id for r in log.runs()], ["b", "a"])
        self.assertEqual(log.target().run_id, "b")


class Select(unittest.TestCase):
    def test_refusals_before_any_request(self):
        log, _ = new_log()
        with self.assertRaises(errors.UndoError) as cm:
            undo.UndoPlanner.select(log, HOST)
        self.assertEqual(cm.exception.code, "nothing")
        f = tree()
        publish(f, log)
        for run_id, host, code in (("nope", HOST, "unknown"), (None, "other.invalid",
                                                                "other-site")):
            with self.subTest(code=code), self.assertRaises(errors.UndoError) as cm:
                undo.UndoPlanner.select(log, host, run_id)
            self.assertEqual(cm.exception.code, code)
        self.assertEqual(cm.exception.logged_host, HOST)
        self.assertEqual(undo.UndoPlanner.select(log, HOST.upper()).run_id, "run1")


class Undo(unittest.TestCase):
    def setUp(self):
        self.f, (self.log, _) = tree(), new_log()
        self.f.add("40", "Note", parent="10")               # v3 "<p>old</p>"
        managed(self.f, "40", "Note.md")
        publish(self.f, self.log)                            # run1: 40 -> v4, Fresh created
        publish(self.f, self.log, name="Fresh.md")           # run2: CREATE only

    def test_restores_the_previous_version_and_lists_created_pages(self):
        up, report = undo_last(self.f, self.log, run_id="run1")
        self.assertEqual([(i.page_id, i.version_before, i.version_after) for i in up.restores],
                         [("40", 3, 4)])
        page = self.f.pages["40"]
        self.assertEqual((page["version"], page["body"]), (5, "<p>old</p>"))
        self.assertEqual(page["messages"][5], "Restored by cpub undo")
        self.assertEqual([w for w in self.f.writes if w[0] == "DELETE"], [])
        self.assertEqual(len(report.written), 1)
        up2, _ = undo_last(self.f, self.log)                 # last not-undone run: run2
        self.assertEqual(([c["title"] for c in up2.created], up2.restores), (["Fresh"], []))
        self.assertIn("Fresh", [p["title"] for p in self.f.pages.values()])   # never deleted

    def test_the_same_run_cannot_be_undone_twice(self):
        undo_last(self.f, self.log, run_id="run1")
        with self.assertRaises(errors.UndoError) as cm:
            undo.UndoPlanner.select(self.log, HOST, "run1")
        self.assertEqual(cm.exception.code, "undone")
        lines = [json.loads(x) for x in self.log.files.text.splitlines()]
        self.assertEqual([(l["kind"], l["action"], l.get("undoes")) for l in lines[-2:]],
                         [("undo", "RESTORE", "run1"), ("undo", "UNDO-END", "run1")])
        with self.assertRaises(errors.UndoError):            # an undo run is not undoable
            undo.UndoPlanner.select(self.log, HOST, lines[-1]["run_id"])

    def test_a_page_edited_since_is_refused_with_its_history_link(self):
        self.f.pages["40"]["version"] = 5                    # a colleague saved v5
        up, report = undo_last(self.f, self.log, run_id="run1")
        (it,) = up.refused
        self.assertEqual((it.reason, it.current, up.restores), ("edited", 5, []))
        self.assertIn("viewpreviousversions.action?pageId=40", it.history)
        self.assertEqual(self.f.pages["40"]["version"], 5)
        self.assertEqual(report.written, [])

    def test_gone_page_is_refused(self):
        del self.f.pages["40"]
        up, _ = undo_last(self.f, self.log, run_id="run1")
        self.assertEqual([i.reason for i in up.refused], ["gone"])

    def test_no_confirmation_means_no_write(self):
        for confirm, code in ((None, "not-interactive"), (lambda items: False, "declined")):
            before = len(self.f.writes)
            with self.subTest(code=code), self.assertRaises(errors.UndoError) as cm:
                undo_last(self.f, self.log, confirm=confirm, run_id="run1")
            self.assertEqual((cm.exception.code, len(self.f.writes)), (code, before))
        self.assertFalse(self.log.target("run1").undone)

    def test_an_edit_after_the_plan_is_skipped_and_the_run_stays_open(self):
        def confirm(items):                                  # someone saves while we ask
            self.f.pages["40"]["version"] = 5
            return True
        up, report = undo_last(self.f, self.log, confirm=confirm, run_id="run1")
        self.assertEqual((report.written, [i.reason for i in report.pending]), ([], ["edited"]))
        self.assertEqual(self.f.pages["40"]["version"], 5)  # the colleague's save stays
        self.assertNotIn(("POST", "/rest/api/content/40/version"), self.f.calls)
        self.assertFalse(self.log.target("run1").undone)


class ForwardAnchors(unittest.TestCase):
    """D37: the extra save of a forward heading link keeps the log consistent."""
    DOCS = (("01 - A", "[[02 - B#Notes]]\n"), ("02 - B", "## Notes\n[[01 - A#Top]]\n"))

    def publish_set(self, f, log):
        ds = docset.DocSet(tuple(docset.SourceDoc(t, md) for t, md in self.DOCS))
        p = plan.Planner(f.client(), storage.StorageConverter()).build(ds, "S1", "10")
        rec = log.recorder(runlog.PUBLISH, HOST, p.space_id)
        executor.PlanExecutor(f.client(), today="2026-10-28").execute(p, log=rec)
        return p

    def test_created_line_has_the_final_version_and_a_republish_undoes(self):
        f, (log, _) = tree(), new_log()
        first = self.publish_set(f, log)
        self.assertEqual([it.relink for it in first.items], [True, False])
        lines = [json.loads(x) for x in log.files.text.splitlines()]
        self.assertEqual([(x["title"], x["action"], x["version_after"]) for x in lines],
                         [("02 - B", "CREATE", 1), ("01 - A", "CREATE", 2)])
        a = lines[1]["page_id"]
        self.assertEqual(f.client().get_page_info(a)["version"], 2)   # the log matches
        up = undo.UndoPlanner(f.client(), log).build(undo.UndoPlanner.select(log, HOST))
        self.assertEqual((up.restores, up.refused, len(up.created)), ([], [], 2))
        again = self.publish_set(f, log)                 # a republish: UPDATEs, no extra save
        self.assertEqual([it.relink for it in again.items], [False, False])
        self.assertEqual(f.client().get_page_info(a)["version"], 3)
        up = undo.UndoPlanner(f.client(), log).build(undo.UndoPlanner.select(log, HOST))
        self.assertEqual([(r.page_id, r.version_after) for r in up.restores][:1], [(a, 3)])
        self.assertEqual(up.refused, [])


    def test_an_updated_page_resaved_for_a_new_target_undoes_to_before_the_run(self):
        f, (log, _) = tree(), new_log()
        f.add("20", "01 - A", parent="10")
        managed(f, "20", "01 - A")
        before = f.client().get_page_info("20")["version"]
        p = self.publish_set(f, log)
        self.assertEqual([(it.action, it.relink) for it in p.items],
                         [("UPDATE", True), ("CREATE", False)])
        line = [json.loads(x) for x in log.files.text.splitlines()][-1]
        self.assertEqual((line["action"], line["version_before"], line["version_after"]),
                         ("UPDATE", before, before + 2))
        self.assertEqual(f.client().get_page_info("20")["version"], before + 2)
        up = undo.UndoPlanner(f.client(), log).build(undo.UndoPlanner.select(log, HOST))
        self.assertEqual([(r.page_id, r.version_before) for r in up.restores], [("20", before)])
        self.assertEqual(up.refused, [])


if __name__ == "__main__":
    unittest.main()
