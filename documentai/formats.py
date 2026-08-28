"""Input format registry.

Every supported extension maps to exactly one *conversion strategy*, which is
what :mod:`documentai.converters` dispatches on to produce a PDF.

Only binary or paginated formats are accepted - ones where the structure has to
be recovered from a rendered layout. Text, Markdown and HTML are deliberately
absent: they already carry explicit structure, so rendering them to a page and
statistically re-deriving it loses information (links and tables in particular).
They are what this package emits, not what it ingests.
"""

from __future__ import annotations

from pathlib import Path

#: Already a PDF - copied straight through to the parsing stage.
PDF_EXTS = frozenset({".pdf"})

#: Document formats PyMuPDF opens natively and can re-emit as PDF.
NATIVE_EXTS = frozenset({".epub", ".xps", ".oxps", ".mobi", ".fb2", ".cbz"})

#: Raster formats PyMuPDF opens as single-page "image documents".
IMAGE_EXTS = frozenset(
    {".png", ".jpg", ".jpeg", ".jfif", ".bmp", ".gif", ".tif", ".tiff", ".pnm",
     ".pgm", ".ppm", ".pbm", ".tga", ".psd", ".jxr", ".jpx", ".jp2", ".webp"}
)

#: Office formats handed to a headless LibreOffice.
OFFICE_EXTS = frozenset(
    {".doc", ".docx", ".docm", ".dot", ".dotx", ".odt", ".rtf", ".wps",
     ".ppt", ".pptx", ".pptm", ".odp", ".pps", ".ppsx",
     ".xls", ".xlsx", ".xlsm", ".ods", ".csv", ".tsv"}
)

_STRATEGY_BY_EXT: dict[str, str] = {
    **{ext: "passthrough" for ext in PDF_EXTS},
    **{ext: "pymupdf" for ext in NATIVE_EXTS},
    **{ext: "image" for ext in IMAGE_EXTS},
    **{ext: "office" for ext in OFFICE_EXTS},
}

SUPPORTED_EXTS = frozenset(_STRATEGY_BY_EXT)


def strategy_for(path: str | Path) -> str | None:
    """Return the conversion strategy for ``path``, or ``None`` if unsupported."""
    return _STRATEGY_BY_EXT.get(Path(path).suffix.lower())


def is_supported(path: str | Path) -> bool:
    return strategy_for(path) is not None


def supported_extensions() -> list[str]:
    """All supported extensions, sorted - handy for CLI help and docs."""
    return sorted(SUPPORTED_EXTS)
