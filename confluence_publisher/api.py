"""Confluence Cloud REST client: pages, attachments, content properties, identity.

Credentials come from the environment (see :mod:`.config`). v2 API for pages,
spaces, ancestors, properties and attachment listing; the v1 endpoint for
multipart attachment upload and for ``user/current``.
"""
import os
import json
import base64
import pathlib
import datetime
import urllib.request, urllib.error, urllib.parse


def _cfg():
    """Read the Confluence credentials from the environment.

    :returns: a ``(base_url, email, api_token)`` tuple.
    :rtype: tuple[str, str, str]
    :raises KeyError: if a required environment variable is unset.
    """
    return (os.environ["CONFLUENCE_BASE_URL"].rstrip("/"),
            os.environ["CONFLUENCE_EMAIL"], os.environ["CONFLUENCE_API_TOKEN"])


def _auth_header():
    """Build the HTTP Basic ``Authorization`` header value from the credentials.

    :returns: the ``Basic <base64>`` header value.
    :rtype: str
    """
    _, email, token = _cfg()
    return "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()


def _req(method, url, data=None, headers=None):
    """Make an authenticated request to the Confluence REST API.

    :param method: the HTTP method (``GET``, ``POST``, ``PUT``).
    :param url: the absolute request URL.
    :param data: the raw request body, or ``None``.
    :param headers: extra request headers to merge in, or ``None``.
    :returns: a ``(status_code, parsed_json)`` pair (``{}`` if the body is empty).
    :rtype: tuple[int, dict]
    """
    h = {"Authorization": _auth_header(), "Accept": "application/json"}
    if headers:
        h.update(headers)
    r = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            body = resp.read().decode()
            return resp.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as e:
        # Confluence puts the real reason (e.g. "parent is not a page") in the
        # response body, which urllib hides behind a bare "HTTP Error 400".
        # Stash it on the exception and re-raise so callers that catch
        # HTTPError (e.g. existing_attachment_names) keep working, while
        # main() can print the detail.
        try:
            e.detail = e.read().decode(errors="replace")
        except Exception:
            e.detail = ""
        raise


def get_page(page_id):
    """Fetch a page, including its storage-format body.

    :param page_id: the page id.
    :returns: the page object.
    :rtype: dict
    """
    base, *_ = _cfg()
    return _req("GET", f"{base}/api/v2/pages/{page_id}?body-format=storage")[1]


def create_page(space_id, parent_id, title, storage):
    """Create a page under a parent.

    :param space_id: numeric space id.
    :param parent_id: id of the parent page.
    :param title: the new page title.
    :param storage: the storage-format body.
    :returns: the created page object.
    :rtype: dict
    """
    base, *_ = _cfg()
    payload = {"spaceId": space_id, "status": "current", "title": title,
               "parentId": parent_id,
               "body": {"representation": "storage", "value": storage}}
    return _req("POST", f"{base}/api/v2/pages",
                json.dumps(payload).encode(), {"Content-Type": "application/json"})[1]


def update_page(page_id, title, storage, version_message="", expected_version=None):
    """Update a page's body, bumping its version number.

    :param page_id: the page id.
    :param title: the page title.
    :param storage: the new storage-format body.
    :param version_message: an optional version comment.
    :param expected_version: the version the caller saw (the plan's); the update
        is sent as that number + 1 with no re-read, so a page edited in between
        is rejected by Confluence (409) instead of silently overwritten. When
        ``None`` the current version is fetched first.
    :returns: the updated page object.
    :rtype: dict
    """
    base, *_ = _cfg()
    current = (get_page(page_id)["version"]["number"] if expected_version is None
               else int(expected_version))
    payload = {"id": str(page_id), "status": "current", "title": title,
               "body": {"representation": "storage", "value": storage},
               "version": {"number": current + 1, "message": version_message}}
    return _req("PUT", f"{base}/api/v2/pages/{page_id}",
                json.dumps(payload).encode(), {"Content-Type": "application/json"})[1]


