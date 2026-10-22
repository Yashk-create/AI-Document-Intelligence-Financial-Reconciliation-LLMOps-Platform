import copy
import json
from decimal import Decimal
from pathlib import Path

import pytest

from billx import gstin, invariants
from billx.amount_words import words_to_amount
from billx.schema import validate_shape

SAMPLES = Path(__file__).resolve().parents[1] / "samples"
LABELS = sorted((SAMPLES / "bills").glob("bill_*.json"))


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


# --- GSTIN -------------------------------------------------------------------

# Synthetic: state 00 does not exist and a 4th PAN character of Z is not a valid holder type.
SYNTHETIC = "00TSTZV4826M1Z0"


def test_synthetic_gstin_is_valid():
    assert gstin.is_valid(SYNTHETIC)


def test_checksum_catches_every_single_character_misread():
    # Mod-36 with alternating weights detects any single substituted character.
    for pos in range(14):
        for ch in gstin.CHARSET:
            if ch == SYNTHETIC[pos]:
                continue
            first14 = SYNTHETIC[:pos] + ch + SYNTHETIC[pos + 1:14]
            assert gstin.check_char(first14) != SYNTHETIC[14], (pos, ch)


@pytest.mark.parametrize("bad, reason", [
    (SYNTHETIC[:14] + "2", "checksum"),          # check character misread
    (SYNTHETIC[:13] + "2" + SYNTHETIC[14:], "format"),   # constant 'Z' misread as '2'
    (SYNTHETIC[:14], "length"),
    ("", "missing"),
])
def test_gstin_rejects_misreads(bad, reason):
    problems = gstin.validate(bad)
    assert problems and reason in problems[0]


# --- amount in words ---------------------------------------------------------

@pytest.mark.parametrize("words, value", [
    ("Nine Lakh Sixty Five Thousand Six Hundred Seven Rupees Fifty Six Paise Only", "965607.56"),
    ("Rupees One Crore Two Lakh Five Thousand Only", "10205000"),
    ("Three Lakh Sixty-Nine Thousand Nine Hundred Eighty Six and Sixty Four Paise", "369986.64"),
    ("Five Hundred Rupees", "500"),
])
def test_words_to_amount(words, value):
    assert words_to_amount(words) == Decimal(value)


def test_words_to_amount_rejects_garbage():
    with pytest.raises(ValueError):
        words_to_amount("Eleven Lakh Banana")


# --- schema ------------------------------------------------------------------

def test_schema_rejects_extra_and_missing_keys():
    doc = load(LABELS[0])
    doc["confidence"] = 0.9
    del doc["grand_total"]
    issues = validate_shape(doc)
    assert any("unexpected key 'confidence'" in i for i in issues)
    assert any("missing key 'grand_total'" in i for i in issues)


def test_schema_rejects_float_money():
    doc = load(LABELS[0])
    doc["total"] = 836820.6
    assert any("'total' should be str" in i for i in validate_shape(doc))


# --- invariants --------------------------------------------------------------

@pytest.mark.parametrize("path", LABELS, ids=lambda p: p.stem)
def test_every_synthetic_label_passes(path):
    assert invariants.check(load(path), seller_state="00") == []


def test_demo_misread_is_caught():
    issues = invariants.check(load(SAMPLES / "misread_bill_02.json"), seller_state="00")
    fields = {i.field for i in invariants.errors(issues)}
    assert "line_items[1].amount" in fields   # weight x rate no longer matches
    assert "total" in fields                  # line amounts no longer sum to total
    assert "buyer_gstin" in fields            # checksum caught the misread check character


def test_words_disagreeing_with_digits_is_caught():
    doc = load(LABELS[0])
    doc["amount_in_words"] = "Eight Lakh Thirty Six Thousand Eight Hundred Twenty Rupees Only"
    doc["grand_total"] = "863820.60"  # digits misread; words still say 836820
    doc["total_amt_gst"] = str(Decimal(doc["grand_total"]) - Decimal(doc["total"]))
    issues = invariants.check(doc)
    assert any(i.field == "grand_total" and "words say" in i.message for i in issues)


def test_rounding_down_by_half_paisa_is_accepted():
    # Bills often round 9% of 818311.50 (= 73648.035) down to 73648.03, not up.
    doc = load(LABELS[4])
    assert doc["total"] == "818311.50"
    doc["cgst_9"] = doc["sgst_9"] = "73648.03"
    doc["total_amt_gst"] = "147296.06"
    doc["grand_total"] = "965607.56"
    doc["amount_in_words"] = "Nine Lakh Sixty Five Thousand Six Hundred Seven Rupees Fifty Six Paise"
    assert invariants.errors(invariants.check(doc)) == []


def test_interstate_buyer_on_cgst_sgst_is_flagged():
    doc = load(LABELS[0])
    doc["state_code"] = "27"
    issues = invariants.check(doc, seller_state="00")
    assert any(i.field == "state_code" and "IGST" in i.message for i in issues)


def test_unreadable_field_goes_to_review_not_error():
    doc = load(LABELS[0])
    doc["buyer_gstin"] = ""
    doc["unreadable_fields"] = ["buyer_gstin"]
    issues = invariants.check(doc)
    assert [str(i) for i in issues] == [
        "[review] buyer_gstin: marked unreadable by the model; needs human review"]
    assert invariants.status(issues) == "needs_review"


def test_empty_money_must_be_declared_unreadable():
    doc = load(LABELS[0])
    doc["grand_total"] = ""
    assert any("unreadable_fields" in i.message for i in invariants.check(doc))


def test_blank_page_needs_confirmation():
    doc = copy.deepcopy(load(LABELS[0]))
    doc.update(page_is_blank=True, line_items=[])
    for k in ("total", "cgst_9", "sgst_9", "total_amt_gst", "grand_total"):
        doc[k] = ""
    assert invariants.status(invariants.check(doc)) == "needs_review"
