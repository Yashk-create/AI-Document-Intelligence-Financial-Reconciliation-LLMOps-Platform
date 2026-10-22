"""Deterministic checks run on every extracted page.

The idea that made the pipeline usable: a handwritten bill is internally
redundant. Weight x rate gives the amount, the amounts give the total, 9% of
the total gives each GST head, and the grand total is written again in words.
A misread digit almost always breaks one of these equalities, so the model's
output is treated as a candidate and these rules decide what happens to it.

Every issue has a severity:

* error  - something is wrong or missing; worth a targeted re-read
* review - the model could not read something, or the page claims to be blank;
           only a person can settle it
* warn   - informational, never blocks

A page is accepted ONLY when it has no errors and no review items. In
particular, a field the model marked unreadable is never silently accepted.
"""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from . import gstin as gstin_mod
from .amount_words import words_to_amount
from .schema import LINE_ITEM, MONEY_FIELDS, TOP_LEVEL, to_decimal, validate_shape

LINE_TOL = Decimal("0.05")   # weight x rate is often written rounded to the rupee
MONEY_TOL = Decimal("0.01")  # bills round each GST head to the paisa, sometimes down
GST_RATE = Decimal("0.09")

# Fields a non-blank bill must have (or explicitly declare unreadable).
REQUIRED = ("invoice_no", "memo_type", "date_iso", "buyer_gstin", "seller_gstin") + MONEY_FIELDS
REQUIRED_LINE = ("particulars", "weight", "rate_per_mt", "amount")


@dataclass
class Issue:
    field: str
    message: str
    severity: str = "error"   # error | review | warn

    def __str__(self) -> str:
        return f"[{self.severity}] {self.field}: {self.message}"


def _q2(x: Decimal) -> Decimal:
    return x.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _close(a: Decimal, b: Decimal, tol: Decimal) -> bool:
    return abs(a - b) <= tol


def _valid_unreadable_names(doc: dict) -> set[str]:
    names = set(TOP_LEVEL) | set(LINE_ITEM)   # bare "weight" is accepted for a 1-line bill
    for i in range(1, len(doc["line_items"]) + 1):
        names |= {f"line_items[{i}].{k}" for k in LINE_ITEM}
    return names


def _check_blank(doc: dict) -> list[Issue]:
    # A page that claims to be blank must really be empty, and even then a
    # person confirms it: a real bill silently marked blank would vanish.
    issues = []
    if doc["line_items"] or any(doc[k] for k in MONEY_FIELDS):
        issues.append(Issue("page_is_blank", "page marked blank but has line items or amounts"))
    issues.append(Issue("page_is_blank", "page marked blank; confirm before skipping", "review"))
    return issues


def _check_required(doc: dict, unreadable: set[str]) -> list[Issue]:
    issues = []
    for key in REQUIRED:
        if doc[key] == "" and key not in unreadable:
            issues.append(Issue(key, "required field is empty and not listed in unreadable_fields"))
    if not doc["line_items"]:
        issues.append(Issue("line_items", "a bill must have at least one line item"))
    for i, item in enumerate(doc["line_items"], start=1):
        for key in REQUIRED_LINE:
            name = f"line_items[{i}].{key}"
            if item[key] == "" and name not in unreadable and key not in unreadable:
                issues.append(Issue(name, "required field is empty and not listed in unreadable_fields"))
    if doc["date_iso"]:
        try:
            date.fromisoformat(doc["date_iso"])
        except ValueError:
            issues.append(Issue("date_iso", f"not a valid YYYY-MM-DD date: {doc['date_iso']!r}"))
    return issues


def _check_unreadable(doc: dict, unreadable: set[str]) -> list[Issue]:
    issues = []
    known = _valid_unreadable_names(doc)
    for name in sorted(unreadable):
        if name not in known:
            issues.append(Issue("unreadable_fields", f"unknown field name {name!r}"))
            continue
        issues.append(Issue(name, "marked unreadable by the model; needs human review", "review"))
        if name in doc and doc[name] not in ("", [], None):
            issues.append(Issue(name, "listed as unreadable but has a value"))
    return issues


