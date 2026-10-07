"""The editable document model behind the GUI.

Deliberately free of any Qt import so it can be tested without a display, and so
the same model could back a different front end.

The model holds *segments* (what conversion produced) and a flat list of *pages*
(what the user sees and edits). Editing operations act on the page list; building
output re-groups consecutive pages back into merge entries, which is what allows
pages from different documents to be interleaved arbitrarily.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .. import assemble


@dataclass
class SegmentNode:
    """One converted run of pages, as produced by Phase A."""

    id: int
    source: Path
    label: str
    kind: str = "document"
    pdf: Path | None = None
    ok: bool = False
    detail: str = ""
    enabled: bool = True
    page_count: int = 0


@dataclass
class PageRef:
    """A single page in the assembled document."""

    segment_id: int
    index: int          # index within that segment's PDF
    rotation: int = 0   # clockwise degrees, applied on output
    selected: bool = False

    @property
    def key(self) -> tuple[int, int]:
        return (self.segment_id, self.index)


class DocumentModel:
    """Editable page list built from converted segments."""

    def __init__(self) -> None:
        self.segments: list[SegmentNode] = []
        self.pages: list[PageRef] = []
        self._next_id = 0

    # ------------------------------------------------------------- segments

    def add_segment(
        self,
        source: Path,
        label: str,
        pdf: Path | None,
        ok: bool,
        kind: str = "document",
        detail: str = "",
        page_count: int = 0,
    ) -> SegmentNode:
        node = SegmentNode(
            id=self._next_id,
            source=Path(source),
            label=label,
            kind=kind,
            pdf=Path(pdf) if pdf else None,
            ok=ok,
            detail=detail,
            page_count=page_count,
        )
        self._next_id += 1
        self.segments.append(node)
        if ok and node.pdf:
            for index in range(page_count):
                self.pages.append(PageRef(node.id, index))
        return node

    def segment(self, segment_id: int) -> SegmentNode | None:
        for node in self.segments:
            if node.id == segment_id:
                return node
        return None

    def clear(self) -> None:
        self.segments.clear()
        self.pages.clear()
        self._next_id = 0

    def set_segment_enabled(self, segment_id: int, enabled: bool) -> None:
        """Show or hide a segment's pages without discarding them."""
        node = self.segment(segment_id)
        if node is None:
            return
        node.enabled = enabled
        if enabled:
            # Re-insert its pages in segment order, after any existing pages of
            # the same segment, so the user's cross-document ordering survives.
            existing = {p.index for p in self.pages if p.segment_id == segment_id}
            insert_at = 0
            for position, page in enumerate(self.pages):
                if page.segment_id == segment_id:
                    insert_at = position + 1
            for index in range(node.page_count):
                if index not in existing:
                    self.pages.insert(insert_at, PageRef(segment_id, index))
                    insert_at += 1
        else:
            self.pages = [p for p in self.pages if p.segment_id != segment_id]

    def enabled_segments(self) -> list[SegmentNode]:
        return [s for s in self.segments if s.enabled and s.ok]

    # ---------------------------------------------------------- page edits

    def rotate(self, indices: list[int], degrees: int) -> None:
        for i in indices:
            if 0 <= i < len(self.pages):
                self.pages[i].rotation = (self.pages[i].rotation + degrees) % 360

    def set_rotation(self, indices: list[int], degrees: int) -> None:
        for i in indices:
            if 0 <= i < len(self.pages):
                self.pages[i].rotation = degrees % 360

    def delete(self, indices: list[int]) -> int:
        drop = {i for i in indices if 0 <= i < len(self.pages)}
        self.pages = [p for i, p in enumerate(self.pages) if i not in drop]
        return len(drop)

    def move(self, indices: list[int], target: int) -> None:
        """Move the selected pages so the first lands at ``target``."""
        picked = [self.pages[i] for i in sorted(indices) if 0 <= i < len(self.pages)]
        if not picked:
            return
        remaining = [p for i, p in enumerate(self.pages) if i not in set(indices)]
        target = max(0, min(target, len(remaining)))
        self.pages = remaining[:target] + picked + remaining[target:]

    def move_up(self, indices: list[int]) -> None:
        for i in sorted(indices):
            if i > 0 and (i - 1) not in set(indices):
                self.pages[i - 1], self.pages[i] = self.pages[i], self.pages[i - 1]

    def move_down(self, indices: list[int]) -> None:
        for i in sorted(indices, reverse=True):
            if i < len(self.pages) - 1 and (i + 1) not in set(indices):
                self.pages[i + 1], self.pages[i] = self.pages[i], self.pages[i + 1]

    def duplicate(self, indices: list[int]) -> None:
        for i in sorted(indices, reverse=True):
            if 0 <= i < len(self.pages):
                original = self.pages[i]
                self.pages.insert(i + 1, PageRef(original.segment_id, original.index,
                                                 original.rotation))

    def select_all(self, selected: bool = True) -> None:
        for page in self.pages:
            page.selected = selected

    def remove_failed_for(self, source: Path) -> int:
        """Drop the failed segments belonging to one source, before a retry.

        Successful segments are left alone, so retrying only ever replaces what
        actually went wrong.
        """
        source = Path(source)
        before = len(self.segments)
        self.segments = [
            s for s in self.segments if not (s.source == source and not s.ok)
        ]
        return before - len(self.segments)

    def group_by_source(self) -> None:
        """Cluster pages so every page from one source file is contiguous.

        Page order *within* each document is preserved, and documents keep the
        order of their first appearance, so this is a stable tidy-up rather than
        a re-sort. It is what the "group by document" button calls.
        """
        order: list[str] = []
        buckets: dict[str, list[PageRef]] = {}
        for page in self.pages:
            node = self.segment(page.segment_id)
            key = str(node.source) if node else str(page.segment_id)
            if key not in buckets:
                buckets[key] = []
                order.append(key)
            buckets[key].append(page)
        self.pages = [page for key in order for page in buckets[key]]

    def sources_in_order(self) -> list[str]:
        """Source labels in the order their pages currently appear."""
        seen: list[str] = []
        for page in self.pages:
            node = self.segment(page.segment_id)
            if node is None:
                continue
            key = str(node.source)
            if key not in seen:
                seen.append(key)
        return seen

    def to_entries(self) -> list[assemble.MergeEntry]:
        """Group consecutive pages of the same segment into merge entries.

        Re-grouping rather than assuming one entry per segment is what makes
        arbitrary interleaving work: pages from document A, then B, then A again
        become three entries, and the output order matches the grid exactly.
        """
        entries: list[assemble.MergeEntry] = []
        current_id: int | None = None
        current_specs: list[assemble.PageSpec] = []
        current_label = ""

        def flush() -> None:
            if current_specs and current_id is not None:
                node = self.segment(current_id)
                entries.append(
                    assemble.MergeEntry(node.pdf, current_label, list(current_specs))
                )

        for page in self.pages:
            node = self.segment(page.segment_id)
            if node is None or not node.ok or not node.pdf:
                continue
            if page.segment_id != current_id:
                flush()
                current_id = page.segment_id
                current_specs = []
                current_label = node.label
            current_specs.append(assemble.PageSpec(page.index, page.rotation))

        flush()
        return entries

    @property
    def page_count(self) -> int:
        return len(self.pages)

    def summary(self) -> str:
        documents = len({p.segment_id for p in self.pages})
        return "%d page(s) from %d segment(s)" % (len(self.pages), documents)
