"""Command-line entry point.

    python run.py validate samples/misread_bill_02.json      # run the checks on a JSON
    python run.py extract samples/bills/bill_01.jpg -o out/  # model call (needs the gateway)
    python run.py extract samples/bills/ -o out/             # every image in a folder
    python run.py images photos/ prepared/                   # renditions + dHash candidates

Exit status (so scripts and CI can rely on it):
    0  every document accepted
    1  at least one document needs review, was rejected, or failed
    2  bad input: missing path, unreadable JSON, or nothing to process
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from billx import images, invariants
from billx.extract import extract_page

IMAGE_EXTS = (".jpg", ".jpeg", ".png")
OK, NOT_ACCEPTED, BAD_INPUT = 0, 1, 2


def cmd_validate(args) -> int:
    worst = OK
    for path in args.files:
        try:
            doc = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"{path}: cannot read JSON: {exc}")
            worst = BAD_INPUT
            continue
        found = invariants.check(doc, seller_state=args.seller_state)
        verdict = invariants.status(found)
        print(f"{path}: {verdict.upper()}")
        for issue in found:
            print(f"   {issue}")
        if verdict != "accepted" and worst == OK:
            worst = NOT_ACCEPTED
    return worst


def cmd_extract(args) -> int:
    src = Path(args.path)
    if not src.exists():
        print(f"{src}: no such file or folder")
        return BAD_INPUT
    files = sorted(p for p in src.iterdir() if p.suffix.lower() in IMAGE_EXTS) if src.is_dir() else [src]
    if not files:
        print(f"{src}: no images found")
        return BAD_INPUT
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    summary = {"accepted": 0, "needs_review": 0, "rejected": 0, "failed": 0}
    for page, image in enumerate(files, start=1):
        result = extract_page(image, page, model=args.model, max_rereads=args.rereads,
                              seller_state=args.seller_state)
        summary[result.status] += 1
        if result.doc is not None:
            (out / f"{image.stem}.json").write_text(json.dumps(result.doc, indent=2), encoding="utf-8")
        print(f"{image.name}: {result.status} after {result.attempts} call(s)")
        for issue in result.issues:
            print(f"   {issue}")
    print(summary)
    return OK if summary["accepted"] == len(files) else NOT_ACCEPTED


def cmd_images(args) -> int:
    if not Path(args.src).is_dir():
        print(f"{args.src}: not a folder")
        return BAD_INPUT
    records = images.make_renditions(args.src, args.out)
    if not records:
        print(f"{args.src}: no images found")
        return BAD_INPUT
    images.write_manifest(records, Path(args.out) / "manifest.csv")
    orig = sum(r.orig_kb for r in records)
    read = sum(r.read_kb for r in records)
    saved = f" ({1 - read / orig:.1%} smaller)" if orig else ""
    print(f"{len(records)} images: {orig / 1024:.1f} MB -> {read / 1024:.1f} MB read renditions{saved}")
    for a, b, d in images.near_duplicates(records):
        print(f"   duplicate candidate ({d} bits): {a} ~ {b} - confirm on extracted content")
    return OK


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Handwritten bill extraction")
    ap.add_argument("--seller-state", default=None,
                    help="seller's GST state code (default: taken from seller_gstin)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("validate", help="run deterministic checks on extracted JSON")
    v.add_argument("files", nargs="+")
    v.set_defaults(func=cmd_validate)

    e = sub.add_parser("extract", help="extract bills with a vision model via the gateway")
    e.add_argument("path")
    e.add_argument("-o", "--out", default="out")
    e.add_argument("--model", default=None, help="gateway model alias (default: $EXTRACT_MODEL)")
    e.add_argument("--rereads", type=int, default=1)
    e.set_defaults(func=cmd_extract)

    i = sub.add_parser("images", help="make triage/read renditions and find duplicate candidates")
    i.add_argument("src")
    i.add_argument("out")
    i.set_defaults(func=cmd_images)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
