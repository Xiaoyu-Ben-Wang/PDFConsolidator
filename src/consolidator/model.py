"""Shared data model.

A *source* is a file the user selected. It expands into one or more *segments*:
an ordinary document yields a single segment, while an Outlook item yields a body
segment plus one per extracted attachment. Segments - not sources - are what the
assembler lays out, which is what lets an email attachment appear as its own
pages in the middle of the output.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# Segment kinds.
DOCUMENT = "document"   # a whole converted file
BODY = "body"           # the message body of an email item
ATTACHMENT = "attachment"  # an attachment extracted from an email item


@dataclass
class Segment:
    """One contiguous run of pages contributed by a single source."""

    source: Path
    label: str
    kind: str = DOCUMENT
    pdf: Path | None = None
    ok: bool = False
    detail: str = ""
    seconds: float = 0.0

    @property
    def display(self) -> str:
        return self.label or self.source.name


@dataclass
class SourceResult:
    """Everything one selected file contributed."""

    source: Path
    segments: list[Segment] = field(default_factory=list)
    detail: str = ""

    @property
    def ok(self) -> bool:
        return any(s.ok for s in self.segments)

    @property
    def failed_segments(self) -> list[Segment]:
        return [s for s in self.segments if not s.ok]

    @property
    def page_count(self) -> int:
        """Total pages contributed, counted from the produced PDFs."""
        total = 0
        for segment in self.segments:
            if segment.ok and segment.pdf and segment.pdf.exists():
                try:
                    from pypdf import PdfReader

                    total += len(PdfReader(str(segment.pdf)).pages)
                except Exception:
                    pass
        return total
