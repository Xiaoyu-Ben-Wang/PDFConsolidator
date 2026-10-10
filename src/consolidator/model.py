"""Shared data model.

A *source* is a file the user selected. It expands into one or more *segments*:
an ordinary document yields a single segment, while an Outlook item yields a body
segment plus one per extracted attachment. Segments - not sources - are what the
assembler lays out, which is what lets an email attachment appear as its own
pages in the middle of the output.

A message attached to a message is expanded in place, and its segments carry the
nesting (see :class:`Segment`), so the interface can show a forwarded message as
a node inside the message that carried it instead of flattening it into a list of
look-alike rows.
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
    # Pages the produced PDF actually has, verified once by the pipeline. The
    # interface used to reopen every PDF to count them, which on a folder of
    # thousands of files meant thousands of extra opens.
    page_count: int = 0

    # Message nesting. An attached message is expanded in place rather than kept
    # as an opaque file, so its pages have to say which message they belong to,
    # which message carried that one, and what to call it.
    #
    # ``group`` identifies the message; it is empty for an ordinary file, whose
    # own path then serves as the group. ``parent`` is the group that embedded
    # this one, which is what turns a flat list into a nested navigator.
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
