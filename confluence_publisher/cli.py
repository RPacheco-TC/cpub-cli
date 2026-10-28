"""Command line for the Confluence publisher: the package's one composition root.

Publishes an Obsidian-style Markdown doc set as a page tree under one parent
page. Every run is planned and checked before the first write; it only updates
pages strictly *below* the parent you pass that it published itself
(``cpub-managed`` content property) or that you hand over in this run, caps
updates per run (``--max-updates``), and never deletes or moves anything.

Usage
-----
.. code-block:: console

    cpub NOTE.md --to PAGE | --under PAGE      # installed (uv tool / pipx)
    cpub DOCS_DIR --parent PAGE [--prefix P]   # also: python3.12 confluence_publish.py …

The command name shown in help and ``Fix:`` lines follows how the tool was
started (:class:`CommandName`).

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
import dataclasses
import urllib.error

from .errors import (CliError, MissingCredentials, SpaceNotFound, NotAFolder, PlanRefused,
                     HandoverRequired, HandoverDeclined, PlanExecutionError, BadPageRef,
                     NotAMarkdownFile)
from .config import CredentialLoader, AppDirs
from .api import ConfluenceClient, HttpTransport
from .storage import StorageConverter
from .docset import DocSet, SingleNote
from .pageref import PageRef
from .plan import Planner, DEFAULT_MAX_UPDATES
from .executor import PlanExecutor
from .cli_output import PlanPrinter, HandoverPrompt
from .cli_errors import ErrorTranslator, CREDENTIAL_LINES, TOKEN_URL
from . import __version__



_HELP_INTRO = """\
Publish Markdown to Confluence Cloud: one note to one page, or a folder of
docs as a page tree. A page can be given as its id or its URL.

examples:
  {cmd} note.md --to https://acme.atlassian.net/wiki/spaces/ENG/pages/123456789 --dry-run
  {cmd} note.md --under 123456789
  {cmd} docs/ --parent 123456789 --prefix myrepo --dry-run

  note.md --to PAGE     replace that page's content (its title is kept)
  note.md --under PAGE  create a child page of PAGE (re-running updates it)
  docs/ --parent PAGE   publish the folder as child pages of PAGE

Safety: every run is planned and checked before anything is written. It only
updates pages it published itself from the same source; any other page needs
a one-time hand-over (type the page count when asked, or --adopt in scripts).
Nothing is ever deleted or moved.
"""

_HELP_CREDS = """\
credentials (first hit wins, per variable):
  1. environment variables CONFLUENCE_BASE_URL, CONFLUENCE_EMAIL, CONFLUENCE_API_TOKEN
  2. --env-file PATH
  3. ./.env in the current folder
  4. ~/.config/confluence-publisher/.env  ($XDG_CONFIG_HOME is honoured)
  Each file holds KEY=VALUE lines; keep it chmod 600:
""" + "".join(f"    {line}\n" for line in CREDENTIAL_LINES) + f"""\
  Create the API token at {TOKEN_URL}
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


# --- the command name (D26) ------------------------------------------------------

class CommandName:
    """The command to show in usage, examples and ``Fix:`` lines: how the tool was started.

    ``cpub`` / ``confluence-publish`` (installed console scripts) show themselves;
    the clone's shim shows ``python3.12 confluence_publish.py``; ``-m`` shows
    ``python3.12 -m confluence_publisher``. Anything else falls back to ``cpub``.
    """
    SCRIPTS = ("cpub", "confluence-publish")
    SHIM = "python3.12 confluence_publish.py"
    MODULE = "python3.12 -m confluence_publisher"
    FALLBACK = "cpub"

    @classmethod
    def resolve(cls, argv0):
        """:param argv0: ``sys.argv[0]`` as the interpreter set it.
        :rtype: str"""
        path = pathlib.PurePath(str(argv0 or ""))
        name = path.name.lower()
        stem = name[:-4] if name.endswith(".exe") else name     # Windows launchers
        if stem in cls.SCRIPTS:
            return stem
        if name == "confluence_publish.py":
            return cls.SHIM
        if name == "__main__.py" and path.parent.name == "confluence_publisher":
            return cls.MODULE
        return cls.FALLBACK


# --- argument parsing -------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class RunMode:
    """What the command line asks for, once the flag combination is validated.

    :param kind: ``"note"`` (one ``.md`` file) or ``"set"`` (a folder).
    :param path: the note or folder.
    :param page: the page reference as typed (id or URL): the ``--to`` target,
        or the parent for ``--under`` / ``--parent``.
    :param replace: ``True`` for ``--to`` (replace that page).
    """
    kind: str
    path: str
    page: str
    replace: bool = False