def upload_attachment(page_id, filepath, filename):
    """Upload a local file as a page attachment (v1 multipart endpoint).

    .. warning::

       Re-uploading an existing filename returns HTTP 400, so callers should
       skip filenames already present on the page (see
       :func:`existing_attachment_names`).

    :param page_id: the page id.
    :param filepath: path to the local file.
    :param filename: the attachment filename to use.
    :returns: the API response object.
    :rtype: dict
    """
    base, *_ = _cfg()
    boundary = "----confluencepublishboundary"
    data = pathlib.Path(filepath).read_bytes()
    body = b"".join([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode(),
        b"Content-Type: application/octet-stream\r\n\r\n", data, b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ])
    return _req("POST", f"{base}/rest/api/content/{page_id}/child/attachment", body,
                {"Content-Type": f"multipart/form-data; boundary={boundary}",
                 "X-Atlassian-Token": "no-check"})[1]


def existing_attachment_names(page_id):
    """List the filenames already attached to a page.

    Used to skip re-uploading content-hashed PNGs, which would otherwise return
    HTTP 400 for a duplicate filename.

    :param page_id: the page id.
    :returns: the set of attachment filenames.
    :rtype: set[str]
    """
    base, *_ = _cfg()
    try:
        _, d = _req("GET", f"{base}/api/v2/pages/{page_id}/attachments?limit=250")
    except urllib.error.HTTPError:
        return set()
    return {a.get("title") for a in d.get("results", [])}


def _iter_pages(url):
    """Yield every item of a paginated v2 collection, following ``_links.next``.

    The Confluence v2 API returns results one page at a time with a cursor in
    ``_links.next``; reading only the first page silently misses everything
    beyond it. ``next`` is a path relative to the host, so it is joined onto the
    configured base URL.

    :param url: the absolute URL of the first page of results.
    :returns: an iterator over all result objects across every page.
    :rtype: Iterator[dict]
    """
    base, *_ = _cfg()
    while url:
        _, d = _req("GET", url)
        for item in d.get("results", []):
            yield item
        nxt = d.get("_links", {}).get("next")
        url = urllib.parse.urljoin(base, nxt) if nxt else None


def find_page_id_by_title(space_id, title):
    """Find a page id by exact title within a space.

    Pages through *all* results (not just the first 250), so an existing page is
    reliably matched no matter how large the space — which is what keeps
    re-publishing idempotent and prevents a spurious ``CREATE`` (and its HTTP
    400) when a same-titled page already exists.

    :param space_id: numeric space id.
    :param title: the exact page title to match.
    :returns: the page id, or ``None`` if no page has that title.
    :rtype: str | None
    """
    base, *_ = _cfg()
    for p in _iter_pages(f"{base}/api/v2/spaces/{space_id}/pages?limit=250"):
        if p.get("title") == title:
            return p["id"]
    return None


def whoami():
    """Check who the credentials authenticate as (one read-only v1 call).

    Confluence Cloud does not reject a bad email/token with 401 on most reads:
    it serves the request anonymously, so a private page then looks like a 404.
    Calling this before planning tells "bad credentials" apart from "page not
    found". With bad credentials Cloud answers ``200`` with ``type:
    "anonymous"``, or ``401``/``403``.

    :returns: ``authenticated`` (bool), ``type``, ``accountId``,
        ``displayName``, ``email`` and ``status`` (the HTTP status).
    :rtype: dict
    :raises urllib.error.HTTPError: for any status other than 2xx/401/403.
    """
    base, *_ = _cfg()
    try:
        status, d = _req("GET", f"{base}/rest/api/user/current")
    except urllib.error.HTTPError as e:
        if e.code not in (401, 403):
            raise
        return {"authenticated": False, "type": None, "accountId": None,
                "displayName": None, "email": None, "status": e.code}
    kind = d.get("type")
    return {"authenticated": kind != "anonymous" and bool(d.get("accountId")),
            "type": kind, "accountId": d.get("accountId"),
            "displayName": d.get("displayName"), "email": d.get("email"), "status": status}


def resolve_space_id(space_key):
    """Resolve a space key to its numeric space id.

    :param space_key: the space key (e.g. ``~yourname`` or ``ENG``).
    :returns: the numeric space id.
    :rtype: str
    :raises SystemExit: if no space matches the key.
    """
    base, *_ = _cfg()
    _, d = _req("GET", f"{base}/api/v2/spaces?keys={urllib.parse.quote(space_key)}")
    results = d.get("results", [])
    if not results:
        raise SystemExit(f"Space not found for key: {space_key}")
    return results[0]["id"]


