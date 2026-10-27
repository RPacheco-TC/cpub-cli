#!/usr/bin/env python3
"""Publish a folder of Markdown docs to Confluence Cloud.

Publishes an Obsidian-style Markdown doc set as a page tree under one parent
page. Reuses :mod:`md_to_confluence` for Markdown-to-HTML, then post-processes
into valid Confluence storage format (code macros; info/note/tip/warning panels;
autolink and ampersand fixes; ``[[wikilinks]]`` to page links), renders Mermaid
to PNG and uploads the images as attachments, validates each body as XML, and
creates or updates pages idempotently (matched by title).

Safe by construction: it only ever creates or updates pages *under* the parent
you pass, and never deletes anything.

Usage
-----
.. code-block:: console

    python3 confluence_publish.py DOCS_DIR --space KEY --parent PAGE_ID [--dry-run]

``DOCS_DIR``
    folder of ``NN - Title.md`` files, with an optional ``In Situ/`` subfolder.
``--space KEY``
    Confluence space key (``~yourname`` for a personal space, or e.g. ``ENG``).
``--parent PAGE_ID``
    id of the page to publish everything under.
``--dry-run``
    print the CREATE/UPDATE plan and write nothing.

Environment
-----------
Credentials are read from the environment only (never commit them):

``CONFLUENCE_BASE_URL``
    e.g. ``https://your-company.atlassian.net/wiki``
``CONFLUENCE_EMAIL``
    the Atlassian account email.
``CONFLUENCE_API_TOKEN``
    an API token.

.. note::

   Not yet implemented (contributions welcome): a ``state.json`` page map (this
   matches by title), content-hash skip of unchanged pages, exact heading-anchor
   links, and ``--prune`` of pages whose source was removed.
"""
import os, re, sys, json, base64, importlib.util, pathlib, argparse
import html as ihtml
import urllib.request, urllib.error, urllib.parse
import xml.etree.ElementTree as ET

# --- interpreter guard -------------------------------------------------------
# Must run before md_to_confluence.py is loaded and before any annotated def:
# both use 3.10+ annotation syntax evaluated at definition time, so an older
# interpreter would otherwise die with a TypeError traceback. Keep everything
# above this point parseable by old Pythons so the user gets this one line.
MIN_PYTHON = (3, 12)


def _check_python(version_info=None):
    """Exit with a one-line message when the interpreter is older than :data:`MIN_PYTHON`.

    :param version_info: version to check (``sys.version_info`` when ``None``);
        injectable so tests can exercise the guard without another interpreter.
    :raises SystemExit: if the version is below the minimum.
    """
    v = sys.version_info if version_info is None else version_info
    if tuple(v[:2]) < MIN_PYTHON:
        raise SystemExit(
            "confluence_publish requires Python %d.%d+ (found %d.%d.%d)"
            % (MIN_PYTHON + tuple(v[:3])))


_check_python()

HERE = pathlib.Path(__file__).resolve().parent

# --- reuse the existing converter for the bulk Markdown -> HTML -------------
_spec = importlib.util.spec_from_file_location("mdc", HERE / "md_to_confluence.py")
mdc = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(mdc)

# --------------------------------------------------------------------------- #
#  Storage-format transform (Markdown -> Confluence storage XHTML)
# --------------------------------------------------------------------------- #

# Obsidian callout type -> Confluence macro (info=blue, note=yellow, tip=green, warning=red)
CALLOUT_MACRO = {
    "info": "info", "note": "info", "abstract": "info", "summary": "info",
    "tldr": "info", "question": "info", "faq": "info", "quote": "info",
    "cite": "info", "example": "info", "todo": "info",
    "tip": "tip", "hint": "tip", "success": "tip", "check": "tip", "done": "tip",
    "important": "note",
    "warning": "warning", "caution": "warning", "attention": "warning",
    "danger": "warning", "error": "warning", "failure": "warning",
    "fail": "warning", "bug": "warning",
}

_CALLOUT_RE = re.compile(r"^>\s*\[!(\w+)\][+-]?\s*(.*)$")


