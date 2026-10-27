"""CLI wiring of the Phase 1a safety features (E0, S1.1, S3.1), in-process.

The library layer (``build_plan`` / ``check_plan`` / ``execute_plan`` /
``load_env`` …) is replaced by small fakes on the module, so these tests pin
what ``main()`` does with its results: which calls happen, what is printed,
and the wording of every expected failure. No network, no files, no
subprocess; ``load_env`` is stubbed so a developer's real ``./.env`` is never
read. Run from the repo root::

    python3.12 -B -m unittest discover -s tests
"""
import contextlib
import io
import os
import pathlib
import ssl
import types
import unittest
import urllib.error

from _support import cli as cp, plan, ROOT

DOCS = str(ROOT / "tests")          # a real folder with no .md files
BASE = "https://example.invalid/wiki"
TOKEN = "s3cr3t-token-value"
CREDS = {"CONFLUENCE_BASE_URL": BASE, "CONFLUENCE_EMAIL": "x@example.invalid",
         "CONFLUENCE_API_TOKEN": TOKEN}
ARGV = [DOCS, "--space", "ENG", "--parent", "100", "--prefix", "p"]


def item(title, action="UPDATE", managed="same", diagrams=0, version=3,
         last_edited="2026-10-01T10:00:00Z", page_id=None):
    if page_id is None and action == "UPDATE":
        page_id = "id-" + title
    return types.SimpleNamespace(title=title, action=action, managed=managed,
                                 diagrams=diagrams, version=version,
                                 last_edited=last_edited, page_id=page_id)


def refusal(title, message, fix):
    return types.SimpleNamespace(code="outside-subtree", title=title, page_id="9",
                                 message=message, fix=fix)


@contextlib.contextmanager
def environ(values):
    saved = dict(os.environ)
    os.environ.clear(); os.environ.update(values)
    try:
        yield
    finally:
        os.environ.clear(); os.environ.update(saved)


class Fake:
    """Records library calls; ``execute`` decides what execute_plan does."""

    def __init__(self, items, refusals=(), execute=None, tty=False, answer=None,
                 resolved=None, http_error=None):
        self.plan = types.SimpleNamespace(
            items=list(items), parent_info={"webui": "/spaces/ENG/pages/100/Parent"})
        self.refusals = list(refusals)
        self.execute = execute
        self.tty, self.answer = tty, answer
        self.resolved = {} if resolved is None else resolved
        self.http_error = http_error
        self.calls, self.prompts = [], []

    def install(self):
        f = self

        def build_plan(*a, **k):
            f.calls.append("build_plan")
            if f.http_error:
                raise f.http_error
            return f.plan

        def execute_plan(plan, **k):
            f.calls.append(("execute_plan", k.get("adopt"), k.get("max_updates"),
                            k.get("confirm_handover") is not None))
            if f.execute:
                return f.execute(plan, **k)
            return types.SimpleNamespace(written=[(i, "1") for i in plan.items],
                                         pending=[], marker_failures=[], error=None)

        def ask(prompt):
            f.prompts.append(prompt)
            if isinstance(f.answer, BaseException):
                raise f.answer
            return f.answer

        return {
            "load_env": lambda env_file=None, **k: f.calls.append(("load_env", env_file)) or f.resolved,
            "env_search_paths": lambda env_file=None, **k: [pathlib.Path("/home/u/proj/.env"),
                                                             pathlib.Path("/home/u/.config/confluence-publisher/.env")],
            "resolve_space_id": lambda key: "S1",
            "whoami": lambda: {"authenticated": True, "status": 200},
            "build_plan": build_plan,
            "check_plan": lambda plan, max_updates=25: list(f.refusals),
            "handover_items": lambda plan: [i for i in plan.items
                                            if i.action == "UPDATE" and i.managed in ("none", "other")],
            "execute_plan": execute_plan,
            "_stdin_is_tty": lambda: f.tty,
            "_ask": ask,
        }


