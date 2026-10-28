"""``cpub check`` (S1.5): what works and what does not, as facts. Never writes.

:class:`EnvironmentCheck` runs every check and returns :class:`CheckResult`
facts; the wording, the ``Fix:`` lines and the exit code shown to the user
belong to the CLI. TLS and authentication come from one read-only
``whoami`` call over the normal transport: certificate verification is never
turned off (AC-1.5.3).
"""
import dataclasses
import os
import pathlib
import ssl
import sys
import urllib.error

from .errors import InsecureBaseUrl, MissingCredentials

#: Check statuses. ``WARN`` never makes the run fail; ``SKIP`` = an earlier check failed.
OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"

#: The check names, in the order they run.
CHECKS = ("python", "credentials", "tls", "auth", "npx", "cpub")

#: Console-script names (D26) and where uv / pipx put them.
SCRIPT_NAMES = ("cpub", "confluence-publish")
USER_BIN = ".local/bin"


@dataclasses.dataclass(frozen=True)
class CheckResult:
    """One check.

    :param name: one of :data:`CHECKS`.
    :param status: :data:`OK`, :data:`WARN`, :data:`FAIL` or :data:`SKIP`.
    :param facts: what was found; keys per check (see :meth:`EnvironmentCheck.run`).
    """
    name: str
    status: str
    facts: dict = dataclasses.field(default_factory=dict)


class EnvironmentCheck:
    """Run the ``cpub check`` checks with injected collaborators.

    :param loader: a :class:`~.config.CredentialLoader`.
    :param client_factory: ``Credentials -> ConfluenceClient``.
    :param app_dirs: an :class:`~.config.AppDirs` (``find_executable``, ``HOME``).
    :param argv0: how the tool was started (``sys.argv[0]``).
    :param version_info: the interpreter version (``sys.version_info``).
    :param minimum: the lowest supported Python.
    """

    def __init__(self, loader, client_factory, app_dirs, argv0, *,
                 version_info=None, minimum=(3, 12)):
        self.loader, self.client_factory, self.app_dirs = loader, client_factory, app_dirs
        self.argv0 = argv0
        self.version_info = tuple(version_info or sys.version_info)[:3]
        self.minimum = minimum

    def run(self, env_file=None) -> list:
        """Run every check, in :data:`CHECKS` order.

        Facts per check:

        - ``python``: ``version`` ("3.12.4"), ``minimum``.
        - ``credentials``: ``sources`` {var: "environment" | path} and ``base_url``;
          on failure ``missing`` and ``searched``, or ``env_file`` (not found).
        - ``tls``: ``base_url``; on failure ``error`` (text) and ``cert`` (True for
          ``CERTIFICATE_VERIFY_FAILED``: the macOS ``Install Certificates`` case), or
          ``insecure`` (True: not ``https://`` nor ``http://`` to a loopback IP, so
          nothing was sent and ``auth`` is skipped: the token would travel in clear).
        - ``auth``: the ``whoami`` fields (``status``, ``type``, ``displayName``,
          ``email``), or ``status`` + ``detail`` for an unexpected HTTP error.
        - ``npx``: ``path`` and ``fallback`` (found outside ``PATH``); WARN if absent.
        - ``cpub``: ``path``, the absolute command for the Obsidian hotkey; WARN if absent.

        :rtype: list[CheckResult]
        """
        out = [self.python()]
        creds_result, creds = self.credentials(env_file)
        out.append(creds_result)
        out.extend(self.connection(creds))
        out += [self.npx(), self.cpub()]
        return out

    @staticmethod
    def failed(results) -> bool:
        """Whether any check is a hard failure (exit code 1, AC-1.5.2)."""
        return any(r.status == FAIL for r in results)

    def python(self):
        version = ".".join(str(n) for n in self.version_info)
        status = OK if self.version_info[:2] >= self.minimum else FAIL
        return CheckResult("python", status, {"version": version,
                                              "minimum": ".".join(map(str, self.minimum))})

    def credentials(self, env_file):
        try:
            creds = self.loader.load(env_file)
        except FileNotFoundError as e:
            return CheckResult("credentials", FAIL, {"env_file": e.filename}), None
        except MissingCredentials as e:
            return CheckResult("credentials", FAIL, {
                "missing": list(e.missing),
                "searched": [str(p) for p in e.searched]}), None
        except InsecureBaseUrl as e:        # found, but unusable: reported under tls
            return CheckResult("credentials", OK, {"sources": e.sources,
                                                   "base_url": f"{e.scheme}://{e.host}"}), e
        return CheckResult("credentials", OK, {"sources": dict(creds.sources),
                                               "base_url": creds.base_url}), creds

    def connection(self, creds):
        """The ``tls`` and ``auth`` results, from one ``whoami`` call."""
        if creds is None:
            return [CheckResult("tls", SKIP), CheckResult("auth", SKIP)]
        if isinstance(creds, InsecureBaseUrl):  # B8: never send the token unencrypted
            return [CheckResult("tls", FAIL, {"base_url": f"{creds.scheme}://{creds.host}",
                                              "insecure": True, "cert": False,
                                              "error": "not an https:// URL",
                                              "source": creds.source}),
                    CheckResult("auth", SKIP)]
        site = {"base_url": creds.base_url}
        try:
            who = self.client_factory(creds).whoami()
        except urllib.error.HTTPError as e:         # TLS worked; the server said no
            detail = creds.mask(str(getattr(e, "detail", "") or ""))[:300]
            return [CheckResult("tls", OK, site),
                    CheckResult("auth", FAIL, {"status": e.code, "detail": detail})]
        except (urllib.error.URLError, OSError) as e:
            reason = getattr(e, "reason", e)
            cert = isinstance(reason, ssl.SSLCertVerificationError) or \
                "CERTIFICATE_VERIFY_FAILED" in str(reason)
            return [CheckResult("tls", FAIL, {**site, "error": creds.mask(str(reason)),
                                              "cert": cert}),
                    CheckResult("auth", SKIP)]
        facts = {k: who.get(k) for k in ("status", "type", "displayName", "email")}
        return [CheckResult("tls", OK, site),
                CheckResult("auth", OK if who.get("authenticated") else FAIL, facts)]

    def npx(self):
        found = self.app_dirs.find_executable("npx")
        if found is None:
            return CheckResult("npx", WARN, {"path": None, "fallback": False})
        path, env = found
        return CheckResult("npx", OK, {"path": path, "fallback": env is not None})

    def cpub(self):
        """The absolute path of the installed command, for the hotkey recipe (S3.2).

        A console script started by name reports its own path (not resolved: uv's
        ``~/.local/bin/cpub`` symlink is the stable one); otherwise ``cpub`` is
        looked up on ``PATH`` and in ``~/.local/bin``.
        """
        arg = pathlib.Path(self.argv0 or "")
        if arg.name in SCRIPT_NAMES and os.sep in str(self.argv0):
            return CheckResult("cpub", OK, {"path": str(arg.absolute())})
        found = self.app_dirs.find_executable(
            "cpub", fallback_dirs=(str(self.app_dirs.home / USER_BIN),))
        if found is None:
            return CheckResult("cpub", WARN, {"path": None})
        return CheckResult("cpub", OK, {"path": found[0]})
