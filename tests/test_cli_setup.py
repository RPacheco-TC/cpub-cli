"""``setup`` and ``check`` (S1.4, S1.5), in-process and offline.

Setup is driven by injected prompts, a fake client and a fake credential
writer (no terminal, no file, no network, AC-1.4.5); check by fake check
results. Run from the repo root::

    python3.12 -B -m unittest discover -s tests
"""
import contextlib
import io
import pathlib
import types
import ssl
import unittest
import urllib.error

from _support import checks, cli, cli_commands, config

TOKEN = "s3cr3t-token-value"
PATH = "/home/u/.config/confluence-publisher/.env"


class Tty:
    def __init__(self, tty=True):
        self.tty = tty

    def isatty(self):
        return self.tty


class Writer:
    """A fake CredentialWriter: records writes, touches nothing."""
    path = PATH

    def __init__(self, exists=False):
        self._exists, self.written = exists, []

    def exists(self):
        return self._exists

    def write(self, credentials, identity, overwrite=False):
        assert identity.get("authenticated"), "never save credentials that failed"
        self.written.append((credentials, overwrite))


class Client:
    """``whoami`` answers from a script: dicts are returned, exceptions raised."""
    def __init__(self, answers, seen):
        self.answers, self.seen = answers, seen

    def whoami(self):
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


ME = {"authenticated": True, "displayName": "Ada Lovelace", "email": "ada@example.com"}
BAD = {"authenticated": False, "status": 401}


