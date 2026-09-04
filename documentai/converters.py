"""Stage 1 of the pipeline: turn any supported input into a PDF.

Three strategies:

``passthrough``  the input already is a PDF
``pymupdf``      PyMuPDF opens the format natively (EPUB, XPS, MOBI, FB2, CBZ)
                 and images, which become single-page PDFs
``libreoffice``  Office formats, converted by a headless soffice process

Each one is faithful: it either preserves the original pages or renders a layout
the source itself defines. None of them invent structure - see
:mod:`documentai.formats` for why text and markup are not accepted here.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import psutil
import pymupdf

from .exceptions import ConversionError, UnsupportedFormatError
from .formats import strategy_for

__all__ = ["ConversionResult", "convert_to_pdf", "find_soffice"]

DEFAULT_TIMEOUT = 180

_SOFFICE_CANDIDATES = (
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    "/usr/bin/soffice",
    "/usr/bin/libreoffice",
    "/usr/local/bin/soffice",
    "/snap/bin/libreoffice",
)


@dataclass(frozen=True)
class ConversionResult:
    """Outcome of the convert-to-PDF stage."""

    source: Path
    pdf: Path
    strategy: str
    #: False when the source already was a PDF and was used as-is.
    converted: bool


def convert_to_pdf(
    source: str | Path,
    destination: str | Path,
    *,
    soffice: str | Path | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> ConversionResult:
    """Convert ``source`` to a PDF written at ``destination``.

    A PDF input is copied to ``destination`` unchanged, so callers downstream
    only ever have to deal with one format.
    """
    source = Path(source).expanduser().resolve()
    destination = Path(destination).expanduser()

    if not source.is_file():
        raise ConversionError(f"input file does not exist: {source}")

    strategy = strategy_for(source)
    if strategy is None:
        raise UnsupportedFormatError(
            f"no conversion strategy for '{source.suffix or source.name}' ({source.name})"
        )

    destination.parent.mkdir(parents=True, exist_ok=True)

    if strategy == "passthrough":
        if source != destination.resolve():
            shutil.copyfile(source, destination)
        return ConversionResult(source, destination, "passthrough", converted=False)

    if strategy in ("pymupdf", "image"):
        _native_to_pdf(source, destination)
        used = "pymupdf"
    elif strategy == "office":
        _office_to_pdf(source, destination, soffice=soffice, timeout=timeout)
        used = "libreoffice"
    else:  # pragma: no cover - guarded by the registry
        raise UnsupportedFormatError(f"unknown strategy {strategy!r}")

    return ConversionResult(source, destination, used, converted=True)


# --------------------------------------------------------------------------- #
# Strategy implementations
# --------------------------------------------------------------------------- #


def _native_to_pdf(source: Path, destination: Path) -> None:
    """Re-emit a PyMuPDF-readable document (or image) as PDF."""
    try:
        with pymupdf.open(source) as doc:
            if doc.page_count == 0:
                raise ConversionError(f"{source.name} contains no pages")
            pdf_bytes = doc.convert_to_pdf()
    except ConversionError:
        raise
    except Exception as exc:
        raise ConversionError(f"PyMuPDF could not convert {source.name}: {exc}") from exc

    destination.write_bytes(pdf_bytes)


def _office_to_pdf(
    source: Path,
    destination: Path,
    *,
    soffice: str | Path | None,
    timeout: int,
) -> None:
    """Convert an Office document with a headless LibreOffice process."""
    binary = find_soffice(soffice)
    if binary is None:
        raise ConversionError(
            f"converting {source.name} needs LibreOffice; install it or point "
            "DOCUMENTAI_SOFFICE / --soffice at the soffice executable"
        )

    with tempfile.TemporaryDirectory(prefix="documentai-lo-") as tmp:
        tmp_path = Path(tmp)
        outdir = tmp_path / "out"
        outdir.mkdir()
        # A throwaway user profile so we never collide with a LibreOffice
        # instance the user already has open (a common failure on Windows).
        profile = (tmp_path / "profile").as_uri()

        cmd = [
            str(binary),
            f"-env:UserInstallation={profile}",
            "--headless",
            "--invisible",
            "--nologo",
            "--nolockcheck",
            "--norestore",
            "--convert-to",
            "pdf",
            "--outdir",
            str(outdir),
            str(source),
        ]
        try:
            proc = _run_capped(cmd, timeout)
        except subprocess.TimeoutExpired as exc:
            raise ConversionError(
                f"LibreOffice timed out after {timeout}s converting {source.name}"
            ) from exc

        produced = next(iter(outdir.glob("*.pdf")), None)
        if produced is None:
            detail = (proc.stderr or proc.stdout or "").strip() or f"exit code {proc.returncode}"
            raise ConversionError(f"LibreOffice failed on {source.name}: {detail}")

        shutil.move(str(produced), str(destination))


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _run_capped(cmd: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    """``subprocess.run`` with a timeout that takes the whole process tree down.

    ``soffice`` is a launcher: on Windows ``soffice.exe`` starts ``soffice.bin``
    and on Linux a shell wrapper does the same, so killing only the process we
    started would leave the real converter running - and, worse, holding the
    profile directory we are about to delete. On timeout every descendant is
    killed first, then the launcher, and ``TimeoutExpired`` is re-raised.
    """
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_tree(proc.pid)
        proc.communicate()  # reap, and release the pipes
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def _kill_process_tree(pid: int) -> None:
    """Kill ``pid`` and every process descended from it, children first."""
    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    processes = [*parent.children(recursive=True), parent]
    for process in processes:
        with contextlib.suppress(psutil.NoSuchProcess):
            process.kill()
    psutil.wait_procs(processes, timeout=5)


def find_soffice(explicit: str | Path | None = None) -> Path | None:
    """Locate the LibreOffice executable, or return ``None``."""
    candidates: list[str | Path] = []
    if explicit:
        candidates.append(explicit)
    env = os.environ.get("DOCUMENTAI_SOFFICE")
    if env:
        candidates.append(env)

    for candidate in candidates:
        path = Path(candidate).expanduser()
        if path.is_file():
            return path
        found = shutil.which(str(candidate))
        if found:
            return Path(found)

    for name in ("soffice", "soffice.exe", "libreoffice"):
        found = shutil.which(name)
        if found:
            return Path(found)

    for candidate in _SOFFICE_CANDIDATES:
        path = Path(candidate)
        if path.is_file():
            return path
    return None
