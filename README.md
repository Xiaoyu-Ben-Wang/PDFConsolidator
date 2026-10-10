# PDF Consolidator

Turn a folder of mixed files — Word, Excel, PowerPoint, Outlook items, email,
images, plain text and existing PDFs — into a single merged PDF, with a desktop
interface for arranging the pages first.

## Requirements

- Windows
- LibreOffice, for Office documents and message bodies
- Python 3.10+ (only for development; the shipped build bundles everything)

## Install (development)

```sh
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
```

Generate some sample input and try it:

```sh
.venv\Scripts\python tools\make_fixtures.py
.venv\Scripts\python tools\make_email_fixtures.py
.venv\Scripts\python -m consolidator gui
```

## Using the interface

```sh
.venv\Scripts\python -m consolidator gui [paths...]
```

- **Sources** (left) lists the selected files with their real Windows file-type
  icons. Expanding a source shows what it contributed — a document, an email's
  body, each extracted attachment. A message attached to a message gets a node of
  its own inside the message that carried it, holding that message's body and its
  own attachments. Unticking anything leaves it out, and unticking a message
  leaves out everything inside it. Select a row and press **Delete** or
  **Backspace** to remove it and its pages.
- **Jump to the page column** with the arrow button beside the *Sources* heading
  (or **Ctrl+J**): it takes the page list to the earliest page the selected row
  stands for, scrolled into the middle of the view and selected, so the preview
  follows. It lands on where the page *is* now, not where it was converted from,
  so it still works after pages have been moved around. Select a source to go to
  that source's first page; select an attachment or an email's body to go to that
  part; select a forwarded message to go to the forwarded message's own first
  page rather than its carrier's. A row with no pages says so in the status bar.
  Clicking a row does *not* move the page list — selecting a source is also how
  the per-email extraction settings are edited, and a jumping list would be noise
  there.
- **Pages** (middle) is a single vertical column, one row per page, each row
  showing the page number, the file it came from, the messages it was found
  inside, and any rotation. Rows are coloured by the message they came from, so
  pages from one file read as a block and a forwarded message reads as a block of
  its own colour inside it.
  Drag rows to reorder them, or select several and use the buttons. Delete and
  Backspace remove the selected pages.
- **Preview** (right) shows the selected page large, rotated as it will be
  written, with its origin underneath.
- **Group by document** clusters the pages so everything from one file sits
  together, keeping each document's own page order.
- **Extract from email items** is decided *per email*. Select an email in the
  sources list and the checkboxes edit that message; with nothing selected they
  edit the default for newly added emails. "Apply to all emails" resets the
  overrides.
- **Output** is a single *Save as…* button that opens the standard Windows save
  dialog.
- **About** (bottom right, and under *Help*) names the version and the terms, and
  opens the full licence inventory.

Conversion happens automatically when files are added — only the new files are
converted, so page edits already made are never lost. **Convert** re-converts
everything from scratch. Changing the page size or layout afterwards only
re-assembles, so editing is cheap.

### Large batches

Adding several hundred files — a folder of Office documents, or a mailbox of
`.msg` items — is the case the pipeline is built for, and the interface is
honest about the wait:

- **Conversion runs several LibreOffice invocations at once.** This is the
  biggest lever there is, because conversion *is* the time: everything else —
  progress, thumbnails, assembly — is measured in tenths of a second. Each
  invocation is a separate `soffice` process with its own profile and its own
  output directory, so they do not collide; what limits them is memory. Measured
  on a sixteen-core machine converting 48 documents: 63 s one at a time, 23 s four
  at a time. The limit is four, or half the logical cores if that is smaller, and
  `CONSOLIDATOR_WORKERS=1` turns it off for a machine that dislikes several
  `soffice` processes.
- **A chunk that converts nothing at all is retried once, after a pause.** That
  signature means the invocation failed rather than the documents, and the usual
  cause is a machine still settling after another run's LibreOffice exited.
- A **progress bar** counts finished files against the total, with the file being
  worked on named in the status bar, and a **Cancel** button stops after the file
  in progress, keeping everything already converted.
- Progress moves *during* a LibreOffice run. One invocation for a thousand files
  would mean a bar that sits still for minutes, so the batch is split into about
  fifteen chunks: each costs one extra LibreOffice start-up (about 5 s, measured)
  and buys a progress update. Small batches are not chunked at all.
