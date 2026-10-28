"""What is being published: sources the :class:`~.plan.Planner` turns into a plan.

A leaf module (it imports only :mod:`.errors`). A source yields
:class:`SourceDoc` entries in publish order (the :class:`PlanSource` protocol);
:class:`DocSet` is the folder source. Each entry reads its Markdown only when
the planner asks for it, so listing a folder (and the duplicate-title check)
never touches file contents.
"""
import dataclasses
import pathlib
import re
import typing

from .errors import NotAFolder, NotAMarkdownFile

#: Title of the container page for the ``In Situ/`` deep-dive folder (unprefixed).
INSITU_TITLE = "In Situ"

#: Where an entry goes in the page tree.
TOP = "top"              # directly under --parent
CONTAINER = "container"  # the In Situ container page, under --parent
IN_SITU = "in-situ"      # under the In Situ container

#: Frontmatter keys naming a note's target (S2.4): the page to replace (``--to``)
#: and the parent to publish under (``--under``). Read only, never written (D5).
TO_KEY = "confluence"
UNDER_KEY = "confluence_parent"


@dataclasses.dataclass(frozen=True)
class SourceDoc:
    """One entry of a source.

    :param title: the bare page title (the filename stem, unprefixed).
    :param markdown: the Markdown text, or ``None`` to read it from ``path``.
    :param path: the file it comes from, or ``None`` for an in-memory doc.
    :param placement: :data:`TOP`, :data:`CONTAINER` or :data:`IN_SITU`.
        A container has no Markdown: its body is fixed by the planner.
    :param source_id: the ownership-marker source, or ``None`` for the
        prefixed title (set mode); note mode uses the note's filename.
    """
    title: str
    markdown: str | None = None
    path: pathlib.Path | None = None
    placement: str = TOP
    source_id: str | None = None

    def text(self) -> str:
        """The Markdown, read from :attr:`path` on demand."""
        if self.markdown is not None:
            return self.markdown
        return pathlib.Path(self.path).read_text()


class PlanSource(typing.Protocol):
    """What the planner needs from anything it publishes.

    :class:`DocSet` and :class:`SingleNote` implement it, so the planner (and
    every E0 guard) does not change when a new source appears.
    """

    #: ``"set"`` or ``"note"``; note mode adds the replace preview and the
    #: ``Published from Obsidian`` version message.
    kind: str

    def entries(self) -> typing.Iterable[SourceDoc]:
        """The entries in publish order; a :data:`CONTAINER` precedes its docs."""
        ...


@dataclasses.dataclass(frozen=True)
class DocSet:
    """A doc set: numbered top-level docs plus an optional ``In Situ/`` folder.

    :param top: the top-level docs, in publish order.
    :param insitu: the ``In Situ/`` docs in publish order, or ``None`` when the
        set has no such folder (an empty tuple means an empty folder).
    :param root: the folder it was read from, if any.
    """
    top: tuple = ()
    insitu: tuple | None = None
    root: pathlib.Path | None = None
    kind: typing.ClassVar[str] = "set"

    @classmethod
    def from_folder(cls, path) -> "DocSet":
        """List a folder: ``*.md`` sorted by name, then ``In Situ/*.md``.

        :raises NotAFolder: if ``path`` is not a directory.
        """
        root = pathlib.Path(path)
        if not root.is_dir():
            raise NotAFolder(path)
        top = tuple(SourceDoc(f.stem, path=f) for f in sorted(root.glob("*.md")))
        d = cls.find_insitu_dir(root)
        insitu = None
        if d is not None:
            insitu = tuple(SourceDoc(f.stem, path=f, placement=IN_SITU)
                           for f in sorted(d.glob("*.md")))
        return cls(top, insitu, root)

    @staticmethod
    def find_insitu_dir(root):
        """Return the ``In Situ`` subfolder, matched case-insensitively, or ``None``.

        The deep-dive folder is canonically named ``In Situ``, but matching it
        exactly would silently skip the whole folder if the generator produced a
        slightly different casing/spacing (e.g. ``in situ``). Matching loosely
        makes sure those pages still get published.

        :param root: the doc-set folder.
        :rtype: pathlib.Path | None
        """
        root = pathlib.Path(root)
        if not root.is_dir():
            return None
        return next((d for d in sorted(root.iterdir())
                     if d.is_dir() and d.name.strip().casefold() == "in situ"), None)

    def entries(self):
        """Top-level docs, then (if the folder exists) the container and its docs."""
        yield from self.top
        if self.insitu is not None:
            yield SourceDoc(INSITU_TITLE, placement=CONTAINER)
            for doc in self.insitu:
                yield dataclasses.replace(doc, placement=IN_SITU)

    def titles(self) -> list:
        """Every page title the set produces (unprefixed), container included."""
        return [e.title for e in self.entries()]

    def duplicate_titles(self) -> list:
        """Titles produced more than once, sorted (titles are unique per space)."""
        titles = self.titles()
        return sorted({t for t in titles if titles.count(t) > 1})


