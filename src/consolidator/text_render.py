"""Plain text to PDF, without losing a character.

Three things the obvious approach gets wrong, each of which silently drops text:

* **Encoding.** Notepad writes UTF-16 with a byte-order mark, older tools write
  windows-1252, and reading everything as UTF-8 turned the first into noise and
  the curly quotes of the second into replacement characters.
* **Glyphs.** ReportLab's built-in Courier covers Western European text only, so
  Cyrillic, Greek, Chinese, Japanese and Korean simply did not appear. Text is
  drawn with a TrueType monospace face, and each character no face covers falls
  back to one of the system fonts that does.
* **Wrapping.** Breaking only at spaces leaves a long URL, a hash or a CSV row
  wider than the page, and whatever ran off the edge was gone. Lines are broken
  at a space when one is available and mid-word when not.
"""

from __future__ import annotations

import codecs
import os
from pathlib import Path

_FONT_DIR = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"

# Monospace first: it keeps columns aligned. The rest are fallbacks for scripts
# the monospace faces lack, in rough order of how much they cover.
_FACES = (
    ("ConsText", "consola.ttf"),
    ("CourText", "cour.ttf"),
    ("LuconText", "lucon.ttf"),
    ("GothicText", "msgothic.ttc"),      # Japanese, and monospaced
    ("SimSunText", "simsun.ttc"),        # Chinese
    ("YaHeiText", "msyh.ttc"),           # Chinese
    ("MalgunText", "malgun.ttf"),        # Korean
    ("NirmalaText", "Nirmala.ttc"),      # Indic scripts
    ("SegoeText", "segoeui.ttf"),        # Arabic, Hebrew, much else
    ("SymbolText", "seguisym.ttf"),      # symbols, arrows, box drawing
    ("ArialText", "arial.ttf"),
)

_faces: list[tuple[str, set[int]]] | None = None


def _load_faces() -> list[tuple[str, set[int]]]:
    """Register the fonts this machine has, once, with the characters each covers."""
    global _faces
    if _faces is not None:
        return _faces
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    found: list[tuple[str, set[int]]] = []
    for name, filename in _FACES:
        path = _FONT_DIR / filename
        if not path.exists():
            continue
        try:
            if name not in pdfmetrics.getRegisteredFontNames():
                kwargs = {"subfontIndex": 0} if filename.lower().endswith(".ttc") else {}
                pdfmetrics.registerFont(TTFont(name, str(path), **kwargs))
            face = pdfmetrics.getFont(name).face
            found.append((name, set(face.charToGlyph)))
        except Exception:
            # A font that will not load is a font we do without.
            continue
    _faces = found
    return found


def decode_text(data: bytes) -> str:
    """Bytes of a text file as text, in the encoding they were most likely written in."""
    for bom, encoding in ((codecs.BOM_UTF32_LE, "utf-32"), (codecs.BOM_UTF32_BE, "utf-32"),
                          (codecs.BOM_UTF8, "utf-8-sig"),
                          (codecs.BOM_UTF16_LE, "utf-16"), (codecs.BOM_UTF16_BE, "utf-16")):
        if data.startswith(bom):
            return data.decode(encoding, "replace")
    # UTF-16 without a mark: mostly-ASCII text has a NUL in every other byte.
    sample = data[:4096]
    if len(sample) >= 4:
        if sample[1::2].count(0) > len(sample) // 4:
            return data.decode("utf-16-le", "replace")
        if sample[0::2].count(0) > len(sample) // 4:
            return data.decode("utf-16-be", "replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", "replace")


class _Measure:
    """Which font draws each character, and how wide it is."""

    def __init__(self, size: float):
        from reportlab.pdfbase.pdfmetrics import stringWidth

        self.size = size
        self.faces = _load_faces()
        self.primary = self.faces[0][0] if self.faces else "Courier"
        self._stringWidth = stringWidth
        self._font: dict[str, str] = {}
        self._width: dict[str, float] = {}

    def font(self, char: str) -> str:
        font = self._font.get(char)
        if font is None:
            font = self.primary
            code = ord(char)
            for name, covered in self.faces:
                if code in covered:
                    font = name
                    break
            # Nothing covers it: the primary face draws its "missing" box, which
            # at least shows that something was there.
            self._font[char] = font
        return font

    def width(self, char: str) -> float:
        width = self._width.get(char)
        if width is None:
            width = self._stringWidth(char, self.font(char), self.size)
            self._width[char] = width
        return width


def wrap(line: str, usable: float, measure: _Measure) -> list[str]:
    """Break one line to fit ``usable`` points: at a space if possible, else anywhere."""
    if not line:
        return [""]
    pieces: list[str] = []
    start = 0
    width = 0.0
    last_space = -1
    index = 0
    while index < len(line):
        char = line[index]
        char_width = measure.width(char)
        if width + char_width > usable and index > start:
            if last_space > start:
                pieces.append(line[start:last_space])
                start = last_space + 1
            else:
                pieces.append(line[start:index])
                start = index
            width = sum(measure.width(c) for c in line[start:index])
            last_space = -1
            continue
        if char == " ":
            last_space = index
        width += char_width
        index += 1
    pieces.append(line[start:])
    return pieces


def _runs(text: str, measure: _Measure):
    """Split ``text`` into stretches drawn with the same font."""
    run = ""
    font = None
    for char in text:
        char_font = measure.font(char)
        if char_font != font and run:
            yield font, run
            run = ""
        font = char_font
        run += char
    if run:
        yield font, run


def render(text: str, dest: Path, page_size: tuple[float, float], title: str = "") -> int:
    """Lay ``text`` out as literal monospaced lines. Returns the page count."""
    from reportlab.pdfgen import canvas as rl_canvas

    width, height = page_size
    margin = 54.0          # 0.75 inch
    size, leading = 9.5, 12.0
    measure = _Measure(size)

    canvas = rl_canvas.Canvas(str(dest), pagesize=(width, height))
    if title:
        canvas.setTitle(title)
    usable = width - 2 * margin
    y = height - margin
    pages = 1

    # A form feed is a page break in a text file; splitlines() would drop it.
    for block_index, block in enumerate(text.replace("\r\n", "\n").split("\f")):
        if block_index:
            canvas.showPage()
            pages += 1
            y = height - margin
        for raw_line in block.splitlines() or [""]:
            # Expand tabs so column alignment survives; other control characters
            # would only draw as boxes.
            line = "".join(c for c in raw_line.expandtabs(4)
                           if c >= " " or c == "\t")
            for chunk in wrap(line, usable, measure):
                if y < margin:
                    canvas.showPage()
                    pages += 1
                    y = height - margin
                x = margin
                for font, run in _runs(chunk, measure):
                    canvas.setFont(font, size)
                    canvas.drawString(x, y, run)
                    x += sum(measure.width(c) for c in run)
                y -= leading

    canvas.save()
    return pages
