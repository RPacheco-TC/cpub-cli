"""E0 overwrite protection, part 2: write a checked plan (the **only** writer).

:class:`PlanExecutor` is the one class that calls the write methods of
:class:`~.api.ConfluenceClient` (create, update, upload, set marker).
"""
import dataclasses

from .errors import PlanRefused, HandoverRequired, HandoverDeclined, PlanExecutionError
from .plan import CONTAINER_REF, DEFAULT_MAX_UPDATES


@dataclasses.dataclass
class RunReport:
    """What a run wrote; filled as it goes, so a failure still reports progress."""
    written: list = dataclasses.field(default_factory=list)   # [(PlanItem, page_id)]
    pending: list = dataclasses.field(default_factory=list)   # [PlanItem] not (fully) written
    partial: tuple | None = None        # (PlanItem, page_id): failed after it was created
    marker_failures: list = dataclasses.field(default_factory=list)  # [(item, pid, exc)]
    error: BaseException | None = None


class PlanExecutor:
    """Re-check a plan, obtain any hand-over, then write it in plan order.

    :param client: the :class:`~.api.ConfluenceClient`.
    :param today: ISO date recorded as the marker's ``since`` (today when ``None``).
    """

    def __init__(self, client, *, today=None):
        self.client = client
        self.today = today

    def execute(self, plan, *, max_updates=DEFAULT_MAX_UPDATES, adopt=False,
                confirm=None, log=None):
        """Run a plan.

        :param plan: a :class:`~.plan.Plan`.
        :param max_updates: blast-radius cap (see :meth:`~.plan.Plan.refusals`).
        :param adopt: ``--adopt``: hand-over granted without asking.
        :param confirm: ``items -> bool`` asking the user (interactive);
            ``None`` means there is no way to ask.
        :param log: ``event_dict -> None`` called after each page is written.
        :rtype: RunReport
        :raises PlanRefused: any E0 refusal — nothing written.
        :raises HandoverRequired: hand-over needed, not adopted, no ``confirm``.
        :raises HandoverDeclined: ``confirm`` returned false — nothing written.
        :raises PlanExecutionError: a write failed; ``.report`` lists written/pending.
        """
        refusals = plan.refusals(max_updates)
        if refusals:
            raise PlanRefused(refusals)
        needs = plan.handover_items()
        if needs and not adopt:
            if confirm is None:
                raise HandoverRequired(needs)
            if not confirm(needs):
                raise HandoverDeclined(needs)
        report = RunReport(pending=list(plan.items))
        container_id = None
        for it in plan.items:
            parent = container_id if it.parent_ref == CONTAINER_REF else it.parent_ref
            try:
                pid = self._write(plan, it, parent, report)
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

    def _write(self, plan, it, parent_id, report):
        """Write one item; return its page id.

        Attachments always precede the body that references them: Confluence
        links ``ri:attachment`` at save time.
        """
        c = self.client
        if it.action == "UPDATE":
            pid = it.page_id
            if it.writes_body:
                self._upload_missing(pid, it.attachments)
                c.update_page(pid, it.title, it.storage, "republish",
                              expected_version=it.version)
                if it.managed != "same":
                    self._mark(it, pid, report)
            return pid
        created = c.create_page(plan.space_id, parent_id, it.title, it.storage)
        pid = str(created["id"])
        report.partial = (it, pid)
        self._mark(it, pid, report)     # right after create: a failed upload re-runs prompt-free
        if self._upload_missing(pid, it.attachments):
            c.update_page(pid, it.title, it.storage, "link diagram attachments",
                          expected_version=(created.get("version") or {}).get("number", 1))
        report.partial = None
        return pid

    def _mark(self, it, pid, report):
        existing = it.marker_ref if it.action == "UPDATE" else None
        try:
            self.client.set_marker(pid, it.source, today=self.today, existing=existing)
        except Exception as e:          # the page content is fine; report it as unmanaged
            report.marker_failures.append((it, pid, e))

    def _upload_missing(self, page_id, atts):
        """Upload the attachments not yet on the page; return how many were uploaded.

        Names are content hashes, so a name already present means identical
        bytes (and re-uploading a duplicate filename returns HTTP 400 anyway).
        """
        if not atts:
            return 0
        existing = self.client.existing_attachment_names(page_id)
        uploaded = 0
        for fn, fp in atts:
            if fn not in existing:
                self.client.upload_attachment(page_id, fp, fn)
                existing.add(fn)
                uploaded += 1
        return uploaded
