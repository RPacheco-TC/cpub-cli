"""S1.1 credential autoload: parse_env_text / load_env, on strings only (no files).

Run from the repo root::

    python3.12 -B -m unittest discover -s tests
"""
import pathlib
import unittest

from _support import config as cp, errors

URL, MAIL, TOK = cp.CRED_VARS


class ParseEnvText(unittest.TestCase):
    def test_accepted_forms(self):
        text = (
            "# a comment\n"
            "\n"
            "PLAIN=value\n"
            "export EXPORTED=yes\n"
            "  SPACED = padded  \n"
            "DQ=\"two words # not a comment\"\n"
            "SQ='lit $HOME \\n'\n"
            "TRAIL=abc # trailing comment\n"
            "HASH=a#b\n"
            "ESC=\"say \\\"hi\\\" \\\\ ok\\n\"\n"
            "EQ=a=b=c\n"
            "EMPTY=\n"
            "not an assignment\n"
            "export\n"
        )
        self.assertEqual(cp.CredentialLoader.parse(text), {
            "PLAIN": "value", "EXPORTED": "yes", "SPACED": "padded",
            "DQ": "two words # not a comment", "SQ": "lit $HOME \\n", "TRAIL": "abc",
            "HASH": "a#b", "ESC": 'say "hi" \\ ok\n', "EQ": "a=b=c", "EMPTY": "",
        })

    def test_never_expands_or_executes(self):
        self.assertEqual(cp.CredentialLoader.parse("A=$(rm -rf /)\nB=`id`\nC=${HOME}\n"),
                         {"A": "$(rm -rf /)", "B": "`id`", "C": "${HOME}"})

    def test_later_assignment_wins(self):
        self.assertEqual(cp.CredentialLoader.parse("A=1\nA=2\n"), {"A": "2"})


def files(mapping):
    """A fake ``read_file``: ``{path: text}`` or ``{path: (text, mode)}``; others are missing."""
    def read(path):
        v = mapping.get(str(path))
        if v is None:
            return None
        return v if isinstance(v, tuple) else (v, 0o100600)
    return read


ENV = {"HOME": "/home/u"}
#: QA's B9 strings: urlsplit / .hostname raise ValueError on them.
B9_URLS = ("http://[::1]%40evil.com/wiki", "http://[::1/wiki", "https://[::1/wiki",
          "http://[evil.com]/wiki")
CWD = "/work"
USER = "/home/u/.config/confluence-publisher/.env"


def load(fs, environ=ENV, env_file=None, warn=None):
    warnings = []
    loader = cp.CredentialLoader(cwd=CWD, environ=dict(environ), read_file=files(fs),
                                 warn=warn or warnings.append)
    out = loader.resolve(env_file)
    return out, warnings


