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
import re
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO, Any

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse

from documentai import DocumentPipeline, __version__, supported_extensions
from documentai.converters import convert_to_pdf, find_soffice
from documentai.exceptions import DocumentAIError, ParseError
from documentai.parsers import OUTPUT_FORMATS, normalize_format
from documentai.pipeline import safe_stem

MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MB per file
MAX_FILES = 20

_COPY_CHUNK = 1024 * 1024

DEFAULT_FORMATS = list(OUTPUT_FORMATS)

app = FastAPI(
    title="DocumentAI",
    version=__version__,
    description=(
        "Convert documents to PDF, then extract plain text, Markdown and "
        "structured JSON. Built on PyMuPDF."
    ),
)


class EmptyUpload(ValueError):
    """The upload held no bytes."""


class UploadTooLarge(ValueError):
    """The upload exceeded ``MAX_UPLOAD_BYTES``."""


#: HTTP status for a failure, keyed by the exception class name that caused it
#: (``DocumentResult.error_type`` for pipeline failures). Anything unlisted is
#: an unexpected error on our side.
_STATUS_BY_ERROR_TYPE = {
    "UnsupportedFormatError": 415,
    "UploadTooLarge": 413,
    "EmptyUpload": 422,
    "ConversionError": 422,
    "ParseError": 422,
}


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


def _check_count(uploads: list[UploadFile]) -> None:
    if not uploads:
        raise HTTPException(422, "no files uploaded")
    if len(uploads) > MAX_FILES:
        raise HTTPException(413, f"{len(uploads)} files sent; the limit is {MAX_FILES}")


def _status_for(error_type: str | None) -> int:
    return _STATUS_BY_ERROR_TYPE.get(error_type or "", 500)


