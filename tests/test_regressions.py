"""One test per bug found in review. Each reproduced a false acceptance, a false
reconciliation match, a crash, or a wrong exit status before it was fixed."""
import copy
import json
from decimal import Decimal as D
from pathlib import Path

import pytest

import run
from billx import images, invariants
from billx.allocation import (Allocation, AllocationError, Document, Payment, Plan,
                              plan_fifo, verify)
from billx.tieout import tie_out

ROOT = Path(__file__).resolve().parents[1]
BILL = json.loads((ROOT / "samples" / "bills" / "bill_01.json").read_text(encoding="utf-8"))
MONEY = ("total", "cgst_9", "sgst_9", "total_amt_gst", "grand_total")


def bill(**changes):
    doc = copy.deepcopy(BILL)
    doc.update(changes)
    return doc


# --- required fields (was: accepted with no errors) ---------------------------

@pytest.mark.parametrize("field", ["invoice_no", "date_iso", "memo_type", "buyer_gstin", "seller_gstin"])
def test_missing_required_field_is_an_error(field):
    issues = invariants.check(bill(**{field: ""}))
    assert any(i.field == field and i.severity == "error" for i in issues)
    assert invariants.status(issues) == "rejected"


def test_bill_without_line_items_is_rejected():
    assert invariants.status(invariants.check(bill(line_items=[]))) == "rejected"


def test_invalid_date_is_rejected():
    assert invariants.status(invariants.check(bill(date_iso="2025-02-30"))) == "rejected"


# --- unreadable fields (was: accepted) ---------------------------------------

def test_everything_unreadable_is_never_accepted():
    doc = bill(line_items=[], unreadable_fields=list(MONEY), **{k: "" for k in MONEY})
    assert invariants.status(invariants.check(doc)) != "accepted"


def test_unreadable_line_field_needs_review():
    doc = bill(unreadable_fields=["line_items[1].weight"])
    doc["line_items"][0]["weight"] = ""
    assert invariants.status(invariants.check(doc)) == "needs_review"


def test_unknown_unreadable_name_is_an_error():
    assert invariants.status(invariants.check(bill(unreadable_fields=["colour"]))) == "rejected"


def test_unreadable_field_that_has_a_value_is_contradictory():
    issues = invariants.check(bill(unreadable_fields=["grand_total"]))
    assert any("listed as unreadable but has a value" in i.message for i in issues)


def test_blank_page_with_amounts_is_rejected():
    assert invariants.status(invariants.check(bill(page_is_blank=True))) == "rejected"


# --- non-finite and malformed numbers (was: crash / accepted as numbers) -----

@pytest.mark.parametrize("value", ["NaN", "nan", "Infinity", "-Infinity", "sNaN", "1e5",
                                   "-709170.00", "+709170.00", "709170.00.5", "0x10"])
def test_non_plain_numbers_are_schema_errors_not_crashes(value):
    issues = invariants.check(bill(total=value))
    assert [i.field for i in issues] == ["schema"]
    assert "not numeric" in issues[0].message


@pytest.mark.parametrize("value", ["NaN", "Infinity", "1e2"])
def test_non_plain_line_numbers_are_schema_errors(value):
    doc = bill()
    doc["line_items"][0]["weight"] = value
    assert invariants.status(invariants.check(doc)) == "rejected"


def test_money_with_three_decimals_is_rejected():
    assert invariants.status(invariants.check(bill(total="709170.005"))) == "rejected"


# --- paise comparison (was: 836820.99 accepted against words for 836820) ------

def test_words_without_paise_cannot_hide_wrong_rupees():
    doc = bill(amount_in_words="Eight Lakh Thirty Six Thousand Eight Hundred Twenty One Rupees Only")
    assert invariants.status(invariants.check(doc)) == "rejected"


def test_words_without_paise_flag_unchecked_paise():
    doc = bill(amount_in_words="Eight Lakh Thirty Six Thousand Eight Hundred Twenty Rupees Only")
    issues = invariants.check(doc)
    assert invariants.status(issues) == "accepted"
    assert any(i.severity == "warn" and "not cross-checked" in i.message for i in issues)


def test_words_with_wrong_paise_are_rejected():
    doc = bill(amount_in_words="Eight Lakh Thirty Six Thousand Eight Hundred Twenty Rupees Fifty Paise")
    assert invariants.status(invariants.check(doc)) == "rejected"


def test_unparseable_words_block_acceptance():
    assert invariants.status(invariants.check(bill(amount_in_words="Eight Lakh Banana"))) == "rejected"


# --- invoice-number normalisation (was: GS/12 matched PB/12, '' matched '-') ---

def test_different_series_never_match():
    t = tie_out([{"doc_no": "GS/12", "total": "5"}], [{"doc_no": "PB/12", "total": "5"}])
    assert t.matched == [] and not t.clean


def test_separator_positions_matter():
    t = tie_out([{"doc_no": "GS/1/23", "total": "5"}], [{"doc_no": "GS/12/3", "total": "5"}])
    assert t.matched == []


