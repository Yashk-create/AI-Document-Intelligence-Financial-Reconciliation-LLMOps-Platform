"""GSTIN format and mod-36 checksum validation.

A GSTIN is 15 characters: 2-digit state code, 10-character PAN, entity number,
the letter 'Z', and a check character computed over the first 14 characters.

On handwritten bills the check character is the cheapest OCR test available:
a 'Z' misread as '2' or an 'O' read as '0' almost always breaks it.
"""
import re

CHARSET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
PATTERN = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")


def check_char(first14: str) -> str:
    """Return the check character for the first 14 characters of a GSTIN."""
    total = 0
    for i, ch in enumerate(first14.upper()):
        product = CHARSET.index(ch) * (2 if i % 2 else 1)
        total += product // 36 + product % 36
    return CHARSET[(36 - total % 36) % 36]


def validate(gstin: str) -> list[str]:
    """Return a list of problems; an empty list means the GSTIN is valid."""
    g = (gstin or "").strip().upper()
    if not g:
        return ["missing"]
    if len(g) != 15:
        return [f"length {len(g)} != 15"]
    if not PATTERN.match(g):
        return ["bad format"]
    expected = check_char(g[:14])
    if g[14] != expected:
        return [f"checksum: expected '{expected}', got '{g[14]}'"]
    return []


def is_valid(gstin: str) -> bool:
    return not validate(gstin)


def state_code(gstin: str) -> str:
    return (gstin or "")[:2]