def _check_words(words: str, grand: Decimal) -> list[Issue]:
    """Compare the amount in words with the digits, rupees and paise separately."""
    try:
        spoken = words_to_amount(words)
    except ValueError as exc:
        return [Issue("amount_in_words", f"could not parse: {exc}")]

    mentions_paise = any(w in words.lower() for w in ("paise", "paisa"))
    digits = _q2(grand)
    if mentions_paise:
        if spoken != digits:
            return [Issue("grand_total", f"digits {digits} but words say {spoken}")]
        return []
    # Words without paise can only confirm the rupee part.
    if spoken != int(digits):
        return [Issue("grand_total", f"digits {digits} but words say {spoken} (rupees)")]
    if digits != int(digits):
        return [Issue("grand_total",
                      f"words give no paise, so the paise in {digits} are not cross-checked", "warn")]
    return []


def check(doc: dict, seller_state: str | None = None) -> list[Issue]:
    shape = validate_shape(doc)
    if shape:
        return [Issue("schema", m) for m in shape]
    if doc["page_is_blank"]:
        return _check_blank(doc)

    unreadable = set(doc["unreadable_fields"])
    issues = _check_required(doc, unreadable) + _check_unreadable(doc, unreadable)

    # 1. line items: amount == weight * rate
    amounts = []
    for i, item in enumerate(doc["line_items"], start=1):
        w, r, a = (to_decimal(item[k]) for k in ("weight", "rate_per_mt", "amount"))
        if a is not None:
            amounts.append(a)
        if None in (w, r, a):
            continue
        if not _close(w * r, a, LINE_TOL):
            issues.append(Issue(
                f"line_items[{i}].amount",
                f"weight {w} x rate {r} = {_q2(w * r)}, but amount reads {a}",
            ))

    money = {k: to_decimal(doc[k]) for k in MONEY_FIELDS}
    total, cgst, sgst = money["total"], money["cgst_9"], money["sgst_9"]
    gst, grand = money["total_amt_gst"], money["grand_total"]

    # 2. total == sum(line amounts); only meaningful when every amount was read
    all_amounts_read = len(amounts) == len(doc["line_items"])
    if total is not None and amounts and all_amounts_read and not _close(sum(amounts), total, MONEY_TOL):
        issues.append(Issue("total", f"line amounts sum to {sum(amounts)}, total reads {total}"))

    # 3. each GST head == 9% of total
    if total is not None:
        expected = total * GST_RATE
        for key, val in (("cgst_9", cgst), ("sgst_9", sgst)):
            if val is not None and not _close(val, expected, MONEY_TOL):
                issues.append(Issue(key, f"9% of {total} is {_q2(expected)}, reads {val}"))

    # 4. total GST == cgst + sgst
    if None not in (cgst, sgst, gst) and not _close(cgst + sgst, gst, MONEY_TOL):
        issues.append(Issue("total_amt_gst", f"{cgst} + {sgst} = {cgst + sgst}, reads {gst}"))

    # 5. grand total == total + GST
    if None not in (total, gst, grand) and not _close(total + gst, grand, MONEY_TOL):
        issues.append(Issue("grand_total", f"{total} + {gst} = {total + gst}, reads {grand}"))

    # 6. words vs digits
    if doc["amount_in_words"] and grand is not None:
        issues += _check_words(doc["amount_in_words"], grand)

    # 7. GSTIN checksums
    for key in ("buyer_gstin", "seller_gstin"):
        if doc[key] and key not in unreadable:
            for problem in gstin_mod.validate(doc[key]):
                issues.append(Issue(key, problem))

    # 8. tax geography: CGST+SGST is only valid for an intra-state buyer
    seller_state = seller_state or gstin_mod.state_code(doc["seller_gstin"])
    buyer_state = doc["state_code"] or gstin_mod.state_code(doc["buyer_gstin"])
    if seller_state and buyer_state and buyer_state != seller_state and (cgst or sgst):
        issues.append(Issue(
            "state_code",
            f"buyer state {buyer_state} != seller state {seller_state}; expected IGST, not CGST+SGST",
        ))

    return issues


def errors(issues: list[Issue]) -> list[Issue]:
    return [i for i in issues if i.severity == "error"]


def reviews(issues: list[Issue]) -> list[Issue]:
    return [i for i in issues if i.severity == "review"]


def status(issues: list[Issue]) -> str:
    """accepted | needs_review | rejected: the single decision callers act on."""
    if errors(issues):
        return "rejected"
    if reviews(issues):
        return "needs_review"
    return "accepted"