def _is_fence(line: str) -> bool:
    """Return whether ``line`` opens or closes a fenced code block.

    The single fence rule shared by every pre-conversion extractor
    (:func:`_extract_toc`, :func:`_extract_wikilinks`, :func:`_extract_callouts`),
    so they agree on what is code. Content inside a fence is source text, not
    document structure: swapping it for a placeholder token would leave the
    token in the published code block, because the post-conversion restore
    never matches inside ``<pre><code>``. Only backtick fences count — the bulk
    converter does not treat ``~~~`` or indented blocks as code either.

    :param line: one line of Markdown (a trailing newline is allowed).
    :type line: str
    :rtype: bool
    """
    return line.lstrip().startswith("```")


def _extract_callouts(md: str):
    """Pull callout blocks out of the Markdown.

    Each Obsidian callout (``> [!type] ...``) becomes a Confluence panel macro;
    in the returned Markdown it is replaced by a placeholder paragraph that the
    bulk converter passes through untouched.

    :param md: the source Markdown.
    :type md: str
    :returns: a ``(markdown_with_tokens, macros)`` pair, where ``macros`` maps
        each placeholder token to its Confluence ``ac:structured-macro`` HTML.
    :rtype: tuple[str, dict[str, str]]
    """
    lines = md.split("\n")
    out, macros, i = [], {}, 0
    in_fence = False
    while i < len(lines):
        if _is_fence(lines[i]):
            in_fence = not in_fence
            out.append(lines[i]); i += 1; continue
        m = None if in_fence else _CALLOUT_RE.match(lines[i])
        if not m:
            out.append(lines[i]); i += 1; continue
        ctype = m.group(1).lower()
        title = m.group(2).strip()
        i += 1
        body = []
        while i < len(lines) and lines[i].startswith(">"):
            body.append(re.sub(r"^>\s?", "", lines[i])); i += 1
        macro = CALLOUT_MACRO.get(ctype, "info")
        body_html = mdc.convert_markdown_to_html("\n".join(body)) if body else ""
        title_html = f"<p><strong>{mdc.process_inline(title)}</strong></p>\n" if title else ""
        token = f"xCALLOUT{len(macros)}x"
        macros[token] = (
            f'<ac:structured-macro ac:name="{macro}"><ac:rich-text-body>\n'
            f"{title_html}{body_html}\n"
            f"</ac:rich-text-body></ac:structured-macro>"
        )
        out += ["", token, ""]
    return "\n".join(out), macros


# A standalone ``[[TOC]]`` (or ``[TOC]``) line -> a Confluence Table of Contents macro.
_TOC_RE = re.compile(r"^\s*\[\[?TOC\]?\]\s*$", re.IGNORECASE)


def _extract_toc(md: str):
    """Swap a standalone ``[[TOC]]`` line for a Table-of-Contents placeholder.

    Must run *before* :func:`_extract_wikilinks`, otherwise ``[[TOC]]`` would be
    captured as a page wikilink. The placeholder is restored to a Confluence
    ``toc`` macro after conversion, reusing the same ``<p>token</p>`` swap the
    callouts use (so the macro lands as a top-level element, not inside a ``<p>``).

    :param md: the source Markdown.
    :type md: str
    :returns: a ``(markdown_with_tokens, macros)`` pair, where ``macros`` maps
        each placeholder token to its ``toc`` macro HTML.
    :rtype: tuple[str, dict[str, str]]
    """
    macros, out = {}, []
    in_fence = False
    for line in md.split("\n"):
        if _is_fence(line):
            in_fence = not in_fence
            out.append(line)
        elif not in_fence and _TOC_RE.match(line):
            token = f"xTOC{len(macros)}x"
            macros[token] = ('<ac:structured-macro ac:name="toc" ac:schema-version="1">'
                             '<ac:parameter ac:name="maxLevel">2</ac:parameter>'
                             "</ac:structured-macro>")
            out += ["", token, ""]
        else:
            out.append(line)
    return "\n".join(out), macros


