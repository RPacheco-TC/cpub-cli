"""``cpub undo`` from the command line (S0.5, AC-0.5.2-0.5.4, D-2..D-6), in-process.

A real publish writes to an in-memory audit log, then ``undo`` runs the real
:class:`~confluence_publisher.undo.UndoPlanner` and
:class:`~confluence_publisher.executor.PlanExecutor` over ``FakeConfluence``,
so the request log proves what was restored and that nothing was deleted.
No terminal, no file, no network. Run from the repo root::

    python3.12 -B -m unittest discover -s tests
"""
import io
import json
import types
import unittest

from _support import cli, config, docset, plan, storage
from _fake_confluence import FakeConfluence, BASE
from _fake_log import memory_log

TOKEN = "s3cr3t-token-value"
CREDS = config.Credentials(BASE, "x@example.invalid", TOKEN)
NOTE_MD = "---\ntitle: Fancy Title\n---\n# Hi\n\nnew body\n"


class Tty:
    def __init__(self, tty):
        self.tty = tty

    def isatty(self):
        return self.tty


class Loader:
    def __init__(self, creds=CREDS):
        self.creds = creds

    def load(self, env_file=None):
        if self.creds is None:
            raise AssertionError("credentials must not be read")
        return self.creds


class Undo(unittest.TestCase):
    def setUp(self):
        self.f = FakeConfluence()
        self.f.add("1", "Home")
        self.f.add("10", "TEST", parent="1")
        self.f.add("20", "Hand written", parent="10", version=7)
        self.log = memory_log()

    def publish(self, *argv):
        out = io.StringIO()
        args = cli.build_parser("cpub").parse_args(["My Note.md", *argv])
        cli.PublishCommand.from_args(
            args, out, command="cpub", credential_loader=Loader(),
            client_factory=self.f.client,
            mode_resolver=cli.ModeResolver(is_file=lambda p: True, is_dir=lambda p: False,
                                           targets_of=lambda p: {}),
            note_loader=lambda path, title: docset.SingleNote(path, title=title,
                                                              markdown=NOTE_MD),
            planner_factory=lambda client, a: plan.Planner(
                client, storage.StorageConverter(a.prefix, None, None)),
            app_dirs=types.SimpleNamespace(note_img_dir="/cache/img"),
            prompt=cli.HandoverPrompt(out, ask=lambda p: None, stdin=Tty(False)),
            run_log=self.log).run()
        return out.getvalue()

    def undo(self, *argv, answer=None, tty=True, creds=CREDS):
        """Return (exit message or None, output, prompts)."""
        out, prompts = io.StringIO(), []

        def ask(prompt):
            prompts.append(prompt)
            return answer

        args = cli.subcommand_parser("undo", "cpub").parse_args(list(argv))
        cmd = cli.build_subcommand("undo", args, out, "cpub", credential_loader=Loader(creds),
                                   client_factory=self.f.client, run_log=self.log,
                                   ask=ask, stdin=Tty(tty))
        try:
            cmd.run()
            msg = None
        except SystemExit as e:
            msg = str(e.code)
        self.assertNotIn(TOKEN, out.getvalue() + str(msg))
        return msg, out.getvalue(), prompts

    def restores(self):
        return [c for c in self.f.writes if c[1].endswith("/version")]

    def run_ids(self):
        return [r.run_id for r in self.log.runs()]


class RestoreTheLastRun(Undo):
    def test_undo_restores_the_version_before_the_run_and_marks_it_undone(self):
        self.publish("--to", "20", "--adopt")
        self.assertEqual(self.f.pages["20"]["version"], 8)
        msg, out, prompts = self.undo(answer="1")
        self.assertIsNone(msg, msg)
        self.assertIn("Restore (1):\n  - Hand written  version 8 -> back to 7\n", out)
        self.assertEqual(prompts, ["Restore 1 page to the version before the run? "
                                   "Type the number 1 to confirm: "])
        self.assertEqual(len(self.restores()), 1)
        self.assertEqual(self.f.pages["20"]["version"], 9)     # a new version: history kept
        self.assertIn("1 page restored.\nRun ", out)
        self.assertIn("is undone.", out)
        kinds = [json.loads(x)["kind"] for x in self.log.files.text.splitlines()]
        self.assertEqual(kinds, ["publish", "undo", "undo"])  # restore + closing line

    def test_the_same_run_cannot_be_undone_twice(self):
        self.publish("--to", "20", "--adopt")
        self.undo(answer="1")
        published = self.log.runs()[-1].run_id
        msg, _, _ = self.undo(answer="1")
        self.assertEqual(msg.splitlines()[0], "Error: There is no publish run to undo.")
        msg, _, _ = self.undo("--run", published, answer="1")
        self.assertEqual(msg.splitlines()[0], f"Error: Run {published} was already undone.")
        self.assertEqual(len(self.restores()), 1)

    def test_declined_changes_nothing(self):
        self.publish("--to", "20", "--adopt")
        msg, _, _ = self.undo(answer="yes")
        self.assertEqual(msg.splitlines()[:2], [
            "Error: Undo not confirmed; nothing was changed.",
            "Fix: Run cpub undo again and type the page count to restore the pages."])
        self.assertEqual(self.restores(), [])


