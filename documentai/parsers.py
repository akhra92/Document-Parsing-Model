"""Stage 2 of the pipeline: extract text, Markdown and structured JSON from a PDF."""

from __future__ import annotations

import json as json_lib
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from .exceptions import ParseError

__all__ = [
    "MARKDOWN_ENGINE",
    "OUTPUT_FORMATS",
    "ParsedDocument",
    "extract",
    "extract_json",
    "extract_markdown",
    "extract_text",
    "markdown_engine",
    "parse_pdf",
]

#: Output format name -> file extension.
OUTPUT_FORMATS: dict[str, str] = {"text": ".txt", "markdown": ".md", "json": ".json"}

#: Accepted spellings on the CLI.
_ALIASES = {"txt": "text", "text": "text", "md": "markdown", "markdown": "markdown",
            "json": "json"}

_PAGE_BREAK = "\f"

#: The Markdown engine this package is pinned to and tested against.
#:
#: pymupdf4llm ships two: the *layout* engine (``pymupdf-layout``, an ONNX model
#: that recovers reading order, headings and tables) and the older font-size
#: *legacy* engine. They produce noticeably different Markdown for the same PDF,
#: so the choice is made explicitly here rather than left to whichever one
#: pymupdf4llm happens to default to. See :func:`markdown_engine`.
MARKDOWN_ENGINE = "layout"

#: Bit 4 of a span's ``flags`` marks bold, bit 1 marks italic.
_FLAG_BOLD = 2 ** 4
_FLAG_ITALIC = 2 ** 1


