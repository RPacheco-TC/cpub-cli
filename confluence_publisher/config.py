"""Credentials (S1.1): env vars > --env-file > ./.env > user config file.

:class:`CredentialLoader` and :class:`AppDirs` are the **only** places in the
package that read ``os.environ``. The loader returns an immutable
:class:`Credentials` that is passed to :class:`~.api.ConfluenceClient`.
Nothing here executes anything; the only writer is :class:`CredentialWriter`
(``cpub setup``, S1.4), through an injectable file system.
"""
import os
import re
import sys
import errno
import base64
import pathlib
import tempfile
import contextlib
import dataclasses

from .errors import ConfigExists, InsecureBaseUrl, MissingCredentials


#: Where Node.js usually lives when an app (e.g. Obsidian) starts cpub with a bare PATH.
NODE_FALLBACK_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")

#: The credentials the publisher needs, in the order they are reported.
CRED_VARS = ("CONFLUENCE_BASE_URL", "CONFLUENCE_EMAIL", "CONFLUENCE_API_TOKEN")

_ENV_LINE_RE = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")
_ENV_DQ_RE = re.compile(r'^"((?:[^"\\]|\\.)*)"\s*(?:#.*)?$')
_ENV_SQ_RE = re.compile(r"^'([^']*)'\s*(?:#.*)?$")
_ENV_DQ_ESCAPES = {"n": "\n", "t": "\t", '"': '"', "\\": "\\", "$": "$"}


@dataclasses.dataclass(frozen=True)
class Credentials:
    """The three values that authenticate every Confluence call.

    :param base_url: site URL ending in ``/wiki`` (a trailing ``/`` is dropped); it
        must pass :meth:`~.errors.InsecureBaseUrl.allows` (``https://``, or ``http://``
        to a loopback IP literal) or :class:`~.errors.InsecureBaseUrl` is raised (B8).
    :param email: the Atlassian account email.
    :param token: the API token; never shown by ``repr`` or :meth:`masked`.
    :param sources: ``{var: "environment" | file path}``, where each value came from.
    """
    base_url: str
    email: str
    token: str = dataclasses.field(repr=False)
    sources: dict = dataclasses.field(default_factory=dict, compare=False, repr=False)

    def __post_init__(self):
        object.__setattr__(self, "base_url", self.base_url.rstrip("/"))
        if not InsecureBaseUrl.allows(self.base_url):   # B8/D29: none can be insecure
            raise InsecureBaseUrl(self.base_url, self.sources)

    def auth_header(self) -> str:
        """The HTTP Basic ``Authorization`` header value."""
        return "Basic " + base64.b64encode(f"{self.email}:{self.token}".encode()).decode()

    @property
    def token_source(self) -> str:
        """Where the token came from: ``"environment"`` or the file path."""
        return self.sources.get("CONFLUENCE_API_TOKEN", "environment")

    def mask(self, text: str) -> str:
        """Replace the token, if it appears in ``text``, with ``****`` (AC-X.6)."""
        return text.replace(self.token, "****") if self.token and len(self.token) >= 4 else text

    def masked(self) -> str:
        """A one-line summary that is safe to print."""
        return f"{self.email} @ {self.base_url} (token ****, from {self.token_source})"


def _safe(check, path):
    """``check(path)``, with any ``OSError`` (unreadable, sandboxed) meaning no."""
    try:
        return check(path)
    except OSError:
        return False


class AppDirs:
    """The per-user folders, from the XDG variables (the only place they are read).

    :param environ: the environment mapping (``os.environ`` when ``None``).
    """

    APP = "confluence-publisher"

    def __init__(self, environ=None):
        self.environ = os.environ if environ is None else environ

    @property
    def home(self) -> pathlib.Path:
        """The user's home folder (``$HOME``)."""
        return pathlib.Path(self.environ.get("HOME") or os.path.expanduser("~"))

    def _base(self, var, default):
        xdg = self.environ.get(var)
        if xdg:
            return pathlib.Path(xdg)
        return self.home / default

    @property
    def config_dir(self) -> pathlib.Path:
        """``$XDG_CONFIG_HOME/confluence-publisher`` (default ``~/.config/…``)."""
        return self._base("XDG_CONFIG_HOME", ".config") / self.APP

    @property
    def cache_dir(self) -> pathlib.Path:
        """``$XDG_CACHE_HOME/confluence-publisher`` (default ``~/.cache/…``)."""
        return self._base("XDG_CACHE_HOME", ".cache") / self.APP

    @property
    def note_img_dir(self) -> pathlib.Path:
        """Where note mode renders Mermaid PNGs, outside the vault (S2.6)."""
        return self.cache_dir / "img"

    def find_executable(self, name, fallback_dirs=NODE_FALLBACK_DIRS, is_exe=None):
        """Find ``name`` on ``PATH``, then in ``fallback_dirs`` (Obsidian's bare PATH).

        :param is_exe: ``Path -> bool`` (a real executable-file check by default);
            an ``OSError`` from it (e.g. an unreadable folder) skips that candidate.
        :returns: ``(absolute path, child_env)``, or ``None`` when not found.
            ``child_env`` is ``None`` (inherit) for a ``PATH`` hit; for a fallback
            hit it is the environment with that folder put first on ``PATH``, so
            the program's ``#!/usr/bin/env node`` finds the ``node`` beside it.
        :rtype: tuple[str, dict | None] | None
        """
        is_exe = is_exe or (lambda p: p.is_file() and os.access(p, os.X_OK))
        path = self.environ.get("PATH") or ""
        on_path = [d for d in path.split(os.pathsep) if d]
        for d in on_path + [d for d in fallback_dirs if d not in on_path]:
            candidate = pathlib.Path(d) / name
            if _safe(is_exe, candidate):
                if d in on_path:
                    return str(candidate), None
                env = dict(self.environ)
                env["PATH"] = os.pathsep.join([d] + on_path)
                return str(candidate), env
        return None


