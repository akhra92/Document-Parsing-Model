"""The end-to-end pipeline: any input -> PDF -> text / Markdown / JSON."""

from __future__ import annotations

import json
import logging
import tempfile
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .converters import DEFAULT_TIMEOUT, convert_to_pdf
from .exceptions import DocumentAIError
from .formats import is_supported
from .parsers import OUTPUT_FORMATS, normalize_format, parse_pdf

__all__ = [
    "DocumentPipeline",
    "DocumentResult",
    "collect_inputs",
    "safe_stem",
    "unique_stems",
    "write_manifest",
]

logger = logging.getLogger("documentai")


@dataclass
class DocumentResult:
    """What the pipeline produced for a single input file."""

    source: Path
    ok: bool
    #: Filename stem shared by every output of this document (``report`` for
    #: ``report.txt`` / ``report.md`` / ``images/report/``). Unique within a
    #: pipeline run - see :func:`safe_stem`.
    stem: str = ""
    strategy: str = ""
    converted: bool = False
    page_count: int = 0
    pdf: Path | None = None
    outputs: dict[str, Path] = field(default_factory=dict)
    images: list[Path] = field(default_factory=list)
    error: str = ""
    #: Class name of the exception behind ``error`` (``UnsupportedFormatError``,
    #: ``ConversionError``, ``ParseError``, or whatever unexpected type occurred).
    error_type: str = ""
    duration: float = 0.0

    def to_dict(self) -> dict:
        data = asdict(self)
        data["source"] = str(self.source)
        data["pdf"] = str(self.pdf) if self.pdf else None
        data["outputs"] = {k: str(v) for k, v in self.outputs.items()}
        data["images"] = [str(p) for p in self.images]
        data["duration"] = round(self.duration, 3)
        return data


class DocumentPipeline:
    """Convert inputs to PDF, then parse them into the requested formats.

    >>> pipeline = DocumentPipeline("out", formats=["text", "markdown"])
    >>> result = pipeline.run("report.docx")
    >>> result.outputs["text"]
    PosixPath('out/report.txt')
    """

    def __init__(
        self,
        output_dir: str | Path,
        *,
        formats: list[str] | tuple[str, ...] = ("text", "markdown", "json"),
        keep_pdf: bool = False,
        extract_images: bool = False,
        spans: bool = True,
        soffice: str | Path | None = None,
        timeout: int = DEFAULT_TIMEOUT,
        overwrite: bool = True,
    ) -> None:
        self.output_dir = Path(output_dir).expanduser()
        self.formats = [normalize_format(f) for f in formats]
        if not self.formats:
            raise ValueError("at least one output format is required")
        if extract_images and "markdown" not in self.formats:
            # Images are written as a side effect of Markdown extraction, so
            # without it the option would silently do nothing.
            raise ValueError("extract_images requires the markdown format")
        self.keep_pdf = keep_pdf
        self.extract_images = extract_images
        self.spans = spans
        self.soffice = soffice
        self.timeout = timeout
        self.overwrite = overwrite
        self._used_stems: set[str] = set()

    # -- public API -------------------------------------------------------- #

    def run(self, source: str | Path, *, stem: str | None = None) -> DocumentResult:
        """Process one file. Failures are captured in the result, not raised.

        ``stem`` names the outputs; it defaults to the source's own stem. Either
        way it is sanitised and made unique within this pipeline's run.
        """
        source = Path(source).expanduser()
        started = time.perf_counter()
        result = DocumentResult(source=source, ok=False)

        workdir: tempfile.TemporaryDirectory | None = None
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            stem = self._reserve_stem(source.stem if stem is None else stem)
            result.stem = stem

            targets = {
                fmt: self.output_dir / f"{stem}{OUTPUT_FORMATS[fmt]}" for fmt in self.formats
            }
            image_dir = self.output_dir / "images" / stem if self.extract_images else None
            if self.keep_pdf:
                pdf_path = self.output_dir / "pdf" / f"{stem}.pdf"
            else:
                workdir = tempfile.TemporaryDirectory(prefix="documentai-")
                pdf_path = Path(workdir.name) / f"{stem}.pdf"

            # Refuse up front, before any conversion work or any file is touched.
            self._check_destinations(
                source, targets, kept_pdf=pdf_path if self.keep_pdf else None, image_dir=image_dir
            )

            logger.info("converting %s -> pdf", source.name)
            conversion = convert_to_pdf(
                source, pdf_path, soffice=self.soffice, timeout=self.timeout
            )
            result.strategy = conversion.strategy
            result.converted = conversion.converted

            logger.info("parsing %s -> %s", pdf_path.name, ", ".join(self.formats))
            parsed = parse_pdf(
                conversion.pdf,
                self.formats,
                image_dir=image_dir,
                image_link_base=f"images/{stem}" if image_dir else None,
                spans=self.spans,
            )
            result.page_count = parsed.page_count
            result.images = list(parsed.images)

            for fmt, target in targets.items():
                target.write_text(parsed.get(fmt), encoding="utf-8")
                result.outputs[fmt] = target

            if self.keep_pdf:
                result.pdf = conversion.pdf
            result.ok = True

        except DocumentAIError as exc:
            result.error = str(exc)
            result.error_type = type(exc).__name__
            logger.error("%s: %s", source.name, exc)
        except Exception as exc:  # unexpected - keep the batch alive
            result.error = f"{type(exc).__name__}: {exc}"
            result.error_type = type(exc).__name__
            logger.exception("unexpected failure on %s", source.name)
        finally:
            if workdir is not None:
                workdir.cleanup()
            result.duration = time.perf_counter() - started

        return result

    def run_many(self, sources: Iterable[str | Path]) -> list[DocumentResult]:
        """Process several files, continuing past individual failures."""
        return [self.run(source) for source in sources]

    # -- internals --------------------------------------------------------- #

    def _reserve_stem(self, stem: str) -> str:
        """A filesystem-safe output stem, unique within this pipeline run."""
        return _next_unique(safe_stem(stem), self._used_stems)

    def _check_destinations(
        self,
        source: Path,
        targets: dict[str, Path],
        *,
        kept_pdf: Path | None,
        image_dir: Path | None,
    ) -> None:
        """Raise if writing the outputs would clobber the input or, with
        overwriting disabled, anything that already exists."""
        resolved_source = source.resolve()
        for fmt, target in targets.items():
            if target.resolve() == resolved_source:
                raise DocumentAIError(
                    f"{fmt} output would overwrite the input {source.name}; "
                    "choose a different --output directory"
                )
        if self.overwrite:
            return

        existing = [target for target in targets.values() if target.exists()]
        if kept_pdf is not None and kept_pdf.exists():
            existing.append(kept_pdf)
        if image_dir is not None and image_dir.is_dir() and any(image_dir.iterdir()):
            existing.append(image_dir)
        if existing:
            raise DocumentAIError(f"{existing[0]} already exists (overwrite disabled)")


