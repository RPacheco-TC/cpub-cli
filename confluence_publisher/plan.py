"""E0 overwrite protection: plan first, write second.

:func:`build_plan` reads (GETs only), :func:`check_plan` decides (pure) and
:func:`execute_plan` writes. Nothing else may call ``create_page``,
``update_page``, ``upload_attachment`` or ``set_marker``.
"""
import pathlib
import dataclasses

from .errors import PlanRefused, HandoverRequired, HandoverDeclined, PlanExecutionError
from .storage import md_to_storage, prefixed_title
from .api import (MARKER_TOOL, create_page, update_page, upload_attachment,
                  existing_attachment_names, space_title_index, get_page_info,
                  is_descendant, get_marker, set_marker)


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