def test_numberless_rows_never_match():
    t = tie_out([{"doc_no": "N/A", "total": "5"}, {"doc_no": "", "total": "5"}],
                [{"doc_no": "-", "total": "5"}])
    assert t.matched == [] and len(t.unkeyed_left) == 1 and len(t.unkeyed_right) == 1
    assert t.only_in_left == ["N/A"] and not t.clean


def test_duplicate_rows_that_sum_correctly_are_not_a_match():
    t = tie_out([{"doc_no": "7", "total": "10"}],
                [{"doc_no": "7", "total": "5"}, {"doc_no": "7", "total": "5"}])
    assert t.matched == [] and t.duplicates_right == ["7"] and not t.clean


@pytest.mark.parametrize("value", ["NaN", "Infinity", "abc"])
def test_non_finite_register_amount_raises_clearly(value):
    with pytest.raises(ValueError, match="right row 1"):
        tie_out([{"doc_no": "7", "total": "5"}], [{"doc_no": "7", "total": value}])


def test_duplicate_photo_check_uses_normalised_numbers_and_values():
    a = dict(BILL, invoice_no="GS/000101", grand_total="836820.6")
    b = dict(BILL, invoice_no="gs-101", grand_total="836820.60")
    c = dict(BILL, invoice_no="PB/101", grand_total="836820.60")
    assert images.duplicate_documents({"a": a, "b": b, "c": c}) == [["a", "b"]]


# --- allocation verification (was: passed or crashed with KeyError) ----------

DOCS = [Document("B1", "acme", "2025-01-01", D("100"))]
PAYS = [Payment("P1", "acme", "2025-01-02", D("100"))]


def test_unaccounted_payment_fails_verification():
    with pytest.raises(AllocationError, match="P1 does not balance"):
        verify(Plan([], {}, {"B1": D("100")}), DOCS, PAYS)


def test_unknown_document_is_a_clear_error():
    with pytest.raises(AllocationError, match="unknown document"):
        verify(Plan([Allocation("P1", "B9", D("1"))], {"P1": D("99")}, {"B1": D("100")}), DOCS, PAYS)


def test_unknown_unapplied_payment_is_an_error():
    plan = Plan([Allocation("P1", "B1", D("100"))], {"P9": D("5")}, {"B1": D("0")})
    with pytest.raises(AllocationError, match="unknown payment"):
        verify(plan, DOCS, PAYS)


def test_wrong_open_balance_fails_verification():
    with pytest.raises(AllocationError, match="open balance"):
        verify(Plan([Allocation("P1", "B1", D("100"))], {}, {"B1": D("100")}), DOCS, PAYS)


@pytest.mark.parametrize("amount", [D("-50"), D("0"), D("NaN"), D("Infinity"), 50.0])
def test_bad_payment_amounts_are_rejected(amount):
    with pytest.raises(AllocationError, match="positive finite Decimal"):
        plan_fifo(DOCS, [Payment("P1", "acme", "2025-01-02", amount)])


def test_duplicate_ids_are_rejected():
    with pytest.raises(AllocationError, match="duplicate payment id"):
        plan_fifo(DOCS, PAYS + PAYS)


# --- CLI exit status (was: 0 on review items, tracebacks on bad input) -------

SAMPLES = ROOT / "samples"


def test_validate_exit_codes(tmp_path, capsys):
    assert run.main(["validate", str(SAMPLES / "bills" / "bill_01.json")]) == 0
    assert run.main(["validate", str(SAMPLES / "misread_bill_02.json")]) == 1
    review = tmp_path / "review.json"
    review.write_text(json.dumps(bill(buyer_gstin="", unreadable_fields=["buyer_gstin"])))
    assert run.main(["validate", str(review)]) == 1
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    assert run.main(["validate", str(broken)]) == 2
    assert run.main(["validate", str(tmp_path / "missing.json")]) == 2
    assert "NEEDS_REVIEW" in capsys.readouterr().out


def test_images_on_empty_folder_is_bad_input_not_a_crash(tmp_path):
    (tmp_path / "empty").mkdir()
    assert run.main(["images", str(tmp_path / "empty"), str(tmp_path / "out")]) == 2
    assert run.main(["images", str(tmp_path / "nope"), str(tmp_path / "out")]) == 2


def test_extract_exit_code_is_nonzero_when_anything_needs_review(tmp_path, monkeypatch):
    from billx.extract import PageResult
    img = tmp_path / "imgs"
    img.mkdir()
    for n in ("a.jpg", "b.jpg"):
        (img / n).write_bytes(b"x")
    outcomes = iter(["accepted", "needs_review"])
    monkeypatch.setattr(run, "extract_page",
                        lambda image, page, **kw: PageResult(page, next(outcomes), {}, [], 1))
    assert run.main(["extract", str(img), "-o", str(tmp_path / "out")]) == 1
    outcomes = iter(["accepted", "accepted"])
    assert run.main(["extract", str(img), "-o", str(tmp_path / "out")]) == 0
    assert run.main(["extract", str(tmp_path / "nothing")]) == 2
