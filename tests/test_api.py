"""Tests for the HTTP API (``api.py``)."""

from __future__ import annotations

import io
import json
import re
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
    assert body["markdown_engine"] == "layout"


def test_formats_lists_inputs_outputs_and_limits(client):
    body = client.get("/formats").json()

    assert ".pdf" in body["inputs"] and ".docx" in body["inputs"]
    assert ".md" not in body["inputs"]  # already-extracted formats are not inputs
    assert body["outputs"] == ["text", "markdown", "json"]
    assert body["max_files"] == MAX_FILES and body["max_concurrency"] >= 1


def test_settings_come_from_the_environment(monkeypatch):
    from api import _env_number

    monkeypatch.delenv("DOCUMENTAI_MAX_FILES", raising=False)
    assert _env_number("DOCUMENTAI_MAX_FILES", 20, minimum=1) == 20

    monkeypatch.setenv("DOCUMENTAI_MAX_FILES", "3")
    assert _env_number("DOCUMENTAI_MAX_FILES", 20, minimum=1) == 3
    monkeypatch.setenv("DOCUMENTAI_QUEUE_TIMEOUT", "0")
    assert _env_number("DOCUMENTAI_QUEUE_TIMEOUT", 30.0, minimum=0.0) == 0.0

    # A bad value must stop the service from starting, not be silently ignored.
    monkeypatch.setenv("DOCUMENTAI_MAX_FILES", "many")
    with pytest.raises(RuntimeError, match="must be a number"):
        _env_number("DOCUMENTAI_MAX_FILES", 20, minimum=1)
    monkeypatch.setenv("DOCUMENTAI_MAX_FILES", "0")
    with pytest.raises(RuntimeError, match="at least 1"):
        _env_number("DOCUMENTAI_MAX_FILES", 20, minimum=1)


def test_every_response_carries_a_request_id(client, sample_pdf):
    fresh = client.get("/health")
    assert re.fullmatch(r"[0-9a-f]{16}", fresh.headers["x-request-id"])

    echoed = client.get("/health", headers={"X-Request-ID": "trace-42"})
    assert echoed.headers["x-request-id"] == "trace-42"
    junk = client.get("/health", headers={"X-Request-ID": "x" * 100})
    assert junk.headers["x-request-id"] != "x" * 100  # malformed ids are replaced

    # Error bodies repeat the id, whichever layer produced them.
    ours = client.post("/parse?formats=nope", files=_upload(sample_pdf))
    assert ours.status_code == 422
    assert ours.json()["request_id"] == ours.headers["x-request-id"]
    validation = client.post("/parse")  # FastAPI's own 422 for the missing files
    assert validation.status_code == 422 and "request_id" in validation.json()
    routing = client.get("/no-such-route")
    assert routing.status_code == 404 and "request_id" in routing.json()


def test_unexpected_errors_return_500_with_a_request_id(sample_pdf, monkeypatch):
    import api

    def boom(*args, **kwargs):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(api, "_process", boom)
    with TestClient(app, raise_server_exceptions=False) as quiet_client:
        response = quiet_client.post("/parse", files=_upload(sample_pdf))

    assert response.status_code == 500
    body = response.json()
    assert "kaboom" not in body["detail"]  # internals stay out of the response
    assert body["request_id"] == response.headers["x-request-id"]


def test_busy_server_returns_503_then_recovers(client, sample_pdf, monkeypatch):
    import threading

    import api

    monkeypatch.setattr(api, "_slots", threading.BoundedSemaphore(1))
    monkeypatch.setattr(api, "QUEUE_TIMEOUT", 0.05)

    api._slots.acquire()  # someone else is converting
    try:
        busy = client.post("/parse?formats=text", files=_upload(sample_pdf))
    finally:
        api._slots.release()

    assert busy.status_code == 503
    assert busy.headers["retry-after"] == "5"
    assert "busy" in busy.json()["detail"]
    assert client.post("/parse?formats=text", files=_upload(sample_pdf)).status_code == 200


def test_bundle_rejects_images_without_markdown(client, sample_pdf):
    response = client.post("/bundle?formats=text&images=true", files=_upload(sample_pdf))

    assert response.status_code == 422
    assert "markdown" in response.json()["detail"]


