"""Command line for the Confluence publisher: the package's one composition root.

Publishes an Obsidian-style Markdown doc set as a page tree under one parent
page. Every run is planned and checked before the first write; it only updates
pages strictly *below* the parent you pass that it published itself
(``cpub-managed`` content property) or that you hand over in this run, caps
updates per run (``--max-updates``), and never deletes or moves anything.

Usage
-----
.. code-block:: console

    python3.12 confluence_publish.py DOCS_DIR --space KEY --parent PAGE_ID [--prefix P] [--dry-run]

``DOCS_DIR``
    folder of ``NN - Title.md`` files, with an optional ``In Situ/`` subfolder.
``--space KEY``
    Confluence space key (``~yourname`` for a personal space, or e.g. ``ENG``).
``--parent PAGE_ID``
    id of the page to publish everything under.
``--dry-run``
    print the plan (CREATE/UPDATE, ownership, refusals) and write nothing.
``--adopt``
    hand over the listed pages without the typed confirmation (non-interactive runs).
``--max-updates N``
    refuse a run that would update more than N existing pages (default 25).
``--env-file PATH``
    read credentials from this file.

Credentials
-----------
Each variable is taken from the first of: the environment, ``--env-file``,
``./.env``, ``$XDG_CONFIG_HOME/confluence-publisher/.env`` (default
``~/.config/…``). Files hold ``KEY=VALUE`` lines; never commit them:

``CONFLUENCE_BASE_URL``
    e.g. ``https://your-company.atlassian.net/wiki``
``CONFLUENCE_EMAIL``
    the Atlassian account email.
``CONFLUENCE_API_TOKEN``
    an API token.

Design
------
This module is the **only** place that constructs the collaborators
(credential loader, Confluence client, converter, planner, executor) and wires
them together. :class:`PublishCommand` receives each of them through its
constructor, so tests pass fakes instead of patching module attributes.

.. note::

   Not yet implemented (contributions welcome): a ``state.json`` page map (this
   matches by title), content-hash skip of unchanged pages, exact heading-anchor
   links, and ``--prune`` of pages whose source was removed.
"""
import sys
import ssl
import pathlib
import argparse
import datetime
import urllib.error

from .errors import (CliError, MissingCredentials, SpaceNotFound, NotAFolder, PlanRefused,
                     HandoverRequired, HandoverDeclined, PlanExecutionError)
from .config import CredentialLoader
from .api import ConfluenceClient, HttpTransport
from .storage import StorageConverter
from .docset import DocSet
from .plan import Planner, DEFAULT_MAX_UPDATES
from .executor import PlanExecutor


CMD = "python3.12 confluence_publish.py"
TOKEN_URL = "https://id.atlassian.com/manage-profile/security/api-tokens"

_MANAGED_LABEL = {
    "new": "new",
    "same": "published by this tool from this source",
    "other": "published by this tool from another source",
    "none": "not published by this tool",
}

_HELP_INTRO = f"""\
Publish a folder of Markdown docs to Confluence Cloud under one parent page.

example:
  {CMD} docs/ --space ENG --parent 123456789 --prefix myrepo --dry-run
  {CMD} docs/ --space ENG --parent 123456789 --prefix myrepo

Safety: every run is planned and checked before anything is written. It only
updates pages below --parent that it published itself; any other page needs a
one-time hand-over (type the page count when asked, or --adopt in scripts).
Nothing is ever deleted or moved.
"""

_HELP_CREDS = """\
credentials (first hit wins, per variable):
  1. environment variables CONFLUENCE_BASE_URL, CONFLUENCE_EMAIL, CONFLUENCE_API_TOKEN
  2. --env-file PATH
  3. ./.env in the current folder
  4. ~/.config/confluence-publisher/.env  ($XDG_CONFIG_HOME is honoured)
  Files use KEY=VALUE lines, as in .env.example. Keep them chmod 600.
"""


# --- tiny stateless helpers ----------------------------------------------------

def _plural(n, word):
    """``"1 page"`` / ``"2 pages"``."""
    return f"{n} {word}" + ("" if n == 1 else "s")


def _need(n):
    """``"1 page needs"`` / ``"2 pages need"``."""
    return f"{_plural(n, 'page')} need" + ("s" if n == 1 else "")