def _fix_autolinks(html: str) -> str:
    """Turn angle-bracket autolinks ``<http://...>`` into ``<a>`` tags.

    :param html: the HTML fragment.
    :type html: str
    :returns: the fragment with autolinks rewritten.
    :rtype: str
    """
    return re.sub(r"<(https?://[^>\s]+)>", r'<a href="\1">\1</a>', html)


def _escape_bare_amp(html: str) -> str:
    """Escape bare ``&`` characters that are not already part of an entity.

    :param html: the HTML fragment.
    :type html: str
    :returns: the fragment with stray ampersands escaped to ``&amp;``.
    :rtype: str
    """
    return re.sub(r"&(?!(?:amp|lt|gt|quot|apos|#x?[0-9a-fA-F]+);)", "&amp;", html)


def _code_blocks_to_macro(html: str) -> str:
    """Convert ``<pre><code>`` blocks into Confluence ``code`` macros.

    The inner text is HTML-unescaped and wrapped in a ``CDATA`` section.

    :param html: the HTML fragment.
    :type html: str
    :returns: the fragment with code blocks rewritten as code macros.
    :rtype: str
    """
    def repl(m):
        text = ihtml.unescape(m.group(1)).rstrip("\n")
        text = text.replace("]]>", "]]]]><![CDATA[>")  # CDATA-safe
        return ('<ac:structured-macro ac:name="code"><ac:plain-text-body>'
                f"<![CDATA[{text}]]></ac:plain-text-body></ac:structured-macro>")
    return re.sub(r"<pre><code>(.*?)</code></pre>", repl, html, flags=re.S)


_VALIDATE_WRAP = ('<root xmlns:ac="urn:ac" xmlns:ri="urn:ri">{}</root>')


def validate_storage(html: str) -> None:
    """Validate that a storage-format body is well-formed XML.

    :param html: the Confluence storage-format fragment.
    :type html: str
    :raises xml.etree.ElementTree.ParseError: if the body is not well-formed
        (the exception carries the error position).
    :returns: ``None``.
    """
    ET.fromstring(_VALIDATE_WRAP.format(html))


# --- wikilinks -> Confluence page links ------------------------------------
_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")


def _wikilink_macro(target_title: str, display: str) -> str:
    """Build a Confluence ``ac:link`` that points at a page by title.

    :param target_title: the exact title of the target page.
    :type target_title: str
    :param display: the link text to show.
    :type display: str
    :returns: the ``ac:link`` storage-format markup.
    :rtype: str
    """
    t = target_title.replace('"', "&quot;")
    return (f'<ac:link><ri:page ri:content-title="{t}" />'
            f"<ac:plain-text-link-body><![CDATA[{display}]]></ac:plain-text-link-body></ac:link>")


def _extract_wikilinks(md: str, prefix: str = ""):
    """Replace ``[[wikilinks]]`` with tokens and build their Confluence links.

    Handles ``[[Target]]``, ``[[Target#Anchor]]`` and ``[[Target|alias]]``.
    Links are page-level: the link points at a page *by title*, and the published
    page title is the prefixed title (see :func:`prefixed_title`). So the link
    target must be prefixed too — otherwise a link written in Obsidian as
    ``[[01 - Foo]]`` would point at a non-existent page ``01 - Foo`` instead of
    the published ``<prefix> - 01 - Foo``. The *display* text is left unprefixed,
    so readers still see the natural name. The ``#anchor`` is dropped for now —
    Confluence heading anchors use a different scheme — so the link still lands
    on the right page; it is dropped from the display text too, so the link
    does not advertise a heading it will not jump to (an alias wins over both).

    Wikilinks inside fenced code blocks are intentionally left untouched. They
    are source code examples, not document navigation, and converting them to
    storage macros can break XML validation after code-block CDATA wrapping.

    :param md: the source Markdown.
    :type md: str
    :param prefix: namespacing prefix applied to each link target, matching the
        prefix used when publishing the pages.
    :type prefix: str
    :returns: a ``(markdown_with_tokens, links)`` pair, where ``links`` maps each
        token to its ``ac:link`` markup.
    :rtype: tuple[str, dict[str, str]]
    """
    links = {}

    def repl(m):
        content = m.group(1).strip()
        alias = None
        if "|" in content:
            content, alias = (s.strip() for s in content.split("|", 1))
        target = content.split("#", 1)[0].strip()
        # The anchor is dropped from the link target, so drop it from the text
        # too; fall back to the raw content for an anchor-only [[#Heading]].
        display = alias or target or content
        token = f"xLINK{len(links)}x"
        links[token] = _wikilink_macro(prefixed_title(prefix, target), display)
        return token

    out = []
    in_fence = False
    for line in md.splitlines(keepends=True):
        if _is_fence(line):
            in_fence = not in_fence
            out.append(line)
            continue
        out.append(line if in_fence else _WIKILINK_RE.sub(repl, line))

    return "".join(out), links


