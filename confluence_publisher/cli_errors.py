"""Friendly errors: every expected failure becomes one sentence plus a ``Fix:`` line (S3.1).

Split from :mod:`.cli` (which wires the run) so each module keeps one job.
:class:`ErrorTranslator` gets the credentials it needs (base URL, token
source, the token to mask) through its constructor; it never reads the
environment.
"""
import re
import shlex
import ssl
import urllib.error

from .errors import CliError
from .docset import TO_KEY as FRONTMATTER_TO, UNDER_KEY as FRONTMATTER_UNDER
from .cli_output import PlanPrinter, _plural, _need

TOKEN_URL = "https://id.atlassian.com/manage-profile/security/api-tokens"
KNOWN_ISSUES_URL = ("https://github.com/RPacheco-TC/confluence-obsidian-publisher"
                    "/blob/main/docs/KNOWN_ISSUES.md")
#: The three credential lines, with placeholders only: an installed ``cpub`` has no
#: ``.env.example`` to point at, and no real value is ever printed.
CREDENTIAL_LINES = ("CONFLUENCE_BASE_URL=https://your-company.atlassian.net/wiki",
                    "CONFLUENCE_EMAIL=you@your-company.com",
                    "CONFLUENCE_API_TOKEN=<your API token>")
_SPACE_BY_ID =re.compile(r"/api/v2/spaces/[^/?#]+(?:[?#]|$)")   # GET /spaces/{id}, not ?keys=


# --- friendly errors (S3.1) -----------------------------------------------------------

