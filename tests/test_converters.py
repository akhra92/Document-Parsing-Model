from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from documentai.converters import convert_to_pdf, find_soffice
from documentai.exceptions import ConversionError, UnsupportedFormatError


def _page_text(pdf: Path) -> str:
    with pymupdf.open(pdf) as doc:
        return "\n".join(page.get_text() for page in doc)


def test_pdf_input_is_passed_through(sample_pdf, tmp_path):
    out = tmp_path / "out" / "copy.pdf"
    result = convert_to_pdf(sample_pdf, out)

    assert result.strategy == "passthrough"
    assert result.converted is False
    assert out.read_bytes() == sample_pdf.read_bytes()


@pytest.mark.parametrize("fixture,needle", [
    ("sample_txt", "Line one"),
    ("sample_md", "Title"),
    ("sample_html", "Report"),
])
def test_text_like_inputs_become_pdfs(request, fixture, needle, tmp_path):
    source = request.getfixturevalue(fixture)
    out = tmp_path / "converted.pdf"

    result = convert_to_pdf(source, out)

    assert result.strategy == "story"
    assert result.converted is True
    assert out.exists() and out.stat().st_size > 0
    assert needle in _page_text(out)


def test_image_becomes_single_page_pdf(sample_png, tmp_path):
    out = tmp_path / "image.pdf"
    result = convert_to_pdf(sample_png, out)

    assert result.strategy == "pymupdf"
    with pymupdf.open(out) as doc:
        assert doc.page_count == 1


def test_unsupported_extension_raises(tmp_path):
    source = tmp_path / "archive.zzz"
    source.write_bytes(b"data")
    with pytest.raises(UnsupportedFormatError):
        convert_to_pdf(source, tmp_path / "out.pdf")


def test_missing_input_raises(tmp_path):
    with pytest.raises(ConversionError):
        convert_to_pdf(tmp_path / "nope.pdf", tmp_path / "out.pdf")


@pytest.mark.skipif(find_soffice() is None, reason="LibreOffice not installed")
def test_office_input_uses_libreoffice(tmp_path):
    source = tmp_path / "table.csv"
    source.write_text("name,amount\nwidget,42\n", encoding="utf-8")
    out = tmp_path / "table.pdf"

    result = convert_to_pdf(source, out)

    assert result.strategy == "libreoffice"
    assert "widget" in _page_text(out)
