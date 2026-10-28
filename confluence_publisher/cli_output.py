"""What a publish run shows the user: the plan, ownership, footer, and the hand-over prompt.

Split from :mod:`.cli` (which wires the run) so each module keeps one job.
Nothing here talks to Confluence or reads the environment: every class gets
its output stream (and, for the prompt, its input) through the constructor.
"""
import sys
import datetime

_MANAGED_LABEL = {
    "new": "new",
    "same": "published by this tool from this source",
    "other": "published by this tool from another source",
    "none": "not published by this tool",
}


# --- tiny stateless helpers ----------------------------------------------------

def _plural(n, word):
    """``"1 page"`` / ``"2 pages"``."""
    return f"{n} {word}" + ("" if n == 1 else "s")


def _need(n):
    """``"1 page needs"`` / ``"2 pages need"``."""
    return f"{_plural(n, 'page')} need" + ("s" if n == 1 else "")


def _size(n):
    """``"812 bytes"`` / ``"4.2 KB"``; ``"unknown size"`` when not known."""
    if n is None:
        return "unknown size"
    return f"{n} bytes" if n < 1024 else f"{n / 1024:.1f} KB"


def _when(iso):
    """Shorten a Confluence ISO timestamp to ``YYYY-MM-DD HH:MM UTC`` (best effort)."""
    try:
        t = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return t.astimezone(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, AttributeError):
        return str(iso)


# --- output -------------------------------------------------------------------------

class PlanPrinter:
    """Everything a run prints on success or dry-run (not errors).

    The v1.1 ``ACTION DIAGRAMS TITLE`` table is kept byte-identical; the
    ownership summary, hand-over list, footer and parent link go around it.

    :param out: the stream to print to.
    """

    def __init__(self, out):
        self.out = out

    def _print(self, *lines):
        for line in lines:
            print(line, file=self.out)

    @staticmethod
    def describe(item):
        """One line describing an existing page (hand-over lists and prompts)."""
        bits = [_MANAGED_LABEL.get(item.managed, item.managed)]
        if item.version is not None:
            bits.append(f"version {item.version}")
        if item.last_edited:
            bits.append(f"last edited {_when(item.last_edited)}")
        return f"  - {item.title}  ({', '.join(bits)})"

    def table(self, items):
        self._print(f"\n{'ACTION':7} {'DIAGRAMS':8} TITLE")
        for it in items:
            self._print(f"{it.action:7} {it.diagrams:<8} {it.title}")

    def ownership(self, plan, refusals=(), adopted=False):
        """Summarise who owns each planned page, then list the hand-over pages.

        Pages that a refusal already covers count as refused and are left out
        of the hand-over list: no hand-over can make them writable.

        :param adopted: the pages were taken over with ``--adopt`` (wording only).
        """
        refused = {r.page_id for r in refusals if getattr(r, "page_id", None)}
        counts = {}
        for it in plan.items:
            key = "refused" if it.page_id and it.page_id in refused else it.managed
            counts[key] = counts.get(key, 0) + 1
        labels = {**_MANAGED_LABEL, "refused": "refused"}
        parts = [f"{counts[k]} {labels[k]}" for k in ("new", "same", "other", "none", "refused")
                 if counts.get(k)]
        self._print("\nOwnership: " + ", ".join(parts))
        handover = [it for it in plan.handover_items() if it.page_id not in refused]
        if handover:
            n = len(handover)
            head = (f"Taken over ({n}):" if adopted else
                    f"{_need(n)} a one-time hand-over (this tool did not publish "
                    f"{'it' if n == 1 else 'them'} from this source):")
            self._print("\n" + head, *(self.describe(it) for it in handover))

    def handover_hint(self, n=2):
        """Dry-run note that the real run will ask; ``n`` picks "it" or "them"."""
        them = "it" if n == 1 else "them"
        self._print(f"\nThe real run will ask you to type the page count to hand {them} over "
                    "(or pass --adopt when not in a terminal).")

    def marker_warnings(self, report):
        for it, pid, exc in report.marker_failures:
            self._print(f"\nWarning: {it.title} was written but could not be marked as managed "
                        f"({exc}); the next run will ask to hand it over once.")

    def footer(self, count, done, args, parent_link, space=None):
        """``N pages <done> — space …`` then the clickable parent link (folder mode).

        :param space: the space to show when ``--space`` was omitted (taken from
            the parent page); the v1.1 line is unchanged when it was given.
        """
        prefix_note = f", prefix {args.prefix!r}" if args.prefix else ""
        self._print(f"\n{count} pages {done} — space {args.space or space}, "
                    f"parent {args.parent}{prefix_note}",
                    f"Parent page: {parent_link}")

    def preview(self, item, space):
        """S2.3: what a single-note run is about to do to its page."""
        if item.action == "CREATE":
            self._print(f"\nNew page: {item.title}  (space {space}, "
                        f"{_size(item.new_size)})")
            return
        bits = [f"version {item.version}"] if item.version is not None else []
        if item.last_edited:
            bits.append(f"last edited {_when(item.last_edited)}")
        self._print("\nReplace preview:",
                    f"  Page:     {item.title}  (space {space})",
                    f"  Current:  {', '.join(bits) or 'unknown version'}",
                    f"  Content:  {_size(item.old_size)} -> {_size(item.new_size)}")

    def note_footer(self, done, space, page_link, history=None):
        """``1 page <done> — space …``, the page link and (after a write) its history."""
        self._print(f"\n1 page {done} — space {space}", f"Page: {page_link}")
        if history:
            self._print(f"History: {history}  (earlier versions can be restored here)")

    def unrendered_warning(self, titles):
        """Set mode without ``npx``: name the pages whose diagrams stay code blocks."""
        if titles:
            self._print(f"Warning: npx not found, so the Mermaid diagrams in "
                        f"{_plural(len(titles), 'page')} are published as code blocks: "
                        + ", ".join(titles))

    def frontmatter_hint(self, line):
        """After a publish driven by flags, the line that makes them unnecessary (AC-2.4.2).

        The tool never edits the note (D5): the user pastes the line.
        """
        self._print("\nTo publish this note again without flags, paste this line into its "
                    "frontmatter",
                    "(the --- block at the top of the note; add one if it has none):",
                    f"  {line}")


# --- hand-over prompt (AC-0.3.3) ----------------------------------------------------

class HandoverPrompt:
    """Interactive hand-over: list the pages, ask for the typed page count.

    There is deliberately no y/N and no ``--yes`` (D4): typing the number makes
    the size of what is being replaced impossible to miss.

    :param out: stream for the listing.
    :param ask: ``prompt -> answer`` (default :func:`input`).
    :param stdin: checked with ``isatty()`` to decide whether asking is possible.
    """

    def __init__(self, out, ask=input, stdin=None):
        self.out, self.ask = out, ask
        self.stdin = sys.stdin if stdin is None else stdin

    def is_interactive(self):
        try:
            return bool(self.stdin.isatty())
        except (AttributeError, ValueError):
            return False

    def confirm(self, items):
        """:returns: ``True`` only when the user typed exactly ``len(items)``."""
        n = len(items)
        head = "This page was" if n == 1 else f"These {n} pages were"
        print(f"\n{head} not published by this tool from this source:", file=self.out)
        for it in items:
            print(PlanPrinter.describe(it), file=self.out)
        try:
            answer = self.ask(f"Take over and replace {n} page(s) you did not publish? "
                              f"Type the number {n} to confirm: ")
        except (EOFError, KeyboardInterrupt):
            print(file=self.out)
            return False
        return answer.strip() == str(n)
