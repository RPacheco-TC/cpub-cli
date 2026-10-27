"""Page arguments: a numeric id or a Confluence page URL (S1.2). Pure, no network.

:meth:`PageRef.parse` accepts what a developer copies from the browser and
rejects, before any request, what cannot be published to: a folder, a short
link, an unrecognised URL, or a URL on another site.
"""
import dataclasses
import re
import urllib.parse

from .errors import (FolderPageRef, ForeignHostPageRef, ShortLinkPageRef,
                     UnrecognisedPageRef)

#: Shown when a URL is not understood.
ACCEPTED_FORMS = (
    "a numeric page id, e.g. 123456",
    "…/wiki/spaces/KEY/pages/ID[/Title]",
    "…/wiki/spaces/KEY/pages/edit-v2/ID",
    "…/wiki/pages/viewpage.action?pageId=ID",
)

_ID_RE = re.compile(r"^\d+$")
_PAGE_RE = re.compile(r"^/wiki/spaces/([^/]+)/pages/(?:edit-v2/)?(\d+)(?:/[^/]*)?/?$")
_FOLDER_RE = re.compile(r"^/wiki/spaces/([^/]+)/folder/(\d+)")
_VIEWPAGE = "/wiki/pages/viewpage.action"


def _forms():
    return "Use " + "; or ".join(ACCEPTED_FORMS) + "."


@dataclasses.dataclass(frozen=True)
class PageRef:
    """A page the user pointed at.

    :param page_id: the numeric page id (as a string).
    :param space_key: the space key from the URL, or ``None`` (bare id or
        ``viewpage.action``); the page's real space is read from Confluence.
    """
    page_id: str
    space_key: str | None = None

    @classmethod
    def parse(cls, text, base_url) -> "PageRef":
        """Parse a page argument.

        Query strings and ``#fragments`` are ignored (except ``pageId``).

        :param text: the argument as typed (id or URL).
        :param base_url: ``CONFLUENCE_BASE_URL``; a URL must be on its host.
        :raises FolderPageRef: for a folder URL.
        :raises ShortLinkPageRef: for a ``/wiki/x/...`` link.
        :raises ForeignHostPageRef: for a URL on another site.
        :raises UnrecognisedPageRef: for anything else.
        """
        ref = str(text).strip()
        if _ID_RE.match(ref):
            return cls(ref)
        url = urllib.parse.urlsplit(ref)
        if url.scheme not in ("http", "https") or not url.netloc:
            raise UnrecognisedPageRef(
                ref, f"{ref!r} is neither a page id nor a Confluence page URL.", _forms())
        site = urllib.parse.urlsplit(base_url).hostname
        if (url.hostname or "").lower() != (site or "").lower():
            raise ForeignHostPageRef(
                ref, f"That URL is on {url.hostname}, but CONFLUENCE_BASE_URL is on {site}.",
                "Use a page on your configured site, or point CONFLUENCE_BASE_URL at that one.")
        path = url.path
        if _FOLDER_RE.match(path):
            raise FolderPageRef(
                ref, "That URL is a folder; publish under a page instead.",
                "Open a page (not a folder) in Confluence and copy its URL.")
        if path.startswith("/wiki/x/"):
            raise ShortLinkPageRef(
                ref, "Short links (/wiki/x/…) are not supported.",
                "Open the page and copy the full URL from the address bar. " + _forms())
        m = _PAGE_RE.match(path)
        if m:
            return cls(m.group(2), urllib.parse.unquote(m.group(1)))
        if path == _VIEWPAGE:
            page_id = urllib.parse.parse_qs(url.query).get("pageId", [""])[0]
            if _ID_RE.match(page_id):
                return cls(page_id)
        raise UnrecognisedPageRef(ref, "That URL is not a Confluence page URL.", _forms())
