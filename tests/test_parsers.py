from __future__ import annotations

import json

import pytest

from documentai.exceptions import ParseError
from documentai.parsers import (
    MARKDOWN_ENGINE,
    extract,
    markdown_engine,
    normalize_format,
    parse_pdf,
)


def test_parse_pdf_extracts_every_format(sample_pdf):
    parsed = parse_pdf(sample_pdf)

    assert parsed.page_count == 2
    assert "Heading 1" in parsed.text and "Body text on page 2." in parsed.text
    assert parsed.text.count("\f") == 1  # one break between two pages
    assert "Heading 1" in parsed.markdown
    assert json.loads(parsed.json)["page_count"] == 2


def test_json_describes_pages_and_blocks(sample_pdf):
    payload = json.loads(extract(sample_pdf, "json"))

    assert payload["source"] == "sample.pdf"
    assert payload["page_count"] == 2
    assert [page["number"] for page in payload["pages"]] == [1, 2]

    first = payload["pages"][0]
    assert first["width"] > 0 and first["height"] > 0
    assert "Heading 1" in first["text"]

    heading = next(b for b in first["blocks"] if "Heading 1" in b.get("text", ""))
    assert heading["type"] == "text"
    assert len(heading["bbox"]) == 4
    span = heading["lines"][0]["spans"][0]
    assert span["text"] == "Heading 1"
    assert span["size"] == 24.0
    assert span["color"].startswith("#") and "font" in span


def test_json_image_blocks_carry_geometry_not_bytes(illustrated_pdf):
    payload = json.loads(extract(illustrated_pdf, "json"))
    images = [b for b in payload["pages"][0]["blocks"] if b["type"] == "image"]

    assert images, "expected an image block"
    assert images[0]["width"] > 0 and images[0]["height"] > 0
    assert "image" not in images[0]  # raw bytes stay out of the JSON


def test_json_keeps_bboxes_on_one_line(sample_pdf):
    raw = extract(sample_pdf, "json")
    bbox_line = next(line for line in raw.splitlines() if '"bbox"' in line)

    assert bbox_line.rstrip(",").endswith("]")  # not split across six lines
    assert len(json.loads(bbox_line.strip().rstrip(",").split(": ", 1)[1])) == 4


def test_bracketed_numbers_in_page_text_survive(tmp_path):
    """The bbox-inlining pass must never reach inside a string literal."""
    import pymupdf

    path = tmp_path / "brackets.pdf"
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 90), "coords [64.0,   66.5] here", fontsize=11)
    doc.save(path)
    doc.close()

    # PyMuPDF normalises runs of spaces on the round-trip; the brackets and the
    # numbers between them are what must survive untouched.
    text = json.loads(extract(path, "json"))["pages"][0]["text"]
    assert text.startswith("coords [64.0,") and text.endswith("66.5] here")


def test_json_spans_can_be_omitted(sample_pdf):
    payload = json.loads(parse_pdf(sample_pdf, ["json"], spans=False).json)
    line = payload["pages"][0]["blocks"][0]["lines"][0]

    assert line["text"] and "spans" not in line


def test_markdown_marks_up_headings(sample_pdf):
    markdown = extract(sample_pdf, "md")
    assert any(line.startswith("#") and "Heading" in line for line in markdown.splitlines())


def test_markdown_runs_on_the_pinned_engine(sample_pdf):
    """pymupdf4llm's two engines emit different Markdown for the same PDF, so
    the package pins one and this test fails if a dependency change flips it."""
    pymupdf4llm = pytest.importorskip("pymupdf4llm")
    pymupdf = pytest.importorskip("pymupdf")

    assert MARKDOWN_ENGINE == "layout"
    assert markdown_engine() == "layout"

    extract(sample_pdf, "md")  # the engine is selected before the first extraction
    assert pymupdf4llm._use_layout is True
    assert callable(pymupdf._get_layout), "pymupdf-layout model was not activated"


def test_only_requested_formats_are_extracted(sample_pdf):
    parsed = parse_pdf(sample_pdf, ["text"])
    assert parsed.text is not None
    assert parsed.json is None and parsed.markdown is None
    with pytest.raises(ParseError):
        parsed.get("json")


def test_format_aliases():
    assert normalize_format("TXT") == "text"
    assert normalize_format("md") == "markdown"
    assert normalize_format("JSON") == "json"
    with pytest.raises(ParseError):
        normalize_format("html")


def test_broken_pdf_raises(tmp_path):
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"not really a pdf")
    with pytest.raises(ParseError):
        parse_pdf(broken, ["text"])
