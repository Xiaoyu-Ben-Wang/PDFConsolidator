"""Build ``.msg`` files from scratch, for tests.

Outlook is the only thing that writes these for real, and it is not available on
every machine - nor are real messages, which carry real people's mail. This writes
the same OLE layout Outlook does ([MS-OXMSG]): a properties stream per object,
string and binary properties as ``__substg1.0_`` streams, attachments in
``__attach_version1.0_#`` storages and an attached Outlook item as a nested
message storage. It covers what the consolidator reads and nothing more.

The point is to reproduce the shapes real mailboxes contain: an email carrying an
appointment, a contact or a task as an *Outlook item*, an item of a class
extract-msg refuses, an attachment with no data stream.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from extract_msg.ole_writer import OleWriter

PT_LONG = 0x0003
PT_UNICODE = 0x001F
PT_BINARY = 0x0102
PT_OBJECT = 0x000D

ATTACH_BY_VALUE = 1
ATTACH_EMBEDDED_MSG = 5
ATTACH_OLE = 6
ATTACH_BY_WEB_REFERENCE = 7


@dataclass
class Att:
    """One attachment: a file by value, or an embedded Outlook item."""

    filename: str = ""
    data: bytes | None = None
    item: "Item | None" = None
    method: int = 0          # 0 = choose from data/item
    mimetype: str = ""
    content_id: str = ""


@dataclass
class Item:
    """An Outlook item: a message, appointment, contact, task..."""

    message_class: str = "IPM.Note"
    subject: str = ""
    body: str = ""
    html: bytes | None = None
    sender: str = ""
    to: str = ""
    # Extra string properties by id, e.g. {0x8208: "Room 4"} would need named
    # properties; plain tagged ones are enough for tests.
    strings: dict[int, str] = field(default_factory=dict)
    attachments: list[Att] = field(default_factory=list)


def _prop(tag_id: int, ptype: int, value: bytes) -> bytes:
    # tag, flags (readable | writable), 8-byte value.
    return struct.pack("<HHI", ptype, tag_id, 6) + value.ljust(8, b"\x00")


def _var(tag_id: int, ptype: int, size: int) -> bytes:
    return _prop(tag_id, ptype, struct.pack("<II", size, 0))


def _long(tag_id: int, value: int) -> bytes:
    return _prop(tag_id, PT_LONG, struct.pack("<I", value))


def _stream(tag_id: int, ptype: int) -> str:
    return "__substg1.0_%04X%04X" % (tag_id, ptype)


def _write_item(writer: OleWriter, prefix: list[str], item: Item, top: bool) -> None:
    props: list[bytes] = []

    def string(tag_id: int, value: str) -> None:
        if not value:
            return
        data = value.encode("utf-16-le")
        writer.addEntry(prefix + [_stream(tag_id, PT_UNICODE)], data)
        props.append(_var(tag_id, PT_UNICODE, len(data) + 2))

    def binary(tag_id: int, value: bytes) -> None:
        writer.addEntry(prefix + [_stream(tag_id, PT_BINARY)], value)
        props.append(_var(tag_id, PT_BINARY, len(value)))

    string(0x001A, item.message_class)
    string(0x0037, item.subject)
    string(0x0E1D, item.subject)            # normalized subject
    string(0x1000, item.body)
    string(0x0C1A, item.sender)
    string(0x0E04, item.to)
    for tag_id, value in item.strings.items():
        string(tag_id, value)
    if item.html is not None:
        binary(0x1013, item.html)
    props.append(_long(0x0E07, 0))          # message flags

    for index, att in enumerate(item.attachments):
        _write_attachment(writer, prefix + ["__attach_version1.0_#%08X" % index], att)

    count = len(item.attachments)
    if top:
        header = struct.pack("<8sIIII8s", b"", 0, count, 0, count, b"")
    else:
        header = struct.pack("<8sIIII", b"", 0, count, 0, count)
    writer.addEntry(prefix + ["__properties_version1.0"], header + b"".join(props))


def _write_attachment(writer: OleWriter, prefix: list[str], att: Att) -> None:
    writer.addEntry(prefix, storage=True)
    props: list[bytes] = []

    def string(tag_id: int, value: str) -> None:
        if not value:
            return
        data = value.encode("utf-16-le")
        writer.addEntry(prefix + [_stream(tag_id, PT_UNICODE)], data)
        props.append(_var(tag_id, PT_UNICODE, len(data) + 2))

    method = att.method or (ATTACH_EMBEDDED_MSG if att.item else ATTACH_BY_VALUE)
    props.append(_long(0x3705, method))
    props.append(_long(0x0E21, 0))          # attach number
    string(0x3707, att.filename)
    string(0x3001, att.filename or (att.item.subject if att.item else ""))
    string(0x370E, att.mimetype)
    string(0x3712, att.content_id)

    if att.item is not None:
        inner = prefix + [_stream(0x3701, PT_OBJECT)]
        writer.addEntry(inner, storage=True)
        _write_item(writer, inner, att.item, top=False)
        props.append(_prop(0x3701, PT_OBJECT, struct.pack("<II", 0xFFFFFFFF, 4)))
    elif att.data is not None:
        writer.addEntry(prefix + [_stream(0x3701, PT_BINARY)], att.data)
        props.append(_var(0x3701, PT_BINARY, len(att.data)))

    writer.addEntry(prefix + ["__properties_version1.0"], b"\x00" * 8 + b"".join(props))


def build(item: Item) -> bytes:
    """The bytes of a ``.msg`` file holding ``item``."""
    import io

    writer = OleWriter()
    writer.addEntry(["__nameid_version1.0"], storage=True)
    for name in ("__substg1.0_00020102", "__substg1.0_00030102", "__substg1.0_00040102"):
        writer.addEntry(["__nameid_version1.0", name], b"")
    _write_item(writer, [], item, top=True)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()
