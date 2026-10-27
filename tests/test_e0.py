"""E0 overwrite protection (S0.1–S0.4) against an in-memory Confluence.

Run from the repo root::

    python3.12 -B -m unittest discover -s tests
"""
import json
import unittest
import urllib.error

from _support import docset, errors, executor, plan as cp, storage
from _fake_confluence import FakeConfluence

TODAY = "2026-10-27"
TOP = [("01 - Overview", "# Overview\n\nHello & welcome.\n"),
       ("02 - Api", "# Api\n\n- one\n- two\n")]


def managed(source):
    return {"source": source, "tool": cp.MARKER_TOOL, "since": "2026-01-01"}


def tree():
    """Home(1) ─┬─ TEST(10) ── Area(11)
                └─ Other team(20)"""
    f = FakeConfluence()
    f.add("1", "Home")
    f.add("10", "TEST", parent="1")
    f.add("11", "Area", parent="10")
    f.add("20", "Other team", parent="1")
    return f


def source(docs):
    """``(top, insitu)`` lists of ``(title, markdown)`` -> a DocSet."""
    top, insitu = docs
    return docset.DocSet(
        tuple(docset.SourceDoc(t, md) for t, md in top),
        None if insitu is None else tuple(docset.SourceDoc(t, md) for t, md in insitu))


def plan(fake, docs=(TOP, None), prefix=""):
    converter = storage.StorageConverter(prefix=prefix)
    return cp.Planner(fake.client(), converter).build(source(docs), "S1", "10")


def run(fake, p, *, confirm_handover=None, **kw):
    return executor.PlanExecutor(fake.client(), today=TODAY).execute(
        p, confirm=confirm_handover, **kw)


def check(p, max_updates=cp.DEFAULT_MAX_UPDATES):
    return p.refusals(max_updates)


class PlanIsReadOnly(unittest.TestCase):
    def test_build_plan_makes_zero_writes(self):
        f = tree(); f.add("30", "01 - Overview", parent="20")
        plan(f)
        self.assertTrue(f.calls)
        self.assertEqual(f.writes, [])

    def test_fresh_set_creates_and_marks_every_page(self):
        f = tree()
        p = plan(f)
        self.assertEqual([(i.action, i.managed) for i in p.items], [("CREATE", "new")] * 2)
        self.assertEqual(check(p), [])
        report = run(f, p)
        self.assertEqual([i.title for i, _ in report.written], ["01 - Overview", "02 - Api"])
        for _, pid in report.written:
            self.assertEqual(f.pages[pid]["parentId"], "10")
            self.assertEqual(f.pages[pid]["props"]["cpub-managed"]["value"],
                             {"source": f.pages[pid]["title"], "tool": cp.MARKER_TOOL,
                              "since": TODAY})

    def test_bodies_are_exactly_md_to_storage_output(self):
        f = tree()
        report = run(f, plan(f, prefix="R"))
        for (stem, md), (_, pid) in zip(TOP, report.written):
            expected = storage.StorageConverter(prefix="R").convert(md).html
            self.assertEqual(f.pages[pid]["body"], expected)
            self.assertEqual(f.pages[pid]["title"], f"R - {stem}")


