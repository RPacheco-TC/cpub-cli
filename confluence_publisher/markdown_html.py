"""Generic Markdown -> HTML, used by :mod:`.storage`.

:class:`MarkdownRenderer` turns Obsidian-style Markdown into HTML (inline
formatting, tables, lists, callouts, code). Mermaid blocks go to the injected
:class:`~.mermaid.MermaidRenderer` (PNG via ``npx``), or become a styled block.
No module state: the image folder lives on that renderer, not in globals.
"""

import re


# --------------------------------------------------------------------------- #
#  Small pure helpers
# --------------------------------------------------------------------------- #


def strip_frontmatter(text: str) -> str:
    """Drop a leading YAML frontmatter block (``---`` ... ``---``) if present.

    :param text: the document text.
    :type text: str
    :returns: the text with any leading frontmatter removed.
    :rtype: str
    """
    if text.startswith('---\n') or text.startswith('---\r\n'):
        m = re.match(r'^---\r?\n.*?\r?\n---\r?\n?', text, re.DOTALL)
        if m:
            return text[m.end():]
    return text


#: An inline code span (one backtick each side); the one pattern for "is this code",
#: shared with :mod:`.storage`'s wikilink extraction (B11).
CODE_SPAN_RE = re.compile(r'`([^`]+)`')


def outside_code(text: str, func) -> str:
    """Apply ``func`` to the text between inline code spans; spans stay as written."""
    out, end = [], 0
    for span in CODE_SPAN_RE.finditer(text):
        out += [func(text[end:span.start()]), span.group()]
        end = span.end()
    return "".join(out) + func(text[end:])


def convert_wikilinks(text: str) -> str:
    """Convert Obsidian ``[[wikilinks]]`` to plain text.

    Internal links are not useful in Confluence, so they are flattened:
    ``[[Page|Alias]]`` becomes ``Alias``; ``[[Page#Heading]]`` becomes ``Page``;
    ``![[embed]]`` becomes ``embed`` (the leading ``!`` of an embed is dropped).
    Inside an inline code span a wikilink is code, so it is left as written (B11).

    :param text: the document text.
    :type text: str
    :returns: the text with wikilinks reduced to plain text.
    :rtype: str
    """
    def repl(m: re.Match) -> str:
        inner = m.group(1)
        if '|' in inner:
            return inner.split('|', 1)[1].strip()
        return inner.split('#', 1)[0].strip()
    return outside_code(text, lambda part: re.sub(r'!?\[\[([^\]]+)\]\]', repl, part))


def strip_glossary_links(text: str) -> str:
    """Remove glossary Markdown links, keeping the link text.

    :param text: the document text.
    :type text: str
    :returns: the text with glossary links replaced by their label.
    :rtype: str
    """
    return re.sub(r'\[([^\]]+)\]\(\.\./(?:\.\./)?glossary/[^)]+\)', r'\1', text)


def convert_internal_links(text: str) -> str:
    """Convert remaining internal Markdown links to plain text.

    Handles ``[t](file.md)``, ``[t](file.md#frag)`` and same-page section links
    ``[t](#anchor)`` — none of these resolve in Confluence, which generates its
    own heading anchors.

    :param text: the document text.
    :type text: str
    :returns: the text with internal links reduced to plain text.
    :rtype: str
    """
    return re.sub(r'\[([^\]]+)\]\((?:[^)]*\.md(?:#[^)]*)?|#[^)]*)\)', r'\1', text)


def escape_html(text: str) -> str:
    """Escape the HTML special characters ``&`` ``<`` ``>`` in text content.

    :param text: the raw text.
    :type text: str
    :returns: the escaped text.
    :rtype: str
    """
    text = text.replace('&', '&amp;')
    text = text.replace('<', '&lt;')
    text = text.replace('>', '&gt;')
    return text


# --------------------------------------------------------------------------- #
#  Markdown -> HTML
# --------------------------------------------------------------------------- #