class CredentialLoader:
    """Resolve the credentials, first hit wins **per variable**.

    Order: real environment variables, ``--env-file``, ``./.env``, then the
    user config file ``$XDG_CONFIG_HOME/confluence-publisher/.env`` (default
    ``~/.config/confluence-publisher/.env``).

    :param environ: the environment mapping (``os.environ`` when ``None``).
    :param cwd: the directory holding ``./.env`` (current directory when ``None``).
    :param config_dir: override for the user config folder (tests).
    :param read_file: ``path -> (text, st_mode) | None`` (tests); the default
        reads the file and returns ``None`` when it does not exist.
    :param warn: called with a one-line message when the file that supplied the
        API token is readable by group/others (default: print to stderr).
    """

    def __init__(self, *, environ=None, cwd=None, config_dir=None, read_file=None, warn=None):
        self.environ = os.environ if environ is None else environ
        self.cwd = cwd
        self.config_dir = config_dir
        self.read_file = read_file or self._read_file
        self.warn = warn or self._warn_stderr

    @staticmethod
    def parse(text):
        """Parse ``.env`` text into a dict, without executing anything.

        Accepts ``KEY=VALUE``, ``export KEY=VALUE``, blank lines, ``#`` comments
        (whole-line, or after an unquoted value / closing quote), and single- or
        double-quoted values. Single quotes are literal; double quotes understand
        ``\\"``, ``\\\\``, ``\\n``, ``\\t`` and ``\\$``. Lines that are not
        assignments are ignored. Later assignments of a key win, as in a shell.

        :param text: the file content.
        :rtype: dict[str, str]
        """
        out = {}
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            m = _ENV_LINE_RE.match(line)
            if not m:
                continue
            key, val = m.group(1), m.group(2).strip()
            if (dq := _ENV_DQ_RE.match(val)):
                val = re.sub(r"\\(.)",
                             lambda e: _ENV_DQ_ESCAPES.get(e.group(1), e.group(0)),
                             dq.group(1))
            elif (sq := _ENV_SQ_RE.match(val)):
                val = sq.group(1)
            else:
                val = re.split(r"\s+#", val, maxsplit=1)[0].rstrip()
            out[key] = val
        return out

    def search_paths(self, env_file=None):
        """Return the credential files :meth:`resolve` reads, highest priority first.

        ``--env-file`` (when given), then ``<cwd>/.env``, then
        ``$XDG_CONFIG_HOME/confluence-publisher/.env`` (default
        ``~/.config/confluence-publisher/.env``). Real environment variables beat
        all of them and are not a file, so they are not listed.

        :param env_file: the ``--env-file`` path, or ``None``.
        :rtype: list[pathlib.Path]
        """
        paths = []
        if env_file:
            paths.append(pathlib.Path(env_file).expanduser())
        paths.append(pathlib.Path(self.cwd if self.cwd is not None else os.getcwd()) / ".env")
        config_dir = self.config_dir
        if config_dir is None:
            config_dir = AppDirs(self.environ).config_dir
        paths.append(pathlib.Path(config_dir) / ".env")
        return paths

    def resolve(self, env_file=None, keys=CRED_VARS):
        """Resolve the credentials, first hit wins **per variable**.

        Order: real environment variables, ``--env-file``, ``./.env``, the user
        config file (see :meth:`search_paths`). Nothing is executed and nothing
        is written.

        :param env_file: the ``--env-file`` path; a missing file is an error.
        :param keys: the variables to resolve.
        :returns: ``{var: (value, source)}`` where ``source`` is ``"environment"``
            or the file path; unresolved variables are absent.
        :rtype: dict[str, tuple[str, str]]
        :raises FileNotFoundError: if ``env_file`` is given and does not exist
            (``.filename`` is the path).
        """
        environ = self.environ
        resolved = {k: (environ[k], "environment") for k in keys if environ.get(k)}
        for i, path in enumerate(self.search_paths(env_file)):
            if len(resolved) == len(keys) and not (env_file and i == 0):
                break                       # still validate an explicit --env-file exists
            got = self.read_file(path)
            if got is None:
                if env_file and i == 0:
                    raise FileNotFoundError(errno.ENOENT, f"--env-file not found: {path}",
                                            str(path))
                continue
            text, mode = got
            values = self.parse(text)
            for k in keys:
                if k in resolved or not values.get(k):
                    continue
                resolved[k] = (values[k], str(path))
                if k == "CONFLUENCE_API_TOKEN" and mode is not None and mode & 0o077:
                    self.warn(f"Warning: {path} holds your API token and is readable by other "
                              f"users — run: chmod 600 {path}")
        return resolved

    def load(self, env_file=None) -> Credentials:
        """Resolve and return the :class:`Credentials`.

        :raises FileNotFoundError: if ``env_file`` is given and does not exist.
        :raises MissingCredentials: naming the unset variables and the files searched.
        :raises InsecureBaseUrl: if ``CONFLUENCE_BASE_URL`` is not allowed (B8/D29).
        """
        resolved = self.resolve(env_file)
        missing = tuple(v for v in CRED_VARS if v not in resolved)
        if missing:
            raise MissingCredentials(missing, self.search_paths(env_file))
        url, email, token = (resolved[v][0] for v in CRED_VARS)
        return Credentials(url, email, token, {v: src for v, (_, src) in resolved.items()})

    @staticmethod
    def _read_file(path):
        try:
            return path.read_text(), path.stat().st_mode
        except FileNotFoundError:
            return None

    @staticmethod
    def _warn_stderr(msg):
        print(msg, file=sys.stderr)


