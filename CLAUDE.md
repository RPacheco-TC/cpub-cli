# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A two-stage Markdown-to-Confluence documentation pipeline. The two stages are **fully independent** and share only the on-disk Markdown doc set as their interface:

1. **Generate** (no code in this repo runs it) — a human points Claude Code at a target repo plus `template.yaml`, and Claude writes a numbered Markdown doc set. `template.yaml` is the *spec/contract* the generator follows; `docs/GUIDE.md` holds the prompt and walkthrough. Nothing here executes this stage — it is prompt-driven.
2. **Publish** — the `confluence_publisher` package (run as `python3.12 confluence_publish.py` or `python3.12 -m confluence_publisher`) uploads that doc set to Confluence Cloud as a page tree under one parent page.

The Markdown is the source of truth; Confluence is a derived, re-publishable view. Edits should preserve that direction (e.g. `--prefix` namespaces titles at publish time *only*, never mutating the Markdown).

## Commands

```bash
# Credentials (never committed); the CLI loads them itself — no `set -a` needed
cp .env.example .env       # then edit: CONFLUENCE_BASE_URL (ends in /wiki), CONFLUENCE_EMAIL, CONFLUENCE_API_TOKEN
chmod 600 .env

# Always dry-run first — builds and checks the full plan, writes nothing
python3.12 confluence_publish.py DOCS_DIR --space KEY --parent PAGE_ID --prefix REPO --dry-run

# Publish for real
python3.12 confluence_publish.py DOCS_DIR --space KEY --parent PAGE_ID --prefix REPO
```

- **Credential lookup order, per variable, first hit wins:** real env vars → `--env-file PATH` → `./.env` (cwd) → `$XDG_CONFIG_HOME/confluence-publisher/.env` (default `~/.config/…`). `load_env()` parses `KEY=VALUE` / `export` / quotes / comments without executing anything; a missing `--env-file` is an error, a missing `./.env` or user file is not; a token file with `mode & 0o077` gets a `chmod 600` warning and the run continues. The `set -a; . ./.env; set +a` workflow still works (env wins).

- `--space` is the **key** (e.g. `ENG`, or `~yourname` for a personal space), resolved to a numeric space id at runtime.
- `--parent` must be a real **page** id, never a folder — the v2 API does not reliably publish under folders.
- `--prefix` is required in practice when several repos share one space, because Confluence titles must be unique *within a space*.
- `--img-dir` overrides where Mermaid PNGs are rendered; it defaults to `<docs_dir>/.img` (gitignored, regenerated each publish).
- `--img-width PX` (optional) sets `ac:width` on every Mermaid `ac:image`; unset means no attribute, i.e. unchanged output.
- `--max-updates N` (default 25) caps how many *existing* pages one run may UPDATE; creates don't count.
- `--adopt` hands over every page listed for hand-over, for this run only, without the typed prompt (non-interactive runs). There is deliberately **no `--yes`** — do not add one (decision D4).
- Every example and message names the command `python3.12 confluence_publish.py`. There is no `cpub` command or `setup` subcommand yet; do not mention them in output or docs.

```bash
# Regression tests — stdlib unittest, in-process and offline (no network, no npx,
# no Confluence env vars, no child processes); -B keeps it from writing __pycache__
python3.12 -B -m unittest discover -s tests
```

There is **no build, no linter, and no dependency install** — pure Python 3.12+ standard library; the only tests are the stdlib `unittest` suite in `tests/` (they import the package once via `tests/_support.py` and test the CLI through `build_parser()` / `main(argv)` — keep them free of subprocesses and temp files). The only external runtime dependency is `npx`/Node (for `@mermaid-js/mermaid-cli`), used solely to render Mermaid to PNG; without it diagrams fall back to a styled code block. The minimum is **Python 3.12**: `_check_python()` in `confluence_publisher/__init__.py` exits with a one-line message on older interpreters. Python runs `__init__.py` before any submodule, so both entry points (the shim and `-m`) hit the guard first; keep `__init__.py`, `__main__.py` and the shim parseable by old Pythons (3.9), because the submodules' `X | None` annotations are evaluated at definition time. On macOS the system `python3` is 3.9; use `python3.12` (Homebrew).

