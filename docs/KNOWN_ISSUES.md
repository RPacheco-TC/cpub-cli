# Known Issues

Known bugs, limitations, and confirmed workarounds for `confluence-docs-publisher`.
Each entry includes a **TODO** tracking follow-up work.

---

## SSL certificate error on macOS (`CERTIFICATE_VERIFY_FAILED`)

### Symptom

Publishing fails immediately with:

```
ssl.SSLCertVerificationError: CERTIFICATE_VERIFY_FAILED
```

### Root cause

The **python.org** installer builds Python with its own CA bundle, but does not
install it by default. Until the bundle is installed, TLS connections (including
to Atlassian) fail.

Homebrew's Python does **not** have this problem. (Apple's system Python,
`/usr/bin/python3`, doesn't either, but it is 3.9 and the tool now requires
Python 3.12+.)

### Quickest fix

Use Homebrew's Python:

```bash
brew install python@3.12
python3.12 confluence_publish.py ...
```

### If you must use the python.org build

Run the CA-bundle installer that ships with it (adjust the version number):

```bash
/Applications/Python\ 3.x/Install\ Certificates.command
```

Double-clicking **Install Certificates.command** inside **Applications → Python 3.x**
does the same thing. Equivalent one-liner:

```bash
pip install --upgrade certifi
```

Confirm the fix before re-running the full publish:

```bash
python3 -c "import urllib.request; urllib.request.urlopen('https://www.atlassian.com'); print('TLS OK')"
```

### TODO

- [x] ~~**Promote `/usr/bin/python3` as the recommended interpreter**~~ — done,
  then **reversed**: the tool now requires Python 3.12+ and Apple's system
  Python is 3.9, so the README recommends Homebrew `python@3.12` instead.
- [x] Make the version reference in the README version-agnostic (`Python 3.x`
  instead of a hardcoded minor version) — done.

---

## Mermaid images show "Preview not available" in Confluence edit mode

### Symptom

Mermaid diagrams render correctly in Confluence **view mode** but appear as grey
"Preview not available" placeholders in **edit mode**. They cannot be resized
through the editor.

Saving from the editor then **drops** the placeholders, so the diagrams vanish
from view mode too.

### Root cause (corrected — this was a tool bug, now fixed)

The original diagnosis below ("a Confluence limitation") was wrong. The cause was
**publish order**: `publish_page` saved the page body *before* uploading the PNGs
it references. Confluence links each `ri:attachment` to the actual file when the
body is saved (it adds `ri:version-at-save="N"` to the reference). A body saved
before the file exists keeps an unlinked, name-only reference: view mode still
finds the file by name, but the editor cannot map it to a media file, shows
"Preview not available", and drops it on save. Verified on the test site:
version 1 of a freshly created page had bare references; re-saving the same body
once the attachments existed produced `ri:version-at-save="1"` and the editor
loaded both images. It hit every CREATE with diagrams, and every UPDATE whose
diagram changed (new content hash → new filename).

**Fix:** the publisher (now `_write_item`, called only from `execute_plan`) uploads missing attachments *before* saving the body
on UPDATE, and on CREATE (the page must exist to take attachments) saves the body
a second time after uploading. Covered by `tests/test_publish_order.py`.
**Existing broken pages:** re-run the publish once; the UPDATE re-saves the body
after the attachments exist, which links them. No manual workaround is needed.

### Old workaround (no longer needed)

> **⚠️ Do all replacements before saving.**
> If you replace some images and save, the remaining attachment-referenced images
> lose their rendering too. Complete all replacements in one editing session, then
> save once.

1. **In view mode**, open every diagram image in a separate browser tab
   (right-click → Open image in new tab). This loads the actual PNG from
   Confluence's CDN into each tab.
2. Enter **edit mode**. The images show as "Preview not available".
3. For each image: switch to its tab, select and copy the PNG
   (Cmd+A / Ctrl+A then Cmd+C / Ctrl+C), then paste it in place of the
   placeholder in the editor.
4. Repeat for every image on the page.
5. **Save once** after all replacements are done. All images render correctly.

### TODO

- [x] Investigate whether the `ac:image` macro supports a `width` attribute at
  publish time — it does (`ac:width`). Added `--img-width PX`, which emits
  `<ac:image ac:width="PX">` on every diagram. **Decision:** no default width —
  when the flag is unset no attribute is emitted, so existing publishes are
  byte-identical; pass e.g. `--img-width 700` to opt in.
- [x] Add this known limitation and workaround to the README troubleshooting
  section — done.
