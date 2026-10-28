# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A two-stage Markdown-to-Confluence documentation pipeline. The two stages are **fully independent** and share only the on-disk Markdown doc set as their interface:

1. **Generate** (no code in this repo runs it) — a human points Claude Code at a target repo plus `template.yaml`, and Claude writes a numbered Markdown doc set. `template.yaml` is the *spec/contract* the generator follows; `docs/GUIDE.md` holds the prompt and walkthrough. Nothing here executes this stage — it is prompt-driven.
2. **Publish** — the `confluence_publisher` package (run as `python3.12 confluence_publish.py` or `python3.12 -m confluence_publisher`) uploads that doc set to Confluence Cloud as a page tree under one parent page.

The Markdown is the source of truth; Confluence is a derived, re-publishable view. Edits should preserve that direction (e.g. `--prefix` namespaces titles at publish time *only*, never mutating the Markdown).

## Commands

```bash
# Install the command (zero dependencies; uv brings Python 3.12+); or use pipx
uv tool install git+https://github.com/RPacheco-TC/cpub-cli
cpub --version                       # cpub 1.5.0; `confluence-publish` is the same command
# From a clone, these are equivalent to `cpub …`:
#   python3.12 confluence_publish.py …      python3.12 -m confluence_publisher …

# Credentials (never committed); the CLI loads them itself — no `set -a` needed
cp .env.example .env       # then edit: CONFLUENCE_BASE_URL (ends in /wiki), CONFLUENCE_EMAIL, CONFLUENCE_API_TOKEN
chmod 600 .env

# Always dry-run first — builds and checks the full plan, writes nothing
python3.12 confluence_publish.py DOCS_DIR --space KEY --parent PAGE_ID --prefix REPO --dry-run

# Publish for real
python3.12 confluence_publish.py DOCS_DIR --space KEY --parent PAGE_ID --prefix REPO

# A single note: replace a page (title kept) or create a child page
python3.12 confluence_publish.py NOTE.md --to PAGE_URL_OR_ID [--title T] --dry-run
python3.12 confluence_publish.py NOTE.md --under PAGE_URL_OR_ID [--title T]
```