class Setup(unittest.TestCase):
    def run_setup(self, answers, secrets, whoami=(ME,), tty=True, exists=False):
        """Return (exit message or None, output, prompts, writer, credentials tried)."""
        out, prompts, tried = io.StringIO(), [], []
        answers, secrets, whoami = list(answers), list(secrets), list(whoami)

        def ask(prompt):
            prompts.append(prompt)
            if not answers:
                raise EOFError
            return answers.pop(0)

        def ask_secret(prompt):
            prompts.append(prompt)
            return secrets.pop(0)

        def client_factory(credentials):
            tried.append(credentials)
            return Client(whoami, tried)

        writer = Writer(exists)
        cmd = cli.build_subcommand("setup", None, out, "cpub", ask=ask, ask_secret=ask_secret,
                                   stdin=Tty(tty), client_factory=client_factory,
                                   writer=writer)
        try:
            cmd.run()
            msg = None
        except SystemExit as e:
            msg = e.code
        self.assertNotIn(TOKEN, out.getvalue())
        self.assertNotIn(TOKEN, str(msg))
        return msg, out.getvalue(), prompts, writer, tried

    def test_saves_tested_credentials_with_the_wiki_url(self):
        msg, out, prompts, writer, _ = self.run_setup(
            ["acme.atlassian.net", "ada@example.com"], [TOKEN])
        self.assertIsNone(msg, msg)
        (creds, overwrite), = writer.written
        self.assertEqual((creds.base_url, creds.email, creds.token, overwrite),
                         ("https://acme.atlassian.net/wiki", "ada@example.com", TOKEN, False))
        self.assertIn("Using https://acme.atlassian.net/wiki", out)
        self.assertIn("Create an API token at https://id.atlassian.com/", out)
        self.assertIn(f"Signed in as Ada Lovelace. Saved to {PATH}.", out)
        self.assertIn("Next: cpub check", out)
        self.assertEqual(prompts[-1], "API token (hidden while you type): ")

    def test_a_rejected_token_is_asked_again_and_never_saved(self):
        msg, out, prompts, writer, tried = self.run_setup(
            ["https://acme.atlassian.net/wiki/spaces/ENG", "ada@example.com", ""],
            ["wrong-token", TOKEN], whoami=(BAD, ME))
        self.assertIsNone(msg, msg)
        self.assertIn("Token rejected: Confluence did not accept this email and token "
                      "(HTTP 401). Nothing was saved; try again.", out)
        self.assertIn("Email [ada@example.com]: ", prompts)   # Enter keeps the email
        self.assertEqual([c.token for c in tried], ["wrong-token", TOKEN])
        self.assertEqual([c.token for c, _ in writer.written], [TOKEN])

    def test_three_rejections_save_nothing(self):
        msg, _, _, writer, _ = self.run_setup(
            ["acme.atlassian.net", "a@b.c", "", ""], ["t1", "t2", "t3"],
            whoami=(BAD, BAD, BAD))
        self.assertTrue(msg.startswith("Error: Confluence did not accept the email and token "
                                       "3 times; nothing was saved.\nFix: "), msg)
        self.assertIn("then run cpub setup again", msg)
        self.assertEqual(writer.written, [])

    def test_a_bad_url_is_asked_again(self):
        msg, out, _, writer, _ = self.run_setup(
            ["http://acme.atlassian.net", "", "acme.atlassian.net", "a@b.c"], [TOKEN])
        self.assertIsNone(msg, msg)
        self.assertIn("The site URL must start with https://", out)
        self.assertIn("The site URL is empty.", out)
        self.assertEqual(len(writer.written), 1)

    def test_an_existing_file_is_kept_unless_yes(self):
        msg, out, prompts, writer, _ = self.run_setup(["no"], [], exists=True)
        self.assertIsNone(msg, msg)
        self.assertEqual(prompts, [f"{PATH} already exists. Replace it? Type yes to replace: "])
        self.assertIn("Nothing changed.", out)
        self.assertEqual(writer.written, [])
        msg, _, _, writer, _ = self.run_setup(["yes", "acme.atlassian.net", "a@b.c"], [TOKEN],
                                              exists=True)
        self.assertIsNone(msg, msg)
        self.assertEqual([o for _, o in writer.written], [True])

    def test_not_a_terminal_refuses_without_asking(self):
        msg, _, prompts, writer, _ = self.run_setup([], [], tty=False)
        self.assertEqual(prompts, [])
        self.assertEqual(writer.written, [])
        self.assertTrue(msg.startswith("Error: cpub setup asks questions, so it needs a "
                                       "terminal.\nFix: Run it in a terminal, or write "
                                       f"{PATH} yourself (then chmod 600 it)."), msg)
        self.assertIn("  CONFLUENCE_BASE_URL=https://your-company.atlassian.net/wiki", msg)
        self.assertIn("  CONFLUENCE_API_TOKEN=<your API token>", msg)

    def test_tls_failure_stops_with_the_certificate_hint(self):
        err = urllib.error.URLError(ssl.SSLCertVerificationError("CERTIFICATE_VERIFY_FAILED"))
        msg, _, _, writer, _ = self.run_setup(["acme.atlassian.net", "a@b.c"], [TOKEN],
                                              whoami=(err,))
        self.assertIn("Error: Python could not verify Confluence's TLS certificate.", msg)
        self.assertIn("Install Certificates.command", msg)
        self.assertEqual(writer.written, [])

    def test_not_a_confluence_site(self):
        err = urllib.error.HTTPError("https://x/wiki/rest/api/user/current", 404, "Not Found",
                                     {}, None)
        msg, _, _, writer, _ = self.run_setup(["x.example.com", "a@b.c"], [TOKEN],
                                              whoami=(err,))
        self.assertIn("Error: No Confluence Cloud site answered at https://x.example.com/wiki.",
                      msg)
        self.assertEqual(writer.written, [])

    def test_ctrl_d_cancels(self):
        msg, _, _, writer, _ = self.run_setup(["acme.atlassian.net"], [])
        self.assertEqual(msg, "\nSetup cancelled; nothing was saved.")
        self.assertEqual(writer.written, [])