- [x] Fix the real cause (body saved before attachments were uploaded) — done;
  README troubleshooting now says "re-publish" instead of the manual workaround.

---

## Wikilink inside a code block breaks XML on publish

### Symptom

Publishing a document that contains a `[[wikilink]]` literally written inside a
fenced code block (` ``` `) fails with an XML parse error — mismatched tag — and
the entire publish aborts.

### Root cause

The wikilink extractor ran over the full document text **before** code fences
were masked. A `[[wikilink]]` inside a ` ``` ` block was extracted and converted
to an `<ac:link>` macro, which was then injected inside a `CDATA` section (where
the converter had already placed the code block content). `CDATA` does not allow
XML tags; the `<ac:link>` markup produced a mismatched-tag error that failed
`validate_storage()` before anything was sent to Confluence.

First encountered with a RabbitMQ document that had a `[[wikilink]]` example
inside a code fence.

### Fix — merged in [PR #1](https://github.com/RPacheco-TC/obx-confluence-obsidian-publisher/pull/1) (2026-07-01)

`_extract_wikilinks` in `confluence_publish.py` now iterates line-by-line,
tracking an `in_fence` boolean that flips on/off at each ` ``` ` boundary. Lines
inside a fence pass through unmodified; lines outside receive the wikilink regex
substitution as before.

### TODO

- [x] ~~Port the fix to the repo and open a PR~~ — done, merged in PR #1.
- [x] Add a regression test — done: `tests/test_md_to_storage.py`
  (`python3.12 -B -m unittest discover -s tests`, offline).
- [x] Audit whether the same masking gap exists for `_extract_callouts` — it
  did, and for `_extract_toc` too: a `> [!info]` or `[[TOC]]` line inside a code
  block was published as a literal `xCALLOUT0x` / `xTOC0x` token (silently, since
  the XML stayed valid). Fixed: all three extractors now share one fence rule
  (`_is_fence`) and leave fenced lines untouched; covered by regression tests.
- [x] ~~Handle tilde fences (`~~~`) and 4-space indented code blocks~~ —
  **won't fix.** `confluence_publisher/markdown_html.py` (formerly `md_to_confluence.py`) does not treat either style as code at
  all (no `<pre><code>`, no CDATA), so a wikilink inside one becomes a normal
  link and cannot break XML. Supporting them would be a converter feature, not
  a fix to this bug.

---

## Wikilink or `[[TOC]]` in a code block inside a callout/blockquote breaks XML

### Symptom

Publishing fails with an XML parse error (`mismatched tag`) when a fenced code
block *inside a callout or blockquote* contains a `[[wikilink]]` or `[[TOC]]`:

````markdown
> [!info] Example
> ```
> [[01 - Foo]]
> ```
````

The same happens with a plain `>` blockquote. Found during QA of the
code-fence fixes; it also fails on the code before those fixes (not a regression).

### Root cause

The pre-conversion extractors only recognise a fence when the line itself starts
with ```` ``` ```` (`_is_fence`). Inside a quote the fence line starts with
`> ` instead, so the wikilink/TOC is still extracted, and its `ac:link` / macro
markup ends up inside the code block's `CDATA`, which cannot hold tags.

### TODO

- [ ] Make the extractors treat `> `-prefixed fence lines as fences inside
  quote blocks (keeping one shared rule), and add regression tests for
  callouts, plain blockquotes and `[[TOC]]`.


---

## First republish of a v1.1 doc set asks for a hand-over

### Symptom

After upgrading, republishing a doc set that v1.1 published lists every
existing page as "not published by this tool" and asks:

```
Take over and replace 18 page(s) you did not publish? Type the number 18 to confirm:
```

In a non-interactive run (script, pipe, editor hotkey) it refuses instead:
"… need a one-time hand-over and this is not an interactive terminal —
nothing was written." A set with more than 25 existing pages is also refused
by the blast-radius cap (`--max-updates`, default 25).

### Root cause

Intended behaviour, not a bug. The publisher now overwrites only pages that
carry its `cpub-managed` content property for the same source; v1.1 never
wrote that property, so its pages look exactly like hand-written ones. There
is deliberately no automatic adoption: a page that merely has a matching
title could be someone else's.

### Fix

Run the publish once in a terminal and type the page count (add
`--max-updates N` if the set has more than 25 pages), or pass `--adopt` in a
script. Each page gets the marker as it is written, so every later run is
silent, as before. The marker is an accident guard, not access control:
anyone with edit rights can remove it, and that page then needs one hand-over
again.

### TODO

- [ ] None planned — this is a one-time step per doc set.

---

## A page written without its ownership marker needs a hand-over next time

### Symptom

A run finishes but prints `Warning: <title> was written but could not be
marked as managed …`. The next run asks to hand that page over again.

### Root cause

The v2 API cannot attach a content property in the same call that creates or
updates a page, so the `cpub-managed` marker is a second request. If that
request fails (network drop, a permission on content properties), the page
body is already written. The run continues rather than aborting, and reports
the page.

### Fix

Run the same command again and confirm the hand-over for that page; the marker
is written then.

### TODO

- [ ] Retry the marker write once before reporting it.

---

## Links to notes that are not in Confluence

### Symptom

- **One note** (`--to` / `--under`, or a `confluence:` target): since v1.4 each
  `[[Other note]]` is looked up by title in the space. If there is no such page,
  the link is published as its display text in italics instead of a link to a
  missing page, and the run says so once, also on `--dry-run`:
  `2 links left as text: Other note, Design ideas`. The text does **not** turn
  into a link by itself later: once `Other note` is published, publish this note
  again.
- **A folder** (set mode) is unchanged: every `[[link]]` becomes a link by title,
  because the pages of the set are created in the same run (a lookup would drop
  links to pages that are about to exist). A link to a note *outside* the folder
  still points at a page that does not exist until that note is published.

### Root cause

Confluence links by title, and set mode cannot know which titles will exist after
the run without making every link depend on the run's own order. Note mode
publishes one page, so it can ask the space first (S2.5, decision in the spec).

### Workaround

Publish the linked notes first (or the whole folder), then republish the note.
In set mode, keep cross-links inside the folder, or write outside references as
plain text.

### TODO

- [ ] Set mode: look up links that point outside the set (targets not in the
  folder) and degrade only those.

---

## A new note page's first version has no "Published from Obsidian" comment

### Symptom

After `--under` creates a page from a note, the page history shows version 1
with an empty version comment. Every later update reads
`Published from Obsidian: <filename>`, and so does the second save of a new
page that has diagrams (its body is saved again once the images are uploaded).

### Root cause

Confluence's v2 "create page" call drops any version message (confirmed live
on the test page). The only way to label version 1 would be an extra save right
after the create: a second version with no change in content, added to the
history of every new page just for a label.