@dataclass
class ParsedDocument:
    """Extracted representations of one PDF."""

    path: Path
    page_count: int
    text: str | None = None
    markdown: str | None = None
    json: str | None = None
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
    formats: list[str] | tuple[str, ...] = ("text", "markdown", "json"),
    *,
    image_dir: str | Path | None = None,
    image_link_base: str | None = None,
    spans: bool = True,
    sort: bool = True,
) -> ParsedDocument:
    """Open ``pdf_path`` once and extract every requested format from it.

    ``image_dir`` turns on image extraction for the Markdown output;
    ``image_link_base`` is the prefix written into the Markdown links (use a
    relative one so the ``.md`` stays portable). ``spans`` keeps the per-span
    font detail in the JSON output.
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
        if "markdown" in wanted:
            parsed.markdown, parsed.images = extract_markdown(
                doc, image_dir=image_dir, image_link_base=image_link_base
            )
        if "json" in wanted:
            parsed.json = extract_json(doc, source=pdf_path, spans=spans, sort=sort)
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
    for page in doc.pages():
        pages.append(page.get_text("text", sort=sort).rstrip())
    return (_PAGE_BREAK + "\n").join(pages).rstrip() + "\n"


def extract_json(
    doc: pymupdf.Document,
    *,
    source: str | Path | None = None,
    spans: bool = True,
    sort: bool = True,
    indent: int | None = 2,
) -> str:
    """Structured JSON: document metadata plus per-page text blocks with geometry.

    Every coordinate is in PDF points with the origin at the top-left of the
    page. Image blocks carry their placement and size but never the raw bytes -
    use ``--images`` for those.
    """
    payload = {
        "source": Path(source).name if source else None,
        "page_count": doc.page_count,
        "metadata": {k: v for k, v in (doc.metadata or {}).items() if v},
        "pages": [_page_payload(page, spans=spans, sort=sort) for page in doc.pages()],
    }
    text = json_lib.dumps(payload, indent=indent, ensure_ascii=False)
    return (_inline_number_arrays(text) if indent else text) + "\n"


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
    _select_markdown_engine(pymupdf4llm)

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


def markdown_engine() -> str:
    """Report which Markdown engine :func:`extract_markdown` will run.

    ``"layout"`` or ``"legacy"`` are the two pymupdf4llm engines (see
    :data:`MARKDOWN_ENGINE`); ``"heuristic"`` means pymupdf4llm is not
    installed and the built-in font-size fallback is used instead.
    """
    try:
        import pymupdf4llm
    except ImportError:
        return "heuristic"
    _select_markdown_engine(pymupdf4llm)
    return "layout" if getattr(pymupdf4llm, "_use_layout", False) else "legacy"


_engine_selected = False


def _select_markdown_engine(pymupdf4llm) -> None:
    """Pin pymupdf4llm to :data:`MARKDOWN_ENGINE`, once per process."""
    global _engine_selected
    if _engine_selected:
        return
    pymupdf4llm.use_layout(MARKDOWN_ENGINE == "layout")
    _engine_selected = True


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _page_payload(page: pymupdf.Page, *, spans: bool, sort: bool) -> dict:
    """One page as plain data: its text, and every block with its bounding box."""
    raw = page.get_text("dict", sort=sort)
    blocks = []
    for block in raw["blocks"]:
        entry: dict = {
            "number": block["number"],
            "type": "image" if block["type"] == 1 else "text",
            "bbox": _round_all(block["bbox"]),
        }
        if block["type"] == 1:
            entry.update(
                width=block.get("width"),
                height=block.get("height"),
                ext=block.get("ext"),
            )
        else:
            lines = [_line_payload(line, spans=spans) for line in block["lines"]]
            entry["text"] = "\n".join(line["text"] for line in lines).strip()
            entry["lines"] = lines
        blocks.append(entry)

    return {
        "number": page.number + 1,
        "width": round(raw["width"], 2),
        "height": round(raw["height"], 2),
        "text": page.get_text("text", sort=sort).strip(),
        "blocks": blocks,
    }


def _line_payload(line: dict, *, spans: bool) -> dict:
    payload = {
        "bbox": _round_all(line["bbox"]),
        "text": "".join(span["text"] for span in line["spans"]),
    }
    if spans:
        payload["spans"] = [
            {
                "text": span["text"],
                "font": span["font"],
                "size": round(span["size"], 2),
                "bold": bool(span["flags"] & _FLAG_BOLD),
                "italic": bool(span["flags"] & _FLAG_ITALIC),
                "color": f"#{span['color']:06x}",
                "bbox": _round_all(span["bbox"]),
            }
            for span in line["spans"]
        ]
    return payload


def _round_all(box) -> list[float]:
    return [round(value, 2) for value in box]


def _inline_number_arrays(text: str) -> str:
    """Put pretty-printed arrays of plain numbers (the bboxes) back on one line."""
    def collapse(match: re.Match) -> str:
        numbers = (part.strip() for part in match.group(1).split(","))
        return "[" + ", ".join(numbers) + "]"

    return _NUMBER_ARRAY_RE.sub(collapse, text)


#: An indented array holding only numbers - i.e. a bbox. The required newlines
#: keep this from ever matching inside a string literal, where JSON escapes them.
_NUMBER_ARRAY_RE = re.compile(r"\[\n\s*(-?\d[\d.,\s\n-]*?)\n\s*\]")


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
    for page in doc.pages():
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                for span in line["spans"]:
                    if span["text"].strip():
                        sizes[round(span["size"], 1)] += len(span["text"])
    body_size = sizes.most_common(1)[0][0] if sizes else 11.0

    chunks: list[str] = []
    for number, page in enumerate(doc.pages(), start=1):
        if number > 1:
            chunks.append("\n---\n")
        for block in page.get_text("dict")["blocks"]:
            lines = []
            max_size = 0.0
            bold = True
            for line in block.get("lines", []):
                spans = [s for s in line["spans"] if s["text"].strip()]
                if not spans:
                    continue
                lines.append("".join(s["text"] for s in spans).strip())
                max_size = max(max_size, *(s["size"] for s in spans))
                bold &= all(s["flags"] & _FLAG_BOLD for s in spans)
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
