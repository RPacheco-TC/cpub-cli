"""Story B library: ``CredentialWriter`` (cpub setup) and ``EnvironmentCheck`` (cpub check).

Offline and in-process: the file system is a recording fake (no real file is
written), credentials come from an in-memory loader, Confluence is the
``FakeConfluence`` transport or a stub that raises. Run from the repo root::

    python3.12 -B -m unittest discover -s tests
"""
import pathlib
import ssl
import unittest
import urllib.error

from _support import checks, config, errors
from _fake_confluence import BASE, FakeConfluence

HOME = {"HOME": "/home/u", "PATH": "/usr/bin"}
CONF = pathlib.Path("/home/u/.config/confluence-publisher")
CREDS = config.Credentials(BASE, "dev@example.invalid", 'tok"$\\en')
WHO_OK = {"authenticated": True, "status": 200}


class FakeFiles:
    """Records the writer's file operations; nothing touches the disk."""
    def __init__(self, existing=()):
        self.files = {pathlib.Path(p): "old" for p in existing}
        self.ops = []

    def exists(self, path):
        return pathlib.Path(path) in self.files

    def make_private_dir(self, path):
        self.ops.append(("dir700", pathlib.Path(path)))

    def write_private(self, path, text):
        self.ops.append(("file600", pathlib.Path(path)))
        self.files[pathlib.Path(path)] = text


def writer(files):
    return config.CredentialWriter(config.AppDirs(HOME), files, today="2026-10-28")


class Writer(unittest.TestCase):
    def test_writes_the_user_config_file_private_and_readable_back(self):
        fs = FakeFiles()
        path = writer(fs).write(CREDS, WHO_OK)
        self.assertEqual(path, CONF / ".env")
        self.assertEqual(fs.ops, [("dir700", CONF), ("file600", CONF / ".env")])
        text = fs.files[path]
        self.assertTrue(text.startswith("# Confluence credentials for cpub"))
        loaded = config.CredentialLoader(
            environ=HOME, cwd="/nowhere", read_file=lambda p: (fs.files[p], 0o100600)
            if p in fs.files else None).load()
        self.assertEqual((loaded.base_url, loaded.email, loaded.token),
                         (CREDS.base_url, CREDS.email, CREDS.token))
        self.assertEqual(loaded.token_source, str(CONF / ".env"))

    def test_existing_file_is_kept_unless_overwrite_is_confirmed(self):
        fs = FakeFiles([CONF / ".env"])
        w = writer(fs)
        self.assertTrue(w.exists())
        with self.assertRaises(errors.ConfigExists) as cm:
            w.write(CREDS, WHO_OK)
        self.assertEqual((cm.exception.path, fs.ops, fs.files[CONF / ".env"]),
                         (CONF / ".env", [], "old"))
        self.assertIsInstance(cm.exception, errors.PublisherError)
        w.write(CREDS, WHO_OK, overwrite=True)
        self.assertIn("CONFLUENCE_API_TOKEN=", fs.files[CONF / ".env"])

    def test_credentials_that_did_not_authenticate_are_never_saved(self):
        fs = FakeFiles()
        for who in (None, {}, {"authenticated": False, "status": 401},
                    {"authenticated": False, "type": "anonymous", "status": 200}):
            with self.subTest(who=who), self.assertRaises(ValueError):
                writer(fs).write(CREDS, who)
        self.assertEqual(fs.ops, [])

    def test_line_break_in_a_value_is_refused(self):
        fs = FakeFiles()
        with self.assertRaises(ValueError):
            writer(fs).write(config.Credentials(BASE, "a@b", "tok\nEVIL=1"), WHO_OK)
        self.assertEqual(fs.ops, [])

    def test_xdg_config_home_is_honoured(self):
        w = config.CredentialWriter(config.AppDirs({"HOME": "/h", "XDG_CONFIG_HOME": "/x"}))
        self.assertEqual(w.path, pathlib.Path("/x/confluence-publisher/.env"))


class StubClient:
    """``whoami`` that returns a value or raises."""
    def __init__(self, result):
        self.result = result

    def whoami(self):
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def loader(env=None):
    env = {**HOME, **(env if env is not None else {
        "CONFLUENCE_BASE_URL": BASE, "CONFLUENCE_EMAIL": "e@x",
        "CONFLUENCE_API_TOKEN": "secret-token"})}
    return config.CredentialLoader(environ=env, cwd="/nowhere", read_file=lambda p: None)


def run(client=None, env=None, argv0="/home/u/.local/bin/cpub", present=(),
        version=(3, 12, 4)):
    def is_exe(p):
        return str(p) in present

    class Dirs(config.AppDirs):
        def find_executable(self, name, fallback_dirs=config.NODE_FALLBACK_DIRS, is_exe=is_exe):
            return super().find_executable(name, fallback_dirs, is_exe)

    factory = (lambda c: client) if client is not None else (lambda c: FakeConfluence().client(c))
    check = checks.EnvironmentCheck(loader(env), factory, Dirs(HOME), argv0,
                                    version_info=version)
    results = check.run()
    return {r.name: r for r in results}, results


