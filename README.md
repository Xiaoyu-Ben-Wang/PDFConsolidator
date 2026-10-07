# PDF Consolidator

Turn a folder of mixed files — Word, Excel, PowerPoint, images, plain text and
existing PDFs — into a single merged PDF.

**Status: prototype 1** — a working command line tool. The GUI is not built yet.

## Requirements

- Windows
- Python 3.10+ (development only; the shipped build bundles its own)
- LibreOffice, for Office formats

## Install (development)

```sh
python -m venv .venv
.venv\Scripts\python -m pip install -e .
```

Generate sample input and run it:

```sh
.venv\Scripts\python tools\make_fixtures.py
.venv\Scripts\python -m consolidator build fixtures\generated -o out\combined.pdf
```

## Usage

```sh
consolidator build <paths...> -o out.pdf [options]
consolidator check
```

| Option | Meaning |
|---|---|
| `-o`, `--output` | destination PDF (required) |
| `--page-size` | `letter` (default), `a4`, `legal`, `original` |
| `--margin` | margin in inches when fitting to a page size (default `0.5`) |
| `--no-bookmarks` | skip the bookmark per source document |
| `--no-recursive` | do not descend into subfolders |
| `--keep-scratch` | keep intermediate PDFs for inspection |

Paths may be files, folders, or a mixture. Folders are scanned recursively.

`consolidator check` reports which conversion engine was detected.

## Supported formats

| Input | Engine |
|---|---|
| `.docx` `.doc` `.rtf` `.odt` `.xlsx` `.xls` `.ods` `.pptx` `.ppt` `.odp` | LibreOffice (headless) |
| `.jpg` `.jpeg` `.png` `.tif` `.tiff` `.bmp` `.gif` `.webp` | `img2pdf` — JPEGs are embedded losslessly, never re-encoded |
| `.txt` `.md` `.log` `.csv` | rendered as literal monospaced text; **Markdown is not interpreted** |
| `.pdf` | copied through untouched |

Not yet implemented: `.msg` / `.eml` (Outlook items and email), `.pub`, HEIC.

## Design notes

Two phases, deliberately separated:

1. **Convert** — each source becomes an intermediate PDF. Office formats are
   converted by LibreOffice in a single batched invocation, because its start-up
   cost dominates. Sources are staged under unique names so that two files with
   the same name in different folders cannot collide.
2. **Assemble** — the intermediate PDFs are merged, fitted to the target page
   size and bookmarked. This phase never touches LibreOffice, so re-running it is
   cheap.

Two things the code goes out of its way to handle, both discovered by testing:

- **LibreOffice inherits the launcher's environment.** If `PYTHONPATH` is set —
  which it is whenever this app is running from Python — LibreOffice's own
  bundled Python breaks and reports `source file could not be loaded`. Those
  variables are stripped before launch.
- **`%TEMP%` is not trustworthy.** LibreOffice needs to create work files, and
  when it cannot it reports the same misleading `source file could not be
  loaded` error. The engine is given its own scratch directory rather than
  inheriting the system one.

## Tests

```sh
.venv\Scripts\python -m pytest tests -q
```

LibreOffice-dependent tests skip automatically when no engine is installed.
