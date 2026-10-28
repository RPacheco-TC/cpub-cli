"""E0 overwrite protection, part 1: plan first (reads only), decide (pure).

The tool may only overwrite a page it can prove it is allowed to: a page
strictly inside the ``--parent`` subtree that it published itself from the same
source, or that the user explicitly hands over in this run. :class:`Planner`
reads (GETs only) and :meth:`Plan.refusals` decides; only
:class:`~.executor.PlanExecutor` writes.
"""
import dataclasses

from .api import MARKER_TOOL
from .docset import CONTAINER, IN_SITU
from .errors import DiagramsUnrenderable
from .storage import prefixed_title

#: Body of the "In Situ" container page (v1.1 text, byte-identical).
INSITU_CONTAINER_BODY = "<p>Stack-specific deep-dive docs for this repository.</p>"
#: ``PlanItem.parent_ref`` of an In Situ doc whose container is created in this run.
CONTAINER_REF = "@in-situ-container"
DEFAULT_MAX_UPDATES = 25
#: Plan modes: a doc set, a note under a page (``--under``), a note replacing a page (``--to``).
SET, UNDER, TO = "set", "under", "to"


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
    writes_body: bool = True        # False for an existing In Situ container (never rewritten)
    managed: str = "new"            # "new" | "same" | "other" | "none"
    marker: dict | None = None
    marker_ref: tuple | None = None  # (property_id, version) of the current marker
    version: int | None = None
    last_edited: str | None = None
    webui: str | None = None
    status: str | None = None
    space_id: str | None = None
    in_subtree: bool | None = None  # allowed target: strict descendant of --parent, or --to
    version_message: str = "republish"
    # message of a CREATE's second save (after its diagrams are attached)
    resave_message: str = "link diagram attachments"
    old_size: int | None = None     # current body, UTF-8 bytes (note mode UPDATE only)
    unrendered: int = 0             # Mermaid diagrams left as code (npx not found)

    @property
    def new_size(self):
        """The new body, UTF-8 bytes."""
        return len(self.storage.encode())

    @property
    def diagrams(self):
        return len(self.attachments)

    @property
    def needs_handover(self):
        """An UPDATE that overwrites a page this tool did not publish from this source.

        Only pages E0 would otherwise allow (strictly inside the fence, current)
        are offered for hand-over; refused pages are never listed as adoptable.
        """
        return (self.action == "UPDATE" and self.writes_body
                and self.managed in ("none", "other")
                and self.in_subtree is True and self.status in (None, "current"))

    def classify(self, marker, marker_ref):
        """Set :attr:`managed` from the page's ownership marker."""
        self.marker, self.marker_ref = marker, marker_ref
        if not marker:
            self.managed = "none"
        elif marker.get("tool") == MARKER_TOOL and marker.get("source") == self.source:
            self.managed = "same"
        else:
            self.managed = "other"


@dataclasses.dataclass
class Refusal:
    """Why a page (or the whole run) is refused, and the one change that would allow it.

    ``code`` is one of: outside-subtree, is-parent, not-current, parent-space,
    parent-not-current, max-updates, homepage, target-space.
    """
    code: str
    title: str | None
    page_id: str | None
    message: str
    fix: str | None = None


