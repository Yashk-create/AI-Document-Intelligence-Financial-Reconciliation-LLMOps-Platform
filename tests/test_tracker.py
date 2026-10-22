"""LLM usage and cost tracker: callback, ledger, privacy, migration, dashboard stats."""
import asyncio
import inspect
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

import tracker_callback as tc
import tracker_report
import tracker_server

START = datetime(2026, 9, 17, 10, 0, 0, tzinfo=timezone.utc)
SECRETS = ["SECRET-PROMPT-TEXT", "SECRET-REPLY-TEXT", "alice@example.com", "sess-123",
           "FAKE-CREDENTIAL-FOR-TEST", "http://internal-host:4000", "x-secret-header", "SECRET-ERROR-TEXT"]


def litellm_kwargs(alias="vision-default", provider="openai", model="mock-vision", cost=0.0021, **extra):
    """Shaped like what LiteLLM passes to callbacks (checked against litellm 1.101)."""
    kwargs = {
        "model": model,
        "messages": [{"role": "user", "content": "SECRET-PROMPT-TEXT"}],
        "user": "alice@example.com",
        "litellm_session_id": "sess-123",
        "api_key": "FAKE-CREDENTIAL-FOR-TEST",
        "api_base": "http://internal-host:4000",
        "custom_llm_provider": provider,
        "response_cost": cost,
        "litellm_params": {"metadata": {"model_group": alias, "headers": {"x": "x-secret-header"}}},
        "standard_logging_object": {
            "model": f"{provider}/{model}", "model_group": alias, "custom_llm_provider": provider,
            "call_type": "acompletion", "response_cost": cost, "messages": "SECRET-PROMPT-TEXT",
            "response": "SECRET-REPLY-TEXT", "error_str": None, "metadata": {"user_api_key_alias": "alice@example.com"},
        },
    }
    kwargs.update(extra)
    return kwargs


def response(prompt=1_000_000, completion=100_000, cached=400_000):
    return {"model": "mock-vision", "choices": [{"message": {"content": "SECRET-REPLY-TEXT"}}],
            "usage": {"prompt_tokens": prompt, "completion_tokens": completion,
                      "total_tokens": prompt + completion,
                      "prompt_tokens_details": {"cached_tokens": cached}}}


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "ledger.db")


def rows(path):
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    out = [dict(r) for r in c.execute("SELECT * FROM calls ORDER BY id")]
    c.close()
    return out


# --- the callback contract ----------------------------------------------------

def test_hook_signatures_match_litellm_keyword_call():
    # LiteLLM calls hooks with keyword arguments, so the names are part of the contract.
    for name in ("log_success_event", "async_log_success_event",
                 "log_failure_event", "async_log_failure_event"):
        params = list(inspect.signature(getattr(tc.CostTracker, name)).parameters)
        assert params == ["self", "kwargs", "response_obj", "start_time", "end_time"], name


def test_async_hooks_are_methods_of_the_class():
    # Regression: the original async hook was outdented to module level, so the
    # proxy (which only calls async hooks) never recorded production traffic.
    assert "async_log_success_event" in vars(tc.CostTracker)
    assert "async_log_failure_event" in vars(tc.CostTracker)


def test_constructing_the_tracker_touches_no_disk(tmp_path):
    tc.CostTracker(db_path=str(tmp_path / "x.db"))
    assert not (tmp_path / "x.db").exists()


# --- what gets recorded --------------------------------------------------------

def test_success_row_records_alias_provider_tokens_cost_latency(db):
    tracker = tc.CostTracker(db_path=db)
    asyncio.run(tracker.async_log_success_event(
        kwargs=litellm_kwargs(), response_obj=response(),
        start_time=START, end_time=START + timedelta(milliseconds=850)))
    [r] = rows(db)
    assert (r["status"], r["alias"], r["provider"], r["provider_model"], r["call_type"]) == \
        ("success", "vision-default", "openai", "openai/mock-vision", "acompletion")
    assert (r["input_tokens"], r["cached_tokens"], r["output_tokens"], r["total_tokens"]) == \
        (1_000_000, 400_000, 100_000, 1_100_000)
    # 600k uncached * $2/M + 400k cached * $0.20/M + 100k out * $10/M
    assert r["benchmark_cost_usd"] == pytest.approx(1.2 + 0.08 + 1.0)
    assert r["response_cost_usd"] == pytest.approx(0.0021)
    assert r["latency_ms"] == pytest.approx(850)
    assert r["error_type"] is None


def test_alias_is_the_model_group_not_the_provider_model(db):
    # Regression: the original stored kwargs["model"], which LiteLLM sets to the
    # provider model, so the requested alias was never recorded.
    tc.CostTracker(db_path=db).log_success_event(
        litellm_kwargs(alias="cheap-text", model="some-provider-model"), response(), START, START)
    [r] = rows(db)
    assert r["alias"] == "cheap-text" and r["provider_model"] == "openai/some-provider-model"


