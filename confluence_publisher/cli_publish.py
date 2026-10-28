"""The publish flow: credentials -> identity -> source -> plan -> check -> write.

Split from :mod:`.cli` to keep each module well under the size limit.
:class:`PublishFlow` runs one publish with the collaborators it is given and
builds none of them. :class:`.cli.PublishCommand` subclasses it with
``from_args``, the composition root, so ``cli`` stays the only module that
constructs a Confluence client. This module never imports :mod:`.cli`.
"""
import argparse
import urllib.error
import urllib.parse

from .errors import (CliError, MissingCredentials, SpaceNotFound, NotAFolder, PlanRefused,
                     HandoverRequired, HandoverDeclined, PlanExecutionError, BadPageRef,
                     NotAMarkdownFile, DiagramsUnrenderable, InsecureBaseUrl,
                     InvalidBaseUrl)
from .docset import TO_KEY, UNDER_KEY
from .pageref import PageRef
from .runlog import PUBLISH
from .cli_output import PlanPrinter
from .cli_errors import ErrorTranslator


class PublishFlow:
    """One publish run: credentials → identity → doc set → plan → check → write.

    Every collaborator is injected and none is built here: the real ones are
    wired by :meth:`.cli.PublishCommand.from_args` (the composition root), and
    tests pass fakes through it.

    :param args: the parsed command line.
    :param out: stream for normal output.
    :param credential_loader: ``.load(env_file) -> Credentials`` (a
        :class:`~.config.CredentialLoader`).
    :param client_factory: ``credentials -> ConfluenceClient``.
    :param docset_loader: ``path -> DocSet``; raises :class:`~.errors.NotAFolder`.
    :param planner_factory: ``(client, args) -> Planner``.
    :param executor_factory: ``client -> PlanExecutor``.
    :param prompt: a :class:`~.cli_output.HandoverPrompt`.
    :param mode_resolver: a :class:`~.cli.ModeResolver` (note vs folder, flag checks).
    :param note_loader: ``(path, title) -> SingleNote``; raises
        :class:`~.errors.NotAMarkdownFile`.
    :param app_dirs: an :class:`~.config.AppDirs` (the note-mode image cache folder).
    :param command: the command name for ``Fix:`` lines (see :class:`~.cli.CommandName`).
    :param run_log: a :class:`~.runlog.RunLog` that records every write (S0.5), or
        ``None`` for no audit log.
    """

    def __init__(self, args, *, out, credential_loader, client_factory, docset_loader,
                 planner_factory, executor_factory, prompt, mode_resolver,
                 note_loader, app_dirs, command, run_log=None):
        self.args, self.out, self.command = args, out, command
        self.mode_resolver = mode_resolver
        self.note_loader = note_loader
        self.app_dirs = app_dirs
        self.run_log = run_log
        self.credential_loader = credential_loader
        self.client_factory = client_factory
        self.docset_loader = docset_loader
        self.planner_factory = planner_factory
        self.executor_factory = executor_factory
        self.prompt = prompt
        self.printer = PlanPrinter(out)
        self.errors = ErrorTranslator(args, command=command)

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
        except InsecureBaseUrl as e:                # the transport's own check (B8)
            err = ErrorTranslator.insecure_base_url(e.scheme, e.host, e.source)
            raise SystemExit(self.errors.mask(str(err))) from None
        except InvalidBaseUrl as e:                 # the transport's own check (B9)
            raise SystemExit(str(ErrorTranslator.invalid_base_url(e.source))) from None

    # -- steps --

    def _credentials(self):
        a = self.args
        try:
            credentials = self.credential_loader.load(a.env_file)
        except FileNotFoundError as e:
            raise ErrorTranslator.env_file_missing(e.filename or a.env_file) from None
        except MissingCredentials as e:
            raise self.errors.missing_credentials(e.missing, e.searched) from None
        except InsecureBaseUrl as e:               # B8: refused before any client exists
            raise ErrorTranslator.insecure_base_url(e.scheme, e.host, e.source) from None
        except InvalidBaseUrl as e:                # B9: unparsable; the value is never shown
            raise ErrorTranslator.invalid_base_url(e.source) from None
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
        except DiagramsUnrenderable as e:          # a note never trades diagrams for code
            raise ErrorTranslator.diagrams_not_renderable(e.note) from None

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
        self._show_and_write(plan, mode, client, credentials, source)

    def _space_label(self, plan):
        info = getattr(plan, "space_info", None) or {}
        return self.args.space or info.get("key") or f"id {plan.space_id}"

    def _frontmatter_line(self, mode, note, credentials):
        """The ``key: value`` line equivalent to this run's flag, if the note has no key yet.

        It mirrors the flag that was used (``--to`` -> ``confluence:``, ``--under`` ->
        ``confluence_parent:``) with the page as a by-id URL, which never contains a
        ``#`` that YAML would read as a comment.
        """
        targets = note.targets()
        if mode.from_frontmatter or targets.get("to") or targets.get("under"):
            return None                            # the note already names a target
        key = TO_KEY if mode.replace else UNDER_KEY
        return f"{key}: {self._page_link(None, self.args.parent, credentials)}"

    def _show_and_write(self, plan, mode, client, credentials, source=None):
        a, p = self.args, self.printer
        refusals = plan.refusals(a.max_updates)
        note = mode.kind == "note"
        parent_link = self._page_link((plan.parent_info or {}).get("webui"), a.parent, credentials)
        p.unrendered_warning([it.title for it in plan.unrendered_items()])   # set mode only

        if a.dry_run or refusals:
            p.table(plan.items)
            p.unresolved_links(plan.unresolved_links)        # S2.5, note mode only
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
        log = None
        if self.run_log is not None:                # S0.5: one line per page written
            host = urllib.parse.urlsplit(credentials.base_url).hostname
            log = self.run_log.recorder(PUBLISH, host, space_id=plan.space_id)
        try:
            report = self.executor_factory(client).execute(
                plan, max_updates=a.max_updates, adopt=a.adopt, confirm=confirm, log=log)
        except PlanRefused as e:                       # re-checked inside the executor
            raise ErrorTranslator.refused(e.refusals) from None
        except HandoverRequired as e:
            raise ErrorTranslator.handover_required(e.items) from None
        except HandoverDeclined as e:
            raise self.errors.handover_declined(e.items) from None
        except PlanExecutionError as e:
            raise self.errors.partial(e.report) from None

        p.table(plan.items)
        p.unresolved_links(plan.unresolved_links)
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
            line = self._frontmatter_line(mode, source, credentials)
            if line:
                p.frontmatter_hint(line)
        else:
            p.footer(len(plan.items), "published", a, parent_link, self._space_label(plan))
