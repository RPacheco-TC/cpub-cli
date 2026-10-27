"""Exceptions shared by the plan and the CLI (a leaf: imports nothing of ours)."""


class PlanRefused(Exception):
    """The plan failed an E0 check; nothing was written."""
    def __init__(self, refusals):
        super().__init__(f"{len(refusals)} page(s) refused")
        self.refusals = refusals


class HandoverRequired(Exception):
    """Pages need a hand-over but there is no way to ask (non-interactive, no --adopt)."""
    def __init__(self, items):
        super().__init__(f"{len(items)} page(s) need a hand-over (--adopt)")
        self.items = items


class HandoverDeclined(Exception):
    """The user declined the hand-over; nothing was written."""
    def __init__(self, items):
        super().__init__(f"hand-over of {len(items)} page(s) declined")
        self.items = items


class PlanExecutionError(Exception):
    """A write failed mid-run; ``report`` says what was and was not written."""
    def __init__(self, report):
        super().__init__(f"write failed: {report.error}")
        self.report = report



class CliError(Exception):
    """An expected failure, shown as one sentence plus a ``Fix:`` line (S3.1).

    :param sentence: what went wrong, in one plain sentence.
    :param fix: the one thing to do about it.
    :param detail: optional raw detail (e.g. Confluence's error body), shown
        underneath, indented.
    """

    def __init__(self, sentence, fix, detail=None):
        super().__init__(sentence)
        self.sentence, self.fix, self.detail = sentence, fix, detail

    def __str__(self):
        out = f"Error: {self.sentence}\nFix: {self.fix}"
        if self.detail:
            out += "\n" + "\n".join(("  " + ln) if ln.strip() else ""
                                      for ln in str(self.detail).splitlines())
        return out


