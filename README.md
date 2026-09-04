# DocumentAI

A document parsing pipeline built on **PyMuPDF**. Every input is normalised to
PDF first, then parsed into **plain text**, **Markdown** and **structured JSON**.

```
input (any supported format) ──▶ stage 1: convert to PDF ──▶ stage 2: parse ──┬─▶ .txt
                                                                              ├─▶ .md
                                                                              └─▶ .json
```

A PDF input skips the conversion work and goes straight to parsing.

## Install

Python 3.10 or newer.

```bash
conda create -n documentai python=3.11
conda activate documentai
pip install -e .                 # the library and CLI
pip install -e ".[api,app,dev]"  # + HTTP API, Streamlit app, test and lint tools
```

The extras are `api` (FastAPI + uvicorn), `app` (Streamlit), and `dev` (pytest,
ruff, mypy, pre-commit). `requirements.txt` is separate — it is the manifest
Streamlit Community Cloud installs from, so it pins only what the deployed web
app needs.

PyMuPDF, `pymupdf4llm` and `pymupdf-layout` are pinned to one exact version in
both files, because `pymupdf4llm` itself requires the other two at its own
version. Bump all three together.

Office inputs (`.docx`, `.pptx`, `.xlsx`, `.odt`, …) additionally need
[LibreOffice](https://www.libreoffice.org/). It is auto-detected on the PATH and
in the usual install locations; otherwise point `--soffice` or the
`DOCUMENTAI_SOFFICE` environment variable at the executable.

## HTTP API

`api.py` is a FastAPI service over the same pipeline.

```bash
pip install -e ".[api]"
uvicorn api:app --reload
```

Interactive docs (Swagger UI) at `http://localhost:8000/docs`.

| Endpoint | Does |
| --- | --- |
| `GET /health` | liveness, version, whether LibreOffice is available |
| `GET /formats` | accepted inputs, available outputs, size limits |
| `POST /parse` | upload documents → extractions returned inline as JSON |
| `POST /convert` | upload one document → the PDF itself |
| `POST /bundle` | upload documents → a ZIP of the outputs, plus a manifest |

```bash
# extract Markdown from a Word document
curl -X POST "http://localhost:8000/parse?formats=markdown" -F "files=@report.docx"

# several files at once, downloaded as a ZIP with images and the PDFs
curl -X POST "http://localhost:8000/bundle?images=true&keep_pdf=true" \
     -F "files=@a.docx" -F "files=@b.pdf" -o output.zip
```

`/parse` returns each document separately, so one bad file never fails the
batch — it comes back with `"ok": false` and an `error`, and the response is
still 200:

```jsonc
{
  "documents": 2, "succeeded": 1, "failed": 1,
  "results": [
    { "filename": "report.docx", "ok": true, "strategy": "libreoffice",
      "page_count": 3, "duration": 3.28,
      "outputs": { "markdown": "# Quarterly Report…" } },
    { "filename": "notes.html", "ok": false,
      "error": "no conversion strategy for '.html' (notes.html)" }
  ]
}
```

JSON output is embedded as a real object, not a quoted string. Status codes:
**415** for an input format the pipeline does not accept, **413** over the size
or file-count limit, **422** for a bad format name or an empty upload.

Uploads are capped at 50 MB and 20 files per request (`MAX_UPLOAD_BYTES` and
`MAX_FILES` in `api.py`). Each request works in a temporary directory that is
deleted before the response is sent.

## Web app

`app.py` is a Streamlit front end: upload documents, pick formats, preview the
results and download them individually or as a ZIP.

```bash
streamlit run app.py
```

### Deploying to Streamlit Community Cloud

1. Push this repository to GitHub.
2. At [share.streamlit.io](https://share.streamlit.io) choose **Create app → Deploy
   a public app from GitHub**, select the repo and branch, and set the main file
   path to `app.py`.
3. Deploy. The first build takes several minutes because `packages.txt` installs
   LibreOffice.

The deployment files:

| File | Purpose |
| --- | --- |
| `app.py` | the entry point Streamlit Cloud runs |
| `requirements.txt` | Python dependencies (includes `streamlit`) |
| `packages.txt` | apt packages — LibreOffice, for the Office formats |
| `.streamlit/config.toml` | upload cap and theme |

The app degrades gracefully: if LibreOffice is unavailable the sidebar says so
and every non-Office format still works. Community Cloud gives an app ~1 GB of
memory, so `config.toml` caps uploads at 50 MB — a large scanned PDF can still
exhaust that. Uploads are processed in a temporary directory that is deleted as
soon as the outputs are read into memory; nothing persists on the server.

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
| `-f, --formats` | any of `text` `markdown` `json` (aliases `txt` `md`) |
| `-r, --recursive` | descend into subdirectories of directory inputs |
| `--keep-pdf` | keep the intermediate PDF under `OUTPUT/pdf/` |
| `--images` | write embedded images to `OUTPUT/images/<stem>/` and link them from the Markdown |
| `--no-spans` | drop the per-span font detail from the JSON (much smaller files) |
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
├── report.md           # Markdown (headings, lists, tables)
├── report.json         # per-page blocks with geometry and font detail
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
parsed = parse_pdf("slides.pdf", ["text", "json"])   # opens the file once
```

## Supported inputs

| Category | Extensions | Conversion strategy |
| --- | --- | --- |
| PDF | `.pdf` | passed through untouched |
| PyMuPDF-native | `.epub` `.xps` `.oxps` `.mobi` `.fb2` `.cbz` | re-emitted as PDF by PyMuPDF |
| Images | `.png` `.jpg` `.jpeg` `.bmp` `.gif` `.tif` `.tiff` `.webp` `.jp2` `.psd` … | single-page PDF per image |
| Office | `.docx` `.doc` `.odt` `.rtf` `.pptx` `.ppt` `.odp` `.xlsx` `.xls` `.ods` `.csv` | headless LibreOffice |

`documentai --help` prints the full list. Unsupported extensions are skipped
when scanning a directory, and reported as an error when named explicitly.

### Why text, Markdown and HTML are not inputs

Inputs are limited to **binary or paginated** formats — ones whose structure has
to be recovered from a rendered layout. Text, Markdown and HTML already carry
their structure explicitly (`<h1>`, `<table>`, `<a href>`), and this pipeline
reconstructs structure *statistically from geometry* — "this span is 1.5× body
size, therefore a heading". Feeding it markup would mean discarding known
structure and guessing it back. Measured on a 310-byte Markdown file, the
round trip lost:

| Input | After a `.md` → PDF → `.md` round trip |
| --- | --- |
| `[the dashboard](https://example.com/dash)` | `the dashboard` — **URL gone** |
| a 4-row Markdown table | `<!-- picture text -->Region Revenue<br>EU 1.2M…` |
| `> Margin held at 32%.` | plain paragraph, quote lost |
| ` ```python ` | ` ``` `, language lost |

The three remaining strategies are all faithful: they either preserve the
original pages or render a layout the source itself defines. Text, Markdown and
JSON are what this package *emits* — for those inputs, reading the file directly
is both lossless and faster.

## How each format is produced

- **Text** — `page.get_text("text", sort=True)` per page, joined with form feeds.
- **Markdown** — `pymupdf4llm` running its *layout* engine (`pymupdf-layout`,
  a small ONNX model that recovers reading order, headings, lists and tables).
  `pymupdf4llm` also ships an older font-size engine that emits different
  Markdown for the same PDF, so the package selects the layout engine
  explicitly (`documentai.MARKDOWN_ENGINE`) and a test guards the choice;
  `documentai.markdown_engine()` reports which one is active. If `pymupdf4llm`
  is not installed at all, a built-in font-size heuristic takes over so the
  pipeline still produces Markdown.
- **JSON** — `page.get_text("dict")` reshaped into a stable schema: document
  metadata, then each page with its text and blocks. Coordinates are PDF points
  with the origin at the page's top-left corner.

```jsonc
{
  "source": "report.pdf",
  "page_count": 2,
  "metadata": { "title": "…", "creationDate": "…" },
  "pages": [
    {
      "number": 1,
      "width": 595.0,
      "height": 842.0,
      "text": "Quarterly Report\nRevenue grew 18% …",
      "blocks": [
        {
          "number": 0,
          "type": "text",
          "bbox": [64.0, 71.9, 231.4, 92.0],
          "text": "Quarterly Report",
          "lines": [
            {
              "bbox": [64.0, 71.9, 231.4, 92.0],
              "text": "Quarterly Report",
              "spans": [
                {
                  "text": "Quarterly Report",
                  "font": "NimbusSans-Bold",
                  "size": 20.0,
                  "bold": true,
                  "italic": false,
                  "color": "#000000",
                  "bbox": [64.0, 71.9, 231.4, 92.0]
                }
              ]
            }
          ]
        },
        { "number": 5, "type": "image", "bbox": [64.0, 300.1, 320.0, 460.0],
          "width": 480, "height": 300, "ext": "png" }
      ]
    }
  ]
}
```

Image blocks carry placement and size but never raw bytes — use `--images` to
write the actual files. `--no-spans` drops the `spans` key when you only need
block text and geometry.

Scanned PDFs contain no text layer; run OCR upstream if you need one.

## Tests and checks

```bash
conda activate documentai
pip install -e ".[api,app,dev]"
pytest          # tests
ruff check .    # lint
mypy            # types (targets are set in pyproject.toml)
```

The LibreOffice test is skipped automatically when LibreOffice is absent, and
the API tests skip themselves when the `api` extra is not installed.

`pre-commit install` wires the same lint and type checks into every commit. CI
(`.github/workflows/ci.yml`) runs them plus the test suite on Python 3.10–3.13
with LibreOffice installed, so nothing is skipped there.

## Project layout

```
api.py              FastAPI HTTP service
app.py              Streamlit web front end
documentai/
├── formats.py      input extension → conversion strategy registry
├── converters.py   stage 1: anything → PDF
├── parsers.py      stage 2: PDF → text / Markdown / JSON
├── pipeline.py     orchestration, batching, manifest
├── cli.py          argparse front end
└── exceptions.py   error hierarchy
```