# --- rendered mermaid <img> -> attachment + ac:image -----------------------
_IMG_RE = re.compile(r'<img src="img/([^"]+)"[^>]*/>')


def _images_to_attachments(html: str, img_dir, img_width=None):
    """Rewrite rendered Mermaid ``<img>`` tags as Confluence ``ac:image`` macros.

    :param html: the HTML fragment containing ``<img src="img/NAME.png">`` tags.
    :type html: str
    :param img_dir: directory the referenced PNG files live in.
    :param img_width: optional display width in pixels, emitted as
        ``ac:width`` on every image; ``None`` emits no attribute.
    :type img_width: int | None
    :returns: a ``(html, attachments)`` pair, where ``attachments`` is a list of
        ``(filename, filepath)`` tuples to upload.
    :rtype: tuple[str, list[tuple[str, str]]]
    """
    names = []
    width_attr = f' ac:width="{int(img_width)}"' if img_width else ""

    def repl(m):
        names.append(m.group(1))
        return f'<ac:image{width_attr}><ri:attachment ri:filename="{m.group(1)}" /></ac:image>'

    html = _IMG_RE.sub(repl, html)
    atts = [(fn, str(pathlib.Path(img_dir) / fn)) for fn in names]
    return html, atts


def md_to_storage(md: str, img_dir=None, prefix="", img_width=None):
    """Convert Markdown to a Confluence storage-format body and its attachments.

    Runs the bulk converter, then applies the storage-format transforms (callout
    panels, wikilink page-links, code macros, autolink and ampersand fixes) and
    validates the result as XML.

    :param md: the source Markdown.
    :type md: str
    :param img_dir: where to render Mermaid PNGs (and upload them from); pass
        ``None`` to skip diagram rendering.
    :param prefix: namespacing prefix applied to wikilink targets so internal
        links resolve to the prefixed published page titles.
    :type prefix: str
    :param img_width: optional display width in pixels for every Mermaid image
        (``ac:width``); ``None`` leaves the images at their natural size.
    :type img_width: int | None
    :returns: a ``(storage_xhtml, attachments)`` pair, where ``attachments`` is
        a list of ``(filename, filepath)`` tuples.
    :rtype: tuple[str, list[tuple[str, str]]]
    :raises xml.etree.ElementTree.ParseError: if the produced body is not
        well-formed.
    """
    md = mdc.strip_frontmatter(md)
    md, tocs = _extract_toc(md)                  # before wikilinks: [[TOC]] is not a page link
    md, links = _extract_wikilinks(md, prefix)   # before conversion (converter flattens them)
    md, macros = _extract_callouts(md)
    prev_dir, prev_rel = mdc._current_img_dir, mdc._current_img_rel
    if img_dir is not None:
        pathlib.Path(img_dir).mkdir(parents=True, exist_ok=True)
        mdc._current_img_dir = pathlib.Path(img_dir); mdc._current_img_rel = "img"
    try:
        html = mdc.convert_markdown_to_html(md)   # renders Mermaid to PNG when img_dir set
    finally:
        mdc._current_img_dir, mdc._current_img_rel = prev_dir, prev_rel
    for token, macro in {**macros, **tocs}.items():
        html = html.replace(f"<p>{token}</p>", macro)
    html, atts = _images_to_attachments(html, img_dir, img_width) if img_dir else (html, [])
    html = _fix_autolinks(html)
    html = _escape_bare_amp(html)
    html = _code_blocks_to_macro(html)        # last: creates CDATA the amp-escape must not touch
    for token, macro in links.items():
        html = html.replace(token, macro)
    validate_storage(html)
    return html, atts


