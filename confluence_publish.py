#!/usr/bin/env python3
"""Publish a folder of Markdown docs to Confluence Cloud.

Publishes an Obsidian-style Markdown doc set as a page tree under one parent
page. Reuses :mod:`md_to_confluence` for Markdown-to-HTML, then post-processes
into valid Confluence storage format (code macros; info/note/tip/warning panels;
autolink and ampersand fixes; ``[[wikilinks]]`` to page links), renders Mermaid
to PNG and uploads the images as attachments, validates each body as XML, and
creates or updates pages idempotently (matched by title).

Safe by construction: every run is planned and checked before the first write;
it only updates pages strictly *below* the parent you pass that it published
itself (``cpub-managed`` content property) or that you hand over in this run,
caps updates per run (``--max-updates``), and never deletes or moves anything.

Usage
-----
.. code-block:: console

    python3.12 confluence_publish.py DOCS_DIR --space KEY --parent PAGE_ID [--prefix P] [--dry-run]

``DOCS_DIR``
    folder of ``NN - Title.md`` files, with an optional ``In Situ/`` subfolder.
``--space KEY``
    Confluence space key (``~yourname`` for a personal space, or e.g. ``ENG``).
``--parent PAGE_ID``
    id of the page to publish everything under.
``--dry-run``
    print the plan (CREATE/UPDATE, ownership, refusals) and write nothing.
``--adopt``
    hand over the listed pages without the typed confirmation (non-interactive runs).
``--max-updates N``
    refuse a run that would update more than N existing pages (default 25).
``--env-file PATH``
    read credentials from this file.

Credentials
-----------
Each variable is taken from the first of: the environment, ``--env-file``,
``./.env``, ``$XDG_CONFIG_HOME/confluence-publisher/.env`` (default
``~/.config/…``). Files hold ``KEY=VALUE`` lines; never commit them:

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
import os, re, sys, ssl, json, base64, errno, datetime, dataclasses, importlib.util, pathlib, argparse
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
#  Credential autoload (S1.1)
# --------------------------------------------------------------------------- #

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


# --------------------------------------------------------------------------- #
#  E0 — plan first, write second (overwrite protection)
# --------------------------------------------------------------------------- #
#
# The tool may only overwrite a page it can prove it is allowed to: a page
# strictly inside the --parent subtree that it published itself from the same
# source, or that the user explicitly handed over in this run. build_plan()
# reads everything (GETs only), check_plan() decides (pure), and only
# execute_plan() writes. create_page/update_page/upload_attachment/set_marker
# must not be called from anywhere else.

#: Body of the "In Situ" container page (v1.1 text, byte-identical).
INSITU_CONTAINER_BODY = "<p>Stack-specific deep-dive docs for this repository.</p>"
#: ``PlanItem.parent_ref`` of an In Situ doc whose container is created in this run.
CONTAINER_REF = "@in-situ-container"
DEFAULT_MAX_UPDATES = 25


@dataclasses.dataclass
class PlanItem:
    """One page of a plan: what will happen to it and why it is allowed."""
    title: str                      # actual (prefixed) Confluence title
    source: str                     # marker source id (prefixed title in set mode)
    action: str                     # "CREATE" | "UPDATE"
    kind: str = "page"              # "page" | "container"
    page_id: str | None = None      # existing page (UPDATE only)
    parent_ref: str | None = None   # parent page id, or CONTAINER_REF
    storage: str = ""
    attachments: list = dataclasses.field(default_factory=list)
    writes_body: bool = True        # False for an existing In Situ container (v1.1 never rewrote it)
    managed: str = "new"            # "new" | "same" | "other" | "none"
    marker: dict | None = None
    marker_ref: tuple | None = None  # (property_id, version) of the current marker
    version: int | None = None
    last_edited: str | None = None
    webui: str | None = None
    status: str | None = None
    space_id: str | None = None
    in_subtree: bool | None = None  # strict descendant of --parent (UPDATE only)

    @property
    def diagrams(self):
        return len(self.attachments)

    @property
    def needs_handover(self):
        """An UPDATE that overwrites a page this tool did not publish from this source.

        Only pages E0 would otherwise allow (strictly inside the fence, current)
        are offered for hand-over; refused pages are never listed as adoptable.
        """
        return (self.action == "UPDATE" and self.writes_body and self.managed in ("none", "other")
                and self.in_subtree is True and self.status in (None, "current"))


@dataclasses.dataclass
class Plan:
    space_id: str
    parent_id: str
    prefix: str
    items: list
    parent_info: dict | None = None


@dataclasses.dataclass
class Refusal:
    """Why a page (or the whole run) is refused, and the one change that would allow it."""
    code: str                 # outside-subtree | is-parent | not-current | parent-space | parent-not-current | max-updates
    title: str | None
    page_id: str | None
    message: str
    fix: str | None = None


@dataclasses.dataclass
class RunReport:
    written: list = dataclasses.field(default_factory=list)          # [(PlanItem, page_id)], completed
    pending: list = dataclasses.field(default_factory=list)          # [PlanItem] not (fully) written
    partial: tuple | None = None                                     # (PlanItem, page_id) failed after it was created
    marker_failures: list = dataclasses.field(default_factory=list)  # [(PlanItem, page_id, exc)]
    error: BaseException | None = None


class PlanRefused(Exception):
    """The plan failed an E0 check; nothing was written."""
    def __init__(self, refusals):
        super().__init__(f"{len(refusals)} page(s) refused")
        self.refusals = refusals


class HandoverRequired(Exception):
    """Pages need a hand-over but there is no way to ask (non-interactive, no --adopt)."""
    def __init__(self, items):
        super().__init__(f"{len(items)} page(s) need a hand-over (--adopt)")
        self.items = items


class HandoverDeclined(Exception):
    """The user declined the hand-over; nothing was written."""
    def __init__(self, items):
        super().__init__(f"hand-over of {len(items)} page(s) declined")
        self.items = items


class PlanExecutionError(Exception):
    """A write failed mid-run; ``report`` says what was and was not written."""
    def __init__(self, report):
        super().__init__(f"write failed: {report.error}")
        self.report = report


def _classify(item, marker, marker_ref):
    item.marker, item.marker_ref = marker, marker_ref
    if not marker:
        item.managed = "none"
    elif marker.get("tool") == MARKER_TOOL and marker.get("source") == item.source:
        item.managed = "same"
    else:
        item.managed = "other"


def read_doc_set(regen_dir):
    """Read a doc-set folder in publish order.

    :returns: ``(top, insitu)``: ``top`` is ``[(stem, markdown)]`` for the
        ``*.md`` files sorted by name; ``insitu`` is the same for the ``In Situ/``
        subfolder, or ``None`` when there is no such folder.
    :rtype: tuple[list[tuple[str, str]], list[tuple[str, str]] | None]
    """
    regen = pathlib.Path(regen_dir)
    top = [(f.stem, f.read_text()) for f in sorted(regen.glob("*.md"))]
    d = find_insitu_dir(regen)
    insitu = None if d is None else [(f.stem, f.read_text()) for f in sorted(d.glob("*.md"))]
    return top, insitu


def build_plan(regen_dir, space_id, parent_id, img_dir, *, prefix="", img_width=None, docs=None):
    """Build the complete plan for a doc set, with **zero write calls**.

    Every body is converted and validated (``md_to_storage``) first, so a bad
    document fails before anything is read from or written to Confluence.
    Items come in v1.1 write order: top-level docs, the In Situ container, the
    In Situ docs.

    :param regen_dir: the doc-set folder.
    :param space_id: numeric space id.
    :param parent_id: id of the ``--parent`` page.
    :param img_dir: where to render Mermaid PNGs.
    :param prefix: namespacing prefix prepended to every title.
    :param img_width: optional Mermaid image width in pixels.
    :param docs: the doc set as :func:`read_doc_set` returns it, instead of
        reading ``regen_dir`` (tests).
    :rtype: Plan
    """
    top, insitu = read_doc_set(regen_dir) if docs is None else docs
    parent_id = str(parent_id)
    items = []

    def page_item(stem, md, parent_ref):
        title = prefixed_title(prefix, stem)
        html, atts = md_to_storage(md, img_dir, prefix, img_width)  # convert + validate
        return PlanItem(title=title, source=title, action="CREATE", parent_ref=parent_ref,
                        storage=html, attachments=atts)

    items.extend(page_item(stem, md, parent_id) for stem, md in top)
    container = None
    if insitu is not None:
        ctitle = prefixed_title(prefix, "In Situ")
        container = PlanItem(title=ctitle, source=ctitle, action="CREATE", kind="container",
                             parent_ref=parent_id, storage=INSITU_CONTAINER_BODY)
        items.append(container)
        items.extend(page_item(stem, md, CONTAINER_REF) for stem, md in insitu)

    parent_info = get_page_info(parent_id)
    index = space_title_index(space_id)
    for it in items:
        pid = index.get(it.title)
        if pid is None:
            continue
        it.action, it.page_id = "UPDATE", str(pid)
        if it is container:
            it.writes_body = False          # v1.1 reuses an existing container as-is
        info = get_page_info(it.page_id)
        it.version, it.last_edited = info["version"], info["last_edited"]
        it.webui, it.status, it.space_id = info["webui"], info["status"], info["spaceId"]
        it.in_subtree = is_descendant(it.page_id, parent_id)
        if it.in_subtree and it.status == "current":
            marker, prop_id, prop_ver = get_marker(it.page_id)
            _classify(it, marker, (prop_id, prop_ver) if prop_id else None)
        else:
            it.managed = "none"             # refused by check_plan; foreign markers are not read
    if container is not None and container.page_id:
        for it in items:
            if it.parent_ref == CONTAINER_REF:
                it.parent_ref = container.page_id
    return Plan(space_id=str(space_id), parent_id=parent_id, prefix=prefix,
                items=items, parent_info=parent_info)


def check_plan(plan, *, max_updates=DEFAULT_MAX_UPDATES):
    """Return **every** E0 refusal for a plan (empty list = the plan may run). Pure.

    :param plan: from :func:`build_plan`.
    :param max_updates: blast-radius cap on body UPDATEs (creates don't count).
    :rtype: list[Refusal]
    """
    out = []
    pinfo = plan.parent_info or {}
    pname = f'"{pinfo["title"]}" ({plan.parent_id})' if pinfo.get("title") else f"page {plan.parent_id}"
    if pinfo.get("spaceId") is not None and str(pinfo["spaceId"]) != str(plan.space_id):
        out.append(Refusal("parent-space", pinfo.get("title"), plan.parent_id,
                           f"--parent {pname} lives in another space (id {pinfo['spaceId']}), "
                           f"not the --space given (id {plan.space_id}).",
                           "Pass the --space that holds the parent page, or a parent in that space."))
    if pinfo.get("status") not in (None, "current"):
        out.append(Refusal("parent-not-current", pinfo.get("title"), plan.parent_id,
                           f"--parent {pname} is {pinfo['status']}, not a live page.",
                           "Restore it in Confluence or choose another --parent."))
    for it in plan.items:
        if it.action != "UPDATE":
            continue
        where = it.webui or f"page {it.page_id}"
        if it.page_id == plan.parent_id:
            out.append(Refusal("is-parent", it.title, it.page_id,
                               f'"{it.title}" is the --parent page itself — not touching it.',
                               "Use a distinct --prefix (or rename the doc) so no doc is titled like the parent."))
        elif not it.in_subtree:
            out.append(Refusal("outside-subtree", it.title, it.page_id,
                               f'"{it.title}" already exists outside {pname} at {where} — not touching it.',
                               "Use a distinct --prefix."))
        if it.status not in (None, "current"):       # not deduped: one page can have two codes
            out.append(Refusal("not-current", it.title, it.page_id,
                               f'"{it.title}" at {where} is {it.status} — not touching it.',
                               "Restore or rename it in Confluence, or use a distinct --prefix."))
    n = sum(1 for it in plan.items if it.action == "UPDATE" and it.writes_body)
    if n > max_updates:
        out.append(Refusal("max-updates", None, None,
                           f"This run would update {n} existing pages, more than --max-updates {max_updates}.",
                           f"Re-run with --max-updates {n} if that is intended."))
    return out


def handover_items(plan):
    """The planned UPDATEs that overwrite a page this tool did not publish from this source.

    :rtype: list[PlanItem]
    """
    return [it for it in plan.items if it.needs_handover]


def _write_item(plan, it, parent_id, report, today):
    """Write one item; return its page id. Attachments always precede the body that
    references them (Confluence links ``ri:attachment`` at save time)."""
    if it.action == "UPDATE":
        pid = it.page_id
        if it.writes_body:
            _upload_missing_attachments(pid, it.attachments)
            update_page(pid, it.title, it.storage, "republish", expected_version=it.version)
            if it.managed != "same":
                _mark(it, pid, report, today)
        return pid
    created = create_page(plan.space_id, parent_id, it.title, it.storage)
    pid = str(created["id"])
    report.partial = (it, pid)
    _mark(it, pid, report, today)   # right after create: a failed upload re-runs prompt-free
    if _upload_missing_attachments(pid, it.attachments):
        update_page(pid, it.title, it.storage, "link diagram attachments",
                    expected_version=(created.get("version") or {}).get("number", 1))
    report.partial = None
    return pid


def _mark(it, pid, report, today):
    try:
        set_marker(pid, it.source, today=today,
                   existing=it.marker_ref if it.action == "UPDATE" else None)
    except Exception as e:          # the page content is fine; report it as unmanaged (Q9)
        report.marker_failures.append((it, pid, e))


def execute_plan(plan, *, max_updates=DEFAULT_MAX_UPDATES, adopt=False,
                 confirm_handover=None, log=None, today=None):
    """Run a plan: re-check it, obtain any hand-over, then write in plan order.

    :param plan: from :func:`build_plan`.
    :param max_updates: blast-radius cap (see :func:`check_plan`).
    :param adopt: ``--adopt``: hand-over granted without asking.
    :param confirm_handover: ``items -> bool`` asking the user (interactive);
        ``None`` means there is no way to ask.
    :param log: ``event_dict -> None`` called after each page is written.
    :param today: ISO date for new markers (today when ``None``).
    :rtype: RunReport
    :raises PlanRefused: any E0 refusal — nothing written.
    :raises HandoverRequired: hand-over needed, not adopted, no ``confirm_handover``.
    :raises HandoverDeclined: ``confirm_handover`` returned false — nothing written.
    :raises PlanExecutionError: a write failed; ``.report`` lists written/pending.
    """
    refusals = check_plan(plan, max_updates=max_updates)
    if refusals:
        raise PlanRefused(refusals)
    needs = handover_items(plan)
    if needs and not adopt:
        if confirm_handover is None:
            raise HandoverRequired(needs)
        if not confirm_handover(needs):
            raise HandoverDeclined(needs)
    report = RunReport(pending=list(plan.items))
    container_id = None
    for it in plan.items:
        parent = container_id if it.parent_ref == CONTAINER_REF else it.parent_ref
        try:
            pid = _write_item(plan, it, parent, report, today)
        except Exception as e:
            report.error = e
            raise PlanExecutionError(report) from e
        if it.kind == "container":
            container_id = pid
        report.pending.remove(it)
        report.written.append((it, pid))
        if log is not None:
            log({"action": it.action, "page_id": pid, "title": it.title,
                 "source": it.source, "version_before": it.version})
    return report


def publish_set(regen_dir, space_id, parent_id, img_dir, dry_run=False, prefix="", img_width=None,
                *, max_updates=DEFAULT_MAX_UPDATES, adopt=False, confirm_handover=None, docs=None):
    """Publish a whole doc-set folder through the E0 plan.

    Numbered ``NN - Title.md`` pages go directly under the parent; docs in an
    ``In Situ/`` subfolder go under an ``In Situ`` container page. Idempotent
    (create-or-update by title, fenced to the ``--parent`` subtree).

    :returns: v1.1 report rows ``(title, action, diagram_count)``.
    :rtype: list[tuple[str, str, int]]
    :raises PlanRefused: also on ``dry_run`` when the plan would be refused.
    """
    plan = build_plan(regen_dir, space_id, parent_id, img_dir, prefix=prefix,
                      img_width=img_width, docs=docs)
    if dry_run:
        refusals = check_plan(plan, max_updates=max_updates)
        if refusals:
            raise PlanRefused(refusals)
    else:
        execute_plan(plan, max_updates=max_updates, adopt=adopt,
                     confirm_handover=confirm_handover)
    return [(it.title, it.action, it.diagrams) for it in plan.items]


# --------------------------------------------------------------------------- #
#  CLI
# --------------------------------------------------------------------------- #

CMD = "python3.12 confluence_publish.py"
TOKEN_URL = "https://id.atlassian.com/manage-profile/security/api-tokens"


class CliError(Exception):
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


def _non_negative_int(value):
    """Parse an integer >= 0 for argparse (used by ``--max-updates``).

    :param value: the raw command-line string.
    :type value: str
    :rtype: int
    :raises argparse.ArgumentTypeError: if ``value`` is not an integer >= 0.
    """
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"must be a whole number >= 0, got {value!r}") from None
    if n < 0:
        raise argparse.ArgumentTypeError(f"must be a whole number >= 0, got {value!r}")
    return n


_HELP_INTRO = f"""\
Publish a folder of Markdown docs to Confluence Cloud under one parent page.

example:
  {CMD} docs/ --space ENG --parent 123456789 --prefix myrepo --dry-run
  {CMD} docs/ --space ENG --parent 123456789 --prefix myrepo

Safety: every run is planned and checked before anything is written. It only
updates pages below --parent that it published itself; any other page needs a
one-time hand-over (type the page count when asked, or --adopt in scripts).
Nothing is ever deleted or moved.
"""

_HELP_CREDS = """\
credentials (first hit wins, per variable):
  1. environment variables CONFLUENCE_BASE_URL, CONFLUENCE_EMAIL, CONFLUENCE_API_TOKEN
  2. --env-file PATH
  3. ./.env in the current folder
  4. ~/.config/confluence-publisher/.env  ($XDG_CONFIG_HOME is honoured)
  Files use KEY=VALUE lines, as in .env.example. Keep them chmod 600.
"""


def build_parser():
    """Build the command-line parser (separate from :func:`main` so tests can
    exercise argument validation in-process).

    :rtype: argparse.ArgumentParser
    """
    ap = argparse.ArgumentParser(
        description=_HELP_INTRO, epilog=_HELP_CREDS,
        formatter_class=argparse.RawDescriptionHelpFormatter)
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
                    help="print the plan (CREATE/UPDATE, ownership, refusals) and write nothing")
    ap.add_argument("--adopt", action="store_true",
                    help="take over the pages listed for hand-over without the typed "
                         "confirmation (for scripts; this run only)")
    ap.add_argument("--max-updates", type=_non_negative_int, default=25, metavar="N",
                    help="refuse a run that would update more than N existing pages "
                         "(default: 25; creates don't count)")
    ap.add_argument("--env-file", default=None, metavar="PATH",
                    help="read credentials from this .env file (see 'credentials' below)")
    return ap


# --- output -----------------------------------------------------------------

_MANAGED_LABEL = {
    "new": "new",
    "same": "published by this tool from this source",
    "other": "published by this tool from another source",
    "none": "not published by this tool",
}


def _page_link(webui=None, page_id=None):
    """Return a clickable URL for a page, from its v2 ``webui`` path or its id.

    :param webui: the page's ``_links.webui`` (relative to the ``/wiki`` base),
        or an absolute URL, or ``None``.
    :param page_id: fallback page id when ``webui`` is unknown.
    :rtype: str
    """
    base = os.environ.get("CONFLUENCE_BASE_URL", "").rstrip("/")
    if webui:
        return webui if webui.startswith("http") else base + webui
    return f"{base}/pages/viewpage.action?pageId={page_id}"


def _print_table(items, out):
    """Print the v1.1 ``ACTION DIAGRAMS TITLE`` table — kept byte-identical."""
    print(f"\n{'ACTION':7} {'DIAGRAMS':8} TITLE", file=out)
    for it in items:
        print(f"{it.action:7} {it.diagrams:<8} {it.title}", file=out)


def _plural(n, word):
    """``"1 page"`` / ``"2 pages"``."""
    return f"{n} {word}" + ("" if n == 1 else "s")


def _when(iso):
    """Shorten a Confluence ISO timestamp to ``YYYY-MM-DD HH:MM UTC`` (best effort)."""
    try:
        t = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return t.astimezone(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, AttributeError):
        return str(iso)


def _describe_page(it):
    """One line describing an existing page for the hand-over list."""
    bits = [_MANAGED_LABEL.get(it.managed, it.managed)]
    if it.version is not None:
        bits.append(f"version {it.version}")
    if it.last_edited:
        bits.append(f"last edited {_when(it.last_edited)}")
    return f"  - {it.title}  ({', '.join(bits)})"


def _need(n):
    """``"1 page needs"`` / ``"2 pages need"``."""
    return f"{_plural(n, 'page')} need" + ("s" if n == 1 else "")


def _print_ownership(plan, out, refusals=(), adopted=False):
    """Summarise who owns each planned page, then list the hand-over pages.

    Pages that a refusal already covers are counted as refused and left out of
    the hand-over list: no hand-over can make them writable.

    :param adopted: the pages were taken over with ``--adopt`` (wording only).
    """
    refused = {r.page_id for r in refusals if getattr(r, "page_id", None)}
    counts = {}
    for it in plan.items:
        key = "refused" if it.page_id and it.page_id in refused else it.managed
        counts[key] = counts.get(key, 0) + 1
    labels = {**_MANAGED_LABEL, "refused": "refused"}
    parts = [f"{counts[k]} {labels[k]}" for k in ("new", "same", "other", "none", "refused")
             if counts.get(k)]
    print("\nOwnership: " + ", ".join(parts), file=out)
    handover = [it for it in handover_items(plan) if it.page_id not in refused]
    if handover:
        n = len(handover)
        head = (f"Taken over ({n}):" if adopted else
                f"{_need(n)} a one-time hand-over (this tool did not publish "
                f"{'it' if n == 1 else 'them'} from this source):")
        print("\n" + head, file=out)
        for it in handover:
            print(_describe_page(it), file=out)


def _refused(refusals):
    """Turn ``check_plan`` refusals into one :class:`CliError` listing all of them."""
    lines = []
    for r in refusals:
        lines.append(f"- {r.message}")
        if r.fix:
            lines.append(f"  Fix: {r.fix}")
    return CliError(
        f"The plan was refused ({_plural(len(refusals), 'problem')}) — nothing was written.",
        "Resolve every item below, then run the same command again (try --dry-run first).",
        "\n".join(lines))


# --- hand-over prompt (AC-0.3.3) ----------------------------------------------

def _stdin_is_tty():
    """Whether we may ask the user a question (patched in tests)."""
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _ask(prompt):
    """Read one answer from the terminal (patched in tests)."""
    return input(prompt)


def confirm_handover(items, *, ask=None, out=None):
    """Interactive hand-over: list the pages, ask for the typed page count.

    There is deliberately no y/N and no ``--yes`` (D4): typing the number makes
    the size of what is being replaced impossible to miss.

    :param items: the plan items that need a hand-over.
    :param ask: input function (default :func:`_ask`).
    :param out: stream for the listing (default stdout).
    :returns: ``True`` only when the user typed exactly ``len(items)``.
    :rtype: bool
    """
    ask = ask or _ask
    out = out or sys.stdout
    n = len(items)
    print(f"\nThese {n} page(s) exist under the parent but were not published by this "
          "tool from this source:", file=out)
    for it in items:
        print(_describe_page(it), file=out)
    try:
        answer = ask(f"Take over and replace {n} page(s) you did not publish? "
                     f"Type the number {n} to confirm: ")
    except (EOFError, KeyboardInterrupt):
        print(file=out)
        return False
    return answer.strip() == str(n)


# --- friendly errors (S3.1) -----------------------------------------------------

def _http_error(e, args):
    """Map a Confluence ``HTTPError`` to a :class:`CliError` (sentence + Fix + detail)."""
    raw = (f"Confluence said: {e.code} {e.reason}\n"
           f"{getattr(e, 'detail', '') or '(no detail returned)'}")
    if e.code == 401:
        return _bad_token(args, raw)
    if e.code == 403:
        return CliError(f"Your account can't edit this page/space ({args.space}).",
                        "Ask a space admin for edit permission, or publish under a page you can edit.",
                        raw)
    if e.code == 404:
        return CliError(f"Page {args.parent} was not found or is not visible to you.",
                        f"Check --parent: it must be the id of a page (not a folder) in space "
                        f"{args.space}.", raw)
    if e.code == 409:
        return CliError("A page was edited in Confluence after this run was planned, so it "
                        "was not overwritten.",
                        "Check the latest version in Confluence, then run the same command "
                        "again to re-plan.", raw)
    if e.code == 400:
        return CliError("Confluence refused the request as invalid.",
                        f"Usually --parent {args.parent} is a folder (use a page) or a page "
                        f"title already exists in space {args.space} (use a distinct --prefix).",
                        raw)
    return CliError(f"Confluence returned an unexpected error ({e.code}).",
                    "Try again; if it persists, check the detail below.", raw)


def _no_space(args, raw=None):
    return CliError(f"Confluence has no space with the key {args.space!r} that you can see.",
                    "Check --space: it is the part after /spaces/ in a page URL "
                    "(~yourname for a personal space).", raw)


def _token_where(args):
    """Where the API token came from, for the ``Fix:`` line."""
    src = getattr(args, "token_source", None) or "environment"
    return "your environment variables" if src == "environment" else src


def _bad_token(args, raw=None):
    return CliError("Confluence did not accept your email/API token.",
                    f"Check CONFLUENCE_EMAIL and CONFLUENCE_API_TOKEN in {_token_where(args)} "
                    f"(create a new token at {TOKEN_URL}).", raw)


def _check_identity(args):
    """B1/AC-3.1.3: one read-only identity check before planning.

    Confluence Cloud serves a bad email/token as an anonymous request, so a
    wrong token would otherwise surface later as a misleading 404.
    """
    me = whoami()
    if not me.get("authenticated"):
        how = (f"Confluence said: {me['status']}" if me.get("status") not in (None, 200)
               else "Confluence treated the request as anonymous.")
        raise _bad_token(args, how)


def _read_error(e, args, stage):
    """Map an ``HTTPError`` from the read-only phase, naming what failed.

    :param stage: ``"space"`` (space-key lookup) or ``"parent"`` (planning
        reads, starting with the ``--parent`` page).
    """
    if e.code == 404 and stage == "space":
        return _no_space(args, _http_error(e, args).detail)
    return _http_error(e, args)


def _url_error(e):
    """Map a network/TLS ``URLError`` to a :class:`CliError`."""
    base = os.environ.get("CONFLUENCE_BASE_URL", "(unset)")
    reason = getattr(e, "reason", e)
    if isinstance(reason, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(reason):
        return CliError("Python could not verify Confluence's TLS certificate.",
                        "If you use the python.org build on macOS, run "
                        "'/Applications/Python 3.12/Install Certificates.command' once "
                        "(never disable verification). See KNOWN_ISSUES.md.", str(reason))
    return CliError(f"Could not reach Confluence at {base}.",
                    "Check CONFLUENCE_BASE_URL (it ends in /wiki) and your network or VPN.",
                    str(reason))


def _mask_secrets(text):
    """Replace the API token, if it appears, with ``****`` (AC-X.6)."""
    token = os.environ.get("CONFLUENCE_API_TOKEN")
    return text.replace(token, "****") if token and len(token) >= 4 else text


# --- main ---------------------------------------------------------------------

def _load_credentials(args):
    """Resolve credentials (S1.1) into ``os.environ`` or raise :class:`CliError`."""
    try:
        resolved = load_env(env_file=args.env_file)
    except FileNotFoundError as e:
        path = e.filename or args.env_file
        raise CliError(f"The --env-file {path} does not exist.",
                       "Pass the path of an existing .env file, or drop --env-file to use "
                       "./.env or ~/.config/confluence-publisher/.env.") from None
    apply_env(resolved)
    args.token_source = (resolved.get("CONFLUENCE_API_TOKEN") or (None, "environment"))[1]
    missing = [v for v in CRED_VARS if not os.environ.get(v)]
    if missing:
        searched = ["environment variables"] + [str(p) for p in env_search_paths(env_file=args.env_file)]
        raise CliError(
            "Missing Confluence credential(s): " + ", ".join(missing) + ".",
            "Add them as KEY=VALUE lines (see .env.example) to ./.env or "
            "~/.config/confluence-publisher/.env, or export them.",
            "Searched, in order:\n" + "\n".join(f"- {s}" for s in searched))


def _check_titles(docs):
    """Pre-flight duplicate-title check (v1.1), before any network call."""
    titles = [f.stem for f in docs.glob("*.md")]
    insitu = find_insitu_dir(docs)
    if insitu is not None:
        titles.append("In Situ")
        titles += [f.stem for f in insitu.glob("*.md")]
    dupes = sorted({t for t in titles if titles.count(t) > 1})
    if dupes:
        raise CliError(
            "Two or more files in this doc set resolve to the same page title, and "
            "Confluence requires titles to be unique within a space.",
            "Rename the colliding file(s) so every page has a distinct title.",
            "\n".join(f"- {t!r}" for t in dupes))


def _run(args, out):
    """The body of :func:`main`; raises :class:`CliError` for expected failures."""
    _load_credentials(args)
    docs = pathlib.Path(args.docs_dir)
    if not docs.is_dir():
        raise CliError(f"Not a folder: {args.docs_dir}.",
                       'Pass the folder that holds the "NN - Title.md" docs.')
    _check_titles(docs)

    stage = "space"
    try:
        _check_identity(args)
        try:
            space_id = resolve_space_id(args.space)
        except SystemExit:
            raise _no_space(args) from None
        stage = "parent"
        img_dir = args.img_dir or str(docs / ".img")
        plan = build_plan(args.docs_dir, space_id, args.parent, img_dir,
                          prefix=args.prefix, img_width=args.img_width)
    except urllib.error.HTTPError as e:
        raise _read_error(e, args, stage) from None
    refusals = check_plan(plan, max_updates=args.max_updates)

    prefix_note = f", prefix {args.prefix!r}" if args.prefix else ""
    parent_link = _page_link((plan.parent_info or {}).get("webui"), args.parent)

    if args.dry_run or refusals:
        _print_table(plan.items, out)
        _print_ownership(plan, out, refusals)
        if refusals:
            raise _refused(refusals)
        if handover_items(plan):
            print("\nThe real run will ask you to type the page count to hand them over "
                  "(or pass --adopt when not in a terminal).", file=out)
        print(f"\n{len(plan.items)} pages DRY-RUN (nothing written) — space {args.space}, "
              f"parent {args.parent}{prefix_note}", file=out)
        print(f"Parent page: {parent_link}", file=out)
        return

    confirm = None
    if not args.adopt and _stdin_is_tty():
        confirm = lambda items: confirm_handover(items, out=out)   # noqa: E731
    try:
        report = execute_plan(plan, max_updates=args.max_updates, adopt=args.adopt,
                              confirm_handover=confirm)
    except PlanRefused as e:                       # re-check inside execute_plan
        raise _refused(e.refusals) from None
    except HandoverRequired as e:
        n = len(e.items)
        raise CliError(
            f"{_need(n)} a one-time hand-over and this is not an interactive "
            "terminal — nothing was written.",
            f"Run it in a terminal and type {n} when asked, or add --adopt to take them over.",
            "\n".join(_describe_page(it).strip() for it in e.items)) from None
    except HandoverDeclined as e:
        n = len(e.items)
        raise CliError("Hand-over not confirmed — nothing was written.",
                       f"Run again and type {n} to take the page(s) over, or use a "
                       "distinct --prefix to publish new pages instead.") from None
    except PlanExecutionError as e:
        raise _partial(e.report, args) from None

    _print_table(plan.items, out)
    if args.adopt and handover_items(plan):
        _print_ownership(plan, out, adopted=True)
    for it, pid, exc in report.marker_failures:
        print(f"\nWarning: {it.title} was written but could not be marked as managed "
              f"({exc}); the next run will ask to hand it over once.", file=out)
    print(f"\n{len(plan.items)} pages published — space {args.space}, "
          f"parent {args.parent}{prefix_note}", file=out)
    print(f"Parent page: {parent_link}", file=out)


def _partial(report, args):
    """AC-0.1.4: a write failed mid-run — say what was and wasn't written."""
    cause = report.error
    if isinstance(cause, urllib.error.HTTPError):
        why = _http_error(cause, args)
    elif isinstance(cause, urllib.error.URLError):
        why = _url_error(cause)
    else:
        why = CliError(str(cause), "")
    partial = getattr(report, "partial", None)
    written = "\n".join(f"- {it.title}" for it, _ in report.written) or "- (none)"
    pending = "\n".join(
        f"- {it.title}" + ("  (created, but its diagrams/body did not finish)"
                           if partial and partial[0] is it else "")
        for it in report.pending) or "- (none)"
    failed = partial[0] if partial else (report.pending[0] if report.pending else None)
    if isinstance(cause, urllib.error.HTTPError) and cause.code == 409 and failed is not None:
        # update_page sends the plan-time version: someone saved the page meanwhile.
        return CliError(
            f'"{failed.title}" was edited in Confluence while this run was publishing, '
            "so it was not overwritten.",
            "Re-run with --dry-run to see its new state, then publish again.",
            f"{why.detail}\n\nWritten ({len(report.written)}):\n{written}\n"
            f"Not written ({len(report.pending)}):\n{pending}")
    detail = (f"{why.sentence}\n{why.detail or ''}\n\n"
              f"Written ({len(report.written)}):\n{written}\n"
              f"Not written ({len(report.pending)}):\n{pending}").replace("\n\n\n", "\n\n")
    rerun = "Then run the same command again; it is safe to re-run."
    if why.fix and "command again" in why.fix:
        fix = why.fix + " It is safe to re-run."
    else:
        fix = (why.fix + " " if why.fix else "") + rerun
    return CliError("The run stopped because a write failed — some pages were written, "
                    "the rest were not.", fix, detail)


def main(argv=None, out=None):
    """Parse arguments, plan, check, and publish the doc set.

    Expected failures (credentials, bad folder, refusals, hand-over, Confluence
    HTTP/TLS errors) exit with one sentence and a ``Fix:`` line, never a
    traceback (S3.1).

    :param argv: argument list (``sys.argv[1:]`` when ``None``).
    :param out: stream for normal output (stdout when ``None``).
    :raises SystemExit: on any expected failure (exit status 1) or bad arguments (2).
    """
    args = build_parser().parse_args(argv)
    out = out or sys.stdout
    try:
        _run(args, out)
    except CliError as e:
        raise SystemExit(_mask_secrets(str(e))) from None
    except urllib.error.HTTPError as e:
        raise SystemExit(_mask_secrets(str(_http_error(e, args)))) from None
    except urllib.error.URLError as e:
        raise SystemExit(_mask_secrets(str(_url_error(e)))) from None


if __name__ == "__main__":
    main()
