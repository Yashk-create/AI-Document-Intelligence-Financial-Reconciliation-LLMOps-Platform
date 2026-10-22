from decimal import Decimal as D

import pytest

from billx.allocation import Allocation, Document, Payment, Plan, plan_fifo, verify
from billx.tieout import normalise_doc_no, tie_out


# --- allocation --------------------------------------------------------------

DOCS = [
    Document("B1", "acme", "2025-04-01", D("100000.00")),
    Document("B2", "acme", "2025-04-10", D("50000.00")),
    Document("B3", "acme", "2025-05-01", D("75000.00")),
    Document("B4", "zen", "2025-04-05", D("20000.00")),
]


def test_fifo_pays_oldest_first_and_reports_advance():
    pays = [Payment("P1", "acme", "2025-04-15", D("120000.00")),
            Payment("P2", "acme", "2025-05-20", D("130000.00"))]
    plan = plan_fifo(DOCS, pays)
    assert [(a.pay_id, a.doc_id, a.amount) for a in plan.allocations] == [
        ("P1", "B1", D("100000.00")),
        ("P1", "B2", D("20000.00")),
        ("P2", "B2", D("30000.00")),
        ("P2", "B3", D("75000.00")),
    ]
    assert plan.unapplied == {"P2": D("25000.00")}      # advance, not forced onto a bill
    assert plan.open_balance["B4"] == D("20000.00")     # other party untouched
    assert plan.applied_total() == D("225000.00")


def test_payment_never_crosses_parties():
    plan = plan_fifo(DOCS, [Payment("P1", "zen", "2025-04-06", D("500000.00"))])
    assert {a.doc_id for a in plan.allocations} == {"B4"}
    assert plan.unapplied["P1"] == D("480000.00")


def test_verify_rejects_over_application():
    pays = [Payment("P1", "acme", "2025-04-15", D("150000.00"))]
    bad = Plan([Allocation("P1", "B1", D("150000.00"))], {}, {})
    with pytest.raises(AssertionError, match="over-applied"):
        verify(bad, DOCS, pays)


def test_verify_rejects_cross_party():
    pays = [Payment("P1", "zen", "2025-04-15", D("1000.00"))]
    bad = Plan([Allocation("P1", "B1", D("1000.00"))], {}, {})
    with pytest.raises(AssertionError, match="cross-party"):
        verify(bad, DOCS, pays)


# --- tie-out -----------------------------------------------------------------

@pytest.mark.parametrize("raw, key", [("GS/000123", "GS/123"), ("gs-123", "GS/123"),
                                      ("GS 123", "GS/123"), ("GS123", "GS/123"),
                                      ("123", "123"), ("000", "0"), ("N/A", "N/A"),
                                      ("", None), ("--/", None)])
def test_normalise_doc_no(raw, key):
    assert normalise_doc_no(raw) == key


def test_strip_prefix_for_registers_that_drop_the_series():
    assert normalise_doc_no("GS/0007", strip_prefixes=("GS",)) == "7"
    assert normalise_doc_no("PB/0007", strip_prefixes=("GS",)) == "PB/7"


def test_tie_out_buckets_every_row_and_reports_residual():
    paper = [{"doc_no": "GS/0001", "total": "1000.00"},
             {"doc_no": "GS/0002", "total": "2500.24"},
             {"doc_no": "GS/0003", "total": "700.00"},
             {"doc_no": "GS/0005", "total": "10.00"}]
    portal = [{"doc_no": "1", "total": "1000.00"},
              {"doc_no": "2", "total": "2500.00"},
              {"doc_no": "4", "total": "300.00"},
              {"doc_no": "5", "total": "5.00"},
              {"doc_no": "5", "total": "5.00"}]
    t = tie_out(paper, portal, strip_prefixes=("GS",))
    assert t.matched == ["1"]
    assert t.amount_mismatch == [("2", D("2500.24"), D("2500.00"))]
    assert t.only_in_left == ["3"] and t.only_in_right == ["4"]
    assert t.duplicates_right == ["5"]       # filed twice: a duplicate, never a match
    assert t.residual == D("4210.24") - D("3810.00")
    assert not t.clean


def test_tie_out_within_tolerance_is_clean():
    t = tie_out([{"doc_no": "7", "total": "10.004"}], [{"doc_no": "7", "total": "10.00"}])
    assert t.clean and t.matched == ["7"]
