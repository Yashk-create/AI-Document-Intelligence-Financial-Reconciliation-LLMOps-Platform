"""The output contract for one extracted bill page.

The model must return exactly these keys with exactly these types. Money and
weights are strings ("709170.00") so the model never emits floats with binary
rounding artefacts and so an unreadable value can be "" rather than a guess.
"""
import re
from decimal import Decimal, InvalidOperation

TOP_LEVEL = {
    "page": int,
    "invoice_no": str,
    "memo_type": str,
    "date_raw": str,
    "date_iso": str,
    "buyer_name": str,
    "buyer_address": str,
    "buyer_gstin": str,
    "state_code": str,
    "broker": str,
    "despatch_through": str,
    "line_items": list,
    "total": str,
    "cgst_9": str,
    "sgst_9": str,
    "total_amt_gst": str,
    "grand_total": str,
    "amount_in_words": str,
    "seller_gstin": str,
    "page_is_blank": bool,
    "unreadable_fields": list,
    "extraction_notes": str,
}

LINE_ITEM = {
    "s_no": int,
    "particulars": str,
    "hsn_code": str,
    "weight": str,
    "weight_unit": str,
    "rate_per_mt": str,
    "amount": str,
}

MONEY_FIELDS = ("total", "cgst_9", "sgst_9", "total_amt_gst", "grand_total")
MEMO_TYPES = ("CASH MEMO", "CREDIT MEMO")

# Plain non-negative decimals only. Rejects NaN, Infinity, exponents ("1e5"),
# signs and stray characters, all of which Decimal() would otherwise accept.
PLAIN_NUMBER = re.compile(r"^\d+(\.\d+)?$")
MONEY = re.compile(r"^\d+(\.\d{1,2})?$")


def to_decimal(value: str) -> Decimal | None:
    """Parse a plain non-negative decimal string; None for '' or anything else."""
    if value is None:
        return None
    text = str(value).replace(",", "").strip()
    if not PLAIN_NUMBER.match(text):
        return None
    try:
        return Decimal(text)
    except InvalidOperation:  # pragma: no cover - the regex already excludes these
        return None


def _check_keys(obj: dict, spec: dict, where: str) -> list[str]:
    issues = []
    missing = spec.keys() - obj.keys()
    extra = obj.keys() - spec.keys()
    for key in sorted(missing):
        issues.append(f"{where}: missing key '{key}'")
    for key in sorted(extra):
        issues.append(f"{where}: unexpected key '{key}'")
    for key, typ in spec.items():
        if key in obj and not isinstance(obj[key], typ):
            # bool is a subclass of int; don't accept True as a page number
            issues.append(f"{where}: '{key}' should be {typ.__name__}")
        elif key in obj and typ is int and isinstance(obj[key], bool):
            issues.append(f"{where}: '{key}' should be int")
    return issues


def validate_shape(doc: dict) -> list[str]:
    """Structural checks only: keys, types, enum values, numeric strings."""
    if not isinstance(doc, dict):
        return ["document is not a JSON object"]
    issues = _check_keys(doc, TOP_LEVEL, "doc")
    if issues:
        return issues

    if doc["memo_type"] and doc["memo_type"] not in MEMO_TYPES:
        issues.append(f"doc: memo_type '{doc['memo_type']}' not in {MEMO_TYPES}")

    for i, item in enumerate(doc["line_items"], start=1):
        if not isinstance(item, dict):
            issues.append(f"line {i}: not an object")
            continue
        issues += _check_keys(item, LINE_ITEM, f"line {i}")
        for key in ("weight", "rate_per_mt", "amount"):
            val = item.get(key, "")
            if val != "" and to_decimal(val) is None:
                issues.append(f"line {i}: '{key}' is not numeric: {val!r}")

        amount = item.get("amount", "")
        if to_decimal(amount) is not None and not MONEY.match(str(amount).replace(",", "").strip()):
            issues.append(f"line {i}: 'amount' has more than 2 decimals: {amount!r}")

    for key in MONEY_FIELDS:
        if doc[key] == "":
            continue
        if to_decimal(doc[key]) is None:
            issues.append(f"doc: '{key}' is not numeric: {doc[key]!r}")
        elif not MONEY.match(doc[key].replace(",", "").strip()):
            issues.append(f"doc: '{key}' has more than 2 decimals: {doc[key]!r}")

    for name in doc["unreadable_fields"]:
        if not isinstance(name, str):
            issues.append(f"doc: unreadable_fields entry is not a string: {name!r}")
    return issues
