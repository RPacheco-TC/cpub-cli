"""The reserved-word commands: ``setup`` (S1.4) and ``check`` (S1.5); ``undo`` later.

Split from :mod:`.cli`, which parses the command line, dispatches the reserved
first words and stays the composition root: every collaborator here
(prompts, hidden prompt, terminal check, client factory, credential writer,
checks) is injected, so the tests drive them with no terminal, no file and
no network.

- ``setup`` asks for the three credentials, tests them with one read-only
  ``whoami``, and saves only credentials that Confluence accepted.
- ``check`` words the raw :class:`~.checks.CheckResult` facts as ``✓`` / ``!``
  / ``✗`` / ``-`` lines with a ``Fix:``; it never writes anything and never
  disables TLS verification.

Dependency direction: this module may import cli_output, cli_errors, config,
checks and api, never :mod:`.cli`.
"""
import argparse
import urllib.error
import urllib.parse

from .errors import CliError, ConfigExists
from .config import Credentials
from .checks import OK, WARN, FAIL, SKIP
from .cli_errors import ErrorTranslator, CREDENTIAL_LINES, TOKEN_URL, KNOWN_ISSUES_URL
from .cli_output import _plural


# --- setup (S1.4) ------------------------------------------------------------------------

#: How many times a rejected email/token is asked for again before giving up.
MAX_ATTEMPTS = 3