def run(fake, *extra, env=CREDS):
    """Run main() with the fakes; return (exit_message_or_None, stdout)."""
    patches = fake.install()
    saved = {n: getattr(cp, n) for n in patches}
    out = io.StringIO()
    try:
        for n, fn in patches.items():
            setattr(cp, n, fn)
        with environ(env):
            try:
                cp.main([*ARGV, *extra], out=out)
                return None, out.getvalue()
            except SystemExit as e:
                return e.code, out.getvalue()
    finally:
        for n, fn in saved.items():
            setattr(cp, n, fn)


def http_error(code, reason, detail):
    e = urllib.error.HTTPError(BASE, code, reason, {}, None)
    e.detail = detail
    return e


def assert_friendly(tc, msg):
    """S3.1: one sentence + a Fix: line, no traceback, no secret."""
    tc.assertIsInstance(msg, str)
    lines = msg.splitlines()
    tc.assertTrue(lines[0].startswith("Error: "), msg)
    tc.assertTrue(lines[1].startswith("Fix: "), msg)
    tc.assertNotIn("Traceback", msg)
    tc.assertNotIn(TOKEN, msg)
    for word in ("cpub", "setup"):
        tc.assertNotIn(word, msg.lower())


class Parser(unittest.TestCase):
    def parse(self, *extra):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            try:
                return cp.build_parser().parse_args([*ARGV, *extra]), None
            except SystemExit as e:
                return None, (e.code, err.getvalue())

    def test_defaults(self):
        args, _ = self.parse()
        self.assertEqual((args.adopt, args.max_updates, args.env_file), (False, 25, None))

    def test_new_flags(self):
        args, _ = self.parse("--adopt", "--max-updates", "40", "--env-file", "x.env")
        self.assertEqual((args.adopt, args.max_updates, args.env_file), (True, 40, "x.env"))

    def test_max_updates_rejects_bad_values(self):
        for bad in ("-1", "abc", "2.5"):
            with self.subTest(value=bad):
                _, failure = self.parse(f"--max-updates={bad}")
                self.assertEqual(failure[0], 2)
                self.assertIn("--max-updates", failure[1])

    def test_no_blanket_yes_flag(self):
        for flag in ("--yes", "-y", "--force"):
            with self.subTest(flag=flag):
                _, failure = self.parse(flag)
                self.assertEqual(failure[0], 2)

    def test_v11_command_line_still_parses(self):
        args, failure = self.parse("--img-width", "700", "--img-dir", "/x", "--dry-run")
        self.assertIsNone(failure)
        self.assertTrue(args.dry_run)

    def test_help_leads_with_folder_example_and_names_the_real_command(self):
        text = cp.build_parser().format_help()
        self.assertIn("example:\n  python3.12 confluence_publish.py docs/", text)
        self.assertLess(text.index("example:"), text.index("options:"))
        self.assertIn("~/.config/confluence-publisher/.env", text)
        for word in ("cpub", "setup", "--yes"):
            self.assertNotIn(word, text)


class Credentials(unittest.TestCase):
    def test_missing_lists_names_and_searched_files(self):
        msg, _ = run(Fake([]), env={"CONFLUENCE_BASE_URL": BASE})
        assert_friendly(self, msg)
        self.assertIn("CONFLUENCE_EMAIL, CONFLUENCE_API_TOKEN", msg)
        self.assertNotIn("CONFLUENCE_BASE_URL,", msg)
        self.assertIn("KEY=VALUE", msg)
        self.assertIn("/home/u/proj/.env", msg)
        self.assertIn("/home/u/.config/confluence-publisher/.env", msg)
        self.assertIn("environment variables", msg)

    def test_values_from_files_are_applied(self):
        fake = Fake([item("p - 01 - A")], resolved={
            k: (v, "/home/u/proj/.env") for k, v in CREDS.items()})
        msg, _ = run(fake, env={})
        self.assertIsNone(msg)
        self.assertIn("build_plan", fake.calls)

    def test_env_file_is_passed_through(self):
        fake = Fake([item("p - 01 - A")])
        run(fake, "--env-file", "custom.env")
        self.assertIn(("load_env", "custom.env"), fake.calls)

    def test_missing_env_file_names_the_path(self):
        fake = Fake([])

        def missing(env_file=None, **k):
            raise FileNotFoundError(2, "No such file", env_file)
        patches = fake.install()
        patches["load_env"] = missing
        fake.install = lambda: patches
        msg, _ = run(fake, "--env-file", "nope/.env")
        assert_friendly(self, msg)
        self.assertIn("nope/.env", msg)
        self.assertNotIn("build_plan", fake.calls)


