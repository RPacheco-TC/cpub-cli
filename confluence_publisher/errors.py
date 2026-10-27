"""Exceptions shared by the package (a leaf: imports nothing of ours).

Every expected failure derives from :class:`PublisherError`, so the CLI can
translate them in one place.
"""


class PublisherError(Exception):
    """Base of every error the publisher raises on purpose."""


class MissingCredentials(PublisherError):
    """Some credentials are not set anywhere.

    :param missing: the unset variable names, in :data:`~.config.CRED_VARS` order.
    :param searched: the files searched, highest priority first.
    """
    def __init__(self, missing, searched):
        super().__init__("Missing Confluence credential(s): " + ", ".join(missing))
        self.missing, self.searched = tuple(missing), list(searched)


class SpaceNotFound(PublisherError):
    """No Confluence space has the given key.

    :param key: the ``--space`` key that matched nothing.
    """
    def __init__(self, key):
        super().__init__(f"Space not found for key: {key}")
        self.key = key


class NotAFolder(PublisherError):
    """The doc-set path is not a folder.

    :param path: the path as given.
    """
    def __init__(self, path):
        super().__init__(f"Not a folder: {path}")
        self.path = path


class PlanRefused(PublisherError):
    """The plan failed an E0 check; nothing was written."""
    def __init__(self, refusals):
        super().__init__(f"{len(refusals)} page(s) refused")
        self.refusals = refusals


class HandoverRequired(PublisherError):
    """Pages need a hand-over but there is no way to ask (non-interactive, no --adopt)."""
    def __init__(self, items):
        super().__init__(f"{len(items)} page(s) need a hand-over (--adopt)")
        self.items = items


class HandoverDeclined(PublisherError):
    """The user declined the hand-over; nothing was written."""
    def __init__(self, items):
        super().__init__(f"hand-over of {len(items)} page(s) declined")
        self.items = items


class PlanExecutionError(PublisherError):
    """A write failed mid-run; ``report`` says what was and was not written."""
    def __init__(self, report):
        super().__init__(f"write failed: {report.error}")
        self.report = report


class CliError(PublisherError):
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


