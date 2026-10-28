"""Confluence Cloud heading anchors (H3, D36): pure, from the heading text alone.

Measured on Confluence Cloud (the H3.0 spike): the live page gives each heading
the id "its shown text, trimmed, every whitespace character -> ``-``", and
numbers repeats ``.1``, ``.2`` across every level. A ``<ac:link ac:anchor="id">``
jumps there, and the editor keeps it on save. :mod:`.storage` writes the
links; this module only computes ids. Offline: it imports only
:mod:`.markdown_html` (for the shown text of inline Markdown).
"""
import html as ihtml
import re

from .markdown_html import MarkdownRenderer

#: ``[[wikilink]]``; shared with :mod:`.storage` (one pattern).
WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")

_TAG_RE = re.compile(r"<[^>]+>")


def heading_plain(text: str, inline=None) -> str:
    """The text Confluence shows for a heading written ``text``: inline Markdown
    rendered by ``inline`` (``MarkdownRenderer().inline`` by default) with its
    tags dropped (`` `code` `` and ``**bold**`` keep only their words), entities
    decoded, and a ``[[link|alias]]`` reduced to what it displays."""
    inline = inline or MarkdownRenderer().inline

    def shown(m):
        content = m.group(1)
        return content.split("|", 1)[1] if "|" in content else content.split("#", 1)[0]
    rendered = inline(WIKILINK_RE.sub(lambda m: shown(m).strip(), text))
    return ihtml.unescape(_TAG_RE.sub("", rendered)).strip()


def heading_anchor(plain: str) -> str:
    """Confluence Cloud's id for a heading whose shown text is ``plain`` (D36):
    trimmed, then every whitespace character becomes ``-``; nothing else changes
    (``6. A — b`` -> ``6.-A-—-b``). A repeat gets ``.1``, ``.2``: see
    :class:`HeadingAnchors`."""
    return re.sub(r"\s", "-", plain.strip())


def _heading_key(text: str) -> str:
    """A lenient match key: Obsidian link text may drop characters (``:``, ``#``…)."""
    return "".join(ch for ch in text.casefold() if ch.isalnum())


class HeadingAnchors:
    """The Confluence anchors of one note's headings (:func:`note_headings`), with
    repeats numbered as Confluence does: the first bare, then ``.1``, ``.2``,
    counted across every level, in order. A suffixed id already taken (a heading
    literally named ``Repeated.1``) is skipped, and a heading whose own id is
    taken is suffixed again: ``Repeated.1.1`` (measured live by QA, B2).

    :param headings: the note's :class:`Heading` list.
    :param inline: the inline renderer (see :func:`heading_plain`).
    """

    def __init__(self, headings, inline=None):
        self.entries, used, last = [], set(), {}
        for h in headings:
            plain = heading_plain(h.text, inline)
            anchor = base = heading_anchor(plain)
            if base in used:
                n = last.get(base, 0) + 1
                while f"{base}.{n}" in used:
                    n += 1
                last[base], anchor = n, f"{base}.{n}"
            used.add(anchor)
            self.entries.append((h.text, plain, anchor, h.in_callout))

    def find(self, text: str):
        """The anchor of the first heading ``text`` names, or ``None``.

        Exact text first, then the shown text, then :func:`_heading_key` (case,
        spaces and punctuation ignored), so ``[[#Notes final]]`` finds ``Notes: final``.
        Each step prefers a heading outside a callout to one inside (B3).
        """
        key = _heading_key(text)
        ordered = sorted(self.entries, key=lambda e: e[3])    # stable: body headings first
        for pick in (lambda e: e[0] == text, lambda e: e[1] == text,
                     lambda e: key != "" and _heading_key(e[0]) == key):
            for entry in ordered:
                if pick(entry):
                    return entry[2]
        return None

    @staticmethod
    def guess(text: str):
        """The anchor for a heading known only by the link text (another page)."""
        return heading_anchor(heading_plain(text)) if text else None