class Refused(Undo):
    def test_a_page_edited_since_the_run_is_left_alone_with_its_history_link(self):
        self.publish("--to", "20", "--adopt")
        self.f.pages["20"]["version"] += 1                   # someone edited it
        msg, out, prompts = self.undo(answer="1")
        self.assertIsNone(msg, msg)
        self.assertIn("Left as they are (1):\n  - Hand written  edited since the run "
                      "(now version 9, the run wrote 8)\n", out)
        self.assertIn("History: ", out)
        self.assertIn("Nothing to restore; nothing was changed.", out)
        self.assertEqual((prompts, self.restores()), ([], []))

    def test_created_pages_are_listed_never_deleted(self):
        self.publish("--under", "10")
        msg, out, _ = self.undo(answer="1")
        self.assertIsNone(msg, msg)
        self.assertIn("Created by the run (1), not deleted (undo never deletes", out)
        self.assertIn("  - Fancy Title  (page id ", out)
        self.assertFalse([c for c in self.f.writes if c[0] == "DELETE"])
        self.assertEqual(self.restores(), [])

    def test_not_a_terminal_is_refused_before_credentials(self):
        self.publish("--to", "20", "--adopt")
        msg, _, _ = self.undo(tty=False, creds=None)
        self.assertEqual(msg.splitlines()[:2], [
            "Error: cpub undo asks you to confirm, so it needs a terminal.",
            "Fix: Run it in a terminal; nothing was changed."])
        self.assertEqual(self.restores(), [])

    def test_a_run_logged_against_another_site_is_refused_before_any_request(self):
        self.publish("--to", "20", "--adopt")
        other = config.Credentials("https://other.example.invalid/wiki", "x@e.invalid", TOKEN)
        calls = len(self.f.calls)
        msg, _, _ = self.undo(answer="1", creds=other)
        self.assertIn("was published to example.invalid, but your credentials are for "
                      "other.example.invalid.", msg)
        self.assertEqual(len(self.f.calls), calls)

    def test_a_bad_token_stops_before_any_page_is_read(self):
        self.publish("--to", "20", "--adopt")
        self.f.user = {"type": "anonymous"}
        calls = len(self.f.calls)
        msg, _, _ = self.undo(answer="1")
        self.assertIn("Error: Confluence did not accept your email/API token.", msg)
        self.assertEqual([c for c in self.f.calls[calls:] if "/pages/" in c[1]], [])

    def test_unknown_run_and_empty_log(self):
        msg, _, _ = self.undo(answer="1")
        self.assertIn("There is no publish run to undo.", msg)
        msg, _, _ = self.undo("--run", "nope", answer="1")
        self.assertIn("There is no publish run nope in the log.", msg)


class ListRuns(Undo):
    def test_list_reads_the_log_only(self):
        _, out, _ = self.undo("--list", tty=False, creds=None)
        self.assertIn("No runs logged yet (", out)
        self.publish("--to", "20", "--adopt")
        self.undo(answer="1")
        calls = len(self.f.calls)
        msg, out, _ = self.undo("--list", tty=False, creds=None)
        self.assertIsNone(msg, msg)
        lines = out.splitlines()
        self.assertEqual(lines[0], "Last runs, newest first:")
        self.assertIn("undo of ", lines[1])
        self.assertIn("1 page restored", lines[1])
        self.assertIn("publish: 1 updated, 0 created  (undone)", lines[2])
        self.assertEqual(len(self.f.calls), calls)


if __name__ == "__main__":
    unittest.main()
