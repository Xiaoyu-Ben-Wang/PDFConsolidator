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
    # Message nesting, copied from the pipeline's segment. See model.Segment.
    group: str = ""
    parent: str = ""
    group_label: str = ""
    depth: int = 0


@dataclass
class MessageNode:
    """A message inside another message, as the navigator shows it.

    A message attached to a message is its own group of pages, hanging inside the
    group that carried it. It is not a segment: it contributes no pages of its
    own, only the ones its body and attachments do.
    """

    key: str
    label: str
    parent: str = ""
    segments: list[SegmentNode] = field(default_factory=list)
    children: list["MessageNode"] = field(default_factory=list)
    page_count: int = 0
    # Segments and child messages together, in the order the message held them,
    # so a forwarded mail sits between the attachments it arrived between.
    entries: list = field(default_factory=list)

    def descendants(self) -> list[SegmentNode]:
        """Every segment in this message, nested ones included."""
        found = list(self.segments)
        for child in self.children:
            found.extend(child.descendants())
        return found


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
        # Lookup indexes, rebuilt on demand. Every row the interface draws asks
        # for its segment and its message group, and rescanning the segment list
        # for each of those turns a few hundred files into tens of millions of
        # comparisons - which is the difference between a responsive window and
        # a frozen one.
        self._by_id: dict[int, SegmentNode] = {}
        self._by_group: dict[str, SegmentNode] = {}
        self._by_source: dict[str, list[SegmentNode]] = {}
        self._indexed = True

    def _index(self) -> None:
        if self._indexed:
            return
        self._by_id = {node.id: node for node in self.segments}
        self._by_group = {}
        self._by_source = {}
        for node in self.segments:
            if node.group:
                # First segment of a group wins: it is the one that knows the
                # group's label and its parent.
                self._by_group.setdefault(node.group, node)
            self._by_source.setdefault(str(node.source), []).append(node)
        self._indexed = True

    def _changed(self) -> None:
        self._indexed = False

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
        group: str = "",
        parent: str = "",
        group_label: str = "",
        depth: int = 0,
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
            group=group,
            parent=parent,
            group_label=group_label,
            depth=depth,
        )
        self._next_id += 1
        self.segments.append(node)
        if ok and node.pdf:
            for index in range(page_count):
                self.pages.append(PageRef(node.id, index))
        self._changed()
        return node

    def segment(self, segment_id: int) -> SegmentNode | None:
        self._index()
        return self._by_id.get(segment_id)

    def segments_of(self, source: Path) -> list[SegmentNode]:
        """Every segment a source contributed, in conversion order."""
        self._index()
        return list(self._by_source.get(str(Path(source)), []))

    def clear(self) -> None:
        self.segments.clear()
        self.pages.clear()
        self._next_id = 0
        self._changed()

    def set_segment_enabled(self, segment_id: int, enabled: bool) -> None:
        """Show or hide a segment's pages without discarding them."""
        node = self.segment(segment_id)
        if node is None:
            return
        node.enabled = enabled
        if enabled:
            existing = {p.index for p in self.pages if p.segment_id == segment_id}
            missing = [index for index in range(node.page_count) if index not in existing]
            if not missing:
                return
            insert_at = self._home_of(segment_id)
            for index in missing:
                self.pages.insert(insert_at, PageRef(segment_id, index))
                insert_at += 1
        else:
            self.pages = [p for p in self.pages if p.segment_id != segment_id]

    def _home_of(self, segment_id: int) -> int:
        """Where a segment's returning pages belong in the page list.

        After its own pages if any remain; otherwise after the last page of the
        nearest segment converted before it, so an attachment ticked back on
        returns to its place in the email instead of jumping to the top of the
        document (where re-inserting at position 0 used to put it).
        """
        last_own = None
        for position, page in enumerate(self.pages):
            if page.segment_id == segment_id:
                last_own = position
        if last_own is not None:
            return last_own + 1

        order = {node.id: rank for rank, node in enumerate(self.segments)}
        rank = order.get(segment_id, len(order))
        best_before, best_rank = None, -1
        after = None
        for position, page in enumerate(self.pages):
            other = order.get(page.segment_id, -1)
            if rank > other >= best_rank:
                best_rank, best_before = other, position
            elif other > rank and after is None:
                after = position
        if best_before is not None:
            return best_before + 1
        return after if after is not None else len(self.pages)

    def enabled_segments(self) -> list[SegmentNode]:
        return [s for s in self.segments if s.enabled and s.ok]

    # ----------------------------------------------------- message nesting

    def group_owner(self, node: SegmentNode | None) -> SegmentNode | None:
        """The segment belonging to the message that carried ``node``.

        Used to keep a nested message's colour clearly different from the colour
        of the message it arrived inside.
        """
        if node is None or not node.parent:
            return None
        self._index()
        return self._by_group.get(node.parent)

    def group_trail(self, node: SegmentNode | None) -> list[str]:
        """Message names from the source down to ``node``'s own message.

        The first entry names the source file itself and is dropped: the row
        already leads with it.
        """
        if node is None or not node.group:
            return []
        self._index()
        names: list[str] = []
        key = node.group
        seen: set[str] = set()
        while key and key not in seen:
            seen.add(key)
            owner = self._by_group.get(key)
            if owner is None:
                break
            names.append(owner.group_label or Path(owner.source).name)
            key = owner.parent
        names.reverse()
        return names[1:]

    def navigator_entries(self, source: Path) -> list:
        """One source's rows, in order: its own segments and its nested messages.

        Each nested message is a :class:`MessageNode` placed where the message was
        among its carrier's attachments, holding its own entries the same way.
        The pipeline emits a nested message's segments at the position of the
        attachment that carried it, so the first segment of a group marks where
        the group goes.
        """
        entries: list = []
        nodes: dict[str, MessageNode] = {}

        for node in self.segments_of(source):
            if node.depth <= 0 or not node.group:
                entries.append(node)
                continue
            group = nodes.get(node.group)
            if group is None:
                group = MessageNode(node.group,
                                    node.group_label or Path(node.group).name,
                                    node.parent)
                nodes[node.group] = group
                owner = nodes.get(node.parent)
                if owner is None:
                    entries.append(group)
                else:
                    owner.children.append(group)
                    owner.entries.append(group)
            group.segments.append(node)
            group.entries.append(node)

        # A message seen before its carrier (which the pipeline does not produce,
        # but a model built by hand can) is moved inside the carrier once known.
        for entry in list(entries):
            if isinstance(entry, MessageNode):
                owner = nodes.get(entry.parent)
                if owner is not None and owner is not entry and entry not in owner.children:
                    entries.remove(entry)
                    owner.children.append(entry)
                    owner.entries.append(entry)

        def total(group: MessageNode) -> int:
            group.page_count = sum(s.page_count for s in group.segments)
            group.page_count += sum(total(child) for child in group.children)
            return group.page_count

        for entry in entries:
            if isinstance(entry, MessageNode):
                total(entry)
        return entries

    def navigator(self, source: Path) -> tuple[list[SegmentNode], list[MessageNode]]:
        """Split one source's segments into what hangs off it and its messages.

        Returns the segments belonging to the source itself, in conversion order,
        and the forest of nested messages, each holding its own segments and the
        messages found inside it. :meth:`navigator_entries` keeps the two in
        their original order instead.
        """
        entries = self.navigator_entries(source)
        top = [entry for entry in entries if isinstance(entry, SegmentNode)]
        roots = [entry for entry in entries if isinstance(entry, MessageNode)]
        return top, roots

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

    def drop_source(self, source: Path) -> int:
        """Forget everything a source contributed, including its pages."""
        doomed = {node.id for node in self.segments_of(source)}
        if not doomed:
            return 0
        self.segments = [s for s in self.segments if s.id not in doomed]
        self.pages = [p for p in self.pages if p.segment_id not in doomed]
        self._changed()
        return len(doomed)

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
        self._changed()
        return before - len(self.segments)

    def replace_source(self, source: Path, segments: list, count_pages=None) -> int:
        """Merge a re-conversion of one source back in, in place. Returns recoveries.

        A retry converts the whole source again, so it comes back with *every*
        segment, not only the ones that failed. Appending them all duplicated the
        parts that had worked and put everything - recovered attachments too - at
        the end of the document. Instead the new segments are matched to the old
        ones by what they are (label, kind, depth, in order):

        * a part that had already worked keeps its node, so its pages and any
          edits made to them stay exactly where they are;
        * a part that failed before and works now takes the failed one's place in
          the email, and its pages go where that place is in the page list;
        * a part that still fails replaces its old failure.

        ``segments`` are pipeline segments; ``count_pages`` counts a PDF's pages
        when a segment does not say.
        """
        source = Path(source)
        old = self.segments_of(source)
        old_ids = {node.id for node in old}
        unclaimed: dict[tuple, list[SegmentNode]] = {}
        for node in old:
            unclaimed.setdefault((node.label, node.kind, node.depth), []).append(node)

        # A nested message's group is named after the retry's scratch path, which
        # differs from the first run's; map it back so siblings stay together.
        groups: dict[str, str] = {}
        merged: list[SegmentNode] = []
        recovered = 0
        for segment in segments:
            key = (segment.label, segment.kind, segment.depth)
            previous = unclaimed.get(key)
            match = previous.pop(0) if previous else None
            if match is not None and segment.group and match.group:
                groups.setdefault(segment.group, match.group)
            if match is not None and match.ok:
                merged.append(match)            # worked before: leave it be
                continue
            pages = segment.page_count
            if not pages and segment.ok and segment.pdf and count_pages:
                pages = count_pages(segment.pdf)
            node = SegmentNode(
                id=self._next_id, source=source, label=segment.label,
                kind=segment.kind, pdf=Path(segment.pdf) if segment.pdf else None,
                ok=bool(segment.ok), detail=segment.detail or "",
                page_count=pages, group=segment.group, parent=segment.parent,
                group_label=segment.group_label, depth=segment.depth,
            )
            self._next_id += 1
            if match is not None:
                node.enabled = match.enabled
            if node.ok:
                recovered += 1
            merged.append(node)

        for node in merged:
            if node.id not in old_ids:
                node.group = groups.get(node.group, node.group)
                node.parent = groups.get(node.parent, node.parent)

        # The source's block of segments is replaced where it stood, so segment
        # order - which decides where returning pages go - is the email's order.
        kept = {node.id for node in merged}
        first = next((i for i, node in enumerate(self.segments) if node.id in old_ids),
                     len(self.segments))
        before = [n for n in self.segments[:first] if n.id not in old_ids]
        after = [n for n in self.segments[first:] if n.id not in old_ids]
        self.segments = before + merged + after
        self.pages = [p for p in self.pages
                      if p.segment_id in kept or p.segment_id not in old_ids]
        self._changed()

        for node in merged:
            if node.id not in old_ids and node.ok and node.pdf and node.enabled:
                insert_at = self._home_of(node.id)
                for index in range(node.page_count):
                    self.pages.insert(insert_at + index, PageRef(node.id, index))
        return recovered

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
