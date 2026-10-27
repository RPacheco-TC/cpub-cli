"""Publish a folder of Markdown docs to Confluence Cloud.

Publishes an Obsidian-style Markdown doc set as a page tree under one parent
page. Reuses :mod:`.markdown_html` for Markdown-to-HTML, then post-processes
into valid Confluence storage format (code macros; info/note/tip/warning panels;
autolink and ampersand fixes; ``[[wikilinks]]`` to page links), renders Mermaid
to PNG and uploads the images as attachments, validates each body as XML, and
creates or updates pages idempotently (matched by title).

Safe by construction: every run is planned and checked before the first write;
it only updates pages strictly *below* the parent you pass that it published
itself (``cpub-managed`` content property) or that you hand over in this run,
caps updates per run (``--max-updates``), and never deletes or moves anything.

Usage
-----
.. code-block:: console

    python3.12 confluence_publish.py DOCS_DIR --space KEY --parent PAGE_ID [--prefix P] [--dry-run]

``DOCS_DIR``
    folder of ``NN - Title.md`` files, with an optional ``In Situ/`` subfolder.
``--space KEY``
    Confluence space key (``~yourname`` for a personal space, or e.g. ``ENG``).
``--parent PAGE_ID``
    id of the page to publish everything under.
``--dry-run``
    print the plan (CREATE/UPDATE, ownership, refusals) and write nothing.
``--adopt``
    hand over the listed pages without the typed confirmation (non-interactive runs).
``--max-updates N``
    refuse a run that would update more than N existing pages (default 25).
``--env-file PATH``
    read credentials from this file.

Credentials
-----------
Each variable is taken from the first of: the environment, ``--env-file``,
``./.env``, ``$XDG_CONFIG_HOME/confluence-publisher/.env`` (default
``~/.config/…``). Files hold ``KEY=VALUE`` lines; never commit them:

``CONFLUENCE_BASE_URL``
    e.g. ``https://your-company.atlassian.net/wiki``
``CONFLUENCE_EMAIL``
    the Atlassian account email.
``CONFLUENCE_API_TOKEN``
    an API token.

.. note::

   Not yet implemented (contributions welcome): a ``state.json`` page map (this
   matches by title), content-hash skip of unchanged pages, exact heading-anchor
   links, and ``--prune`` of pages whose source was removed.
"""
import os
import sys
import ssl
import pathlib
import argparse
import datetime
import urllib.error

from .errors import (CliError, PlanRefused, HandoverRequired, HandoverDeclined,
                     PlanExecutionError)
from .config import CRED_VARS, load_env, apply_env, env_search_paths
from .api import whoami, resolve_space_id
from .plan import build_plan, check_plan, handover_items, execute_plan, find_insitu_dir


CMD = "python3.12 confluence_publish.py"
TOKEN_URL = "https://id.atlassian.com/manage-profile/security/api-tokens"


def _positive_int(value):
    """Parse a strictly positive integer for argparse (used by ``--img-width``).

    :param value: the raw command-line string.
    :type value: str
    :rtype: int
    :raises argparse.ArgumentTypeError: if ``value`` is not an integer >= 1.
    """
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {value!r}") from None
    if n < 1:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {value!r}")
    return n


def _non_negative_int(value):
    """Parse an integer >= 0 for argparse (used by ``--max-updates``).

    :param value: the raw command-line string.
    :type value: str
    :rtype: int
    :raises argparse.ArgumentTypeError: if ``value`` is not an integer >= 0.
    """
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"must be a whole number >= 0, got {value!r}") from None
    if n < 0:
        raise argparse.ArgumentTypeError(f"must be a whole number >= 0, got {value!r}")
    return n


_HELP_INTRO = f"""\
Publish a folder of Markdown docs to Confluence Cloud under one parent page.

example:
  {CMD} docs/ --space ENG --parent 123456789 --prefix myrepo --dry-run
  {CMD} docs/ --space ENG --parent 123456789 --prefix myrepo

Safety: every run is planned and checked before anything is written. It only
updates pages below --parent that it published itself; any other page needs a
one-time hand-over (type the page count when asked, or --adopt in scripts).
Nothing is ever deleted or moved.
"""

_HELP_CREDS = """\
credentials (first hit wins, per variable):
  1. environment variables CONFLUENCE_BASE_URL, CONFLUENCE_EMAIL, CONFLUENCE_API_TOKEN
  2. --env-file PATH
  3. ./.env in the current folder
  4. ~/.config/confluence-publisher/.env  ($XDG_CONFIG_HOME is honoured)
  Files use KEY=VALUE lines, as in .env.example. Keep them chmod 600.
"""


