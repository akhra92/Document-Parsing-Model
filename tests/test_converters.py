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


@pytest.mark.parametrize("name,content", [
    ("notes.txt", "Line one\nLine two\n"),
    ("readme.md", "# Title\n\n[link](https://example.com)\n"),
    ("page.html", "<html><body><h1>Report</h1></body></html>"),
    ("data.json", '{"a": 1}'),
])
def test_already_extracted_formats_are_rejected(name, content, tmp_path):
    """Text, Markdown and HTML carry their own structure.

    Rendering them to a page and re-deriving it loses information (links and
    tables especially), so they are not accepted as inputs.
    """
    source = tmp_path / name
    source.write_text(content, encoding="utf-8")

    with pytest.raises(UnsupportedFormatError):
        convert_to_pdf(source, tmp_path / "out.pdf")


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


def test_timeout_kills_the_whole_libreoffice_process_tree(tmp_path):
    """A stand-in ``soffice`` that spawns a child and hangs, like the real
    launcher does with ``soffice.bin``: after the timeout the child must be
    gone as well, not left orphaned."""
    import os
    import sys
    import time

    import psutil

    pid_file = tmp_path / "child.pid"
    child = (
        "import os, time; "
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid())); "
        "time.sleep(60)"
    )
    if os.name == "nt":
        stub = tmp_path / "soffice.bat"
        stub.write_text(f'@echo off\r\n"{sys.executable}" -c "{child}"\r\n')
    else:
        stub = tmp_path / "soffice"
        stub.write_text(f'#!/bin/sh\n"{sys.executable}" -c "{child}"\n')
        stub.chmod(0o755)
    source = tmp_path / "table.csv"
    source.write_text("a,b\n1,2\n", encoding="utf-8")

    started = time.perf_counter()
    with pytest.raises(ConversionError, match="timed out"):
        convert_to_pdf(source, tmp_path / "out.pdf", soffice=stub, timeout=2)
    assert time.perf_counter() - started < 30  # not the child's 60 s sleep

    child_pid = int(pid_file.read_text())
    deadline = time.time() + 5
    while time.time() < deadline and psutil.pid_exists(child_pid):
        time.sleep(0.1)
    assert not psutil.pid_exists(child_pid), "the grandchild survived the timeout"


@pytest.mark.skipif(find_soffice() is None, reason="LibreOffice not installed")
def test_office_input_uses_libreoffice(tmp_path):
    source = tmp_path / "table.csv"
    source.write_text("name,amount\nwidget,42\n", encoding="utf-8")
    out = tmp_path / "table.pdf"

    result = convert_to_pdf(source, out)

    assert result.strategy == "libreoffice"
    assert "widget" in _page_text(out)