class SubtreeFence(unittest.TestCase):
    def test_match_inside_subtree_updates(self):
        f = tree(); f.add("40", "01 - Overview", parent="10", marker=managed("01 - Overview"))
        p = plan(f)
        it = p.items[0]
        self.assertEqual((it.action, it.page_id, it.managed), ("UPDATE", "40", "same"))
        run(f, p)
        self.assertEqual(f.pages["40"]["version"], 4)
        # same source: the marker is left untouched
        self.assertNotIn(("PUT", "/api/v2/pages/40/properties/prop40"), f.calls)

    def test_match_deep_inside_subtree_updates(self):
        f = tree(); f.add("41", "02 - Api", parent="11", marker=managed("02 - Api"))
        p = plan(f)
        self.assertEqual(p.items[1].action, "UPDATE")
        self.assertEqual(check(p), [])
        run(f, p)
        self.assertEqual(f.pages["41"]["parentId"], "11")     # never moved

    def test_match_in_sibling_tree_is_refused_with_zero_writes(self):
        f = tree(); f.add("30", "02 - Api", parent="20", marker=managed("02 - Api"))
        p = plan(f)
        refusals = check(p)
        self.assertEqual([(r.code, r.page_id) for r in refusals], [("outside-subtree", "30")])
        self.assertEqual(refusals[0].message,
                         '"02 - Api" already exists outside "TEST" (10) at '
                         'https://example.invalid/wiki/spaces/X/pages/30 — not touching it.')
        self.assertEqual(refusals[0].fix, "Use a distinct --prefix.")
        with self.assertRaises(errors.PlanRefused) as cm:
            run(f, p, adopt=True)
        self.assertEqual(len(cm.exception.refusals), 1)
        self.assertEqual(f.writes, [])                       # page 01 (allowed) not written either
        self.assertEqual(f.pages["30"]["version"], 3)

    def test_deep_subtree_with_paginated_ancestors_updates(self):
        f = tree()
        f.add("12", "L3", parent="11"); f.add("13", "L4", parent="12")
        f.add("43", "01 - Overview", parent="13", marker=managed("01 - Overview"))
        f.ancestor_page_size = 2                      # Home, TEST | Area, L3 | L4
        p = plan(f)
        self.assertTrue(p.items[0].in_subtree)
        self.assertEqual(check(p), [])
        reads = [c for c in f.calls if c == ("GET", "/api/v2/pages/43/ancestors")]
        self.assertGreaterEqual(len(reads), 3)

    def test_page_with_two_problems_gets_both_refusals(self):
        f = tree(); f.add("30", "01 - Overview", parent="20", status="archived")
        self.assertEqual([r.code for r in check(plan(f))], ["outside-subtree", "not-current"])

    def test_refused_pages_are_never_offered_for_hand_over(self):
        f = tree()
        f.add("30", "01 - Overview", parent="20")                    # sibling-tree decoy
        f.add("42", "02 - Api", parent="10", status="archived")      # archived, inside
        # plus a doc titled like the parent
        p = plan(f, docs=(TOP + [("TEST", "x\n")], None))
        self.assertEqual(len(check(p)), 3)
        self.assertEqual(p.handover_items(), [])

    def test_foreign_marker_is_not_read(self):
        f = tree(); f.add("30", "02 - Api", parent="20")
        plan(f)
        self.assertNotIn(("GET", "/api/v2/pages/30/properties"), f.calls)

    def test_match_is_the_parent_is_refused(self):
        f = tree()
        p = plan(f, docs=([("TEST", "x\n")], None))
        self.assertEqual([r.code for r in check(p)], ["is-parent"])

    def test_space_homepage_match_is_refused(self):
        f = tree()
        p = plan(f, docs=([("Home", "x\n")], None))
        self.assertEqual([r.code for r in check(p)], ["outside-subtree"])

    def test_every_refusal_is_listed(self):
        f = tree(); f.add("30", "01 - Overview", parent="20"); f.add("31", "02 - Api", parent="1")
        self.assertEqual([r.page_id for r in check(plan(f))], ["30", "31"])

    def test_archived_match_inside_subtree_is_refused(self):
        f = tree(); f.add("42", "01 - Overview", parent="10", status="archived")
        self.assertEqual([r.code for r in check(plan(f))], ["not-current"])

    def test_parent_in_another_space_is_refused(self):
        f = tree(); f.pages["10"]["spaceId"] = "S2"
        self.assertIn("parent-space", [r.code for r in check(plan(f))])


class HandOver(unittest.TestCase):
    def setUp(self):
        self.f = tree()
        # 40: unmanaged (v1.1 or hand-written); 41: managed from another source
        self.f.add("40", "01 - Overview", parent="10")
        self.f.add("41", "02 - Api", parent="10", marker=managed("other repo - 02 - Api"))

    def test_classification(self):
        p = plan(self.f)
        self.assertEqual([i.managed for i in p.items], ["none", "other"])
        self.assertEqual([i.page_id for i in p.handover_items()], ["40", "41"])

    def test_non_interactive_without_adopt_writes_nothing(self):
        with self.assertRaises(errors.HandoverRequired) as cm:
            run(self.f, plan(self.f))
        self.assertEqual(len(cm.exception.items), 2)
        self.assertEqual(self.f.writes, [])

    def test_declined_writes_nothing(self):
        asked = []
        with self.assertRaises(errors.HandoverDeclined):
            run(self.f, plan(self.f), confirm_handover=lambda items: asked.append(items) or False)
        self.assertEqual([i.page_id for i in asked[0]], ["40", "41"])
        self.assertEqual(self.f.writes, [])

    def test_confirmed_hand_over_updates_and_marks(self):
        run(self.f, plan(self.f), confirm_handover=lambda items: True)
        marker = self.f.pages["40"]["props"]["cpub-managed"]["value"]
        self.assertEqual(marker["source"], "01 - Overview")
        prop = self.f.pages["41"]["props"]["cpub-managed"]
        self.assertEqual((prop["value"]["source"], prop["version"]), ("02 - Api", 2))
        # next run from the same source needs no hand-over
        p = plan(self.f)
        self.assertEqual(p.handover_items(), [])
        run(self.f, p)                                # confirm_handover=None, adopt=False: fine

    def test_adopt_skips_the_prompt(self):
        run(self.f, plan(self.f), adopt=True,
            confirm_handover=lambda items: self.fail("must not ask with --adopt"))
        self.assertEqual(self.f.pages["40"]["version"], 4)

    def test_failed_marker_write_is_reported_not_fatal(self):
        self.f.fail_on = ("POST", "/api/v2/pages/40/properties")
        report = run(self.f, plan(self.f), adopt=True)
        self.assertEqual([(i.title, pid) for i, pid, _ in report.marker_failures],
                         [("01 - Overview", "40")])
        self.assertEqual(len(report.written), 2)
        self.assertEqual(self.f.pages["40"]["version"], 4)


