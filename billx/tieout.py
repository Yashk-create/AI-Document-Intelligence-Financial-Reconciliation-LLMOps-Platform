"""Tie-out: compare two independent registers of the same documents.

Used to compare (a) what was extracted from paper, (b) what the GST portal
says was filed, and (c) what the accounting system reads back after posting.
A tie-out is a way of finding differences, not of making them disappear:

* every key appears in exactly one bucket;
* a document number that occurs twice in a register is a DUPLICATE, never a
  match, even if its amounts happen to add up;
* a row whose document number has no letters or digits is UNKEYED and never
  matches anything;
* the residual is reported, not plugged.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

TOKEN = re.compile(r"[A-Z]+|[0-9]+")


def normalise_doc_no(value, strip_prefixes: tuple[str, ...] = ()) -> str | None:
    """Canonical form of a document number, or None if it has no letters/digits.

    'GS/000123', 'gs-123', 'GS 123' and 'GS123' all become 'GS/123'.
    Different series stay different: 'GS/12' != 'PB/12', 'GS/1/23' != 'GS/12/3'.
    A bare '123' does NOT match 'GS/123' unless 'GS' is passed in strip_prefixes,
    for registers (such as a portal export) that drop the series prefix.
    """
    tokens = TOKEN.findall(str(value or "").upper())
    if not tokens:
        return None
    if strip_prefixes and len(tokens) > 1 and tokens[0] in {p.upper() for p in strip_prefixes}:
        tokens = tokens[1:]
    tokens = [t.lstrip("0") or "0" if t.isdigit() else t for t in tokens]
    return "/".join(tokens)


def _amount(value, where: str) -> Decimal:
    try:
        amount = Decimal(str(value).replace(",", "").strip())
    except InvalidOperation:
        raise ValueError(f"{where}: amount is not a number: {value!r}") from None
    if not amount.is_finite():
        raise ValueError(f"{where}: amount is not finite: {value!r}")
    return amount


@dataclass
class TieOut:
    matched: list[str] = field(default_factory=list)
    amount_mismatch: list[tuple[str, Decimal, Decimal]] = field(default_factory=list)
    only_in_left: list[str] = field(default_factory=list)
    only_in_right: list[str] = field(default_factory=list)
    duplicates_left: list[str] = field(default_factory=list)
    duplicates_right: list[str] = field(default_factory=list)
    unkeyed_left: list[dict] = field(default_factory=list)
    unkeyed_right: list[dict] = field(default_factory=list)
    left_total: Decimal = Decimal("0")
    right_total: Decimal = Decimal("0")

    @property
    def residual(self) -> Decimal:
        return self.left_total - self.right_total

    @property
    def clean(self) -> bool:
        return not (self.amount_mismatch or self.only_in_left or self.only_in_right
                    or self.duplicates_left or self.duplicates_right
                    or self.unkeyed_left or self.unkeyed_right)


def _index(rows, side, key, amount, strip_prefixes):
    groups, unkeyed, total = defaultdict(list), [], Decimal("0")
    for n, row in enumerate(rows, start=1):
        value = _amount(row[amount], f"{side} row {n}")
        total += value
        k = normalise_doc_no(row[key], strip_prefixes)
        if k is None:
            unkeyed.append(row)
        else:
            groups[k].append(value)
    single = {k: v[0] for k, v in groups.items() if len(v) == 1}
    dups = sorted(k for k, v in groups.items() if len(v) > 1)
    return single, dups, unkeyed, total


def tie_out(left, right, key="doc_no", amount="total", tolerance=Decimal("0.01"),
            strip_prefixes: tuple[str, ...] = ()) -> TieOut:
    """left/right: iterables of dicts with a document number and an amount."""
    li, ldup, lnone, ltotal = _index(left, "left", key, amount, strip_prefixes)
    ri, rdup, rnone, rtotal = _index(right, "right", key, amount, strip_prefixes)
    result = TieOut(duplicates_left=ldup, duplicates_right=rdup,
                    unkeyed_left=lnone, unkeyed_right=rnone,
                    left_total=ltotal, right_total=rtotal)
    duplicated = set(ldup) | set(rdup)
    for k in sorted((li.keys() | ri.keys()) - duplicated):
        if k not in ri:
            result.only_in_left.append(k)
        elif k not in li:
            result.only_in_right.append(k)
        elif abs(li[k] - ri[k]) > tolerance:
            result.amount_mismatch.append((k, li[k], ri[k]))
        else:
            result.matched.append(k)
    return result