class ModeResolver:
    """Chooses note or set mode from the path and checks the flags that go with it.

    :param is_file: ``path -> bool`` (default :meth:`pathlib.Path.is_file`).
    :param is_dir: ``path -> bool`` (default :meth:`pathlib.Path.is_dir`).
    """

    def __init__(self, is_file=None, is_dir=None):
        self.is_file = is_file or (lambda p: pathlib.Path(p).is_file())
        self.is_dir = is_dir or (lambda p: pathlib.Path(p).is_dir())

    def resolve(self, args, errors=None):
        """:rtype: RunMode
        :raises CliError: for a missing path, a non-Markdown file, or flags
            that do not fit the mode."""
        path = args.path
        err = errors or ErrorTranslator(args, command=CommandName.FALLBACK)
        if args.parent and args.under and args.parent != args.under:
            raise err.parent_and_under()
        if self.is_dir(path):
            if args.to:
                raise err.to_with_folder()
            if args.title:
                raise err.title_with_folder()
            page = args.parent or args.under
            if not page:
                raise err.folder_without_parent(path)
            return RunMode("set", path, page)
        if not self.is_file(path):
            raise err.no_such_path(path)
        if not path.lower().endswith(".md"):
            raise err.not_markdown(path)
        under = args.under or args.parent
        if args.to and under:
            raise err.to_and_under()
        if not (args.to or under):
            raise err.note_without_target(path)
        return RunMode("note", path, args.to or under, replace=bool(args.to))


