"""LLM usage and cost tracker: a LiteLLM callback that writes a SQLite ledger.

One row per model call, success or failure:

    ts_utc, status, alias, provider, provider_model, call_type,
    input_tokens, cached_tokens, output_tokens, total_tokens,
    response_cost_usd, benchmark_cost_usd, latency_ms, error_type

* alias          - what the client asked for ("vision-default"), i.e. the
                   gateway model group; provider_model is what actually served it
* response cost  - LiteLLM's own estimate from its model price map (None when the
                   model is unknown to it)
* benchmark cost - computed here from a local price table, so spend stays
                   comparable across providers, aliases and price-map changes
* error_type     - exception class name only

Privacy: prompt and response text, prompt hashes, users, session ids, API keys,
API base URLs, error messages and LiteLLM's raw logging payload are never
written. LiteLLM's payload contains the full messages and its error strings can
echo prompt content, so only the fields listed above are extracted from it.
Opening an older ledger that still has prompt columns migrates it and vacuums
the file so the old text is physically removed.

Cost figures are estimates. Provider invoices are the source of truth.
"""
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone

try:
    from litellm.integrations.custom_logger import CustomLogger
except ImportError:  # lets the module be imported and tested without litellm
    CustomLogger = object

DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tracker.db")
DB_PATH = os.environ.get("TRACKER_DB_PATH", DEFAULT_DB)

COLUMNS = {
    "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
    "ts_utc": "TEXT NOT NULL",
    "status": "TEXT NOT NULL",
    "alias": "TEXT",
    "provider": "TEXT",
    "provider_model": "TEXT",
    "call_type": "TEXT",
    "input_tokens": "INTEGER DEFAULT 0",
    "cached_tokens": "INTEGER DEFAULT 0",
    "output_tokens": "INTEGER DEFAULT 0",
    "total_tokens": "INTEGER DEFAULT 0",
    "response_cost_usd": "REAL",
    "benchmark_cost_usd": "REAL",
    "latency_ms": "REAL",
    "error_type": "TEXT",
}
# Columns the original tracker had that held prompt text or request metadata.
LEGACY_PRIVATE_COLUMNS = {"prompt_preview", "prompt_sha256", "raw_meta_json", "call_id"}

_lock = threading.Lock()   # the proxy serves requests concurrently
_ready: set[str] = set()   # ledgers whose schema has been checked this process


# --- prices -----------------------------------------------------------------

def default_prices() -> dict:
    """USD per million tokens, from the environment (override per deployment)."""
    return {
        "input": float(os.environ.get("TRACKER_INPUT_USD_PER_M", "2.00")),
        "cached_input": float(os.environ.get("TRACKER_CACHED_INPUT_USD_PER_M", "0.20")),
        "output": float(os.environ.get("TRACKER_OUTPUT_USD_PER_M", "10.00")),
    }


def load_price_table(path: str | None = None) -> dict:
    """Optional JSON file: {"<provider_model or alias>": {"input": .., "cached_input": .., "output": ..}}."""
    path = path or os.environ.get("TRACKER_PRICES_FILE")
    if not path:
        return {}
    with open(path, encoding="utf-8") as f:
        table = {k: v for k, v in json.load(f).items() if not k.startswith("_")}   # "_comment" etc.
    for key, row in table.items():
        if not isinstance(row, dict):
            raise ValueError(f"price table entry {key!r} must be an object")
        missing = {"input", "cached_input", "output"} - row.keys()
        if missing:
            raise ValueError(f"price table entry {key!r} is missing {sorted(missing)}")
    return table


def benchmark_cost(input_tokens, cached_tokens, output_tokens, prices: dict) -> float:
    uncached = max(0, input_tokens - cached_tokens)
    return (uncached * prices["input"]
            + cached_tokens * prices["cached_input"]
            + output_tokens * prices["output"]) / 1_000_000


# --- extracting fields from LiteLLM's callback arguments --------------------