def _when(iso):
    """Shorten a Confluence ISO timestamp to ``YYYY-MM-DD HH:MM UTC`` (best effort)."""
    try:
        t = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return t.astimezone(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, AttributeError):
        return str(iso)


def _int_at_least(minimum, message):
    """Return an argparse ``type=`` that accepts integers >= ``minimum``."""
    def parse(value):
        try:
            n = int(value)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{message}, got {value!r}") from None
        if n < minimum:
            raise argparse.ArgumentTypeError(f"{message}, got {value!r}")
        return n
    return parse


# --- argument parsing -------------------------------------------------------------

class ArgumentParserFactory:
    """Builds the ``argparse`` parser: every flag, its help text and validation."""

    def build(self):
        """:rtype: argparse.ArgumentParser"""
        ap = argparse.ArgumentParser(
            description=_HELP_INTRO, epilog=_HELP_CREDS,
            formatter_class=argparse.RawDescriptionHelpFormatter)
        ap.add_argument("docs_dir",
                        help='folder of "NN - Title.md" docs (optional "In Situ/" subfolder)')
        ap.add_argument("--space", required=True,
                        help="Confluence space KEY (e.g. ~yourname or ENG)")
        ap.add_argument("--parent", required=True, help="ID of the parent page to publish under")
        ap.add_argument("--img-dir", default=None,
                        help="where to render Mermaid PNGs (default: <docs_dir>/.img)")
        ap.add_argument("--prefix", default="",
                        help="unique identifier prepended to every page title "
                             '(e.g. "langflow" -> "langflow - 01 - Overview"); use a '
                             "distinct value per repo so docs sharing a space never collide")
        ap.add_argument("--img-width", type=_int_at_least(1, "must be a positive integer"),
                        default=None, metavar="PX",
                        help="display width in pixels for every Mermaid diagram "
                             "(default: unset, natural size)")
        ap.add_argument("--dry-run", action="store_true",
                        help="print the plan (CREATE/UPDATE, ownership, refusals) "
                             "and write nothing")
        ap.add_argument("--adopt", action="store_true",
                        help="take over the pages listed for hand-over without the typed "
                             "confirmation (for scripts; this run only)")
        ap.add_argument("--max-updates", type=_int_at_least(0, "must be a whole number >= 0"),
                        default=DEFAULT_MAX_UPDATES, metavar="N",
                        help="refuse a run that would update more than N existing pages "
                             "(default: 25; creates don't count)")
        ap.add_argument("--env-file", default=None, metavar="PATH",
                        help="read credentials from this .env file (see 'credentials' below)")
        return ap


# --- output -------------------------------------------------------------------------