@dataclasses.dataclass
class Plan:
    """Every page of one run, in write order, with what the E0 checks need."""
    space_id: str
    parent_id: str
    prefix: str
    items: list
    parent_info: dict | None = None
    mode: str = SET                 # SET, UNDER or TO
    space_info: dict | None = None  # note mode: id, key, name, homepageId
    requested_space_id: str | None = None   # --space, when given (TO mode check)

    def rows(self):
        """The v1.1 report rows ``(title, action, diagram_count)``."""
        return [(it.title, it.action, it.diagrams) for it in self.items]

    def unrendered_items(self):
        """Items whose Mermaid diagrams stay code blocks because npx was not found
        (set mode only: a note is refused instead). For a one-line warning.

        :rtype: list[PlanItem]
        """
        return [it for it in self.items if it.unrendered]

    def handover_items(self):
        """The planned UPDATEs that overwrite a page this tool did not publish
        from this source.

        :rtype: list[PlanItem]
        """
        return [it for it in self.items if it.needs_handover]

    def refusals(self, max_updates=DEFAULT_MAX_UPDATES):
        """Return **every** E0 refusal (empty list = the plan may run). Pure.

        :param max_updates: blast-radius cap on body UPDATEs (creates don't count).
        :rtype: list[Refusal]
        """
        out = []
        pinfo = self.parent_info or {}
        pid = self.parent_id
        pname = f'"{pinfo["title"]}" ({pid})' if pinfo.get("title") else f"page {pid}"
        if pinfo.get("spaceId") is not None and str(pinfo["spaceId"]) != str(self.space_id):
            out.append(Refusal(
                "parent-space", pinfo.get("title"), pid,
                f"--parent {pname} lives in another space (id {pinfo['spaceId']}), "
                f"not the --space given (id {self.space_id}).",
                "Pass the --space that holds the parent page, or a parent in that space."))
        if pinfo.get("status") not in (None, "current"):
            out.append(Refusal(
                "parent-not-current", pinfo.get("title"), pid,
                f"--parent {pname} is {pinfo['status']}, not a live page.",
                "Restore it in Confluence or choose another --parent."))
        if self.mode == TO:
            out.extend(self._target_refusals())
        for it in self.items:
            if it.action != "UPDATE":
                continue
            out.extend(self._item_refusals(it, pname))
        n = sum(1 for it in self.items if it.action == "UPDATE" and it.writes_body)
        if n > max_updates:
            out.append(Refusal(
                "max-updates", None, None,
                f"This run would update {n} existing pages, "
                f"more than --max-updates {max_updates}.",
                f"Re-run with --max-updates {n} if that is intended."))
        return out

    def _target_refusals(self):
        """``--to``: the space homepage and a page outside ``--space`` are refused."""
        out = []
        it, space = self.items[0], self.space_info or {}
        where = it.webui or f"page {it.page_id}"
        if space.get("homepageId") and it.page_id == space["homepageId"]:
            out.append(Refusal(
                "homepage", it.title, it.page_id,
                f'"{it.title}" at {where} is the homepage of space {space.get("key")} '
                "— not touching it.",
                "Pick a page below the homepage, or use --under to add a child page."))
        if self.requested_space_id and str(it.space_id) != str(self.requested_space_id):
            out.append(Refusal(
                "target-space", it.title, it.page_id,
                f'"{it.title}" at {where} lives in space {space.get("key")} '
                f"(id {it.space_id}), not the --space given (id {self.requested_space_id}).",
                "Drop --space (the page's own space is used) or pass the page's space."))
        return out

    def _item_refusals(self, it, pname):
        out = []
        where = it.webui or f"page {it.page_id}"
        if self.mode == TO:
            if it.status not in (None, "current"):
                out.append(Refusal(
                    "not-current", it.title, it.page_id,
                    f'"{it.title}" at {where} is {it.status} — not touching it.',
                    "Restore it in Confluence, or pick another --to page."))
            return out
        if it.page_id == self.parent_id:
            out.append(Refusal(
                "is-parent", it.title, it.page_id,
                f'"{it.title}" is the --parent page itself — not touching it.',
                "Use a distinct --prefix (or rename the doc) "
                "so no doc is titled like the parent."))
        elif not it.in_subtree:
            fix = ("Use --to with that page to replace it, or --title to publish "
                   "under another title." if self.mode == UNDER else "Use a distinct --prefix.")
            out.append(Refusal(
                "outside-subtree", it.title, it.page_id,
                f'"{it.title}" already exists outside {pname} at {where} — not touching it.',
                fix))
        if it.status not in (None, "current"):    # not deduped: one page can have two codes
            out.append(Refusal(
                "not-current", it.title, it.page_id,
                f'"{it.title}" at {where} is {it.status} — not touching it.',
                "Restore or rename it in Confluence, or use a distinct --prefix."))
        return out