def _get(obj, key, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def call_identity(kwargs, response_obj):
    """(alias, provider, provider_model, call_type) without touching message content."""
    slo = _get(kwargs, "standard_logging_object") or {}
    meta = _get(_get(kwargs, "litellm_params") or {}, "metadata") or {}
    alias = _get(slo, "model_group") or _get(meta, "model_group") or _get(kwargs, "model") or "unknown"
    provider = _get(slo, "custom_llm_provider") or _get(kwargs, "custom_llm_provider")
    model = _get(slo, "model") or _get(kwargs, "model") or _get(response_obj, "model")
    if provider and model and not str(model).startswith(f"{provider}/"):
        model = f"{provider}/{model}"
    call_type = _get(slo, "call_type") or _get(kwargs, "call_type")
    return str(alias), provider, model, call_type


def usage(response_obj, kwargs=None):
    """(input, cached, output, total) tokens; tolerant of both common usage shapes."""
    u = _get(response_obj, "usage") or {}
    slo = _get(kwargs, "standard_logging_object") or {}
    input_tokens = _get(u, "prompt_tokens") or _get(u, "input_tokens") or _get(slo, "prompt_tokens") or 0
    output_tokens = (_get(u, "completion_tokens") or _get(u, "output_tokens")
                     or _get(slo, "completion_tokens") or 0)
    total_tokens = _get(u, "total_tokens") or _get(slo, "total_tokens") or (input_tokens + output_tokens)
    details = _get(u, "prompt_tokens_details") or _get(u, "input_tokens_details")
    cached_tokens = (_get(u, "cache_read_input_tokens") or _get(u, "cached_tokens")
                     or (_get(details, "cached_tokens", 0) if details else 0) or 0)
    return int(input_tokens), int(cached_tokens), int(output_tokens), int(total_tokens)


def _latency_ms(kwargs, start_time, end_time):
    try:
        return (end_time - start_time).total_seconds() * 1000
    except (TypeError, AttributeError):
        seconds = _get(_get(kwargs, "standard_logging_object") or {}, "response_time")
        return float(seconds) * 1000 if isinstance(seconds, (int, float)) else None


# --- ledger -----------------------------------------------------------------

def _connect(path):
    c = sqlite3.connect(path, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def init_db(path=None) -> None:
    """Create the ledger, or migrate an older one and purge its prompt columns."""
    path = path or os.environ.get("TRACKER_DB_PATH", DEFAULT_DB)
    with _lock:
        c = _connect(path)
        try:
            existing = [r["name"] for r in c.execute("PRAGMA table_info(calls)")]
            if existing and set(existing) != set(COLUMNS):
                _migrate(c, set(existing))
            cols = ", ".join(f"{name} {kind}" for name, kind in COLUMNS.items())
            c.execute(f"CREATE TABLE IF NOT EXISTS calls ({cols})")
            c.execute("CREATE INDEX IF NOT EXISTS calls_ts ON calls (ts_utc)")
            c.execute("CREATE INDEX IF NOT EXISTS calls_alias ON calls (alias)")
            c.commit()
        finally:
            c.close()
        _ready.add(path)


def _migrate(c, old: set[str]) -> None:
    # Keep only numeric/accounting fields; the original 'model' column held the
    # provider model, and the alias was never recorded, so alias becomes 'legacy'.
    model_src = "provider_model" if "provider_model" in old else ("model" if "model" in old else "NULL")
    keep = [col for col in ("input_tokens", "cached_tokens", "output_tokens", "total_tokens",
                            "response_cost_usd", "benchmark_cost_usd", "latency_ms") if col in old]
    cols = ", ".join(f"{name} {kind}" for name, kind in COLUMNS.items())
    c.execute("PRAGMA secure_delete = ON")   # overwrite freed pages with zeros
    c.execute(f"CREATE TABLE calls_migrated ({cols})")
    c.execute(
        f"INSERT INTO calls_migrated (ts_utc, status, alias, provider_model, {', '.join(keep)}) "
        f"SELECT ts_utc, 'success', 'legacy', {model_src}, {', '.join(keep)} FROM calls"
    )
    c.execute("DROP TABLE calls")
    c.execute("ALTER TABLE calls_migrated RENAME TO calls")
    c.commit()
    c.execute("VACUUM")                       # rebuild the file without the old pages


def _insert(path, row: dict) -> None:
    if path not in _ready:
        init_db(path)
    names = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    with _lock:
        c = _connect(path)
        try:
            c.execute(f"INSERT INTO calls ({names}) VALUES ({marks})", tuple(row.values()))
            c.commit()
        finally:
            c.close()


class CostTracker(CustomLogger):
    """Register in LiteLLM (proxy config or litellm.callbacks). Never raises into a model call.

    The hook parameter names must stay exactly kwargs / response_obj / start_time /
    end_time: LiteLLM passes them as keyword arguments.
    """

    def __init__(self, db_path=None, prices: dict | None = None, price_table: dict | None = None):
        self.db_path = db_path or os.environ.get("TRACKER_DB_PATH", DEFAULT_DB)
        self.prices = prices or default_prices()
        self.price_table = load_price_table() if price_table is None else price_table

    # LiteLLM hooks ---------------------------------------------------------
    def log_success_event(self, kwargs, response_obj, start_time, end_time):
        self._safe(self._record_success, kwargs, response_obj, start_time, end_time)

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        self.log_success_event(kwargs, response_obj, start_time, end_time)

    def log_failure_event(self, kwargs, response_obj, start_time, end_time):
        self._safe(self._record_failure, kwargs, response_obj, start_time, end_time)

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        self.log_failure_event(kwargs, response_obj, start_time, end_time)

    # internals -------------------------------------------------------------
    def _safe(self, fn, *args):
        try:
            fn(*args)
        except Exception as exc:  # metering must never break a model call
            print("[tracker_callback]", type(exc).__name__)

    def _prices_for(self, alias, provider_model):
        return self.price_table.get(provider_model) or self.price_table.get(alias) or self.prices

    def _base_row(self, kwargs, response_obj, start_time, end_time, status):
        alias, provider, provider_model, call_type = call_identity(kwargs, response_obj)
        return {
            "ts_utc": datetime.now(timezone.utc).isoformat(),
            "status": status,
            "alias": alias,
            "provider": provider,
            "provider_model": provider_model,
            "call_type": call_type,
            "latency_ms": _latency_ms(kwargs, start_time, end_time),
        }

    def _record_success(self, kwargs, response_obj, start_time, end_time):
        row = self._base_row(kwargs, response_obj, start_time, end_time, "success")
        inp, cached, out, total = usage(response_obj, kwargs)
        slo = _get(kwargs, "standard_logging_object") or {}
        cost = _get(kwargs, "response_cost")
        if cost is None:
            cost = _get(slo, "response_cost")
        if cost is None:
            hidden = _get(response_obj, "_hidden_params")
            cost = _get(hidden, "response_cost") if hidden else None
        row.update(
            input_tokens=inp, cached_tokens=cached, output_tokens=out, total_tokens=total,
            response_cost_usd=float(cost) if cost is not None else None,
            benchmark_cost_usd=benchmark_cost(inp, cached, out,
                                              self._prices_for(row["alias"], row["provider_model"])),
        )
        _insert(self.db_path, row)

    def _record_failure(self, kwargs, response_obj, start_time, end_time):
        row = self._base_row(kwargs, response_obj, start_time, end_time, "failure")
        exc = _get(kwargs, "exception")
        row["error_type"] = type(exc).__name__ if exc is not None else "unknown"
        _insert(self.db_path, row)


# Referenced from the LiteLLM config as tracker_callback.tracker_handler_instance.
# Creating it does not touch the disk; the ledger is created on the first call.
tracker_handler_instance = CostTracker()
