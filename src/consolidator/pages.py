"""Page geometry shared by the converters and the assembler."""

from __future__ import annotations

# Dimensions in PDF points (1/72 inch).
LETTER = (612.0, 792.0)
A4 = (595.276, 841.890)
LEGAL = (612.0, 1008.0)

PAGE_SIZES: dict[str, tuple[float, float] | None] = {
    "letter": LETTER,
    "a4": A4,
    "legal": LEGAL,
    "original": None,  # keep whatever each source page already is
}

DEFAULT_PAGE_SIZE = "letter"


def resolve(name: str) -> tuple[float, float] | None:
    """Look up a page size by name; ``None`` means 'leave pages alone'."""
    key = (name or DEFAULT_PAGE_SIZE).lower()
    if key not in PAGE_SIZES:
        raise ValueError("unknown page size %r (expected one of %s)"
                         % (name, ", ".join(sorted(PAGE_SIZES))))
    return PAGE_SIZES[key]
