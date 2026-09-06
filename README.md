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
| `GET /health` | liveness, version, whether LibreOffice is available, Markdown engine |
| `GET /formats` | accepted inputs, available outputs, size and concurrency limits |
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
batch — whether it is unsupported, empty, over the size limit or simply broken,
it comes back with `"ok": false`, an `error` and an `error_type`, and the
response is still 200:

```jsonc
{
  "documents": 2, "succeeded": 1, "failed": 1,
  "results": [
    { "filename": "report.docx", "stem": "report", "ok": true,
      "strategy": "libreoffice", "page_count": 3, "duration": 3.28,
      "outputs": { "markdown": "# Quarterly Report…" } },
    { "filename": "notes.html", "stem": "notes", "ok": false,
      "error": "no conversion strategy for '.html' (notes.html)",
      "error_type": "UnsupportedFormatError" }
  ]
}
```

### Running in Docker

The `Dockerfile` builds the API with LibreOffice and fonts, running as an
unprivileged user with a health check on `/health`:

```bash
docker compose up --build          # http://localhost:8000/docs
# or without compose
docker build -t documentai-api .
docker run --rm -p 8000:8000 -e DOCUMENTAI_MAX_CONCURRENCY=4 documentai-api
```

`docker-compose.yml` sets the environment variables above, a 2 GB memory
limit and a tmpfs for the working directory. Keep the concurrency in step with
the memory limit. The image installs the same apt packages Streamlit Community
Cloud does (`packages.txt`), so both deployments convert Office files alike.

## Web app

`app.py` is a Streamlit front end: upload documents, pick formats, preview the
results and download them individually or as a ZIP.

```bash
streamlit run app.py
```

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
| `--images` | write embedded images to `OUTPUT/images/<stem>/` and link them from the Markdown (requires the `markdown` format) |
| `--no-spans` | drop the per-span font detail from the JSON (much smaller files) |
| `--manifest [PATH]` | JSON summary of the run |
| `--soffice PATH` | LibreOffice executable for Office inputs |
| `--timeout SEC` | per-document LibreOffice timeout (default 180); on expiry the whole LibreOffice process tree is killed, not just the launcher |
| `--no-overwrite` | fail instead of replacing existing outputs (checked before any work, and covering the kept PDF and images too) |

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

## Supported inputs

| Category | Extensions | Conversion strategy |
| --- | --- | --- |
| PDF | `.pdf` | passed through untouched |
| PyMuPDF-native | `.epub` `.xps` `.oxps` `.mobi` `.fb2` `.cbz` | re-emitted as PDF by PyMuPDF |
| Images | `.png` `.jpg` `.jpeg` `.bmp` `.gif` `.tif` `.tiff` `.webp` `.jp2` `.psd` … | single-page PDF per image |
| Office | `.docx` `.doc` `.odt` `.rtf` `.pptx` `.ppt` `.odp` `.xlsx` `.xls` `.ods` `.csv` | headless LibreOffice |

`documentai --help` prints the full list. Unsupported extensions are skipped
when scanning a directory, and reported as an error when named explicitly.

## Project layout

```
api.py              FastAPI HTTP service
Dockerfile          the API with LibreOffice, for docker compose up
app.py              Streamlit web front end
documentai/
├── formats.py      input extension → conversion strategy registry
├── converters.py   stage 1: anything → PDF
├── parsers.py      stage 2: PDF → text / Markdown / JSON
├── pipeline.py     orchestration, batching, manifest
├── cli.py          argparse front end
└── exceptions.py   error hierarchy
```
