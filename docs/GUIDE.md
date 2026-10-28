# How to generate and publish documentation for your repo

This guide shows you how to turn **any code repository** into a clean, numbered documentation set using `template.yaml` and your own Claude — and then, optionally, publish that set to Confluence Cloud. No prior knowledge is needed; just follow the steps below.

The two stages are independent: **Stage 1** generates the docs (you review them in Obsidian), and **Stage 2** publishes them. The Markdown is the source of truth — you can edit it and re-publish at any time.

Estimated time: about 10 to 30 minutes to generate, a few minutes to publish.

---

## What it does

You point Claude at your repository and at `template.yaml`. Claude reads your code and writes a set of Markdown documents (Welcome, Architecture Overview, API Reference, and so on) into a folder you choose. You open that folder in Obsidian and read it like a wiki, with working links between pages and rendered diagrams.

The document set adapts to your repo automatically:
- Every repo gets the same **core documents** (the "universal" set).
- Documents that do not apply are skipped. A repo with no database gets no "Database Schema" page; a repo with no user interface gets no frontend pages.
- Each document is **re-framed** for what your repo is. "API Reference" means HTTP endpoints for a web service, exported functions for a library, and commands for a command-line tool.
- Your repo's own special subsystems get extra deep-dive documents (the "In Situ" set), discovered from your code.

---

## What you need

| Thing | Why | How to get it |
| ----- | --- | ------------- |
| **Claude Code** | This is the version of Claude that can read your files and write documents. Plain chat on the website cannot read a repository directly. | Install from <https://claude.com/claude-code>, then sign in. |
| **A repository to document** | The code Claude will read. Have it cloned locally. | Any local folder with code. |
| **`template.yaml`** | The recipe Claude follows. | Copy [`template.yaml`](../template.yaml) from the root of this repository to somewhere you can find it (for example, into your repo, or your home folder). |
| **Obsidian** | The app you read the result in. The documents use Obsidian-style links and diagrams. | Free download from <https://obsidian.md>. |

> [!tip] You do not need a database, a frontend, or any particular language.
> The template works for a backend service, a library, a command-line tool, a frontend app, a data pipeline, or infrastructure code, in any programming language.

---

## Stage 1 — Generate the docs

### Step 1 — Put `template.yaml` where you can reach it
Copy `template.yaml` into a convenient place. The simplest option is to drop it inside the repo you want to document (for example at the repo root). You can delete it afterward.

### Step 2 — Decide where the documents will go
Pick an **empty folder** for the output. It can be anywhere, for example `~/Desktop/my-repo-docs`. This folder will become your Obsidian vault. Keeping it separate from the code keeps things tidy.

### Step 3 — Open Claude Code in your repo
Open a terminal, change into your repository folder, and start Claude Code:

```bash
cd /path/to/your/repo
claude
```