class LoadEnv(unittest.TestCase):
    def test_search_paths(self):
        paths = cp.CredentialLoader(cwd=CWD, environ=ENV).search_paths("~/x.env")
        self.assertEqual(paths[1:],
                         [pathlib.Path("/work/.env"), pathlib.Path(USER)])
        xdg = cp.CredentialLoader(cwd=CWD, environ={"XDG_CONFIG_HOME": "/xdg"})
        self.assertEqual(xdg.search_paths()[-1],
                         pathlib.Path("/xdg/confluence-publisher/.env"))

    def test_first_hit_wins_per_variable(self):
        fs = {"/explicit.env": f"{URL}=https://explicit/wiki\n",
              "/work/.env": f"{URL}=https://cwd/wiki\n{MAIL}=cwd@x\n",
              USER: f"{URL}=https://user/wiki\n{MAIL}=user@x\n{TOK}=user-token\n"}
        out, _ = load(fs, environ={**ENV, MAIL: "env@x"}, env_file="/explicit.env")
        self.assertEqual(out, {URL: ("https://explicit/wiki", "/explicit.env"),
                               MAIL: ("env@x", "environment"),
                               TOK: ("user-token", USER)})

    def test_env_vars_win_set_a_workflow_unchanged(self):
        environ = {**ENV, URL: "u", MAIL: "m", TOK: "t"}
        out, _ = load({"/work/.env": f"{TOK}=file\n"}, environ=environ)
        self.assertEqual({k: v[1] for k, v in out.items()},
                         dict.fromkeys(cp.CRED_VARS, "environment"))

    def test_empty_env_var_falls_through_to_files(self):
        out, _ = load({"/work/.env": f"{TOK}=file\n"}, environ={**ENV, TOK: ""})
        self.assertEqual(out[TOK], ("file", "/work/.env"))

    def test_missing_env_file_is_an_error_naming_it(self):
        with self.assertRaises(FileNotFoundError) as cm:
            load({}, environ={**ENV, URL: "u", MAIL: "m", TOK: "t"}, env_file="/nope.env")
        self.assertEqual(cm.exception.filename, "/nope.env")
        self.assertIn("/nope.env", str(cm.exception))

    def test_missing_implicit_files_are_not_an_error(self):
        self.assertEqual(load({}), ({}, []))

    def test_group_readable_token_file_warns_but_loads(self):
        out, warnings = load({"/work/.env": (f"{TOK}=t\n", 0o100644)})
        self.assertEqual(out[TOK], ("t", "/work/.env"))
        self.assertEqual(len(warnings), 1)
        self.assertIn("chmod 600 /work/.env", warnings[0])
        self.assertNotIn("=t", warnings[0])

    def test_no_warning_for_private_file_or_non_token_file(self):
        _, w1 = load({"/work/.env": (f"{TOK}=t\n", 0o100600)})
        _, w2 = load({"/work/.env": (f"{URL}=u\n", 0o100644)})
        self.assertEqual((w1, w2), ([], []))

    def test_only_credential_vars_are_loaded(self):
        out, _ = load({"/work/.env": "PATH=/evil\nOTHER=x\n"})
        self.assertEqual(out, {})

    def test_load_fills_only_unset(self):
        """(was apply_env) env values are kept; files only fill what is unset."""
        fs = {"/work/.env": f"{URL}=https://u/wiki\n{MAIL}=m\n{TOK}=tok1\n"}
        loader = cp.CredentialLoader(cwd=CWD, environ={**ENV, MAIL: "keep"}, read_file=files(fs))
        creds = loader.load()
        self.assertEqual((creds.base_url, creds.email, creds.token),
                         ("https://u/wiki", "keep", "tok1"))
        self.assertEqual(creds.sources, {URL: "/work/.env", MAIL: "environment",
                                         TOK: "/work/.env"})

    def test_http_base_url_is_refused_naming_its_source_never_the_token(self):
        """B8: no Credentials object can carry a non-https URL; nothing is sent."""
        for url in ("http://acme.atlassian.net/wiki", "HTTP://h/wiki", "ftp://h/wiki",
                    "http://user:pw@h:8080/wiki",
                    "http://localhost:8080/wiki", "http://127.0.0.1.evil.com/wiki",
                    "http://[::2]/wiki"):
            fs = {"/work/.env": f"{URL}={url}\n{MAIL}=m\n{TOK}=s3cr3t-tok\n"}
            loader = cp.CredentialLoader(cwd=CWD, environ=dict(ENV), read_file=files(fs))
            with self.subTest(url=url), self.assertRaises(errors.InsecureBaseUrl) as cm:
                loader.load()
            e = cm.exception
            self.assertIsInstance(e, errors.PublisherError)
            self.assertEqual(e.source, "/work/.env")
            for secret in ("s3cr3t-tok", "pw"):
                self.assertNotIn(secret, str(e) + e.host)
        self.assertEqual(cm.exception.url, "http://[::2]/wiki")
        e = errors.InsecureBaseUrl("http://user:pw@h:8080/wiki")
        self.assertEqual(str(e), "CONFLUENCE_BASE_URL must start with https:// "
                                 "(found http://h:8080).")
        self.assertEqual(cp.Credentials("HTTPS://H/wiki", "m", "t").base_url, "HTTPS://H/wiki")

    def test_malformed_base_url_is_refused_without_echoing_it(self):
        """B9: a URL that does not parse, has no host or a bad port is a typed error
        that keeps no part of the value, never a traceback."""
        for url in B9_URLS + ("acme.atlassian.net/wiki", "https:/no-host",
                              "https://pw:s3cr3t@h:abc/wiki"):
            fs = {"/work/.env": f"{URL}={url}\n{MAIL}=m\n{TOK}=s3cr3t-tok\n"}
            loader = cp.CredentialLoader(cwd=CWD, environ=dict(ENV), read_file=files(fs))
            with self.subTest(url=url), self.assertRaises(errors.InvalidBaseUrl) as cm:
                loader.load()
            e = cm.exception
            self.assertEqual((str(e), e.source),
                             ("CONFLUENCE_BASE_URL is not a valid URL.", "/work/.env"))
            self.assertIsInstance(e, errors.PublisherError)
            self.assertNotIn("s3cr3t", repr(vars(e)) + str(e))
            self.assertFalse({"url", "host", "scheme"} & set(vars(e)))

    def test_http_to_a_loopback_ip_literal_is_allowed(self):
        """D29: a local test server only; the name localhost stays refused."""
        for url in ("http://127.0.0.1:8080/wiki", "http://127.5.5.5/wiki",
                    "http://[::1]:9000/wiki", "http://user@127.0.0.1/wiki",
                    "HTTP://127.0.0.1/wiki", "http://[::ffff:127.0.0.1]/wiki"):
            with self.subTest(url=url):
                self.assertEqual(cp.Credentials(url, "m", "t").base_url, url)
        for url in ("http://localhost/wiki", "http://127.0.0.1.evil.com/wiki",
                    "http://10.0.0.1/wiki", "http://user@localhost@127.0.0.1.evil.com/",
                    "http://127.0.0.1@evil.com/wiki", "http://evil.com#@127.0.0.1/wiki",
                    "http://0x7f.0.0.1/wiki", "http://2130706433/wiki", "http://127.1/wiki",
                    "http://127.0.0.1%2f@evil.com/"):
            with self.subTest(url=url), self.assertRaises(errors.InsecureBaseUrl):
                cp.Credentials(url, "m", "t")

    def test_load_raises_missing_credentials_with_searched_files(self):
        loader = cp.CredentialLoader(cwd=CWD, environ=dict(ENV), read_file=files({}))
        with self.assertRaises(errors.MissingCredentials) as cm:
            loader.load()
        self.assertEqual(cm.exception.missing, cp.CRED_VARS)
        self.assertEqual(cm.exception.searched, [pathlib.Path("/work/.env"), pathlib.Path(USER)])


class CredentialsValue(unittest.TestCase):

    def setUp(self):
        self.c = cp.Credentials("https://x.invalid/wiki/", "me@x", "s3cr3t-token",
                                {TOK: "/work/.env"})

    def test_base_url_trailing_slash_dropped(self):
        self.assertEqual(self.c.base_url, "https://x.invalid/wiki")

    def test_auth_header_is_basic(self):
        import base64
        self.assertEqual(self.c.auth_header(),
                         "Basic " + base64.b64encode(b"me@x:s3cr3t-token").decode())

    def test_token_never_shown(self):
        for text in (repr(self.c), self.c.masked(), self.c.mask("bad s3cr3t-token here")):
            self.assertNotIn("s3cr3t-token", text)
        self.assertEqual(self.c.mask("x s3cr3t-token y"), "x **** y")
        self.assertEqual(self.c.token_source, "/work/.env")


if __name__ == "__main__":
    unittest.main()