class ArgumentParserFactory:
    """Builds the ``argparse`` parser: every flag, its help text and validation.

    :param command: the command name to show (see :class:`CommandName`).
    """

    def __init__(self, command):
        self.command = command

    def build(self):
        """:rtype: argparse.ArgumentParser"""
        ap = argparse.ArgumentParser(
            prog=self.command, description=_HELP_INTRO.format(cmd=self.command),
            epilog=_HELP_CREDS, formatter_class=argparse.RawDescriptionHelpFormatter)
        ap.add_argument("--version", action="version", version=f"{self.command} {__version__}")
        ap.add_argument("path", metavar="PATH",
                        help='a Markdown note (.md file), or a folder of "NN - Title.md" docs '
                             '(optional "In Situ/" subfolder)')
        ap.add_argument("--to", default=None, metavar="PAGE",
                        help="note only: replace the content of this page (id or URL)")
        ap.add_argument("--under", default=None, metavar="PAGE",
                        help="publish under this page (id or URL): a note becomes a child "
                             "page; for a folder it is the same as --parent")
        ap.add_argument("--parent", default=None, metavar="PAGE",
                        help="folder: the page (id or URL) to publish the docs under")
        ap.add_argument("--title", default=None,
                        help="note only: the page title (default: --to keeps the page's "
                             "title; --under uses the frontmatter title: or the file name)")
        ap.add_argument("--space", default=None,
                        help="Confluence space KEY (e.g. ~yourname or ENG); optional, taken "
                             "from the page when omitted, and checked against it when given")
        ap.add_argument("--img-dir", default=None,
                        help="where to render Mermaid PNGs (default: <folder>/.img for a "
                             "folder, the user cache folder for a note)")
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
    :param mode_resolver: a :class:`ModeResolver` (note vs folder, flag checks).
    :param note_loader: ``(path, title) -> SingleNote``; raises
        :class:`~.errors.NotAMarkdownFile`.
    :param app_dirs: an :class:`~.config.AppDirs` (the note-mode image cache folder).
    :param command: the command name for ``Fix:`` lines (see :class:`CommandName`).
    """

    def __init__(self, args, *, out, credential_loader, client_factory, docset_loader,
                 planner_factory, executor_factory, prompt, mode_resolver=None,
                 note_loader=None, app_dirs=None, command=CommandName.FALLBACK):
        self.args, self.out, self.command = args, out, command
        self.mode_resolver = mode_resolver or ModeResolver()
        self.note_loader = note_loader or (lambda path, title: SingleNote.from_file(path, title))
        self.app_dirs = app_dirs or AppDirs()
        self.credential_loader = credential_loader
        self.client_factory = client_factory
        self.docset_loader = docset_loader
        self.planner_factory = planner_factory
        self.executor_factory = executor_factory
        self.prompt = prompt
        self.printer = PlanPrinter(out)
        self.errors = ErrorTranslator(args, command=command)

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
        img_dir = args.img_dir or str(pathlib.Path(args.path) / ".img")
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
        self.errors = ErrorTranslator(a, credentials, command=self.command)
        return credentials

    def _docset(self):
        try:
            docs = self.docset_loader(self.args.path)
        except NotAFolder:
            raise self.errors.not_a_folder() from None
        dupes = docs.duplicate_titles()
        if dupes:
            raise ErrorTranslator.duplicate_titles(dupes)
        return docs

    def _note(self, mode):
        try:
            return self.note_loader(mode.path, self.args.title)
        except NotAMarkdownFile:
            raise ErrorTranslator.not_markdown(mode.path) from None

    def _page_id(self, text, credentials):
        """A page id from an id or a page URL (pure; no network)."""
        try:
            return PageRef.parse(text, credentials.base_url).page_id
        except BadPageRef as e:
            raise ErrorTranslator.bad_page_ref(e) from None

    def _plan(self, client, source, mode):
        a, stage = self.args, "space"
        try:
            me = client.whoami()
            if not me.get("authenticated"):
                raise self.errors.unauthenticated(me)
            space_id = None
            if a.space:
                try:
                    space_id = client.resolve_space_id(a.space)
                except SpaceNotFound:
                    raise self.errors.no_space() from None
            stage = "parent"
            planner = self.planner_factory(client, a)
            if mode.replace:
                return planner.build_replace(source, a.parent, space_id)
            return planner.build(source, space_id, a.parent)
        except urllib.error.HTTPError as e:
            raise self.errors.read(e, stage) from None

    def _page_link(self, webui, page_id, credentials):
        if webui:
            return webui if webui.startswith("http") else credentials.base_url + webui
        return f"{credentials.base_url}/pages/viewpage.action?pageId={page_id}"

    def _run(self):
        mode = self.mode_resolver.resolve(self.args, self.errors)   # local, before any I/O
        credentials = self._credentials()
        page_id = self._page_id(mode.page, credentials)
        # --to/--under/--parent all resolve to one page id; a note's diagrams go to the cache
        img_dir = self.args.img_dir
        if mode.kind == "note" and not img_dir:
            img_dir = str(self.app_dirs.note_img_dir)
        self.args = argparse.Namespace(**{**vars(self.args), "parent": page_id,
                                          "img_dir": img_dir})
        self.errors = ErrorTranslator(self.args, credentials, command=self.command)
        source = self._docset() if mode.kind == "set" else self._note(mode)
        client = self.client_factory(credentials)
        plan = self._plan(client, source, mode)
        self._show_and_write(plan, mode, client, credentials)

    def _space_label(self, plan):
        info = getattr(plan, "space_info", None) or {}
        return self.args.space or info.get("key") or f"id {plan.space_id}"

    def _show_and_write(self, plan, mode, client, credentials):
        a, p = self.args, self.printer
        refusals = plan.refusals(a.max_updates)
        note = mode.kind == "note"
        parent_link = self._page_link((plan.parent_info or {}).get("webui"), a.parent, credentials)

        if a.dry_run or refusals:
            p.table(plan.items)
            if note and not refusals:                 # never preview what will be refused
                p.preview(plan.items[0], self._space_label(plan))
            p.ownership(plan, refusals)
            if refusals:
                raise ErrorTranslator.refused(refusals)
            if plan.handover_items():
                p.handover_hint(len(plan.handover_items()))
            if note:
                it = plan.items[0]
                link = (self._page_link(it.webui, it.page_id, credentials) if it.page_id
                        else parent_link)
                p.note_footer("DRY-RUN (nothing written)", self._space_label(plan), link)
            else:
                p.footer(len(plan.items), "DRY-RUN (nothing written)", a, parent_link,
                     self._space_label(plan))
            return

        if note:
            p.preview(plan.items[0], self._space_label(plan))
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
            raise self.errors.handover_declined(e.items) from None
        except PlanExecutionError as e:
            raise self.errors.partial(e.report) from None

        p.table(plan.items)
        if a.adopt and plan.handover_items():
            p.ownership(plan, adopted=True)
        p.marker_warnings(report)
        if note:
            first = plan.items[0]
            it, pid = report.written[0] if report.written else (first, first.page_id)
            p.note_footer("published", self._space_label(plan),
                          self._page_link(None, pid, credentials),   # by id: title may change
                          history=f"{credentials.base_url}/pages/viewpreviousversions.action"
                                  f"?pageId={pid}")
        else:
            p.footer(len(plan.items), "published", a, parent_link, self._space_label(plan))


# --- entry points ------------------------------------------------------------------------

def build_parser(command=None):
    """The command-line parser; ``command`` defaults to how the tool was started."""
    return ArgumentParserFactory(command or CommandName.resolve(sys.argv[0])).build()


def main(argv=None, out=None):
    """Entry point of ``cpub``, ``confluence-publish``, the shim and ``-m``.

    Decides the command name once (:class:`CommandName`), then parses ``argv``
    and runs :class:`PublishCommand` with the real collaborators.

    :raises SystemExit: on any expected failure (exit status 1) or bad arguments (2).
    """
    command = CommandName.resolve(sys.argv[0])
    args = build_parser(command).parse_args(argv)
    PublishCommand.from_args(args, out, command=command).run()


if __name__ == "__main__":
    main()
