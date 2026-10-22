"""Field-level accuracy of extracted bills against hand-checked labels.

    python eval/score.py LABELS_DIR PREDICTIONS_DIR [--md report.md]

Both directories hold one JSON per page with the same file name. A field counts
as correct on exact match after light normalisation (case, whitespace, numeric
value for money/weights). An empty prediction for a non-empty label counts as
an abstention, reported separately from a wrong value: in this domain an honest
"" is much cheaper than a confident wrong digit.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

TEXT_FIELDS = ["invoice_no", "memo_type", "date_iso", "buyer_name", "buyer_gstin",
               "state_code", "despatch_through"]
MONEY_FIELDS = ["total", "cgst_9", "sgst_9", "total_amt_gst", "grand_total"]
LINE_FIELDS = ["particulars", "hsn_code", "weight", "rate_per_mt", "amount"]
NUMERIC = set(MONEY_FIELDS) | {"weight", "rate_per_mt", "amount"}


def norm(field: str, value) -> str:
    s = "" if value is None else str(value).strip()
    if field in NUMERIC and s:
        # Only plain decimals are compared by value; "NaN", "1e5", "-5" stay as text
        # so they can never equal a real label by accident.
        if re.fullmatch(r"\d+(\.\d+)?", s.replace(",", "")):
            return str(Decimal(s.replace(",", "")).normalize())
        return s
    return re.sub(r"\s+", " ", s).upper()


def compare(label: dict, pred: dict) -> dict[str, str]:
    """field -> 'correct' | 'wrong' | 'abstained'"""
    pairs = [(f, label.get(f), pred.get(f)) for f in TEXT_FIELDS + MONEY_FIELDS]
    l_items, p_items = label.get("line_items", []), pred.get("line_items", [])
    for i, li in enumerate(l_items):
        pi = p_items[i] if i < len(p_items) else {}
        pairs += [(f"line.{f}", li.get(f), pi.get(f)) for f in LINE_FIELDS]

    out = {}
    for field, truth, guess in pairs:
        base = field.split(".")[-1]
        t, g = norm(base, truth), norm(base, guess)
        if not t:
            continue  # nothing to score
        out[field] = "correct" if t == g else ("abstained" if not g else "wrong")
    return out


def score(labels_dir: Path, preds_dir: Path) -> dict:
    per_field = defaultdict(lambda: {"correct": 0, "wrong": 0, "abstained": 0})
    docs, perfect, missing = 0, 0, []
    for label_path in sorted(labels_dir.glob("*.json")):
        pred_path = preds_dir / label_path.name
        if not pred_path.exists():
            missing.append(label_path.name)
            continue
        result = compare(json.loads(label_path.read_text(encoding="utf-8")),
                         json.loads(pred_path.read_text(encoding="utf-8")))
        docs += 1
        perfect += all(v == "correct" for v in result.values())
        for field, outcome in result.items():
            per_field[field][outcome] += 1
    return {"documents": docs, "fully_correct": perfect, "missing_predictions": missing,
            "fields": dict(per_field)}


def to_markdown(report: dict) -> str:
    lines = [f"Documents scored: {report['documents']}  |  fully correct: {report['fully_correct']}",
             "", "| Field | Correct | Wrong | Abstained | Accuracy |", "|---|---:|---:|---:|---:|"]
    for field, c in report["fields"].items():
        n = c["correct"] + c["wrong"] + c["abstained"]
        lines.append(f"| {field} | {c['correct']} | {c['wrong']} | {c['abstained']} | "
                     f"{c['correct'] / n:.1%} |")
    if report["missing_predictions"]:
        lines += ["", "Missing predictions: " + ", ".join(report["missing_predictions"])]
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("labels", type=Path)
    ap.add_argument("predictions", type=Path)
    ap.add_argument("--md", type=Path, help="also write the table to this file")
    args = ap.parse_args(argv)
    md = to_markdown(score(args.labels, args.predictions))
    print(md)
    if args.md:
        args.md.write_text(md + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