def normalize_site_url(text):
    """``acme.atlassian.net``, ``https://acme.atlassian.net`` or any page URL of
    the site -> ``https://acme.atlassian.net/wiki``.

    :raises ValueError: with a sentence for the user, when it is not an https site URL.
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("The site URL is empty.")
    if "://" not in text:
        text = "https://" + text
    url = urllib.parse.urlsplit(text)
    if url.scheme != "https":
        raise ValueError("The site URL must start with https:// (your token is sent to it).")
    if not url.hostname or " " in text:
        raise ValueError(f"{text!r} is not a site URL.")
    return f"https://{url.netloc}/wiki"


class SetupCommand:
    """One ``setup`` run: prompts -> one read-only identity check -> atomic write.

    :param out: stream for the conversation.
    :param ask: ``prompt -> str`` (default :func:`input`).
    :param ask_secret: ``prompt -> str`` without echo (default :func:`getpass.getpass`).
    :param stdin: checked with ``isatty()``; setup never waits on a pipe.
    :param client_factory: ``Credentials -> ConfluenceClient`` (only ``whoami`` is used).
    :param writer: a :class:`~.config.CredentialWriter` (``path``, ``exists()``,
        ``write(credentials, identity, overwrite=)``).
    :param command: the command name for messages (see :class:`~.cli.CommandName`).
    """

    def __init__(self, *, out, ask, ask_secret, stdin, client_factory, writer, command):
        self.out, self.ask, self.ask_secret, self.stdin = out, ask, ask_secret, stdin
        self.client_factory, self.writer, self.command = client_factory, writer, command

    def _print(self, *lines):
        for line in lines:
            print(line, file=self.out)

    def run(self):
        """:raises SystemExit: on a non-interactive stdin, a cancel, or repeated rejection."""
        try:
            self._run()
        except CliError as e:
            raise SystemExit(str(e)) from None
        except (EOFError, KeyboardInterrupt):
            raise SystemExit("\nSetup cancelled; nothing was saved.") from None

    def _run(self):
        path = self.writer.path
        if not self.stdin.isatty():
            raise self.not_interactive(path)
        self._print(f"This saves your Confluence credentials to {path} (readable only by you).")
        replace = self.writer.exists()
        if replace:
            answer = self.ask(f"{path} already exists. Replace it? Type yes to replace: ")
            if answer.strip().lower() != "yes":
                self._print("Nothing changed.")
                return
        base_url = self._site_url()
        email = self._required("Email of your Atlassian account: ")
        self._print(f"Create an API token at {TOKEN_URL}")
        credentials, me = self._signed_in(base_url, email)
        try:
            self.writer.write(credentials, me, overwrite=replace)
        except ConfigExists:
            raise CliError(f"{path} was created while setup was running; it was left as it is.",
                           f"Run {self.command} setup again to replace it.") from None
        except OSError as e:
            raise CliError(f"Could not save {path}: {e.strerror or e}.",
                           "Check that you can write to that folder, or write the file "
                           f"yourself (see {self.command} --help).") from None
        who = me.get("displayName") or me.get("email") or credentials.email
        self._print(f"Signed in as {who}. Saved to {path}.",
                    f"Next: {self.command} check   (or publish a note with "
                    f"{self.command} note.md --to <page URL> --dry-run)")

    def _required(self, prompt, default=None):
        while True:
            value = self.ask(prompt).strip() or (default or "")
            if value:
                return value
            self._print("This one is needed.")

    def _site_url(self):
        while True:
            text = self.ask("Confluence site URL (e.g. https://your-company.atlassian.net): ")
            try:
                url = normalize_site_url(text)
            except ValueError as e:
                self._print(str(e))
                continue
            self._print(f"Using {url}")
            return url

    def _signed_in(self, base_url, email):
        """Ask for the token until Confluence accepts it (at most :data:`MAX_ATTEMPTS`)."""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if attempt > 1:
                email = self._required(f"Email [{email}]: ", default=email)
            token = self.ask_secret("API token (hidden while you type): ").strip()
            if not token:
                self._print("This one is needed.")
                continue
            credentials = Credentials(base_url, email, token)
            translator = ErrorTranslator(argparse.Namespace(space=None, parent=None),
                                         credentials, command=self.command)
            try:
                me = self.client_factory(credentials).whoami()
            except urllib.error.HTTPError as e:
                err = self.no_site(base_url, e) if e.code == 404 else translator.http(e)
                raise SystemExit(translator.mask(str(err))) from None
            except urllib.error.URLError as e:          # network or TLS: re-asking won't help
                raise SystemExit(translator.mask(str(translator.url(e)))) from None
            if me.get("authenticated"):
                return credentials, me
            status = me.get("status")
            self._print("Token rejected: Confluence did not accept this email and token"
                        + (f" (HTTP {status})." if status not in (None, 200) else ".")
                        + " Nothing was saved; try again.")
        raise self.rejected()

    # -- messages --

    def not_interactive(self, path):
        """AC-1.4.4: never hang on a pipe; say how to write the file by hand."""
        return CliError(f"{self.command} setup asks questions, so it needs a terminal.",
                        f"Run it in a terminal, or write {path} yourself (then chmod 600 it).",
                        "One KEY=VALUE per line:\n"
                        + "\n".join(f"  {line}" for line in CREDENTIAL_LINES))

    @staticmethod
    def no_site(base_url, e):
        return CliError(f"No Confluence Cloud site answered at {base_url}.",
                        "Check the site URL: it is the address you open Confluence at, "
                        "e.g. https://your-company.atlassian.net.",
                        f"Confluence said: {e.code} {e.reason}")

    def rejected(self):
        return CliError(f"Confluence did not accept the email and token {MAX_ATTEMPTS} times; "
                        "nothing was saved.",
                        f"Check the email of your Atlassian account, create a new token at "
                        f"{TOKEN_URL}, then run {self.command} setup again.")


# --- check (S1.5) ------------------------------------------------------------------------

_MARK = {OK: "✓", WARN: "!", FAIL: "✗", SKIP: "-"}
_INSTALL = "uv tool install git+https://github.com/RPacheco-TC/obx-confluence-obsidian-publisher"
_TITLE = {"tls": "Connection", "auth": "Sign-in"}


def _sources(sources):
    """``{var: source}`` -> ``"from ~/.config/…/.env"`` or one phrase per source."""
    names = {"CONFLUENCE_BASE_URL": "site URL", "CONFLUENCE_EMAIL": "email",
             "CONFLUENCE_API_TOKEN": "token"}
    by_source = {}
    for var, src in (sources or {}).items():
        by_source.setdefault(str(src), []).append(names.get(var, var))
    if len(by_source) == 1:
        return f"from {next(iter(by_source))}"
    return "; ".join(f"{', '.join(v)} from {k}" for k, v in by_source.items())


class CheckReport:
    """Prints each check as ``✓`` / ``!`` (warning) / ``✗`` / ``-`` (skipped) plus a
    ``Fix:`` line, from the raw facts of :class:`~.checks.CheckResult`.

    :param out: the output stream.
    :param command: the command name for ``Fix:`` lines.
    """

    def __init__(self, out, command):
        self.out, self.command = out, command

    def show(self, results):
        """Print every check; return the exit status (1 if any check failed, else 0)."""
        for r in results:
            text, fix = self.word(r)
            print(f"{_MARK.get(r.status, '?')} {text}", file=self.out)
            if fix and r.status in (WARN, FAIL):
                print(f"  Fix: {fix}", file=self.out)
        failed = [r for r in results if r.status == FAIL]
        if failed:
            summary = f"{_plural(len(failed), 'problem')} to fix."
        elif all(r.status == OK for r in results):
            summary = "All good."
        else:
            summary = "Ready, with warnings."
        print("\n" + summary, file=self.out)
        return 1 if failed else 0

    def word(self, r):
        """``(text, fix)`` for one check result."""
        f, cmd = r.facts or {}, self.command
        if r.status == SKIP:
            return f"{_TITLE.get(r.name, r.name)}: not checked (see above)", None
        method = getattr(self, "_" + r.name, None)
        return method(r.status == OK, f) if method else (r.name, None)

    @staticmethod
    def _python(ok, f):
        return (f"Python {f.get('version', '?')}",
                f"Install Python {f.get('minimum', '3.12')} or newer (uv brings its own).")

    def _credentials(self, ok, f):
        if ok:
            return f"Credentials found ({_sources(f.get('sources'))})", None
        if f.get("env_file"):
            return (f"The --env-file {f['env_file']} does not exist",
                    "Pass the path of an existing .env file, or drop --env-file.")
        searched = ", ".join(["environment variables"] + list(f.get("searched") or ()))
        return (f"Credentials missing: {', '.join(f.get('missing') or ())} "
                f"(searched {searched})",
                f"Run {self.command} setup, or add them to ~/.config/confluence-publisher/.env.")

    def _tls(self, ok, f):
        base = f.get("base_url", "the site")
        if ok and str(base).lower().startswith("http://"):     # D29: loopback IP only
            return f"Connection to {base} (plain http to this computer: no TLS)", None
        if ok:
            return f"Connection to {base} (TLS certificate verified)", None
        if f.get("invalid"):                       # B9: unparsable, never echoed
            err = ErrorTranslator.invalid_base_url(f.get("source"))
            return err.sentence.rstrip("."), err.fix
        if f.get("insecure"):                      # B8: nothing was sent
            scheme, _, host = str(f.get("base_url", "")).partition("://")
            err = ErrorTranslator.insecure_base_url(scheme, host, f.get("source"))
            return err.sentence.rstrip("."), err.fix
        if f.get("cert"):
            return (f"Python could not verify the TLS certificate of {base}: {f.get('error')}",
                    "If you use the python.org build on macOS, run "
                    "'/Applications/Python 3.12/Install Certificates.command' once "
                    f"(never disable verification). See {KNOWN_ISSUES_URL}")
        return (f"Could not reach {base}: {f.get('error')}",
                "Check CONFLUENCE_BASE_URL (it ends in /wiki) and your network or VPN.")

    def _auth(self, ok, f):
        if ok:
            who = f.get("displayName") or f.get("email") or "your account"
            mail = f" ({f['email']})" if f.get("email") and f.get("displayName") else ""
            return f"Signed in as {who}{mail}", None
        status = f.get("status")
        return ("Confluence did not accept your email/API token"
                + (f" (HTTP {status})" if status not in (None, 200) else ""),
                f"Run {self.command} setup to enter them again "
                f"(create a new token at {TOKEN_URL}).")

    @staticmethod
    def _npx(ok, f):
        if ok:
            where = "  (found outside PATH)" if f.get("fallback") else ""
            return f"npx {f.get('path')}: Mermaid diagrams are rendered{where}", None
        return ("npx not found: a folder publishes its diagrams as code blocks, and a note "
                "with diagrams is refused",
                "Install Node.js (https://nodejs.org), or add its folder to PATH.")

    def _cpub(self, ok, f):
        if ok:
            return (f"cpub is {f.get('path')}  (use this full path in the Obsidian hotkey)",
                    None)
        return ("cpub is not installed, so there is no fixed path for the Obsidian hotkey",
                f"Install it: {_INSTALL}")


class CheckCommand:
    """``check``: run the injected read-only checks and report them (S1.5).

    :param checks: ``.run(env_file) -> list[Check]`` (a :class:`~.config.SystemChecks`).
    :param report: a :class:`CheckReport`.
    """

    def __init__(self, args, *, checks, report):
        self.args, self.checks, self.report = args, checks, report

    def run(self):
        """:raises SystemExit: with status 1 when a check failed (warnings pass)."""
        status = self.report.show(self.checks.run(self.args.env_file))
        if status:
            raise SystemExit(status)
