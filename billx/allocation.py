"""Offline payment-to-document allocation with a no-over-application proof.

Context: bills and invoices had been posted to the accounting system without
their payment links, so dozens showed as Overdue even though they were paid.
Clicking "Apply" by hand across 50+ documents invites mistakes, and every API
call counted against a 1,000-calls/day quota. So the whole allocation was
computed locally first, proven safe, and only then written.

Rule: per party, apply each payment (in date order) to that party's open
documents oldest-first (FIFO). Whatever cannot be applied is an advance and is
reported, never forced onto a document.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

ZERO = Decimal("0")


@dataclass(frozen=True)
class Document:
    doc_id: str
    party: str
    date: str          # ISO date
    total: Decimal


@dataclass(frozen=True)
class Payment:
    pay_id: str
    party: str
    date: str
    amount: Decimal


@dataclass(frozen=True)
class Allocation:
    pay_id: str
    doc_id: str
    amount: Decimal


@dataclass
class Plan:
    allocations: list[Allocation]
    unapplied: dict[str, Decimal]       # pay_id -> amount left over (advance)
    open_balance: dict[str, Decimal]    # doc_id -> amount still due

    def applied_total(self) -> Decimal:
        return sum((a.amount for a in self.allocations), ZERO)


def plan_fifo(documents: list[Document], payments: list[Payment]) -> Plan:
    check_inputs(documents, payments)
    remaining = {d.doc_id: d.total for d in documents}
    docs_by_party = defaultdict(list)
    for d in sorted(documents, key=lambda d: (d.date, d.doc_id)):
        docs_by_party[d.party].append(d)

    allocations, unapplied = [], {}
    for p in sorted(payments, key=lambda p: (p.date, p.pay_id)):
        left = p.amount
        for d in docs_by_party.get(p.party, []):
            if left <= ZERO:
                break
            take = min(left, remaining[d.doc_id])
            if take <= ZERO:
                continue
            allocations.append(Allocation(p.pay_id, d.doc_id, take))
            remaining[d.doc_id] -= take
            left -= take
        if left > ZERO:
            unapplied[p.pay_id] = left

    plan = Plan(allocations, unapplied, remaining)
    verify(plan, documents, payments)
    return plan


class AllocationError(AssertionError):
    """The plan (or its inputs) would put the books in a wrong state."""


def _positive(value, what: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= ZERO:
        raise AllocationError(f"{what} must be a positive finite Decimal, got {value!r}")


def check_inputs(documents: list[Document], payments: list[Payment]) -> None:
    for kind, ids in (("document", [d.doc_id for d in documents]),
                      ("payment", [p.pay_id for p in payments])):
        seen = set()
        for i in ids:
            if i in seen:
                raise AllocationError(f"duplicate {kind} id {i!r}")
            seen.add(i)
    for d in documents:
        _positive(d.total, f"document {d.doc_id} total")
    for p in payments:
        _positive(p.amount, f"payment {p.pay_id} amount")


def verify(plan: Plan, documents: list[Document], payments: list[Payment]) -> None:
    """Raise AllocationError unless the plan is exactly consistent with its inputs.

    Every payment must balance (applied + unapplied == amount), including payments
    with no allocations; every document's open balance must equal its total minus
    what was applied; no document is over-applied; no allocation crosses parties
    or refers to an unknown document or payment.
    """
    check_inputs(documents, payments)
    docs = {d.doc_id: d for d in documents}
    pays = {p.pay_id: p for p in payments}

    per_doc, per_pay = defaultdict(lambda: ZERO), defaultdict(lambda: ZERO)
    for a in plan.allocations:
        if a.doc_id not in docs:
            raise AllocationError(f"allocation to unknown document {a.doc_id!r}")
        if a.pay_id not in pays:
            raise AllocationError(f"allocation from unknown payment {a.pay_id!r}")
        _positive(a.amount, f"allocation {a.pay_id}->{a.doc_id}")
        if docs[a.doc_id].party != pays[a.pay_id].party:
            raise AllocationError(f"cross-party allocation {a}")
        per_doc[a.doc_id] += a.amount
        per_pay[a.pay_id] += a.amount

    for doc_id, d in docs.items():
        applied = per_doc[doc_id]
        if applied > d.total:
            raise AllocationError(f"{doc_id} over-applied: {applied} > {d.total}")
        if plan.open_balance.get(doc_id) != d.total - applied:
            raise AllocationError(
                f"{doc_id} open balance {plan.open_balance.get(doc_id)} != {d.total - applied}")

    for pay_id in plan.unapplied:
        if pay_id not in pays:
            raise AllocationError(f"unapplied entry for unknown payment {pay_id!r}")
    for pay_id, p in pays.items():
        left = plan.unapplied.get(pay_id, ZERO)
        if left < ZERO:
            raise AllocationError(f"{pay_id} has negative unapplied amount {left}")
        if per_pay[pay_id] + left != p.amount:
            raise AllocationError(
                f"{pay_id} does not balance: applied {per_pay[pay_id]} + unapplied {left} != {p.amount}")