class Check(unittest.TestCase):
    def test_all_good(self):
        by, results = run(present={"/opt/homebrew/bin/npx"})
        self.assertEqual([r.name for r in results], list(checks.CHECKS))
        self.assertEqual({n: r.status for n, r in by.items()},
                         {n: checks.OK for n in checks.CHECKS})
        self.assertEqual(by["python"].facts, {"version": "3.12.4", "minimum": "3.12"})
        self.assertEqual(by["credentials"].facts["sources"]["CONFLUENCE_API_TOKEN"],
                         "environment")
        self.assertEqual(by["auth"].facts["displayName"], "Dev")
        self.assertEqual(by["npx"].facts, {"path": "/opt/homebrew/bin/npx", "fallback": True})
        self.assertEqual(by["cpub"].facts, {"path": "/home/u/.local/bin/cpub"})
        self.assertFalse(checks.EnvironmentCheck.failed(results))

    def test_missing_credentials_skip_the_network(self):
        by, results = run(env={}, client=StubClient(AssertionError("no call expected")))
        self.assertEqual(by["credentials"].status, checks.FAIL)
        self.assertEqual(by["credentials"].facts["missing"], list(config.CRED_VARS))
        self.assertIn(str(CONF / ".env"), by["credentials"].facts["searched"])
        self.assertEqual((by["tls"].status, by["auth"].status), (checks.SKIP, checks.SKIP))
        self.assertTrue(checks.EnvironmentCheck.failed(results))

    def test_certificate_failure_is_flagged_for_the_macos_hint(self):
        err = urllib.error.URLError(ssl.SSLCertVerificationError(
            1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"))
        by, _ = run(client=StubClient(err))
        self.assertEqual((by["tls"].status, by["tls"].facts["cert"], by["auth"].status),
                         (checks.FAIL, True, checks.SKIP))

    def test_network_error_is_a_tls_failure_without_the_cert_hint(self):
        by, _ = run(client=StubClient(urllib.error.URLError(OSError("nodename nor servname"))))
        self.assertEqual((by["tls"].status, by["tls"].facts["cert"]), (checks.FAIL, False))
        self.assertIn("nodename", by["tls"].facts["error"])

    def test_rejected_token_and_anonymous_fail_auth(self):
        f = FakeConfluence(); f.user = 401
        by, _ = run(client=f.client())
        self.assertEqual((by["tls"].status, by["auth"].status, by["auth"].facts["status"]),
                         (checks.OK, checks.FAIL, 401))
        f = FakeConfluence(); f.user = {"type": "anonymous"}
        by, results = run(client=f.client())
        self.assertEqual((by["auth"].status, by["auth"].facts["type"]),
                         (checks.FAIL, "anonymous"))
        self.assertTrue(checks.EnvironmentCheck.failed(results))

    def test_unexpected_http_error_masks_the_token(self):
        err = urllib.error.HTTPError(BASE, 404, "Not Found", {}, None)
        err.detail = "no site here; you sent secret-token"
        by, _ = run(client=StubClient(err))
        self.assertEqual((by["auth"].status, by["auth"].facts["status"]), (checks.FAIL, 404))
        self.assertNotIn("secret-token", by["auth"].facts["detail"])

    def test_missing_npx_and_cpub_are_warnings_not_failures(self):
        by, results = run(argv0="confluence_publish.py")
        self.assertEqual((by["npx"].status, by["cpub"].status), (checks.WARN, checks.WARN))
        self.assertFalse(checks.EnvironmentCheck.failed(results))

    def test_cpub_path_found_from_a_clone_or_by_bare_name(self):
        by, _ = run(argv0="confluence_publish.py", present={"/home/u/.local/bin/cpub"})
        self.assertEqual(by["cpub"].facts["path"], "/home/u/.local/bin/cpub")
        by, _ = run(argv0="cpub", present={"/usr/bin/cpub"})
        self.assertEqual(by["cpub"].facts["path"], "/usr/bin/cpub")

    def test_old_python_is_a_failure(self):
        by, results = run(version=(3, 11, 9))
        self.assertEqual(by["python"].status, checks.FAIL)
        self.assertTrue(checks.EnvironmentCheck.failed(results))

    def test_http_base_url_fails_tls_and_sends_nothing(self):
        env = {"CONFLUENCE_BASE_URL": "http://acme.atlassian.net:8080/wiki",
               "CONFLUENCE_EMAIL": "e@x", "CONFLUENCE_API_TOKEN": "secret-token"}
        by, results = run(env=env, client=StubClient(AssertionError("token sent in clear")))
        self.assertEqual((by["tls"].status, by["tls"].facts["insecure"], by["auth"].status),
                         (checks.FAIL, True, checks.SKIP))
        self.assertEqual((by["credentials"].status, by["tls"].facts["base_url"],
                          by["tls"].facts["source"]),
                         (checks.OK, "http://acme.atlassian.net:8080", "environment"))
        self.assertNotIn("secret-token", repr(results))
        env["CONFLUENCE_BASE_URL"] = "http://127.0.0.1:8080/wiki"     # D29: local test server
        by, _ = run(env=env, client=StubClient({"authenticated": True, "status": 200}))
        self.assertEqual((by["tls"].status, by["auth"].status), (checks.OK, checks.OK))
        self.assertTrue(checks.EnvironmentCheck.failed(results))
        by, _ = run(present={"/opt/homebrew/bin/npx"})
        self.assertNotIn("insecure", by["tls"].facts)

    def test_check_never_writes(self):
        f = FakeConfluence()
        run(client=f.client())
        self.assertEqual((f.calls, f.writes), ([("GET", "/rest/api/user/current")], []))


if __name__ == "__main__":
    unittest.main()
