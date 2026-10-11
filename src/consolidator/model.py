"""Shared data model.

A *source* is a file the user selected. It expands into one or more *segments*:
an ordinary document yields one segment, and an Outlook item yields a body
segment plus one per extracted attachment. The assembler lays out segments, not
sources, so an email attachment can appear as its own pages in the output.

A message attached to another message is expanded in place. Its segments record
the nesting (see :class:`Segment`) so the interface can show a forwarded message
as a child of the message that carried it.
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
    # Page count of the produced PDF, set once by the pipeline so the interface
    # does not have to reopen every PDF to count pages.
    page_count: int = 0

    # Message nesting for attached messages that are expanded in place.
    #
    # ``group`` identifies the message; it is empty for an ordinary file, whose
    # path then serves as the group. ``parent`` is the group that embedded this
    # one. ``group_label`` is the message's display name.
    group: str = ""
    parent: str = ""
    group_label: str = ""
    depth: int = 0

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