def test_openapi_schema_is_typed(client):
    schema = client.get("/openapi.json").json()
    components = schema["components"]["schemas"]

    parse_ok = schema["paths"]["/parse"]["post"]["responses"]["200"]
    assert parse_ok["content"]["application/json"]["schema"]["$ref"].endswith("/ParseResponse")
    report = components["DocumentReport"]["properties"]
    assert {"filename", "stem", "ok", "error", "error_type", "outputs"} <= set(report)
    assert {"detail", "request_id"} <= set(components["ErrorResponse"]["properties"])

    convert_ok = schema["paths"]["/convert"]["post"]["responses"]["200"]
    assert "application/pdf" in convert_ok["content"]
    assert "503" in schema["paths"]["/bundle"]["post"]["responses"]


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
    assert failure["error_type"] == "UnsupportedFormatError"


def test_empty_upload_is_reported_per_file(client, sample_pdf):
    response = client.post(
        "/parse?formats=text",
        files=[
            ("files", (sample_pdf.name, sample_pdf.read_bytes(), "application/pdf")),
            ("files", ("empty.pdf", b"", "application/pdf")),
        ],
    )
    body = response.json()

    # Validation failures do not fail the batch either.
    assert response.status_code == 200
    assert body["succeeded"] == 1 and body["failed"] == 1
    failure = next(r for r in body["results"] if not r["ok"])
    assert failure["filename"] == "empty.pdf"
    assert failure["error_type"] == "EmptyUpload" and "empty" in failure["error"]


def test_oversized_upload_is_reported_per_file(client, sample_pdf, monkeypatch):
    import api

    monkeypatch.setattr(api, "MAX_UPLOAD_BYTES", 16)
    response = client.post(
        "/parse?formats=text",
        files={"files": ("big.pdf", sample_pdf.read_bytes(), "application/pdf")},
    )
    result = response.json()["results"][0]

    assert response.status_code == 200
    assert result["ok"] is False
    assert result["error_type"] == "UploadTooLarge" and "limit" in result["error"]


def test_convert_maps_upload_errors_to_status_codes(client, sample_pdf, monkeypatch):
    import api

    empty = client.post("/convert", files={"file": ("empty.pdf", b"", "application/pdf")})
    assert empty.status_code == 422

    monkeypatch.setattr(api, "MAX_UPLOAD_BYTES", 16)
    big = client.post("/convert", files=_upload(sample_pdf, field="file"))
    assert big.status_code == 413


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


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("../../evil.pdf", "evil.pdf"),
        ("C:\\Users\\me\\evil.pdf", "evil.pdf"),  # Windows client, POSIX server
        ("..", "document"),
        (".", "document"),
        ("", "document"),
        (" report .PDF", "report.PDF"),
        ("we|rd:name?.docx", "we_rd_name_.docx"),
    ],
)
def test_filenames_are_sanitised(raw, expected):
    from api import _safe_filename

    assert _safe_filename(raw) == expected


def test_dotdot_filename_is_handled_not_crashed(client, sample_pdf):
    response = client.post(
        "/parse?formats=text",
        files={"files": ("..", sample_pdf.read_bytes(), "application/pdf")},
    )
    result = response.json()["results"][0]

    assert response.status_code == 200
    assert result["filename"] == "document"
    # No extension survives, so the pipeline cannot pick a strategy.
    assert result["ok"] is False and result["error_type"] == "UnsupportedFormatError"


def test_parse_reports_unique_stems_for_duplicate_names(client, sample_pdf):
    data = sample_pdf.read_bytes()
    body = client.post(
        "/parse?formats=text",
        files=[("files", ("s.pdf", data, "application/pdf"))] * 2,
    ).json()

    assert all(r["ok"] for r in body["results"])
    assert [r["stem"] for r in body["results"]] == ["s", "s_1"]


def test_same_stem_uploads_get_distinct_bundle_entries(client, sample_pdf, sample_png):
    response = client.post(
        "/bundle?formats=text",
        files=[
            ("files", ("figure.pdf", sample_pdf.read_bytes(), "application/pdf")),
            ("files", ("figure.png", sample_png.read_bytes(), "image/png")),
        ],
    )

    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = archive.namelist()
        manifest = json.loads(archive.read("manifest.json"))
    assert names.count("figure.txt") == 1 and "figure_1.txt" in names
    assert [entry["stem"] for entry in manifest] == ["figure", "figure_1"]


def test_convert_passes_through_a_password_protected_pdf(client, sample_pdf, tmp_path):
    import pymupdf

    locked = tmp_path / "locked.pdf"
    with pymupdf.open(sample_pdf) as doc:
        doc.save(
            locked, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="secret", owner_pw="secret"
        )

    response = client.post("/convert", files=_upload(locked, field="file"))

    # Conversion never opens the pages, so no password is needed.
    assert response.status_code == 200
    assert response.content == locked.read_bytes()