class SiteUrl(unittest.TestCase):
    def test_forms(self):
        for text in ("acme.atlassian.net", "https://acme.atlassian.net",
                     " https://acme.atlassian.net/ ", "https://acme.atlassian.net/wiki",
                     "https://acme.atlassian.net/wiki/spaces/ENG/pages/1/X?a=b#c"):
            with self.subTest(text=text):
                self.assertEqual(cli_commands.normalize_site_url(text),
                                 "https://acme.atlassian.net/wiki")

    def test_refused(self):
        for text in ("", "http://acme.atlassian.net", "not a url", "https://"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                cli_commands.normalize_site_url(text)


def check(name_, status_, **facts):
    return checks.CheckResult(name_, status_, facts)


ALL_OK = [
    check("python", "ok", version="3.12.4"),
    check("credentials", "ok", sources={"CONFLUENCE_BASE_URL": PATH, "CONFLUENCE_EMAIL": PATH,
                                        "CONFLUENCE_API_TOKEN": PATH}),
    check("tls", "ok", base_url="https://acme.atlassian.net/wiki"),
    check("auth", "ok", status=200, displayName="Ada Lovelace", email="ada@example.com"),
    check("npx", "ok", path="/opt/homebrew/bin/npx"),
    check("cpub", "ok", path="/Users/ada/.local/bin/cpub"),
]


class Check(unittest.TestCase):
    def show(self, checks):
        out = io.StringIO()
        status = cli_commands.CheckReport(out, "cpub").show(checks)
        return status, out.getvalue()

    def test_all_good(self):
        status, out = self.show(ALL_OK)
        self.assertEqual(status, 0)
        self.assertEqual(out.splitlines(), [
            "✓ Python 3.12.4",
            f"✓ Credentials found (from {PATH})",
            "✓ Connection to https://acme.atlassian.net/wiki (TLS certificate verified)",
            "✓ Signed in as Ada Lovelace (ada@example.com)",
            "✓ npx /opt/homebrew/bin/npx: Mermaid diagrams are rendered",
            "✓ cpub is /Users/ada/.local/bin/cpub  (use this full path in the Obsidian hotkey)",
            "",
            "All good."])

    def test_npx_missing_is_a_warning(self):
        status, out = self.show(ALL_OK[:4] + [check("npx", "warn")] + ALL_OK[5:])
        self.assertEqual(status, 0)
        self.assertIn("! npx not found: a folder publishes its diagrams as code blocks", out)
        self.assertIn("  Fix: Install Node.js", out)
        self.assertTrue(out.endswith("Ready, with warnings.\n"))

    def test_missing_credentials_fail_and_skip_the_network_checks(self):
        results = [ALL_OK[0], check("credentials", "fail", missing=["CONFLUENCE_API_TOKEN"],
                                    searched=[PATH]),
                   check("tls", "skip"), check("auth", "skip"), check("npx", "warn"),
                   ALL_OK[5]]
        status, out = self.show(results)
        self.assertEqual(status, 1)
        self.assertIn("✗ Credentials missing: CONFLUENCE_API_TOKEN (searched environment "
                      f"variables, {PATH})\n  Fix: Run cpub setup", out)
        self.assertIn("- Connection: not checked (see above)\n- Sign-in: not checked", out)
        self.assertTrue(out.endswith("1 problem to fix.\n"))

    def test_sources_from_several_places(self):
        _, out = self.show([check("credentials", "ok", sources={
            "CONFLUENCE_BASE_URL": "environment", "CONFLUENCE_EMAIL": "environment",
            "CONFLUENCE_API_TOKEN": PATH})])
        self.assertIn(f"(site URL, email from environment; token from {PATH})", out)

    def test_tls_and_auth_failures(self):
        _, out = self.show([check("tls", "fail", base_url="https://a/wiki", cert=True,
                                  error="[SSL: CERTIFICATE_VERIFY_FAILED] boom")])
        self.assertIn("Install Certificates.command", out)
        self.assertIn("never disable verification", out)
        _, out = self.show([check("auth", "fail", status=401)])
        self.assertIn("✗ Confluence did not accept your email/API token (HTTP 401)\n"
                      "  Fix: Run cpub setup", out)

    def test_loopback_http_does_not_claim_tls(self):
        _, out = self.show([check("tls", "ok", base_url="http://127.0.0.1:8080/wiki")])
        self.assertIn("✓ Connection to http://127.0.0.1:8080/wiki (plain http to this "
                      "computer: no TLS)", out)
        self.assertNotIn("certificate verified", out)

    def test_env_file_not_found(self):
        status, out = self.show([check("credentials", "fail", env_file="x.env")])
        self.assertEqual(status, 1)
        self.assertIn("✗ The --env-file x.env does not exist", out)

    def test_not_an_installed_cpub(self):
        _, out = self.show([check("cpub", "warn", path=None)])
        self.assertIn("! cpub is not installed, so there is no fixed path", out)
        self.assertIn("  Fix: Install it: uv tool install git+https://github.com/", out)

    def test_command_exits_1_on_a_failure_and_passes_the_env_file(self):
        seen = []

        class Checks:
            def __init__(self, result):
                self.result = result

            def run(self, env_file=None):
                seen.append(env_file)
                return self.result

        args = cli.subcommand_parser("check", "cpub").parse_args(["--env-file", "x.env"])
        out = io.StringIO()
        cli.build_subcommand("check", args, out, "cpub", checks=Checks(ALL_OK)).run()
        bad = [check("credentials", "fail", missing=["CONFLUENCE_EMAIL"])]
        with self.assertRaises(SystemExit) as ctx:
            cli.build_subcommand("check", args, out, "cpub", checks=Checks(bad)).run()
        self.assertEqual((ctx.exception.code, seen), (1, ["x.env", "x.env"]))


class InsecureBaseUrl(unittest.TestCase):
    """B8: an http:// base URL is refused before any client exists; the token never shows."""

    def run_publish(self, url, extra=()):
        environ = {"CONFLUENCE_BASE_URL": url, "CONFLUENCE_EMAIL": "a@b.c",
                   "CONFLUENCE_API_TOKEN": TOKEN}
        built = []
        loader = config.CredentialLoader(environ=environ, cwd="/proj", config_dir="/cfg",
                                         read_file=lambda p: None, warn=lambda m: None)
        args = cli.build_parser("cpub").parse_args(["note.md", "--to", "1", *extra])
        cmd = cli.PublishCommand.from_args(
            args, io.StringIO(), command="cpub", credential_loader=loader,
            client_factory=lambda creds: built.append(creds),
            mode_resolver=cli.ModeResolver(is_file=lambda p: True, is_dir=lambda p: False))
        with self.assertRaises(SystemExit) as ctx:
            cmd.run()
        self.assertEqual(built, [])                       # no client, so no request
        return str(ctx.exception.code)

    def test_http_is_refused_before_any_client(self):
        msg = self.run_publish("http://u:pw@acme.atlassian.net/wiki")
        self.assertEqual(msg.splitlines(), [
            "Error: CONFLUENCE_BASE_URL must start with https:// "
            "(found http://acme.atlassian.net).",
            "Fix: Change it to https://acme.atlassian.net/wiki in the environment variable "
            "CONFLUENCE_BASE_URL. Nothing was sent."])
        self.assertNotIn(TOKEN, msg)
        self.assertNotIn("pw", msg)

    def test_check_words_it_the_same_way(self):
        out = io.StringIO()
        cli_commands.CheckReport(out, "cpub").show([
            checks.CheckResult("tls", "fail", {"base_url": "http://acme.atlassian.net",
                                               "insecure": True, "cert": False,
                                               "source": PATH}),
            checks.CheckResult("auth", "skip", {})])
        self.assertIn("✗ CONFLUENCE_BASE_URL must start with https:// "
                      "(found http://acme.atlassian.net)\n"
                      "  Fix: Change it to https://acme.atlassian.net/wiki in "
                      f"{PATH}. Nothing was sent.", out.getvalue())


class InvalidBaseUrlWording(unittest.TestCase):
    """B9: an unparsable base URL; the raw value is never shown."""

    def test_wording(self):
        e = cli.ErrorTranslator.invalid_base_url
        self.assertEqual(str(e(PATH)).splitlines(), [
            "Error: CONFLUENCE_BASE_URL is not a valid URL.",
            f"Fix: Use the form https://<your-site>.atlassian.net/wiki in {PATH}. "
            "Nothing was sent."])
        self.assertIn("in the environment variable CONFLUENCE_BASE_URL.", str(e("environment")))

    BAD = ("http://[::1]%40evil.com/wiki", "http://[::1/wiki", "https://[::1/wiki",
           "http://[evil.com]/wiki", "acme.atlassian.net/wiki")

    def test_publish_and_dry_run_refuse_before_any_client(self):
        for url in self.BAD:
            for extra in ((), ("--dry-run",)):
                with self.subTest(url=url, extra=extra):
                    msg = InsecureBaseUrl.run_publish(self, url, extra)
                    self.assertEqual(msg.splitlines(), [
                        "Error: CONFLUENCE_BASE_URL is not a valid URL.",
                        "Fix: Use the form https://<your-site>.atlassian.net/wiki in the "
                        "environment variable CONFLUENCE_BASE_URL. Nothing was sent."])
                    self.assertNotIn(TOKEN, msg)
                    self.assertNotIn("evil", msg)

    def test_check_runs_the_real_checks_with_zero_calls(self):
        for url in self.BAD:
            with self.subTest(url=url):
                calls = []
                loader = config.CredentialLoader(
                    environ={"CONFLUENCE_BASE_URL": url, "CONFLUENCE_EMAIL": "a@b.c",
                             "CONFLUENCE_API_TOKEN": TOKEN},
                    cwd="/proj", config_dir="/cfg", read_file=lambda p: None,
                    warn=lambda m: None)
                dirs = types.SimpleNamespace(find_executable=lambda *a, **k: None,
                                             home=pathlib.Path("/h"))
                env_check = checks.EnvironmentCheck(loader, calls.append, dirs, "cpub")
                out = io.StringIO()
                args = cli.subcommand_parser("check", "cpub").parse_args([])
                with self.assertRaises(SystemExit):
                    cli.build_subcommand("check", args, out, "cpub", checks=env_check).run()
                self.assertEqual(calls, [])
                self.assertIn("✗ CONFLUENCE_BASE_URL is not a valid URL", out.getvalue())
                self.assertNotIn("evil", out.getvalue())
                self.assertNotIn(TOKEN, out.getvalue())

    def test_check_words_it(self):
        out = io.StringIO()
        status = cli_commands.CheckReport(out, "cpub").show([
            checks.CheckResult("tls", "fail", {"invalid": True, "source": PATH}),
            checks.CheckResult("auth", "skip", {})])
        self.assertEqual(status, 1)
        self.assertIn("✗ CONFLUENCE_BASE_URL is not a valid URL\n"
                      "  Fix: Use the form https://<your-site>.atlassian.net/wiki in "
                      f"{PATH}. Nothing was sent.", out.getvalue())


class Dispatch(unittest.TestCase):
    """D2: setup and check are reserved first words; ./setup is a path."""

    def help_of(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as ctx:
            cli.main(list(argv))
        self.assertEqual(ctx.exception.code, 0)
        return out.getvalue()

    def test_setup_and_check_have_their_own_help(self):
        self.assertTrue(self.help_of("setup", "--help").startswith("usage: cpub setup"))
        text = self.help_of("check", "-h")
        self.assertTrue(text.startswith("usage: cpub check [-h] [--env-file PATH]"), text)
        self.assertIn("Writes nothing", text)

    def test_a_path_named_setup_is_published_as_dot_slash_setup(self):
        with self.assertRaises(SystemExit) as ctx:
            cli.main(["./no-such-dir/setup", "--to", "1"])
        self.assertIn("No such file or folder: ./no-such-dir/setup.", str(ctx.exception.code))

    def test_main_help_lists_setup_and_check(self):
        text = self.help_of("--help")
        self.assertIn("  cpub setup   ask for your credentials", text)
        self.assertIn("  cpub check   show what works", text)

    def test_insecure_base_url_wording(self):
        """B8: scheme and host only; the place to edit is named."""
        e = cli.ErrorTranslator.insecure_base_url
        self.assertEqual(str(e("http", "acme.atlassian.net", PATH)).splitlines(), [
            "Error: CONFLUENCE_BASE_URL must start with https:// "
            "(found http://acme.atlassian.net).",
            f"Fix: Change it to https://acme.atlassian.net/wiki in {PATH}. Nothing was sent."])
        self.assertIn("in the environment variable CONFLUENCE_BASE_URL.",
                      str(e("http", "h", "environment")))
        self.assertIn("(found no https:// at the start)", str(e("", "", PATH)))

    def test_missing_credentials_point_at_setup(self):
        err = cli.ErrorTranslator(None, command="cpub").missing_credentials(["X"], [])
        self.assertIn("Fix: Run cpub setup, which asks for them and saves them;", str(err))


if __name__ == "__main__":
    unittest.main()