- **Thumbnails are rendered for what is on screen**, plus a margin, and again when
  you scroll. Tens of thousands of pages would otherwise be rendered before the
  window was usable.
- The thumbnail and open-document caches are bounded, so the file handles and
  memory stay flat as files are added rather than growing with the batch.

Measured on this machine (Windows, LibreOffice 26):

| Scenario | Result |
|---|---|
| 600 PDFs converted | 0.8 s, 0 failures |
| …then loaded into the window | 0.6 s (600 navigator rows, 1200 page rows) |
| …navigator rebuild / page column rebuild | 0.06 s / 0.11 s |
| …thumbnails asked for | 40, not 1200 |
| 240 Office documents converted | 156 s, 0 failures — **65 s with conversion running four at a time** |
| …longest gap between progress updates | 16 s |
| …then loaded into the window | 0.17 s |
| 3 000 PDFs converted → loaded → assembled | 3.9 s → 3.0 s → 8.9 s for 6 000 pages |
| 10 000 PDFs converted → loaded → assembled | 13.8 s → 10.7 s → 32.4 s for 20 000 pages |
| …navigator rebuild / page column rebuild at 10 000 rows | 2.0 s / 1.1 s |
| …thumbnails asked for at 20 000 rows | 40 |

So a thousand Office documents is about eleven minutes with the bar moving every
fifteen seconds, and everything after conversion is effectively instant. The cost
is LibreOffice's, not the pipeline's.

**Where it starts to hurt.** At ten thousand files the conversion and the assembly
are still fine, but a *full* rebuild of the navigator costs two to three seconds,
and it grows slightly with each rebuild — no Python leak (an explicit
`gc.collect()` makes it worse, and `gc.garbage` stays empty), just Qt item churn
on Windows. Nothing in the editing path is affected: adding rows, reordering,
thumbnails and preview stay responsive. The fix, if that scale ever matters, is to
add navigator rows for the sources that changed instead of clearing and rebuilding
the whole tree; it is deliberately not done yet, because at the scale this is
built for a rebuild costs 0.06 s and the extra state would be pure risk.

### Email extraction

| Option | Effect |
|---|---|
| Message body | the headers and body become a page |
| PDF documents | attached PDFs are merged **byte-for-byte**, so page counts are exact |
| Images | attached images are converted; **inline** signature images are excluded |
| Documents | attached Word/Excel/PowerPoint, **HTML**, text, Markdown and CSV files are converted |
| Embedded messages | attached `.msg`/`.eml` are **drilled into**: parsed and expanded in place, recursively, up to `--max-depth` levels (default 4) |
| Other attachments | listed on the body page, not converted |

All of these are on by default except *Other attachments*.

### Forwarded messages

An email attached to an email — the usual "Fwd:" — is not treated as an opaque
file. The attached message is read, and its body and **its own attachments**
become pages, nested as deep as the depth limit allows (four by default: A carries
B carries C carries D).

The case worth being explicit about, because it is the one that quietly breaks
naive implementations: **A carries B, and B carries its own attachment.** B's
attachment is parsed as *B's* attachment, converted at B's level in the tree, and
grouped under B — not reported as an attachment of A. Both halves of that matter:
`_walk_parts` stops `Message.walk()` descending into an attached message (which
would list B's attachments as A's, duplicating them and flattening the nest), and
`_decode_part` re-serialises the attached message, because
`get_payload(decode=True)` returns `None` for `message/rfc822` and B would
otherwise arrive as a zero-byte file. An A → B → C → PDF chain arrives as three
attachments at three depths; there are tests for both.

In the navigator a forwarded message becomes a node inside the message that carried
it, so you can see what came from where and leave out a whole forwarded
conversation by unticking one row. In the page column every message has its own
colour, derived from the message it arrived inside but shifted away from it, so a
forwarded message never blends into its carrier and separate forwards stay
distinguishable. Both levels of that structure come from the same tags on the
segment, which is why the navigator and the page column always agree.

## Command line

```sh
consolidator build <paths...> -o out.pdf [options]
consolidator check
consolidator gui
```

