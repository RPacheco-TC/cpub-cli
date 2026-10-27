"""What is being published: sources the :class:`~.plan.Planner` turns into a plan.

A leaf module (it imports only :mod:`.errors`). A source yields
:class:`SourceDoc` entries in publish order (the :class:`PlanSource` protocol);
:class:`DocSet` is the folder source. Each entry reads its Markdown only when
the planner asks for it, so listing a folder (and the duplicate-title check)
never touches file contents.
"""
import dataclasses
import pathlib
import typing

from .errors import NotAFolder

#: Title of the container page for the ``In Situ/`` deep-dive folder (unprefixed).
INSITU_TITLE = "In Situ"

#: Where an entry goes in the page tree.
TOP = "top"              # directly under --parent
CONTAINER = "container"  # the In Situ container page, under --parent
IN_SITU = "in-situ"      # under the In Situ container


@dataclasses.dataclass(frozen=True)
class SourceDoc:
    """One entry of a source.

    :param title: the bare page title (the filename stem, unprefixed).
    :param markdown: the Markdown text, or ``None`` to read it from ``path``.
    :param path: the file it comes from, or ``None`` for an in-memory doc.
    :param placement: :data:`TOP`, :data:`CONTAINER` or :data:`IN_SITU`.
        A container has no Markdown: its body is fixed by the planner.
    """
    title: str
    markdown: str | None = None
    path: pathlib.Path | None = None
    placement: str = TOP

    def text(self) -> str:
        """The Markdown, read from :attr:`path` on demand."""
        if self.markdown is not None:
            return self.markdown
        return pathlib.Path(self.path).read_text()


class PlanSource(typing.Protocol):
    """What the planner needs from anything it publishes.

    :class:`DocSet` implements it; a single-note source will too, so the
    planner (and every E0 guard) does not change when a new source appears.
    """

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