## Architecture: module map

```
confluence_publish.py          thin entry shim: from confluence_publisher.cli import main (the package import runs the guard)
confluence_publisher/
  __init__.py      Python >= 3.12 guard (3.9-parseable) + __version__
  __main__.py      python3.12 -m confluence_publisher (same usage/error lines as the shim)
  cli.py           build_parser, main, hand-over prompt, all output and error wording
  errors.py        CliError, PlanRefused, HandoverRequired, HandoverDeclined, PlanExecutionError
  config.py        credential loading (env > --env-file > ./.env > ~/.config/...), chmod warning
  api.py           Confluence REST client: _req, pages, attachments, whoami, space lookup,
                   ancestors/is_descendant, cpub-managed marker get_marker/set_marker
  plan.py          E0 safety: Plan/PlanItem/Refusal/RunReport, read_doc_set, build_plan/check_plan/
                   execute_plan (the only writer), fence and hand-over decisions, In Situ layout
  storage.py       Markdown -> Confluence storage XHTML: extractors, macros, md_to_storage,
                   validate_storage, prefixed_title
  markdown_html.py generic Markdown -> HTML, Mermaid rendering (was md_to_confluence.py)
docs/              GUIDE.md, KNOWN_ISSUES.md
template.yaml      Stage 1 contract (stays at the root)
```

**Dependency rule (no cycles; QA checks the imports):**

```
markdown_html  <-  storage  <-  plan (/ note in 1b)  <-  cli
plan also imports api and errors; cli imports config, api, errors and plan
config, api, errors: leaves — they import no other package module
storage, markdown_html: never import api — conversion stays pure and offline
```

One responsibility per module, none over ~650 lines (after R: markdown_html 615, cli 574, plan 374, api 369, storage 347, config 150, errors 53). A new module appears only when a new responsibility does (1b adds `pageref.py` for page ids/URLs and `note.py` for single-note publish).

`★ Insight ─────────────────────────────────────`
- The split is deliberate: `markdown_html` produces vanilla HTML and knows nothing about Confluence; `storage` upgrades it to Confluence storage XHTML (code → `ac:structured-macro`, callouts → colored panels, `<img>` → `ac:image` attachments, `[[wikilinks]]` → `ac:link`). Keep Confluence-specific markup out of `markdown_html.py`.
- Because `storage` never imports `api`, every body can be built and validated with no credentials and no network — which is what makes `build_plan` able to fail on a bad document before the first request, and what keeps the conversion tests offline.
- Plain package imports only: the old `importlib`-by-file-path load of `md_to_confluence.py` is gone.
`─────────────────────────────────────────────────`

### Where a change goes

- **A new Markdown → Confluence transform** (a macro, a link form, an embed): `storage.py`, inside `md_to_storage()`, at an explicit position in the ordering below. Generic Markdown syntax that is not Confluence-specific goes in `markdown_html.py`. Neither may call the REST API: anything that needs Confluence data is looked up by the caller (`plan`/`note`) and passed in.
- **A new safety rule or refusal:** `plan.py` (`check_plan` returns a `Refusal` with `message` and `fix`); its wording on screen is `cli.py`.
- **A new REST call:** `api.py`, through `_req` (keeps `HTTPError.detail`).
- **A new flag, prompt, message or output line:** `cli.py`. Expected failures raise `errors.CliError(sentence, fix, detail)`, never a bare `SystemExit` string.
- **Tests** patch a function on the module **where it is looked up** (e.g. `cli.load_env`, `plan.update_page`), not where it is defined.

### The transform ordering in `md_to_storage()` is load-bearing

`md_to_storage()` (in `storage.py`) is the heart of the conversion, and its step order is not arbitrary — several steps depend on running before/after others:

1. `_extract_wikilinks` **before** conversion — the bulk converter (`markdown_html.convert_wikilinks`, imported in `storage` as `mdc`) would otherwise *flatten* `[[links]]` to plain text. They are swapped for `xLINKNx` tokens and restored as `ac:link` markup at the end.
2. `_extract_callouts` **before** conversion — Obsidian callouts become `xCALLOUTNx` placeholder paragraphs, converted to panel macros and swapped back in after.
3. Mermaid `<img>` → attachments, then autolink + bare-`&` escaping.
4. `_code_blocks_to_macro` runs **last**, because it emits `CDATA` sections that the ampersand-escaping step must not touch.
5. `validate_storage()` parses the final body as XML before anything is sent — malformed content fails locally rather than producing a broken Confluence page.

`★ Insight ─────────────────────────────────────`
- This is a token-masking strategy: anything that must survive HTML conversion or escaping is pulled out, replaced with an inert sentinel (`xLINKNx`, `xCALLOUTNx`, or `\x00N\x00` inside `process_inline`), then restored last. If you add a new transform, decide explicitly where it sits relative to escaping and CDATA generation.
- `process_inline()` in `markdown_html.py` uses the same idea at the inline level — it masks code spans and links *before* applying emphasis/escaping so stray `*` or `<` inside code can't create crossing/invalid tags.
`─────────────────────────────────────────────────`

### A shared-mutable-state gotcha

Mermaid rendering is driven by **module-level globals** in `markdown_html.py`: `_current_img_dir` and `_current_img_rel`. `md_to_storage()` sets them before calling `convert_markdown_to_html()` and restores them in a `finally`. They are not parameters. If you refactor rendering, preserve this save/restore discipline or concurrent/repeat calls will render diagrams to the wrong place (or not at all).

### Publishing semantics

- **Plan first, write second (E0).** `main()` calls `build_plan()` (read-only: converts + validates every body, matches titles, reads page info, ancestors and the marker), then `check_plan()` (pure; returns *all* refusals), and only then `execute_plan()`, which re-checks and is the **only** code path allowed to call `create_page` / `update_page`. Any refusal aborts with zero writes and lists every offender. `--dry-run` is the same path stopped before `execute_plan`, so a passing dry-run means the real checks pass. A mid-run write failure raises `PlanExecutionError` whose report lists written vs not-written pages; re-running is safe.
- **Idempotency is by exact title match, fenced to the subtree.** A title match is an UPDATE target only if the page is a **strict descendant** of `--parent` (v2 ancestors); a match elsewhere in the space, the parent itself, an archived/trashed page, or a `--parent` in another space is refused. The `In Situ` container is fenced like every page (an existing one is never rewritten, as in v1.1).
- **Ownership marker `cpub-managed`** (a v2 content property, written in a *separate* call after the body — never inside the body, so storage bodies stay byte-identical to v1.1) records `{source, tool, since}`; source = prefixed title. Managed + same source → silent UPDATE; unmanaged or other source → **hand-over**: on a TTY the CLI lists the pages and asks `Take over and replace N page(s) you did not publish? Type the number N to confirm:`; non-TTY needs `--adopt` or refuses (never hangs). v1.1-published sets need this hand-over once on their first republish. The marker is an accident guard, **not access control**.
- **Never deletes, never moves (no `parentId` on update), never renames.** This is the core safety property — do not add deletion without an explicit opt-in flag (a `--prune` is listed as a deliberately-unimplemented future option).
- **CLI output contract:** the v1.1 `ACTION DIAGRAMS TITLE` table is byte-identical; ownership summary, hand-over list and refusals print around it, and a successful run ends with `Parent page: <url>`. Before planning, `main()` calls `whoami()` (one read-only GET `/rest/api/user/current`): Cloud serves a bad token anonymously, so this is the only reliable "bad credentials" signal — never infer it from a 404. Expected failures (`CliError`) print `Error: <one sentence>` + `Fix: <one line>` + indented raw detail (Confluence's `HTTPError.detail` is kept) and never a traceback; the API token is masked in every message.
- **Layout:** top-level `NN - Title.md` files become children of the parent; an `In Situ/` subfolder (matched case-insensitively via `find_insitu_dir`) becomes an `In Situ` container page with the deep-dives beneath it.
- **Attachments** (rendered Mermaid PNGs) are content-hash-named; `existing_attachment_names` skips names already present, since re-uploading a duplicate filename returns HTTP 400. **They must be uploaded *before* the body that references them is saved:** Confluence links each `ri:attachment` at save time (`ri:version-at-save`), and an unlinked reference renders in view mode but shows "Preview not available" in the editor and is dropped on the user's next save. So UPDATE uploads then saves; CREATE (the page must exist to take attachments) creates, uploads, then saves the body again. `tests/test_publish_order.py` pins this order.
- **Pre-flight duplicate check:** `main()` rejects a doc set with two files resolving to the same title *before* contacting Confluence, naming the colliding files.
- REST uses the **v2 API** for pages/spaces/attachments-listing, but the **v1 endpoint** for attachment *upload* (multipart). `_req` stashes Confluence's real error body on `HTTPError.detail` so `main()` can print the actual reason (v2's bare "HTTP Error 400" otherwise hides it).