class MarkdownRenderer:
    """Convert Obsidian-style Markdown to a generic HTML fragment.

    Holds no global state: the optional :class:`~.mermaid.MermaidRenderer` passed to the
    constructor decides whether Mermaid blocks become PNG ``<img>`` tags or the
    styled fallback block. A renderer without one never touches the disk.

    :param mermaid: the diagram renderer, or ``None`` for the fallback block.
    """

    def __init__(self, mermaid: "MermaidRenderer | None" = None):
        self.mermaid = mermaid

    def render(self, md_text: str) -> str:
        """Convert a Markdown document to an HTML fragment.

        Handles headings, paragraphs, lists, tables, blockquotes, Obsidian callouts,
        fenced code, and Mermaid blocks. When the renderer has a
        :class:`~.mermaid.MermaidRenderer`, Mermaid blocks are rendered to PNG; otherwise they
        fall back to a styled block.

        :param md_text: the Markdown document.
        :type md_text: str
        :returns: an HTML fragment (no ``<html>`` / ``<body>`` wrapper).
        :rtype: str
        """
        md_text = strip_glossary_links(md_text)

        lines = md_text.split('\n')
        html_parts = []
        i = 0

        while i < len(lines):
            line = lines[i]

            # Skip empty lines
            if not line.strip():
                i += 1
                continue

            # Horizontal rule
            if re.match(r'^---\s*$', line):
                html_parts.append('<hr />')
                i += 1
                continue

            # <details>/<summary> blocks
            if line.strip() == '<details>':
                i += 1
                summary_text = ''
                if i < len(lines) and '<summary>' in lines[i]:
                    summary_match = re.search(r'<summary>(.*?)</summary>', lines[i])
                    if summary_match:
                        summary_text = summary_match.group(1)
                    i += 1
                inner_lines = []
                while i < len(lines) and lines[i].strip() != '</details>':
                    inner_lines.append(lines[i])
                    i += 1
                if i < len(lines):
                    i += 1  # skip </details>
                inner_html = self.render('\n'.join(inner_lines))
                if summary_text:
                    html_parts.append(f'<p><strong>{self.inline(summary_text)}</strong></p>')
                html_parts.append(inner_html.rstrip())
                continue

            # Mermaid code blocks
            if line.strip().startswith('```mermaid'):
                mermaid_lines = []
                i += 1
                while i < len(lines) and not lines[i].strip().startswith('```'):
                    mermaid_lines.append(lines[i])
                    i += 1
                i += 1  # skip closing ```
                mermaid_code = '\n'.join(mermaid_lines)

                img_html = None
                if self.mermaid is not None:
                    img_html = self.mermaid.render(mermaid_code)

                if img_html:
                    html_parts.append(
                        f'<div style="margin: 12px 0;">\n'
                        f'{img_html}\n'
                        f'</div>'
                    )
                else:
                    mermaid_content = escape_html(mermaid_code)
                    html_parts.append(
                        f'<div style="border-left: 4px solid #0052CC; background: #f4f5f7; '
                        f'padding: 12px; margin: 12px 0;">\n'
                        f'<strong>Diagram (Mermaid)</strong>\n'
                        f'<pre><code>{mermaid_content}</code></pre>\n'
                        f'</div>'
                    )
                continue

            # Code blocks
            if line.strip().startswith('```'):
                lang_match = re.match(r'^```(\w*)', line.strip())
                code_lines = []
                i += 1
                while i < len(lines) and not lines[i].strip().startswith('```'):
                    code_lines.append(lines[i])
                    i += 1
                i += 1  # skip closing ```
                code_content = escape_html('\n'.join(code_lines))
                html_parts.append(f'<pre><code>{code_content}</code></pre>')
                continue

            # Callouts: > [!type] optional title  (any Obsidian callout type;
            # trailing +/- fold marker and an inline title are both supported)
            callout_match = re.match(r'^>\s*\[!(\w+)\][+-]?\s*(.*)$', line)
            if callout_match:
                callout_type = callout_match.group(1)
                inline_title = callout_match.group(2).strip()
                callout_lines = []
                i += 1
                while i < len(lines) and lines[i].startswith('>'):
                    # Keep blank ('>') lines: they are paragraph breaks inside the callout.
                    callout_lines.append(lines[i].lstrip('>').strip())
                    i += 1

                # Use the author's inline title if given, else the capitalized type.
                if inline_title:
                    header = f'<p><strong>{self.inline(inline_title)}</strong></p>\n'
                else:
                    header = f'<p><strong>{callout_type.capitalize()}:</strong></p>\n'
                callout_html = self._blockquote_split(
                    callout_lines,
                    f'<blockquote>\n{header}',
                    '</blockquote>'
                )
                html_parts.append(callout_html)
                continue

            # Blockquotes (non-callout)
            if line.startswith('>'):
                bq_lines = []
                while i < len(lines) and lines[i].startswith('>'):
                    content = lines[i].lstrip('>').strip()
                    bq_lines.append(content)
                    i += 1
                bq_html = self._blockquote_split(bq_lines, '<blockquote>\n', '</blockquote>')
                html_parts.append(bq_html)
                continue

            # Headings
            heading_match = re.match(r'^(#{1,6})\s+(.*)', line)
            if heading_match:
                level = len(heading_match.group(1))
                text = self.inline(heading_match.group(2))
                html_parts.append(f'<h{level}>{text}</h{level}>')
                i += 1
                continue

            # Tables
            if '|' in line and i + 1 < len(lines) and re.match(r'^[\s|:-]+$', lines[i + 1]):
                table_lines = []
                while i < len(lines) and '|' in lines[i]:
                    table_lines.append(lines[i])
                    i += 1
                html_parts.append(self._table(table_lines))
                continue

            # Checkbox lists — must precede the unordered-list test below, since
            # "- [ ] item" also matches the generic "^[-*]\s" list pattern.
            if re.match(r'^- \[[ xX]\]\s', line):
                items = []
                while i < len(lines) and re.match(r'^- \[[ xX]\]\s', lines[i]):
                    checked = lines[i][3] in ('x', 'X')
                    text = self.inline(lines[i][6:])
                    marker = '&#x2611;' if checked else '&#x2610;'
                    items.append(f'<li>{marker} {text}</li>')
                    i += 1
                html_parts.append('<ul>\n' + '\n'.join(items) + '\n</ul>')
                continue

            # Unordered lists
            if re.match(r'^(\s*)[-*]\s', line):
                html_parts.append(self._list_block(lines, i))
                while i < len(lines) and (re.match(r'^(\s*)[-*]\s', lines[i])
                                          or (lines[i].strip() and lines[i].startswith('  '))):
                    i += 1
                continue

            # Ordered lists
            if re.match(r'^(\s*)\d+\.\s', line):
                html_parts.append(self._ordered_list_block(lines, i))
                while i < len(lines) and (re.match(r'^(\s*)\d+\.\s', lines[i])
                                          or (lines[i].strip() and lines[i].startswith('  '))):
                    i += 1
                continue


            # Paragraph (default)
            para_lines = []
            while i < len(lines) and self._continues_paragraph(lines, i):
                para_lines.append(lines[i])
                i += 1
            if para_lines:
                text = self.inline(' '.join(para_lines))
                html_parts.append(f'<p>{text}</p>')
            continue

        return '\n\n'.join(html_parts) + '\n'


    @staticmethod
    def _continues_paragraph(lines, i) -> bool:
        """Whether line ``i`` belongs to the paragraph being collected."""
        line = lines[i]
        if not line.strip() or line.startswith(('#', '```', '>')):
            return False
        if (re.match(r'^---\s*$', line) or re.match(r'^[-*]\s', line)
                or re.match(r'^\d+\.\s', line)):
            return False
        return not ('|' in line and i + 1 < len(lines)
                    and re.match(r'^[\s|:-]+$', lines[i + 1]))

    def inline(self, text: str) -> str:
        """Process inline Markdown (bold, italic, code, links) into well-formed XHTML.

        Code spans and links are masked *before* emphasis and escaping, so that
        (a) stray ``*`` / ``_`` inside code (globs, regexes, generics) never create
        crossing ``<em>`` tags, and (b) literal ``<`` ``>`` ``&`` in ordinary text
        are escaped without corrupting the tags being generated. This is what keeps
        the output valid Confluence storage format.

        :param text: a single line or paragraph of Markdown.
        :type text: str
        :returns: the inline HTML.
        :rtype: str
        """
        text = strip_glossary_links(text)
        text = convert_wikilinks(text)
        text = convert_internal_links(text)

        store: list[str] = []

        def stash(html: str) -> str:
            store.append(html)
            return f'\x00{len(store) - 1}\x00'

        # Inline code `code` — escape content, then mask so emphasis/escaping skip it.
        text = CODE_SPAN_RE.sub(
            lambda m: stash('<code>' + escape_html(m.group(1)) + '</code>'), text)
        # External links [text](url)
        text = re.sub(r'\[([^\]]+)\]\((https?://[^)]+)\)',
                      lambda m: stash('<a href="' + m.group(2).replace('&', '&amp;') + '">'
                                      + escape_html(m.group(1)) + '</a>'), text)
        # Bare autolinks <http(s)://...>
        text = re.sub(r'<(https?://[^>\s]+)>',
                      lambda m: stash('<a href="' + m.group(1).replace('&', '&amp;') + '">'
                                      + m.group(1) + '</a>'), text)

        # Escape stray < > & in the remaining plain text (masked spans are inert \x00N\x00).
        text = escape_html(text)

        # Emphasis — safe now: no code/links inside, placeholders are inert.
        text = re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', text)
        text = re.sub(r'__([^_]+)__', r'<strong>\1</strong>', text)
        text = re.sub(r'(?<!\w)\*([^*]+)\*(?!\w)', r'<em>\1</em>', text)
        text = re.sub(r'(?<!\w)_([^_]+)_(?!\w)', r'<em>\1</em>', text)

        # Restore masked code/link spans.
        text = re.sub(r'\x00(\d+)\x00', lambda m: store[int(m.group(1))], text)
        return text


    def _table(self, lines: list[str]) -> str:
        """Convert Markdown table lines to an HTML ``<table>``.

        :param lines: the table's lines, including the header and separator rows.
        :type lines: list[str]
        :returns: the table HTML, or an empty string if there are too few lines.
        :rtype: str
        """
        if len(lines) < 2:
            return ''

        html = '<table>\n<thead>\n<tr>\n'
        headers = [cell.strip() for cell in lines[0].strip('|').split('|')]
        for h in headers:
            html += f'<th>{self.inline(h)}</th>\n'
        html += '</tr>\n</thead>\n<tbody>\n'

        for row_line in lines[2:]:  # skip separator line
            if not row_line.strip():
                continue
            cells = [cell.strip() for cell in row_line.strip('|').split('|')]
            html += '<tr>\n'
            for c in cells:
                html += f'<td>{self.inline(c)}</td>\n'
            html += '</tr>\n'

        html += '</tbody>\n</table>\n'
        return html


    def _blockquote_segments(self, content_lines: list[str]) -> list[tuple[str, str]]:
        """Split blockquote content into typed segments.

        :param content_lines: the blockquote's inner lines (leading ``>`` removed).
        :type content_lines: list[str]
        :returns: a list of ``(kind, html)`` segments, where ``kind`` is ``'inline'``
            or ``'block'``.
        :rtype: list[tuple[str, str]]
        """
        segments = []
        text_buf: list[str] = []
        i = 0

        def flush_text():
            if text_buf:
                segments.append(('inline', f'<p>{self.inline(" ".join(text_buf))}</p>'))
                text_buf.clear()

        while i < len(content_lines):
            line = content_lines[i]
            # Fenced code block
            if line.startswith('```'):
                flush_text()
                code_lines = []
                i += 1
                while i < len(content_lines) and not content_lines[i].startswith('```'):
                    code_lines.append(content_lines[i])
                    i += 1
                i += 1  # skip closing ```
                code_content = escape_html('\n'.join(code_lines))
                segments.append(('block', f'<pre><code>{code_content}</code></pre>'))
                continue
            # Unordered list item
            if re.match(r'^[-*]\s', line):
                flush_text()
                items = []
                while i < len(content_lines) and re.match(r'^[-*]\s', content_lines[i]):
                    item_text = re.sub(r'^[-*]\s+', '', content_lines[i])
                    items.append(f'<li>{self.inline(item_text)}</li>')
                    i += 1
                segments.append(('inline', '<ul>\n' + '\n'.join(items) + '\n</ul>'))
                continue
            # Ordered list item
            if re.match(r'^\d+\.\s', line):
                flush_text()
                items = []
                while i < len(content_lines) and re.match(r'^\d+\.\s', content_lines[i]):
                    item_text = re.sub(r'^\d+\.\s+', '', content_lines[i])
                    items.append(f'<li>{self.inline(item_text)}</li>')
                    i += 1
                segments.append(('inline', '<ol>\n' + '\n'.join(items) + '\n</ol>'))
                continue
            # Table
            if ('|' in line and i + 1 < len(content_lines)
                    and re.match(r'^[\s|:-]+$', content_lines[i + 1])):
                flush_text()
                table_lines = []
                while i < len(content_lines) and '|' in content_lines[i]:
                    table_lines.append(content_lines[i])
                    i += 1
                segments.append(('block', self._table(table_lines)))
                continue
            # Empty line = paragraph break
            if not line:
                flush_text()
                i += 1
                continue
            text_buf.append(line)
            i += 1
        flush_text()
        return segments


    def _blockquote_split(self, content_lines: list[str], wrapper_open: str,
                          wrapper_close: str) -> str:
        """Render blockquote content, lifting tables and code outside the wrapper.

        Confluence does not render block elements nested inside a ``<blockquote>``
        well, so block segments are emitted as siblings of the quoted text.

        :param content_lines: the blockquote's inner lines.
        :type content_lines: list[str]
        :param wrapper_open: opening markup for the first inline run.
        :type wrapper_open: str
        :param wrapper_close: closing markup for each inline run.
        :type wrapper_close: str
        :returns: the rendered HTML.
        :rtype: str
        """
        segments = self._blockquote_segments(content_lines)
        parts: list[str] = []
        inline_buf: list[str] = []
        first = True

        def flush_inline():
            nonlocal first
            if inline_buf:
                opener = wrapper_open if first else '<blockquote>\n'
                first = False
                parts.append(opener + '\n'.join(inline_buf) + '\n' + wrapper_close)
                inline_buf.clear()

        for kind, html in segments:
            if kind == 'block':
                flush_inline()
                parts.append(html)
            else:
                inline_buf.append(html)

        flush_inline()
        return '\n'.join(parts)


    def _list_block(self, lines: list[str], start: int) -> str:
        """Convert an unordered list block (with nesting) to HTML.

        :param lines: all document lines.
        :type lines: list[str]
        :param start: index of the first list item.
        :type start: int
        :returns: the ``<ul>`` HTML.
        :rtype: str
        """
        items = []
        i = start
        while i < len(lines):
            line = lines[i]
            match = re.match(r'^(\s*)[-*]\s+(.*)', line)
            if not match:
                break
            indent = len(match.group(1))
            text = self.inline(match.group(2))

            i += 1
            while i < len(lines):
                next_match = re.match(r'^(\s*)[-*]\s', lines[i])
                if next_match and len(next_match.group(1)) > indent:
                    nested_html = self._list_block(lines, i)
                    while i < len(lines) and (
                            re.match(r'^(\s*)[-*]\s', lines[i])
                            and len(re.match(r'^(\s*)', lines[i]).group(1)) > indent):
                        i += 1
                    text += '\n' + nested_html
                    break
                else:
                    break

            items.append(f'<li>{text}</li>')

        return '<ul>\n' + '\n'.join(items) + '\n</ul>'


    def _ordered_list_block(self, lines: list[str], start: int) -> str:
        """Convert an ordered list block to HTML.

        :param lines: all document lines.
        :type lines: list[str]
        :param start: index of the first list item.
        :type start: int
        :returns: the ``<ol>`` HTML.
        :rtype: str
        """
        items = []
        i = start
        while i < len(lines):
            line = lines[i]
            match = re.match(r'^(\s*)\d+\.\s+(.*)', line)
            if not match:
                break
            text = self.inline(match.group(2))
            items.append(f'<li>{text}</li>')
            i += 1

        return '<ol>\n' + '\n'.join(items) + '\n</ol>'
