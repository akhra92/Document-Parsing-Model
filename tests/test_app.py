"""Smoke tests for the Streamlit entry point (``app.py``)."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parents[1] / "app.py")


@pytest.fixture
def app_module():
    """``app.py`` imported as a plain module - the UI stays behind ``main()``."""
    import app

    return app


def test_app_renders_without_uploads():
    at = AppTest.from_file(APP, default_timeout=60).run()

    assert not at.exception
    assert at.title[0].value == "📄 DocumentAI"
    assert "Upload one or more documents" in at.info[0].value
    assert set(at.multiselect[0].value) == {"text", "markdown", "json"}


def test_app_reports_missing_formats():
    at = AppTest.from_file(APP, default_timeout=60).run()
    at.multiselect[0].set_value([]).run()

    assert not at.exception
    assert "at least one output format" in at.error[0].value


def test_process_returns_outputs_in_memory(app_module, sample_pdf):
    payload = app_module.process(
        "sample.pdf", sample_pdf.read_bytes(), ("text", "json"), False, True, False
    )

    assert payload["ok"] and payload["page_count"] == 2
    assert "Heading 1" in payload["outputs"]["text"]
    assert json.loads(payload["outputs"]["json"])["page_count"] == 2
    assert payload["converted"] is False


def test_process_converts_non_pdf_input(app_module, sample_md):
    payload = app_module.process(
        "readme.md", sample_md.read_bytes(), ("text",), False, True, True
    )

    assert payload["ok"] and payload["strategy"] == "story"
    assert "Title" in payload["outputs"]["text"]
    assert payload["pdf"] and payload["pdf"].startswith(b"%PDF")


def test_process_reports_failure_without_raising(app_module):
    payload = app_module.process("mystery.zzz", b"x", ("text",), False, True, False)

    assert payload["ok"] is False
    assert "no conversion strategy" in payload["error"]


def test_zip_bundles_every_output(app_module, sample_pdf, illustrated_pdf):
    payloads = [
        app_module.process(
            "sample.pdf", sample_pdf.read_bytes(), ("text", "markdown"), False, True, False
        ),
        # Images are only written while extracting Markdown.
        app_module.process(
            "illustrated.pdf", illustrated_pdf.read_bytes(),
            ("text", "markdown"), True, True, False,
        ),
    ]

    with zipfile.ZipFile(io.BytesIO(app_module.build_zip(payloads))) as archive:
        names = archive.namelist()
        assert "sample.txt" in names and "sample.md" in names
        assert "illustrated.txt" in names
        assert any(name.startswith("images/illustrated/") for name in names)
        assert "Heading 1" in archive.read("sample.txt").decode("utf-8")