### Step 4 — Paste the generation prompt
Copy the entire block in the [The generation prompt](#the-generation-prompt) section below, fill in the two paths at the top, and paste it into Claude Code. Claude will work through your repo and write the documents into your output folder. It will tell you when it is done.

### Step 5 — Review what it wrote
Claude generates from your **current code**, so the facts should be accurate, but it is still machine-written. Skim the documents. Fix anything that looks off — these are normal Markdown files.

### Step 6 — Open the folder in Obsidian
Open Obsidian, choose **Open folder as vault**, and select your output folder. The pages, links, and diagrams will render. That is your documentation.

---

## The generation prompt

Fill in the two paths on the first two lines, then paste the whole thing into Claude Code.

```
You are going to generate a documentation set for THIS repository by following a spec file.

SPEC FILE:    <path to template.yaml, e.g. ./template.yaml>
OUTPUT FOLDER: <path to your empty output folder, e.g. /Users/me/Desktop/my-repo-docs>

Do this in order:

1. CLASSIFY the repo from its code:
   - Pick ONE archetype from the spec's `archetype` block (service, library, cli,
     frontend, mobile, data-pipeline, or infra), based on what this repo mainly is.
   - Evaluate the `detect` feature-gates: does it have a frontend? does it own a
     database schema? Use the spec's signals. If a database signal is only "weak"
     (a bare driver, a stray .sql file), do NOT assume a schema doc — ask me.
   - Tell me the archetype and gate results before generating, in one short line.

2. PLAN the set:
   - Take the universal sections from the spec whose `when:` condition passes for
     this repo (skip frontend docs if there is no frontend, skip the database doc
     if there is no owned schema).
   - Sort them by their `order` field, then number them gap-free 01, 02, 03 ...
     (the skipped ones must NOT leave a hole in the numbering).
   - Then DISCOVER "In Situ" docs: scan the repo for any significant, cohesive
     subsystem a newcomer could not understand from the universal docs alone, and
     propose one deep-dive doc per subsystem, titled with the subsystem's real
     name. Zero is a valid answer. These are unnumbered.

3. GENERATE each document into the OUTPUT FOLDER, following the spec exactly:
   - Read the matching `sections` entry in the spec for each doc and follow its
     `intent`, adapted to this repo's archetype.
   - Follow the spec's `meta.conventions`: open with YAML frontmatter (title), then
     one H1 repeating "NN - Title"; H2 section headings are UNNUMBERED; use Obsidian
     callouts; emit native ```mermaid``` blocks where useful; write in FULL FORMS,
     never contractions ("it is", not "it's").
   - Ground every fact in code you actually read. Do not invent versions, ports,
     paths, or commands. If unsure, say so in the doc.
   - Put numbered universal docs at the top level of the output folder. Put In Situ
     docs (unnumbered) in an "In Situ" subfolder.

4. CROSS-LINKS:
   - Link to a numbered doc with its FULL name: [[03 - Domain Concepts]], never the
     bare [[Domain Concepts]].
   - Link to an In Situ doc by its bare title: [[Some Subsystem]].
   - Only use #heading anchors for links inside the SAME document, to headings you
     wrote yourself (use the exact heading text).

5. FINALIZE:
   - Generate "00 - Index.md" LAST, listing every document with a one-line summary.
   - Re-check every [[link]]: fix any bare-title link to a numbered doc into its
     full [[NN - Title]] form, and confirm every link points at a file that exists.
   - Report: the archetype, the list of documents you created, and any links or
     facts you were unsure about.
```

> [!tip] Large repositories
> If your repo is very large, Claude may generate the documents one at a time and take a while. That is fine — let it work. You can also ask it to "generate only docs 01 to 04 first" and continue later.

---

## What you get

A folder like this (the exact list depends on your repo):

```
my-repo-docs/
├── 00 - Index.md
├── 01 - Welcome and Quick Start.md
├── 02 - Architecture Overview.md
├── 03 - Domain Concepts.md
├── 04 - Code Architecture.md
├── 05 - API Reference.md
├── ... (more, numbered with no gaps)
└── In Situ/
    ├── <Your Subsystem>.md
    └── <Another Subsystem>.md
```

- **Numbered docs** are the core set; the numbers are always gap-free.
- **In Situ docs** are the deep-dives specific to your repo. There may be several, or none.

---

## Tips and troubleshooting

- **A link shows "not created yet" in Obsidian.** A link to a numbered doc must use the full `[[NN - Title]]` form. Ask Claude to "normalize all bare-title links to their NN - Title form." (The prompt already asks for this, but it is the most common slip.)
- **A diagram does not render.** Obsidian is strict about characters inside Mermaid labels. Ask Claude to "fix the Mermaid diagram in <file> so it renders in Obsidian" — the spec's `mermaid` block has the rules it will follow.
- **The archetype is wrong** (for example it called your component library a "frontend app"). Tell Claude the correct archetype and ask it to regenerate. The spec expects a human to confirm this.
- **You want to regenerate later** after the code changes. Generate into a fresh folder and compare, or ask Claude to regenerate specific documents. Do not let it blindly overwrite docs you have hand-edited; ask for a diff first.
- **Keep the Markdown as the source of truth.** Edit the generated files directly when something is wrong; they are plain Markdown.

---

## Stage 2 — Publish to Confluence

Once you have reviewed the generated docs, you can publish them to Confluence Cloud as a page tree. This stage is separate from generation: you can publish, edit the Markdown later, and re-publish, as often as you like. (To publish **one note** instead of a whole folder, see [Publish a single note](#publish-a-single-note) below.)

### What you need to publish
- **The `cpub` command.** Install it once with [`uv`](https://docs.astral.sh/uv/) (it brings a suitable Python by itself):

  ```bash
  uv tool install git+https://github.com/RPacheco-TC/obx-confluence-obsidian-publisher
  ```

  or with `pipx install git+https://github.com/RPacheco-TC/obx-confluence-obsidian-publisher`. Check it with `cpub --version`; update later with `uv tool upgrade obx-confluence-obsidian-publisher`. (Prefer not to install? From a clone of this repo, `python3.12 confluence_publish.py` does exactly the same; it needs **Python 3.12+** — on macOS the built-in `python3` is 3.9, so `brew install python@3.12`.)
- **Node.js / `npx`** — only if your docs contain Mermaid diagrams, which get rendered to images. Optional otherwise.
- A **Confluence Cloud account** and an **API token** — create one at <https://id.atlassian.com/manage-profile/security/api-tokens>.

### Step 1 — Set your credentials
With `cpub` installed, run:

```bash
cpub setup
```

It asks for your site URL (`https://your-company.atlassian.net` is enough), your email and your API token (hidden while you type), signs in once to test them, and saves them to `~/.config/confluence-publisher/.env`, readable only by you, so every folder can publish. A token Confluence rejects is asked for again and never saved; an existing file is only replaced if you type `yes`.

Then `cpub check` shows what works, one line each: Python, where the credentials come from, the connection (and its TLS certificate), the sign-in, `npx` for diagrams, and the full path of `cpub`. It never writes anything.

Prefer to write the file yourself? In a clone you can also copy `.env.example` to `.env` and fill in your values. The publisher reads it by itself, so there is nothing to load into your shell:

```bash
cp .env.example .env       # edit .env: base URL, email, API token
chmod 600 .env             # only you can read it — never commit .env
```

The base URL for Confluence Cloud ends in `/wiki`, for example `https://your-company.atlassian.net/wiki`.

> [!tip] One credentials file for every repo
> Put the same three lines in `~/.config/confluence-publisher/.env` instead, and every repo and folder can publish without its own `.env`. Each variable is looked up in this order, first hit wins: a real environment variable, then `--env-file PATH`, then `./.env` in the folder you run from, then `~/.config/confluence-publisher/.env`. If something is missing, the error lists every place it looked.

### Step 2 — Create a parent PAGE and copy its URL

> [!important] You must publish under a PAGE, not a folder
> The tool places all the docs as **children of one Confluence page** that you choose. You have to **create (or pick) a real page** to act as the parent and pass its URL (or id) with `--parent`. A Confluence **folder cannot be the target** — the API does not reliably support publishing under folders. You may still keep your parent page *inside* a folder for tidiness; the tool addresses the page, and the folder is just a wrapper around it.

You give the publisher one thing: that **parent page**. Everything is published as children of that one page, and nothing else in the space is ever touched.

**Choose or create the parent page.** Decide where the docs should live, and create one page to act as the container — for example `Service X Documentation`. You need permission to add pages in that space, so if it is a shared space, make sure you can create pages there (and it is polite to let the space owner know). Then open the page and **copy its URL from the browser's address bar**:

```
https://your-company.atlassian.net/wiki/spaces/DOCS/pages/123456789/Service+X+Documentation
```

Paste that whole URL as the `--parent` value (the number `123456789` on its own works too, as do the `…/pages/edit-v2/<id>` and `…/viewpage.action?pageId=<id>` forms). The tool works out the space from the page, so you don't need `--space`. If you pass `--space` anyway (the part after `/spaces/`, e.g. `DOCS`, or `~yourname` for a personal space), it checks that the page really is in that space and stops if not.

The tool refuses a **folder** URL (`…/folder/…`), a short link (`…/wiki/x/…`), and a URL from a different Confluence site than the one in your credentials, before sending anything.

> [!tip] Test in your personal space first
> Before publishing to a shared space, do a trial run in your **personal space** (its key starts with `~`) or a throwaway space — it is private and risk-free. When it looks right, repoint at the real parent by simply changing `--parent`.

### Step 3 — Choose a unique prefix (per repo)

> [!important] Confluence page titles must be unique **within a space**
> You cannot have two pages with the same title in the same space — not even two. So if you publish more than one repo's docs into a **shared** space, generic titles like `01 - Welcome and Quick Start` or `In Situ` will collide. The second publish is then refused before anything is written, because the same-titled page lives in the other repo's tree.

The fix is `--prefix`: a short unique identifier (usually the repo name) that the publisher prepends to **every** page title. Pick a distinct value per repo:

| Repo | `--prefix` | Resulting page titles |
| ---- | ---------- | --------------------- |
| langflow | `langflow` | `langflow - 01 - Welcome…`, `langflow - In Situ`, … |
| my-api   | `my-api`   | `my-api - 01 - Welcome…`, `my-api - In Situ`, … |

Notes:
- The prefix is applied at **publish time only** — your Markdown files and Obsidian vault stay clean and unprefixed. The Markdown remains the source of truth.
- **Cross-page `[[wikilinks]]` keep working.** At publish time the link *target* is prefixed to match the published page title (the visible link text stays the natural name), so links between docs — including links to `In Situ` pages — resolve correctly in Confluence. Because the files themselves are not renamed, Obsidian's own navigation is unaffected.
- Re-publishing with the **same** prefix updates the same pages in place (the create-or-update match uses the prefixed title), so it stays idempotent.
- If you change the prefix later, the next publish creates a **new** set of pages under the new titles rather than renaming the old ones.
- Publishing into your **own personal space** (`~yourname`)? A prefix is optional there, since nothing else shares that space — but it is harmless to use one.

### Step 4 — Dry-run, then publish
```bash
# Preview the plan — writes nothing:
cpub "/path/to/my-repo-docs" --parent "<parent page URL>" --prefix langflow --dry-run

# Publish for real:
cpub "/path/to/my-repo-docs" --parent "<parent page URL>" --prefix langflow
```

Quote the URL so your shell doesn't trip over `?` or `&` in it. You will see a per-page `CREATE` / `UPDATE` plan (showing the actual prefixed titles), how many diagrams each page carries, and who owns the pages it would update. After a real publish it ends with a link to the parent page. Re-running updates the same pages in place (matched by title, below the parent only), so it is safe to run again after editing the Markdown. The dry-run runs exactly the same checks as the real publish.

> [!note] Collisions are caught before anything is written
> If two source files in **your own folder** resolve to the same title, the publisher stops *before* contacting Confluence and tells you which file to rename. If a title already exists **elsewhere in the space** (outside your parent page), the whole run is refused with nothing written and every clashing page listed — `--prefix` is the way to avoid it.

### Step 5 — The one-time hand-over (first publish, or upgrading from v1.1)

The publisher only replaces pages it published itself: it marks every page it creates with an invisible Confluence content property (`cpub-managed`) that records the source. When the plan wants to update a page **without** that mark (written by hand, or published by v1.1 of this tool, which did not mark pages), it lists those pages with their version and last-edit date and asks:

```
Take over and replace 18 page(s) you did not publish? Type the number 18 to confirm:
```

Type the number to go ahead; anything else writes nothing. The pages are marked as they are written, so from the next run on there is no question. In a non-interactive run (a script, a pipe, an editor hotkey) there is no prompt: the run refuses unless you add `--adopt`, which hands over the listed pages for that run only. There is no `--yes`.

**Already published with v1.1?** Your first republish shows every page as "not published by this tool" and asks once. A set with more than 25 pages also needs `--max-updates N` for that run, because one run may update at most 25 existing pages by default (new pages don't count).

### What lands in Confluence
- The numbered docs become **child pages** of your parent page; the `In Situ/` folder becomes an `In Situ` page with the deep-dives beneath it.
- **Mermaid diagrams** are rendered to images and embedded; **`[[wikilinks]]`** become real Confluence page links; **code blocks** and **callouts** become code macros and colored panels.

> [!tip] Why this is safe
> Every run is planned and checked before the first write; one refusal means nothing is written. The publisher only updates pages **below** the parent you pass that it published itself (or that you just handed over), updates at most 25 existing pages per run unless you raise `--max-updates`, validates every page before sending it, and never deletes, moves or renames anything. The marker is an accident guard, not access control: Confluence permissions remain the real boundary.

---

## Publish a single note

You can also publish **one Obsidian note** to Confluence, without a doc set. Point the command at the `.md` file and say where it goes:

| You want to… | Command |
| --- | --- |
| Replace the content of an existing page | `cpub "My Note.md" --to "<page URL>"` |
| Create the note as a new child page | `cpub "My Note.md" --under "<parent page URL>"` |

Add `--dry-run` to see what would happen without writing anything. The credentials are the same as in Step 1, and `--space` is not needed: it is taken from the page.

### `--to`: replace a page

`--to` replaces exactly the page you name; no title search is involved. The page keeps its **title**, its place in the tree and its space (pass `--title "New title"` to rename it). Before writing, the tool shows a **replace preview**:

- the page title and space,
- its current version and when it was last edited,
- the size of the old content and of the new content.

`--dry-run` stops after the preview. The new version's comment reads `Published from Obsidian: My Note.md`. After the write you get two links: the page itself and its **page history**, where the previous version is one click away if you need it back.

### `--under`: create a child page

`--under` creates a new page below the one you name. Its title is `--title` if you give one, else the `title:` in the note's frontmatter, else the file name (`My Note`). Re-running the same command finds that child and updates it. If a page with the same title already exists **elsewhere** in the space, the run is refused (pick another `--title`, or use `--to` on that page if it is the one you mean).

### The safety prompt

The first time you publish a note to a page this tool did not create from that note — a page written by hand, or one published from a different note — the tool lists it and asks you to confirm:

```
Take over and replace 1 page(s) you did not publish? Type the number 1 to confirm:
```

Type the number to go ahead; anything else writes nothing. From then on the page is marked as published from that note, so republishing it never asks again. In a script or an editor hotkey there is no prompt: the run refuses unless you add `--adopt`. The space's **home page** can never be replaced with `--to`.

### Let the note remember its page

Instead of typing `--to` every time, write the target into the note's frontmatter (the `---` block at the top of the note, the one Obsidian shows as **Properties**):

```yaml
---
confluence: https://your-company.atlassian.net/wiki/spaces/ENG/pages/123456789
---
```

From then on `cpub "My Note.md"` with no flag publishes the note to that page. Use `confluence_parent:` instead to mean `--under` (create, then update, a child of that page). Either key takes a page URL or id, checked exactly like the flag.

- A flag on the command line **wins** over the frontmatter, so you can still send a note somewhere else for one run.
- A note with **both** keys is refused: keep the one you mean.
- The tool **never edits your note**. After a publish made with `--to` or `--under`, on a note that has neither key yet, it prints the line to paste:

  ```
  To publish this note again without flags, paste this line into its frontmatter
  (the --- block at the top of the note; add one if it has none):
    confluence: https://your-company.atlassian.net/wiki/pages/viewpage.action?pageId=123456789
  ```

### Good to know

- **No title prefix** is added in note mode unless you pass `--prefix`.
- **Diagrams** are rendered into your user cache folder (`~/.cache/confluence-publisher/img`), not next to the note, so nothing new appears in your vault.
- **Links to other notes** (`[[Other note]]`) become page links by title when a page with that title exists in the space. A link to a note that is not in Confluence yet is published as its text, in italics, and the run lists it once (`1 link left as text: Other note`, also on `--dry-run`). Publish that note, then this one again, to turn it into a link.

## Publish the current note from Obsidian

With the target in the frontmatter, one command (or one hotkey) publishes the note you are looking at. This uses **[cpub for Confluence](https://github.com/RPacheco-TC/confluence-publisher-obsidian)**, our own Obsidian plugin (desktop only). It runs `cpub` for you: it never reads, stores or sends your Confluence credentials and makes no network request itself.

1. **Install `cpub` 1.4.0 or newer** and save your credentials (`cpub setup`), as above. Keep them in `~/.config/confluence-publisher/.env`: Obsidian runs `cpub` from the vault folder, so a `./.env` in your project folder is not found from there.

2. **Publish the note once from a terminal**, so its page is marked as published from that note:

   ```bash
   cpub "/path/to/vault/My Note.md" --to "<page URL>"
   ```

   Type the page count when asked (the hand-over), then paste the `confluence:` line it prints into the note's frontmatter.

3. **Install the plugin.** Once it is listed in Obsidian's community store: **Settings → Community plugins → Browse**, search for **cpub for Confluence**, install and enable it. Until then, install it by hand: download `main.js`, `manifest.json` and `styles.css` from its [latest release](https://github.com/RPacheco-TC/confluence-publisher-obsidian/releases/latest), put them in `<vault>/.obsidian/plugins/cpub/` (create the folder), then enable **cpub for Confluence** under **Settings → Community plugins** (you may need to reload the list or restart Obsidian).

   Installed 0.1 or 0.2 by hand? The plugin's id changed to `cpub` in 0.3.0, so Obsidian sees a new plugin: remove the old `<vault>/.obsidian/plugins/confluence-publisher/` folder, install into `plugins/cpub/` as above, then bind your hotkey again and re-enter the **cpub path** if you had set one (hotkeys and settings belong to the old id).

4. Run **Check cpub setup** from the command palette. The plugin finds `cpub` by itself in `~/.local/bin` (uv, pipx), `/opt/homebrew/bin` or `/usr/local/bin`; if yours is elsewhere, set **cpub path** in the plugin's settings (`cpub check` in a terminal prints the full path). If `cpub` is missing or older than 1.4.0, the plugin says so and shows the install line.

5. Optional: **Settings → Hotkeys**, search for **cpub for Confluence** and bind a key to **Publish current note to Confluence**.

The plugin adds three commands (and a ribbon button for Publish, which you can turn off):

| Command | Runs |
|---|---|
| Publish current note to Confluence | `cpub <note>` |
| Preview publish (dry run) | `cpub <note> --dry-run` |
| Check cpub setup | `cpub check` |

It saves the note first, then shows the result as a notice: the page link on success, or `cpub`'s `Error:` and `Fix:` lines; **Show full output** opens everything `cpub` printed. A run that takes longer than the timeout (setting, 120 seconds by default) is stopped and reported.

- **The plugin never takes a page over.** It never passes `--adopt`: nobody can type the confirmation from inside Obsidian, so a page that this tool did not publish from this note is refused, with nothing written. It can only update pages you already handed over in step 2. If you see the hand-over error, run the command from a terminal once.
- **Diagrams need `npx`** (Node.js). The plugin adds `~/.local/bin`, `/opt/homebrew/bin` and `/usr/local/bin` to the `PATH` it gives `cpub`, and `cpub` also looks for `npx` in the last two. If Node.js is installed elsewhere (nvm, for example), start Obsidian from a terminal where `npx` works, or install Node.js with Homebrew. If no `npx` is found, a note with Mermaid diagrams is **refused** rather than published with its diagrams turned into code: `Error: Diagrams in My Note.md cannot be rendered (npx not found).`
- **Undo is not in the plugin**: `cpub undo` always asks for a typed confirmation, so run it from a terminal.

The *Shell commands* plugin recipe that this guide described before no longer works with current Obsidian; see [KNOWN_ISSUES](KNOWN_ISSUES.md#the-shell-commands-plugin-does-not-open-its-settings-on-obsidian-114).

## Undo a publish

Published something you did not mean to? From a terminal:

```bash
cpub undo
```

It takes the last publish run from the tool's local log, shows what it would do, and asks you to type the page count:

```
Undo run 20261028-101500-3f9a2c (2026-10-28T10:15:00Z, your-company.atlassian.net):

Restore (1):
  - My Note  version 8 -> back to 7

Restore 1 page to the version before the run? Type the number 1 to confirm:
```

- Every page the run updated goes back to the version before it, as a **new** version: the page history keeps everything.
- A page someone edited after your publish is **left alone** and listed with its history link; restore it there by hand if you need to.
- Pages the run **created** are only listed: undo never deletes anything.
- `cpub undo --list` shows the last 10 runs; `cpub undo --run <id>` undoes an older one. A run can be undone once.
- Undo always asks, so it does not run from the Obsidian plugin: use a terminal.

---

## In one sentence

**Generate:** install Claude Code and Obsidian, copy `template.yaml` next to your repo, paste the generation prompt into Claude Code with your two paths filled in, and review the result in Obsidian. **Publish:** set your three Confluence credentials in `.env` (or `~/.config/confluence-publisher/.env`), install `cpub` and run `cpub <docs-folder> --parent <page URL> --prefix <repo-name>` for a doc set, or `cpub <note.md> --to <page URL>` for one note (try `--dry-run` first; the `--prefix` keeps each repo's titles unique in a shared space).
