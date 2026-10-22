"""FastAPI dashboard over the tracker ledger.

GET /api/stats -> totals, failures, cache share, budget, 60-second spend rate,
                  a per-alias breakdown with p50/p95 latency, and recent calls
GET /          -> a small self-refreshing HTML dashboard over the same data

    python gateway/tracker_server.py          # http://127.0.0.1:4100

The budget is a display threshold, not an enforced limit. The "$ per second" figure
is an observed rate over calls completed in the last 60 seconds. All costs are
estimates; provider invoices remain the source of truth.
"""
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

DB = os.environ.get("TRACKER_DB_PATH", str(Path(__file__).with_name("tracker.db")))
BUDGET = float(os.environ.get("TRACKER_BUDGET_USD", "50"))

app = FastAPI(title="LLM Usage & Cost Tracker")


def percentile(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile; None for an empty list."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, -(-len(ordered) * q // 100))   # ceil(n * q / 100), at least 1
    return ordered[int(rank) - 1]


def stats(db_path=None, budget=None, now=None):
    budget = BUDGET if budget is None else budget
    now = now or datetime.now(timezone.utc)
    c = sqlite3.connect(db_path or DB, timeout=30)
    c.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in c.execute("SELECT * FROM calls ORDER BY id")]
    except sqlite3.OperationalError:   # ledger not created yet
        rows = []
    finally:
        c.close()

    ok = [r for r in rows if r["status"] == "success"]
    spent = sum(r["benchmark_cost_usd"] or 0 for r in ok)
    reported = [r["response_cost_usd"] for r in ok if r["response_cost_usd"] is not None]
    inp = sum(r["input_tokens"] or 0 for r in ok)
    cached = sum(r["cached_tokens"] or 0 for r in ok)

    cutoff = (now - timedelta(seconds=60)).isoformat()
    recent = [r for r in ok if r["ts_utc"] >= cutoff]   # same ISO format on both sides
    rate = sum(r["benchmark_cost_usd"] or 0 for r in recent) / 60

    by_alias = {}
    for r in rows:
        key = (r["alias"], r["provider"], r["provider_model"])
        a = by_alias.setdefault(key, {"alias": key[0], "provider": key[1], "provider_model": key[2],
                                      "calls": 0, "failures": 0, "input_tokens": 0, "cached_tokens": 0,
                                      "output_tokens": 0, "benchmark_cost": 0.0, "_lat": []})
        a["calls"] += 1
        if r["status"] != "success":
            a["failures"] += 1
            continue
        a["input_tokens"] += r["input_tokens"] or 0
        a["cached_tokens"] += r["cached_tokens"] or 0
        a["output_tokens"] += r["output_tokens"] or 0
        a["benchmark_cost"] += r["benchmark_cost_usd"] or 0
        if r["latency_ms"] is not None:
            a["_lat"].append(r["latency_ms"])
    breakdown = []
    for a in by_alias.values():
        lat = a.pop("_lat")
        a["p50_latency_ms"], a["p95_latency_ms"] = percentile(lat, 50), percentile(lat, 95)
        breakdown.append(a)
    breakdown.sort(key=lambda a: -a["benchmark_cost"])

    return {
        "calls": len(rows),
        "successes": len(ok),
        "failures": len(rows) - len(ok),
        "error_rate": round((len(rows) - len(ok)) / len(rows), 4) if rows else 0.0,
        "input_tokens": inp,
        "cached_tokens": cached,
        "cached_share": round(cached / inp, 4) if inp else 0.0,
        "output_tokens": sum(r["output_tokens"] or 0 for r in ok),
        "total_tokens": sum(r["total_tokens"] or 0 for r in ok),
        "benchmark_cost": spent,
        "response_cost": sum(reported) if reported else None,
        "response_cost_coverage": round(len(reported) / len(ok), 4) if ok else 0.0,
        "budget": budget,
        "remaining": max(0.0, budget - spent),
        "over_budget": spent > budget,
        "usd_per_second_60s": rate,
        "usd_per_hour_60s": rate * 3600,
        "by_alias": breakdown,
        "recent": rows[-25:][::-1],
    }