class PlanPrinter:
    """Everything a run prints on success or dry-run (not errors).

    The v1.1 ``ACTION DIAGRAMS TITLE`` table is kept byte-identical; the
    ownership summary, hand-over list, footer and parent link go around it.

    :param out: the stream to print to.
    """

    def __init__(self, out):
        self.out = out

    def _print(self, *lines):
        for line in lines:
            print(line, file=self.out)

    @staticmethod
    def describe(item):
        """One line describing an existing page (hand-over lists and prompts)."""
        bits = [_MANAGED_LABEL.get(item.managed, item.managed)]
        if item.version is not None:
            bits.append(f"version {item.version}")
        if item.last_edited:
            bits.append(f"last edited {_when(item.last_edited)}")
        return f"  - {item.title}  ({', '.join(bits)})"

    def table(self, items):
        self._print(f"\n{'ACTION':7} {'DIAGRAMS':8} TITLE")
        for it in items:
            self._print(f"{it.action:7} {it.diagrams:<8} {it.title}")

    def ownership(self, plan, refusals=(), adopted=False):
        """Summarise who owns each planned page, then list the hand-over pages.

        Pages that a refusal already covers count as refused and are left out
        of the hand-over list: no hand-over can make them writable.

        :param adopted: the pages were taken over with ``--adopt`` (wording only).
        """
        refused = {r.page_id for r in refusals if getattr(r, "page_id", None)}
        counts = {}
        for it in plan.items:
            key = "refused" if it.page_id and it.page_id in refused else it.managed
            counts[key] = counts.get(key, 0) + 1
        labels = {**_MANAGED_LABEL, "refused": "refused"}
        parts = [f"{counts[k]} {labels[k]}" for k in ("new", "same", "other", "none", "refused")
                 if counts.get(k)]
        self._print("\nOwnership: " + ", ".join(parts))
        handover = [it for it in plan.handover_items() if it.page_id not in refused]
        if handover:
            n = len(handover)
            head = (f"Taken over ({n}):" if adopted else
                    f"{_need(n)} a one-time hand-over (this tool did not publish "
                    f"{'it' if n == 1 else 'them'} from this source):")
            self._print("\n" + head, *(self.describe(it) for it in handover))

    def handover_hint(self):
        self._print("\nThe real run will ask you to type the page count to hand them over "
                    "(or pass --adopt when not in a terminal).")

    def marker_warnings(self, report):
        for it, pid, exc in report.marker_failures:
            self._print(f"\nWarning: {it.title} was written but could not be marked as managed "
                        f"({exc}); the next run will ask to hand it over once.")

    def footer(self, count, done, args, parent_link):
        """``N pages <done> — space …`` then the clickable parent link."""
        prefix_note = f", prefix {args.prefix!r}" if args.prefix else ""
        self._print(f"\n{count} pages {done} — space {args.space}, "
                    f"parent {args.parent}{prefix_note}",
                    f"Parent page: {parent_link}")


# --- hand-over prompt (AC-0.3.3) ----------------------------------------------------

class HandoverPrompt:
    """Interactive hand-over: list the pages, ask for the typed page count.

    There is deliberately no y/N and no ``--yes`` (D4): typing the number makes
    the size of what is being replaced impossible to miss.

    :param out: stream for the listing.
    :param ask: ``prompt -> answer`` (default :func:`input`).
    :param stdin: checked with ``isatty()`` to decide whether asking is possible.
    """

    def __init__(self, out, ask=input, stdin=None):
        self.out, self.ask = out, ask
        self.stdin = sys.stdin if stdin is None else stdin

    def is_interactive(self):
        try:
            return bool(self.stdin.isatty())
        except (AttributeError, ValueError):
            return False

    def confirm(self, items):
        """:returns: ``True`` only when the user typed exactly ``len(items)``."""
        n = len(items)
        print(f"\nThese {n} page(s) exist under the parent but were not published by this "
              "tool from this source:", file=self.out)
        for it in items:
            print(PlanPrinter.describe(it), file=self.out)
        try:
            answer = self.ask(f"Take over and replace {n} page(s) you did not publish? "
                              f"Type the number {n} to confirm: ")
        except (EOFError, KeyboardInterrupt):
            print(file=self.out)
            return False
        return answer.strip() == str(n)


# --- friendly errors (S3.1) -----------------------------------------------------------

