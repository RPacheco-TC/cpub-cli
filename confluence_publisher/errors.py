"""Exceptions shared by the package (a leaf: imports nothing of ours).

Every expected failure derives from :class:`PublisherError`, so the CLI can
translate them in one place.
"""
import ipaddress
import urllib.parse


class PublisherError(Exception):
    """Base of every error the publisher raises on purpose."""


class MissingCredentials(PublisherError):
    """Some credentials are not set anywhere.

    :param missing: the unset variable names, in :data:`~.config.CRED_VARS` order.
    :param searched: the files searched, highest priority first.
    """
    def __init__(self, missing, searched):
        super().__init__("Missing Confluence credential(s): " + ", ".join(missing))
        self.missing, self.searched = tuple(missing), list(searched)


class SpaceNotFound(PublisherError):
    """No Confluence space has the given key.

    :param key: the ``--space`` key that matched nothing.
    """
    def __init__(self, key):
        super().__init__(f"Space not found for key: {key}")
        self.key = key


class NotAFolder(PublisherError):
    """The doc-set path is not a folder.

    :param path: the path as given.
    """
    def __init__(self, path):
        super().__init__(f"Not a folder: {path}")
        self.path = path


class NotAMarkdownFile(PublisherError):
    """The note path is a file but not a ``.md`` file (or is neither file nor folder).

    :param path: the path as given.
    """
    def __init__(self, path):
        super().__init__(f"Not a Markdown note or a folder: {path}")
        self.path = path


class BadPageRef(PublisherError):
    """A page argument (id or URL) that cannot be used; nothing was sent.

    :param ref: the argument as given.
    :param message: what is wrong, in one sentence.
    :param fix: the one thing to do about it.
    """
    def __init__(self, ref, message, fix):
        super().__init__(message)
        self.ref, self.message, self.fix = ref, message, fix


class FolderPageRef(BadPageRef):
    """The URL points at a Confluence folder, which cannot hold published pages."""


class ShortLinkPageRef(BadPageRef):
    """A ``/wiki/x/...`` short link (resolving it needs a redirect; out of scope)."""


class ForeignHostPageRef(BadPageRef):
    """The URL is on another site than ``CONFLUENCE_BASE_URL``."""


class UnrecognisedPageRef(BadPageRef):
    """Neither a numeric id nor one of the accepted page URL forms."""


class PlanRefused(PublisherError):
    """The plan failed an E0 check; nothing was written."""
    def __init__(self, refusals):
        super().__init__(f"{len(refusals)} page(s) refused")
        self.refusals = refusals


class HandoverRequired(PublisherError):
    """Pages need a hand-over but there is no way to ask (non-interactive, no --adopt)."""
    def __init__(self, items):
        super().__init__(f"{len(items)} page(s) need a hand-over (--adopt)")
        self.items = items


class HandoverDeclined(PublisherError):
    """The user declined the hand-over; nothing was written."""
    def __init__(self, items):
        super().__init__(f"hand-over of {len(items)} page(s) declined")
        self.items = items


class DiagramsUnrenderable(PublisherError):
    """Note mode: the note has Mermaid diagrams and no ``npx`` to render them.

    Raised before any request, so a hotkey republish never swaps a page's
    diagrams for code blocks. Set mode keeps the fallback (with a warning).

    :param note: the note's filename.
    :param count: how many diagrams could not be rendered.
    """
    def __init__(self, note, count):
        super().__init__(f"Diagrams in {note} cannot be rendered (npx not found).")
        self.note, self.count = note, count


class InsecureBaseUrl(PublisherError):
    """A Confluence URL the token may not travel on: refused before any request (B8).

    The rule (D29) is :meth:`allows`: ``https://``, or ``http://`` to a loopback
    IP literal (a local test server). Anything else would send the Basic-auth
    token in clear text.

    :param url: the URL as configured (kept raw on ``.url``).
    :param sources: ``{var: "environment" | path}`` of the credentials, if known.
    ``.scheme``, ``.host`` (hostname[:port], never any ``user:pass@`` part) and
    ``.source`` (where ``CONFLUENCE_BASE_URL`` came from, or ``None``) are set.
    """

    @staticmethod
    def allows(url) -> bool:
        """``https://``; or ``http://`` whose host is a loopback IP **literal**
        (127.0.0.0/8, ``[::1]``). The name ``localhost`` is refused: a hosts
        file can point it anywhere."""
        parts = urllib.parse.urlsplit(str(url))
        if parts.scheme == "https":
            return True
        if parts.scheme != "http":
            return False
        try:
            return ipaddress.ip_address(parts.hostname or "").is_loopback
        except ValueError:
            return False

    def __init__(self, url, sources=None):
        self.url = str(url)
        parts = urllib.parse.urlsplit(str(url))
        self.scheme = parts.scheme or ""
        try:
            port = parts.port
        except ValueError:
            port = None
        self.host = (parts.hostname or "") + (f":{port}" if port else "")
        self.sources = dict(sources or {})
        self.source = self.sources.get("CONFLUENCE_BASE_URL")
        found = f"{self.scheme}://{self.host}" if self.scheme else "no scheme"
        super().__init__(f"CONFLUENCE_BASE_URL must start with https:// (found {found}).")
        # .url is the raw value: it may hold a user:pass@ part, so print .host instead


class ConfigExists(PublisherError):
    """``cpub setup``: the user config file exists and overwriting was not confirmed.

    :param path: the existing file.
    """
    def __init__(self, path):
        super().__init__(f"{path} already exists.")
        self.path = path


class PlanExecutionError(PublisherError):
    """A write failed mid-run; ``report`` says what was and was not written."""
    def __init__(self, report):
        super().__init__(f"write failed: {report.error}")
        self.report = report


class CliError(PublisherError):
    """An expected failure, shown as one sentence plus a ``Fix:`` line (S3.1).

    :param sentence: what went wrong, in one plain sentence.
    :param fix: the one thing to do about it.
    :param detail: optional raw detail (e.g. Confluence's error body), shown
        underneath, indented.
    """

    def __init__(self, sentence, fix, detail=None):
        super().__init__(sentence)
        self.sentence, self.fix, self.detail = sentence, fix, detail

    def __str__(self):
        out = f"Error: {self.sentence}\nFix: {self.fix}"
        if self.detail:
            out += "\n" + "\n".join(("  " + ln) if ln.strip() else ""
                                      for ln in str(self.detail).splitlines())
        return out