class PrivateFiles:
    """The real file operations :class:`CredentialWriter` needs (tests inject a fake)."""

    @staticmethod
    def exists(path) -> bool:
        return pathlib.Path(path).exists()

    @staticmethod
    def make_private_dir(path):
        """Create ``path`` (and parents) and set it to ``700``."""
        path = pathlib.Path(path)
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path, 0o700)

    @staticmethod
    def write_private(path, text):
        """Write ``text`` atomically as a ``600`` file: a temp file beside it, then
        ``os.replace``, so a crash never leaves a half-written credentials file."""
        path = pathlib.Path(path)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")   # mode 600
        try:
            with os.fdopen(fd, "w") as f:
                os.fchmod(f.fileno(), 0o600)
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise


class CredentialWriter:
    """Save verified credentials to the user config file (``cpub setup``, S1.4).

    The file is ``AppDirs().config_dir / ".env"`` (lookup path 4), in the
    ``KEY="value"`` format :class:`CredentialLoader` reads; folder ``700``,
    file ``600``, written atomically.

    :param app_dirs: an :class:`AppDirs` (for ``config_dir``); a real one when ``None``.
    :param files: the file operations (default :class:`PrivateFiles`).
    :param today: date written in the header comment (``None``: no date).
    """

    HEADER = "# Confluence credentials for cpub, written by `cpub setup`. Keep it private."

    def __init__(self, app_dirs=None, files=None, today=None):
        self.app_dirs = app_dirs or AppDirs()
        self.files = files or PrivateFiles()
        self.today = today

    @property
    def path(self) -> pathlib.Path:
        return pathlib.Path(self.app_dirs.config_dir) / ".env"

    def exists(self) -> bool:
        """Whether a config file is already there (ask before overwriting it)."""
        return self.files.exists(self.path)

    def render(self, credentials) -> str:
        """The file text; every value double-quoted so the loader reads it back exactly.

        :raises ValueError: for a value containing a line break.
        """
        lines = [self.HEADER + (f" ({self.today})" if self.today else "")]
        values = (credentials.base_url, credentials.email, credentials.token)
        for var, value in zip(CRED_VARS, values):
            if "\n" in value or "\r" in value:
                raise ValueError(f"{var} contains a line break")
            quoted = value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")
            lines.append(f'{var}="{quoted}"')
        return "\n".join(lines) + "\n"

    def write(self, credentials, identity, overwrite=False) -> pathlib.Path:
        """Write the file; return its path.

        :param credentials: the :class:`Credentials` to save.
        :param identity: the :meth:`~.api.ConfluenceClient.whoami` result for
            them: credentials that did not authenticate are never saved.
        :param overwrite: replace an existing file (the user confirmed it).
        :raises ValueError: if ``identity`` is not authenticated.
        :raises ConfigExists: if the file exists and ``overwrite`` is false.
        """
        if not (identity or {}).get("authenticated"):
            raise ValueError("refusing to save credentials that did not authenticate")
        text = self.render(credentials)
        if not overwrite and self.exists():
            raise ConfigExists(self.path)
        self.files.make_private_dir(self.path.parent)
        self.files.write_private(self.path, text)
        return self.path