def build_parser():
    """Build the command-line parser (separate from :func:`main` so tests can
    exercise argument validation in-process).

    :rtype: argparse.ArgumentParser
    """
    ap = argparse.ArgumentParser(
        description=_HELP_INTRO, epilog=_HELP_CREDS,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("docs_dir", help='folder of "NN - Title.md" docs (optional "In Situ/" subfolder)')
    ap.add_argument("--space", required=True, help="Confluence space KEY (e.g. ~yourname or ENG)")
    ap.add_argument("--parent", required=True, help="ID of the parent page to publish under")
    ap.add_argument("--img-dir", default=None,
                    help="where to render Mermaid PNGs (default: <docs_dir>/.img)")
    ap.add_argument("--prefix", default="",
                    help="unique identifier prepended to every page title "
                         '(e.g. "langflow" -> "langflow - 01 - Overview"); use a '
                         "distinct value per repo so docs sharing a space never collide")
    ap.add_argument("--img-width", type=_positive_int, default=None, metavar="PX",
                    help="display width in pixels for every Mermaid diagram "
                         "(default: unset, natural size)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the plan (CREATE/UPDATE, ownership, refusals) and write nothing")
    ap.add_argument("--adopt", action="store_true",
                    help="take over the pages listed for hand-over without the typed "
                         "confirmation (for scripts; this run only)")
    ap.add_argument("--max-updates", type=_non_negative_int, default=25, metavar="N",
                    help="refuse a run that would update more than N existing pages "
                         "(default: 25; creates don't count)")
    ap.add_argument("--env-file", default=None, metavar="PATH",
                    help="read credentials from this .env file (see 'credentials' below)")
    return ap


# --- output -----------------------------------------------------------------

_MANAGED_LABEL = {
    "new": "new",
    "same": "published by this tool from this source",
    "other": "published by this tool from another source",
    "none": "not published by this tool",
}


def _page_link(webui=None, page_id=None):
    """Return a clickable URL for a page, from its v2 ``webui`` path or its id.

    :param webui: the page's ``_links.webui`` (relative to the ``/wiki`` base),
        or an absolute URL, or ``None``.
    :param page_id: fallback page id when ``webui`` is unknown.
    :rtype: str
    """
    base = os.environ.get("CONFLUENCE_BASE_URL", "").rstrip("/")
    if webui:
        return webui if webui.startswith("http") else base + webui
    return f"{base}/pages/viewpage.action?pageId={page_id}"


def _print_table(items, out):
    """Print the v1.1 ``ACTION DIAGRAMS TITLE`` table — kept byte-identical."""
    print(f"\n{'ACTION':7} {'DIAGRAMS':8} TITLE", file=out)
    for it in items:
        print(f"{it.action:7} {it.diagrams:<8} {it.title}", file=out)


def _plural(n, word):
    """``"1 page"`` / ``"2 pages"``."""
    return f"{n} {word}" + ("" if n == 1 else "s")


def _when(iso):
    """Shorten a Confluence ISO timestamp to ``YYYY-MM-DD HH:MM UTC`` (best effort)."""
    try:
        t = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return t.astimezone(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, AttributeError):
        return str(iso)


def _describe_page(it):
    """One line describing an existing page for the hand-over list."""
    bits = [_MANAGED_LABEL.get(it.managed, it.managed)]
    if it.version is not None:
        bits.append(f"version {it.version}")
    if it.last_edited:
        bits.append(f"last edited {_when(it.last_edited)}")
    return f"  - {it.title}  ({', '.join(bits)})"


def _need(n):
    """``"1 page needs"`` / ``"2 pages need"``."""
    return f"{_plural(n, 'page')} need" + ("s" if n == 1 else "")


def _print_ownership(plan, out, refusals=(), adopted=False):
    """Summarise who owns each planned page, then list the hand-over pages.

    Pages that a refusal already covers are counted as refused and left out of
    the hand-over list: no hand-over can make them writable.

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
    print("\nOwnership: " + ", ".join(parts), file=out)
    handover = [it for it in handover_items(plan) if it.page_id not in refused]
    if handover:
        n = len(handover)
        head = (f"Taken over ({n}):" if adopted else
                f"{_need(n)} a one-time hand-over (this tool did not publish "
                f"{'it' if n == 1 else 'them'} from this source):")
        print("\n" + head, file=out)
        for it in handover:
            print(_describe_page(it), file=out)


def _refused(refusals):
    """Turn ``check_plan`` refusals into one :class:`CliError` listing all of them."""
    lines = []
    for r in refusals:
        lines.append(f"- {r.message}")
        if r.fix:
            lines.append(f"  Fix: {r.fix}")
    return CliError(
        f"The plan was refused ({_plural(len(refusals), 'problem')}) — nothing was written.",
        "Resolve every item below, then run the same command again (try --dry-run first).",
        "\n".join(lines))


# --- hand-over prompt (AC-0.3.3) ----------------------------------------------

def _stdin_is_tty():
    """Whether we may ask the user a question (patched in tests)."""
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _ask(prompt):
    """Read one answer from the terminal (patched in tests)."""
    return input(prompt)


def confirm_handover(items, *, ask=None, out=None):
    """Interactive hand-over: list the pages, ask for the typed page count.

    There is deliberately no y/N and no ``--yes`` (D4): typing the number makes
    the size of what is being replaced impossible to miss.

    :param items: the plan items that need a hand-over.
    :param ask: input function (default :func:`_ask`).
    :param out: stream for the listing (default stdout).
    :returns: ``True`` only when the user typed exactly ``len(items)``.
    :rtype: bool
    """
    ask = ask or _ask
    out = out or sys.stdout
    n = len(items)
    print(f"\nThese {n} page(s) exist under the parent but were not published by this "
          "tool from this source:", file=out)
    for it in items:
        print(_describe_page(it), file=out)
    try:
        answer = ask(f"Take over and replace {n} page(s) you did not publish? "
                     f"Type the number {n} to confirm: ")
    except (EOFError, KeyboardInterrupt):
        print(file=out)
        return False
    return answer.strip() == str(n)


# --- friendly errors (S3.1) -----------------------------------------------------

def _http_error(e, args):
    """Map a Confluence ``HTTPError`` to a :class:`CliError` (sentence + Fix + detail)."""
    raw = (f"Confluence said: {e.code} {e.reason}\n"
           f"{getattr(e, 'detail', '') or '(no detail returned)'}")
    if e.code == 401:
        return _bad_token(args, raw)
    if e.code == 403:
        return CliError(f"Your account can't edit this page/space ({args.space}).",
                        "Ask a space admin for edit permission, or publish under a page you can edit.",
                        raw)
    if e.code == 404:
        return CliError(f"Page {args.parent} was not found or is not visible to you.",
                        f"Check --parent: it must be the id of a page (not a folder) in space "
                        f"{args.space}.", raw)
    if e.code == 409:
        return CliError("A page was edited in Confluence after this run was planned, so it "
                        "was not overwritten.",
                        "Check the latest version in Confluence, then run the same command "
                        "again to re-plan.", raw)
    if e.code == 400:
        return CliError("Confluence refused the request as invalid.",
                        f"Usually --parent {args.parent} is a folder (use a page) or a page "
                        f"title already exists in space {args.space} (use a distinct --prefix).",
                        raw)
    return CliError(f"Confluence returned an unexpected error ({e.code}).",
                    "Try again; if it persists, check the detail below.", raw)


def _no_space(args, raw=None):
    return CliError(f"Confluence has no space with the key {args.space!r} that you can see.",
                    "Check --space: it is the part after /spaces/ in a page URL "
                    "(~yourname for a personal space).", raw)


def _token_where(args):
    """Where the API token came from, for the ``Fix:`` line."""
    src = getattr(args, "token_source", None) or "environment"
    return "your environment variables" if src == "environment" else src


def _bad_token(args, raw=None):
    return CliError("Confluence did not accept your email/API token.",
                    f"Check CONFLUENCE_EMAIL and CONFLUENCE_API_TOKEN in {_token_where(args)} "
                    f"(create a new token at {TOKEN_URL}).", raw)


def _check_identity(args):
    """B1/AC-3.1.3: one read-only identity check before planning.

    Confluence Cloud serves a bad email/token as an anonymous request, so a
    wrong token would otherwise surface later as a misleading 404.
    """
    me = whoami()
    if not me.get("authenticated"):
        how = (f"Confluence said: {me['status']}" if me.get("status") not in (None, 200)
               else "Confluence treated the request as anonymous.")
        raise _bad_token(args, how)


def _read_error(e, args, stage):
    """Map an ``HTTPError`` from the read-only phase, naming what failed.

    :param stage: ``"space"`` (space-key lookup) or ``"parent"`` (planning
        reads, starting with the ``--parent`` page).
    """
    if e.code == 404 and stage == "space":
        return _no_space(args, _http_error(e, args).detail)
    return _http_error(e, args)


def _url_error(e):
    """Map a network/TLS ``URLError`` to a :class:`CliError`."""
    base = os.environ.get("CONFLUENCE_BASE_URL", "(unset)")
    reason = getattr(e, "reason", e)
    if isinstance(reason, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(reason):
        return CliError("Python could not verify Confluence's TLS certificate.",
                        "If you use the python.org build on macOS, run "
                        "'/Applications/Python 3.12/Install Certificates.command' once "
                        "(never disable verification). See docs/KNOWN_ISSUES.md.", str(reason))
    return CliError(f"Could not reach Confluence at {base}.",
                    "Check CONFLUENCE_BASE_URL (it ends in /wiki) and your network or VPN.",
                    str(reason))


def _mask_secrets(text):
    """Replace the API token, if it appears, with ``****`` (AC-X.6)."""
    token = os.environ.get("CONFLUENCE_API_TOKEN")
    return text.replace(token, "****") if token and len(token) >= 4 else text


# --- main ---------------------------------------------------------------------

def _load_credentials(args):
    """Resolve credentials (S1.1) into ``os.environ`` or raise :class:`CliError`."""
    try:
        resolved = load_env(env_file=args.env_file)
    except FileNotFoundError as e:
        path = e.filename or args.env_file
        raise CliError(f"The --env-file {path} does not exist.",
                       "Pass the path of an existing .env file, or drop --env-file to use "
                       "./.env or ~/.config/confluence-publisher/.env.") from None
    apply_env(resolved)
    args.token_source = (resolved.get("CONFLUENCE_API_TOKEN") or (None, "environment"))[1]
    missing = [v for v in CRED_VARS if not os.environ.get(v)]
    if missing:
        searched = ["environment variables"] + [str(p) for p in env_search_paths(env_file=args.env_file)]
        raise CliError(
            "Missing Confluence credential(s): " + ", ".join(missing) + ".",
            "Add them as KEY=VALUE lines (see .env.example) to ./.env or "
            "~/.config/confluence-publisher/.env, or export them.",
            "Searched, in order:\n" + "\n".join(f"- {s}" for s in searched))


def _check_titles(docs):
    """Pre-flight duplicate-title check (v1.1), before any network call."""
    titles = [f.stem for f in docs.glob("*.md")]
    insitu = find_insitu_dir(docs)
    if insitu is not None:
        titles.append("In Situ")
        titles += [f.stem for f in insitu.glob("*.md")]
    dupes = sorted({t for t in titles if titles.count(t) > 1})
    if dupes:
        raise CliError(
            "Two or more files in this doc set resolve to the same page title, and "
            "Confluence requires titles to be unique within a space.",
            "Rename the colliding file(s) so every page has a distinct title.",
            "\n".join(f"- {t!r}" for t in dupes))


def _run(args, out):
    """The body of :func:`main`; raises :class:`CliError` for expected failures."""
    _load_credentials(args)
    docs = pathlib.Path(args.docs_dir)
    if not docs.is_dir():
        raise CliError(f"Not a folder: {args.docs_dir}.",
                       'Pass the folder that holds the "NN - Title.md" docs.')
    _check_titles(docs)

    stage = "space"
    try:
        _check_identity(args)
        try:
            space_id = resolve_space_id(args.space)
        except SystemExit:
            raise _no_space(args) from None
        stage = "parent"
        img_dir = args.img_dir or str(docs / ".img")
        plan = build_plan(args.docs_dir, space_id, args.parent, img_dir,
                          prefix=args.prefix, img_width=args.img_width)
    except urllib.error.HTTPError as e:
        raise _read_error(e, args, stage) from None
    refusals = check_plan(plan, max_updates=args.max_updates)

    prefix_note = f", prefix {args.prefix!r}" if args.prefix else ""
    parent_link = _page_link((plan.parent_info or {}).get("webui"), args.parent)

    if args.dry_run or refusals:
        _print_table(plan.items, out)
        _print_ownership(plan, out, refusals)
        if refusals:
            raise _refused(refusals)
        if handover_items(plan):
            print("\nThe real run will ask you to type the page count to hand them over "
                  "(or pass --adopt when not in a terminal).", file=out)
        print(f"\n{len(plan.items)} pages DRY-RUN (nothing written) — space {args.space}, "
              f"parent {args.parent}{prefix_note}", file=out)
        print(f"Parent page: {parent_link}", file=out)
        return

    confirm = None
    if not args.adopt and _stdin_is_tty():
        confirm = lambda items: confirm_handover(items, out=out)   # noqa: E731
    try:
        report = execute_plan(plan, max_updates=args.max_updates, adopt=args.adopt,
                              confirm_handover=confirm)
    except PlanRefused as e:                       # re-check inside execute_plan
        raise _refused(e.refusals) from None
    except HandoverRequired as e:
        n = len(e.items)
        raise CliError(
            f"{_need(n)} a one-time hand-over and this is not an interactive "
            "terminal — nothing was written.",
            f"Run it in a terminal and type {n} when asked, or add --adopt to take them over.",
            "\n".join(_describe_page(it).strip() for it in e.items)) from None
    except HandoverDeclined as e:
        n = len(e.items)
        raise CliError("Hand-over not confirmed — nothing was written.",
                       f"Run again and type {n} to take the page(s) over, or use a "
                       "distinct --prefix to publish new pages instead.") from None
    except PlanExecutionError as e:
        raise _partial(e.report, args) from None

    _print_table(plan.items, out)
    if args.adopt and handover_items(plan):
        _print_ownership(plan, out, adopted=True)
    for it, pid, exc in report.marker_failures:
        print(f"\nWarning: {it.title} was written but could not be marked as managed "
              f"({exc}); the next run will ask to hand it over once.", file=out)
    print(f"\n{len(plan.items)} pages published — space {args.space}, "
          f"parent {args.parent}{prefix_note}", file=out)
    print(f"Parent page: {parent_link}", file=out)


def _partial(report, args):
    """AC-0.1.4: a write failed mid-run — say what was and wasn't written."""
    cause = report.error
    if isinstance(cause, urllib.error.HTTPError):
        why = _http_error(cause, args)
    elif isinstance(cause, urllib.error.URLError):
        why = _url_error(cause)
    else:
        why = CliError(str(cause), "")
    partial = getattr(report, "partial", None)
    written = "\n".join(f"- {it.title}" for it, _ in report.written) or "- (none)"
    pending = "\n".join(
        f"- {it.title}" + ("  (created, but its diagrams/body did not finish)"
                           if partial and partial[0] is it else "")
        for it in report.pending) or "- (none)"
    failed = partial[0] if partial else (report.pending[0] if report.pending else None)
    if isinstance(cause, urllib.error.HTTPError) and cause.code == 409 and failed is not None:
        # update_page sends the plan-time version: someone saved the page meanwhile.
        return CliError(
            f'"{failed.title}" was edited in Confluence while this run was publishing, '
            "so it was not overwritten.",
            "Re-run with --dry-run to see its new state, then publish again.",
            f"{why.detail}\n\nWritten ({len(report.written)}):\n{written}\n"
            f"Not written ({len(report.pending)}):\n{pending}")
    detail = (f"{why.sentence}\n{why.detail or ''}\n\n"
              f"Written ({len(report.written)}):\n{written}\n"
              f"Not written ({len(report.pending)}):\n{pending}").replace("\n\n\n", "\n\n")
    rerun = "Then run the same command again; it is safe to re-run."
    if why.fix and "command again" in why.fix:
        fix = why.fix + " It is safe to re-run."
    else:
        fix = (why.fix + " " if why.fix else "") + rerun
    return CliError("The run stopped because a write failed — some pages were written, "
                    "the rest were not.", fix, detail)


def main(argv=None, out=None):
    """Parse arguments, plan, check, and publish the doc set.

    Expected failures (credentials, bad folder, refusals, hand-over, Confluence
    HTTP/TLS errors) exit with one sentence and a ``Fix:`` line, never a
    traceback (S3.1).

    :param argv: argument list (``sys.argv[1:]`` when ``None``).
    :param out: stream for normal output (stdout when ``None``).
    :raises SystemExit: on any expected failure (exit status 1) or bad arguments (2).
    """
    args = build_parser().parse_args(argv)
    out = out or sys.stdout
    try:
        _run(args, out)
    except CliError as e:
        raise SystemExit(_mask_secrets(str(e))) from None
    except urllib.error.HTTPError as e:
        raise SystemExit(_mask_secrets(str(_http_error(e, args)))) from None
    except urllib.error.URLError as e:
        raise SystemExit(_mask_secrets(str(_url_error(e)))) from None


if __name__ == "__main__":
    main()
