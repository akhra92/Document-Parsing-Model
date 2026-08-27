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

```bash
conda create -n documentai python=3.11
conda activate documentai
pip install -e .          # the package and its dependencies
pip install -r requirements.txt   # adds streamlit for the web app
```

Office inputs (`.docx`, `.pptx`, `.xlsx`, `.odt`, …) additionally need
[LibreOffice](https://www.libreoffice.org/). It is auto-detected on the PATH and
in the usual install locations; otherwise point `--soffice` or the
`DOCUMENTAI_SOFFICE` environment variable at the executable.

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
| Text & markup | `.txt` `.md` `.html` `.htm` `.rst` `.json` `.yaml` `.xml` `.log` | laid out by PyMuPDF's `Story` engine |
| Office | `.docx` `.doc` `.odt` `.rtf` `.pptx` `.ppt` `.odp` `.xlsx` `.xls` `.ods` `.csv` | headless LibreOffice |

`documentai --help` prints the full list. Unsupported extensions are skipped
when scanning a directory, and reported as an error when named explicitly.
HTML is an *input* format only — there is no HTML output.

## How each format is produced

- **Text** — `page.get_text("text", sort=True)` per page, joined with form feeds.
- **Markdown** — `pymupdf4llm`, which reconstructs headings, lists and tables.
  If it is not installed, a built-in font-size heuristic takes over so the
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

## Tests

```bash
conda activate documentai
pytest
```

The LibreOffice test is skipped automatically when LibreOffice is absent.

## Project layout

```
app.py              Streamlit web front end
documentai/
├── formats.py      input extension → conversion strategy registry
├── converters.py   stage 1: anything → PDF
├── parsers.py      stage 2: PDF → text / Markdown / JSON
├── pipeline.py     orchestration, batching, manifest
├── cli.py          argparse front end
└── exceptions.py   error hierarchy
```
