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
import getpass
import datetime
import pathlib
import argparse
import dataclasses
import urllib.error

from .errors import NotAMarkdownFile
from .config import CredentialLoader, AppDirs, CredentialWriter
from .checks import EnvironmentCheck
from .api import ConfluenceClient, HttpTransport
from .storage import StorageConverter
from .docset import DocSet, SingleNote
from .plan import Planner, DEFAULT_MAX_UPDATES
from .executor import PlanExecutor
from .cli_output import HandoverPrompt
from .cli_errors import ErrorTranslator, CREDENTIAL_LINES, TOKEN_URL
from .cli_commands import SetupCommand, CheckReport, CheckCommand
from .cli_publish import PublishFlow
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

  {cmd} setup   ask for your credentials, test them, save them (first run)
  {cmd} check   show what works: Python, credentials, connection, sign-in, npx
  (setup and check are commands: publish a file or folder named so as ./setup)

A note can keep its target in its frontmatter, so {cmd} note.md needs no flag:
  confluence: PAGE         same as --to PAGE
  confluence_parent: PAGE  same as --under PAGE
A flag on the command line wins over the frontmatter. The tool never edits the note.

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
    :param from_frontmatter: ``True`` when the target came from the note's
        ``confluence:`` / ``confluence_parent:`` key instead of a flag (S2.4).
    """
    kind: str
    path: str
    page: str
    replace: bool = False
    from_frontmatter: bool = False


class ModeResolver:
    """Chooses note or set mode from the path and checks the flags that go with it.

    :param is_file: ``path -> bool`` (default :meth:`pathlib.Path.is_file`).
    :param is_dir: ``path -> bool`` (default :meth:`pathlib.Path.is_dir`).
    :param targets_of: ``path -> {"to": str|None, "under": str|None}``, the note's
        frontmatter targets (default :meth:`~.docset.SingleNote.targets`); read only
        for a note given with no ``--to`` / ``--under`` / ``--parent``.
    """

    def __init__(self, is_file=None, is_dir=None, targets_of=None):
        self.is_file = is_file or (lambda p: pathlib.Path(p).is_file())
        self.is_dir = is_dir or (lambda p: pathlib.Path(p).is_dir())
        self.targets_of = targets_of or (lambda p: SingleNote.from_file(p).targets())

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
        if args.to or under:                       # flags win over the frontmatter
            return RunMode("note", path, args.to or under, replace=bool(args.to))
        try:
            targets = self.targets_of(path)
        except NotAMarkdownFile:
            raise err.not_markdown(path) from None
        to, under = targets.get("to"), targets.get("under")
        if to and under:
            raise err.both_frontmatter_targets(path)
        if not (to or under):
            raise err.note_without_target(path)
        return RunMode("note", path, to or under, replace=bool(to), from_frontmatter=True)


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


# --- the publish command: PublishFlow plus its composition -------------------------------

class PublishCommand(PublishFlow):
    """:class:`~.cli_publish.PublishFlow` plus :meth:`from_args`, which wires the real
    collaborators, so this module stays the package's one composition root."""

    @classmethod
    def from_args(cls, args, out=None, **overrides):
        """Composition root: wire the real collaborators.

        :param overrides: replace any constructor collaborator by name (tests
            pass fakes this way; nothing is patched).
        """
        out = out or sys.stdout
        app_dirs = overrides.get("app_dirs") or AppDirs()
        wiring = dict(
            mode_resolver=ModeResolver(),
            note_loader=lambda path, title: SingleNote.from_file(path, title),
            command=CommandName.FALLBACK,
            credential_loader=CredentialLoader(),
            client_factory=lambda creds: ConfluenceClient(creds, HttpTransport()),
            docset_loader=DocSet.from_folder,
            planner_factory=lambda client, args: cls._real_planner(
                client, args, app_dirs.find_executable("npx")),
            app_dirs=app_dirs,
            executor_factory=PlanExecutor,
            prompt=HandoverPrompt(out),
        )
        wiring.update(overrides)
        return cls(args, out=out, **wiring)

    @staticmethod
    def _real_planner(client, args, npx):
        """:param npx: ``(command, child_env)`` or ``None`` when npx was not found
        (searched on PATH, then the usual Node folders: Obsidian starts commands
        with a bare PATH)."""
        img_dir = args.img_dir or str(pathlib.Path(args.path) / ".img")
        return Planner(client, StorageConverter(args.prefix, img_dir, args.img_width, npx=npx))


# --- setup and check (reserved first words, D2) -----------------------------------------

#: First words that name a command instead of a path to publish.
SUBCOMMANDS = ("setup", "check")


def subcommand_parser(name, command):
    """The small parser of ``setup`` / ``check``."""
    if name == "setup":
        ap = argparse.ArgumentParser(
            prog=f"{command} setup",
            description="Ask for the Confluence site URL, your email and an API token, test "
                        "them with one read-only call, and save them to "
                        "~/.config/confluence-publisher/.env (mode 600). Nothing is saved "
                        "unless Confluence accepts them.")
        return ap
    ap = argparse.ArgumentParser(
        prog=f"{command} check",
        description="Check Python, the credentials (and where they come from), the "
                    "connection and its TLS certificate, the sign-in and npx, and print the "
                    "full path of the command for the Obsidian hotkey. Writes nothing; exit "
                    "status 1 if something must be fixed.")
    ap.add_argument("--env-file", default=None, metavar="PATH",
                    help="read credentials from this .env file first")
    return ap


def build_subcommand(name, args, out=None, command=CommandName.FALLBACK, **overrides):
    """Composition root of ``setup`` / ``check``: the real collaborators, or ``overrides``."""
    out = out or sys.stdout
    client_factory = overrides.pop(
        "client_factory", lambda creds: ConfluenceClient(creds, HttpTransport()))
    if name == "setup":
        wiring = dict(out=out, ask=input, ask_secret=getpass.getpass, stdin=sys.stdin,
                      client_factory=client_factory, command=command)
        wiring.update(overrides)
        if "writer" not in wiring:
            wiring["writer"] = CredentialWriter(AppDirs(),
                                                today=datetime.date.today().isoformat())
        return SetupCommand(**wiring)
    wiring = dict(report=CheckReport(out, command))
    wiring.update(overrides)
    if "checks" not in wiring:
        wiring["checks"] = EnvironmentCheck(CredentialLoader(), client_factory, AppDirs(),
                                            sys.argv[0])
    return CheckCommand(args, **wiring)


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
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv and argv[0] in SUBCOMMANDS:          # a path named so is given as ./setup
        name = argv[0]
        args = subcommand_parser(name, command).parse_args(argv[1:])
        build_subcommand(name, args, out, command).run()
        return
    args = build_parser(command).parse_args(argv)
    PublishCommand.from_args(args, out, command=command).run()


if __name__ == "__main__":
    main()
