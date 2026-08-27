from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest


@pytest.fixture
def sample_pdf(tmp_path: Path) -> Path:
    """A two-page PDF with a large heading and body text."""
    path = tmp_path / "sample.pdf"
    doc = pymupdf.open()
    for number in (1, 2):
        page = doc.new_page()
        page.insert_text((72, 90), f"Heading {number}", fontsize=24)
        page.insert_text((72, 130), f"Body text on page {number}.", fontsize=11)
    doc.save(path)
    doc.close()
    return path


@pytest.fixture
def illustrated_pdf(tmp_path: Path, sample_png: Path) -> Path:
    """A one-page PDF with an embedded raster image."""
    path = tmp_path / "illustrated.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 90), "Figure below", fontsize=14)
    page.insert_image(pymupdf.Rect(72, 110, 312, 270), filename=str(sample_png))
    doc.save(path)
    doc.close()
    return path


@pytest.fixture
def sample_txt(tmp_path: Path) -> Path:
    path = tmp_path / "notes.txt"
    path.write_text("Line one\nLine two\n", encoding="utf-8")
    return path


@pytest.fixture
def sample_md(tmp_path: Path) -> Path:
    path = tmp_path / "readme.md"
    path.write_text("# Title\n\nSome **bold** body text.\n", encoding="utf-8")
    return path


@pytest.fixture
def sample_html(tmp_path: Path) -> Path:
    path = tmp_path / "page.html"
    path.write_text(
        "<html><body><h1>Report</h1><p>Paragraph body.</p></body></html>",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def sample_png(tmp_path: Path) -> Path:
    path = tmp_path / "picture.png"
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 120, 80))
    pix.clear_with(200)
    pix.save(path)
    return path
