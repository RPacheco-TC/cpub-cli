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
from .storage import prefixed_title

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
    writes_body: bool = True        # False for an existing In Situ container (never rewritten)
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
    parent-not-current, max-updates.
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

    def rows(self):
        """The v1.1 report rows ``(title, action, diagram_count)``."""
        return [(it.title, it.action, it.diagrams) for it in self.items]

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

    def _item_refusals(self, it, pname):
        out = []
        where = it.webui or f"page {it.page_id}"
        if it.page_id == self.parent_id:
            out.append(Refusal(
                "is-parent", it.title, it.page_id,
                f'"{it.title}" is the --parent page itself — not touching it.',
                "Use a distinct --prefix (or rename the doc) "
                "so no doc is titled like the parent."))
        elif not it.in_subtree:
            out.append(Refusal(
                "outside-subtree", it.title, it.page_id,
                f'"{it.title}" already exists outside {pname} at {where} — not touching it.',
                "Use a distinct --prefix."))
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
        """Plan a source (any :class:`~.docset.PlanSource`).

        Every body is converted and validated first, so a bad document fails
        before anything is read from Confluence. Items come in write order:
        top-level docs, the In Situ container, the In Situ docs.

        :param source: what to publish, e.g. a :class:`~.docset.DocSet`.
        :param space_id: numeric space id.
        :param parent_id: id of the ``--parent`` page.
        :rtype: Plan
        """
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
            parent_ref = CONTAINER_REF if e.placement == IN_SITU else parent_id
            items.append(PlanItem(title=title, source=title, action="CREATE",
                                  parent_ref=parent_ref, storage=doc.html,
                                  attachments=doc.attachments))

        parent_info = self.client.get_page_info(parent_id)
        index = self.client.space_title_index(space_id)
        for it in items:
            pid = index.get(it.title)
            if pid is not None:
                self._inspect(it, str(pid), parent_id, is_container=it is container)
        if container is not None and container.page_id:
            for it in items:
                if it.parent_ref == CONTAINER_REF:
                    it.parent_ref = container.page_id
        return Plan(space_id=str(space_id), parent_id=parent_id, prefix=prefix,
                    items=items, parent_info=parent_info)

    def _inspect(self, it, page_id, parent_id, is_container):
        """Turn ``it`` into an UPDATE of ``page_id`` and read what E0 needs."""
        it.action, it.page_id = "UPDATE", page_id
        if is_container:
            it.writes_body = False          # an existing container is reused as-is
        info = self.client.get_page_info(page_id)
        it.version, it.last_edited = info["version"], info["last_edited"]
        it.webui, it.status, it.space_id = info["webui"], info["status"], info["spaceId"]
        it.in_subtree = self.client.is_descendant(page_id, parent_id)
        if it.in_subtree and it.status == "current":
            marker, prop_id, prop_ver = self.client.get_marker(page_id)
            it.classify(marker, (prop_id, prop_ver) if prop_id else None)
        else:
            it.managed = "none"             # refused anyway; foreign markers are not read