class WhoAmI(unittest.TestCase):
    def call(self, user):
        f = tree(); f.user = user
        out = f.client().whoami()
        self.assertEqual(f.calls, [("GET", "/rest/api/user/current")])    # one read, no writes
        return out

    def test_valid_credentials(self):
        out = self.call({"type": "known", "accountId": "acc-1", "displayName": "Dev",
                         "email": "d@x"})
        self.assertEqual((out["authenticated"], out["accountId"], out["status"]),
                         (True, "acc-1", 200))

    def test_bad_token_served_anonymously(self):
        out = self.call({"type": "anonymous", "displayName": "Anonymous"})
        self.assertEqual((out["authenticated"], out["type"]), (False, "anonymous"))

    def test_401_and_403_are_not_authenticated(self):
        for code in (401, 403):
            self.assertEqual(self.call(code)["status"], code)
            self.assertFalse(self.call(code)["authenticated"])

    def test_other_errors_propagate(self):
        f = tree(); f.user = 500
        with self.assertRaises(urllib.error.HTTPError):
            f.client().whoami()


class VersionRace(unittest.TestCase):
    def test_edit_between_plan_and_write_is_rejected_not_overwritten(self):
        f = tree()
        f.add("40", "01 - Overview", parent="10", marker=managed("01 - Overview"), version=3)
        p = plan(f)
        self.assertEqual(p.items[0].version, 3)
        f.pages["40"]["version"] = 4                  # a colleague saves while the prompt is open
        puts = []
        real = f.request

        def spy(m, u, d=None, h=None):
            if m == "PUT":
                puts.append(d)
            return real(m, u, d, h)

        f.request = spy
        with self.assertRaises(errors.PlanExecutionError) as cm:
            run(f, p)
        self.assertEqual(cm.exception.report.error.code, 409)
        # the plan's 3 + 1, not the re-read 4 + 1
        self.assertEqual(json.loads(puts[0])["version"]["number"], 4)
        self.assertEqual(f.pages["40"]["body"], "<p>old</p>")


class BlastRadius(unittest.TestCase):
    def test_updates_over_the_cap_are_refused(self):
        f = tree()
        for i, (stem, _) in enumerate(TOP):
            f.add(f"4{i}", stem, parent="10", marker=managed(stem))
        p = plan(f)
        r = check(p, max_updates=1)
        self.assertEqual([x.code for x in r], ["max-updates"])
        self.assertEqual(r[0].fix, "Re-run with --max-updates 2 if that is intended.")
        with self.assertRaises(errors.PlanRefused):
            run(f, p, max_updates=1)
        self.assertEqual(f.writes, [])
        run(f, p, max_updates=2)

    def test_creates_do_not_count(self):
        f = tree()
        self.assertEqual(check(plan(f), max_updates=0), [])

    def test_default_cap_is_25(self):
        f = tree()
        docs = [(f"{n:02} - Doc", "x\n") for n in range(26)]
        for n, (stem, _) in enumerate(docs):
            f.add(f"5{n:02}", stem, parent="10", marker=managed(stem))
        self.assertEqual([r.code for r in check(plan(f, docs=(docs, None)))], ["max-updates"])
        self.assertEqual(check(plan(f, docs=(docs[:25], None))), [])


