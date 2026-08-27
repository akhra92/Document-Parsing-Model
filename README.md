# DocumentAI

A document parsing pipeline built on **PyMuPDF**. Every input is normalised to
PDF first, then parsed into **plain text**, **HTML** and **Markdown**.

```
input (any supported format) ──▶ stage 1: convert to PDF ──▶ stage 2: parse ──┬─▶ .txt
                                                                              ├─▶ .html
                                                                              └─▶ .md
```

A PDF input skips the conversion work and goes straight to parsing.

## Install

```bash
conda env create -f environment.yml      # creates the "documentai" env
conda activate documentai
pip install -e .
```

Office inputs (`.docx`, `.pptx`, `.xlsx`, `.odt`, …) additionally need
[LibreOffice](https://www.libreoffice.org/). It is auto-detected on the PATH and
in the usual install locations; otherwise point `--soffice` or the
`DOCUMENTAI_SOFFICE` environment variable at the executable.

## Usage

```bash
# one file, all three formats, into ./output
documentai contract.docx

# a whole tree, Markdown only, keeping the intermediate PDFs
documentai ./inbox -r -f md -o ./parsed --keep-pdf

# extract embedded images and write a JSON run summary
documentai report.pdf --images --manifest
```

Equivalent module form: `python -m documentai ...`

| Option | Effect |
| --- | --- |
| `-o, --output DIR` | where extracted files land (default `output`) |
| `-f, --formats` | any of `text` `html` `markdown` (aliases `txt` `md` `htm`) |
| `-r, --recursive` | descend into subdirectories of directory inputs |
| `--keep-pdf` | keep the intermediate PDF under `OUTPUT/pdf/` |
| `--images` | write embedded images to `OUTPUT/images/<stem>/` and link them from the Markdown |
| `--manifest [PATH]` | JSON summary of the run |
| `--soffice PATH` | LibreOffice executable for Office inputs |
| `--timeout SEC` | per-document LibreOffice timeout (default 180) |
| `--no-overwrite` | fail instead of replacing existing outputs |

Exit code is `0` when every document succeeded, `1` when any failed, `2` on a
usage error. One bad file never aborts a batch.

### Output layout

```
output/
├── report.txt          # plain text, pages separated by \f
├── report.html         # self-contained, one <section class="page"> per page
├── report.md           # Markdown (headings, lists, tables)
├── images/report/…     # only with --images
├── pdf/report.pdf      # only with --keep-pdf
└── manifest.json       # only with --manifest
```

## Python API

```python
from documentai import DocumentPipeline

pipeline = DocumentPipeline("output", formats=["text", "markdown"], keep_pdf=True)
result = pipeline.run("contract.docx")

print(result.strategy)                 # "libreoffice"
print(result.page_count)               # 12
print(result.outputs["markdown"].read_text(encoding="utf-8"))
```

Failures surface on the result object rather than as exceptions, so batches keep
going:

```python
for result in pipeline.run_many(["a.pdf", "b.pptx", "c.epub"]):
    print(result.source.name, "ok" if result.ok else result.error)
```

Lower-level pieces are usable on their own:

```python
from documentai import convert_to_pdf, extract, parse_pdf

convert_to_pdf("slides.pptx", "slides.pdf")
markdown = extract("slides.pdf", "md")
parsed = parse_pdf("slides.pdf", ["text", "html"])   # opens the file once
```

## Supported inputs

| Category | Extensions | Conversion strategy |
| --- | --- | --- |
| PDF | `.pdf` | passed through untouched |
| PyMuPDF-native | `.epub` `.xps` `.oxps` `.mobi` `.fb2` `.cbz` | re-emitted as PDF by PyMuPDF |
| Images | `.png` `.jpg` `.jpeg` `.bmp` `.gif` `.tif` `.tiff` `.webp` `.jp2` `.psd` … | single-page PDF per image |
| Text & markup | `.txt` `.md` `.html` `.htm` `.rst` `.json` `.yaml` `.xml` `.log` | laid out by PyMuPDF's `Story` engine |
| Office | `.docx` `.doc` `.odt` `.rtf` `.pptx` `.ppt` `.odp` `.xlsx` `.xls` `.ods` `.csv` | headless LibreOffice |

`documentai --help` prints the full list. Unsupported extensions are skipped
when scanning a directory, and reported as an error when named explicitly.

## How each format is produced

- **Text** — `page.get_text("text", sort=True)` per page, joined with form feeds.
- **HTML** — PyMuPDF's HTML extraction per page (absolutely positioned spans,
  base64-inlined images) wrapped in one self-contained document. No external
  assets, so the file opens anywhere.
- **Markdown** — `pymupdf4llm`, which reconstructs headings, lists and tables.
  If it is not installed, a built-in font-size heuristic takes over so the
  pipeline still produces Markdown.

Scanned PDFs contain no text layer; run OCR upstream if you need one.

## Tests

```bash
conda activate documentai
pytest
```

The LibreOffice test is skipped automatically when LibreOffice is absent.

## Project layout

```
documentai/
├── formats.py      input extension → conversion strategy registry
├── converters.py   stage 1: anything → PDF
├── parsers.py      stage 2: PDF → text / HTML / Markdown
├── pipeline.py     orchestration, batching, manifest
├── cli.py          argparse front end
└── exceptions.py   error hierarchy
```
