# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A two-stage Markdown-to-Confluence documentation pipeline. The two stages are **fully independent** and share only the on-disk Markdown doc set as their interface:

1. **Generate** (no code in this repo runs it) — a human points Claude Code at a target repo plus `template.yaml`, and Claude writes a numbered Markdown doc set. `template.yaml` is the *spec/contract* the generator follows; `GUIDE.md` holds the prompt and walkthrough. Nothing here executes this stage — it is prompt-driven.
2. **Publish** — `confluence_publish.py` uploads that doc set to Confluence Cloud as a page tree under one parent page.

The Markdown is the source of truth; Confluence is a derived, re-publishable view. Edits should preserve that direction (e.g. `--prefix` namespaces titles at publish time *only*, never mutating the Markdown).

## Commands

```bash
# Load credentials (never committed) into the shell
cp .env.example .env       # then edit: CONFLUENCE_BASE_URL (ends in /wiki), CONFLUENCE_EMAIL, CONFLUENCE_API_TOKEN
set -a; . ./.env; set +a

# Always dry-run first — resolves the CREATE/UPDATE plan and writes nothing
python3 confluence_publish.py DOCS_DIR --space KEY --parent PAGE_ID --prefix REPO --dry-run

# Publish for real
python3 confluence_publish.py DOCS_DIR --space KEY --parent PAGE_ID --prefix REPO
```

- `--space` is the **key** (e.g. `ENG`, or `~yourname` for a personal space), resolved to a numeric space id at runtime.
- `--parent` must be a real **page** id, never a folder — the v2 API does not reliably publish under folders.
- `--prefix` is required in practice when several repos share one space, because Confluence titles must be unique *within a space*.
- `--img-dir` overrides where Mermaid PNGs are rendered; it defaults to `<docs_dir>/.img` (gitignored, regenerated each publish).
- `--img-width PX` (optional) sets `ac:width` on every Mermaid `ac:image`; unset means no attribute, i.e. unchanged output.

```bash
# Regression tests — stdlib unittest, in-process and offline (no network, no npx,
# no Confluence env vars, no child processes); -B keeps it from writing __pycache__
python3.12 -B -m unittest discover -s tests
```

There is **no build, no linter, and no dependency install** — pure Python 3.12+ standard library; the only tests are the stdlib `unittest` suite in `tests/` (they load `confluence_publish.py` once via `tests/_support.py` and test the CLI through `build_parser()` / `main(argv)` — keep them free of subprocesses and temp files). The only external runtime dependency is `npx`/Node (for `@mermaid-js/mermaid-cli`), used solely to render Mermaid to PNG; without it diagrams fall back to a styled code block. The minimum is **Python 3.12**: `_check_python()` near the top of `confluence_publish.py` exits with a one-line message on older interpreters. It must run *before* `md_to_confluence.py` is loaded and before any annotated `def`, because `X | None` annotations are evaluated at definition time — so keep the code above it parseable by old Pythons. On macOS the system `python3` is 3.9; use `python3.12` (Homebrew).

## Architecture: the conversion pipeline

The publish path is the whole codebase. Understanding it requires reading both modules together:

- **`md_to_confluence.py`** — a library of pure functions that turn Obsidian-style Markdown into *generic* HTML (headings, lists, tables, blockquotes, callouts, code, Mermaid). It knows nothing about Confluence's REST API.
- **`confluence_publish.py`** — the CLI. It loads `md_to_confluence.py` dynamically via `importlib` (as `mdc`), then **post-processes** the generic HTML into valid Confluence *storage format* (the XHTML-with-macros dialect Confluence stores), and handles all REST calls.

`★ Insight ─────────────────────────────────────`
- The split is deliberate: `mdc` produces vanilla HTML; `confluence_publish` upgrades it to Confluence storage XHTML (code → `ac:structured-macro`, callouts → colored panels, `<img>` → `ac:image` attachments, `[[wikilinks]]` → `ac:link`). Keep Confluence-specific markup out of `md_to_confluence.py`.
- `confluence_publish.py` imports `md_to_confluence.py` by file path with `importlib`, not a normal `import`, so the two files must sit side by side (`HERE / "md_to_confluence.py"`). Moving or renaming either breaks the load.
`─────────────────────────────────────────────────`

