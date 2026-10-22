"""Image prep, eval scorer, and cost-ledger reconstruction."""
import json
import sys
from pathlib import Path

import pytest
from PIL import Image

from billx import images

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval"))
sys.path.insert(0, str(ROOT / "cost_analysis"))

import build_cost_ledger  # noqa: E402
import score  # noqa: E402

BILLS = ROOT / "samples" / "bills"


def test_renditions_shrink_and_dhash_finds_reshoot(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    big = Image.open(BILLS / "bill_01.jpg").resize((3000, 4000))
    big.save(src / "a.jpg", quality=95)
    big.rotate(0.4, fillcolor="white").save(src / "a_reshot.jpg", quality=90)  # same bill, re-shot
    Image.open(BILLS / "bill_04.jpg").resize((3000, 4000)).save(src / "b.jpg", quality=95)

    records = images.make_renditions(src, tmp_path / "out")
    assert len(records) == 3
    for r in records:
        assert r.read_kb < r.orig_kb and r.triage_kb < r.read_kb
    with Image.open(tmp_path / "out" / "read" / "a.jpg") as im:
        assert max(im.size) == images.READ_SIDE

    pairs = {(a, b) for a, b, _ in images.near_duplicates(records)}
    assert ("a.jpg", "a_reshot.jpg") in pairs
    # Same printed template -> a different bill is ALSO a dHash candidate.
    # That is why candidates are confirmed on extracted content, below.
    assert ("a.jpg", "b.jpg") in pairs


def test_duplicates_confirmed_on_content_not_pixels():
    one = json.loads((BILLS / "bill_01.json").read_text(encoding="utf-8"))
    four = json.loads((BILLS / "bill_04.json").read_text(encoding="utf-8"))
    groups = images.duplicate_documents({"a.jpg": one, "a_reshot.jpg": dict(one, invoice_no="101"),
                                         "b.jpg": four})
    assert groups == [["a.jpg", "a_reshot.jpg"]]


def test_score_separates_wrong_from_abstained(tmp_path):
    labels, preds = tmp_path / "labels", tmp_path / "preds"
    labels.mkdir(), preds.mkdir()
    truth = json.loads((BILLS / "bill_01.json").read_text(encoding="utf-8"))
    guess = json.loads(json.dumps(truth))
    guess["grand_total"] = "836820.6"            # same value, different format -> correct
    guess["buyer_gstin"] = ""                     # honest abstention
    guess["line_items"][0]["weight"] = "18.120"   # confident misread
    (labels / "p1.json").write_text(json.dumps(truth))
    (preds / "p1.json").write_text(json.dumps(guess))

    report = score.score(labels, preds)
    f = report["fields"]
    assert f["grand_total"]["correct"] == 1
    assert f["buyer_gstin"]["abstained"] == 1
    assert f["line.weight"]["wrong"] == 1
    assert report["fully_correct"] == 0
    assert "| line.weight | 0 | 1 | 0 | 0.0% |" in score.to_markdown(report)


def test_cost_ledger_reconstruction(tmp_path):
    session = tmp_path / "2026" / "09" / "s1.jsonl"
    session.parent.mkdir(parents=True)
    lines = [
        {"type": "token_usage_record", "timestamp": "2026-09-03T10:00:00Z",
         "payload": {"session_id": "s1", "model": "m",
                     "usage": {"input_tokens": 1_000_000, "cached_input_tokens": 500_000,
                               "output_tokens": 10_000, "total_tokens": 1_010_000}}},
        {"type": "something_else"},
        "not json",
    ]
    session.write_text("\n".join(l if isinstance(l, str) else json.dumps(l) for l in lines))
    rows, sessions = build_cost_ledger.build(str(tmp_path))
    assert len(rows) == 1 and sessions == 1
    # 500k * $2/M + 500k * $0.20/M + 10k * $10/M
    assert rows[0]["estimated_cost_usd"] == pytest.approx(1.0 + 0.1 + 0.1)
    summary = build_cost_ledger.summarise(rows, sessions)
    assert summary["cached_share"] == 0.5 and summary["calls"] == 1 and summary["sessions"] == 1


def test_cost_ledger_export_contains_no_session_identifiers(tmp_path, capsys):
    logs = tmp_path / "logs"
    logs.mkdir()
    for sid in ("SESSION-ID-AAA", "SESSION-ID-BBB"):
        record = {"type": "token_usage_record", "timestamp": "2026-09-03T10:00:00Z",
                  "payload": {"session_id": sid, "thread_id": f"THREAD-{sid}", "turn_id": f"TURN-{sid}",
                              "model": "m", "usage": {"input_tokens": 10, "output_tokens": 2,
                                                      "total_tokens": 12}}}
        (logs / f"{sid}.jsonl").write_text(json.dumps(record))
    out = tmp_path / "ledger.csv"
    assert build_cost_ledger.main(["x", str(logs), str(out)]) == 0
    text = out.read_text()
    assert "session" not in text.splitlines()[0] and "SESSION-ID" not in text
    assert "THREAD-" not in text and "TURN-" not in text
    assert "sessions: 2" in capsys.readouterr().out    # the aggregate count survives
