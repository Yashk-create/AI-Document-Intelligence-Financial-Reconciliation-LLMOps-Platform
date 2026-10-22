"""Reconstruct an LLM usage/cost ledger from coding-agent session logs.

The live gateway meter only sees traffic that goes through the gateway. To
price the work done before it existed, this script walks the agent's JSONL
session files, picks out token-usage records, and prices each one with a
fixed local price table.

The output is an ESTIMATE: list prices, no discounts, no provider invoice.
Always report it with its date span.

Privacy: the exported rows hold only a timestamp, model name, token counts and
costs. Session, thread and turn identifiers are never written. They are used
in memory only, to report how many distinct sessions the ledger covers. Don't
commit session logs or the generated CSV (*.csv is git-ignored).

    python cost_analysis/build_cost_ledger.py path/to/agent/sessions ledger.csv
"""
import csv
import glob
import json
import os
import sys
from collections import Counter

# USD per million tokens. Edit to match the model actually used.
PRICES = {
    "input": 2.00,
    "cached_input": 0.20,
    "cache_write": 2.50,
    "output": 10.00,
}


def price(usage: dict, prices=PRICES) -> dict:
    inp = usage.get("input_tokens", 0) or 0
    cached = usage.get("cached_input_tokens", 0) or 0
    write = usage.get("cache_write_input_tokens", 0) or 0
    out = usage.get("output_tokens", 0) or 0
    parts = {
        "cost_input_usd": max(inp - cached, 0) * prices["input"] / 1e6,
        "cost_cached_usd": cached * prices["cached_input"] / 1e6,
        "cost_cache_write_usd": write * prices["cache_write"] / 1e6,
        "cost_output_usd": out * prices["output"] / 1e6,
    }
    parts["estimated_cost_usd"] = sum(parts.values())
    return {k: round(v, 8) for k, v in parts.items()}


def iter_usage_records(root: str):
    for path in glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True):
        with open(path, encoding="utf-8", errors="ignore") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "token_usage_record":
                    continue
                payload = obj.get("payload", {})
                usage = payload.get("usage")
                if isinstance(usage, dict):
                    yield obj, payload, usage


def build(root: str) -> tuple[list[dict], int]:
    """Return (rows, distinct_session_count). Rows never contain session ids."""
    rows, sessions = [], set()
    for obj, payload, usage in iter_usage_records(root):
        sessions.add(payload.get("session_id"))
        rows.append({
            "timestamp": obj.get("timestamp", ""),
            "model": payload.get("model", ""),
            "input_tokens": usage.get("input_tokens", 0) or 0,
            "cached_input_tokens": usage.get("cached_input_tokens", 0) or 0,
            "output_tokens": usage.get("output_tokens", 0) or 0,
            "reasoning_output_tokens": usage.get("reasoning_output_tokens", 0) or 0,
            "total_tokens": usage.get("total_tokens", 0) or 0,
            **price(usage),
        })
    sessions.discard(None)
    return rows, len(sessions)


def summarise(rows: list[dict], sessions: int) -> dict:
    inp = sum(r["input_tokens"] for r in rows)
    cached = sum(r["cached_input_tokens"] for r in rows)
    stamps = sorted(r["timestamp"] for r in rows if r["timestamp"])
    return {
        "calls": len(rows),
        "total_tokens": sum(r["total_tokens"] for r in rows),
        "input_tokens": inp,
        "cached_share": round(cached / inp, 4) if inp else 0.0,
        "output_tokens": sum(r["output_tokens"] for r in rows),
        "estimated_cost_usd": round(sum(r["estimated_cost_usd"] for r in rows), 2),
        "span": (stamps[0], stamps[-1]) if stamps else None,
        "sessions": sessions,
        "models": dict(Counter(r["model"] for r in rows)),
    }


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2
    rows, sessions = build(argv[1])
    if not rows:
        print("no usage records found")
        return 1
    with open(argv[2], "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    for k, v in summarise(rows, sessions).items():
        print(f"{k:>20}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
