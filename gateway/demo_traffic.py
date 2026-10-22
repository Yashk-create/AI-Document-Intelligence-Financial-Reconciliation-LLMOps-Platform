"""Send mock traffic through a real LiteLLM Router so the tracker can be seen working.

No API keys and no network: every deployment uses LiteLLM's mock_response. Two
aliases are routed to two (mock) backend models, and a few calls are made to fail
on purpose so failure tracking shows up too.

    pip install -e ".[gateway]"
    python gateway/demo_traffic.py                 # writes gateway/demo_tracker.db, prints a report
    TRACKER_DB_PATH=gateway/demo_tracker.db python gateway/tracker_server.py   # then open :4100
"""
import argparse
import asyncio
import os
import sys
from pathlib import Path

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")   # don't fetch the price map

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

MODEL_LIST = [
    {"model_name": "vision-default",
     "litellm_params": {"model": "openai/mock-vision-model", "api_key": "not-a-key",
                        "mock_response": '{"invoice_no": "000101", "page_is_blank": false}'}},
    {"model_name": "cheap-text",
     "litellm_params": {"model": "openai/mock-text-model", "api_key": "not-a-key",
                        "mock_response": "mapped"}},
]


async def send(router, n_calls: int, n_failures: int) -> None:
    for i in range(n_calls):
        alias = "vision-default" if i % 3 else "cheap-text"
        await router.acompletion(model=alias, messages=[{"role": "user", "content": f"synthetic request {i}"}])
    for i in range(n_failures):
        try:
            await router.acompletion(model="vision-default", num_retries=0,
                                     messages=[{"role": "user", "content": "synthetic failure"}],
                                     mock_response=Exception("simulated provider outage"))
        except Exception:
            pass
    await asyncio.sleep(1.0)   # LiteLLM runs logging callbacks in the background


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=str(HERE / "demo_tracker.db"))
    ap.add_argument("--calls", type=int, default=12)
    ap.add_argument("--failures", type=int, default=2)
    args = ap.parse_args(argv)

    import litellm
    import tracker_callback
    import tracker_report

    litellm.callbacks = [tracker_callback.CostTracker(db_path=args.db)]
    router = litellm.Router(model_list=MODEL_LIST)
    asyncio.run(send(router, args.calls, args.failures))
    return tracker_report.main(["--db", args.db])


if __name__ == "__main__":
    sys.exit(main())
