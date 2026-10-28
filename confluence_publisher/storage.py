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

from .anchors import WIKILINK_RE as _WIKILINK_RE, HeadingAnchors
from .markdown_html import MarkdownRenderer, outside_code, strip_frontmatter
from .mermaid import MermaidRenderer


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


def _xml_text(text: str, attr: bool = False) -> str:
    """Escape ``&``, ``<``, ``>`` (and ``"`` for an attribute value) for storage XHTML.

    The one escape helper for link titles and link text (B10). Apostrophes are
    left alone: attributes here are double-quoted, and titles with ``'`` were
    already valid, so their output stays byte-identical.
    """
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return text.replace('"', "&quot;") if attr else text


def _cdata(text: str) -> str:
    """Wrap ``text`` in CDATA; a literal ``]]>`` is split so it cannot end it early.
    (CDATA is not entity-decoded, so its content must not be escaped.)"""
    return "<![CDATA[" + text.replace("]]>", "]]]]><![CDATA[>") + "]]>"


def _wikilink_macro(target_title, display: str, anchor=None) -> str:
    """Build a Confluence ``ac:link`` that points at a page by title, or at a heading.

    :param target_title: the exact title of the target page; ``None`` for a
        heading of the same page (no ``ri:page``).
    :param display: the link text to show.
    :param anchor: Confluence's heading id (:func:`~.anchors.heading_anchor`, D36), or
        ``None`` for a page link (byte-identical to v1.1).
    :returns: the ``ac:link`` storage-format markup.
    :rtype: str
    """
    head = "<ac:link>" if anchor is None else f'<ac:link ac:anchor="{_xml_text(anchor, True)}">'
    page = ("" if target_title is None else
            f'<ri:page ri:content-title="{_xml_text(target_title, attr=True)}" />')
    return (f"{head}{page}"
            f"<ac:plain-text-link-body>{_cdata(display)}</ac:plain-text-link-body></ac:link>")


@dataclasses.dataclass(frozen=True)
class Wikilink:
    """One extracted ``[[wikilink]]``: what its ``xLINKNx`` token is restored to.

    :param target: the (prefixed) title of the page it points at.
    :param display: the text shown (alias, else the target without its anchor).
    :param name: the title that was looked up, for messages: the prefixed
        target without its anchor (``#Heading`` for a same-note link).
    :param anchor: the Confluence heading id it jumps to (H3, D36), or ``None``.
    :param same_note: a ``[[#Heading]]`` link: no page, only the anchor.
    :param missing: a same-note link whose heading the note does not have.
    """
    target: str
    display: str
    name: str = ""
    anchor: str | None = None
    same_note: bool = False
    missing: bool = False

    @property
    def macro(self) -> str:
        """The ``ac:link`` it becomes; without an anchor byte-identical to v1.1."""
        if self.same_note and self.anchor is None:     # [[#^block]]: nothing to jump to
            return self.text
        return _wikilink_macro(None if self.same_note else self.target, self.display,
                               self.anchor)

    def resolved(self, exists) -> bool:
        """Whether it stays a link: ``exists`` (``title -> bool``, note mode) is asked
        about its page; a same-note link needs its heading. ``None`` keeps it (set mode)."""
        if exists is None:
            return True
        return not self.missing if self.same_note else exists(self.target)

    @property
    def text(self) -> str:
        """What it becomes when the page does not exist (S2.5): the text, in ``<em>``."""
        return f"<em>{_xml_text(self.display)}</em>"