| Option | Meaning |
|---|---|
| `--page-size` | `letter` (default), `a4`, `legal`, `original` |
| `--margin` | margin in inches when fitting to a page size (default `0.5`) |
| `--separator` | `none`, `blank`, or `title` between documents |
| `--page-numbers` | `none`, `continuous`, or `per-document` |
| `--number-format` | template using `{n}` `{total}` `{doc}` `{n_doc}` `{total_doc}` `{date}` |
| `--number-position` | e.g. `bottom-center`, `top-right` |
| `--header-*`, `--footer-*` | text for the left/centre/right slot |
| `--extract` | attachment categories to pull out of email, comma separated (`body`, `pdf`, `images`, `documents`, `embedded`, `other`; `office` is accepted as the old name for `documents`) |
| `--no-body` | omit the message body page |
| `--max-attachment-mb` | skip attachments larger than this |
| `--max-depth` | nested message recursion limit |
| `--no-bookmarks` | skip the bookmark per segment |
| `--no-recursive` | do not descend into subfolders |
| `--keep-scratch` | keep intermediate PDFs for inspection |

## Building the standalone bundle

```sh
.venv\Scripts\python tools\build.py
```

Produces `dist/PDFConsolidator/`: the frozen application plus a trimmed
LibreOffice. Copy that folder to the target machine and run
`PDFConsolidator.exe`. No installer, no Python and no Office are required.

### Publisher name

Windows reports "Unknown Publisher" because the executable has no Authenticode
signature, and **there is no setting that simply declares a publisher** — the
name comes from the signing certificate. The build therefore:

1. embeds a version resource (`build/version_info.txt`) setting CompanyName,
   ProductName and copyright; this is what the file's *Properties → Details*
   tab shows;
2. signs the executable with a self-signed certificate whose subject is the
   publisher, which is what SmartScreen and UAC read.

```sh
python tools/build.py --publisher "Xiaoyu-Ben-Wang"       # default
python tools/build.py --no-sign                            # skip signing
python tools/build.py --trust                              # also trust it locally
```

`tools/sign.ps1` can be run on its own against an existing executable.

**Being honest about what this does.** A self-signed certificate *identifies* the
publisher; it does not *vouch* for them. The name will appear in place of
"Unknown Publisher", but Windows will still warn until the certificate is trusted
on that machine. `--trust` adds it to the local Trusted Root and Trusted
Publishers stores and needs administrator rights; other machines need
`pdfconsolidator-publisher.cer` installed the same way. Only a certificate from a
public authority earns "verified publisher" reputation.


## Supported formats

| Input | Engine |
|---|---|
| `.docx` `.doc` `.docm` `.dotx` `.dotm` `.xlsx` `.xls` `.xlsm` `.xltx` `.xltm` `.pptx` `.ppt` `.pptm` `.pps` `.ppsx` `.rtf` `.odt` `.ods` `.odp` `.odg` | LibreOffice — including the macro-enabled and template variants, which are the same containers |
| `.html` `.htm` `.xhtml` | LibreOffice, **rendered** — the same path an email body takes, so a page arrives as a page and not as markup on one |
| `.msg` | `extract-msg` (Outlook is never involved) |
| `.eml` | standard library |
| `.jpg` `.jpeg` `.png` `.tif` `.tiff` `.bmp` `.gif` `.webp` | `img2pdf` — JPEGs embedded losslessly |
| `.txt` `.md` `.markdown` `.log` `.csv` | literal monospaced text; **Markdown is not interpreted** |
| `.pdf` | copied through untouched |

Not supported: `.pub`, `.xlsb` (binary workbooks), HEIC, OCR of scans. The file
dialog's filter is built from `convert.SUPPORTED_EXTS`, so it cannot drift from
what converts; the CLI's `--extract` categories and the "supported" checks read
the same sets, so adding a format in one place is enough. The sets are
`OFFICE_EXTS`, `WEB_EXTS`, `TEXT_EXTS`, `IMAGE_EXTS`, `PDF_EXTS` and
`EMAIL_EXTS`, with `DOCUMENT_EXTS` (all of the first three) deciding what counts
as an attached *document* rather than a stranger.

## Design notes

Two phases, deliberately separated:

1. **Convert** — each source becomes one or more PDF *segments*. Office documents
   and any email bodies are converted in batched LibreOffice invocations, because
   start-up dominates; the batch is chunked so progress can be reported (see
   *Large batches*). Sources are staged under unique names so two files with the
   same name cannot collide.
