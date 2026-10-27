# Case study: from paper bills to checked records

*Sep–Oct 2026 · built for a small trading business (name withheld). This write-up covers the
engineering only. Business data and incidents are left out; the headline results are in the
[README](../README.md#results).*

## The problem

The business's newest records existed only as **phone photos of handwritten cash/credit
memos**, alongside GST portal downloads and bank statements. The goal was to turn the photos
into structured records that could be checked against those independent sources before
anything was written to an accounting system.

What "done" meant:

1. a structured record for every supported document;
2. amounts, tax, weights, rates, dates and parties internally consistent;
3. totals tied against independent sources, with every difference explained;
4. records written through an API and checked by reading them back;
5. anything uncertain left visible for a person, never silently accepted.

## 1. Building the gateway, and five faults behind one symptom

I put a **self-hosted LiteLLM proxy** in front of all model traffic: one OpenAI-compatible
endpoint, with model **aliases** so a client asks for `vision-default` and the config decides
which provider serves it.

The first request failed with a bare "connection failed". Bisecting the stack layer by layer
turned up five separate faults:

| Layer | Fault |
|---|---|
| Client pre-flight | the client refused to start because an API-key variable it checks locally was unset |
| Proxy | "No connected db": key and spend features needed a database or had to be disabled |
| Routing | "Invalid model name": the alias map didn't match what the client sent |
| Hidden sub-call | the client fired an extra auto-review request at a model that wasn't configured |
| Provider translation | tool results replayed in a shape one backend rejected (`unexpected tool_use_id`, "assistant prefill not supported") |

**Lesson:** a shared API shape doesn't make backends feature-compatible. Test each backend's
capabilities and read the actual error events instead of changing config at random.

## 2. Cost observability, and the bug that hid it

I wrote a LiteLLM `CustomLogger` callback that records each call's tokens, cached tokens,
provider model, latency and cost into a thread-safe SQLite ledger, with a FastAPI dashboard
showing spend, budget remaining and a rolling 60-second rate. Prompt text and request
metadata are deliberately **not** stored, because prompts can contain document contents.

A separate script ([`cost_analysis/`](../cost_analysis/)) reconstructs a usage ledger from
agent session logs and prices it with a local table. That gives an estimate, not an invoice.

**What I found when preparing this repo:** the live ledger held exactly one row, a manual
test. Driving the callback through a real LiteLLM Router in a test explained why, and turned
up more:

- `async_log_success_event` was indented at module level instead of inside the class. A
  Router only calls the async hooks, so real traffic never reached the database.
- The "alias" column was filled from `kwargs["model"]`, which LiteLLM sets to the *provider*
  model. The alias the client asked for lives in the logging payload's `model_group`, so the
  per-alias view the meter was built for never existed.
- The 60-second rate compared ISO timestamps (`2026-09-17T10:00`) with SQLite's `datetime()`
  text (`2026-09-17 10:00`). Because `'T' > ' '`, the whole day counted as "the last minute".
- Failures weren't recorded at all. And LiteLLM's error strings and logging payload carry
  prompt content, so the stored prompt preview was a liability, not a feature.

All of these are fixed and tested. The tracker now copies a fixed whitelist of fields, and old
ledgers are migrated and vacuumed. The full record is in [`cost-tracker.md`](cost-tracker.md).

**Lesson:** a meter you haven't tested through the real framework isn't a meter yet. Mocks
written from memory get the hook signature, the async path and the field names wrong. A
test against the actual library caught all three.

## 3. The extraction contract

Three constraints shaped the design:

- **Image budget.** A per-conversation image cap, and `413 Payload Too Large` when photos
  were batched. Fix: two renditions per photo (1024 px triage, 2048 px read) and **one page
  per call**. `python run.py images` reports the before/after size for any photo folder.
- **Drift.** A model asked for many rows at once will invent a plausible digit. One page per
  call keeps errors local and each output inspectable.
- **Verifiability.** Free text can't be checked, so the output is a strict JSON contract
  ([`billx/EXTRACT_SPEC.md`](../billx/EXTRACT_SPEC.md)). Numbers are plain strings, there are
  no extra keys, and an unreadable value becomes `""` plus an entry in `unreadable_fields`.

The key idea is that **a handwritten bill is internally redundant**. Weight × rate gives the
amount, the amounts give the total, 9% gives each GST head, and the grand total is written
again in words. The validator checks all of that independently of the model and then makes
one of three decisions:

| Decision | When | What happens |
|---|---|---|
| `accepted` | every required field present, nothing unreadable, every check passes | may be posted |
| `rejected` | something is missing, malformed or contradicts the arithmetic | targeted re-read, then a person |
| `needs_review` | the model marked a field unreadable, or the page claims to be blank | straight to a person, no re-read |

A GSTIN checksum caught a handwritten `Z` read as `2`, which no arithmetic check could have
caught.

**Honest gap:** there was no hand-labelled ground-truth set, so there is no defensible
accuracy percentage. About half the pages in an early test batch were flagged for review. The
[`eval/`](../eval/) harness exists to close this gap.

## 4. Deduplication: pixels propose, content decides

Re-shot and re-shared photos of the same bill are common. A 64-bit perceptual hash (dHash)
cheaply finds candidate duplicates. On the synthetic set, though, *different* bills printed on
the same form template hash within 0–4 bits of each other: the template dominates a tiny
greyscale. So the hash only proposes candidates, and a duplicate is confirmed when the
normalised invoice number and the grand total agree.

## 5. Hardening after an external review

A review of the first public version found cases where the code could say "yes" when it
should have said "no":

- a bill with no invoice number, date or line items passed validation;
- a field the model marked unreadable was silently accepted;
- `NaN` and `Infinity` crashed the validator, and exponent or signed numbers were parsed;
- amount-in-words without paise hid a wrong paise value;
- tie-out matched `GS/12` against `PB/12`, matched two rows that had no number at all, and
  counted a document filed twice as "matched" when the two halves added up;
- allocation verification passed a payment that was never accounted for;
- the CLI exited 0 while bills still needed review.

Each one is fixed and has a regression test in
[`tests/test_regressions.py`](../tests/test_regressions.py). On the previous code, 49 of
those 51 tests fail.

**Lesson:** for financial data the dangerous failure is a false "yes". Tests should try hard
to get a wrong document accepted, not only check that good documents pass.

## 6. Lessons from writing to a live accounting system

- **Local-first.** Reason over local snapshots and use the API only for writes and one-time
  reads. Rate limits are a design input, not an afterthought.
- **Plan writes offline and prove them safe** (see [`billx/allocation.py`](../billx/allocation.py))
  before spending a single API call.
- **Snapshot before anything destructive, and dry-run it against a copy.** Bulk operations can
  have side effects nobody asked for. Verify by reading back.
- **Model entities before posting.** Settle one party, one contact and all of its tax
  registrations first.
- **Reconcile, don't plug.** A tie-out finds differences. An unexplained difference becomes a
  named, evidenced question for the owner or accountant, never a balancing entry.

## What I'd do differently

1. Put the gateway and the meter in front of every call from day one.
2. Write the extraction contract, and the tests that try to break it, before touching an image.
3. Go local-first from the first API call.
4. Build the labelled eval set at the start, not the end.

## My role

I designed the system and directed AI coding agents to implement it. I owned the
architecture, the extraction contract, the validation and reconciliation rules, and the
decision about what counted as correct, and I reviewed and debugged what the agents produced.
This repo is a cleaned-up, tested version of that work. Company data, credentials and
organisation-specific write code have been removed.
