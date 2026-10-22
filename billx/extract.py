"""Extract one bill page with a vision LLM, validate it, and re-read on failure.

Flow per page:
    image -> model (spec as system prompt) -> JSON -> invariants
      ok                      -> status "accepted"
      errors, retries left    -> send the failed checks back, ask for a re-read
      errors, no retries left -> status "needs_review" (never silently "fixed")

The model is reached through any OpenAI-compatible endpoint, normally the
self-hosted LiteLLM gateway in gateway/, so the backend can be swapped by
changing a model alias rather than code.

In the original project this contract was executed by coding-agent sub-agents,
one per page. This module is a standalone re-implementation of the same
contract so it can be run, tested and evaluated outside that environment.
"""
from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import invariants

SPEC_PATH = Path(__file__).with_name("EXTRACT_SPEC.md")
FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


@dataclass
class PageResult:
    page: int
    status: str                      # accepted | needs_review | rejected | failed
    doc: dict | None
    issues: list[str] = field(default_factory=list)
    attempts: int = 0


def parse_json(text: str) -> dict:
    """Accept a bare JSON object, optionally wrapped in a code fence."""
    cleaned = FENCE.sub("", text.strip())
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end < 0:
        raise ValueError("no JSON object in model reply")
    return json.loads(cleaned[start:end + 1])


def _image_part(path: Path) -> dict:
    mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}}


def make_client():
    """OpenAI client pointed at the gateway (or any compatible endpoint)."""
    from openai import OpenAI

    return OpenAI(
        base_url=os.environ.get("GATEWAY_BASE_URL", "http://127.0.0.1:4000"),
        api_key=os.environ.get("GATEWAY_API_KEY", "not-set"),
    )


def extract_page(
    image_path: str | Path,
    page: int,
    client=None,
    model: str | None = None,
    max_rereads: int = 1,
    seller_state: str | None = None,
) -> PageResult:
    client = client or make_client()
    model = model or os.environ.get("EXTRACT_MODEL", "vision-default")
    image_path = Path(image_path)

    messages = [
        {"role": "system", "content": SPEC_PATH.read_text(encoding="utf-8")},
        {"role": "user", "content": [
            {"type": "text", "text": f"Extract page {page}. Reply with the JSON object only."},
            _image_part(image_path),
        ]},
    ]

    doc, problems = None, []
    for attempt in range(1, max_rereads + 2):
        reply = client.chat.completions.create(model=model, messages=messages, temperature=0)
        text = reply.choices[0].message.content or ""
        try:
            doc = parse_json(text)
        except (ValueError, json.JSONDecodeError) as exc:
            problems = [f"[error] reply: {exc}"]
            doc = None
        else:
            found = invariants.check(doc, seller_state=seller_state)
            problems = [str(i) for i in found]
            verdict = invariants.status(found)
            if verdict == "accepted":
                return PageResult(page, "accepted", doc, problems, attempt)
            if verdict == "needs_review":
                # Nothing contradicts itself, but something is unreadable or the page
                # claims to be blank. Re-asking would only invite a guess.
                return PageResult(page, "needs_review", doc, problems, attempt)

        # Ask for a targeted re-read rather than a fresh guess.
        messages += [
            {"role": "assistant", "content": text},
            {"role": "user", "content": (
                "These checks failed:\n- " + "\n- ".join(problems) +
                "\nRe-read ONLY the cells involved on the same image. Change a digit only if "
                "the ink supports it. If a value is genuinely unreadable, set it to \"\" and list "
                "it in unreadable_fields. Reply with the full JSON object only."
            )},
        ]

    # Still contradicting itself after the re-reads: a person must look at it.
    status = "failed" if doc is None else "rejected"
    return PageResult(page, status, doc, problems, max_rereads + 1)