2. **Assemble** — segments are merged and fitted to the target page size. This
   phase never touches LibreOffice, so re-running it is nearly free.

Four things the code goes out of its way to handle, all found by testing:

- **LibreOffice inherits the launcher's environment.** If `PYTHONPATH` is set —
  which it is whenever this app runs from Python — LibreOffice's own bundled
  Python breaks and reports `source file could not be loaded`.
- **LibreOffice cannot be given a relative path.** It resolves what it is told
  against its own working directory, so a relative source or `--outdir` produces
  `Error: source file could not be loaded` and no output at all — with nothing in
  the message to suggest the path was the problem. `convert_batch` makes both
  absolute before launching, and keeps the result dictionaries keyed by the paths
  the caller passed. The application itself was incidentally safe, because the
  command line and the window both stage every Office document into an absolute
  scratch directory first; the library entry points were not. Any script calling
  `convert_batch`, `convert_one`, `convert.convert` or `pipeline.build` with a
  relative path — a relative `scratch_dir` is enough — got silent failure, one
  "LibreOffice crashed" per file, for every file.
- **The system temp directory is not used.** The application creates its own
  scratch folder, `.scratch`, beside itself — the directory it is run from — and
  falls back only if that is unwritable. LibreOffice needs to create work files,
  and when it cannot it reports that same misleading error. A portable
  application is copied somewhere writable by definition, so it should not
  depend on `%TEMP%` at all.
- **LibreOffice is not manifested for long paths.** Its profile holds very deep
  internal paths; nesting that under a deep working directory exceeds
  `MAX_PATH` and crashes it. Windows' `LongPathsEnabled` does not help, because
  it only applies to applications that opt in. The profile therefore lives at a
  short fixed prefix (`.lo-profiles` beside the application), not under the
  working directory, and is removed after each run.

A page's `/Rotate` is baked into its content stream before anything else happens,
because a viewer applies `/Rotate` to the whole page — so a page number drawn
onto a rotated page would itself appear rotated. The order is: bake, rotate,
scale, decorate.

## Tests

```sh
.venv\Scripts\python -m pytest tests -q
```

LibreOffice- and fixture-dependent tests skip automatically when unavailable.
Generate fixtures first with `tools/make_fixtures.py`,
`tools/make_email_fixtures.py` and `tools/get_msg_samples.py`.

## Licence

**GPL-3.0-or-later**, no warranty. `LICENSE` is the full text; the **About**
button in the window carries the summary, with every third-party component and
its licence text one click away.

GPL rather than permissive because one dependency is `extract-msg` (GPL-3.0), the
`.msg` reader. The rest is either permissive (pypdf, reportlab, Pillow, pywin32)
or weak copyleft a GPL build satisfies anyway — Qt, img2pdf and RTFDE under the
LGPL-3.0, pikepdf under the MPL-2.0. A proprietary build is not an option; that
was a deliberate choice.

### What ships with the binaries

Beside the executable: `LICENSE`, `THIRD-PARTY-NOTICES.md`, `LICENSES/` (every
text verbatim, as the packages ship them), and LibreOffice's own notices under
`libreoffice/` — those last are the authoritative ones for that tree.

`tools/collect_licenses.py` reads the *installed* packages rather than a
hand-kept list, so a new dependency cannot quietly go unlicensed. The build
copies the material in and then checks that the frozen module list is covered:

```sh
.venv\Scripts\python tools\collect_licenses.py
.venv\Scripts\python tools\build.py
```

Two details worth knowing. Qt is used under the LGPL-3.0-only option, its wheels
ship only a commercial licence reference, so the LGPL text is published as
`LICENSES/LGPL-3.0.txt`; and because the Qt libraries are separate DLLs under
`_internal/PySide6/`, they can be replaced with another build of the same version
without touching the program — which is what the LGPL asks for.

### Where the source is

GPL-3.0 section 6 requires the Corresponding Source to reach anyone who gets the
binaries. This tree *is* that source, build scripts included:

```sh
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python tools\collect_licenses.py
.venv\Scripts\python tools\build.py
```

If you publish the bundle, set `SOURCE_URL` at the top of
`tools/collect_licenses.py` to where the repository lives and re-run it; the
notices then name the location instead of telling the recipient to ask you.
LibreOffice is redistributed unmodified apart from the trimming described above,
and its own source is published by The Document Foundation.