class Refusals(unittest.TestCase):
    REFUSALS = [
        refusal("p - 01 - A", '"p - 01 - A" already exists outside 100 at https://x/1 — not touching it.',
                "Use a distinct --prefix."),
        refusal("p - 02 - B", '"p - 02 - B" already exists outside 100 at https://x/2 — not touching it.',
                "Use a distinct --prefix."),
    ]

    def test_refused_pages_are_not_offered_for_hand_over(self):
        items = [item("p - 01 - A", managed="none"), item("p - 02 - B", managed="none")]
        refs = [refusal("p - 01 - A", '"p - 01 - A" already exists outside 100.', "Use a distinct --prefix.")]
        refs[0].page_id = "id-p - 01 - A"
        msg, out = run(Fake(items, refusals=refs), "--dry-run")
        self.assertIn("Ownership: 1 not published by this tool, 1 refused", out)
        self.assertIn("1 page needs a one-time hand-over (this tool did not publish it", out)
        self.assertNotIn("  - p - 01 - A  (", out)
        self.assertIn("  - p - 02 - B  (", out)

    def test_real_run_lists_every_refusal_and_never_executes(self):
        fake = Fake([item("p - 01 - A"), item("p - 02 - B")], refusals=self.REFUSALS)
        msg, out = run(fake)
        assert_friendly(self, msg)
        self.assertIn("nothing was written", msg)
        self.assertIn('"p - 01 - A" already exists', msg)
        self.assertIn('"p - 02 - B" already exists', msg)
        self.assertEqual(msg.count("Fix: Use a distinct --prefix."), 2)
        self.assertFalse([c for c in fake.calls if c[0] == "execute_plan"])

    def test_dry_run_shows_plan_and_refusals(self):
        fake = Fake([item("p - 01 - A")], refusals=self.REFUSALS[:1])
        msg, out = run(fake, "--dry-run")
        self.assertIn("UPDATE  0        p - 01 - A", out)
        self.assertIn("already exists outside", msg)

    def test_refusal_raised_inside_execute_plan_is_friendly(self):
        def execute(plan, **k):
            raise cp.PlanRefused(self.REFUSALS)
        msg, _ = run(Fake([item("p - 01 - A")], execute=execute))
        assert_friendly(self, msg)
        self.assertIn("(2 problems)", msg)

    def test_max_updates_is_forwarded(self):
        fake = Fake([item("p - 01 - A")])
        run(fake, "--max-updates", "40")
        self.assertIn(("execute_plan", False, 40, False), fake.calls)


