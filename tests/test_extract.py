"""The extraction loop, with a fake model client (no network, no keys)."""
import json
from pathlib import Path
from types import SimpleNamespace

from billx.extract import extract_page, parse_json

SAMPLES = Path(__file__).resolve().parents[1] / "samples"
IMAGE = SAMPLES / "bills" / "bill_02.jpg"
GOOD = (SAMPLES / "bills" / "bill_02.json").read_text(encoding="utf-8")
MISREAD = (SAMPLES / "misread_bill_02.json").read_text(encoding="utf-8")


class FakeClient:
    """Returns scripted replies and records the messages it was sent."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.sent = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, model, messages, temperature):
        self.sent.append(json.loads(json.dumps(messages, default=str)))
        text = self.replies.pop(0)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


def test_parse_json_strips_code_fence():
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_clean_first_read_is_accepted():
    client = FakeClient(GOOD)
    result = extract_page(IMAGE, 2, client=client, model="m", seller_state="00")
    assert result.status == "accepted" and result.attempts == 1
    # the image goes as a base64 data URL alongside the spec
    content = client.sent[0][1]["content"]
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert "ARITHMETIC IS YOUR BEST OCR CHECK" in client.sent[0][0]["content"]


def test_failed_checks_trigger_a_targeted_reread():
    client = FakeClient(MISREAD, GOOD)
    result = extract_page(IMAGE, 2, client=client, model="m", seller_state="00")
    assert result.status == "accepted" and result.attempts == 2
    reread_prompt = client.sent[1][-1]["content"]
    assert "line_items[1].amount" in reread_prompt
    assert "buyer_gstin" in reread_prompt


def test_persistent_misread_is_rejected_not_booked():
    client = FakeClient(MISREAD, MISREAD)
    result = extract_page(IMAGE, 2, client=client, model="m", max_rereads=1)
    assert result.status == "rejected"
    assert any("checksum" in i for i in result.issues)


def test_unreadable_field_goes_to_review_without_a_reread():
    doc = json.loads(GOOD)
    doc["buyer_gstin"], doc["unreadable_fields"] = "", ["buyer_gstin"]
    client = FakeClient(json.dumps(doc))
    result = extract_page(IMAGE, 2, client=client, model="m")
    assert result.status == "needs_review" and result.attempts == 1
    assert len(client.sent) == 1     # no re-read: asking again would only invite a guess


def test_non_json_reply_fails_cleanly():
    client = FakeClient("I could not read this page.", "still no json")
    result = extract_page(IMAGE, 2, client=client, model="m")
    assert result.status == "failed" and result.doc is None
