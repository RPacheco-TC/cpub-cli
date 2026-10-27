"""Markdown -> Confluence storage format (pure, offline: never imports :mod:`.api`).

:mod:`.markdown_html` produces generic HTML; this module upgrades it to
Confluence storage XHTML (code macros, callout panels, ``ac:image``
attachments, ``[[wikilinks]]`` -> ``ac:link``) and validates it as XML.
The step order inside :func:`md_to_storage` is load-bearing (see CLAUDE.md).
"""
import re
import pathlib
import html as ihtml
import xml.etree.ElementTree as ET

from . import markdown_html as mdc


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


