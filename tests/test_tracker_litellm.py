"""Integration: the tracker driven by a real LiteLLM Router (mock responses, no network, no keys)."""
import asyncio
import sqlite3

import pytest

litellm = pytest.importorskip("litellm")

import demo_traffic  # noqa: E402
import tracker_callback as tc  # noqa: E402

MODEL_LIST = [
    {"model_name": "vision-default",
     "litellm_params": {"model": "openai/mock-vision-model", "api_key": "not-a-key", "mock_response": "ok"}},
]


@pytest.fixture
def router(tmp_path, monkeypatch):
    db = str(tmp_path / "ledger.db")
    monkeypatch.setattr(litellm, "callbacks", [tc.CostTracker(db_path=db)])
    return litellm.Router(model_list=MODEL_LIST), db


def rows(db):
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    out = [dict(r) for r in c.execute("SELECT * FROM calls ORDER BY id")]
    c.close()
    return out


def test_async_success_and_failure_through_a_router(router):
    r, db = router

    async def go():
        await r.acompletion(model="vision-default", messages=[{"role": "user", "content": "PRIVATE-PROMPT"}])
        with pytest.raises(Exception):
            await r.acompletion(model="vision-default", num_retries=0,
                                messages=[{"role": "user", "content": "PRIVATE-PROMPT"}],
                                mock_response=Exception("PRIVATE-ERROR"))
        await asyncio.sleep(1.0)

    asyncio.run(go())
    ok, failed = rows(db)
    assert (ok["status"], ok["alias"], ok["provider"], ok["provider_model"]) == \
        ("success", "vision-default", "openai", "openai/mock-vision-model")
    assert ok["input_tokens"] > 0 and ok["output_tokens"] > 0 and ok["benchmark_cost_usd"] > 0
    assert (failed["status"], failed["alias"]) == ("failure", "vision-default")
    assert failed["error_type"]
    raw = open(db, "rb").read()
    assert b"PRIVATE-PROMPT" not in raw and b"PRIVATE-ERROR" not in raw


def test_sync_call_is_recorded_exactly_once(router):
    r, db = router
    r.completion(model="vision-default", messages=[{"role": "user", "content": "hi"}])
    import time
    time.sleep(1.0)
    assert len(rows(db)) == 1


def test_demo_traffic_script(tmp_path, capsys):
    db = tmp_path / "demo.db"
    assert demo_traffic.main(["--db", str(db), "--calls", "6", "--failures", "1"]) == 0
    assert len(rows(str(db))) == 7
    out = capsys.readouterr().out
    assert "vision-default" in out and "cheap-text" in out
