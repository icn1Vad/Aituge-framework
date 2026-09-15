"""Provider-neutral document extraction results."""

from dataclasses import dataclass


class DocumentExtractionError(RuntimeError):
    """Extraction failed; callers must not mark this document ready."""


@dataclass(frozen=True, slots=True)
class ExtractedDocument:
    file_name: str
    markdown: str
    provider: str
    backend: str
