# confluence-docs-publisher

Turn any code repository into a clean, numbered documentation set, then publish it to Confluence Cloud — with rendered diagrams and working cross-page links.

There are two stages, and they are independent:

1. **Generate** — point your own Claude at a repo and a spec (`template.yaml`); it writes a numbered Markdown doc set you review in Obsidian.
2. **Publish** — `confluence_publish.py` uploads that doc set to Confluence Cloud as a page tree under one parent page, idempotently and safely. It also publishes **a single note** to one page.

The Markdown is the source of truth; Confluence is a derived view you can re-publish at any time.

### Quick start: install `cpub`, publish one note

```bash
uv tool install git+https://github.com/RPacheco-TC/obx-confluence-obsidian-publisher
```

(or `pipx install git+https://github.com/RPacheco-TC/obx-confluence-obsidian-publisher`). That gives you the `cpub` command in any folder; `uv` also provides a suitable Python (3.12+) by itself. Then save your credentials once, for every repo and vault:

```bash
mkdir -p ~/.config/confluence-publisher
cat > ~/.config/confluence-publisher/.env <<'EOF'
CONFLUENCE_BASE_URL=https://your-company.atlassian.net/wiki
CONFLUENCE_EMAIL=you@your-company.com
CONFLUENCE_API_TOKEN=your-api-token
EOF
chmod 600 ~/.config/confluence-publisher/.env
```

and publish:

```bash
cpub note.md --to https://your-company.atlassian.net/wiki/spaces/ENG/pages/123456789 --dry-run
cpub note.md --to https://your-company.atlassian.net/wiki/spaces/ENG/pages/123456789
```

Create the token at <https://id.atlassian.com/manage-profile/security/api-tokens>. `--to` replaces that page's content (its title is kept); use `--under <page>` instead to create the note as a new child page. Paste the page URL straight from your browser — no space key or id needed. The first publish to a page you did not create with this tool asks you to confirm the hand-over once (see [Safety](#safety)).

Update later with `uv tool upgrade obx-confluence-obsidian-publisher` (or `pipx upgrade …`); `cpub --version` shows what you have. `confluence-publish` is the same command under a longer name. Working from a clone instead? `python3.12 confluence_publish.py …` and `python3.12 -m confluence_publisher …` behave exactly like `cpub …`.

---

## What you need

| Tool | For | Notes |
| ---- | --- | ----- |
| **`uv`** (or `pipx`) | Installing `cpub` | <https://docs.astral.sh/uv/>; `uv` brings its own Python 3.12+. Running from a clone instead needs **Python 3.12+** (standard library only, nothing to `pip install`); macOS's built-in `python3` is 3.9 — `brew install python@3.12` and run `python3.12`. |
| **Node.js + `npx`** | Diagrams | Used to render Mermaid diagrams to PNG (`@mermaid-js/mermaid-cli`, fetched on first use). Optional — without it, diagrams fall back to a code block. |
| **Claude Code** | Generating | Only needed for stage 1. See [`docs/GUIDE.md`](docs/GUIDE.md). |
| **Obsidian** | Reading/editing | The generated docs use Obsidian wikilinks and Mermaid. Free from <https://obsidian.md>. |
| **A Confluence Cloud account + API token** | Publishing | Token: <https://id.atlassian.com/manage-profile/security/api-tokens> |

---

## Repository contents