def _restore_links(html, links, exists=None):
    """The ``xLINKNx`` restore step (the last one): every token back to its link, or
    to plain text when ``exists`` says its page is missing (S2.5). ``exists=None``
    links everything, as v1.1 does."""
    for token, link in links.items():
        html = html.replace(token, link.macro if link.resolved(exists) else link.text)
    return html


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

    Handles ``[[Target]]``, ``[[Target#Heading]]``, ``[[#Heading]]`` and
    ``[[Target|alias]]``. A page is linked *by title*, and the published title is
    the prefixed one (:func:`prefixed_title`), so the target is prefixed too; the
    display text is not. A heading keeps its anchor (H3, D36): Confluence's own
    heading id, from the note's real heading when it is known
    (:class:`HeadingAnchors`), else from the link text. ``[[P#A#B]]`` names
    heading ``B``; a block reference ``[[P#^id]]`` stays a page link.
    ``[[P#H]]`` shows ``P`` and ``[[#H]]`` shows ``H`` (an alias wins over both).

    Wikilinks inside fenced code blocks are intentionally left untouched. They
    are source code examples, not document navigation, and converting them to
    storage macros can break XML validation after code-block CDATA wrapping.
    The same goes for inline code spans (B11), found with the renderer's own
    pattern so both agree on what is code: `` `[[Note]]` `` stays literal.

    :param prefix: namespacing prefix applied to each link target, matching the
        prefix used when publishing the pages.
    """

    def __init__(self, prefix: str = ""):
        self.prefix = prefix

    def extract(self, md: str, own=None, others=None):
        """:param own: the note's :class:`HeadingAnchors` (``None``: none known).
        :param others: ``bare title -> HeadingAnchors | None`` (set mode)."""
        links = {}

        def repl(m):
            content = m.group(1).strip()
            alias = None
            if "|" in content:
                content, alias = (s.strip() for s in content.split("|", 1))
            parts = content.split("#")
            target = parts[0].strip()
            sub = parts[-1].strip() if len(parts) > 1 else ""
            heading = "" if sub.startswith("^") else sub
            token = f"xLINK{len(links)}x"
            if not target and len(parts) > 1:
                links[token] = self._same_note(heading, alias or heading or content,
                                               f"#{sub}", own)
                return token
            title = prefixed_title(self.prefix, target)
            known = others(target) if others is not None and heading else None
            anchor = (known and known.find(heading)) or HeadingAnchors.guess(heading)
            links[token] = Wikilink(title, alias or target or content,
                                    title if target else content, anchor)
            return token

        def line_links(line):               # B11: links inside `code` stay as written
            return outside_code(line, lambda part: _WIKILINK_RE.sub(repl, part))

        out = self.rewrite(md.splitlines(keepends=True),
                           lambda lines, i: (i + 1, [line_links(lines[i])]))
        return "".join(out), links

    @staticmethod
    def _same_note(heading, display, name, own):
        """A ``[[#Heading]]`` link; ``missing`` when the note has no such heading."""
        anchor = own.find(heading) if own is not None and heading else None
        missing = anchor is None and (own is not None or not heading)
        if missing and heading:             # set mode keeps it: it stays on the page
            anchor = HeadingAnchors.guess(heading)
        return Wikilink("", display, name, anchor, same_note=True, missing=missing)


def callout_at(lines, i):
    """The callout at line ``i``: ``(title match, next line, body without >)`` or ``None``
    (the one callout rule, shared by the extractor and :func:`note_headings`)."""
    m = _CALLOUT_RE.match(lines[i])
    if not m:
        return None
    j = i + 1
    while j < len(lines) and lines[j].startswith(">"):
        j += 1
    return m, j, [re.sub(r"^>\s?", "", line) for line in lines[i + 1:j]]


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
            hit = callout_at(lines, i)
            if hit is None:
                return None
            m, i, body = hit
            ctype = m.group(1).lower()
            title = m.group(2).strip()
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
    :param links: ``{token: Wikilink}`` of the body, in order of appearance.
    :param template: the body before the link restore step (tokens still in it),
        so :meth:`relink` can redo only that step once titles are known (S2.5).
    """
    html: str
    attachments: list = dataclasses.field(default_factory=list)
    unrendered: int = 0
    links: dict = dataclasses.field(default_factory=dict, repr=False)
    template: str | None = dataclasses.field(default=None, repr=False)

    def unresolved(self, exists) -> list:
        """Titles whose page is missing (``Wikilink.name``: prefixed, no anchor),
        distinct, in order of appearance.

        :param exists: ``title -> bool`` (asked with the prefixed title).
        :rtype: list[str]
        """
        out = []
        for link in self.links.values():
            if not link.resolved(exists) and link.name not in out:
                out.append(link.name)
        return out

    def relink(self, exists) -> "StorageDocument":
        """This document with links to missing pages restored as text (S2.5): only
        the ``xLINKNx`` restore step is redone; the result is validated.

        :param exists: ``title -> bool``.
        """
        if self.template is None or not self.links:
            return self
        doc = dataclasses.replace(self, html=_restore_links(self.template, self.links, exists))
        doc.validate()
        return doc

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
        :class:`~.mermaid.MermaidRenderer` on ``img_dir`` (none when
        ``img_dir`` is ``None``).
    :param npx: ``(command, child_env)`` or ``None`` (not found), for that
        default :class:`~.mermaid.MermaidRenderer`.
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

    def convert(self, md: str, headings_of=None) -> StorageDocument:
        """Convert Markdown to a validated storage document. The order is load-bearing.

        :param headings_of: ``bare title -> list[Heading] | None``: the other
            documents' headings (set mode), so ``[[NN - Title#H]]`` jumps to the
            real heading; ``None`` computes such anchors from the link text.
        :raises xml.etree.ElementTree.ParseError: if the body is not well-formed.
        """
        inline = self.renderer.inline
        own = HeadingAnchors(note_headings(md), inline)     # before frontmatter is cut
        others = None
        if headings_of is not None:
            def others(title):
                found = headings_of(title)
                return None if found is None else HeadingAnchors(found, inline)
        md = strip_frontmatter(md)
        md, tocs = self.toc.extract(md)            # before wikilinks: [[TOC]] is not a page link
        md, links = self.wikilinks.extract(md, own, others)   # before conversion (flattened)
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
        template = html
        html = _restore_links(html, links)  # every link, as v1.1; StorageDocument.relink (S2.5)
        doc = StorageDocument(html, atts, getattr(mermaid, "skipped", 0) - skipped,
                              links, template)
        doc.validate()
        return doc


# --------------------------------------------------------------------------- #
#  Headings of a note (one parser: H2 section references, H3 same-note anchors)
# --------------------------------------------------------------------------- #

_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$")
_HEADING_NUMBER_RE = re.compile(r"(\d+(?:\.\d+)*)(\.?)(?=\s|$)")


@dataclasses.dataclass(frozen=True)
class Heading:
    """One ATX heading of a note.

    :param level: 1-6 (the number of ``#``).
    :param text: the heading as written, without the ``#`` marks: what an Obsidian
        ``[[#Heading]]`` link names.
    :param line: 1-based line number in the file (frontmatter counted).
    :param in_callout: inside an Obsidian callout (rendered as a heading in the panel).
    """
    level: int
    text: str
    line: int
    in_callout: bool = False

    @property
    def number(self):
        """``"3"`` for ``3. Title``, ``"3.2"`` for ``3.2 Title`` / ``3.2. Title``;
        ``None`` for an unnumbered heading (``7 Title`` and ``2026 plans`` too)."""
        m = _HEADING_NUMBER_RE.match(self.text)
        if m and (m.group(2) or "." in m.group(1)):
            return m.group(1)
        return None


def note_headings(md: str) -> list:
    """Every ATX heading of ``md`` outside code fences and frontmatter, in order: the
    ``<hN>`` of the body, so callout bodies count and ``> ##`` quotes do not (B3).

    The one heading parser: it shares the extractors' fence rule (``is_fence``) and
    callout rule (:func:`callout_at`); line numbers are file lines.

    :param md: the note's whole text.
    :rtype: list[Heading]
    """
    lines = md.split("\n")
    first, found = _body_start(lines), []

    def add(line, number, in_callout=False):
        m = _HEADING_RE.match(line)
        if m and m.group(2):
            found.append(Heading(len(m.group(1)), m.group(2), number, in_callout))

    def handle(lines, i):
        if i < first:
            return None
        hit = callout_at(lines, i)
        if hit is None:
            add(lines[i], i + 1)
            return None
        _, end, body = hit
        in_fence = False                        # a fence inside the callout body
        for k, line in enumerate(body):
            if FenceAwareExtractor.is_fence(line):
                in_fence = not in_fence
            elif not in_fence:
                add(line, i + 2 + k, True)
        return end, lines[i:end]

    FenceAwareExtractor().rewrite(lines, handle)
    return found


# --------------------------------------------------------------------------- #
#  Plain-text section references (H2): a warning, never part of the body
# --------------------------------------------------------------------------- #

_SECTION_REF_RE = re.compile(r"§[ \u00a0]?(\d+(?:\.\d+)*)")
_LINKED_TEXT_RE = re.compile(r"\[\[[^\]]+\]\]|\[[^\]]*\]\([^)]*\)")  # [[…]], [text](url)


class SectionRefFinder(FenceAwareExtractor):
    """Find bare ``§N`` / ``§N.M`` text that names a numbered heading of the same note.

    Such a reference is a link in the writer's head but plain text on the page
    (H2): it is reported so the writer links it as ``[[#Heading|§N]]``. Only a
    reference whose number matches a heading numbered ``N.`` or ``N.M``
    (:attr:`Heading.number`, from :func:`note_headings`) counts: a ``§`` pointing
    into another document is left alone. Code is skipped with the extractors'
    own rules (fences: :meth:`is_fence`; inline spans:
    :func:`~.markdown_html.outside_code`), and so is text that is already a
    link (``[[…|§3]]``, ``[§3](…)``). Pure: it reads the Markdown, never
    changes the body.
    """

    def find(self, md: str) -> list:
        """Return distinct ``[(ref, line)]``, e.g. ``[("§3", 48)]``, in order of appearance.

        :param md: the note's whole text; frontmatter is skipped but counted,
            so ``line`` is the 1-based line number in the file.
        :rtype: list[tuple[str, int]]
        """
        numbers = {h.number for h in note_headings(md)} - {None}
        if not numbers:
            return []
        lines = md.split("\n")
        first, refs = _body_start(lines), []

        def scan(part, line_no):
            for m in _SECTION_REF_RE.finditer(_LINKED_TEXT_RE.sub(" ", part)):
                refs.append((m.group(1), line_no))
            return part

        def handle(lines, i):
            if i >= first:
                outside_code(lines[i], lambda part: scan(part, i + 1))
            return None

        self.rewrite(lines, handle)
        hits = [(f"§{n}", line) for n, line in refs if n in numbers]
        return list(dict.fromkeys(hits))    # "§1 and §1" on one line: listed once

    def extract(self, md: str):
        return md, {}                       # never rewrites: it only reports


def _body_start(lines) -> int:
    """Index of the first line after a leading ``---`` frontmatter block (0 if none)."""
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                return i + 1
    return 0