# --------------------------------------------------------------------------- #
#  Confluence Cloud REST client (env auth)
# --------------------------------------------------------------------------- #

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


def update_page(page_id, title, storage, version_message=""):
    """Update a page's body, bumping its version number.

    :param page_id: the page id.
    :param title: the page title.
    :param storage: the new storage-format body.
    :param version_message: an optional version comment.
    :returns: the updated page object.
    :rtype: dict
    """
    base, *_ = _cfg()
    cur = get_page(page_id)
    payload = {"id": str(page_id), "status": "current", "title": title,
               "body": {"representation": "storage", "value": storage},
               "version": {"number": cur["version"]["number"] + 1, "message": version_message}}
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


def prefixed_title(prefix, title):
    """Prepend the namespacing prefix to a page title, if one was given.

    Confluence page titles must be unique *within a space*, so publishing several
    repos' docs into one shared space requires a per-repo prefix to avoid
    collisions. The prefix is applied uniformly to every title — including the
    ``In Situ`` container — and to the title used for the idempotent lookup, so
    re-publishing the same set keeps matching the same pages.

    :param prefix: the namespacing prefix (e.g. ``langflow``), or ``""`` for none.
    :param title: the bare page title (the Markdown filename stem).
    :returns: ``"<prefix> - <title>"`` when a prefix is set, else ``title``.
    :rtype: str
    """
    return f"{prefix} - {title}" if prefix else title


def publish_page(space_id, parent_id, title, md, img_dir, dry_run=False, prefix="", img_width=None):
    """Create or update one page and upload its rendered diagrams.

    The body is always converted and validated first, so errors surface early.

    :param space_id: numeric space id.
    :param parent_id: id of the parent page (used only when creating).
    :param title: the bare page title (prefix is applied internally).
    :param md: the source Markdown.
    :param img_dir: where to render Mermaid PNGs.
    :param dry_run: when ``True``, resolve the action but write nothing.
    :param prefix: namespacing prefix prepended to the title (see
        :func:`prefixed_title`).
    :param img_width: optional Mermaid image width in pixels (see
        :func:`md_to_storage`).
    :returns: an ``(action, page_id, attachments)`` tuple, where ``action`` is
        ``"CREATE"`` or ``"UPDATE"``.
    :rtype: tuple[str, str | None, list]
    """
    full_title = prefixed_title(prefix, title)
    html, atts = md_to_storage(md, img_dir, prefix, img_width)   # convert + validate; prefix wikilink targets
    pid = find_page_id_by_title(space_id, full_title)
    action = "UPDATE" if pid else "CREATE"
    if dry_run:
        return action, pid, atts
    # Attachments must exist BEFORE the body that references them is saved:
    # Confluence links each ri:attachment to the file at save time (it records
    # ri:version-at-save). A body saved first keeps unlinked references, which
    # view mode still resolves by name but the editor shows as "Preview not
    # available" and drops on the user's next save. A new page can only take
    # attachments once it exists, so a CREATE with diagrams saves its body twice.
    if pid:
        _upload_missing_attachments(pid, atts)
        update_page(pid, full_title, html, "republish")
    else:
        pid = create_page(space_id, parent_id, full_title, html)["id"]
        if _upload_missing_attachments(pid, atts):
            update_page(pid, full_title, html, "link diagram attachments")
    return action, pid, atts


