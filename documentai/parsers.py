"""Stage 2 of the pipeline: extract text, HTML and Markdown from a PDF."""

from __future__ import annotations

import html as html_lib
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from .exceptions import ParseError

__all__ = [
    "OUTPUT_FORMATS",
    "ParsedDocument",
    "extract",
    "extract_html",
    "extract_markdown",
    "extract_text",
    "parse_pdf",
]

#: Output format name -> file extension.
OUTPUT_FORMATS: dict[str, str] = {"text": ".txt", "html": ".html", "markdown": ".md"}

#: Accepted spellings on the CLI.
_ALIASES = {"txt": "text", "text": "text", "html": "html", "htm": "html",
            "md": "markdown", "markdown": "markdown"}

_PAGE_BREAK = "\f"
_BODY_RE = re.compile(r"<body[^>]*>(.*)</body>", re.DOTALL | re.IGNORECASE)

_HTML_SHELL = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
body {{ margin: 0; background: #e9e9ee; font-family: sans-serif; }}
.page {{ margin: 16px auto; background: #fff; box-shadow: 0 1px 4px rgba(0,0,0,.25); }}
.page > div {{ position: relative; }}
img {{ max-width: 100%; }}
</style>
</head>
<body>
{pages}
</body>
</html>
"""


@dataclass
class ParsedDocument:
    """Extracted representations of one PDF."""

    path: Path
    page_count: int
    text: str | None = None
    html: str | None = None
    markdown: str | None = None
    #: Image files written while extracting Markdown.
    images: list[Path] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    def get(self, fmt: str) -> str:
        value = getattr(self, normalize_format(fmt))
        if value is None:
            raise ParseError(f"format {fmt!r} was not extracted from {self.path.name}")
        return value


def normalize_format(fmt: str) -> str:
    """Map a user-supplied format name onto a canonical one."""
    try:
        return _ALIASES[fmt.strip().lower()]
    except KeyError:
        raise ParseError(
            f"unknown output format {fmt!r}; choose from {', '.join(OUTPUT_FORMATS)}"
        ) from None


def parse_pdf(
    pdf_path: str | Path,
    formats: list[str] | tuple[str, ...] = ("text", "html", "markdown"),
    *,
    image_dir: str | Path | None = None,
    image_link_base: str | None = None,
    sort: bool = True,
) -> ParsedDocument:
    """Open ``pdf_path`` once and extract every requested format from it.

    ``image_dir`` turns on image extraction for the Markdown output;
    ``image_link_base`` is the prefix written into the Markdown links (use a
    relative one so the ``.md`` stays portable).
    """
    pdf_path = Path(pdf_path)
    wanted = [normalize_format(f) for f in formats]
    if not wanted:
        raise ParseError("no output formats requested")

    try:
        doc = pymupdf.open(pdf_path)
    except Exception as exc:
        raise ParseError(f"could not open {pdf_path.name}: {exc}") from exc

    try:
        if doc.needs_pass:
            raise ParseError(f"{pdf_path.name} is password protected")

        parsed = ParsedDocument(
            path=pdf_path,
            page_count=doc.page_count,
            metadata=dict(doc.metadata or {}),
        )
        if "text" in wanted:
            parsed.text = extract_text(doc, sort=sort)
        if "html" in wanted:
            parsed.html = extract_html(doc, title=pdf_path.stem)
        if "markdown" in wanted:
            parsed.markdown, parsed.images = extract_markdown(
                doc, image_dir=image_dir, image_link_base=image_link_base
            )
        return parsed
    finally:
        doc.close()


def extract(pdf_path: str | Path, fmt: str, **kwargs) -> str:
    """Convenience wrapper returning a single format as a string."""
    fmt = normalize_format(fmt)
    return parse_pdf(pdf_path, [fmt], **kwargs).get(fmt)


# --------------------------------------------------------------------------- #
# Per-format extractors
# --------------------------------------------------------------------------- #


def extract_text(doc: pymupdf.Document, *, sort: bool = True) -> str:
    """Plain text, one form feed (``\\f``) between pages."""
    pages = []
    for page in doc:
        pages.append(page.get_text("text", sort=sort).rstrip())
    return (_PAGE_BREAK + "\n").join(pages).rstrip() + "\n"


def extract_html(doc: pymupdf.Document, *, title: str = "document") -> str:
    """A single self-contained HTML file, one ``div.page`` per PDF page.

    PyMuPDF emits absolutely positioned spans and base64-inlined images, so the
    result keeps the original layout without referencing any external asset.
    """
    pages = []
    for number, page in enumerate(doc, start=1):
        fragment = _page_body(page.get_text("html"))
        pages.append(f'<section class="page" id="page-{number}">\n{fragment}\n</section>')
    return _HTML_SHELL.format(title=html_lib.escape(title), pages="\n".join(pages))


def extract_markdown(
    doc: pymupdf.Document,
    *,
    image_dir: str | Path | None = None,
    image_link_base: str | None = None,
) -> tuple[str, list[Path]]:
    """Markdown text plus any image files written.

    Uses ``pymupdf4llm`` when installed - it reconstructs headings, lists and
    tables - and falls back to a font-size heuristic otherwise.
    """
    try:
        import pymupdf4llm
    except ImportError:
        return _heuristic_markdown(doc), []

    kwargs: dict = {"show_progress": False}
    images: list[Path] = []
    if image_dir is not None:
        image_dir = Path(image_dir)
        image_dir.mkdir(parents=True, exist_ok=True)
        before = set(image_dir.iterdir())
        kwargs.update(write_images=True, image_path=str(image_dir), image_format="png")

    try:
        markdown = pymupdf4llm.to_markdown(doc, **kwargs)
    except Exception as exc:
        raise ParseError(f"Markdown extraction failed: {exc}") from exc

    if image_dir is not None:
        images = sorted(set(image_dir.iterdir()) - before)
        markdown = _relink_images(markdown, image_dir, image_link_base)
    return markdown, images


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _page_body(page_html: str) -> str:
    """Strip the document wrapper PyMuPDF may put around a page's HTML."""
    match = _BODY_RE.search(page_html)
    return (match.group(1) if match else page_html).strip()


def _relink_images(markdown: str, image_dir: Path, link_base: str | None) -> str:
    """Rewrite the absolute image paths pymupdf4llm emits into ``link_base``."""
    if link_base is None:
        return markdown
    link_base = link_base.rstrip("/") + "/"
    for prefix in {str(image_dir), image_dir.as_posix()}:
        for separator in ("/", "\\"):
            markdown = markdown.replace(prefix + separator, link_base)
    return markdown


def _heuristic_markdown(doc: pymupdf.Document) -> str:
    """Minimal Markdown: promote spans larger than body text to headings."""
    sizes: Counter[float] = Counter()
    for page in doc:
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", ()):
                for span in line["spans"]:
                    if span["text"].strip():
                        sizes[round(span["size"], 1)] += len(span["text"])
    body_size = sizes.most_common(1)[0][0] if sizes else 11.0

    chunks: list[str] = []
    for number, page in enumerate(doc, start=1):
        if number > 1:
            chunks.append("\n---\n")
        for block in page.get_text("dict")["blocks"]:
            lines = []
            max_size = 0.0
            bold = True
            for line in block.get("lines", ()):
                spans = [s for s in line["spans"] if s["text"].strip()]
                if not spans:
                    continue
                lines.append("".join(s["text"] for s in spans).strip())
                max_size = max(max_size, *(s["size"] for s in spans))
                bold &= all(s["flags"] & 2 ** 4 for s in spans)
            if not lines:
                continue
            text = " ".join(lines).strip()
            ratio = max_size / body_size if body_size else 1.0
            if ratio >= 1.5:
                chunks.append(f"# {text}")
            elif ratio >= 1.25:
                chunks.append(f"## {text}")
            elif ratio >= 1.1 or (bold and len(text) < 80):
                chunks.append(f"### {text}")
            else:
                chunks.append(text)
    return "\n\n".join(chunks).strip() + "\n"