def test_alias_falls_back_when_called_outside_a_router(db):
    kwargs = {"model": "direct-model", "messages": []}
    tc.CostTracker(db_path=db).log_success_event(kwargs, response(10, 5, 0), START, START)
    assert rows(db)[0]["alias"] == "direct-model"


def test_failure_row_keeps_only_the_exception_type(db):
    class ProviderDown(Exception):
        pass
    kwargs = litellm_kwargs(exception=ProviderDown("SECRET-ERROR-TEXT in the prompt"))
    kwargs["standard_logging_object"]["error_str"] = "SECRET-ERROR-TEXT"
    asyncio.run(tc.CostTracker(db_path=db).async_log_failure_event(
        kwargs=kwargs, response_obj=None, start_time=START, end_time=START + timedelta(seconds=2)))
    [r] = rows(db)
    assert (r["status"], r["alias"], r["error_type"]) == ("failure", "vision-default", "ProviderDown")
    assert r["input_tokens"] == 0 and r["benchmark_cost_usd"] is None
    assert r["latency_ms"] == pytest.approx(2000)


def test_both_usage_shapes():
    assert tc.usage({"usage": {"input_tokens": 10, "output_tokens": 5,
                               "cache_read_input_tokens": 3}}) == (10, 3, 5, 15)
    assert tc.usage({"usage": {"prompt_tokens": 7, "completion_tokens": 2}}) == (7, 0, 2, 9)
    slo_only = {"standard_logging_object": {"prompt_tokens": 4, "completion_tokens": 1, "total_tokens": 5}}
    assert tc.usage({}, slo_only) == (4, 0, 1, 5)


def test_latency_falls_back_to_reported_response_time():
    kwargs = {"standard_logging_object": {"response_time": 0.25}}
    assert tc._latency_ms(kwargs, None, None) == pytest.approx(250)


# --- privacy -------------------------------------------------------------------

def test_no_prompt_credentials_session_or_metadata_reach_the_ledger(db):
    tracker = tc.CostTracker(db_path=db)
    tracker.log_success_event(litellm_kwargs(), response(), START, START)
    failing = litellm_kwargs(exception=RuntimeError("SECRET-ERROR-TEXT"))
    tracker.log_failure_event(failing, None, START, START)
    assert set(rows(db)[0]) == set(tc.COLUMNS)
    raw = open(db, "rb").read()
    for secret in SECRETS:
        assert secret.encode() not in raw, secret


def test_legacy_ledger_is_migrated_and_prompt_text_purged_from_the_file(db):
    c = sqlite3.connect(db)
    c.execute("""CREATE TABLE calls (id INTEGER PRIMARY KEY AUTOINCREMENT, ts_utc TEXT NOT NULL,
        model TEXT, provider_model TEXT, call_id TEXT, input_tokens INTEGER, cached_tokens INTEGER,
        output_tokens INTEGER, total_tokens INTEGER, response_cost_usd REAL, benchmark_cost_usd REAL,
        latency_ms REAL, prompt_preview TEXT, prompt_sha256 TEXT, raw_meta_json TEXT)""")
    c.execute("""INSERT INTO calls (ts_utc, model, provider_model, call_id, input_tokens, cached_tokens,
        output_tokens, total_tokens, response_cost_usd, benchmark_cost_usd, latency_ms, prompt_preview,
        prompt_sha256, raw_meta_json) VALUES ('2026-09-17T10:00:00+00:00', 'm', 'prov/m', 'resp-1',
        100, 10, 20, 120, 0.5, 0.4, 900, ?, 'abc123', ?)""",
              ("SECRET-PROMPT-TEXT " * 50, json.dumps({"user": "alice@example.com"})))
    c.commit()
    c.close()

    tc.init_db(db)
    [r] = rows(db)
    assert set(r) == set(tc.COLUMNS)
    assert (r["alias"], r["provider_model"], r["status"]) == ("legacy", "prov/m", "success")
    assert (r["input_tokens"], r["cached_tokens"], r["output_tokens"]) == (100, 10, 20)
    assert r["benchmark_cost_usd"] == pytest.approx(0.4)
    raw = open(db, "rb").read()
    for secret in (b"SECRET-PROMPT-TEXT", b"alice@example.com", b"abc123", b"resp-1"):
        assert secret not in raw


def test_metering_failure_never_breaks_the_call_or_echoes_details(capsys):
    tracker = tc.CostTracker(db_path="/nonexistent/dir/ledger.db")
    tracker.log_success_event(litellm_kwargs(), response(), START, START)
    out = capsys.readouterr().out
    assert out.startswith("[tracker_callback]") and "SECRET" not in out


# --- prices --------------------------------------------------------------------