class Handover(unittest.TestCase):
    ITEMS = [item("p - 01 - A", managed="none", version=7),
             item("p - 02 - B", managed="other", version=2), item("p - 03 - C", action="CREATE", managed="new")]

    def test_non_tty_without_adopt_refuses_and_lists_pages(self):
        def execute(plan, **k):
            self.assertIsNone(k["confirm_handover"])
            raise cp.HandoverRequired([i for i in plan.items if i.managed in ("none", "other")])
        msg, _ = run(Fake(self.ITEMS, execute=execute, tty=False))
        assert_friendly(self, msg)
        self.assertIn("2 pages need a one-time hand-over", msg)
        self.assertIn("--adopt", msg)
        self.assertIn("p - 01 - A", msg)
        self.assertIn("version 7", msg)

    def test_adopt_skips_the_prompt_and_says_what_it_took_over(self):
        fake = Fake(self.ITEMS, tty=True)
        msg, out = run(fake, "--adopt")
        self.assertIsNone(msg)
        self.assertIn("Taken over (2):\n  - p - 01 - A", out)
        self.assertIn("  - p - 01 - A  (not published by this tool, version 7", out)
        self.assertLess(out.index("ACTION  DIAGRAMS TITLE"), out.index("Taken over"))
        self.assertIn(("execute_plan", True, 25, False), fake.calls)
        self.assertEqual(fake.prompts, [])

    def test_tty_prompt_wording_and_typed_count(self):
        def execute(plan, **k):
            items = [i for i in plan.items if i.managed in ("none", "other")]
            if not k["confirm_handover"](items):
                raise cp.HandoverDeclined(items)
            return types.SimpleNamespace(written=[], pending=[], marker_failures=[], error=None)

        for answer, ok in (("2", True), (" 2 ", True), ("y", False), ("yes", False),
                           ("3", False), ("", False), (EOFError(), False)):
            with self.subTest(answer=answer):
                fake = Fake(self.ITEMS, execute=execute, tty=True, answer=answer)
                msg, out = run(fake)
                self.assertEqual(fake.prompts, [
                    "Take over and replace 2 page(s) you did not publish? "
                    "Type the number 2 to confirm: "])
                self.assertIn("p - 01 - A  (not published by this tool, version 7, last edited 2026-10-01 10:00 UTC)", out)
                if ok:
                    self.assertIsNone(msg)
                else:
                    assert_friendly(self, msg)
                    self.assertIn("nothing was written", msg)

    def test_dry_run_mentions_handover_without_prompting(self):
        fake = Fake(self.ITEMS, tty=True)
        msg, out = run(fake, "--dry-run")
        self.assertIsNone(msg)
        self.assertEqual(fake.prompts, [])
        self.assertIn("2 pages need a one-time hand-over", out)
        self.assertFalse([c for c in fake.calls if c[0] == "execute_plan"])


class Output(unittest.TestCase):
    ITEMS = [item("p - 01 - A", action="CREATE", managed="new", diagrams=2),
             item("p - In Situ", action="UPDATE"), item("p - Deep", diagrams=1)]

    V11_TABLE = ("\nACTION  DIAGRAMS TITLE\n"
                 "CREATE  2        p - 01 - A\n"
                 "UPDATE  0        p - In Situ\n"
                 "UPDATE  1        p - Deep\n")

    def test_success_prints_v11_table_then_parent_link(self):
        msg, out = run(Fake(self.ITEMS))
        self.assertIsNone(msg)
        self.assertTrue(out.startswith(self.V11_TABLE), out)
        self.assertIn("\n3 pages published — space ENG, parent 100, prefix 'p'\n", out)
        self.assertTrue(out.endswith(f"Parent page: {BASE}/spaces/ENG/pages/100/Parent\n"), out)

    def test_dry_run_table_ownership_and_footer(self):
        fake = Fake(self.ITEMS)
        msg, out = run(fake, "--dry-run")
        self.assertIsNone(msg)
        self.assertTrue(out.startswith(self.V11_TABLE), out)
        self.assertIn("Ownership: 1 new, 2 published by this tool from this source", out)
        self.assertIn("3 pages DRY-RUN (nothing written) — space ENG, parent 100, prefix 'p'", out)
        self.assertNotIn("execute_plan", [c[0] for c in fake.calls if isinstance(c, tuple)])

    def test_marker_failure_is_reported_not_fatal(self):
        def execute(plan, **k):
            return types.SimpleNamespace(written=[(plan.items[0], "1")], pending=[],
                                         marker_failures=[(plan.items[0], "1", OSError("boom"))], error=None)
        msg, out = run(Fake(self.ITEMS, execute=execute))
        self.assertIsNone(msg)
        self.assertIn("Warning: p - 01 - A was written but could not be marked as managed", out)


