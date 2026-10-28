"""``cpub undo`` planning (S0.5): what a restore of the last run would do. Read-only.

:class:`UndoPlanner` picks the run from the :class:`~.runlog.RunLog` and checks
every page it UPDATEd: a page still at the version the run wrote is restored
to the version before it; a page edited since (or gone) is refused and listed.
Pages the run CREATED are listed, never deleted. The writes themselves are
:meth:`~.executor.PlanExecutor.undo`'s.
"""
import dataclasses
import urllib.error

from .errors import UndoError
from .runlog import PUBLISH

#: Why a page is not restored.
EDITED, GONE = "edited", "gone"


@dataclasses.dataclass
class UndoItem:
    """One page of an undo plan.

    :param version_before: the version the run replaced (what undo brings back).
    :param version_after: the version the run wrote.
    :param current: the page's version now (``None`` if it could not be read).
    :param reason: ``None`` (restore), :data:`EDITED` or :data:`GONE`.
    """
    page_id: str
    title: str
    version_before: int | None
    version_after: int | None
    current: int | None = None
    webui: str | None = None
    reason: str | None = None

    @property
    def history(self):
        """The page-history link (the previous version can be restored by hand there)."""
        if not self.webui or "/pages/" not in self.webui:
            return None
        base = self.webui.split("/spaces/", 1)[0]
        return f"{base}/pages/viewpreviousversions.action?pageId={self.page_id}"


@dataclasses.dataclass
class UndoPlan:
    """What undoing ``run`` would do.

    :param restores: pages to restore (still at the version the run wrote).
    :param refused: pages left as they are (``reason`` says why), with history links.
    :param created: the run's CREATE lines: listed, never deleted (never-delete rule).
    """
    run: object
    restores: list = dataclasses.field(default_factory=list)
    refused: list = dataclasses.field(default_factory=list)
    created: list = dataclasses.field(default_factory=list)


class UndoPlanner:
    """Build an :class:`UndoPlan` with read calls only.

    :param client: the :class:`~.api.ConfluenceClient`.
    :param runlog: the :class:`~.runlog.RunLog`.
    """

    def __init__(self, client, runlog):
        self.client, self.runlog = client, runlog

    @staticmethod
    def select(runlog, host, run_id=None):
        """The run to undo: ``run_id``, else the last publish run not undone (D-2).

        :param host: the current ``CONFLUENCE_BASE_URL`` host (D-6).
        :raises UndoError: ``nothing``, ``unknown``, ``undone`` or ``other-site``
            (checked before any request).
        """
        run = runlog.target(run_id)
        if run is None:
            raise UndoError("unknown" if run_id else "nothing", run_id)
        if run.kind != PUBLISH:
            raise UndoError("unknown", run.run_id)       # an undo run is not undoable (D-5)
        if run.undone:
            raise UndoError("undone", run.run_id)
        if (run.host or "").lower() != (host or "").lower():
            raise UndoError("other-site", run.run_id, logged_host=run.host, host=host)
        return run

    def build(self, run) -> UndoPlan:
        """Check each page the run UPDATEd against its current version (AC-0.5.3)."""
        plan = UndoPlan(run, created=run.created())
        for line in run.updated():
            it = UndoItem(str(line.get("page_id")), line.get("title"),
                          line.get("version_before"), line.get("version_after"))
            try:
                info = self.client.get_page_info(it.page_id)
            except urllib.error.HTTPError as e:
                if e.code != 404:
                    raise
                it.reason = GONE
                plan.refused.append(it)
                continue
            it.current, it.webui = info["version"], info["webui"]
            if info["status"] != "current":
                it.reason = GONE
            elif it.version_before is None or it.current != it.version_after:
                it.reason = EDITED
            (plan.refused if it.reason else plan.restores).append(it)
        return plan
