"""DocumentAI - a PyMuPDF document parsing pipeline.

Any supported input is normalised to PDF first, then parsed into plain text,
Markdown and structured JSON:

    from documentai import DocumentPipeline

    pipeline = DocumentPipeline("output", formats=["text", "markdown"])
    result = pipeline.run("contract.docx")
    print(result.outputs["markdown"].read_text(encoding="utf-8"))
"""

from __future__ import annotations

__version__ = "0.1.0"

from .converters import ConversionResult, convert_to_pdf
from .exceptions import (
    ConversionError,
    DocumentAIError,
    ParseError,
    UnsupportedFormatError,
)
from .formats import is_supported, supported_extensions
from .parsers import (
    MARKDOWN_ENGINE,
    OUTPUT_FORMATS,
    ParsedDocument,
    extract,
    extract_json,
    extract_markdown,
    extract_text,
    markdown_engine,
    parse_pdf,
)
from .pipeline import DocumentPipeline, DocumentResult, collect_inputs, write_manifest

__all__ = [
    "MARKDOWN_ENGINE",
    "OUTPUT_FORMATS",
    "ConversionError",
    "ConversionResult",
    "DocumentAIError",
    "DocumentPipeline",
    "DocumentResult",
    "ParseError",
    "ParsedDocument",
    "UnsupportedFormatError",
    "__version__",
    "collect_inputs",
    "convert_to_pdf",
    "extract",
    "extract_json",
    "extract_markdown",
    "extract_text",
    "is_supported",
    "markdown_engine",
    "parse_pdf",
    "supported_extensions",
    "write_manifest",
]