def read_frontmatter(md: str) -> dict:
    """Read ``key: value`` lines of a leading ``---`` frontmatter block. Pure.

    Optional single or double quotes around a value are removed; lists,
    nesting and anything else are ignored. No YAML library, nothing executed.

    :rtype: dict[str, str]
    """
    lines = md.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}
    out = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return out
        m = _FRONTMATTER_RE.match(line)
        if m:
            value = m.group(2).strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            out[m.group(1)] = value
    return {}                       # no closing ---: not frontmatter


_FRONTMATTER_RE = re.compile(r"^([A-Za-z_][\w-]*):[ \t]*(.*)$")


@dataclasses.dataclass(frozen=True)
class SingleNote:
    """One Obsidian note published as one page (S2.1/S2.2).

    :param path: the ``.md`` file.
    :param title: an explicit ``--title``, or ``None``.
    :param markdown: the note text (read from ``path`` when ``None``).
    """
    path: pathlib.Path
    title: str | None = None
    markdown: str | None = None
    kind: typing.ClassVar[str] = "note"

    @classmethod
    def from_file(cls, path, title=None) -> "SingleNote":
        """Check the path is a ``.md`` file and read it.

        :raises NotAMarkdownFile: for anything else.
        """
        p = pathlib.Path(path)
        if not p.is_file() or p.suffix.lower() != ".md":
            raise NotAMarkdownFile(path)
        return cls(p, title, p.read_text())

    @property
    def filename(self) -> str:
        """The note's filename: its marker source and version-message name."""
        return pathlib.Path(self.path).name

    def text(self) -> str:
        return self.markdown if self.markdown is not None else pathlib.Path(self.path).read_text()

    def resolved_title(self) -> str:
        """``--title``, else frontmatter ``title:``, else the filename stem (AC-2.2.1)."""
        if self.title:
            return self.title
        return read_frontmatter(self.text()).get("title") or pathlib.Path(self.path).stem

    def targets(self) -> dict:
        """The targets the note names in its frontmatter (S2.4, AC-2.4.1).

        ``{"to": <confluence: value>, "under": <confluence_parent: value>}``,
        each the raw string (quotes stripped), or ``None`` when the key is
        missing, empty or a list. Not parsed: the caller runs the value through
        ``PageRef.parse`` like a flag. Both keys may be set; refusing that is
        the caller's decision. Nothing is ever written to the note (D5).

        :rtype: dict[str, str | None]
        """
        fm = read_frontmatter(self.text())
        return {"to": fm.get(TO_KEY) or None, "under": fm.get(UNDER_KEY) or None}

    def entries(self):
        yield SourceDoc(self.resolved_title(), self.text(), pathlib.Path(self.path),
                        TOP, source_id=self.filename)
