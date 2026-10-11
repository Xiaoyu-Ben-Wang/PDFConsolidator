# PDF Consolidator

Converts a set of mixed files into one merged PDF: Word, Excel, PowerPoint,
Outlook items, email, images, plain text and existing PDFs. A desktop window
lets you reorder, rotate and remove pages before saving.

## Runs offline

No internet connection is needed. All conversion happens on your machine, and
files are never uploaded anywhere. The app makes no network requests, and
remote images linked from HTML emails are not downloaded.

It is built on local, open-source components:

| Component | Used for |
|---|---|
| [LibreOffice](https://www.libreoffice.org/) (headless) | Office documents, HTML and email bodies to PDF |
| [extract-msg](https://github.com/TeamMsgExtractor/msg-extractor) | reading Outlook `.msg` files |
| Python `email` package | reading `.eml` files |
| [img2pdf](https://gitlab.mister-muffin.de/josch/img2pdf), [Pillow](https://python-pillow.org/) | images to PDF |
| [pypdf](https://github.com/py-pdf/pypdf), [pikepdf](https://github.com/pikepdf/pikepdf), [reportlab](https://www.reportlab.com/) | merging, page fitting, page numbers and headers |
| [pdfium](https://pdfium.googlesource.com/pdfium/) | page previews, thumbnails and the output check |
| [Qt](https://www.qt.io/) (PySide6) | the desktop window |

Two development scripts do use the internet: `tools/get_msg_samples.py`
(downloads sample `.msg` files for tests) and `tools/vendor.py` (downloads
packages). Neither is part of the app.

## Requirements

- Windows
- LibreOffice, for Office documents and email bodies (the standalone build
  includes it)
- Python 3.10+ (development only)

## Install (development)

```sh
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
```

Generate sample input and open the window:

```sh
.venv\Scripts\python tools\make_fixtures.py
.venv\Scripts\python tools\make_email_fixtures.py
.venv\Scripts\python -m consolidator gui
```

## Using the window

```sh
.venv\Scripts\python -m consolidator gui [paths...]
```

The window has three columns.

**Sources** (left) lists the input files.

- Expand a source to see its parts: the document, an email's body, and each
  attachment. An attached email is shown as a child node with its own body and
  attachments.
- Parts are listed in the order the email stores them.
- Untick a row to exclude it. Unticking an email excludes everything inside it.
  Ticking it again puts the pages back in their original position.
- **Delete** or **Backspace** removes the selected row and its pages.
- An attachment that could not be read shows a warning icon and the reason.
- The arrow at the end of a row (or **Ctrl+J**) selects that row's first page in
  the page list. It follows the page's current position, so it still works after
  pages are reordered. Clicking the row itself does not move the page list.

**Pages** (middle) has one row per page: page number, source file, the emails
it was found inside, and rotation.

- Rows are coloured by source email. An attached email gets a different colour
  from the email that carried it.
- Drag rows to reorder, or select rows and use the buttons. **Delete** and
  **Backspace** remove the selected pages.
- **Group by document** puts each file's pages together, keeping their order.

**Preview** (right) shows the selected page as it will be written, with its
source underneath.

Other controls:

- **Extract from email items** applies to the selected email. With nothing
  selected, it sets the default for emails added later. **Apply to all emails**
  clears the per-email settings.
- **Save as…** opens the Windows save dialog. Existing files are never
  overwritten: if `report.pdf` exists, the output is written as
  `report (1).pdf`, then `report (2).pdf`, and so on. The status bar and the
  *Done* dialog show the name used. The command line works the same way.
- **Closing** the window asks for confirmation (default: No) and lists what
  would be lost.
- **About** (bottom right, and under *Help*) shows the version, licence and
  warranty disclaimer, and opens the full licence list.

Files are converted when they are added. Only new files are converted, so page
edits are kept. **Convert** re-converts everything. Changing page size or layout
only re-runs assembly, which is fast.

### Large batches

- **Parallel conversion.** LibreOffice conversion takes almost all of the time,
  so up to four `soffice` processes run at once (or half the logical cores, if
  lower). Each has its own profile and output folder. Measured: 48 documents
  took 63 s one at a time and 23 s four at a time. Set `CONSOLIDATOR_WORKERS=1`
  to turn this off.
- **Retry.** If a chunk produces no output at all, it is retried once after a
  short pause. This usually means LibreOffice failed to start, not that the
  documents are bad.
- **Progress.** Large batches are split into about 15 chunks so the progress bar
  updates during conversion. Each chunk adds about 5 s of LibreOffice start-up.
  Small batches are not split. **Cancel** stops after the current file and keeps
  what is already converted.
- **Thumbnails** are rendered only for visible rows plus a margin.
- **Caches** for thumbnails and open documents have a fixed size, so memory and
  file handles do not grow with the batch.

Measured on Windows with LibreOffice 26:

| Scenario | Result |
|---|---|
| 600 PDFs converted | 0.8 s, 0 failures |
| …loaded into the window | 0.6 s (600 source rows, 1200 page rows) |
| …source list rebuild / page list rebuild | 0.06 s / 0.11 s |
| …thumbnails rendered | 40 of 1200 |
| 240 Office documents converted | 156 s, 0 failures; **65 s with 4 workers** |
| …longest gap between progress updates | 16 s |
| …loaded into the window | 0.17 s |
| 3,000 PDFs converted / loaded / assembled | 3.9 s / 3.0 s / 8.9 s (6,000 pages) |
| 10,000 PDFs converted / loaded / assembled | 13.8 s / 10.7 s / 32.4 s (20,000 pages) |
| …source list rebuild / page list rebuild at 10,000 rows | 2.0 s / 1.1 s |
| …thumbnails rendered at 20,000 rows | 40 |

About 1,000 Office documents take roughly 11 minutes to convert. Everything
after conversion is close to instant.

**Known limit.** At 10,000 files a full rebuild of the source list takes 2–3 s
and gets slightly slower with each rebuild. This is Qt item overhead on Windows,
not a Python memory leak. Editing, thumbnails and preview are not affected. The
fix would be to update only changed rows instead of rebuilding the tree. It is
not done yet because at normal sizes a rebuild takes 0.06 s.

### Email extraction

| Option | Effect |
|---|---|
| Message body | Headers and body become a page |
| PDF documents | Attached PDFs are copied in unchanged |
| Images | Attached images are converted; inline signature images are skipped |
| Documents | Attached Word/Excel/PowerPoint, HTML, text, Markdown and CSV files are converted |
| Embedded messages | Attached `.msg`/`.eml` files are opened and their contents extracted, up to `--max-depth` levels (default 4) |
| Other attachments | Listed on the body page, not converted |

All options are on by default except *Other attachments*.

### Attached emails

An attached email (for example a forward) is opened, and its body and
attachments become pages. If email A carries email B, and B has an attachment,
that attachment is listed under B, not A.

Two implementation details make this work:

- `_walk_parts` stops `Message.walk()` from descending into attached messages.
  Otherwise B's attachments would be listed again as A's.
- `_decode_part` re-serialises attached messages, because
  `get_payload(decode=True)` returns `None` for `message/rfc822`, which would
  produce an empty file.

Tests cover a three-level chain (A → B → C → PDF).

### Outlook items

Outlook can attach items that are not emails: meetings, contacts, tasks, or
documents stored in a mail folder. Each is read by type. Its body becomes a page
headed with the item type (for example "Appointment" or "Contact") and its main
fields. Its attachments are extracted like an email's.

- The `.msg` file is opened in lenient mode. In strict mode, `extract-msg`
  fails on the whole message if one attached item has an unsupported class
  (`IPM.Document.*`, `IPM.Outlook.Recall`) or has no data stream.
- Attached items are parsed directly from the parent message, not exported and
  re-parsed.
- No attachment is skipped silently. Empty, damaged or cloud (link-only)
  attachments each get a row with the reason, and are listed on the body page.

### Output check

Assembly resizes, rotates and adds page numbers, and any of these can drop
content without raising an error. After the PDF is written, each output page is
compared with its source page using pdfium:

- every word on the source page must appear on the output page (extra words,
  such as page numbers, are allowed)
- the output page must have at least as many images
- no source text may fall outside the output page

Failures are shown in the status bar, the banner, the *Done* dialog, and on the
command line, for example:
`MISSING page 4 (mail - invoice.pdf, page 2): 12 of 300 word(s) missing...`.
The check takes about 2 ms per page. It cannot check text drawn as vector
outlines.

## Command line

```sh
consolidator build <paths...> -o out.pdf [options]
consolidator check
consolidator gui [paths...]
consolidator selftest
```

`check` reports the detected LibreOffice. `selftest` converts generated samples
end to end and reports the result.

| Option | Meaning |
|---|---|
| `--page-size` | `letter` (default), `a4`, `legal`, `original` |
| `--margin` | margin in inches when fitting to a page size (default `0.5`) |
| `--separator` | `none`, `blank`, or `title` page between documents |
| `--page-numbers` | `none`, `continuous`, or `per-document` |
| `--number-format` | template using `{n}` `{total}` `{doc}` `{n_doc}` `{total_doc}` `{date}` |
| `--number-position` | e.g. `bottom-center`, `top-right` |
| `--number-start` | first page number (default `1`) |
| `--header-*`, `--footer-*` | text for the `left`/`center`/`right` slot |
| `--title`, `--author`, `--subject` | PDF metadata |
| `--extract` | email attachment categories, comma separated: `body`, `pdf`, `images`, `documents`, `embedded`, `other` (`office` is an old name for `documents`) |
| `--no-body` | omit the message body page |
| `--max-attachment-mb` | skip attachments larger than this (default `64`) |
| `--max-depth` | maximum nested-message depth |
| `--no-bookmarks` | do not add a bookmark per document |
| `--no-recursive` | do not search subfolders |
| `--keep-scratch` | keep intermediate PDFs |

## Building the standalone bundle

```sh
.venv\Scripts\python tools\build.py
```

This produces `dist/PDFConsolidator/`, containing the application and a trimmed
LibreOffice. Copy the folder to the target machine and run
`PDFConsolidator.exe`. Python and Office are not needed. `--zip` also writes
`PDFConsolidator.zip`.

### Publisher name

Windows shows "Unknown Publisher" for unsigned executables. The publisher name
comes from the signing certificate, so the build:

1. embeds a version resource (`build/version_info.txt`) with company name,
   product name and copyright, shown under *Properties → Details*;
2. signs the executable with a self-signed certificate whose subject is the
   publisher name.

```sh
python tools/build.py --publisher "Xiaoyu-Ben-Wang"       # default
python tools/build.py --no-sign                            # skip signing
python tools/build.py --trust                              # also trust it locally
```

`tools/sign.ps1` signs an existing executable on its own.

A self-signed certificate shows the publisher name but is not trusted by
default, so Windows still warns. `--trust` adds the certificate to the local
Trusted Root and Trusted Publishers stores (requires administrator rights). On
other machines, install `pdfconsolidator-publisher.cer` the same way. Only a
certificate from a public certificate authority removes the warning everywhere.

## Supported formats

| Input | Converted by |
|---|---|
| `.docx` `.doc` `.docm` `.dotx` `.dotm` `.xlsx` `.xls` `.xlsm` `.xltx` `.xltm` `.pptx` `.ppt` `.pptm` `.pps` `.ppsx` `.rtf` `.odt` `.ods` `.odp` `.odg` | LibreOffice |
| `.html` `.htm` `.xhtml` | LibreOffice (rendered, not shown as source) |
| `.msg` | `extract-msg` (Outlook is not used) |
| `.eml` | Python standard library |
| `.jpg` `.jpeg` `.png` `.tif` `.tiff` `.bmp` `.gif` `.webp` | `img2pdf` (JPEGs embedded without re-encoding) |
| `.txt` `.md` `.markdown` `.log` `.csv` | plain monospaced text (Markdown is not rendered) |
| `.pdf` | copied unchanged |

Not supported: `.pub`, `.xlsb`, HEIC, OCR.

Supported extensions are defined once in `convert.py` (`OFFICE_EXTS`,
`WEB_EXTS`, `TEXT_EXTS`, `IMAGE_EXTS`, `PDF_EXTS`, `EMAIL_EXTS`, and
`DOCUMENT_EXTS` for the first three combined). The file dialog filter, the
`--extract` categories and the support checks all read these sets, so a new
format only needs to be added there.

## Design notes

Processing has two separate phases:

1. **Convert.** Each source becomes one or more PDF segments. Office documents
   and email bodies are converted in batched LibreOffice runs, because start-up
   time dominates. Sources are copied to unique names first so files with the
   same name do not collide.
2. **Assemble.** Segments are merged and fitted to the page size. This phase
   does not use LibreOffice, so re-running it is fast.

LibreOffice issues handled in the code:

- **`PYTHONPATH`.** LibreOffice inherits the caller's environment. If
  `PYTHONPATH` is set, LibreOffice's bundled Python breaks with
  `source file could not be loaded`. It is removed before launch.
- **Relative paths.** LibreOffice resolves paths against its own working
  directory, so relative input or `--outdir` paths fail with the same error.
  `convert_batch` converts them to absolute paths and returns results keyed by
  the caller's original paths.
- **Temp directory.** The app does not use the system temp folder. It creates
  `.scratch` next to itself and falls back only if that is not writable.
  LibreOffice gives the same misleading error when it cannot write work files.
- **Long paths.** LibreOffice does not support paths over `MAX_PATH`, and its
  profile contains deep paths. The profile is kept under a short fixed folder
  (`.lo-profiles` next to the app) and deleted after each run.

Page `/Rotate` values are applied to the page content first, so page numbers
added later are not rotated with the page. Order: apply rotation, rotate,
scale, add page numbers and headers.

## Tests

```sh
.venv\Scripts\python -m pytest tests -q
```

Tests that need LibreOffice or fixtures are skipped when those are missing.
Generate fixtures with `tools/make_fixtures.py`, `tools/make_email_fixtures.py`
and `tools/get_msg_samples.py`.

## About

PDF Consolidator 0.1.0. Copyright (C) 2026 Xiaoyu-Ben-Wang.

The **About** dialog in the app shows the version, copyright, licence and
warranty disclaimer, and links to every third-party licence.

## Licence and warranty

### No warranty

**The software is provided "as is," without any express or implied warranty.
The authors are not liable for any damages.** This includes any warranty of
merchantability or fitness for a particular purpose. Use it at your own risk,
including the risk of losing documents or data.

### Licence

PDF Consolidator is licensed under the **GNU General Public License, version 3
or later** (GPL-3.0-or-later). The full text is in [`LICENSE`](LICENSE).
Sections 15 and 16 of the GPL contain the formal warranty disclaimer and
limitation of liability.

The GPL is required because `extract-msg` (the `.msg` reader) is GPL-3.0. All
other dependencies are GPL-compatible:

| Component | Licence |
|---|---|
| extract-msg | GPL-3.0 |
| Qt (PySide6), img2pdf, RTFDE | LGPL-3.0 |
| pikepdf | MPL-2.0 |
| LibreOffice (bundled) | MPL-2.0 |
| pypdf, reportlab, Pillow, pywin32 and others | permissive (BSD, MIT, PSF and similar) |

[`THIRD-PARTY-NOTICES.md`](THIRD-PARTY-NOTICES.md) lists every component, and
[`LICENSES/`](LICENSES) contains each licence text unchanged.

### Files shipped with the bundle

Next to the executable: `LICENSE`, `THIRD-PARTY-NOTICES.md`, `LICENSES/`, and
LibreOffice's own notices under `libreoffice/`.

`tools/collect_licenses.py` builds the notices from the installed packages, so
new dependencies are picked up automatically. The build copies the notices and
checks that every bundled module is covered:

```sh
.venv\Scripts\python tools\collect_licenses.py
.venv\Scripts\python tools\build.py
```

Qt is used under LGPL-3.0. Its wheels only include a commercial licence
reference, so the LGPL text is added as `LICENSES/LGPL-3.0.txt`. The Qt DLLs in
`_internal/PySide6/` can be replaced with another build of the same version, as
the LGPL requires.

### Source code

GPL-3.0 section 6 requires the source to be available to anyone who receives
the binaries. This repository is the full source, including build scripts.

Before publishing the bundle, set `SOURCE_URL` at the top of
`tools/collect_licenses.py` to the repository URL and re-run it, so the notices
include the link. LibreOffice is redistributed unmodified apart from trimming;
its source is published by The Document Foundation.
