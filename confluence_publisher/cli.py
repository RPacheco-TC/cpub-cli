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
import pathlib
import argparse
import urllib.error

from .errors import (CliError, MissingCredentials, SpaceNotFound, NotAFolder, PlanRefused,
                     HandoverRequired, HandoverDeclined, PlanExecutionError)
from .config import CredentialLoader
from .api import ConfluenceClient, HttpTransport
from .storage import StorageConverter
from .docset import DocSet
from .plan import Planner, DEFAULT_MAX_UPDATES
from .executor import PlanExecutor
from .cli_output import PlanPrinter, HandoverPrompt
from .cli_errors import ErrorTranslator


CMD = "python3.12 confluence_publish.py"

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
