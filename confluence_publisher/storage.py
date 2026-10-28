"""Markdown -> Confluence storage format (pure, offline: never imports :mod:`.api`).

:class:`StorageConverter` runs :class:`~.markdown_html.MarkdownRenderer` and
upgrades its generic HTML to Confluence storage XHTML (code macros, callout
panels, ``ac:image`` attachments, ``[[wikilinks]]`` -> ``ac:link``), returning a
validated :class:`StorageDocument`. The step order inside
:meth:`StorageConverter.convert` is load-bearing (see CLAUDE.md).
"""
import re
import pathlib
import dataclasses
import html as ihtml
import xml.etree.ElementTree as ET

from .markdown_html import MarkdownRenderer, MermaidRenderer, strip_frontmatter


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
_TOC_RE = re.compile(r"^\s*\[\[?TOC\]?\]\s*$", re.IGNORECASE)
_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
_IMG_RE = re.compile(r'<img src="img/([^"]+)"[^>]*/>')
_VALIDATE_WRAP = ('<root xmlns:ac="urn:ac" xmlns:ri="urn:ri">{}</root>')
_TOC_MACRO = ('<ac:structured-macro ac:name="toc" ac:schema-version="1">'
              '<ac:parameter ac:name="maxLevel">2</ac:parameter>'
              "</ac:structured-macro>")


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


# --------------------------------------------------------------------------- #
#  Pre-conversion extractors (token masking)
# --------------------------------------------------------------------------- #

class FenceAwareExtractor:
    """Base of every pre-conversion extractor: the **one** code-fence rule.

    An extractor swaps a Markdown construct for an inert token before the bulk
    conversion and returns ``{token: storage_markup}`` to restore afterwards.
    Lines inside a fenced code block are never touched: subclasses only supply
    the pattern, as a ``handle`` callback to :meth:`rewrite`.
    """

    @staticmethod
    def is_fence(line: str) -> bool:
        """Return whether ``line`` opens or closes a fenced code block.

        The single fence rule shared by every pre-conversion extractor
        (:class:`TocExtractor`, :class:`WikilinkExtractor`, :class:`CalloutExtractor`),
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

    def rewrite(self, lines, handle):
        """Apply ``handle`` to every line outside a fence; keep fenced lines as-is.

        :param lines: the Markdown lines (with or without their newlines).
        :param handle: ``(lines, i) -> (next_i, replacement_lines) | None``; it
            may consume several lines (a callout body). ``None`` keeps line ``i``.
        :returns: the rewritten lines.
        :rtype: list[str]
        """
        out, i, in_fence = [], 0, False
        while i < len(lines):
            line = lines[i]
            if self.is_fence(line):
                in_fence = not in_fence
            elif not in_fence:
                hit = handle(lines, i)
                if hit is not None:
                    i, replacement = hit
                    out.extend(replacement)
                    continue
            out.append(line)
            i += 1
        return out

    def extract(self, md: str):
        """Return ``(markdown_with_tokens, {token: markup})``."""
        raise NotImplementedError


class TocExtractor(FenceAwareExtractor):
    """Swap a standalone ``[[TOC]]`` / ``[TOC]`` line for a Table-of-Contents token.

    Must run *before* :class:`WikilinkExtractor`, otherwise ``[[TOC]]`` would be
    captured as a page wikilink. The token is restored as a Confluence ``toc``
    macro through the same ``<p>token</p>`` swap the callouts use, so the macro
    lands as a top-level element.
    """

    def extract(self, md: str):
        macros = {}

        def handle(lines, i):
            if not _TOC_RE.match(lines[i]):
                return None
            token = f"xTOC{len(macros)}x"
            macros[token] = _TOC_MACRO
            return i + 1, ["", token, ""]

        return "\n".join(self.rewrite(md.split("\n"), handle)), macros


class WikilinkExtractor(FenceAwareExtractor):
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

    :param prefix: namespacing prefix applied to each link target, matching the
        prefix used when publishing the pages.
    """

    def __init__(self, prefix: str = ""):
        self.prefix = prefix

    def extract(self, md: str):
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
            links[token] = _wikilink_macro(prefixed_title(self.prefix, target), display)
            return token

        out = self.rewrite(md.splitlines(keepends=True),
                           lambda lines, i: (i + 1, [_WIKILINK_RE.sub(repl, lines[i])]))
        return "".join(out), links


class CalloutExtractor(FenceAwareExtractor):
    """Pull Obsidian callouts (``> [!type] ...``) out as Confluence panel macros.

    In the returned Markdown each callout is a placeholder paragraph the bulk
    converter passes through untouched. The body is rendered by ``renderer``,
    which (as in v1.1) has no Mermaid renderer: a diagram inside a callout
    stays the styled fallback block.

    :param renderer: the :class:`~.markdown_html.MarkdownRenderer` for bodies.
    """

    def __init__(self, renderer: MarkdownRenderer):
        self.renderer = renderer

    def extract(self, md: str):
        macros = {}

        def handle(lines, i):
            m = _CALLOUT_RE.match(lines[i])
            if not m:
                return None
            ctype = m.group(1).lower()
            title = m.group(2).strip()
            i += 1
            body = []
            while i < len(lines) and lines[i].startswith(">"):
                body.append(re.sub(r"^>\s?", "", lines[i])); i += 1
            macro = CALLOUT_MACRO.get(ctype, "info")
            body_html = self.renderer.render("\n".join(body)) if body else ""
            title_html = f"<p><strong>{self.renderer.inline(title)}</strong></p>\n" if title else ""
            token = f"xCALLOUT{len(macros)}x"
            macros[token] = (
                f'<ac:structured-macro ac:name="{macro}"><ac:rich-text-body>\n'
                f"{title_html}{body_html}\n"
                f"</ac:rich-text-body></ac:structured-macro>"
            )
            return i, ["", token, ""]

        return "\n".join(self.rewrite(md.split("\n"), handle)), macros


