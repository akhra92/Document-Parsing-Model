"""HTTP API for the DocumentAI pipeline.

    pip install -e ".[api]"
    uvicorn api:app --reload

Interactive docs are then at http://localhost:8000/docs.

Endpoints are declared with ``def`` rather than ``async def`` on purpose: the
pipeline is blocking (PyMuPDF parsing, a LibreOffice subprocess), so FastAPI
runs each call in its threadpool instead of stalling the event loop.
"""

from __future__ import annotations

import io
import json
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse

from documentai import DocumentPipeline, __version__, supported_extensions
from documentai.converters import find_soffice
from documentai.exceptions import ParseError
from documentai.parsers import OUTPUT_FORMATS, normalize_format

MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MB per file
MAX_FILES = 20

DEFAULT_FORMATS = list(OUTPUT_FORMATS)

app = FastAPI(
    title="DocumentAI",
    version=__version__,
    description=(
        "Convert documents to PDF, then extract plain text, Markdown and "
        "structured JSON. Built on PyMuPDF."
    ),
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _validate_formats(formats: list[str]) -> list[str]:
    """Normalise requested format names, or fail with 422."""
    if not formats:
        raise HTTPException(422, "at least one output format is required")
    try:
        return [normalize_format(fmt) for fmt in formats]
    except ParseError as exc:
        raise HTTPException(422, str(exc)) from exc


def _read_upload(upload: UploadFile) -> tuple[str, bytes]:
    """Read one upload, enforcing the size limit and a safe filename."""
    # Strip any directory component a client may have sent.
    name = Path(upload.filename or "document").name
    data = upload.file.read()
    if not data:
        raise HTTPException(422, f"{name} is empty")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            413, f"{name} is {len(data) / 1e6:.1f} MB; the limit is "
                 f"{MAX_UPLOAD_BYTES / 1e6:.0f} MB"
        )
    return name, data


def _check_count(uploads: list[UploadFile]) -> None:
    if not uploads:
        raise HTTPException(422, "no files uploaded")
    if len(uploads) > MAX_FILES:
        raise HTTPException(413, f"{len(uploads)} files sent; the limit is {MAX_FILES}")


def _process(
    uploads: list[UploadFile],
    formats: list[str],
    *,
    spans: bool = True,
    images: bool = False,
    keep_pdf: bool = False,
) -> list[dict]:
    """Run every upload through the pipeline, reading results into memory.

    The working directory is deleted before returning, so the response never
    depends on files that still exist on disk.
    """
    payloads: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="documentai-api-") as tmp:
        tmp_path = Path(tmp)
        pipeline = DocumentPipeline(
            tmp_path / "output",
            formats=formats,
            extract_images=images,
            spans=spans,
            keep_pdf=keep_pdf,
        )
        for upload in uploads:
            name, data = _read_upload(upload)
            source = tmp_path / "input" / name
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(data)

            result = pipeline.run(source)
            payload: dict[str, Any] = {
                "filename": name,
                "ok": result.ok,
                "strategy": result.strategy or None,
                "converted": result.converted,
                "page_count": result.page_count,
                "duration": round(result.duration, 3),
                "error": result.error or None,
                "outputs": {},
                "images": {},
                "pdf": None,
            }
            if result.ok:
                for fmt, path in result.outputs.items():
                    content = path.read_text(encoding="utf-8")
                    # Hand JSON back as a real object, not a quoted string.
                    payload["outputs"][fmt] = json.loads(content) if fmt == "json" else content
                payload["images"] = {img.name: img.read_bytes() for img in result.images}
                if keep_pdf and result.pdf:
                    payload["pdf"] = result.pdf.read_bytes()
            payloads.append(payload)
    return payloads


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #


@app.get("/health", summary="Liveness probe")
def health() -> dict:
    return {
        "status": "ok",
        "version": __version__,
        "libreoffice": find_soffice() is not None,
    }


@app.get("/formats", summary="What this deployment accepts and produces")
def formats() -> dict:
    """Office inputs need LibreOffice, so report whether it is available."""
    return {
        "inputs": supported_extensions(),
        "outputs": list(OUTPUT_FORMATS),
        "office_support": find_soffice() is not None,
        "max_upload_bytes": MAX_UPLOAD_BYTES,
        "max_files": MAX_FILES,
    }


@app.post("/parse", summary="Extract text, Markdown and JSON from documents")
def parse(
    files: list[UploadFile] = File(..., description="One or more documents"),
    formats: list[str] = Query(
        default=DEFAULT_FORMATS,
        description="Any of: text, markdown, json (aliases: txt, md)",
    ),
    spans: bool = Query(True, description="Keep per-span font detail in the JSON"),
) -> dict:
    """Convert each upload to PDF if needed, then return the extractions inline.

    A file that fails is reported in its own entry with ``ok: false``; the rest
    of the batch still succeeds, and the response is still 200.
    """
    _check_count(files)
    wanted = _validate_formats(formats)
    results = _process(files, wanted, spans=spans)

    for payload in results:  # image bytes are not JSON-serialisable
        payload.pop("images", None)
        payload.pop("pdf", None)

    return {
        "documents": len(results),
        "succeeded": sum(1 for r in results if r["ok"]),
        "failed": sum(1 for r in results if not r["ok"]),
        "results": results,
    }


@app.post("/convert", summary="Convert one document to PDF")
def convert(file: UploadFile = File(..., description="A single document")) -> StreamingResponse:
    """Return the intermediate PDF itself, without parsing it.

    A PDF input comes back unchanged.
    """
    payload = _process([file], ["text"], keep_pdf=True)[0]
    if not payload["ok"]:
        raise HTTPException(_status_for(payload["error"]), payload["error"])

    stem = Path(payload["filename"]).stem
    return StreamingResponse(
        io.BytesIO(payload["pdf"]),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{stem}.pdf"'},
    )


@app.post("/bundle", summary="Extract and download everything as a ZIP")
def bundle(
    files: list[UploadFile] = File(..., description="One or more documents"),
    formats: list[str] = Query(default=DEFAULT_FORMATS),
    spans: bool = Query(True),
    images: bool = Query(False, description="Include embedded images (needs markdown)"),
    keep_pdf: bool = Query(False, description="Include the intermediate PDF"),
) -> StreamingResponse:
    """Same work as ``/parse``, delivered as a ZIP of the output files."""
    _check_count(files)
    wanted = _validate_formats(formats)
    results = _process(files, wanted, spans=spans, images=images, keep_pdf=keep_pdf)

    if not any(payload["ok"] for payload in results):
        first = next((p["error"] for p in results if p["error"]), "every document failed")
        raise HTTPException(_status_for(first), first)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for payload in results:
            if not payload["ok"]:
                continue
            stem = Path(payload["filename"]).stem
            for fmt, content in payload["outputs"].items():
                text = json.dumps(content, indent=2) if fmt == "json" else content
                archive.writestr(f"{stem}{OUTPUT_FORMATS[fmt]}", text)
            for name, blob in payload["images"].items():
                archive.writestr(f"images/{stem}/{name}", blob)
            if payload["pdf"]:
                archive.writestr(f"pdf/{stem}.pdf", payload["pdf"])
        archive.writestr(
            "manifest.json",
            json.dumps(
                [{k: p[k] for k in ("filename", "ok", "strategy", "page_count", "error")}
                 for p in results],
                indent=2,
            ),
        )

    buffer.seek(0)
    return StreamingResponse(
        buffer,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="documentai-output.zip"'},
    )


def _status_for(error: str) -> int:
    """415 for a format we do not accept, 422 for anything else the client sent."""
    return 415 if "no conversion strategy" in (error or "") else 422
