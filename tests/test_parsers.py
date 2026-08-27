from __future__ import annotations

import pytest

from documentai.exceptions import ParseError
from documentai.parsers import extract, normalize_format, parse_pdf


def test_parse_pdf_extracts_every_format(sample_pdf):
    parsed = parse_pdf(sample_pdf)

    assert parsed.page_count == 2
    assert "Heading 1" in parsed.text and "Body text on page 2." in parsed.text
    assert parsed.text.count("\f") == 1  # one break between two pages

    assert parsed.html.lstrip().startswith("<!DOCTYPE html>")
    assert 'id="page-1"' in parsed.html and 'id="page-2"' in parsed.html
    assert "Heading 1" in parsed.html

    assert "Heading 1" in parsed.markdown


def test_markdown_marks_up_headings(sample_pdf):
    markdown = extract(sample_pdf, "md")
    assert any(line.startswith("#") and "Heading" in line for line in markdown.splitlines())


def test_only_requested_formats_are_extracted(sample_pdf):
    parsed = parse_pdf(sample_pdf, ["text"])
    assert parsed.text is not None
    assert parsed.html is None and parsed.markdown is None
    with pytest.raises(ParseError):
        parsed.get("html")


def test_format_aliases():
    assert normalize_format("TXT") == "text"
    assert normalize_format("md") == "markdown"
    assert normalize_format("htm") == "html"
    with pytest.raises(ParseError):
        normalize_format("xml")


def test_broken_pdf_raises(tmp_path):
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"not really a pdf")
    with pytest.raises(ParseError):
        parse_pdf(broken, ["text"])