### Why this is acceptable

The version comment is information for readers, not what the tool relies on.
Pages the tool published are identified by their `cpub-managed` content
property (the ownership marker), which is written for every created page,
labelled or not.

### TODO

- [ ] Revisit if Confluence's create call starts accepting a version message.

---

## `cpub undo` leaves created pages, attachments and older runs alone

### Symptom

- After `cpub undo`, pages the run **created** are still in Confluence (undo lists
  them), and diagram **attachments** the run uploaded stay on their pages.
- An undo cannot itself be undone with `cpub undo`, and runs published before
  v1.4 are not in the log, so `cpub undo` says there is nothing to undo.
- A page edited after the publish is not restored (undo lists it with its
  history link).
- If a restore fails half way (network), the run is **not** marked undone; the
  pages already restored now count as "edited since the run".

### Root cause

By design (S0.5, decisions D-3 to D-5): the tool never deletes anything, and it
never overwrites a version someone else saved after it. The log only exists from
v1.4 on.

### Workaround

- Delete created pages by hand in Confluence if you do not want them.
- To undo an undo, or a page edited since, use the page history link that undo
  prints ("Restore this version" in Confluence).
- After a failed undo, fix the cause and run `cpub undo` again: it picks the same
  run and restores only the pages still at the version the publish wrote.

### TODO

- [ ] A `--delete-created` opt-in, only if users ask for it (never by default).

---

## The Shell commands plugin does not open its settings on Obsidian 1.14

### Symptom

The one-key recipe of v1.4's first draft used the community plugin *Shell commands*
(0.23.0). On Obsidian 1.14.5 its settings tab does not open; the developer console
shows:

```
No tab content was found with activate_tab_id 'SC-tab-main-shell-commands'
```

so the `cpub {{file_path:absolute}}` command cannot be created.

### Root cause

An incompatibility between Shell commands 0.23.0 and Obsidian 1.14 (found in the
user's click-through, 2026-10-28). It is outside this repository.

### Fix

Publishing from Obsidian now uses our own plugin, **Confluence Publisher**
(separate repository, `confluence-publisher-obsidian`): it runs `cpub` with no
shell and no credentials of its own. See
[Publish the current note from Obsidian](GUIDE.md#publish-the-current-note-from-obsidian).
`cpub` itself did not change.

### TODO

- [ ] Link the plugin's public repository and store page once they exist.
