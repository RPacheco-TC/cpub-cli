"""Credential autoload (S1.1): env vars > --env-file > ./.env > user config file.

Never executes anything and never writes; :func:`apply_env` copies the result
into ``os.environ`` where :mod:`.api` reads it.
"""
import os
import re
import sys
import errno
import pathlib


#: The credentials the publisher needs, in the order they are reported.
CRED_VARS = ("CONFLUENCE_BASE_URL", "CONFLUENCE_EMAIL", "CONFLUENCE_API_TOKEN")

_ENV_LINE_RE = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")
_ENV_DQ_RE = re.compile(r'^"((?:[^"\\]|\\.)*)"\s*(?:#.*)?$')
_ENV_SQ_RE = re.compile(r"^'([^']*)'\s*(?:#.*)?$")
_ENV_DQ_ESCAPES = {"n": "\n", "t": "\t", '"': '"', "\\": "\\", "$": "$"}


def parse_env_text(text):
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
            val = re.sub(r"\\(.)", lambda e: _ENV_DQ_ESCAPES.get(e.group(1), e.group(0)), dq.group(1))
        elif (sq := _ENV_SQ_RE.match(val)):
            val = sq.group(1)
        else:
            val = re.split(r"\s+#", val, maxsplit=1)[0].rstrip()
        out[key] = val
    return out


def env_search_paths(env_file=None, cwd=None, config_dir=None, environ=None):
    """Return the credential files :func:`load_env` reads, highest priority first.

    ``--env-file`` (when given), then ``<cwd>/.env``, then
    ``$XDG_CONFIG_HOME/confluence-publisher/.env`` (default
    ``~/.config/confluence-publisher/.env``). Real environment variables beat
    all of them and are not a file, so they are not listed.

    :param env_file: the ``--env-file`` path, or ``None``.
    :param cwd: the directory holding ``./.env`` (current directory when ``None``).
    :param config_dir: override for the user config folder (tests).
    :param environ: the environment to read ``XDG_CONFIG_HOME``/``HOME`` from.
    :rtype: list[pathlib.Path]
    """
    environ = os.environ if environ is None else environ
    paths = []
    if env_file:
        paths.append(pathlib.Path(env_file).expanduser())
    paths.append(pathlib.Path(cwd if cwd is not None else os.getcwd()) / ".env")
    if config_dir is None:
        xdg = environ.get("XDG_CONFIG_HOME")
        base = pathlib.Path(xdg) if xdg else pathlib.Path(
            environ.get("HOME") or os.path.expanduser("~")) / ".config"
        config_dir = base / "confluence-publisher"
    paths.append(pathlib.Path(config_dir) / ".env")
    return paths


def _read_env_file(path):
    """Default file reader for :func:`load_env`: ``(text, st_mode)``, or ``None`` if absent."""
    try:
        return path.read_text(), path.stat().st_mode
    except FileNotFoundError:
        return None


def _warn_stderr(msg):
    print(msg, file=sys.stderr)


def load_env(env_file=None, cwd=None, config_dir=None, environ=None, *,
             read_file=None, warn=None, keys=CRED_VARS):
    """Resolve the credentials, first hit wins **per variable**.

    Order: real environment variables, ``--env-file``, ``./.env``, the user
    config file (see :func:`env_search_paths`). Nothing is executed and nothing
    is written; call :func:`apply_env` to put the result into ``os.environ``.

    :param env_file: the ``--env-file`` path; a missing file is an error.
    :param cwd: the directory holding ``./.env``.
    :param config_dir: override for the user config folder.
    :param environ: the environment mapping (``os.environ`` when ``None``).
    :param read_file: injectable ``path -> (text, st_mode) | None`` (tests).
    :param warn: called with a one-line message when the file that supplied the
        API token is readable by group/others (default: print to stderr).
    :param keys: the variables to resolve.
    :returns: ``{var: (value, source)}`` where ``source`` is ``"environment"``
        or the file path; unresolved variables are absent.
    :rtype: dict[str, tuple[str, str]]
    :raises FileNotFoundError: if ``env_file`` is given and does not exist
        (``.filename`` is the path).
    """
    environ = os.environ if environ is None else environ
    read_file = read_file or _read_env_file
    warn = warn or _warn_stderr
    resolved = {k: (environ[k], "environment") for k in keys if environ.get(k)}
    for i, path in enumerate(env_search_paths(env_file, cwd, config_dir, environ)):
        if len(resolved) == len(keys) and not (env_file and i == 0):
            break                       # still validate an explicit --env-file exists
        got = read_file(path)
        if got is None:
            if env_file and i == 0:
                raise FileNotFoundError(errno.ENOENT, f"--env-file not found: {path}", str(path))
            continue
        text, mode = got
        values = parse_env_text(text)
        for k in keys:
            if k in resolved or not values.get(k):
                continue
            resolved[k] = (values[k], str(path))
            if k == "CONFLUENCE_API_TOKEN" and mode is not None and mode & 0o077:
                warn(f"Warning: {path} holds your API token and is readable by other "
                     f"users — run: chmod 600 {path}")
    return resolved


def apply_env(resolved, environ=None):
    """Copy resolved credentials into the environment where they are not set yet.

    :param resolved: the result of :func:`load_env`.
    :param environ: target mapping (``os.environ`` when ``None``).
    """
    environ = os.environ if environ is None else environ
    for k, (value, _source) in resolved.items():
        if not environ.get(k):
            environ[k] = value


