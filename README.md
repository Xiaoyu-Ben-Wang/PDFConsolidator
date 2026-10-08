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
.venv\Scripts\python -m pip install -e . PySide6-Essentials
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
  body, each extracted attachment. Unticking anything leaves it out. Select a
  row and press **Delete** or **Backspace** to remove it and its pages.
- **Pages** (middle) is a single vertical column, one row per page, each row
  showing the page number, the file it came from and any rotation. Rows are
  coloured by source document, so pages from one file read as a block.
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

Conversion happens automatically when files are added — only the new files are
converted, so page edits already made are never lost. **Convert** re-converts
everything from scratch. Changing the page size or layout afterwards only
re-assembles, so editing is cheap.

### Email extraction

| Option | Effect |
|---|---|
| Message body | the headers and body become a page |
| PDF documents | attached PDFs are merged **byte-for-byte**, so page counts are exact |
| Images | attached images are converted; **inline** signature images are excluded |
| Office documents | attached Word/Excel/PowerPoint files are converted |
| Embedded messages | attached `.msg`/`.eml` are expanded recursively (depth-limited) |
| Other attachments | listed on the body page, not converted |

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
| `--extract` | attachment categories to pull out of email, comma separated |
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
| `.docx` `.doc` `.rtf` `.odt` `.xlsx` `.xls` `.ods` `.pptx` `.ppt` `.odp` | LibreOffice |
| `.msg` | `extract-msg` (Outlook is never involved) |
| `.eml` | standard library |
| `.jpg` `.jpeg` `.png` `.tif` `.tiff` `.bmp` `.gif` `.webp` | `img2pdf` — JPEGs embedded losslessly |
| `.txt` `.md` `.log` `.csv` | literal monospaced text; **Markdown is not interpreted** |
| `.pdf` | copied through untouched |

Not supported: `.pub`, HEIC, OCR of scans.

## Design notes

Two phases, deliberately separated:

1. **Convert** — each source becomes one or more PDF *segments*. Office documents
   and any email bodies are converted in a single batched LibreOffice invocation,
   because start-up dominates. Sources are staged under unique names so two files
   with the same name cannot collide.
2. **Assemble** — segments are merged and fitted to the target page size. This
   phase never touches LibreOffice, so re-running it is nearly free.

Three things the code goes out of its way to handle, all found by testing:

- **LibreOffice inherits the launcher's environment.** If `PYTHONPATH` is set —
  which it is whenever this app runs from Python — LibreOffice's own bundled
  Python breaks and reports `source file could not be loaded`.
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
