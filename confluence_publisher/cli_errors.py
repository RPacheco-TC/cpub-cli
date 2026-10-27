"""Friendly errors: every expected failure becomes one sentence plus a ``Fix:`` line (S3.1).

Split from :mod:`.cli` (which wires the run) so each module keeps one job.
:class:`ErrorTranslator` gets the credentials it needs (base URL, token
source, the token to mask) through its constructor; it never reads the
environment.
"""
import ssl
import urllib.error

from .errors import CliError
from .cli_output import PlanPrinter, _plural, _need

TOKEN_URL = "https://id.atlassian.com/manage-profile/security/api-tokens"


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