class ErrorTranslator:
    """Turns every expected failure into a :class:`CliError` (sentence + Fix + detail).

    :param args: the parsed command line (``space``, ``parent``).
    :param credentials: the resolved :class:`~.config.Credentials`, once known
        (base URL and token source for messages, token for masking).
    """

    def __init__(self, args, credentials=None):
        self.args = args
        self.credentials = credentials

    # credentials and local input
    @staticmethod
    def env_file_missing(path):
        return CliError(f"The --env-file {path} does not exist.",
                        "Pass the path of an existing .env file, or drop --env-file to use "
                        "./.env or ~/.config/confluence-publisher/.env.")

    @staticmethod
    def missing_credentials(missing, searched):
        searched = ["environment variables"] + [str(p) for p in searched]
        return CliError(
            "Missing Confluence credential(s): " + ", ".join(missing) + ".",
            "Add them as KEY=VALUE lines (see .env.example) to ./.env or "
            "~/.config/confluence-publisher/.env, or export them.",
            "Searched, in order:\n" + "\n".join(f"- {s}" for s in searched))

    def not_a_folder(self):
        return CliError(f"Not a folder: {self.args.docs_dir}.",
                        'Pass the folder that holds the "NN - Title.md" docs.')

    @staticmethod
    def duplicate_titles(dupes):
        return CliError(
            "Two or more files in this doc set resolve to the same page title, and "
            "Confluence requires titles to be unique within a space.",
            "Rename the colliding file(s) so every page has a distinct title.",
            "\n".join(f"- {t!r}" for t in dupes))

    # identity and reads
    def bad_token(self, raw=None):
        src = (self.credentials.token_source if self.credentials else None) or "environment"
        where = "your environment variables" if src == "environment" else src
        return CliError("Confluence did not accept your email/API token.",
                        f"Check CONFLUENCE_EMAIL and CONFLUENCE_API_TOKEN in {where} "
                        f"(create a new token at {TOKEN_URL}).", raw)

    def unauthenticated(self, me):
        how = (f"Confluence said: {me['status']}" if me.get("status") not in (None, 200)
               else "Confluence treated the request as anonymous.")
        return self.bad_token(how)

    def no_space(self, raw=None):
        return CliError(f"Confluence has no space with the key {self.args.space!r} "
                        "that you can see.",
                        "Check --space: it is the part after /spaces/ in a page URL "
                        "(~yourname for a personal space).", raw)

    def read(self, e, stage):
        """An ``HTTPError`` from the read-only phase, naming what failed.

        :param stage: ``"space"`` (space-key lookup) or ``"parent"`` (planning).
        """
        if e.code == 404 and stage == "space":
            return self.no_space(self.http(e).detail)
        return self.http(e)

    def http(self, e):
        a = self.args
        raw = (f"Confluence said: {e.code} {e.reason}\n"
               f"{getattr(e, 'detail', '') or '(no detail returned)'}")
        if e.code == 401:
            return self.bad_token(raw)
        if e.code == 403:
            return CliError(f"Your account can't edit this page/space ({a.space}).",
                            "Ask a space admin for edit permission, or publish under a page "
                            "you can edit.",
                            raw)
        if e.code == 404:
            return CliError(f"Page {a.parent} was not found or is not visible to you.",
                            f"Check --parent: it must be the id of a page (not a folder) in space "
                            f"{a.space}.", raw)
        if e.code == 409:
            return CliError("A page was edited in Confluence after this run was planned, so it "
                            "was not overwritten.",
                            "Check the latest version in Confluence, then run the same command "
                            "again to re-plan.", raw)
        if e.code == 400:
            return CliError("Confluence refused the request as invalid.",
                            f"Usually --parent {a.parent} is a folder (use a page) or a page "
                            f"title already exists in space {a.space} (use a distinct --prefix).",
                            raw)
        return CliError(f"Confluence returned an unexpected error ({e.code}).",
                        "Try again; if it persists, check the detail below.", raw)

    def url(self, e):
        """A network or TLS ``URLError``."""
        base = self.credentials.base_url if self.credentials else "(unset)"
        reason = getattr(e, "reason", e)
        tls = (isinstance(reason, ssl.SSLCertVerificationError)
               or "CERTIFICATE_VERIFY_FAILED" in str(reason))
        if tls:
            return CliError("Python could not verify Confluence's TLS certificate.",
                            "If you use the python.org build on macOS, run "
                            "'/Applications/Python 3.12/Install Certificates.command' once "
                            "(never disable verification). See docs/KNOWN_ISSUES.md.", str(reason))
        return CliError(f"Could not reach Confluence at {base}.",
                        "Check CONFLUENCE_BASE_URL (it ends in /wiki) and your network or VPN.",
                        str(reason))

    # plan and execution
    @staticmethod
    def refused(refusals):
        lines = []
        for r in refusals:
            lines.append(f"- {r.message}")
            if r.fix:
                lines.append(f"  Fix: {r.fix}")
        return CliError(
            f"The plan was refused ({_plural(len(refusals), 'problem')}) — nothing was written.",
            "Resolve every item below, then run the same command again (try --dry-run first).",
            "\n".join(lines))

    @staticmethod
    def handover_required(items):
        n = len(items)
        return CliError(
            f"{_need(n)} a one-time hand-over and this is not an interactive "
            "terminal — nothing was written.",
            f"Run it in a terminal and type {n} when asked, or add --adopt to take them over.",
            "\n".join(PlanPrinter.describe(it).strip() for it in items))

    @staticmethod
    def handover_declined(items):
        return CliError("Hand-over not confirmed — nothing was written.",
                        f"Run again and type {len(items)} to take the page(s) over, or use a "
                        "distinct --prefix to publish new pages instead.")

    def partial(self, report):
        """AC-0.1.4: a write failed mid-run — say what was and wasn't written."""
        cause = report.error
        if isinstance(cause, urllib.error.HTTPError):
            why = self.http(cause)
        elif isinstance(cause, urllib.error.URLError):
            why = self.url(cause)
        else:
            why = CliError(str(cause), "")
        partial = getattr(report, "partial", None)
        written = "\n".join(f"- {it.title}" for it, _ in report.written) or "- (none)"
        pending = "\n".join(
            f"- {it.title}" + ("  (created, but its diagrams/body did not finish)"
                               if partial and partial[0] is it else "")
            for it in report.pending) or "- (none)"
        failed = partial[0] if partial else (report.pending[0] if report.pending else None)
        if isinstance(cause, urllib.error.HTTPError) and cause.code == 409 and failed is not None:
            # The update carried the plan-time version: someone saved the page meanwhile.
            return CliError(
                f'"{failed.title}" was edited in Confluence while this run was publishing, '
                "so it was not overwritten.",
                "Re-run with --dry-run to see its new state, then publish again.",
                f"{why.detail}\n\nWritten ({len(report.written)}):\n{written}\n"
                f"Not written ({len(report.pending)}):\n{pending}")
        detail = (f"{why.sentence}\n{why.detail or ''}\n\n"
                  f"Written ({len(report.written)}):\n{written}\n"
                  f"Not written ({len(report.pending)}):\n{pending}").replace("\n\n\n", "\n\n")
        if why.fix and "command again" in why.fix:
            fix = why.fix + " It is safe to re-run."
        else:
            rerun = "Then run the same command again; it is safe to re-run."
            fix = (why.fix + " " if why.fix else "") + rerun
        return CliError("The run stopped because a write failed — some pages were written, "
                        "the rest were not.", fix, detail)

    def mask(self, text):
        """Replace the API token, if it appears, with ``****`` (AC-X.6)."""
        return self.credentials.mask(text) if self.credentials else text


