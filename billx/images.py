"""Image preparation: renditions, perceptual-hash dedupe, and PDF page rendering.

Phone photos of bills arrive at ~3000x4000 px and ~6 MB each. Sending them as-is
hit HTTP 413 (payload too large) and a per-conversation image cap. The fix was
two renditions per photo:

* triage (1024 px long side): cheap, used only to decide whether a photo is a
  bill, a duplicate, or junk
* read (2048 px long side): what the model actually reads line items from

plus a 64-bit difference hash (dHash) per photo so re-shot or re-shared photos
of the same bill are flagged before they cost a model call. `python run.py images`
prints the before/after size for any folder of photos.
"""
from __future__ import annotations

import csv
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from PIL import Image, ImageFilter, ImageOps

TRIAGE_SIDE = 1024
READ_SIDE = 2048
IMAGE_EXTS = (".jpg", ".jpeg", ".png")


def dhash(img: Image.Image, size: int = 8) -> int:
    """Difference hash: compare each pixel with its right neighbour on a tiny greyscale."""
    g = ImageOps.grayscale(img).resize((size + 1, size), Image.LANCZOS)
    px = list(g.tobytes())
    bits = 0
    for r in range(size):
        for c in range(size):
            left = px[r * (size + 1) + c]
            right = px[r * (size + 1) + c + 1]
            bits = (bits << 1) | (1 if left > right else 0)
    return bits


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


@dataclass
class ImageRecord:
    file: str
    orig_w: int
    orig_h: int
    orig_kb: int
    triage_kb: int
    read_kb: int
    dhash: str


def make_renditions(src_dir: str | Path, out_dir: str | Path) -> list[ImageRecord]:
    """Write triage/ and read/ JPEGs for every image in src_dir; return a manifest."""
    src_dir, out_dir = Path(src_dir), Path(out_dir)
    records = []
    for name in sorted(os.listdir(src_dir)):
        if not name.lower().endswith(IMAGE_EXTS):
            continue
        path = src_dir / name
        with Image.open(path) as raw:
            img = ImageOps.exif_transpose(raw).convert("RGB")
        sizes = {}
        for tag, side, quality in (("triage", TRIAGE_SIDE, 80), ("read", READ_SIDE, 85)):
            target = out_dir / tag / (path.stem + ".jpg")
            target.parent.mkdir(parents=True, exist_ok=True)
            copy = img.copy()
            copy.thumbnail((side, side), Image.LANCZOS)
            copy.save(target, "JPEG", quality=quality, optimize=True)
            sizes[tag] = round(target.stat().st_size / 1024)
        records.append(ImageRecord(
            file=name, orig_w=img.width, orig_h=img.height,
            orig_kb=round(path.stat().st_size / 1024),
            triage_kb=sizes["triage"], read_kb=sizes["read"],
            dhash=f"{dhash(img):016x}",
        ))
    return records


def near_duplicates(records: list[ImageRecord], max_distance: int = 4) -> list[tuple[str, str, int]]:
    """CANDIDATE duplicate pairs: dHashes within max_distance bits.

    Caveat measured on the synthetic set: bills printed on the same form template
    hash almost identically (distance 0-4 at 8x8) even when the handwriting differs,
    because the template dominates a tiny greyscale. So a hit here means "look
    closer", and confirmation comes from content: see duplicate_documents().
    """
    pairs = []
    hashes = [(r.file, int(r.dhash, 16)) for r in records]
    for i in range(len(hashes)):
        for j in range(i + 1, len(hashes)):
            d = hamming(hashes[i][1], hashes[j][1])
            if d <= max_distance:
                pairs.append((hashes[i][0], hashes[j][0], d))
    return pairs


def duplicate_documents(docs: dict[str, dict]) -> list[list[str]]:
    """Confirmed duplicates: photos whose extracted invoice number AND grand total agree.

    docs maps image file name -> extracted bill JSON.
    """
    from .schema import to_decimal
    from .tieout import normalise_doc_no

    groups: dict[tuple, list[str]] = {}
    for name, doc in docs.items():
        number = normalise_doc_no(doc.get("invoice_no"))
        total = to_decimal(doc.get("grand_total"))
        if number is not None and total is not None:
            groups.setdefault((number, total), []).append(name)
    return [sorted(names) for names in groups.values() if len(names) > 1]


def write_manifest(records: list[ImageRecord], path: str | Path) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(asdict(records[0]).keys()))
        writer.writeheader()
        writer.writerows(asdict(r) for r in records)


# --- scanned PDFs -----------------------------------------------------------

MAX_PIXELS = 2_450_000   # keep under the viewer's downscale threshold:
MAX_WIDTH = 1334         # a downscale destroys handwriting legibility
MAX_HEIGHT = 1888


def _content_box(page, dpi: int = 200):
    """Ink bounding box via local-contrast thresholding, in PDF points."""
    import numpy as np

    pix = page.get_pixmap(dpi=dpi)
    gray = Image.frombytes("RGB", (pix.width, pix.height), pix.samples).convert("L")
    local = np.asarray(gray.filter(ImageFilter.BoxBlur(28)), dtype=np.int16)
    ink = np.asarray(gray, dtype=np.int16) < local - 32
    rows, cols = ink.sum(axis=1), ink.sum(axis=0)
    r = np.where(rows > 0.12 * rows.max())[0] if rows.max() else []
    c = np.where(cols > 0.12 * cols.max())[0] if cols.max() else []
    if len(r) < 2 or len(c) < 2:
        return None
    scale, pad = 72.0 / dpi, 4.0
    box = (max(c[0] * scale - pad, 0.0), max(r[0] * scale - pad, 0.0),
           min(c[-1] * scale + pad, page.rect.width), min(r[-1] * scale + pad, page.rect.height))
    if box[2] - box[0] < 200 or box[3] - box[1] < 400:
        return None
    return box


def render_pdf_pages(pdf_path: str | Path, out_dir: str | Path) -> list[Path]:
    """One JPEG per page: the form's content box at the highest DPI within the pixel budget."""
    import pymupdf

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    with pymupdf.open(pdf_path) as doc:
        for index, page in enumerate(doc, start=1):
            box = _content_box(page) or tuple(page.rect)
            w_pt, h_pt = box[2] - box[0], box[3] - box[1]
            dpi = next(
                (d for d in range(200, 60, -2)
                 if round(w_pt / 72 * d) <= MAX_WIDTH
                 and round(h_pt / 72 * d) <= MAX_HEIGHT
                 and round(w_pt / 72 * d) * round(h_pt / 72 * d) <= MAX_PIXELS),
                72,
            )
            pix = page.get_pixmap(dpi=dpi, clip=pymupdf.Rect(*box))
            image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            target = out_dir / f"page_{index:02d}.jpg"
            image.save(target, "JPEG", quality=92, optimize=True)
            written.append(target)
    return written