def space_title_index(space_id):
    """Map every page title in a space to its page id, in one paginated scan.

    Same listing and first-match rule as :func:`find_page_id_by_title`, read
    once per run instead of once per page.

    :param space_id: numeric space id.
    :rtype: dict[str, str]
    """
    base, *_ = _cfg()
    index = {}
    for p in _iter_pages(f"{base}/api/v2/spaces/{space_id}/pages?limit=250"):
        index.setdefault(p.get("title"), p["id"])
    return index


def get_page_info(page_id):
    """Fetch a page's metadata (no body): what the plan shows and the fence checks.

    :param page_id: the page id.
    :returns: ``id``, ``title``, ``spaceId``, ``parentId``, ``status``,
        ``version`` (int), ``last_edited`` (ISO time of the current version) and
        ``webui`` (absolute page URL, or ``None``).
    :rtype: dict
    """
    base, *_ = _cfg()
    d = _req("GET", f"{base}/api/v2/pages/{page_id}")[1]
    ver = d.get("version") or {}
    webui = (d.get("_links") or {}).get("webui")
    return {"id": str(d.get("id", page_id)), "title": d.get("title"),
            "spaceId": None if d.get("spaceId") is None else str(d["spaceId"]),
            "parentId": None if d.get("parentId") is None else str(d["parentId"]),
            "status": d.get("status"), "version": ver.get("number"),
            "last_edited": ver.get("createdAt"),
            "webui": base + webui if webui else None}


def get_ancestor_ids(page_id):
    """Return the ids of every ancestor of a page (v2 ``ancestors``, all pages).

    :param page_id: the page id.
    :rtype: list[str]
    """
    base, *_ = _cfg()
    return [str(a["id"]) for a in _iter_pages(f"{base}/api/v2/pages/{page_id}/ancestors?limit=250")]


def is_descendant(page_id, ancestor_id):
    """Whether ``page_id`` is a **strict** descendant of ``ancestor_id``.

    A page is never its own descendant, so a doc titled like the parent can
    never overwrite the parent (AC-0.2.1).

    :rtype: bool
    """
    if str(page_id) == str(ancestor_id):
        return False
    return str(ancestor_id) in get_ancestor_ids(page_id)


#: Content-property key of the ownership marker (S0.3). An accident guard,
#: not access control: anyone who can edit the page can remove it (D17).
MARKER_KEY = "cpub-managed"
MARKER_TOOL = "obx-confluence-obsidian-publisher"


def get_marker(page_id):
    """Read the ``cpub-managed`` content property of a page.

    :returns: ``(value, property_id, version)``; all ``None`` when the page has
        no marker (hand-written, or published by v1.1).
    :rtype: tuple[dict | None, str | None, int | None]
    """
    base, *_ = _cfg()
    _, d = _req("GET", f"{base}/api/v2/pages/{page_id}/properties?key={MARKER_KEY}")
    for p in d.get("results", []):
        if p.get("key") == MARKER_KEY:
            value = p.get("value")
            if isinstance(value, str):          # tolerate a JSON string value
                try:
                    value = json.loads(value)
                except ValueError:
                    value = None
            return (value if isinstance(value, dict) else None,
                    str(p["id"]), (p.get("version") or {}).get("number"))
    return None, None, None


def set_marker(page_id, source, *, today=None, existing=None):
    """Mark a page as published by this tool from ``source``.

    Two calls are needed: v2 ``POST /pages`` cannot carry content properties,
    so the marker is written after the page exists (Q9). An existing marker is
    replaced in place with a version bump.

    :param page_id: the page id.
    :param source: the source id (prefixed title in set mode).
    :param today: ISO date recorded as ``since`` (today when ``None``).
    :param existing: ``(property_id, version)`` of the current marker, if any.
    :returns: the marker value written.
    :rtype: dict
    """
    base, *_ = _cfg()
    value = {"source": source, "tool": MARKER_TOOL,
             "since": today or datetime.date.today().isoformat()}
    hdr = {"Content-Type": "application/json"}
    if existing and existing[0]:
        prop_id, version = existing
        payload = {"key": MARKER_KEY, "value": value,
                   "version": {"number": (version or 0) + 1}}
        _req("PUT", f"{base}/api/v2/pages/{page_id}/properties/{prop_id}",
             json.dumps(payload).encode(), hdr)
    else:
        _req("POST", f"{base}/api/v2/pages/{page_id}/properties",
             json.dumps({"key": MARKER_KEY, "value": value}).encode(), hdr)
    return value