# --- the command ------------------------------------------------------------------------

class PublishCommand:
    """One publish run: credentials → identity → doc set → plan → check → write.

    Every collaborator is injected; :meth:`from_args` is the composition root
    that builds the real ones. Tests pass fakes to the constructor instead.

    :param args: the parsed command line.
    :param out: stream for normal output.
    :param credential_loader: ``.load(env_file) -> Credentials`` (a
        :class:`~.config.CredentialLoader`).
    :param client_factory: ``credentials -> ConfluenceClient``.
    :param docset_loader: ``path -> DocSet``; raises :class:`~.errors.NotAFolder`.
    :param planner_factory: ``(client, args) -> Planner``.
    :param executor_factory: ``client -> PlanExecutor``.
    :param prompt: a :class:`HandoverPrompt`.
    """

    def __init__(self, args, *, out, credential_loader, client_factory, docset_loader,
                 planner_factory, executor_factory, prompt):
        self.args, self.out = args, out
        self.credential_loader = credential_loader
        self.client_factory = client_factory
        self.docset_loader = docset_loader
        self.planner_factory = planner_factory
        self.executor_factory = executor_factory
        self.prompt = prompt
        self.printer = PlanPrinter(out)
        self.errors = ErrorTranslator(args)

    @classmethod
    def from_args(cls, args, out=None, **overrides):
        """Composition root: wire the real collaborators.

        :param overrides: replace any constructor collaborator by name (tests
            pass fakes this way; nothing is patched).
        """
        out = out or sys.stdout
        wiring = dict(
            credential_loader=CredentialLoader(),
            client_factory=lambda creds: ConfluenceClient(creds, HttpTransport()),
            docset_loader=DocSet.from_folder,
            planner_factory=cls._real_planner,
            executor_factory=PlanExecutor,
            prompt=HandoverPrompt(out),
        )
        wiring.update(overrides)
        return cls(args, out=out, **wiring)

    @staticmethod
    def _real_planner(client, args):
        img_dir = args.img_dir or str(pathlib.Path(args.docs_dir) / ".img")
        return Planner(client, StorageConverter(args.prefix, img_dir, args.img_width))

    def run(self):
        """Run once; expected failures exit with one sentence and a ``Fix:`` line.

        :raises SystemExit: on any expected failure (exit status 1).
        """
        try:
            self._run()
        except CliError as e:
            raise SystemExit(self.errors.mask(str(e))) from None
        except urllib.error.HTTPError as e:
            raise SystemExit(self.errors.mask(str(self.errors.http(e)))) from None
        except urllib.error.URLError as e:
            raise SystemExit(self.errors.mask(str(self.errors.url(e)))) from None

    # -- steps --

    def _credentials(self):
        a = self.args
        try:
            credentials = self.credential_loader.load(a.env_file)
        except FileNotFoundError as e:
            raise ErrorTranslator.env_file_missing(e.filename or a.env_file) from None
        except MissingCredentials as e:
            raise ErrorTranslator.missing_credentials(e.missing, e.searched) from None
        self.errors = ErrorTranslator(a, credentials)
        return credentials

    def _docset(self):
        try:
            docs = self.docset_loader(self.args.docs_dir)
        except NotAFolder:
            raise self.errors.not_a_folder() from None
        dupes = docs.duplicate_titles()
        if dupes:
            raise ErrorTranslator.duplicate_titles(dupes)
        return docs

    def _plan(self, client, docs):
        a, stage = self.args, "space"
        try:
            me = client.whoami()
            if not me.get("authenticated"):
                raise self.errors.unauthenticated(me)
            try:
                space_id = client.resolve_space_id(a.space)
            except SpaceNotFound:
                raise self.errors.no_space() from None
            stage = "parent"
            return self.planner_factory(client, a).build(docs, space_id, a.parent)
        except urllib.error.HTTPError as e:
            raise self.errors.read(e, stage) from None

    def _parent_link(self, plan, credentials):
        webui = (plan.parent_info or {}).get("webui")
        if webui:
            return webui if webui.startswith("http") else credentials.base_url + webui
        return f"{credentials.base_url}/pages/viewpage.action?pageId={self.args.parent}"

    def _run(self):
        a, p = self.args, self.printer
        credentials = self._credentials()
        docs = self._docset()
        client = self.client_factory(credentials)
        plan = self._plan(client, docs)
        refusals = plan.refusals(a.max_updates)
        link = self._parent_link(plan, credentials)

        if a.dry_run or refusals:
            p.table(plan.items)
            p.ownership(plan, refusals)
            if refusals:
                raise ErrorTranslator.refused(refusals)
            if plan.handover_items():
                p.handover_hint()
            p.footer(len(plan.items), "DRY-RUN (nothing written)", a, link)
            return

        confirm = None
        if not a.adopt and self.prompt.is_interactive():
            confirm = self.prompt.confirm
        try:
            report = self.executor_factory(client).execute(
                plan, max_updates=a.max_updates, adopt=a.adopt, confirm=confirm)
        except PlanRefused as e:                       # re-checked inside the executor
            raise ErrorTranslator.refused(e.refusals) from None
        except HandoverRequired as e:
            raise ErrorTranslator.handover_required(e.items) from None
        except HandoverDeclined as e:
            raise ErrorTranslator.handover_declined(e.items) from None
        except PlanExecutionError as e:
            raise self.errors.partial(e.report) from None

        p.table(plan.items)
        if a.adopt and plan.handover_items():
            p.ownership(plan, adopted=True)
        p.marker_warnings(report)
        p.footer(len(plan.items), "published", a, link)


# --- entry points ------------------------------------------------------------------------

def build_parser():
    """The command-line parser (kept for tests and the v1.x API)."""
    return ArgumentParserFactory().build()


def main(argv=None, out=None):
    """Parse ``argv`` and run :class:`PublishCommand` with the real collaborators.

    :raises SystemExit: on any expected failure (exit status 1) or bad arguments (2).
    """
    PublishCommand.from_args(build_parser().parse_args(argv), out).run()


if __name__ == "__main__":
    main()
