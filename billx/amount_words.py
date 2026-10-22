"""Parse an amount written in English words (Indian numbering) into a Decimal.

Handwritten bills carry the grand total twice: in digits and in words. Parsing the
words independently gives a second reading of the same number, which catches
digit misreads that still look plausible.

    >>> words_to_amount("Nine Lakh Sixty Five Thousand Six Hundred Seven Rupees Fifty Six Paise Only")
    Decimal('965607.56')
"""
import re
from decimal import Decimal

UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fourty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
SCALES = {
    "thousand": 1_000,
    "lakh": 100_000, "lakhs": 100_000, "lac": 100_000, "lacs": 100_000,
    "crore": 10_000_000, "crores": 10_000_000,
}
IGNORED = {"and", "only", "rs", "inr", "rupee", "rupees"}


def _parse_integer(tokens: list[str]) -> int:
    total = 0
    current = 0
    for tok in tokens:
        if tok in UNITS:
            current += UNITS[tok]
        elif tok in TENS:
            current += TENS[tok]
        elif tok == "hundred":
            current = (current or 1) * 100
        elif tok in SCALES:
            total += (current or 1) * SCALES[tok]
            current = 0
        elif tok in IGNORED:
            continue
        else:
            raise ValueError(f"unknown word: {tok!r}")
    return total + current


def words_to_amount(text: str) -> Decimal:
    """Convert '... Rupees ... Paise' to a Decimal with two places."""
    clean = re.sub(r"[^a-z ]", " ", (text or "").lower().replace("-", " "))
    tokens = clean.split()
    if not tokens:
        raise ValueError("empty amount text")

    # Split on 'paise' / 'paisa': words before the last rupee-ish marker are rupees.
    paise_tokens: list[str] = []
    if tokens[-1] in ("paise", "paisa") or "paise" in tokens or "paisa" in tokens:
        end = max(i for i, t in enumerate(tokens) if t in ("paise", "paisa"))
        body = tokens[:end]
        split_at = None
        for i, t in enumerate(body):
            if t in ("rupee", "rupees", "and"):
                split_at = i
        if split_at is not None:
            paise_tokens = body[split_at + 1:]
            tokens = body[:split_at]
        else:
            paise_tokens, tokens = body, []

    rupees = _parse_integer(tokens) if tokens else 0
    paise = _parse_integer(paise_tokens) if paise_tokens else 0
    if paise >= 100:
        raise ValueError(f"paise out of range: {paise}")
    return Decimal(rupees) + Decimal(paise) / 100
