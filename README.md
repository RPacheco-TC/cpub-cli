# confluence-docs-publisher

Turn any code repository into a clean, numbered documentation set, then publish it to Confluence Cloud — with rendered diagrams and working cross-page links.

There are two stages, and they are independent:

1. **Generate** — point your own Claude at a repo and a spec (`template.yaml`); it writes a numbered Markdown doc set you review in Obsidian.
2. **Publish** — `confluence_publish.py` uploads that doc set to Confluence Cloud as a page tree under one parent page, idempotently and safely.

The Markdown is the source of truth; Confluence is a derived view you can re-publish at any time.

---

## What you need

| Tool | For | Notes |
| ---- | --- | ----- |
| **Python 3.12+** | Publishing | Standard library only; no `pip install` required. Check with `python3 --version`. macOS's built-in `python3` is 3.9 — install a newer one with `brew install python@3.12` and run the tool as `python3.12`. |
| **Node.js + `npx`** | Diagrams | Used to render Mermaid diagrams to PNG (`@mermaid-js/mermaid-cli`, fetched on first use). Optional — without it, diagrams fall back to a code block. |
| **Claude Code** | Generating | Only needed for stage 1. See [`GUIDE.md`](GUIDE.md). |
| **Obsidian** | Reading/editing | The generated docs use Obsidian wikilinks and Mermaid. Free from <https://obsidian.md>. |
| **A Confluence Cloud account + API token** | Publishing | Token: <https://id.atlassian.com/manage-profile/security/api-tokens> |

---

## Repository contents

| File | What it is |
| ---- | ---------- |
| `template.yaml` | The doc-set spec the generator follows (universal sections + per-repo "In Situ" deep-dives, adapted to the repo's archetype). |
| `GUIDE.md` | Step-by-step walkthrough for stage 1 (generate docs with your own Claude). |
| `confluence_publish.py` | The publisher (stage 2). A self-contained CLI. |
| `md_to_confluence.py` | Markdown → Confluence-storage conversion library used by the publisher. |
| `.env.example` | Template for the three credentials (copy to `.env`). |

---

## Stage 1 — Generate the docs

Full instructions are in [`GUIDE.md`](GUIDE.md). In short: open Claude Code in your repo, paste the generation prompt from the guide (with `template.yaml`), and it writes a folder like:

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

### 1. Set your credentials

```bash
cp .env.example .env      # then edit .env with your base URL, email, and token
set -a; . ./.env; set +a  # load them into your shell (never commit .env)
```

The base URL for Confluence Cloud ends in `/wiki`, for example `https://your-company.atlassian.net/wiki`.

### 2. Choose the space and parent page

The tool publishes everything under **one parent page** you specify, and never touches anything outside it. You need two values:

- **Space key** — the part after `/spaces/` in the URL: `…/wiki/spaces/`**`DOCS`**`/…`. A team space has a short key like `DOCS` or `ENG`; a personal space key starts with `~`.
- **Parent page id** — you must create a **page** (not a folder) in that space to hold the docs; everything is published as children of it. Create it (you need permission to add pages there), then copy its id from the URL: `…/pages/`**`123456789`**`/…`. You can keep that page inside a Confluence folder for tidiness; the tool still targets the page.

Tip: trial it in your **personal space** (`~yourname`) first, then repoint at the team space by changing `--space` and `--parent`.

### 3. Pick a unique prefix (when sharing a space)

**Confluence page titles must be unique within a space** — you cannot have two pages with the same title in one space. If you publish more than one repo's docs into the same (shared) space, generic titles like `01 - Welcome and Quick Start` or `In Situ` collide and the second publish fails with `HTTP 400`.

Use `--prefix <id>` (typically the repo name) to namespace every page title. It is applied **at publish time only** — your Markdown stays clean — and re-publishing with the same prefix updates the same pages, so it remains idempotent.

```
--prefix langflow   →   "langflow - 01 - Welcome and Quick Start", "langflow - In Situ", …
```

A prefix is optional in your own personal space (nothing else shares it) but recommended everywhere else.

Your Obsidian `[[wikilinks]]` keep working both ways: the files keep their original names (so Obsidian navigation is unchanged), and at publish time each link's **target** is prefixed to match the published page title — so cross-page links in Confluence resolve to the prefixed pages, while the visible link text stays the natural name.

### 4. Dry-run, then publish

```bash
# See exactly what would happen — writes nothing:
python3 confluence_publish.py my-repo-docs --space DOCS --parent 123456789 --prefix langflow --dry-run

# Publish for real:
python3 confluence_publish.py my-repo-docs --space DOCS --parent 123456789 --prefix langflow
```

The tool prints a per-page plan (`CREATE` / `UPDATE`, with the actual prefixed titles) and how many diagrams each page carries. Re-running updates the same pages in place (matched by title), so it is safe to run repeatedly.

Optional: `--img-width <px>` sets a display width on every Mermaid diagram (e.g. `--img-width 700`), so diagrams rarely need resizing in the Confluence editor (see [Troubleshooting](#troubleshooting)). Without it, diagrams keep their natural size and the output is unchanged.

### What it produces

- The numbered docs become child pages of your parent page.
- The `In Situ/` folder becomes an `In Situ` page with the deep-dives beneath it.
- Mermaid diagrams are rendered to PNG, uploaded as attachments, and embedded.
- `[[wikilinks]]` become real Confluence page links.
- Code blocks become code macros; Obsidian callouts (`> [!info]`, `> [!warning]`, …) become colored panels.

---

## Safety

The publisher is non-destructive by construction:

- It only ever **creates or updates pages under the parent page id you pass**. It does not scan, edit, or touch anything elsewhere in the space.
- It **never deletes** anything.
- Each page body is **validated as XML before it is sent**, so malformed content fails locally instead of producing broken pages.
- Credentials are read from the environment only and are never written to disk by the tool.

The worst realistic failure is a mis-formatted page under your own parent, which you can edit or delete.

---

## Troubleshooting

**`HTTP Error 400` on publish (but `--dry-run` was fine).**
The dry-run never exercises the write path, so a problem only surfaces on the real publish. The tool now prints Confluence's own reason. The usual causes:

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
(and vanish after you save from the editor). Pages published by older versions of this tool saved the page body *before* uploading its diagrams, so Confluence never linked the images to their attachments; view mode hides this, the editor does not. Current versions upload first. **Fix an affected page by re-running the same publish command once** — the UPDATE re-saves the body after the attachments exist, which links them. If a diagram already vanished after an editor save, the re-publish restores it from the Markdown. See `KNOWN_ISSUES.md` for details.

## Status and roadmap

Working today: end-to-end generation and publishing, including code macros, colored panels, tables, Mermaid diagrams as attachments, and wikilink-to-Confluence-link conversion.

Not yet implemented (contributions welcome):

- A `state.json` page map (currently pages are matched by title), to survive renames and skip unchanged pages.
- Content-hash skip of unchanged pages.
- Exact heading-anchor links (`[[Page#Heading]]` currently lands on the page, not the heading).
- A `--prune` option to remove pages whose source doc was deleted (off by default).

---

## License

MIT — see [`LICENSE`](LICENSE).