class Planner:
    """Build a :class:`Plan` with **zero write calls**.

    :param client: the :class:`~.api.ConfluenceClient` (only its reads are used).
    :param converter: the :class:`~.storage.StorageConverter`; its ``prefix``
        namespaces every title.
    """

    def __init__(self, client, converter):
        self.client = client
        self.converter = converter

    def build(self, source, space_id, parent_id) -> Plan:
        """Plan a source under a parent page (set mode, or a note with ``--under``).

        Every body is converted and validated first, so a bad document fails
        before anything is read from Confluence. Items come in write order:
        top-level docs, the In Situ container, the In Situ docs. A title match
        is an UPDATE only inside the ``--parent`` subtree (the fence).

        :param source: any :class:`~.docset.PlanSource` (``DocSet`` or ``SingleNote``).
        :param space_id: numeric space id, or ``None`` to use the parent's space.
        :param parent_id: id of the ``--parent`` / ``--under`` page.
        :rtype: Plan
        """
        note = getattr(source, "kind", "set") == "note"
        prefix = self.converter.prefix
        parent_id = str(parent_id)
        entries = list(source.entries())
        texts = {id(e): e.text() for e in entries if e.placement != CONTAINER}
        items, container = [], None
        for e in entries:
            title = prefixed_title(prefix, e.title)
            if e.placement == CONTAINER:
                container = PlanItem(title=title, source=title, action="CREATE",
                                     kind="container", parent_ref=parent_id,
                                     storage=INSITU_CONTAINER_BODY)
                items.append(container)
                continue
            doc = self.converter.convert(texts[id(e)])      # convert + validate
            if note:
                self._check_diagrams(source, doc)
            parent_ref = CONTAINER_REF if e.placement == IN_SITU else parent_id
            items.append(PlanItem(title=title, source=e.source_id or title, action="CREATE",
                                  parent_ref=parent_ref, storage=doc.html,
                                  attachments=doc.attachments,
                                  version_message=self._message(source),
                                  resave_message=self._resave_message(source),
                                  unrendered=doc.unrendered))

        parent_info = self.client.get_page_info(parent_id)
        requested = space_id
        if space_id is None:                # AC-1.2.5: the parent's own space
            space_id = parent_info["spaceId"]
        # note mode shows the space in the preview; a derived space needs its key for the
        # footer. With --space in set mode nothing extra is read (v1.1 request sequence).
        space_info = self.client.get_space(space_id) if note or requested is None else None
        index = self.client.space_title_index(space_id)
        for it in items:
            pid = index.get(it.title)
            if pid is not None:
                self._inspect(it, str(pid), parent_id, is_container=it is container,
                              with_body=note)
        if container is not None and container.page_id:
            for it in items:
                if it.parent_ref == CONTAINER_REF:
                    it.parent_ref = container.page_id
        return Plan(space_id=str(space_id), parent_id=parent_id, prefix=prefix,
                    items=items, parent_info=parent_info, mode=UNDER if note else SET,
                    space_info=space_info,
                    requested_space_id=None if requested is None else str(requested))

    def build_replace(self, note, target_id, space_id=None) -> Plan:
        """Plan a note that replaces exactly one page (``--to``; S2.1).

        No title search: the target is the page given. Its title is kept
        unless the note has an explicit ``--title`` (D3); it is never moved.
        The space is the page's own; a different ``--space`` is refused, as is
        the space homepage.

        :param note: a :class:`~.docset.SingleNote`.
        :param target_id: the page to replace.
        :param space_id: numeric id of ``--space``, or ``None``.
        :rtype: Plan
        """
        (entry,) = list(note.entries())
        doc = self.converter.convert(entry.text())          # convert + validate first
        self._check_diagrams(note, doc)
        target_id = str(target_id)
        info = self.client.get_page_info(target_id, with_body=True)
        space_info = self.client.get_space(info["spaceId"])
        title = info["title"]
        if note.title:                                      # explicit --title renames
            title = prefixed_title(self.converter.prefix, note.title)
        it = PlanItem(title=title, source=entry.source_id, action="UPDATE",
                      page_id=target_id, parent_ref=info["parentId"], storage=doc.html,
                      attachments=doc.attachments, version_message=self._message(note))
        self._apply_info(it, info)
        it.in_subtree = True                # --to: the page given is the fence
        self._read_marker(it)
        return Plan(space_id=str(info["spaceId"]), parent_id=str(info["parentId"]),
                    prefix=self.converter.prefix, items=[it], parent_info=None, mode=TO,
                    space_info=space_info,
                    requested_space_id=None if space_id is None else str(space_id))

    @staticmethod
    def _check_diagrams(note, doc):
        """A note never publishes its diagrams as code: refused before any read."""
        if doc.unrendered:
            raise DiagramsUnrenderable(note.filename, doc.unrendered)

    @staticmethod
    def _message(source):
        """Version message of an UPDATE: v1.1's, or ``Published from Obsidian: <file>``."""
        if getattr(source, "kind", "set") == "note":
            return f"Published from Obsidian: {source.filename}"
        return "republish"

    @staticmethod
    def _resave_message(source):
        """A note's CREATE resave is still "Published from Obsidian" (its history reads
        consistently); set mode keeps v1.1's message."""
        if getattr(source, "kind", "set") == "note":
            return f"Published from Obsidian: {source.filename}"
        return "link diagram attachments"

    @staticmethod
    def _apply_info(it, info):
        it.version, it.last_edited = info["version"], info["last_edited"]
        it.webui, it.status, it.space_id = info["webui"], info["status"], info["spaceId"]
        it.old_size = info.get("body_size")

    def _read_marker(self, it):
        if it.in_subtree and it.status == "current":
            marker, prop_id, prop_ver = self.client.get_marker(it.page_id)
            it.classify(marker, (prop_id, prop_ver) if prop_id else None)
        else:
            it.managed = "none"             # refused anyway; foreign markers are not read

    def _inspect(self, it, page_id, parent_id, is_container, with_body=False):
        """Turn ``it`` into an UPDATE of ``page_id`` and read what E0 needs."""
        it.action, it.page_id = "UPDATE", page_id
        if is_container:
            it.writes_body = False          # an existing container is reused as-is
        if with_body:
            info = self.client.get_page_info(page_id, with_body=True)
        else:
            info = self.client.get_page_info(page_id)
        self._apply_info(it, info)
        it.in_subtree = self.client.is_descendant(page_id, parent_id)
        self._read_marker(it)
