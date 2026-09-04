"""Tests for the HTTP API (``api.py``)."""

from __future__ import annotations

import io
import zipfile

import pytest

pytest.importorskip("fastapi", reason="install the [api] extra to test the HTTP API")

from fastapi.testclient import TestClient

from api import MAX_FILES, app


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def _upload(path, field="files"):
    return {field: (path.name, path.read_bytes(), "application/octet-stream")}


def test_health(client):
    body = client.get("/health").json()

    assert body["status"] == "ok"
    assert isinstance(body["libreoffice"], bool)


def test_formats_lists_inputs_and_outputs(client):
    body = client.get("/formats").json()

    assert ".pdf" in body["inputs"] and ".docx" in body["inputs"]
    assert ".md" not in body["inputs"]  # already-extracted formats are not inputs
    assert body["outputs"] == ["text", "markdown", "json"]


def test_parse_returns_every_format_inline(client, sample_pdf):
    response = client.post("/parse", files=_upload(sample_pdf))
    body = response.json()

    assert response.status_code == 200
    assert body["documents"] == 1 and body["succeeded"] == 1
    result = body["results"][0]
    assert result["ok"] and result["page_count"] == 2
    assert "Heading 1" in result["outputs"]["text"]
    assert "Heading 1" in result["outputs"]["markdown"]
    # JSON comes back as a real object, not a quoted string.
    assert result["outputs"]["json"]["page_count"] == 2


def test_parse_honours_requested_formats(client, sample_pdf):
    body = client.post("/parse?formats=txt", files=_upload(sample_pdf)).json()

    assert set(body["results"][0]["outputs"]) == {"text"}


def test_parse_rejects_unknown_format(client, sample_pdf):
    response = client.post("/parse?formats=html", files=_upload(sample_pdf))

    assert response.status_code == 422
    assert "unknown output format" in response.json()["detail"]


def test_parse_converts_non_pdf_input(client, sample_png):
    result = client.post("/parse?formats=text", files=_upload(sample_png)).json()["results"][0]

    assert result["ok"] and result["strategy"] == "pymupdf"
    assert result["converted"] is True


def test_unsupported_input_is_reported_per_file(client, sample_pdf, tmp_path):
    bad = tmp_path / "mystery.zzz"
    bad.write_bytes(b"x")
    response = client.post(
        "/parse",
        files=[
            ("files", (sample_pdf.name, sample_pdf.read_bytes(), "application/pdf")),
            ("files", (bad.name, bad.read_bytes(), "application/octet-stream")),
        ],
    )
    body = response.json()

    # One bad file does not fail the batch.
    assert response.status_code == 200
    assert body["succeeded"] == 1 and body["failed"] == 1
    failure = next(r for r in body["results"] if not r["ok"])
    assert "no conversion strategy" in failure["error"]


def test_empty_upload_is_rejected(client, tmp_path):
    empty = tmp_path / "empty.pdf"
    empty.write_bytes(b"")
    response = client.post("/parse", files=_upload(empty))

    assert response.status_code == 422
    assert "empty" in response.json()["detail"]


def test_too_many_files_rejected(client, sample_pdf):
    payload = [
        ("files", (f"copy{n}.pdf", sample_pdf.read_bytes(), "application/pdf"))
        for n in range(MAX_FILES + 1)
    ]
    response = client.post("/parse", files=payload)

    assert response.status_code == 413
    assert "limit" in response.json()["detail"]


def test_convert_returns_a_pdf(client, sample_png):
    response = client.post("/convert", files=_upload(sample_png, field="file"))

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert "picture.pdf" in response.headers["content-disposition"]
    assert response.content.startswith(b"%PDF")


def test_convert_rejects_unsupported_format(client, tmp_path):
    bad = tmp_path / "mystery.zzz"
    bad.write_bytes(b"x")
    response = client.post("/convert", files=_upload(bad, field="file"))

    assert response.status_code == 415  # unsupported media type


def test_bundle_returns_a_zip(client, illustrated_pdf):
    response = client.post(
        "/bundle?formats=text&formats=markdown&images=true&keep_pdf=true",
        files=_upload(illustrated_pdf),
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"

    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = archive.namelist()
        assert "illustrated.txt" in names and "illustrated.md" in names
        assert "pdf/illustrated.pdf" in names
        assert "manifest.json" in names
        assert any(name.startswith("images/illustrated/") for name in names)


def test_filename_path_traversal_is_stripped(client, sample_pdf):
    response = client.post(
        "/parse?formats=text",
        files={"files": ("../../evil.pdf", sample_pdf.read_bytes(), "application/pdf")},
    )
    result = response.json()["results"][0]

    assert result["ok"]
    assert "/" not in result["filename"] and "\\" not in result["filename"]
    assert result["filename"] == "evil.pdf"