| File | What it is |
| ---- | ---------- |
| `template.yaml` | The doc-set spec the generator follows (universal sections + per-repo "In Situ" deep-dives, adapted to the repo's archetype). |
| `docs/GUIDE.md` | Step-by-step walkthrough: generate docs with your own Claude, then publish them. |
| `docs/KNOWN_ISSUES.md` | Known limitations, their causes and workarounds. |
| `pyproject.toml` | Packaging (no dependencies): `uv tool install` / `pipx install` give the `cpub` and `confluence-publish` commands. |
| `confluence_publish.py` | Entry point when running from a clone: `python3.12 confluence_publish.py …`. A thin wrapper around the package below. |
| `confluence_publisher/` | The publisher itself, standard library only: command line, credentials, Confluence REST client, the safety plan, and the Markdown → Confluence conversion. Also runs as `python3.12 -m confluence_publisher …`. |
| `tests/` | Offline test suite (`python3.12 -B -m unittest discover -s tests`). |
| `.env.example` | Template for the three credentials (copy to `.env`). |

---

## Stage 1 — Generate the docs

Full instructions are in [`docs/GUIDE.md`](docs/GUIDE.md). In short: open Claude Code in your repo, paste the generation prompt from the guide (with `template.yaml`), and it writes a folder like:

```
my-repo-docs/
├── 00 - Index.md
├── 01 - Welcome and Quick Start.md
├── 02 - Architecture Overview.md
├── ...
└── In Situ/
    └── <repo-specific deep-dives>.md
```

Review and edit the Markdown before publishing. It is the source of truth.

---

## Stage 2 — Publish to Confluence

### 0. Install `cpub`

```bash
uv tool install git+https://github.com/RPacheco-TC/obx-confluence-obsidian-publisher
```

(or `pipx install git+https://github.com/RPacheco-TC/obx-confluence-obsidian-publisher`). The examples below use `cpub`; from a clone of this repo, `python3.12 confluence_publish.py` works exactly the same.

### 1. Set your credentials

With `cpub` installed, put the three values in `~/.config/confluence-publisher/.env` (see the [quick start](#quick-start-install-cpub-publish-one-note)) so every folder can publish. In a clone you can also use a local `.env`:

```bash
cp .env.example .env      # then edit .env with your base URL, email, and token
chmod 600 .env            # only you can read it (never commit .env)
```

That's it: the publisher finds the credentials itself. No `set -a; . ./.env; set +a` is needed any more (it still works). The base URL for Confluence Cloud ends in `/wiki`, for example `https://your-company.atlassian.net/wiki`.

Each of the three variables (`CONFLUENCE_BASE_URL`, `CONFLUENCE_EMAIL`, `CONFLUENCE_API_TOKEN`) is looked up in this order, and the first place that has it wins:

1. a real environment variable (`export …`, or the old `set -a` workflow);
2. the file given with `--env-file PATH` (an error if that file does not exist);
3. `./.env` in the folder you run the command from;
4. `~/.config/confluence-publisher/.env` (or `$XDG_CONFIG_HOME/confluence-publisher/.env`) — one file for every repo.

The files use plain `KEY=VALUE` lines as in `.env.example` (`export KEY=VALUE`, `#` comments and quoted values are fine; nothing is executed). If the file that holds the token can be read by other users, the tool prints a one-line warning suggesting `chmod 600 <file>` and carries on. If a credential is still missing, the error names it and lists every place it looked.

### 2. Choose the parent page

The tool publishes everything under **one parent page** you specify, and never touches anything outside it. You must create a **page** (not a folder) to hold the docs; everything is published as children of it (you need permission to add pages there). You can keep that page inside a Confluence folder for tidiness; the tool still targets the page.

Every page argument (`--parent`, `--under`, `--to`) takes either the page **URL** copied from your browser or its numeric **id**:

```
https://your-company.atlassian.net/wiki/spaces/DOCS/pages/123456789/Service+X+Documentation
https://your-company.atlassian.net/wiki/spaces/DOCS/pages/edit-v2/123456789
https://your-company.atlassian.net/wiki/pages/viewpage.action?pageId=123456789
123456789
```

A folder URL (`…/folder/…`), a short link (`…/wiki/x/…`), or a URL on another Confluence site is refused before anything is sent, with the reason.

**`--space` is optional**: the space is taken from the page itself. If you do pass it (the part after `/spaces/`, e.g. `DOCS`, or `~yourname` for a personal space), the run stops if the page lives in a different space.

Tip: trial it in your **personal space** first, then repoint at the team space by changing `--parent`.

### 3. Pick a unique prefix (when sharing a space)

**Confluence page titles must be unique within a space** — you cannot have two pages with the same title in one space. If you publish more than one repo's docs into the same (shared) space, generic titles like `01 - Welcome and Quick Start` or `In Situ` collide: the second publish is refused before anything is written ("already exists outside <parent> — not touching it"), because the same-titled page belongs to the other repo's tree.

Use `--prefix <id>` (typically the repo name) to namespace every page title. It is applied **at publish time only** — your Markdown stays clean — and re-publishing with the same prefix updates the same pages, so it remains idempotent.

```
--prefix langflow   →   "langflow - 01 - Welcome and Quick Start", "langflow - In Situ", …
```

A prefix is optional in your own personal space (nothing else shares it) but recommended everywhere else.

Your Obsidian `[[wikilinks]]` keep working both ways: the files keep their original names (so Obsidian navigation is unchanged), and at publish time each link's **target** is prefixed to match the published page title — so cross-page links in Confluence resolve to the prefixed pages, while the visible link text stays the natural name.

### 4. Dry-run, then publish

```bash
# See exactly what would happen — writes nothing:
cpub my-repo-docs --parent 123456789 --prefix langflow --dry-run

# Publish for real:
cpub my-repo-docs --parent 123456789 --prefix langflow
```

(`--under` works the same as `--parent` for a folder, and the v1.1 form with `--space DOCS` still works.)

The tool prints a per-page plan (`CREATE` / `UPDATE`, with the actual prefixed titles) and how many diagrams each page carries, then who owns each page (see [Safety](#safety)) and, after a real publish, a link to the parent page. Re-running updates the same pages in place (matched by title, inside the parent only), so it is safe to run repeatedly. The dry-run runs the same checks as the real run: if the dry-run passes, the real run will not be refused.

Optional: `--max-updates N` raises the cap on how many existing pages one run may update (default `25`, see [Safety](#safety)).

Optional: `--img-width <px>` sets a display width on every Mermaid diagram (e.g. `--img-width 700`), so diagrams rarely need resizing in the Confluence editor (see [Troubleshooting](#troubleshooting)). Without it, diagrams keep their natural size and the output is unchanged.

### Publish a single note

Point the command at one `.md` file instead of a folder, and say where it goes:

```bash
# Replace the content of an existing page (its title, place and space stay the same):
cpub "My Note.md" --to <page URL or id> --dry-run
cpub "My Note.md" --to <page URL or id>

# Create the note as a new child page (re-running updates that same page):
cpub "My Note.md" --under <page URL or id>
```

- **`--to`** replaces exactly that page. Before writing it shows a **replace preview** — the page title, space, current version, when it was last edited, and the old and new size — and `--dry-run` stops there. The page keeps its title unless you pass `--title "New title"`. The version comment reads `Published from Obsidian: <filename>`. After the write it prints the page link and the page-history link, where the previous version can be restored.
- **`--under`** creates a child page titled from `--title`, else the note's frontmatter `title:`, else the file name. Re-running finds and updates the same child. A page with that title elsewhere in the space is refused (use `--title` or `--to`).
- **Safety is the same as for folders**: a page this tool published from the same note updates silently; any other page — written by hand, or published from a different note — needs the one-time hand-over (type the count, or `--adopt`). The space's **home page** can never be a `--to` target.
- **No title prefix** by default in note mode (pass `--prefix` if you want one).
- **Diagrams** are rendered into your user cache folder (`~/.cache/confluence-publisher/img`, or `$XDG_CACHE_HOME/confluence-publisher/img`), never next to the note, so your vault stays untouched. `--img-dir` still overrides it.

### What it produces

- The numbered docs become child pages of your parent page.
- The `In Situ/` folder becomes an `In Situ` page with the deep-dives beneath it.
- Mermaid diagrams are rendered to PNG, uploaded as attachments, and embedded.
- `[[wikilinks]]` become real Confluence page links.
- Code blocks become code macros; Obsidian callouts (`> [!info]`, `> [!warning]`, …) become colored panels.

---

## Safety

The publisher may only overwrite a page it can prove it is allowed to. Every run follows these rules:

- **Plan first, write second.** The whole run is planned and checked before the first write. If anything is refused, **nothing** is written and **every** problem is listed, not just the first one. `--dry-run` prints that same plan and stops.
- **Only inside the parent.** A page counts as "already published" only if it sits **below** `--parent` (at any depth). A page with the same title anywhere else in the space, the parent page itself, or a page that is archived or in the trash is refused: `"<title>" already exists outside <parent> … — not touching it. Use a distinct --prefix.` A `--parent` that lives in a different space than `--space` is refused too. With `--to`, no title search happens at all: exactly the page you named is the target.
- **Only pages it published itself.** Every page the tool creates gets an invisible Confluence content property, `cpub-managed`, recording which source it came from (the prefixed page title for a folder, the note's file name for a single note). Pages carrying the marker from the same source update silently. Any other page (written by hand, published by an older version of this tool, or from a different source) needs a **hand-over** (below).
- **A cap on the blast radius.** A run that would update more than 25 existing pages is refused with the count; re-run with `--max-updates N` if that is really what you want. New pages don't count (nothing is lost).
- **It never deletes or moves anything**, and never renames a page.
- Each page body is **validated as XML before it is sent**, so malformed content fails locally instead of producing broken pages.
- Credentials are never written to disk by the tool and the API token is never printed.

If a write fails half-way (network drop, Confluence error), the run stops and lists the pages that were written and the ones that were not. Running the same command again is safe and finishes the job.

### Hand-over: replacing a page the tool did not publish

When the plan wants to update pages without a matching marker, it lists them (title, version, last edited) and asks:

```
Take over and replace 3 page(s) you did not publish? Type the number 3 to confirm:
```

Type the number to go ahead; anything else writes nothing. The pages get the marker, so the next run from the same source does not ask again. In a script or any non-interactive run there is no prompt: the run refuses unless you pass `--adopt`, which hands over the listed pages for this run only. There is deliberately no `--yes`: a blanket "yes" ends up pasted into scripts and then protects nothing.

**Upgrading from v1.1:** pages published by v1.1 have no marker, so the **first** republish of an existing doc set shows them all as "not published by this tool" and asks for this hand-over once. After that, it runs exactly as before. If the doc set has more than 25 pages, add `--max-updates` for that run as well.

The marker is an **accident guard, not access control**: anyone with edit rights can remove a content property. Confluence permissions remain the real boundary.

---

## Troubleshooting

Every expected failure prints one sentence, a `Fix:` line, and, for Confluence errors, Confluence's own reason underneath. Before planning, every run makes one read-only request to check who your credentials log in as, because Confluence Cloud answers a wrong token as if you were anonymous (a misleading `404` later). "Confluence did not accept your email/API token" therefore really means the token or email, and the `Fix:` line names the file it came from. After that check, a `403` means your account can't edit that page or space, and a `404` names what was not found: the `--space` key, or the `--parent` page ("not found or not visible to you": it must exist, be a page and not a folder, and be visible to your account).

**"The plan was refused … nothing was written".**
A safety check stopped the run before the first write (see [Safety](#safety)). Every refused page is listed with its own `Fix:`. The most common one, a title that already exists elsewhere in the space, is fixed with a distinct `--prefix`.

**"… need a one-time hand-over and this is not an interactive terminal".**
The run was started from a script, a pipe or an editor hotkey, so it could not ask you. Run it once in a terminal and type the count, or add `--adopt`.

**`HTTP Error 400` on publish (but `--dry-run` was fine).**
The dry-run reads from Confluence but never writes, so a problem in the write itself only surfaces on the real publish. The tool prints Confluence's own reason. The usual causes:

- **Duplicate page title.** Titles must be unique within a space (see [step 3](#3-pick-a-unique-prefix-when-sharing-a-space)). Publish each repo with a distinct `--prefix`. If two files *in your own doc set* collide, the tool stops before contacting Confluence and names the file to rename.
- **`--parent` is not a page in that space.** The parent must be a real **page** (URL shape `…/pages/<id>/<slug>`), and it must live in the `--space` you passed — not a folder, and not a page in another space.

**`confluence_publish requires Python 3.12+ (found 3.9.6)`.**
The interpreter is too old — on macOS, plain `python3` is Apple's 3.9. Install a supported one and use it explicitly:

```bash
brew install python@3.12
python3.12 confluence_publish.py my-repo-docs --space DOCS --parent 123456789 --prefix langflow --dry-run
```

**`ssl.SSLCertVerificationError: CERTIFICATE_VERIFY_FAILED` on macOS.**
This happens with the **python.org** build of Python, which ships its own CA bundle that isn't installed by default.

Quickest fix — use Homebrew's Python (`brew install python@3.12`, then `python3.12 …`), which works with HTTPS out of the box.

If you prefer the python.org build, fix it once by installing its CA bundle (replace `3.x` with your version, 3.12 or newer):

```bash
# Run the installer that ships with python.org Python:
/Applications/Python\ 3.x/Install\ Certificates.command

# …or, equivalently:
/Library/Frameworks/Python.framework/Versions/3.x/bin/python3 -m pip install --upgrade certifi
```

Verify with: `python3 -c "import urllib.request; urllib.request.urlopen('https://www.atlassian.com'); print('TLS OK')"`. Do **not** work around this by disabling certificate verification — that removes the security the check provides.

**Mermaid diagrams show "Preview not available" in Confluence edit mode.**
(and vanish after you save from the editor). Pages published by older versions of this tool saved the page body *before* uploading its diagrams, so Confluence never linked the images to their attachments; view mode hides this, the editor does not. Current versions upload first. **Fix an affected page by re-running the same publish command once** — the UPDATE re-saves the body after the attachments exist, which links them. If a diagram already vanished after an editor save, the re-publish restores it from the Markdown. See [`docs/KNOWN_ISSUES.md`](docs/KNOWN_ISSUES.md) for details.

## Status and roadmap

Working today: end-to-end generation and publishing, including code macros, colored panels, tables, Mermaid diagrams as attachments, and wikilink-to-Confluence-link conversion.

Not yet implemented (contributions welcome):

- A page map that survives renames (pages are matched by title inside the parent; the `cpub-managed` marker records ownership, not identity).
- Content-hash skip of unchanged pages.
- Exact heading-anchor links (`[[Page#Heading]]` currently lands on the page, not the heading).
- A `--prune` option to remove pages whose source doc was deleted (off by default).

---

## License

MIT — see [`LICENSE`](LICENSE).