# --------------------------------------------------------------------------- #
# Output naming
# --------------------------------------------------------------------------- #


def safe_stem(stem: str) -> str:
    """Reduce a filename stem to something safe to write on any filesystem.

    Letters, digits, ``-``, ``_``, ``.`` and spaces are kept; anything else
    becomes ``_``. Leading and trailing dots and spaces are dropped (Windows
    rejects them, and they would let ``..`` through). An empty result becomes
    ``document``.
    """
    cleaned = "".join(c if c.isalnum() or c in "-_. " else "_" for c in stem).strip(" .")
    return cleaned or "document"


def unique_stems(names: Iterable[str]) -> list[str]:
    """Safe, mutually distinct output stems for a batch of filenames.

    The same rule :class:`DocumentPipeline` applies within one run, for callers
    that process each document with its own pipeline (a web app caching per
    upload, say) and still need ``a.pdf`` and ``a.docx`` to land apart::

        >>> unique_stems(["a.pdf", "a.docx", "A.png"])
        ['a', 'a_1', 'A_2']

    Pass the results to :meth:`DocumentPipeline.run` as ``stem``.
    """
    used: set[str] = set()
    return [_next_unique(safe_stem(Path(name).stem), used) for name in names]


def _next_unique(base: str, used: set[str]) -> str:
    """``base``, or ``base_N`` for the first N that is not yet in ``used``.

    Comparison is case-insensitive so the outputs stay distinct on Windows and
    macOS; ``used`` is updated in place.
    """
    stem, counter = base, 1
    while stem.lower() in used:
        stem = f"{base}_{counter}"
        counter += 1
    used.add(stem.lower())
    return stem


# --------------------------------------------------------------------------- #
# Batch helpers
# --------------------------------------------------------------------------- #


def collect_inputs(paths: Iterable[str | Path], *, recursive: bool = False) -> list[Path]:
    """Expand files and directories into a sorted list of supported inputs."""
    collected: list[Path] = []
    for raw in paths:
        path = Path(raw).expanduser()
        if path.is_dir():
            pattern = "**/*" if recursive else "*"
            collected.extend(
                sorted(p for p in path.glob(pattern) if p.is_file() and is_supported(p))
            )
        else:
            collected.append(path)

    seen: set[Path] = set()
    unique = []
    for path in collected:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(path)
    return unique


def write_manifest(results: list[DocumentResult], destination: str | Path) -> Path:
    """Write a JSON summary of a batch run.

    Steps aside if a document's own JSON output already claims that filename.
    """
    destination = Path(destination)
    extracted = {path.resolve() for r in results for path in r.outputs.values()}
    if destination.resolve() in extracted:
        destination = destination.with_name(f"{destination.stem}_run{destination.suffix}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "documents": len(results),
        "succeeded": sum(1 for r in results if r.ok),
        "failed": sum(1 for r in results if not r.ok),
        "results": [r.to_dict() for r in results],
    }
    destination.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return destination