class InSitu(unittest.TestCase):
    DOCS = (TOP[:1], [("Django", "# Django\n")])

    def test_new_container_is_created_marked_and_parents_the_deep_dives(self):
        f = tree()
        p = plan(f, docs=self.DOCS, prefix="R")
        self.assertEqual([(i.kind, i.title, i.action) for i in p.items],
                         [("page", "R - 01 - Overview", "CREATE"),
                          ("container", "R - In Situ", "CREATE"),
                          ("page", "R - Django", "CREATE")])
        report = run(f, p)
        ids = {i.title: pid for i, pid in report.written}
        self.assertEqual(f.pages[ids["R - In Situ"]]["body"], cp.INSITU_CONTAINER_BODY)
        self.assertEqual(f.pages[ids["R - In Situ"]]["parentId"], "10")
        self.assertEqual(f.pages[ids["R - Django"]]["parentId"], ids["R - In Situ"])
        self.assertIn("cpub-managed", f.pages[ids["R - In Situ"]]["props"])

    def test_existing_container_is_reused_without_writing_it(self):
        f = tree(); f.add("60", "In Situ", parent="10")   # unmanaged; its body is never rewritten
        p = plan(f, docs=self.DOCS)
        container = p.items[1]
        self.assertEqual((container.action, container.writes_body, container.needs_handover),
                         ("UPDATE", False, False))
        self.assertEqual(check(p, max_updates=0), [])
        run(f, p)
        self.assertEqual(f.pages["60"]["version"], 3)
        self.assertFalse([c for c in f.writes if "/pages/60" in c[1]])
        django = next(pid for pid, pg in f.pages.items() if pg["title"] == "Django")
        self.assertEqual(f.pages[django]["parentId"], "60")

    def test_container_outside_the_subtree_is_refused(self):
        f = tree(); f.add("61", "In Situ", parent="20")
        p = plan(f, docs=self.DOCS)
        self.assertEqual([(r.code, r.page_id) for r in check(p)], [("outside-subtree", "61")])


class MidRunFailure(unittest.TestCase):
    def test_reports_written_and_pending_then_reruns_cleanly(self):
        f = tree()
        f.fail_on = ("PUT", "/api/v2/pages/")      # page 02 exists, its body update fails
        f.add("41", "02 - Api", parent="10", marker=managed("02 - Api"))
        with self.assertRaises(errors.PlanExecutionError) as cm:
            run(f, plan(f))
        rep = cm.exception.report
        self.assertEqual([i.title for i, _ in rep.written], ["01 - Overview"])
        self.assertEqual([i.title for i in rep.pending], ["02 - Api"])
        self.assertEqual(rep.error.code, 500)
        f.fail_on = None
        p = plan(f)
        self.assertEqual([(i.action, i.managed) for i in p.items],
                         [("UPDATE", "same"), ("UPDATE", "same")])
        run(f, p)
        self.assertEqual(f.pages["41"]["version"], 4)

    def test_failure_after_create_is_reported_as_partial(self):
        f = tree()
        p = plan(f)
        p.items[0].attachments = [("mermaid_a.png", "/x/a.png")]
        f.fail_on = ("POST", "/rest/api/content/1000/child/attachment")   # upload down
        with self.assertRaises(errors.PlanExecutionError) as cm:
            run(f, p)
        rep = cm.exception.report
        self.assertEqual((rep.partial[0].title, rep.partial[1]), ("01 - Overview", "1000"))
        # marked before the uploads, so a re-run is prompt-free
        self.assertIn("cpub-managed", f.pages["1000"]["props"])


class DryRun(unittest.TestCase):
    """A dry run is the plan alone: v1.1 rows, refusals, zero writes."""

    def test_returns_v11_rows_and_dry_run_writes_nothing(self):
        f = tree(); f.add("40", "01 - Overview", parent="10", marker=managed("01 - Overview"))
        rows = plan(f).rows()
        self.assertEqual(rows, [("01 - Overview", "UPDATE", 0), ("02 - Api", "CREATE", 0)])
        self.assertEqual(f.writes, [])

    def test_dry_run_raises_on_refusal(self):
        f = tree(); f.add("30", "01 - Overview", parent="20")
        p = plan(f)
        self.assertEqual([r.code for r in check(p)], ["outside-subtree"])
        with self.assertRaises(errors.PlanRefused):
            run(f, p)
        self.assertEqual(f.writes, [])


if __name__ == "__main__":
    unittest.main()