### The transform ordering in `md_to_storage()` is load-bearing

`md_to_storage()` (in `confluence_publish.py`) is the heart of the conversion, and its step order is not arbitrary — several steps depend on running before/after others:

1. `_extract_wikilinks` **before** conversion — the bulk converter (`mdc.convert_wikilinks`) would otherwise *flatten* `[[links]]` to plain text. They are swapped for `xLINKNx` tokens and restored as `ac:link` markup at the end.
2. `_extract_callouts` **before** conversion — Obsidian callouts become `xCALLOUTNx` placeholder paragraphs, converted to panel macros and swapped back in after.
3. Mermaid `<img>` → attachments, then autolink + bare-`&` escaping.
4. `_code_blocks_to_macro` runs **last**, because it emits `CDATA` sections that the ampersand-escaping step must not touch.
5. `validate_storage()` parses the final body as XML before anything is sent — malformed content fails locally rather than producing a broken Confluence page.

`★ Insight ─────────────────────────────────────`
- This is a token-masking strategy: anything that must survive HTML conversion or escaping is pulled out, replaced with an inert sentinel (`xLINKNx`, `xCALLOUTNx`, or `\x00N\x00` inside `process_inline`), then restored last. If you add a new transform, decide explicitly where it sits relative to escaping and CDATA generation.
- `process_inline()` in `md_to_confluence.py` uses the same idea at the inline level — it masks code spans and links *before* applying emphasis/escaping so stray `*` or `<` inside code can't create crossing/invalid tags.
`─────────────────────────────────────────────────`

### A shared-mutable-state gotcha

Mermaid rendering is driven by **module-level globals** in `md_to_confluence.py`: `_current_img_dir` and `_current_img_rel`. `md_to_storage()` sets them before calling `convert_markdown_to_html()` and restores them in a `finally`. They are not parameters. If you refactor rendering, preserve this save/restore discipline or concurrent/repeat calls will render diagrams to the wrong place (or not at all).

### Publishing semantics

- **Idempotency is by exact title match**, not a stored page map. `find_page_id_by_title` pages through *all* results (following `_links.next`), so re-publishing reliably UPDATEs rather than spuriously CREATEs. Re-running after editing Markdown is safe.
- **Never deletes; only creates/updates under the `--parent` you pass.** This is the core safety property — do not add deletion without an explicit opt-in flag (a `--prune` is listed as a deliberately-unimplemented future option).
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

- **macOS `ssl.SSLCertVerificationError: CERTIFICATE_VERIFY_FAILED` on publish.** Affects the **python.org** Python build, which ships a CA bundle that isn't installed by default (Homebrew's `python@3.12` works out of the box; Apple's `/usr/bin/python3` is 3.9 and no longer supported). Fix once by running the bundled `/Applications/Python 3.x/Install Certificates.command` (or `pip install --upgrade certifi` for that interpreter). Do **not** work around it by disabling certificate verification — that removes the security the check provides. See README troubleshooting.
- **A `400` only ever surfaces on the real publish, never on `--dry-run`** (the dry-run never exercises the write path). The usual causes are a duplicate title in the space (use a distinct `--prefix`) or a `--parent` that is a folder, or a page in a different space than `--space`.

## Do not touch the project `.gitignore` for tooling/personal files

Editor, MCP, and AgentsRoom artifacts — `.mcp.json`, `.agentsroom/handoff-*`, `.claude/settings.local.json`, and the `.agentsroom/sessions/` cache — are **already ignored globally** via `~/.config/git/ignore`, and `.agentsroom/` also carries its own `.gitignore`. Do **not** add these paths to this repo's `.gitignore`; the committed `.gitignore` is for the *project's* build/runtime artifacts only (`.env`, `__pycache__/`, `.img/`, IDE dirs, `.DS_Store`). Personal/tooling ignores belong in the user's global excludes, not in a file shared with everyone who clones. If you notice one of these paths showing as untracked, it is already ignored — leave it alone rather than "fixing" it in the project `.gitignore`.