class FriendlyErrors(unittest.TestCase):
    def check(self, code, *needles):
        e = http_error(code, "Reason", f'{{"message": "raw detail {code}"}}')
        msg, _ = run(Fake([], http_error=e))
        assert_friendly(self, msg)
        self.assertIn(f"raw detail {code}", msg)        # Confluence's own reason kept
        self.assertIn(f"Confluence said: {code} Reason", msg)
        for n in needles:
            self.assertIn(n, msg)
        return msg

    def test_401(self):
        self.check(401, "Confluence did not accept your email/API token", "CONFLUENCE_API_TOKEN")

    def test_403(self):
        self.check(403, "can't edit this page/space")

    def test_404(self):
        self.check(404, "Page 100 was not found or is not visible to you", "not a folder")

    def test_400(self):
        self.check(400, "folder", "--prefix")

    def test_409_page_changed_since_plan_during_run(self):
        items = [item("p - 01 - A"), item("p - 02 - B")]

        def execute(plan, **k):
            raise cp.PlanExecutionError(types.SimpleNamespace(
                written=[(items[0], "1")], pending=items[1:], partial=None, marker_failures=[],
                error=http_error(409, "Conflict", "version mismatch")))
        msg, _ = run(Fake(items, execute=execute))
        assert_friendly(self, msg)
        self.assertTrue(msg.startswith('Error: "p - 02 - B" was edited in Confluence while this '
                                       "run was publishing, so it was not overwritten.\n"
                                       "Fix: Re-run with --dry-run to see its new state, then "
                                       "publish again.\n"), msg)
        self.assertIn("Confluence said: 409 Conflict", msg)
        self.assertIn("version mismatch", msg)
        self.assertIn("Written (1):\n  - p - 01 - A", msg)
        self.assertIn("Not written (1):\n  - p - 02 - B", msg)

    def test_token_is_masked_even_if_echoed_back(self):
        e = http_error(401, "Unauthorized", f"bad token {TOKEN}")
        msg, _ = run(Fake([], http_error=e))
        self.assertIn("bad token ****", msg)

    def with_patch(self, **patches):
        fake = Fake([])
        base = fake.install()
        base.update(patches)
        fake.install = lambda: base
        return fake

    def test_bad_token_is_caught_before_planning_and_names_its_file(self):
        for me in ({"authenticated": False, "status": 200, "type": "anonymous"},
                   {"authenticated": False, "status": 401}):
            with self.subTest(me=me):
                fake = self.with_patch(whoami=lambda me=me: me)
                fake.resolved = {"CONFLUENCE_API_TOKEN": (TOKEN, "/home/u/.config/confluence-publisher/.env")}
                msg, _ = run(fake)
                assert_friendly(self, msg)
                self.assertTrue(msg.startswith("Error: Confluence did not accept your email/API token.\n"
                                               "Fix: Check CONFLUENCE_EMAIL and CONFLUENCE_API_TOKEN in "
                                               "/home/u/.config/confluence-publisher/.env"), msg)
                self.assertNotIn("build_plan", fake.calls)

    def test_bad_token_from_environment(self):
        fake = self.with_patch(whoami=lambda: {"authenticated": False, "status": 403})
        msg, _ = run(fake)
        self.assertIn("in your environment variables", msg)
        self.assertIn("Confluence said: 403", msg)

    def test_authenticated_404_names_the_failing_lookup(self):
        def fail(*a, **k):
            raise http_error(404, "Not Found", "raw read detail")
        msg, _ = run(self.with_patch(resolve_space_id=fail))
        assert_friendly(self, msg)
        self.assertIn("no space with the key 'ENG'", msg)
        self.assertIn("raw read detail", msg)
        self.assertNotIn("--parent", msg)
        msg, _ = run(self.with_patch(build_plan=fail))
        assert_friendly(self, msg)
        self.assertIn("Page 100 was not found or is not visible to you", msg)

    def test_tls(self):
        err = urllib.error.URLError(ssl.SSLCertVerificationError(
            1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"))
        msg, _ = run(Fake([], http_error=err))
        assert_friendly(self, msg)
        self.assertIn("TLS certificate", msg)
        self.assertIn("Install Certificates.command", msg)

    def test_network(self):
        msg, _ = run(Fake([], http_error=urllib.error.URLError("Name or service not known")))
        assert_friendly(self, msg)
        self.assertIn(f"Could not reach Confluence at {BASE}", msg)

    def test_unknown_space(self):
        fake = Fake([])
        patches = fake.install()

        def no_space(key):
            raise SystemExit(f"Space not found for key: {key}")
        patches["resolve_space_id"] = no_space
        fake.install = lambda: patches
        msg, _ = run(fake)
        assert_friendly(self, msg)
        self.assertIn("'ENG'", msg)

    def test_not_a_folder(self):
        saved = list(ARGV)
        ARGV[0] = "no/such/dir/for/tests"
        try:
            msg, _ = run(Fake([]))
        finally:
            ARGV[:] = saved
        assert_friendly(self, msg)
        self.assertIn("Not a folder: no/such/dir/for/tests", msg)

    def test_partial_write_lists_written_and_not_written(self):
        items = [item("p - 01 - A"), item("p - 02 - B"), item("p - 03 - C")]

        def execute(plan, **k):
            report = types.SimpleNamespace(
                written=[(items[0], "1")], pending=items[1:], marker_failures=[],
                error=urllib.error.URLError("Connection reset"))
            raise cp.PlanExecutionError(report)
        msg, _ = run(Fake(items, execute=execute))
        assert_friendly(self, msg)
        self.assertIn("safe to re-run", msg)
        self.assertIn("Written (1):\n  - p - 01 - A", msg)
        self.assertIn("Not written (2):\n  - p - 02 - B\n  - p - 03 - C", msg)


class EndToEnd(unittest.TestCase):
    """main() over the real planner and the in-memory Confluence: E0 seen from the CLI."""

    DOCS = ([("01 - Intro", "# Hi\n"), ("02 - Arch", "text\n")], [("Deep", "x\n")])

    def setUp(self):
        from _fake_confluence import FakeConfluence
        self.f = FakeConfluence()
        self.f.add("10", "TEST")
        self.f.add("5", "Other team")

    def main(self, *extra, tty=False, answer=None, on_ask=None):
        from _fake_confluence import installed
        prompts, out = [], io.StringIO()
        patches = {"resolve_space_id": lambda k: "S1",
                   "load_env": lambda *a, **k: {}, "_stdin_is_tty": lambda: tty,
                   "_ask": lambda p: prompts.append(p) or (on_ask and on_ask()) or answer}
        saved = {n: getattr(cp, n) for n in patches}
        saved_read = plan.read_doc_set                    # looked up by plan.build_plan
        try:
            plan.read_doc_set = lambda d: self.DOCS
            for n, fn in patches.items():
                setattr(cp, n, fn)
            with environ(CREDS), installed(self.f):
                try:
                    cp.main([DOCS, "--space", "ENG", "--parent", "10", "--prefix", "p", *extra], out=out)
                    return None, out.getvalue(), prompts
                except SystemExit as e:
                    return e.code, out.getvalue(), prompts
        finally:
            plan.read_doc_set = saved_read
            for n, fn in saved.items():
                setattr(cp, n, fn)

    def test_sibling_tree_match_refused_with_zero_writes(self):
        self.f.add("12", "p - 02 - Arch", parent="5")
        for extra in ((), ("--dry-run",), ("--adopt",)):
            with self.subTest(extra=extra):
                msg, _, _ = self.main(*extra)
                assert_friendly(self, msg)
                self.assertIn('"p - 02 - Arch" already exists outside', msg)
                self.assertEqual(self.f.writes, [])

    def test_v11_pages_need_one_hand_over_then_run_silently(self):
        self.f.add("11", "p - 01 - Intro", parent="10")          # published by v1.1: no marker
        msg, _, _ = self.main()                                   # non-interactive, no --adopt
        self.assertIn("1 page needs a one-time hand-over", msg)
        self.assertEqual(self.f.writes, [])
        msg, _, prompts = self.main(tty=True, answer="2")         # wrong count
        self.assertIn("Hand-over not confirmed", msg)
        self.assertEqual(self.f.writes, [])
        msg, out, prompts = self.main(tty=True, answer="1")
        self.assertIsNone(msg)
        self.assertEqual(len(prompts), 1)
        self.assertIn("Parent page: https://example.invalid/wiki/spaces/X/pages/10", out)
        msg, out, prompts = self.main(tty=True)                   # now managed: no prompt
        self.assertIsNone(msg)
        self.assertEqual(prompts, [])
        self.assertIn("UPDATE  0        p - 01 - Intro", out)

    def test_page_edited_during_the_prompt_is_not_overwritten(self):
        self.f.add("11", "p - 01 - Intro", parent="10", version=3)   # v1.1 page: needs hand-over

        def colleague_saves():
            self.f.pages["11"]["version"] = 4                      # while the prompt is open
        msg, _, prompts = self.main(tty=True, answer="1", on_ask=colleague_saves)
        self.assertEqual(len(prompts), 1)
        assert_friendly(self, msg)
        self.assertIn('"p - 01 - Intro" was edited in Confluence while this run was publishing', msg)
        self.assertIn("Fix: Re-run with --dry-run to see its new state, then publish again.", msg)
        self.assertIn("409", msg)
        self.assertIn("Written (0):", msg)
        self.assertIn("Not written (4):\n  - p - 01 - Intro", msg)
        self.assertEqual(self.f.pages["11"]["body"], "<p>old</p>")  # not overwritten

    def test_identity_checked_first_against_fake_confluence(self):
        for user in ({"type": "anonymous"}, 401):
            with self.subTest(user=user):
                self.f.user = user
                self.f.calls.clear()
                msg, _, _ = self.main("--dry-run")
                assert_friendly(self, msg)
                self.assertIn("Confluence did not accept your email/API token", msg)
                self.assertEqual(self.f.calls, [("GET", "/rest/api/user/current")])

    def test_authenticated_missing_parent_is_a_page_not_found(self):
        del self.f.pages["10"]
        msg, _, _ = self.main("--dry-run")
        assert_friendly(self, msg)
        self.assertIn("Page 10 was not found or is not visible to you", msg)
        self.assertEqual(self.f.writes, [])

    def test_dry_run_writes_nothing(self):
        self.f.add("11", "p - 01 - Intro", parent="10")
        msg, out, prompts = self.main("--dry-run", tty=True)
        self.assertIsNone(msg)
        self.assertEqual((self.f.writes, prompts), ([], []))
        self.assertIn("DRY-RUN (nothing written)", out)

    def test_write_failure_mid_run_reports_and_rerun_finishes(self):
        self.f.fail_on = ("POST", "/api/v2/pages")
        self.f.add("11", "p - 01 - Intro", parent="10",
                   marker={"source": "p - 01 - Intro", "tool": "obx-confluence-obsidian-publisher",
                           "since": "2026-10-01"})
        msg, _, _ = self.main()
        assert_friendly(self, msg)
        self.assertIn("Written (1):\n  - p - 01 - Intro", msg)
        self.assertIn("Not written (3):", msg)
        self.assertIn("safe to re-run", msg)
        self.f.fail_on = None
        msg, out, _ = self.main()
        self.assertIsNone(msg, msg)
        self.assertIn("4 pages published", out)


if __name__ == "__main__":
    unittest.main()
