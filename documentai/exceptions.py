"""Exception hierarchy for the DocumentAI pipeline."""

from __future__ import annotations


class DocumentAIError(Exception):
    """Base class for every error raised by this package."""


class UnsupportedFormatError(DocumentAIError):
    """The input file extension has no registered conversion strategy."""


class ConversionError(DocumentAIError):
    """A source document could not be converted to PDF."""


class ParseError(DocumentAIError):
    """A PDF could not be parsed into the requested output format."""