def _upload_missing_attachments(page_id, atts):
    """Upload the attachments not yet on the page; return how many were uploaded.

    Names are content hashes, so a name already present means identical bytes
    (and re-uploading a duplicate filename would return HTTP 400 anyway).

    :param page_id: the page id.
    :param atts: ``(filename, filepath)`` pairs from :func:`md_to_storage`.
    :rtype: int
    """
    if not atts:
        return 0
    existing = existing_attachment_names(page_id)
    uploaded = 0
    for fn, fp in atts:
        if fn not in existing:
            upload_attachment(page_id, fp, fn)
            existing.add(fn)
            uploaded += 1
    return uploaded


def find_insitu_dir(regen):
    """Return the ``In Situ`` subfolder, matched case-insensitively, or ``None``.

    The deep-dive folder is canonically named ``In Situ``, but matching it
    exactly would silently skip the whole folder if the generator produced a
    slightly different casing/spacing (e.g. ``in situ``). Matching loosely makes
    sure those pages still get published.

    :param regen: the doc-set folder.
    :returns: the matching subfolder path, or ``None`` if there is none.
    :rtype: pathlib.Path | None
    """
    if not regen.is_dir():
        return None
    return next((d for d in sorted(regen.iterdir())
                 if d.is_dir() and d.name.strip().casefold() == "in situ"), None)


def publish_set(regen_dir, space_id, parent_id, img_dir, dry_run=False, prefix="", img_width=None):
    """Publish a whole doc-set folder.

    Numbered ``NN - Title.md`` pages go directly under the parent; docs in an
    ``In Situ/`` subfolder go under an ``In Situ`` container page. Idempotent
    (create-or-update by title). When ``prefix`` is set, every page title is
    namespaced so multiple repos can share one space without title collisions.

    :param regen_dir: the doc-set folder.
    :param space_id: numeric space id.
    :param parent_id: id of the parent page.
    :param img_dir: where to render Mermaid PNGs.
    :param dry_run: when ``True``, write nothing.
    :param prefix: namespacing prefix prepended to every page title.
    :param img_width: optional Mermaid image width in pixels (see
        :func:`md_to_storage`).
    :returns: a list of ``(title, action, diagram_count)`` tuples, where ``title``
        is the actual (prefixed) Confluence page title.
    :rtype: list[tuple[str, str, int]]
    """
    regen = pathlib.Path(regen_dir)
    report = []
    for f in sorted(regen.glob("*.md")):                     # 00..NN top-level
        action, pid, atts = publish_page(space_id, parent_id, f.stem, f.read_text(), img_dir, dry_run, prefix, img_width)
        report.append((prefixed_title(prefix, f.stem), action, len(atts)))
    insitu = find_insitu_dir(regen)
    if insitu is not None:
        insitu_title = prefixed_title(prefix, "In Situ")
        cid = find_page_id_by_title(space_id, insitu_title)
        container_action = "UPDATE" if cid else "CREATE"
        if not cid and not dry_run:
            cid = create_page(space_id, parent_id, insitu_title,
                              "<p>Stack-specific deep-dive docs for this repository.</p>")["id"]
        report.append((insitu_title, container_action, 0))   # always show the container in the plan
        for f in sorted(insitu.glob("*.md")):
            action, pid, atts = publish_page(space_id, cid or parent_id, f.stem, f.read_text(), img_dir, dry_run, prefix, img_width)
            report.append((prefixed_title(prefix, f.stem), action, len(atts)))
    return report


# --------------------------------------------------------------------------- #
#  CLI
# --------------------------------------------------------------------------- #

def _positive_int(value):
    """Parse a strictly positive integer for argparse (used by ``--img-width``).

    :param value: the raw command-line string.
    :type value: str
    :rtype: int
    :raises argparse.ArgumentTypeError: if ``value`` is not an integer >= 1.
    """
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {value!r}") from None
    if n < 1:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {value!r}")
    return n


