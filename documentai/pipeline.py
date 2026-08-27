"""The end-to-end pipeline: any input -> PDF -> text / HTML / Markdown."""

from __future__ import annotations

import json
import logging
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .converters import DEFAULT_TIMEOUT, convert_to_pdf
from .exceptions import DocumentAIError
from .formats import is_supported
from .parsers import OUTPUT_FORMATS, normalize_format, parse_pdf

__all__ = ["DocumentPipeline", "DocumentResult", "collect_inputs", "write_manifest"]

logger = logging.getLogger("documentai")


@dataclass
class DocumentResult:
    """What the pipeline produced for a single input file."""

    source: Path
    ok: bool
    strategy: str = ""
    converted: bool = False
    page_count: int = 0
    pdf: Path | None = None
    outputs: dict[str, Path] = field(default_factory=dict)
    images: list[Path] = field(default_factory=list)
    error: str = ""
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
        formats: list[str] | tuple[str, ...] = ("text", "html", "markdown"),
        keep_pdf: bool = False,
        extract_images: bool = False,
        soffice: str | Path | None = None,
        timeout: int = DEFAULT_TIMEOUT,
        overwrite: bool = True,
    ) -> None:
        self.output_dir = Path(output_dir).expanduser()
        self.formats = [normalize_format(f) for f in formats]
        if not self.formats:
            raise ValueError("at least one output format is required")
        self.keep_pdf = keep_pdf
        self.extract_images = extract_images
        self.soffice = soffice
        self.timeout = timeout
        self.overwrite = overwrite
        self._used_stems: set[str] = set()

    # -- public API -------------------------------------------------------- #

    def run(self, source: str | Path) -> DocumentResult:
        """Process one file. Failures are captured in the result, not raised."""
        source = Path(source).expanduser()
        started = time.perf_counter()
        result = DocumentResult(source=source, ok=False)

        workdir: tempfile.TemporaryDirectory | None = None
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            stem = self._reserve_stem(source)

            if self.keep_pdf:
                pdf_path = self.output_dir / "pdf" / f"{stem}.pdf"
            else:
                workdir = tempfile.TemporaryDirectory(prefix="documentai-")
                pdf_path = Path(workdir.name) / f"{stem}.pdf"

            logger.info("converting %s -> pdf", source.name)
            conversion = convert_to_pdf(
                source, pdf_path, soffice=self.soffice, timeout=self.timeout
            )
            result.strategy = conversion.strategy
            result.converted = conversion.converted

            image_dir = self.output_dir / "images" / stem if self.extract_images else None
            logger.info("parsing %s -> %s", pdf_path.name, ", ".join(self.formats))
            parsed = parse_pdf(
                conversion.pdf,
                self.formats,
                image_dir=image_dir,
                image_link_base=f"images/{stem}" if image_dir else None,
            )
            result.page_count = parsed.page_count
            result.images = list(parsed.images)

            for fmt in self.formats:
                target = self.output_dir / f"{stem}{OUTPUT_FORMATS[fmt]}"
                if target.resolve() == conversion.source:
                    raise DocumentAIError(
                        f"{fmt} output would overwrite the input {source.name}; "
                        "choose a different --output directory"
                    )
                if target.exists() and not self.overwrite:
                    raise DocumentAIError(f"{target} already exists (overwrite disabled)")
                target.write_text(parsed.get(fmt), encoding="utf-8")
                result.outputs[fmt] = target

            if self.keep_pdf:
                result.pdf = conversion.pdf
            result.ok = True

        except DocumentAIError as exc:
            result.error = str(exc)
            logger.error("%s: %s", source.name, exc)
        except Exception as exc:  # unexpected - keep the batch alive
            result.error = f"{type(exc).__name__}: {exc}"
            logger.exception("unexpected failure on %s", source.name)
        finally:
            if workdir is not None:
                workdir.cleanup()
            result.duration = time.perf_counter() - started

        return result

    def run_many(self, sources) -> list[DocumentResult]:
        """Process several files, continuing past individual failures."""
        return [self.run(source) for source in sources]

    # -- internals --------------------------------------------------------- #

    def _reserve_stem(self, source: Path) -> str:
        """A filesystem-safe output stem, unique within this pipeline run."""
        base = "".join(c if c.isalnum() or c in "-_. " else "_" for c in source.stem).strip()
        base = base or "document"
        stem, counter = base, 1
        while stem.lower() in self._used_stems:
            stem = f"{base}_{counter}"
            counter += 1
        self._used_stems.add(stem.lower())
        return stem


def collect_inputs(paths, *, recursive: bool = False) -> list[Path]:
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
    """Write a JSON summary of a batch run."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "documents": len(results),
        "succeeded": sum(1 for r in results if r.ok),
        "failed": sum(1 for r in results if not r.ok),
        "results": [r.to_dict() for r in results],
    }
    destination.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return destination