def test_price_table_overrides_per_model_and_alias(tmp_path, db):
    table = {"openai/mock-vision": {"input": 1.0, "cached_input": 0.1, "output": 4.0},
             "cheap-text": {"input": 0.1, "cached_input": 0.01, "output": 0.4}}
    path = tmp_path / "prices.json"
    path.write_text(json.dumps(table))
    tracker = tc.CostTracker(db_path=db, price_table=tc.load_price_table(str(path)))
    tracker.log_success_event(litellm_kwargs(), response(1_000_000, 0, 0), START, START)
    tracker.log_success_event(litellm_kwargs(alias="cheap-text", model="other"),
                              response(1_000_000, 0, 0), START, START)
    tracker.log_success_event(litellm_kwargs(alias="unpriced", model="x"), response(1_000_000, 0, 0), START, START)
    assert [r["benchmark_cost_usd"] for r in rows(db)] == pytest.approx([1.0, 0.1, 2.0])


def test_shipped_price_example_loads():
    from pathlib import Path
    example = Path(tc.__file__).with_name("prices.example.json")
    table = tc.load_price_table(str(example))
    assert "_comment" not in table and table["cheap-text"]["output"] == 0.80


def test_price_table_rejects_incomplete_rows(tmp_path):
    path = tmp_path / "prices.json"
    path.write_text(json.dumps({"m": {"input": 1.0}}))
    with pytest.raises(ValueError, match="missing"):
        tc.load_price_table(str(path))


# --- concurrency ------------------------------------------------------------------

def test_concurrent_writes_are_all_recorded(db):
    tracker = tc.CostTracker(db_path=db)
    threads = [threading.Thread(target=tracker.log_success_event,
                                args=(litellm_kwargs(), response(10, 5, 0), START, START)) for _ in range(25)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(rows(db)) == 25


# --- dashboard stats and report ---------------------------------------------------

def _seed(db):
    tracker = tc.CostTracker(db_path=db)
    for ms in (100, 200, 300, 400, 1000):
        tracker.log_success_event(litellm_kwargs(), response(100, 20, 25), START,
                                  START + timedelta(milliseconds=ms))
    tracker.log_success_event(litellm_kwargs(alias="cheap-text", model="t", cost=None),
                              response(100, 10, 0), START, START + timedelta(milliseconds=50))
    tracker.log_failure_event(litellm_kwargs(exception=TimeoutError()), None, START, START)
    return tracker


def test_stats_totals_breakdown_and_percentiles(db):
    _seed(db)
    s = tracker_server.stats(db_path=db, budget=1.0)
    assert (s["calls"], s["successes"], s["failures"]) == (7, 6, 1)
    assert s["error_rate"] == pytest.approx(1 / 7, abs=1e-4)
    assert s["cached_share"] == pytest.approx(125 / 600, abs=1e-4)
    vision = next(a for a in s["by_alias"] if a["alias"] == "vision-default")
    assert (vision["calls"], vision["failures"]) == (6, 1)
    assert (vision["p50_latency_ms"], vision["p95_latency_ms"]) == (pytest.approx(300), pytest.approx(1000))
    assert s["response_cost_coverage"] == pytest.approx(5 / 6, abs=1e-4)
    assert s["remaining"] == pytest.approx(1.0 - s["benchmark_cost"]) and not s["over_budget"]
    assert tracker_server.stats(db_path=db, budget=0.0)["over_budget"]


def test_rate_window_excludes_older_rows(db):
    # Regression: comparing ISO timestamps with SQLite datetime() text counted every
    # row from the same day as "the last 60 seconds".
    _seed(db)
    now = datetime.now(timezone.utc)
    assert tracker_server.stats(db_path=db, now=now)["usd_per_second_60s"] > 0
    later = now + timedelta(minutes=5)
    assert tracker_server.stats(db_path=db, now=later)["usd_per_second_60s"] == 0


def test_stats_on_a_missing_ledger_is_empty_not_an_error(tmp_path):
    s = tracker_server.stats(db_path=str(tmp_path / "none.db"))
    assert s["calls"] == 0 and s["by_alias"] == []


@pytest.mark.parametrize("values, q, expected", [([], 50, None), ([5], 95, 5),
                                                 ([1, 2, 3, 4], 50, 2), ([1, 2, 3, 4], 95, 4)])
def test_percentile(values, q, expected):
    assert tracker_server.percentile(values, q) == expected


def test_report_cli(db, tmp_path, capsys):
    _seed(db)
    out_csv = tmp_path / "by_alias.csv"
    assert tracker_report.main(["--db", db, "--csv", str(out_csv)]) == 0
    text = capsys.readouterr().out
    assert "vision-default" in text and "failures 1" in text
    assert out_csv.read_text().splitlines()[0].startswith("alias,provider,provider_model")
    assert tracker_report.main(["--db", str(tmp_path / "missing.db")]) == 2


def test_dashboard_routes(db, monkeypatch):
    from fastapi.testclient import TestClient
    _seed(db)
    monkeypatch.setattr(tracker_server, "DB", db)
    client = TestClient(tracker_server.app)
    assert client.get("/api/stats").json()["calls"] == 7
    page = client.get("/").text
    assert "LLM Usage &amp; Cost Tracker" in page and "esc(" in page   # values are HTML-escaped