def build_parser():
    """Build the command-line parser (separate from :func:`main` so tests can
    exercise argument validation in-process).

    :rtype: argparse.ArgumentParser
    """
    ap = argparse.ArgumentParser(
        description="Publish a folder of Markdown docs to Confluence Cloud under one parent page.")
    ap.add_argument("docs_dir", help='folder of "NN - Title.md" docs (optional "In Situ/" subfolder)')
    ap.add_argument("--space", required=True, help="Confluence space KEY (e.g. ~yourname or ENG)")
    ap.add_argument("--parent", required=True, help="ID of the parent page to publish under")
    ap.add_argument("--img-dir", default=None,
                    help="where to render Mermaid PNGs (default: <docs_dir>/.img)")
    ap.add_argument("--prefix", default="",
                    help="unique identifier prepended to every page title "
                         '(e.g. "langflow" -> "langflow - 01 - Overview"); use a '
                         "distinct value per repo so docs sharing a space never collide")
    ap.add_argument("--img-width", type=_positive_int, default=None, metavar="PX",
                    help="display width in pixels for every Mermaid diagram "
                         "(default: unset, natural size)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the CREATE/UPDATE plan and write nothing")
    return ap


def main(argv=None):
    """Parse arguments, resolve the space, and publish the doc set.

    :param argv: argument list (``sys.argv[1:]`` when ``None``).
    :raises SystemExit: on missing credentials, a bad docs folder, or an unknown
        space key.
    """
    args = build_parser().parse_args(argv)

    missing = [v for v in ("CONFLUENCE_BASE_URL", "CONFLUENCE_EMAIL", "CONFLUENCE_API_TOKEN")
               if not os.environ.get(v)]
    if missing:
        raise SystemExit("Missing environment variable(s): " + ", ".join(missing))
    if not pathlib.Path(args.docs_dir).is_dir():
        raise SystemExit(f"Not a folder: {args.docs_dir}")

    # Pre-flight: Confluence page titles must be unique within a space, so two
    # source docs that resolve to the same title would make the second CREATE
    # fail with HTTP 400. The title is the filename stem (no prefix), for both
    # top-level docs and "In Situ/" docs, plus the "In Situ" container page.
    docs = pathlib.Path(args.docs_dir)
    titles = [f.stem for f in docs.glob("*.md")]
    insitu = find_insitu_dir(docs)
    if insitu is not None:
        titles.append("In Situ")
        titles += [f.stem for f in insitu.glob("*.md")]
    dupes = sorted({t for t in titles if titles.count(t) > 1})
    if dupes:
        raise SystemExit(
            "Duplicate page title(s) in this doc set — Confluence requires titles "
            "to be unique within a space:\n"
            + "\n".join(f"  - {t!r}" for t in dupes)
            + "\nRename the colliding file(s) so every page has a distinct title."
        )

    space_id = resolve_space_id(args.space)
    img_dir = args.img_dir or str(pathlib.Path(args.docs_dir) / ".img")
    try:
        report = publish_set(args.docs_dir, space_id, args.parent, img_dir,
                             dry_run=args.dry_run, prefix=args.prefix,
                             img_width=args.img_width)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            hint = (f"A 404 means --parent {args.parent} does not exist — check the "
                    f"page id, and that it lives in space {args.space!r}.")
        elif e.code == 400:
            hint = (f"A 400 on CREATE usually means --parent {args.parent} is a folder "
                    f"(not a page) or lives in a space other than {args.space!r}, or a "
                    f"page with this title already exists in the space.")
        else:
            hint = "Check the credentials, space key, and parent page id."
        raise SystemExit(
            f"\nConfluence rejected the request:\n"
            f"  {e.code} {e.reason}\n"
            f"  {getattr(e, 'detail', '') or '(no detail returned)'}\n\n"
            f"{hint}"
        ) from None

    print(f"\n{'ACTION':7} {'DIAGRAMS':8} TITLE")
    for title, action, n in report:
        print(f"{action:7} {n:<8} {title}")
    mode = "DRY-RUN (nothing written)" if args.dry_run else "published"
    prefix_note = f", prefix {args.prefix!r}" if args.prefix else ""
    print(f"\n{len(report)} pages {mode} — space {args.space}, parent {args.parent}{prefix_note}")


if __name__ == "__main__":
    main()