## Conventions worth knowing (from `template.yaml`)

These shape the Markdown the publisher consumes, so they matter when debugging "why didn't this render/link":

- Filenames are `NN - Title.md` with gap-free numbering; `00 - Index.md` is generated last.
- Cross-links to numbered docs use the **full** `[[NN - Title]]` form — a bare `[[Title]]` renders as unresolved in Obsidian. Only unnumbered In Situ docs are linked bare.
- H2 section headings are **unnumbered plain text**, because the literal heading text is the cross-link anchor.
- `#heading` anchors in wikilinks are currently **dropped** on publish (`_extract_wikilinks`) — the link lands on the page, not the heading. Confluence uses a different anchor scheme; this is a known limitation, not a bug to "fix" casually.
- Mermaid labels must avoid `( ) : ; , # & " ' < > { } [ ]` unquoted (Obsidian's parser is strict); the `mermaid:` block in `template.yaml` has the full rules.

## Operational gotchas

- **macOS `ssl.SSLCertVerificationError: CERTIFICATE_VERIFY_FAILED` on publish.** Affects the **python.org** Python build, which ships a CA bundle that isn't installed by default (Homebrew's `python@3.12` works out of the box; Apple's `/usr/bin/python3` is 3.9 and no longer supported). Fix once by running the bundled `/Applications/Python 3.x/Install Certificates.command` (or `pip install --upgrade certifi` for that interpreter). Do **not** work around it by disabling certificate verification — that removes the security the check provides. See README troubleshooting and `docs/KNOWN_ISSUES.md`.
- **A `400` only ever surfaces on the real publish, never on `--dry-run`** (the dry-run reads but never exercises the write path). A duplicate title elsewhere in the space and a `--parent` in another space are now refused at plan time; a `400` that remains usually means `--parent` is a folder.
- **Tests must not pick up a real `./.env`.** The repo root may hold a developer's `.env`; CLI tests stub `load_env` / `env_search_paths` (and the REST helpers) where `cli` looks them up instead of reading files.

## Do not touch the project `.gitignore` for tooling/personal files

Editor, MCP, and AgentsRoom artifacts — `.mcp.json`, `.agentsroom/handoff-*`, `.claude/settings.local.json`, and the `.agentsroom/sessions/` cache — are **already ignored globally** via `~/.config/git/ignore`, and `.agentsroom/` also carries its own `.gitignore`. Do **not** add these paths to this repo's `.gitignore`; the committed `.gitignore` is for the *project's* build/runtime artifacts only (`.env`, `__pycache__/`, `.img/`, IDE dirs, `.DS_Store`). Personal/tooling ignores belong in the user's global excludes, not in a file shared with everyone who clones. If you notice one of these paths showing as untracked, it is already ignored — leave it alone rather than "fixing" it in the project `.gitignore`.
