"""The audit log of every write (S0.5): one JSON line per page written.

:class:`RunLog` appends to ``AppDirs().log_path`` (``$XDG_STATE_HOME/
confluence-publisher/log.jsonl``; folder ``700``, file ``600``). A line holds
the run id, kind (``publish`` / ``undo``), time, site host, space, page id,
title, action, ``version_before`` -> ``version_after`` and source: **never** a
body, a token or an email. ``cpub undo`` reads it back with :meth:`RunLog.runs`.
A leaf: it imports nothing of the package.
"""
import dataclasses
import datetime
import json
import os
import pathlib
import secrets

PUBLISH, UNDO = "publish", "undo"
#: The action of the line that closes an undo run and marks its target undone.
UNDO_END = "UNDO-END"
#: The only keys a line may carry (AC-0.5.1: no body, no secrets).
FIELDS = ("run_id", "kind", "time", "host", "space_id", "page_id", "title", "action",
          "version_before", "version_after", "source", "undoes")


class LogFiles:
    """The real file operations of :class:`RunLog` (tests inject an in-memory fake)."""

    @staticmethod
    def append_line(path, line):
        """Append one line to a ``600`` file in a ``700`` folder (both created if needed)."""
        path = pathlib.Path(path)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, (line + "\n").encode())
        finally:
            os.close(fd)

    @staticmethod
    def read_text(path):
        """The file's text, or ``None`` when there is no log yet."""
        try:
            return pathlib.Path(path).read_text()
        except FileNotFoundError:
            return None


@dataclasses.dataclass
class Run:
    """One logged run, rebuilt from its lines.

    :param pages: its page lines (``dict``), in write order; an undo run's
        closing ``UNDO-END`` line is not a page.
    :param undone: a publish run that a completed undo run has undone.
    """
    run_id: str
    kind: str
    time: str
    host: str | None
    space_id: str | None
    pages: list = dataclasses.field(default_factory=list)
    undoes: str | None = None
    undone: bool = False

    def updated(self):
        return [p for p in self.pages if p.get("action") == "UPDATE"]

    def created(self):
        return [p for p in self.pages if p.get("action") == "CREATE"]


class Recorder:
    """The ``log`` callback of one run: :class:`~.executor.PlanExecutor` calls it
    after each page it writes. A failed write warns once and never raises: the
    publish itself must not fail because of its log (D-1).
    """

    def __init__(self, runlog, kind, host, space_id=None, undoes=None):
        self.runlog, self.kind, self.host = runlog, kind, host
        self.space_id, self.undoes = space_id, undoes
        self.run_id = runlog.new_id()
        self.failed = False

    def __call__(self, event):
        self._write({"page_id": event.get("page_id"), "title": event.get("title"),
                     "action": event.get("action"),
                     "version_before": event.get("version_before"),
                     "version_after": event.get("version_after"),
                     "source": event.get("source")})

    def finish(self):
        """Close an undo run: its target counts as undone from now on (D-5)."""
        if self.kind == UNDO:
            self._write({"action": UNDO_END})

    def _write(self, fields):
        line = {"run_id": self.run_id, "kind": self.kind, "time": self.runlog.now(),
                "host": self.host, "space_id": self.space_id, **fields}
        if self.undoes:
            line["undoes"] = self.undoes
        try:
            self.runlog.files.append_line(self.runlog.path, json.dumps(
                {k: line[k] for k in FIELDS if k in line}, ensure_ascii=False))
        except OSError as e:
            if not self.failed:
                self.runlog.warn(f"Warning: could not write the audit log "
                                 f"{self.runlog.path}: {e.strerror or e}")
            self.failed = True


class RunLog:
    """Append to and read back the audit log.

    :param path: the log file (``AppDirs().log_path``).
    :param files: the file operations (default :class:`LogFiles`).
    :param clock: ``() -> datetime`` (UTC now by default).
    :param new_id: ``() -> str`` run id (time plus 6 random hex digits by default).
    :param warn: called with a one-line message when a log write fails.
    """

    def __init__(self, path, files=None, clock=None, new_id=None, warn=None):
        self.path = pathlib.Path(path)
        self.files = files or LogFiles()
        self.clock = clock or (lambda: datetime.datetime.now(datetime.timezone.utc))
        self._new_id = new_id
        self.warn = warn or (lambda msg: None)

    def now(self) -> str:
        return self.clock().strftime("%Y-%m-%dT%H:%M:%SZ")

    def new_id(self) -> str:
        if self._new_id is not None:
            return self._new_id()
        return self.clock().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)

    def recorder(self, kind, host, space_id=None, undoes=None) -> Recorder:
        """Start a run; the returned :class:`Recorder` is the executor's ``log``."""
        return Recorder(self, kind, host, space_id, undoes)

    def entries(self) -> list:
        """Every well-formed line, oldest first (a damaged line is skipped)."""
        out = []
        for raw in (self.files.read_text(self.path) or "").splitlines():
            try:
                line = json.loads(raw)
            except ValueError:
                continue
            if isinstance(line, dict) and line.get("run_id"):
                out.append(line)
        return out

    def runs(self) -> list:
        """Every run, **newest first**, with ``undone`` set on undone publish runs.

        :rtype: list[Run]
        """
        runs, done = {}, set()
        for line in self.entries():
            run = runs.get(line["run_id"])
            if run is None:
                run = runs[line["run_id"]] = Run(
                    line["run_id"], line.get("kind"), line.get("time"), line.get("host"),
                    line.get("space_id"), undoes=line.get("undoes"))
            if line.get("action") == UNDO_END:
                done.add(line.get("undoes"))
            else:
                run.pages.append(line)
        for run in runs.values():
            run.undone = run.kind == PUBLISH and run.run_id in done
        return list(reversed(runs.values()))

    def target(self, run_id=None) -> Run | None:
        """The run ``cpub undo`` acts on: ``run_id``, else the newest publish run
        that is not undone yet (``None`` when there is none)."""
        runs = self.runs()
        if run_id is not None:
            return next((r for r in runs if r.run_id == run_id), None)
        return next((r for r in runs if r.kind == PUBLISH and not r.undone), None)