class ErrorTranslator:
    """Turns every expected failure into a :class:`CliError` (sentence + Fix + detail).

    :param args: the parsed command line (``space``, ``parent``).
    :param credentials: the resolved :class:`~.config.Credentials`, once known
        (base URL and token source for messages, token for masking).
    :param command: the command name for ``Fix:`` lines, decided once in
        :mod:`.cli` from how the tool was started.
    """

    def __init__(self, args, credentials=None, *, command):
        self.args = args
        self.credentials = credentials
        self.command = command

    # credentials and local input
    @staticmethod
    def env_file_missing(path):
        return CliError(f"The --env-file {path} does not exist.",
                        "Pass the path of an existing .env file, or drop --env-file to use "
                        "./.env or ~/.config/confluence-publisher/.env.")

    def missing_credentials(self, missing, searched):
        searched = ["environment variables"] + [str(p) for p in searched]
        return CliError(
            "Missing Confluence credential(s): " + ", ".join(missing) + ".",
            f"Run {self.command} setup, which asks for them and saves them; or add them to "
            "~/.config/confluence-publisher/.env (or ./.env), one KEY=VALUE per line as below, "
            f"or export them; create a token at {TOKEN_URL}.",
            "For example:\n" + "\n".join(f"  {line}" for line in CREDENTIAL_LINES)
            + "\nSearched, in order:\n" + "\n".join(f"- {s}" for s in searched))

    @staticmethod
    def diagrams_not_renderable(path):
        """A note with Mermaid and no ``npx``: refused, never published as code (PM #108)."""
        return CliError(f"Diagrams in {path} cannot be rendered (npx not found).",
                        "Install Node.js, or add its folder to PATH in the Shell commands "
                        "plugin.")

    @staticmethod
    def insecure_base_url(scheme, host, source):
        """B8: a non-https base URL would send the token in clear text; nothing was sent.

        :param scheme: the URL's scheme ("" when it has none).
        :param host: ``hostname[:port]`` only (never a ``user:password@`` part).
        :param source: ``"environment"``, a file path, or ``None``.
        """
        found = f"{scheme}://{host}" if scheme else "no https:// at the start"
        where = ("the environment variable CONFLUENCE_BASE_URL"
                 if source in (None, "environment") else str(source))
        target = f"https://{host}/wiki" if host else "https://your-company.atlassian.net/wiki"
        return CliError(f"CONFLUENCE_BASE_URL must start with https:// (found {found}).",
                        f"Change it to {target} in {where}. Nothing was sent.")

    @staticmethod
    def invalid_base_url(source):
        """B9: a CONFLUENCE_BASE_URL that cannot be parsed. The value is never echoed
        (it may hold a user:password); nothing was sent."""
        where = ("the environment variable CONFLUENCE_BASE_URL"
                 if source in (None, "environment") else str(source))
        return CliError("CONFLUENCE_BASE_URL is not a valid URL.",
                        f"Use the form https://<your-site>.atlassian.net/wiki in {where}. "
                        "Nothing was sent.")

    # flag combinations (note vs folder)
    @staticmethod
    def no_such_path(path):
        return CliError(f"No such file or folder: {path}.",
                        'Pass a Markdown note (a .md file) or a folder of "NN - Title.md" docs.')

    @staticmethod
    def not_markdown(path):
        return CliError(f"Not a Markdown note: {path}.",
                        'Pass a .md file, or a folder of "NN - Title.md" docs.')

    @staticmethod
    def bad_page_ref(e):
        """A page id/URL that cannot be used (folder, short link, other site, unknown)."""
        return CliError(str(e), e.fix)

    @staticmethod
    def to_and_under():
        return CliError("--to and --under can't be used together.",
                        "Use --to PAGE to replace that page, or --under PAGE to create a "
                        "child page of it.")

    def note_without_target(self, path):
        arg = shlex.quote(str(path))               # a copy-pasteable command line
        return CliError("Say where this note goes: --to replaces a page, --under creates a "
                        "child page.",
                        f"{self.command} {arg} --to <page URL or id>   or   "
                        f"{self.command} {arg} --under <page URL or id>",
                        "Or put the target in the note's frontmatter, then run "
                        f"{self.command} {arg} with no flag:\n"
                        f"  {FRONTMATTER_TO + ':':<19}<page URL or id>   (replace that page)\n"
                        f"  {FRONTMATTER_UNDER + ':':<19}<page URL or id>   (create a child page)")

    @staticmethod
    def both_frontmatter_targets(path):
        """The note names a page to replace and a parent: which one is meant?"""
        return CliError(f"The frontmatter of {path} has both {FRONTMATTER_TO}: and "
                        f"{FRONTMATTER_UNDER}:, so it is not clear where the note goes.",
                        f"Keep only one of them ({FRONTMATTER_TO}: replaces that page, "
                        f"{FRONTMATTER_UNDER}: creates a child page), or pass --to or "
                        "--under, which win over the frontmatter.")

    def folder_without_parent(self, path):
        return CliError("Say which page to publish this folder under.",
                        f"{self.command} {shlex.quote(str(path))} --parent <page URL or id>"
                        "   (--under works too)")

    @staticmethod
    def to_with_folder():
        return CliError("--to replaces one page, so it takes a single note, not a folder.",
                        "Use --parent PAGE (or --under PAGE) to publish the folder as child "
                        "pages.")

    @staticmethod
    def title_with_folder():
        return CliError("--title names one page, so it takes a single note, not a folder.",
                        "Drop --title: folder pages are titled from their file names.")

    @staticmethod
    def parent_and_under():
        return CliError("--parent and --under name different pages.",
                        "Give only one of them: they mean the same thing for a folder.")

    def not_a_folder(self):
        return CliError(f"Not a folder: {self.args.path}.",
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
        if e.code == 404 and _SPACE_BY_ID.search(getattr(e, "url", None) or ""):
            # the page was found, but its space (looked up by id) was not
            return CliError(f"Confluence could not show the space of page {self.args.parent} "
                            "(it may be restricted for your account).",
                            "Ask a space admin for access to that space, or check the page URL.",
                            self.http(e).detail)
        return self.http(e)

    def http(self, e):
        a = self.args
        raw = (f"Confluence said: {e.code} {e.reason}\n"
               f"{getattr(e, 'detail', '') or '(no detail returned)'}")
        if e.code == 401:
            return self.bad_token(raw)
        if e.code == 403:
            where = f" ({a.space})" if a.space else ""
            return CliError(f"Your account can't edit this page/space{where}.",
                            "Ask a space admin for edit permission, or publish under a page "
                            "you can edit.",
                            raw)
        if e.code == 404:
            return CliError(f"Page {a.parent} was not found or is not visible to you.",
                            "Check the page URL or id: it must be a page (not a folder)"
                            + (f" in space {a.space}." if a.space else " that you can open."),
                            raw)
        if e.code == 409:
            return CliError("A page was edited in Confluence after this run was planned, so it "
                            "was not overwritten.",
                            "Check the latest version in Confluence, then run the same command "
                            "again to re-plan.", raw)
        if e.code == 400:
            return CliError("Confluence refused the request as invalid.",
                            f"Usually --parent {a.parent} is a folder (use a page) or a page "
                            "title already exists in the space (use a distinct --prefix).",
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
                            f"(never disable verification). See {KNOWN_ISSUES_URL}", str(reason))
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
            f"Run it in a terminal and type {n} when asked, or add --adopt to take "
            f"{'it' if n == 1 else 'them'} over.",
            "\n".join(PlanPrinter.describe(it).strip() for it in items))

    def handover_declined(self, items):
        """The typed count did not match; the alternative depends on the mode."""
        n = len(items)
        if getattr(self.args, "to", None):          # note --to: one named page
            other = 'use --under PAGE --title "New title" to publish a new page instead.'
        elif str(getattr(self.args, "path", "")).lower().endswith(".md"):   # note --under
            other = "use a different --title to publish a new page instead."
        else:
            other = "use a distinct --prefix to publish new pages instead."
        return CliError("Hand-over not confirmed — nothing was written.",
                        f"Run again and type {n} to take the page(s) over, or {other}")

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
