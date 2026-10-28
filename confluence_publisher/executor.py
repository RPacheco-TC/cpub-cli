"""E0 overwrite protection, part 2: write a checked plan (the **only** writer).

:class:`PlanExecutor` is the one class that calls the write methods of
:class:`~.api.ConfluenceClient` (create, update, upload, set marker).
"""
import dataclasses

from .errors import (PlanRefused, HandoverRequired, HandoverDeclined, PlanExecutionError,
                     UndoError)
from .plan import CONTAINER_REF, DEFAULT_MAX_UPDATES


@dataclasses.dataclass
class RunReport:
    """What a run wrote; filled as it goes, so a failure still reports progress."""
    written: list = dataclasses.field(default_factory=list)   # [(PlanItem, page_id)]
    pending: list = dataclasses.field(default_factory=list)   # [PlanItem] not (fully) written
    partial: tuple | None = None        # (PlanItem, page_id): failed after it was created
    marker_failures: list = dataclasses.field(default_factory=list)  # [(item, pid, exc)]
    error: BaseException | None = None


def _version(page, default):
    """The version number of a page object Confluence returned, else ``default``."""
    return ((page or {}).get("version") or {}).get("number") or default


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
        :param log: ``event_dict -> None`` called after each page is written, with
            ``action``, ``page_id``, ``title``, ``source``, ``version_before`` and
            ``version_after`` (a :class:`~.runlog.Recorder`, for the audit log).
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
        container_id, deferred = None, []
        try:
            for it in plan.items:
                parent = container_id if it.parent_ref == CONTAINER_REF else it.parent_ref
                try:
                    pid, after = self._write(plan, it, parent, report)
                except Exception as e:
                    report.error = e
                    raise PlanExecutionError(report) from e
                if it.kind == "container":
                    container_id = pid
                report.pending.remove(it)
                report.written.append((it, pid))
                event = {"action": it.action, "page_id": pid, "title": it.title,
                         "source": it.source, "version_before": it.version,
                         "version_after": after}
                if it.relink:
                    deferred.append((it, event))
                elif log is not None:
                    log(event)
            for it, event in deferred:
                self._relink(it, event, report)
        finally:        # a written page is always logged, once, with its last version
            if log is not None:
                for _, event in deferred:
                    log(event)
        return report

    def undo(self, plan, *, confirm, log=None, message="Restored by cpub undo"):
        """Restore every page of an :class:`~.undo.UndoPlan` (S0.5).

        Each page is re-read just before its restore: one edited after the plan
        was shown is skipped, never overwritten. Created pages are not touched.

        :param confirm: ``items -> bool``, the typed confirmation; ``None`` (no
            way to ask) is refused (D-4).
        :param log: the undo run's :class:`~.runlog.Recorder`; its run is closed
            (marking the target undone) only when every restore went through.
        :rtype: RunReport (``written`` = restored, ``pending`` = skipped or not reached)
        :raises UndoError: ``not-interactive`` or ``declined`` - nothing written.
        :raises PlanExecutionError: a restore failed; ``.report`` says what was done.
        """
        if plan.restores:
            if confirm is None:
                raise UndoError("not-interactive", plan.run.run_id)
            if not confirm(plan.restores):
                raise UndoError("declined", plan.run.run_id)
        report = RunReport(pending=list(plan.restores))
        for it in plan.restores:
            try:
                now = self.client.get_page_info(it.page_id)["version"]
                if now != it.version_after:          # edited after the plan: leave it
                    it.current, it.reason = now, "edited"
                    continue
                done = self.client.restore_version(it.page_id, it.version_before, message)
            except Exception as e:
                report.error = e
                raise PlanExecutionError(report) from e
            after = (done or {}).get("number") or now + 1
            report.pending.remove(it)
            report.written.append((it, it.page_id))
            if log is not None:
                log({"action": "RESTORE", "page_id": it.page_id, "title": it.title,
                     "source": None, "version_before": now, "version_after": after})
        if log is not None and not report.pending:
            log.finish()
        return report

    def _write(self, plan, it, parent_id, report):
        """Write one item; return ``(page id, version after)``.

        Attachments always precede the body that references them: Confluence
        links ``ri:attachment`` at save time.
        """
        c = self.client
        if it.action == "UPDATE":
            pid = it.page_id
            if not it.writes_body:
                return pid, it.version          # an existing container: not rewritten
            self._upload_missing(pid, it.attachments)
            saved = c.update_page(pid, it.title, it.storage, it.version_message,
                                  expected_version=it.version)
            if it.managed != "same":
                self._mark(it, pid, report)
            return pid, _version(saved, (it.version or 0) + 1)
        created = c.create_page(plan.space_id, parent_id, it.title, it.storage)
        pid = str(created["id"])
        report.partial = (it, pid)
        self._mark(it, pid, report)     # right after create: a failed upload re-runs prompt-free
        after = _version(created, 1)
        if self._upload_missing(pid, it.attachments):
            saved = c.update_page(pid, it.title, it.storage, it.resave_message,
                                  expected_version=after)
            after = _version(saved, after + 1)
        report.partial = None
        return pid, after

    def _relink(self, it, event, report):
        """D37: save a page's body again once every page of the run exists, so
        Confluence links its anchors to pages created after it. Same body."""
        try:
            saved = self.client.update_page(event["page_id"], it.title, it.storage,
                                            it.relink_message,
                                            expected_version=event["version_after"])
        except Exception as e:
            report.error = e
            raise PlanExecutionError(report) from e
        event["version_after"] = _version(saved, event["version_after"] + 1)

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