@app.get("/api/stats")
def api_stats():
    return stats()


PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>LLM Usage & Cost Tracker</title>
<style>body{font-family:system-ui,Arial;background:#0b1020;color:#eef2ff;margin:0;padding:24px}
.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.card{background:#141b2f;border:1px solid #273250;
border-radius:12px;padding:14px}.muted{color:#9aa5bf}.v{font-size:22px;font-weight:700;margin-top:6px}
.bad{color:#ff8a8a}table{width:100%;border-collapse:collapse;margin:8px 0 20px}th,td{padding:6px;
border-bottom:1px solid #273250;text-align:left;font-size:12px}</style></head><body>
<h1>LLM Usage &amp; Cost Tracker</h1>
<p class="muted">Refreshes every 2 s. Costs are estimates from a local price table; the budget is a display threshold.</p>
<div class="grid" id="cards"></div>
<h3>By alias</h3><table><thead><tr><th>Alias</th><th>Provider model</th><th>Calls</th><th>Failures</th>
<th>Input</th><th>Cached</th><th>Output</th><th>Cost</th><th>p50</th><th>p95</th></tr></thead><tbody id="alias"></tbody></table>
<h3>Recent calls</h3><table><thead><tr><th>UTC</th><th>Status</th><th>Alias</th><th>In</th><th>Cached</th><th>Out</th>
<th>Cost</th><th>Latency</th><th>Error type</th></tr></thead><tbody id="rows"></tbody></table>
<script>
const f=n=>Number(n||0).toLocaleString(), m=(n,d=4)=>n==null?'-':'$'+Number(n).toFixed(d);
const ms=x=>x==null?'':Number(x).toFixed(0)+' ms';
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function r(){const d=await fetch('/api/stats').then(x=>x.json());
document.getElementById('cards').innerHTML=[['Spend (est.)',m(d.benchmark_cost)],
['Budget left',m(d.remaining,2),d.over_budget],['$ / hour (last 60 s)',m(d.usd_per_hour_60s,2)],
['Calls',f(d.calls)],['Failures',f(d.failures)+' ('+(d.error_rate*100).toFixed(1)+'%)',d.failures>0],
['Input tokens',f(d.input_tokens)],['Cached share',(d.cached_share*100).toFixed(1)+'%'],
['Output tokens',f(d.output_tokens)]].map(([k,v,bad])=>'<div class="card"><span class="muted">'+k+
'</span><div class="v'+(bad?' bad':'')+'">'+v+'</div></div>').join('');
document.getElementById('alias').innerHTML=d.by_alias.map(a=>'<tr><td>'+esc(a.alias)+'</td><td>'+
esc(a.provider_model)+'</td><td>'+f(a.calls)+'</td><td>'+f(a.failures)+'</td><td>'+f(a.input_tokens)+'</td><td>'+
f(a.cached_tokens)+'</td><td>'+f(a.output_tokens)+'</td><td>'+m(a.benchmark_cost,6)+'</td><td>'+
ms(a.p50_latency_ms)+'</td><td>'+ms(a.p95_latency_ms)+'</td></tr>').join('');
document.getElementById('rows').innerHTML=d.recent.map(x=>'<tr><td>'+esc(x.ts_utc)+'</td><td>'+esc(x.status)+
'</td><td>'+esc(x.alias)+'</td><td>'+f(x.input_tokens)+'</td><td>'+f(x.cached_tokens)+'</td><td>'+
f(x.output_tokens)+'</td><td>'+m(x.benchmark_cost_usd,6)+'</td><td>'+ms(x.latency_ms)+'</td><td>'+
esc(x.error_type)+'</td></tr>').join('')}
r();setInterval(r,2000)</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
def home():
    return PAGE


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("TRACKER_PORT", "4100")))
