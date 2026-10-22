"""Print a usage and cost report from the tracker ledger, or export it as CSV.

    python gateway/tracker_report.py                       # $TRACKER_DB_PATH or gateway/tracker.db
    python gateway/tracker_report.py --db demo.db --csv by_alias.csv
"""
import argparse
import csv
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tracker_server  # noqa: E402


def render(s: dict) -> str:
    def ms(x):
        return "-" if x is None else f"{x:.0f} ms"

    lines = [
        f"calls {s['calls']}  (failures {s['failures']}, error rate {s['error_rate']:.1%})",
        f"tokens  input {s['input_tokens']:,}  cached {s['cached_tokens']:,} ({s['cached_share']:.1%})  "
        f"output {s['output_tokens']:,}",
        f"estimated cost ${s['benchmark_cost']:.6f}  (budget ${s['budget']:.2f}, "
        f"remaining ${s['remaining']:.2f}{', OVER BUDGET' if s['over_budget'] else ''})",
        "",
        f"{'alias':<16}{'provider model':<28}{'calls':>6}{'fail':>6}{'in':>10}{'out':>10}"
        f"{'cost $':>12}{'p50':>9}{'p95':>9}",
    ]
    for a in s["by_alias"]:
        lines.append(
            f"{str(a['alias'])[:15]:<16}{str(a['provider_model'])[:27]:<28}{a['calls']:>6}{a['failures']:>6}"
            f"{a['input_tokens']:>10,}{a['output_tokens']:>10,}{a['benchmark_cost']:>12.6f}"
            f"{ms(a['p50_latency_ms']):>9}{ms(a['p95_latency_ms']):>9}"
        )
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=os.environ.get("TRACKER_DB_PATH", tracker_server.DB))
    ap.add_argument("--budget", type=float, default=None)
    ap.add_argument("--csv", type=Path, help="write the per-alias breakdown to this CSV file")
    args = ap.parse_args(argv)
    if not Path(args.db).exists():
        print(f"{args.db}: no ledger yet (run gateway/demo_traffic.py, or send traffic through the gateway)")
        return 2
    s = tracker_server.stats(db_path=args.db, budget=args.budget)
    print(render(s))
    if args.csv and s["by_alias"]:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(s["by_alias"][0]))
            writer.writeheader()
            writer.writerows(s["by_alias"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