- **Mode is chosen by the path**: a `.md` file → note mode, a folder → set mode (`ModeResolver`, before credentials or any network call). Note mode needs exactly one of `--to` / `--under` (`--parent` is accepted as `--under`), or a frontmatter target (below); set mode needs `--parent` (or its alias `--under`) and rejects `--to` / `--title`.
- **Every page argument takes an id or a page URL** (`…/spaces/KEY/pages/ID[/slug]`, `…/pages/edit-v2/ID`, `…/viewpage.action?pageId=ID`), parsed by `PageRef.parse` with no network call; folder URLs, `/wiki/x/` short links, unknown forms and other hosts are refused locally.
- **`--space` is optional** in both modes: the space comes from the page; when given it is checked against the page (refusal on mismatch). With `--space`, set mode makes exactly the v1.1/R2 request sequence.
- **Note mode**: no title prefix by default; `--to` replaces exactly that page (no title search, never moved, title kept unless `--title`, the space homepage refused); `--under` titles the page `--title` > frontmatter `title:` > file stem and is fenced like a set. The marker source is the note's **filename**; the version comment is `Published from Obsidian: <filename>`. Diagrams render to `AppDirs().note_img_dir` (`$XDG_CACHE_HOME/confluence-publisher/img`), never into the vault.
- **Unresolved links (S2.5, note mode only)**: the planner reads the space's titles once and gives `StorageConverter` a `link_resolver`; only the `xLINKNx` **restore** step changes (missing target → display text in `<em>`), never extraction, the fence rule or the step order. `plan.unresolved_links` feeds one `N links left as text: …` line (`PlanPrinter.unresolved_links`, real run and `--dry-run`). Set mode passes no resolver: byte-identical output.
- **Plain-text section references (H2, warning only)**: `storage.SectionRefFinder` (pure, a `FenceAwareExtractor` that never rewrites) finds bare `§N` / `§N.M` outside code and outside `[[…]]` / `[…](…)` text whose number matches a heading numbered `N.` or `N.M` in the same source. `Planner` stores them on `PlanItem.section_refs` (file line numbers, frontmatter counted) and `PlanPrinter.section_refs` prints one `N section references are plain text: §3 (line 48), … Link them as [[#Heading|§N]].` line after the plan (real run, `--dry-run` and a refused plan; set mode: one line per page, `In <title>: …`). Bodies, requests and the exit code never change.
- **Frontmatter target (S2.4)**: a note given with no `--to` / `--under` / `--parent` takes its target from its frontmatter: `confluence:` acts as `--to`, `confluence_parent:` as `--under` (`SingleNote.targets()` -> `{"to", "under"}`, read by `ModeResolver` through its injected `targets_of`, only when no flag is given). **Flags win**; both keys at once, or neither key and no flag, is a `CliError`. Values go through `PageRef.parse` like a flag. The tool **never writes the note** (D5): after a real publish made with a flag, on a note with neither key, `PlanPrinter.frontmatter_hint` prints the line to paste (the key mirrors the flag; the value is the by-id `viewpage.action?pageId=` URL, which has no `#` for YAML to eat). Publishing from Obsidian uses our own plugin **cpub for Confluence** (separate repo https://github.com/RPacheco-TC/confluence-publisher-obsidian; it runs `cpub` with `execFile`, never `--adopt`, no credentials); the recipe is in `docs/GUIDE.md` (the Shell commands plugin is broken on Obsidian 1.14, see `docs/KNOWN_ISSUES.md`).

- **Credential lookup order, per variable, first hit wins:** real env vars → `--env-file PATH` → `./.env` (cwd) → `$XDG_CONFIG_HOME/confluence-publisher/.env` (default `~/.config/…`). `CredentialLoader` parses `KEY=VALUE` / `export` / quotes / comments without executing anything; a missing `--env-file` is an error, a missing `./.env` or user file is not; a token file with `mode & 0o077` gets a `chmod 600` warning and the run continues. The `set -a; . ./.env; set +a` workflow still works (env wins).

- `--space` is the **key** (e.g. `ENG`, or `~yourname` for a personal space), resolved to a numeric space id at runtime.
- `--parent` must be a real **page** id, never a folder — the v2 API does not reliably publish under folders.
- `--prefix` is required in practice when several repos share one space, because Confluence titles must be unique *within a space*.
- `--img-dir` overrides where Mermaid PNGs are rendered; it defaults to `<docs_dir>/.img` (gitignored, regenerated each publish).
- `--img-width PX` (optional) sets `ac:width` on every Mermaid `ac:image`; unset means no attribute, i.e. unchanged output.
- `--max-updates N` (default 25) caps how many *existing* pages one run may UPDATE; creates don't count.
- `--adopt` hands over every page listed for hand-over, for this run only, without the typed prompt (non-interactive runs). There is deliberately **no `--yes`** — do not add one (decision D4).
- **The command name follows how the tool was started (D26)**: `cpub` / `confluence-publish` (console scripts), `python3.12 confluence_publish.py` (the shim), `python3.12 -m confluence_publisher` (`-m`). `CommandName.resolve(sys.argv[0])` in `cli.py` decides it **once** and `main()` injects it into `ArgumentParserFactory` (usage, examples, `--version`) and `ErrorTranslator` (every `Fix:` line); no command string is hard-coded anywhere else (a test checks). `setup`, `check` and `undo` are reserved first words (D2): `main()` dispatches them to `subcommand_parser` + `build_subcommand` (their composition root, in `cli.py`); a path named so is given as `./setup`.
- **`setup` (S1.4)** is `cli_commands.SetupCommand`: refuses a non-TTY stdin (prints the KEY= format, never hangs); asks before replacing an existing file (`yes`); site URL normalised to `https://host/wiki` (`normalize_site_url`, https only); hidden token via injected `getpass`; tests with one `whoami` (re-asks email/token up to 3 times on rejection; network/TLS/404 stop at once); `config.CredentialWriter.write(creds, identity, overwrite)` refuses unauthenticated credentials and writes atomically (dir 700, file 600). Only `whoami` is ever called.
- **`check` (S1.5)**: `checks.EnvironmentCheck(...).run(env_file)` returns raw `CheckResult(name, status, facts)` (`ok`/`warn`/`fail`/`skip`) for python, credentials, tls, auth, npx, cpub; `cli_commands.CheckReport` words them (`✓ ! ✗ -` plus `Fix:`), and `CheckCommand` exits 1 only on a `fail` (npx and a missing installed `cpub` are warnings). Writes nothing; TLS verification is never turned off.
- **Packaging:** `pyproject.toml` (setuptools, `requires-python >= 3.12`, **no dependencies**, the `confluence_publisher` package only — no tests, docs or shim in the wheel). The version has one source, `confluence_publisher.__version__` (D27); scripts `cpub` and `confluence-publish` → `confluence_publisher.cli:main`.

```bash
# Regression tests — stdlib unittest, in-process and offline (no network, no npx,
# no Confluence env vars, no child processes); -B keeps it from writing __pycache__
python3.12 -B -m unittest discover -s tests
```

There is **no build, no linter, and no dependency install** — pure Python 3.12+ standard library; the only tests are the stdlib `unittest` suite in `tests/` (they import the package once via `tests/_support.py` and drive the CLI through `build_parser()` and `PublishCommand.from_args(...)` with injected fakes — keep them free of subprocesses, temp files and `setattr` patching). The only external runtime dependency is `npx`/Node (for `@mermaid-js/mermaid-cli`), used solely to render Mermaid to PNG. `AppDirs.find_executable("npx")` looks on `PATH`, then `/opt/homebrew/bin` and `/usr/local/bin` (Obsidian starts commands with a bare `PATH`); `PublishCommand.from_args` passes the result to `StorageConverter(npx=…)`. Without npx, **set mode** falls back to a styled code block and prints one `Warning: npx not found …` line naming the pages (`plan.unrendered_items()`); **note mode refuses** before any write (`DiagramsUnrenderable` → `Error: Diagrams in <note> cannot be rendered (npx not found).`), so a hotkey republish never swaps a page's diagrams for code. The minimum is **Python 3.12**: `_check_python()` in `confluence_publisher/__init__.py` exits with a one-line message on older interpreters. Python runs `__init__.py` before any submodule, so both entry points (the shim and `-m`) hit the guard first; keep `__init__.py`, `__main__.py` and the shim parseable by old Pythons (3.9), because the submodules' `X | None` annotations are evaluated at definition time. On macOS the system `python3` is 3.9; use `python3.12` (Homebrew).

## Architecture: classes and modules

The package is **class-based with constructor injection** (decision D24): behaviour and state live in classes whose dependencies are passed to the constructor; module level holds only constants, compiled regexes and short stateless private helpers. Nothing reads `os.environ` except `CredentialLoader` and `AppDirs`, and there is no module-level mutable state.

```
pyproject.toml          packaging: scripts cpub / confluence-publish -> confluence_publisher.cli:main
confluence_publish.py   thin entry shim for clones: from confluence_publisher.cli import main
confluence_publisher/
  __init__.py      Python >= 3.12 guard (3.9-parseable) + __version__
  __main__.py      python3.12 -m confluence_publisher (names itself so in help and Fix lines)
  cli.py           the ONE composition root: PublishCommand.from_args (wires PublishFlow),
                   build_subcommand (setup/check); CommandName (how it was started),
                   ArgumentParserFactory (+ --version), ModeResolver -> RunMode; main()
  cli_publish.py   PublishFlow: the publish run (credentials -> whoami -> source -> plan ->
                   refusals -> write) with injected collaborators; builds none of them
  cli_output.py    PlanPrinter (table, ownership, replace preview, footers), HandoverPrompt
  cli_errors.py    ErrorTranslator (every expected failure -> CliError sentence + Fix:)
  cli_commands.py  the reserved-word commands: SetupCommand (cpub setup: prompts, whoami
                   test, CredentialWriter), CheckReport + CheckCommand (cpub check),
                   UndoCommand (cpub undo: plan, typed count, --list)
  checks.py        EnvironmentCheck -> CheckResult facts (python, credentials, tls, auth, npx,
                   cpub); read-only
  errors.py        PublisherError base: CliError, MissingCredentials, SpaceNotFound, NotAFolder,
                   PlanRefused, HandoverRequired, HandoverDeclined, PlanExecutionError
  config.py        Credentials (frozen: base_url, email, token, sources; mask(), token_source),
                   CredentialLoader (env > --env-file > ./.env > ~/.config/..., chmod warning),
                   AppDirs (XDG config/cache folders; the only other environment reader)
  api.py           HttpTransport (urllib; keeps HTTPError.detail), ConfluenceClient(credentials,
                   transport): pages, attachments, ancestors, title index, whoami, space lookup,
                   cpub-managed marker
  docset.py        SourceDoc, DocSet (numbered docs + In Situ/, duplicate_titles()),
                   SingleNote (one .md file; title/filename; frontmatter title: and the
                   confluence: / confluence_parent: targets)
  pageref.py       PageRef.parse(text, base_url): page id from an id or URL (pure, no network)
  plan.py          PlanItem, Refusal, Plan (mode, space_info, refusals(), handover_items()),
                   Planner(client, converter): build(source, space_id, parent_id) for a set or
                   --under, build_replace(note, target_id, space_id) for --to — read-only
  executor.py      PlanExecutor(client).execute(...) / .undo(...) — the ONLY writer; RunReport
  runlog.py        RunLog (audit log, append-only JSONL), Recorder (the executor's log), Run
  undo.py          UndoPlanner (select the run, check versions; read-only), UndoPlan, UndoItem
  storage.py       FenceAwareExtractor (the one fence rule) -> TocExtractor, WikilinkExtractor,
                   CalloutExtractor; StorageConverter.convert(md) -> StorageDocument (.validate())
  anchors.py       Confluence heading ids (D36): heading_plain, heading_anchor, HeadingAnchors
  markdown_html.py MarkdownRenderer (.render(md), .inline(text)); vanilla HTML only
  mermaid.py       MermaidRenderer(img_dir, npx=...) (npx mmdc -> content-hash PNGs);
                   stdlib only, no package imports
docs/              GUIDE.md, KNOWN_ISSUES.md
template.yaml      Stage 1 contract (stays at the root)
```

**Dependency rule (no cycles; QA checks the imports):**

```
markdown_html, mermaid  <-  storage  <-  plan  <-  executor  <-  cli
markdown_html <- anchors <- storage (anchors imports nothing else of the package)
cli_output <- cli_errors <- cli (output/error wording never imports api, plan or config;
                                  cli_errors reads only docset's frontmatter key names)
cli_output, cli_errors, config, checks, api <- cli_commands <- cli (never the other way round)
cli_output, cli_errors, docset, pageref, errors <- cli_publish <- cli (cli_publish never imports cli)
docset <- plan; plan reads only constants from api; cli imports every module (composition root)
errors: imports nothing; config, api, docset import only errors
storage, anchors, markdown_html, mermaid: never import api — conversion stays pure and offline;
mermaid imports no package module (storage wires it into MarkdownRenderer)
```

One responsibility per module, none over ~650 lines (storage 655, markdown_html 585, cli_commands 519, cli 448, api 435, plan 419, config 385, cli_errors 320, cli_publish 251, errors 247, cli_output 222, docset 219, executor 198, runlog 180, checks 168, undo 108, anchors 91, pageref 84, mermaid 78). Lines stay at 100 characters or fewer.

**Composition.** `PublishCommand.from_args(args, out, **overrides)` is the only place that builds the real objects: `CredentialLoader().load()` → `ConfluenceClient(creds, HttpTransport())` → `DocSet.from_folder()` → `Planner(client, StorageConverter(prefix, img_dir, img_width)).build(...)` → `plan.refusals()` → `PlanExecutor(client).execute(...)`. No other module constructs a `ConfluenceClient`.

`★ Insight ─────────────────────────────────────`
- The split is deliberate: `markdown_html` produces vanilla HTML and knows nothing about Confluence; `storage` upgrades it to Confluence storage XHTML (code → `ac:structured-macro`, callouts → colored panels, `<img>` → `ac:image` attachments, `[[wikilinks]]` → `ac:link`). Keep Confluence-specific markup out of `markdown_html.py`.
- Because `storage` never imports `api`, every body can be built and validated with no credentials and no network — which is what lets the planner fail on a bad document before the first request, and keeps the conversion tests offline.
- Injection is what makes the safety guarantees testable: a test hands `ConfluenceClient` a fake transport (`tests/_fake_confluence.FakeConfluence`) and the real `Planner`/`PlanExecutor` run on top, recording every request — so "a refused plan writes nothing" is asserted on real code, not on stubs.
`─────────────────────────────────────────────────`

### Where a change goes

- **A new Markdown → Confluence transform** (a macro, a link form, an embed): `storage.py`, as a step in `StorageConverter.convert()` at an explicit position in the ordering below; a new pre-conversion extractor subclasses `FenceAwareExtractor` and adds only its own pattern (never its own fence logic). Generic, non-Confluence Markdown syntax goes in `MarkdownRenderer`. Neither may call the REST API: anything that needs Confluence data is looked up by the caller (the planner) and passed in.
- **A new safety rule or refusal:** `plan.py` (`Plan.refusals()` returns a `Refusal` with `message` and `fix`); its wording on screen is `ErrorTranslator` in `cli.py`. Anything that writes belongs in `PlanExecutor`, nowhere else.
- **A new REST call:** a method on `ConfluenceClient` in `api.py`, through its transport (keeps `HTTPError.detail`).
- **A new flag, prompt, message or output line:** flags in `ArgumentParserFactory` and the run's steps in `PublishCommand` (`cli.py`); printing and prompts in `PlanPrinter` / `HandoverPrompt` (`cli_output.py`); failures in `ErrorTranslator` (`cli_errors.py` — expected failures are `errors.CliError(sentence, fix, detail)`, never a bare `SystemExit` string).
- **A new collaborator** (e.g. a page-ref parser in 1b): its own class, constructed in `PublishCommand.from_args()` and passed in; never constructed inside another class.
- **Tests inject fakes through constructors** — `FakeConfluence` as the client's transport, `PublishCommand.from_args(args, out, client_factory=…, prompt=HandoverPrompt(out, ask=…, stdin=…))`, `CredentialLoader(environ=…, read_file=…)`. No `setattr` patching of package code.

### The transform ordering in `StorageConverter.convert()` is load-bearing

`StorageConverter.convert()` (in `storage.py`) is the heart of the conversion, and its step order is not arbitrary — several steps depend on running before/after others:

1. `TocExtractor` **before** wikilinks — `[[TOC]]` is a macro, not a page link.
2. `WikilinkExtractor` **before** conversion — the bulk renderer would otherwise *flatten* `[[links]]` to plain text. They are swapped for `xLINKNx` tokens and restored as `ac:link` markup at the end.
3. `CalloutExtractor` **before** conversion — Obsidian callouts become `xCALLOUTNx` placeholder paragraphs, converted to panel macros and swapped back in after.
4. Mermaid `<img>` → attachments, then autolink + bare-`&` escaping.
5. `_code_blocks_to_macro` runs **last**, because it emits `CDATA` sections that the ampersand-escaping step must not touch.
6. `StorageDocument.validate()` parses the final body as XML before anything is sent — malformed content fails locally rather than producing a broken Confluence page.

`★ Insight ─────────────────────────────────────`
- This is a token-masking strategy: anything that must survive HTML conversion or escaping is pulled out, replaced with an inert sentinel (`xLINKNx`, `xCALLOUTNx`, or `\x00N\x00` inside `MarkdownRenderer.inline`), then restored last. If you add a new transform, decide explicitly where it sits relative to escaping and CDATA generation.
- `MarkdownRenderer.inline()` uses the same idea at the inline level — it masks code spans and links *before* applying emphasis/escaping so stray `*` or `<` inside code can't create crossing/invalid tags.
- The old "shared-mutable-state gotcha" (module globals `_current_img_dir` / `_current_img_rel`) is gone: the image folder is now constructor state of `MermaidRenderer` / `StorageConverter`, so two conversions can never render into each other's folder.
`─────────────────────────────────────────────────`

### Publishing semantics

- **Plan first, write second (E0).** `PublishCommand` calls `Planner.build()` (read-only: converts + validates every body, matches titles, reads page info, ancestors and the marker), then `Plan.refusals()` (pure; returns *all* refusals), and only then `PlanExecutor.execute()`, which re-checks and is the **only** code that calls the client's write methods (create, update, upload, set marker). Any refusal aborts with zero writes and lists every offender. `--dry-run` is the same path stopped before the executor, so a passing dry-run means the real checks pass. A mid-run write failure raises `PlanExecutionError` whose report lists written vs not-written pages; re-running is safe.
- **Idempotency is by exact title match, fenced to the subtree.** A title match is an UPDATE target only if the page is a **strict descendant** of `--parent` (v2 ancestors); a match elsewhere in the space, the parent itself, an archived/trashed page, or a `--parent` in another space is refused. The `In Situ` container is fenced like every page (an existing one is never rewritten, as in v1.1).
- **Ownership marker `cpub-managed`** (a v2 content property, written in a *separate* call after the body — never inside the body, so storage bodies stay byte-identical to v1.1) records `{source, tool, since}`; source = prefixed title. Managed + same source → silent UPDATE; unmanaged or other source → **hand-over**: on a TTY the CLI lists the pages and asks `Take over and replace N page(s) you did not publish? Type the number N to confirm:`; non-TTY needs `--adopt` or refuses (never hangs). v1.1-published sets need this hand-over once on their first republish. The marker is an accident guard, **not access control**.
- **Audit log and undo (S0.5)**: every page write is one JSON line in `AppDirs().log_path` (`$XDG_STATE_HOME/confluence-publisher/log.jsonl`, dir 700, file 600) through `PlanExecutor`'s injected `log` (a `runlog.Recorder`): run id, kind, time, host, space, page id, title, action, versions, source — never a body, token or email; a failed log write warns and never aborts. `PublishFlow` gets `run_log` (composition: `PublishCommand.real_run_log`); **tests must inject `_fake_log.memory_log()`** wherever a real write can happen. `undo` (`cli_commands.UndoCommand`, reserved word) refuses a non-TTY first, picks the run (`UndoPlanner.select`: `nothing` / `unknown` / `undone` / `other-site` → `UndoError`) before any request, checks `whoami`, then `UndoPlanner.build` restores only pages still at `version_after` (others refused with their history link), lists CREATEs (never deleted), asks for the typed count, and `PlanExecutor.undo` restores via v1 version restore. `--list` reads only the log.
- **Never deletes, never moves (no `parentId` on update), never renames.** This is the core safety property — do not add deletion without an explicit opt-in flag (a `--prune` is listed as a deliberately-unimplemented future option).
- **CLI output contract:** the v1.1 `ACTION DIAGRAMS TITLE` table is byte-identical; ownership summary, hand-over list and refusals print around it, and a successful run ends with `Parent page: <url>`. Before planning, `PublishCommand` calls `client.whoami()` (one read-only GET `/rest/api/user/current`): Cloud serves a bad token anonymously, so this is the only reliable "bad credentials" signal — never infer it from a 404. Expected failures (`CliError`) print `Error: <one sentence>` + `Fix: <one line>` + indented raw detail (Confluence's `HTTPError.detail` is kept) and never a traceback; the API token is masked in every message.
- **Layout:** top-level `NN - Title.md` files become children of the parent; an `In Situ/` subfolder (matched case-insensitively via `DocSet.find_insitu_dir`) becomes an `In Situ` container page with the deep-dives beneath it.
- **Attachments** (rendered Mermaid PNGs) are content-hash-named; `ConfluenceClient.existing_attachment_names` lets the executor skip names already present, since re-uploading a duplicate filename returns HTTP 400. **They must be uploaded *before* the body that references them is saved:** Confluence links each `ri:attachment` at save time (`ri:version-at-save`), and an unlinked reference renders in view mode but shows "Preview not available" in the editor and is dropped on the user's next save. So UPDATE uploads then saves; CREATE (the page must exist to take attachments) creates, uploads, then saves the body again. `tests/test_publish_order.py` pins this order.
- **Pre-flight duplicate check:** `PublishCommand` (via `DocSet.duplicate_titles()`) rejects a doc set with two files resolving to the same title *before* contacting Confluence, naming the colliding files.
- REST uses the **v2 API** for pages/spaces/attachments-listing, but the **v1 endpoint** for attachment *upload* (multipart). `HttpTransport` stashes Confluence's real error body on `HTTPError.detail` so `ErrorTranslator` can print the actual reason (v2's bare "HTTP Error 400" otherwise hides it).

## Conventions worth knowing (from `template.yaml`)

These shape the Markdown the publisher consumes, so they matter when debugging "why didn't this render/link":

- Filenames are `NN - Title.md` with gap-free numbering; `00 - Index.md` is generated last.
- Cross-links to numbered docs use the **full** `[[NN - Title]]` form — a bare `[[Title]]` renders as unresolved in Obsidian. Only unnumbered In Situ docs are linked bare.
- H2 section headings are **unnumbered plain text**, because the literal heading text is the cross-link anchor.
- **Heading links keep their anchor (H3, D36)**: `[[#H]]` becomes a same-page `<ac:link ac:anchor="X">` (no `ri:page`), `[[P#H]]` adds `ri:page` by prefixed title. X is Confluence Cloud's own heading id, measured in the H3.0 spike: the heading's **shown** text (inline Markdown rendered, tags dropped), trimmed, every whitespace character → `-`, nothing else changed; repeats `.1`, `.2` across all levels (`anchors.HeadingAnchors`, pure). Same-note links resolve against `note_headings`, which lists exactly the body's `<hN>` (callout bodies included through `storage.callout_at`, the one callout rule; plain `> ##` quotes not; B3), leniently (case, spaces and punctuation ignored; a heading outside a callout wins); set mode passes `convert(md, headings_of)` so `[[NN - Title#H]]` finds that doc's real heading; otherwise X comes from the link text. A same-note link to a missing heading is S2.5 text in note mode (`#H` in the `links left as text` line) and kept as a link in set mode; `[[P#^block]]` stays a page link. Confluence links an anchor only to a page that exists when the body is saved, so a page written in a run (CREATE or UPDATE) that links a heading of a page created *later* in that run is saved once more after every write (D37: `Planner` sets `PlanItem.relink`, `PlanExecutor` re-saves with `link heading anchors`, version+1; one log line per page, with its last version; the plan table and `--max-updates` count do not change; plain forward links are unchanged, see `docs/KNOWN_ISSUES.md`). Bodies with no `#` link are byte-identical to 1.4.2. The rule is renderer behaviour, not a documented contract: re-check it live when Confluence changes.
- Mermaid labels must avoid `( ) : ; , # & " ' < > { } [ ]` unquoted (Obsidian's parser is strict); the `mermaid:` block in `template.yaml` has the full rules.

## Operational gotchas

- **macOS `ssl.SSLCertVerificationError: CERTIFICATE_VERIFY_FAILED` on publish.** Affects the **python.org** Python build, which ships a CA bundle that isn't installed by default (Homebrew's `python@3.12` works out of the box; Apple's `/usr/bin/python3` is 3.9 and no longer supported). Fix once by running the bundled `/Applications/Python 3.x/Install Certificates.command` (or `pip install --upgrade certifi` for that interpreter). Do **not** work around it by disabling certificate verification — that removes the security the check provides. See README troubleshooting and `docs/KNOWN_ISSUES.md`.
- **A `400` only ever surfaces on the real publish, never on `--dry-run`** (the dry-run reads but never exercises the write path). A duplicate title elsewhere in the space and a `--parent` in another space are now refused at plan time; a `400` that remains usually means `--parent` is a folder.
- **Tests must not pick up a real `./.env`.** The repo root may hold a developer's `.env`; CLI tests give the real `CredentialLoader` a fake `environ` and an in-memory `read_file` (and fakes for the client, planner and executor) through `PublishCommand.from_args(...)` instead of reading files.

## Do not touch the project `.gitignore` for tooling/personal files

Editor, MCP, and AgentsRoom artifacts — `.mcp.json`, `.agentsroom/handoff-*`, `.claude/settings.local.json`, and the `.agentsroom/sessions/` cache — are **already ignored globally** via `~/.config/git/ignore`, and `.agentsroom/` also carries its own `.gitignore`. Do **not** add these paths to this repo's `.gitignore`; the committed `.gitignore` is for the *project's* build/runtime artifacts only (`.env`, `__pycache__/`, `.img/`, IDE dirs, `.DS_Store`). Personal/tooling ignores belong in the user's global excludes, not in a file shared with everyone who clones. If you notice one of these paths showing as untracked, it is already ignored — leave it alone rather than "fixing" it in the project `.gitignore`.