# --------------------------------------------------------------------------- #
#  The converter
# --------------------------------------------------------------------------- #

@dataclasses.dataclass
class StorageDocument:
    """A Confluence storage-format body and the attachments it references.

    :param html: the storage XHTML fragment.
    :param attachments: ``(filename, filepath)`` pairs to upload before saving.
    :param unrendered: Mermaid diagrams left as code blocks because npx was not found.
    """
    html: str
    attachments: list = dataclasses.field(default_factory=list)
    unrendered: int = 0

    def validate(self) -> None:
        """Check the body is well-formed XML.

        :raises xml.etree.ElementTree.ParseError: if it is not (with the position).
        """
        ET.fromstring(_VALIDATE_WRAP.format(self.html))


class StorageConverter:
    """Markdown -> validated :class:`StorageDocument`.

    :param prefix: namespacing prefix applied to wikilink targets so internal
        links resolve to the prefixed published page titles.
    :param img_dir: where to render Mermaid PNGs (and upload them from);
        ``None`` skips diagram rendering.
    :param img_width: optional display width in pixels for every Mermaid image
        (``ac:width``); ``None`` leaves the images at their natural size.
    :param renderer: the body renderer; by default a
        :class:`~.markdown_html.MarkdownRenderer` with a
        :class:`~.markdown_html.MermaidRenderer` on ``img_dir`` (none when
        ``img_dir`` is ``None``).
    :param npx: ``(command, child_env)`` or ``None`` (not found), for that
        default :class:`~.markdown_html.MermaidRenderer`.
    """

    def __init__(self, prefix: str = "", img_dir=None, img_width=None, renderer=None,
                 npx=("npx", None)):
        self.prefix, self.img_dir, self.img_width = prefix, img_dir, img_width
        if renderer is None:
            mermaid = None if img_dir is None else MermaidRenderer(img_dir, "img", npx=npx)
            renderer = MarkdownRenderer(mermaid)
        self.renderer = renderer
        self.toc = TocExtractor()
        self.wikilinks = WikilinkExtractor(prefix)
        self.callouts = CalloutExtractor(MarkdownRenderer())   # bodies: never Mermaid (v1.1)

    def attach_images(self, html: str):
        """Rewrite rendered Mermaid ``<img>`` tags as Confluence ``ac:image`` macros.

        Uses this converter's ``img_dir`` (where the PNGs live) and ``img_width``
        (``ac:width`` on every image; ``None`` emits no attribute).

        :param html: the HTML fragment containing ``<img src="img/NAME.png">`` tags.
        :type html: str
        :returns: a ``(html, attachments)`` pair, where ``attachments`` is a list of
            ``(filename, filepath)`` tuples to upload.
        :rtype: tuple[str, list[tuple[str, str]]]
        """
        names = []
        width_attr = f' ac:width="{int(self.img_width)}"' if self.img_width else ""

        def repl(m):
            names.append(m.group(1))
            return f'<ac:image{width_attr}><ri:attachment ri:filename="{m.group(1)}" /></ac:image>'

        html = _IMG_RE.sub(repl, html)
        atts = [(fn, str(pathlib.Path(self.img_dir) / fn)) for fn in names]
        return html, atts

    def convert(self, md: str) -> StorageDocument:
        """Convert Markdown to a validated storage document. The order is load-bearing.

        :raises xml.etree.ElementTree.ParseError: if the body is not well-formed.
        """
        md = strip_frontmatter(md)
        md, tocs = self.toc.extract(md)            # before wikilinks: [[TOC]] is not a page link
        md, links = self.wikilinks.extract(md)     # before conversion (converter flattens them)
        md, macros = self.callouts.extract(md)
        if self.img_dir is not None:
            pathlib.Path(self.img_dir).mkdir(parents=True, exist_ok=True)
        mermaid = getattr(self.renderer, "mermaid", None)
        skipped = getattr(mermaid, "skipped", 0)
        html = self.renderer.render(md)            # renders Mermaid to PNG when img_dir set
        for token, macro in {**macros, **tocs}.items():
            html = html.replace(f"<p>{token}</p>", macro)
        html, atts = self.attach_images(html) if self.img_dir else (html, [])
        html = _fix_autolinks(html)
        html = _escape_bare_amp(html)
        html = _code_blocks_to_macro(html)  # last: its CDATA must escape the amp-escape
        for token, macro in links.items():
            html = html.replace(token, macro)
        doc = StorageDocument(html, atts, getattr(mermaid, "skipped", 0) - skipped)
        doc.validate()
        return doc