def _safe_filename(raw: str | None) -> str:
    """Reduce a client-supplied filename to a bare, filesystem-safe name.

    Directory components in either slash style are dropped, unusual characters
    in the stem become ``_``, and names such as ``""``, ``"."`` and ``".."``
    fall back to ``document`` - so an upload can never land outside its own
    directory. The extension is kept (minus anything odd) because the pipeline
    dispatches on it.
    """
    bare = (raw or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    path = PurePosixPath(bare)
    suffix = re.sub(r"[^A-Za-z0-9.]", "", path.suffix)
    return safe_stem(path.stem) + suffix


@dataclass
class _Upload:
    """One upload after staging: either on disk at ``path`` or rejected."""

    name: str
    path: Path | None = None
    error: str | None = None
    error_type: str | None = None


def _copy_capped(name: str, stream: IO[bytes], destination: Path) -> None:
    """Copy ``stream`` to ``destination`` in chunks, stopping at the size cap.

    An oversized body is never held in memory: copying aborts as soon as the
    limit is passed.
    """
    written = 0
    with destination.open("wb") as out:
        while chunk := stream.read(_COPY_CHUNK):
            written += len(chunk)
            if written > MAX_UPLOAD_BYTES:
                raise UploadTooLarge(
                    f"{name} exceeds the {MAX_UPLOAD_BYTES / 1e6:.0f} MB upload limit"
                )
            out.write(chunk)
    if written == 0:
        raise EmptyUpload(f"{name} is empty")


def _stage_uploads(uploads: list[UploadFile], directory: Path) -> list[_Upload]:
    """Write every upload under ``directory`` before any is processed.

    Validation failures (empty, over the size limit) are recorded on the entry
    rather than raised, so one bad upload never fails the batch. Each upload
    gets its own subdirectory, so two uploads with the same name stay apart.
    """
    staged: list[_Upload] = []
    for index, upload in enumerate(uploads):
        name = _safe_filename(upload.filename)
        entry = _Upload(name=name)
        path = directory / str(index) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            _copy_capped(name, upload.file, path)
        except (EmptyUpload, UploadTooLarge) as exc:
            path.unlink(missing_ok=True)
            entry.error, entry.error_type = str(exc), type(exc).__name__
        else:
            entry.path = path
        staged.append(entry)
    return staged


def _payload(name: str, **fields: Any) -> dict[str, Any]:
    """The per-document response entry, with every key present."""
    payload: dict[str, Any] = {
        "filename": name,
        "stem": None,
        "ok": False,
        "strategy": None,
        "converted": False,
        "page_count": 0,
        "duration": 0.0,
        "error": None,
        "error_type": None,
        "outputs": {},
        "images": {},
        "pdf": None,
    }
    payload.update(fields)
    return payload


def _process(
    uploads: list[UploadFile],
    formats: list[str],
    *,
    spans: bool = True,
    images: bool = False,
    keep_pdf: bool = False,
) -> list[dict[str, Any]]:
    """Run every upload through the pipeline, reading results into memory.

    The working directory is deleted before returning, so the response never
    depends on files that still exist on disk.
    """
    payloads: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="documentai-api-") as tmp:
        tmp_path = Path(tmp)
        staged = _stage_uploads(uploads, tmp_path / "input")
        pipeline = DocumentPipeline(
            tmp_path / "output",
            formats=formats,
            extract_images=images,
            spans=spans,
            keep_pdf=keep_pdf,
        )
        for entry in staged:
            if entry.path is None:
                payloads.append(
                    _payload(entry.name, error=entry.error, error_type=entry.error_type)
                )
                continue

            result = pipeline.run(entry.path)
            payload = _payload(
                entry.name,
                stem=result.stem,
                ok=result.ok,
                strategy=result.strategy or None,
                converted=result.converted,
                page_count=result.page_count,
                duration=round(result.duration, 3),
                error=result.error or None,
                error_type=result.error_type or None,
            )
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

    A file that fails - unsupported, empty, over the size limit, or broken - is
    reported in its own entry with ``ok: false`` and an ``error_type``; the
    rest of the batch still succeeds, and the response is still 200.
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

    A PDF input comes back unchanged - even a password-protected one, since
    nothing here needs to read its pages.
    """
    with tempfile.TemporaryDirectory(prefix="documentai-api-") as tmp:
        tmp_path = Path(tmp)
        entry = _stage_uploads([file], tmp_path / "input")[0]
        if entry.path is None:
            raise HTTPException(_status_for(entry.error_type), entry.error)

        stem = safe_stem(PurePosixPath(entry.name).stem)
        try:
            conversion = convert_to_pdf(entry.path, tmp_path / "output" / f"{stem}.pdf")
        except DocumentAIError as exc:
            raise HTTPException(_status_for(type(exc).__name__), str(exc)) from exc
        pdf_bytes = conversion.pdf.read_bytes()

    return StreamingResponse(
        io.BytesIO(pdf_bytes),
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
    """Same work as ``/parse``, delivered as a ZIP of the output files.

    Entries are named by each document's ``stem``, which the pipeline keeps
    unique, so ``a.pdf`` and ``a.docx`` land as ``a.*`` and ``a_1.*``.
    """
    _check_count(files)
    wanted = _validate_formats(formats)
    results = _process(files, wanted, spans=spans, images=images, keep_pdf=keep_pdf)

    if not any(payload["ok"] for payload in results):
        first = next(p for p in results if p["error"])
        raise HTTPException(_status_for(first["error_type"]), first["error"])

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for payload in results:
            if not payload["ok"]:
                continue
            stem = payload["stem"]
            for fmt, content in payload["outputs"].items():
                text = json.dumps(content, indent=2) if fmt == "json" else content
                archive.writestr(f"{stem}{OUTPUT_FORMATS[fmt]}", text)
            for name, blob in payload["images"].items():
                archive.writestr(f"images/{stem}/{name}", blob)
            if payload["pdf"]:
                archive.writestr(f"pdf/{stem}.pdf", payload["pdf"])
        manifest_keys = ("filename", "stem", "ok", "strategy", "page_count", "error", "error_type")
        archive.writestr(
            "manifest.json",
            json.dumps([{k: p[k] for k in manifest_keys} for p in results], indent=2),
        )

    buffer.seek(0)
    return StreamingResponse(
        buffer,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="documentai-output.zip"'},
    )
