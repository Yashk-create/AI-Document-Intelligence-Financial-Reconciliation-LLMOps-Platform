# Evaluation

The live project measured correctness indirectly, through arithmetic invariants and
reconciliation against the GST portal and bank. This folder measures it directly:
**field-level accuracy against hand-checked labels**.

```bash
python run.py extract samples/bills/ -o out/      # needs the gateway running
python eval/score.py samples/bills out/ --md eval/results.md
```

Each field gets one of three outcomes:

| Outcome | Meaning |
|---|---|
| correct | matches the label (numbers compared by value, so `836820.6` == `836820.60`) |
| wrong | a different, non-empty value: the dangerous case |
| abstained | `""` where the label has a value: safe, goes to a human |

Keeping *wrong* and *abstained* apart matters. A system that abstains on 10% of fields
and is never wrong is far more useful for accounting than one that is 95% "accurate" with
5% confident errors.

## Status

- `samples/bills/` holds 5 synthetic bills with labels. That is enough to exercise the harness,
  not enough to report a meaningful accuracy.
- **Next:** 40–60 labelled pages, either real pages with sensitive fields redacted or more
  varied synthetic ones, reported per field and per model alias, with and without the
  re-read loop. Results will go in `eval/results.md`.
