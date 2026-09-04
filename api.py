"""HTTP API for the DocumentAI pipeline.

    pip install -e ".[api]"
    uvicorn api:app --reload

Interactive docs are then at http://localhost:8000/docs.

Endpoints are declared with ``def`` rather than ``async def`` on purpose: the
pipeline is blocking (PyMuPDF parsing, a LibreOffice subprocess), so FastAPI
runs each call in its threadpool instead of stalling the event loop. A
semaphore (``DOCUMENTAI_MAX_CONCURRENCY``) bounds how many of those threads
convert at once, since each conversion can mean a LibreOffice process.

Settings, all read from the environment at import time:

``DOCUMENTAI_MAX_UPLOAD_BYTES``  per-file size cap (default 50 MiB)
``DOCUMENTAI_MAX_FILES``         files per request (default 20)
``DOCUMENTAI_MAX_CONCURRENCY``   simultaneous conversions (default 2)
``DOCUMENTAI_QUEUE_TIMEOUT``     seconds to wait for a slot before 503 (default 30)
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import tempfile
import threading
import uuid
import zipfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO, Any, TypeVar

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from documentai import DocumentPipeline, __version__, markdown_engine, supported_extensions
from documentai.converters import convert_to_pdf, find_soffice
from documentai.exceptions import DocumentAIError, ParseError
from documentai.parsers import OUTPUT_FORMATS, normalize_format
from documentai.pipeline import safe_stem

logger = logging.getLogger("documentai.api")

_Number = TypeVar("_Number", int, float)


def _env_number(name: str, default: _Number, *, minimum: _Number) -> _Number:
    """A numeric setting from the environment, or ``default`` when unset.

    A value that is not a number, or is below ``minimum``, is a configuration
    error and fails at import so a bad deployment never starts half-working.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = type(default)(raw)
    except ValueError:
        raise RuntimeError(f"{name} must be a number, got {raw!r}") from None
    if value < minimum:
        raise RuntimeError(f"{name} must be at least {minimum}, got {value}")
    return value


MAX_UPLOAD_BYTES = _env_number("DOCUMENTAI_MAX_UPLOAD_BYTES", 50 * 1024 * 1024, minimum=1)
MAX_FILES = _env_number("DOCUMENTAI_MAX_FILES", 20, minimum=1)
MAX_CONCURRENCY = _env_number("DOCUMENTAI_MAX_CONCURRENCY", 2, minimum=1)
QUEUE_TIMEOUT = _env_number("DOCUMENTAI_QUEUE_TIMEOUT", 30.0, minimum=0.0)

_COPY_CHUNK = 1024 * 1024
_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")

DEFAULT_FORMATS = list(OUTPUT_FORMATS)

#: Conversion slots. Acquired around the pipeline, not around the upload, so
#: waiting requests hold nothing but their staged files.
_slots = threading.BoundedSemaphore(MAX_CONCURRENCY)

app = FastAPI(
    title="DocumentAI",
    version=__version__,
    description=(
        "Convert documents to PDF, then extract plain text, Markdown and "
        "structured JSON. Built on PyMuPDF.\n\n"
        "Every response carries an `X-Request-ID` header; error bodies repeat "
        "it as `request_id` - quote it when reporting a problem."
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
# Response models
# --------------------------------------------------------------------------- #


class ErrorResponse(BaseModel):
    detail: str | list[Any] = Field(description="What went wrong")
    request_id: str = Field(description="Matches the X-Request-ID response header")


class HealthResponse(BaseModel):
    status: str
    version: str
    libreoffice: bool = Field(description="LibreOffice was found, so Office inputs convert")
    markdown_engine: str = Field(description="Markdown engine in use: layout, legacy or heuristic")


class FormatsResponse(BaseModel):
    inputs: list[str] = Field(description="Accepted input extensions")
    outputs: list[str] = Field(description="Output format names")
    office_support: bool
    max_upload_bytes: int
    max_files: int
    max_concurrency: int


class DocumentSummary(BaseModel):
    """One document's outcome, without its content."""

    filename: str = Field(description="Upload name, reduced to a bare safe name")
    stem: str | None = Field(
        default=None, description="Name the outputs are filed under; unique within the request"
    )
    ok: bool
    strategy: str | None = Field(default=None, description="passthrough, pymupdf or libreoffice")
    converted: bool = False
    page_count: int = 0
    duration: float = Field(default=0.0, description="Seconds spent on this document")
    error: str | None = None
    error_type: str | None = Field(
        default=None,
        description=(
            "Exception class behind the error: UnsupportedFormatError, EmptyUpload, "
            "UploadTooLarge, ConversionError or ParseError"
        ),
    )


class DocumentReport(DocumentSummary):
    """One document's outcome plus its extracted content."""

    outputs: dict[str, str | dict[str, Any]] = Field(
        default_factory=dict,
        description="Keyed by format: text and markdown as strings, json as an object",
    )


class ParseResponse(BaseModel):
    documents: int
    succeeded: int
    failed: int
    results: list[DocumentReport]


_ERRORS: dict[int | str, dict[str, Any]] = {
    413: {"model": ErrorResponse, "description": "Over the size or file-count limit"},
    415: {"model": ErrorResponse, "description": "Input format not accepted"},
    422: {"model": ErrorResponse, "description": "Bad request"},
    503: {"model": ErrorResponse, "description": "All conversion slots busy"},
}


# --------------------------------------------------------------------------- #
# Request IDs and error bodies
# --------------------------------------------------------------------------- #


def _request_id(request: Request) -> str:
    rid = getattr(request.state, "request_id", None)
    if not rid:
        rid = request.state.request_id = uuid.uuid4().hex[:16]
    return rid


@app.middleware("http")
async def _tag_request(request: Request, call_next: Any) -> Any:
    """Give every request an id, honouring a well-formed one the client sent."""
    incoming = request.headers.get("x-request-id", "")
    request.state.request_id = (
        incoming if _REQUEST_ID_RE.fullmatch(incoming) else uuid.uuid4().hex[:16]
    )
    response = await call_next(request)
    response.headers["X-Request-ID"] = request.state.request_id
    return response


def _error(
    request: Request, status: int, detail: Any, headers: Mapping[str, str] | None = None
) -> JSONResponse:
    rid = _request_id(request)
    return JSONResponse(
        {"detail": detail, "request_id": rid},
        status_code=status,
        headers={**(headers or {}), "X-Request-ID": rid},
    )


@app.exception_handler(StarletteHTTPException)
async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    return _error(request, exc.status_code, exc.detail, exc.headers)


@app.exception_handler(RequestValidationError)
async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    return _error(request, 422, jsonable_encoder(exc.errors()))


@app.exception_handler(Exception)
async def _unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("request %s failed", _request_id(request))
    return _error(request, 500, "internal error; quote the request_id when reporting it")


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


@contextmanager
def _conversion_slot() -> Iterator[None]:
    """Hold one of the conversion slots, or fail with 503 after the queue timeout."""
    if not _slots.acquire(timeout=QUEUE_TIMEOUT):
        raise HTTPException(
            503,
            f"all {MAX_CONCURRENCY} conversion slots are busy; retry shortly",
            headers={"Retry-After": "5"},
        )
    try:
        yield
    finally:
        _slots.release()


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


@dataclass
class _Processed:
    """A document's report plus the binary outputs that only ``/bundle`` ships."""

    report: DocumentReport
    images: dict[str, bytes]
    pdf: bytes | None


def _process(
    uploads: list[UploadFile],
    formats: list[str],
    *,
    spans: bool = True,
    images: bool = False,
    keep_pdf: bool = False,
) -> list[_Processed]:
    """Run every upload through the pipeline, reading results into memory.

    The working directory is deleted before returning, so the response never
    depends on files that still exist on disk.
    """
    processed: list[_Processed] = []
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
        with _conversion_slot():
            for entry in staged:
                if entry.path is None:
                    report = DocumentReport(
                        filename=entry.name, ok=False,
                        error=entry.error, error_type=entry.error_type,
                    )
                    processed.append(_Processed(report, {}, None))
                    continue

                result = pipeline.run(entry.path)
                report = DocumentReport(
                    filename=entry.name,
                    stem=result.stem,
                    ok=result.ok,
                    strategy=result.strategy or None,
                    converted=result.converted,
                    page_count=result.page_count,
                    duration=round(result.duration, 3),
                    error=result.error or None,
                    error_type=result.error_type or None,
                )
                image_blobs: dict[str, bytes] = {}
                pdf_bytes: bytes | None = None
                if result.ok:
                    for fmt, path in result.outputs.items():
                        content = path.read_text(encoding="utf-8")
                        # Hand JSON back as a real object, not a quoted string.
                        report.outputs[fmt] = json.loads(content) if fmt == "json" else content
                    image_blobs = {img.name: img.read_bytes() for img in result.images}
                    if keep_pdf and result.pdf:
                        pdf_bytes = result.pdf.read_bytes()
                processed.append(_Processed(report, image_blobs, pdf_bytes))
    return processed


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #


@app.get("/health", summary="Liveness probe", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        version=__version__,
        libreoffice=find_soffice() is not None,
        markdown_engine=markdown_engine(),
    )


@app.get(
    "/formats",
    summary="What this deployment accepts and produces",
    response_model=FormatsResponse,
)
def formats() -> FormatsResponse:
    """Office inputs need LibreOffice, so report whether it is available."""
    return FormatsResponse(
        inputs=supported_extensions(),
        outputs=list(OUTPUT_FORMATS),
        office_support=find_soffice() is not None,
        max_upload_bytes=MAX_UPLOAD_BYTES,
        max_files=MAX_FILES,
        max_concurrency=MAX_CONCURRENCY,
    )


@app.post(
    "/parse",
    summary="Extract text, Markdown and JSON from documents",
    response_model=ParseResponse,
    responses={k: v for k, v in _ERRORS.items() if k != 415},
)
def parse(
    files: list[UploadFile] = File(..., description="One or more documents"),
    formats: list[str] = Query(
        default=DEFAULT_FORMATS,
        description="Any of: text, markdown, json (aliases: txt, md)",
    ),
    spans: bool = Query(True, description="Keep per-span font detail in the JSON"),
) -> ParseResponse:
    """Convert each upload to PDF if needed, then return the extractions inline.

    A file that fails - unsupported, empty, over the size limit, or broken - is
    reported in its own entry with ``ok: false`` and an ``error_type``; the
    rest of the batch still succeeds, and the response is still 200.
    """
    _check_count(files)
    wanted = _validate_formats(formats)
    processed = _process(files, wanted, spans=spans)
    reports = [item.report for item in processed]

    return ParseResponse(
        documents=len(reports),
        succeeded=sum(1 for r in reports if r.ok),
        failed=sum(1 for r in reports if not r.ok),
        results=reports,
    )


@app.post(
    "/convert",
    summary="Convert one document to PDF",
    response_class=StreamingResponse,
    responses={200: {"content": {"application/pdf": {}}}, **_ERRORS},
)
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
        with _conversion_slot():
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


@app.post(
    "/bundle",
    summary="Extract and download everything as a ZIP",
    response_class=StreamingResponse,
    responses={200: {"content": {"application/zip": {}}}, **_ERRORS},
)
def bundle(
    files: list[UploadFile] = File(..., description="One or more documents"),
    formats: list[str] = Query(default=DEFAULT_FORMATS),
    spans: bool = Query(True),
    images: bool = Query(False, description="Include embedded images (requires markdown)"),
    keep_pdf: bool = Query(False, description="Include the intermediate PDF"),
) -> StreamingResponse:
    """Same work as ``/parse``, delivered as a ZIP of the output files.

    Entries are named by each document's ``stem``, which the pipeline keeps
    unique, so ``a.pdf`` and ``a.docx`` land as ``a.*`` and ``a_1.*``. The
    ZIP also holds a ``manifest.json`` listing every document's outcome.
    """
    _check_count(files)
    wanted = _validate_formats(formats)
    if images and "markdown" not in wanted:
        raise HTTPException(
            422, "images=true requires the markdown format (images are extracted with it)"
        )
    processed = _process(files, wanted, spans=spans, images=images, keep_pdf=keep_pdf)

    if not any(item.report.ok for item in processed):
        first = next(item.report for item in processed if item.report.error)
        raise HTTPException(_status_for(first.error_type), first.error)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for item in processed:
            report = item.report
            if not report.ok:
                continue
            for fmt, content in report.outputs.items():
                text = content if isinstance(content, str) else json.dumps(content, indent=2)
                archive.writestr(f"{report.stem}{OUTPUT_FORMATS[fmt]}", text)
            for name, blob in item.images.items():
                archive.writestr(f"images/{report.stem}/{name}", blob)
            if item.pdf:
                archive.writestr(f"pdf/{report.stem}.pdf", item.pdf)
        manifest = [item.report.model_dump(exclude={"outputs"}) for item in processed]
        archive.writestr("manifest.json", json.dumps(manifest, indent=2))

    buffer.seek(0)
    return StreamingResponse(
        buffer,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="documentai-output.zip"'},
    )
