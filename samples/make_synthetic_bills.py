"""Generate synthetic handwritten-style cash memos plus ground-truth labels.

Real bills from the project cannot be published (live company data), so the
demo and the eval harness run on look-alikes: a printed form layout with the
variable fields drawn in a handwriting-style font, slight rotation and noise.
Every party, GSTIN, vehicle number and amount here is invented. The PAN part of
each GSTIN has 'Z' as its 4th character, which is not a valid PAN holder type,
and the state code is 00, which no GST registration uses. Together that makes
every GSTIN here structurally impossible for a real taxpayer, while still passing
the format and checksum rules the validator enforces. Every image also carries a
visible SYNTHETIC SAMPLE watermark.

    python samples/make_synthetic_bills.py          # writes samples/bills/
"""
from __future__ import annotations

import json
import random
import sys
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from billx.gstin import check_char  # noqa: E402

OUT = Path(__file__).with_name("bills")
W, H = 1240, 1754   # A4 at 150 dpi

PRINT_FONTS = ["arial.ttf", "DejaVuSans.ttf", "LiberationSans-Regular.ttf"]
HAND_FONTS = ["Inkfree.ttf", "segoepr.ttf", "comic.ttf", "DejaVuSans-Oblique.ttf"]
FONT_DIRS = [Path("C:/Windows/Fonts"), Path("/usr/share/fonts/truetype/dejavu"),
             Path("/usr/share/fonts/truetype/liberation"), Path("/Library/Fonts")]


def font(candidates, size):
    for name in candidates:
        for d in FONT_DIRS:
            if (d / name).exists():
                return ImageFont.truetype(str(d / name), size)
    return ImageFont.load_default(size)


def make_gstin(state: str, pan: str, entity: str = "1") -> str:
    first14 = f"{state}{pan}{entity}Z"
    return first14 + check_char(first14)


ONES = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten",
        "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen", "Seventeen",
        "Eighteen", "Nineteen"]
TENS = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]


def _two(n):
    return ONES[n] if n < 20 else (TENS[n // 10] + (" " + ONES[n % 10] if n % 10 else ""))


def _three(n):
    h, r = divmod(n, 100)
    return " ".join(p for p in ((ONES[h] + " Hundred") if h else "", _two(r) if r else "") if p)


def to_words(amount: Decimal) -> str:
    rupees = int(amount)
    paise = int((amount - rupees) * 100)
    parts = []
    for div, name in ((10_000_000, "Crore"), (100_000, "Lakh"), (1000, "Thousand")):
        q, rupees = divmod(rupees, div)
        if q:
            parts.append(f"{_three(q) if q >= 100 else _two(q)} {name}")
    if rupees:
        parts.append(_three(rupees))
    text = " ".join(parts) + " Rupees"
    if paise:
        text += f" {_two(paise)} Paise"
    return text + " Only"


def q2(x: Decimal) -> Decimal:
    return x.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


STATE = "00"   # not an Indian GST state code
SELLER_GSTIN = make_gstin(STATE, "TSTZV4826M")
BILLS = [
    ("000101", "CASH MEMO", "2025-04-03", "Demo Buyer A", "Address Line A, Sample City",
     make_gstin(STATE, "BBBZD1001B"), "XX01AB1001", "M.S. Scrap", "7204", "18.420", "38500"),
    ("000102", "CREDIT MEMO", "2025-04-07", "Demo Buyer B", "Address Line B, Sample City",
     make_gstin(STATE, "CCCZE2002C"), "XX02AB2002", "H.M.S. Scrap", "7204", "22.075", "39250"),
    ("000103", "CASH MEMO", "2025-04-11", "Demo Buyer C", "Address Line C, Sample City",
     make_gstin(STATE, "DDDZT3003D"), "XX01AB3003", "Turning Scrap", "7204", "9.860", "31800"),
    ("000104", "CREDIT MEMO", "2025-04-15", "Demo Buyer D", "Address Line D, Sample City",
     make_gstin(STATE, "EEEZP4004E"), "XX01AB4004", "Aluminium Scrap", "7602", "3.215", "152000"),
    ("000105", "CASH MEMO", "2025-04-19", "Demo Buyer E", "Address Line E, Sample City",
     make_gstin(STATE, "FFFZM5005F"), "XX02AB5005", "M.S. Scrap", "7204", "21.145", "38700"),
]
MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


def label_for(i: int, bill) -> dict:
    inv, memo, date, name, addr, gstin, vehicle, goods, hsn, weight, rate = bill
    amount = q2(Decimal(weight) * Decimal(rate))
    gst_head = q2(amount * Decimal("0.09"))
    grand = amount + gst_head * 2
    y, m, d = date.split("-")
    return {
        "page": i,
        "invoice_no": inv,
        "memo_type": memo,
        "date_raw": f"{d} {MONTHS[int(m) - 1]} {y}",
        "date_iso": date,
        "buyer_name": name,
        "buyer_address": addr,
        "buyer_gstin": gstin,
        "state_code": STATE,
        "broker": "",
        "despatch_through": vehicle,
        "line_items": [{"s_no": 1, "particulars": goods, "hsn_code": hsn, "weight": weight,
                        "weight_unit": "MT", "rate_per_mt": rate, "amount": f"{amount:.2f}"}],
        "total": f"{amount:.2f}",
        "cgst_9": f"{gst_head:.2f}",
        "sgst_9": f"{gst_head:.2f}",
        "total_amt_gst": f"{gst_head * 2:.2f}",
        "grand_total": f"{grand:.2f}",
        "amount_in_words": to_words(grand),
        "seller_gstin": SELLER_GSTIN,
        "page_is_blank": False,
        "unreadable_fields": [],
        "extraction_notes": "",
    }


def draw_bill(label: dict, seed: int) -> Image.Image:
    rnd = random.Random(seed)
    img = Image.new("RGB", (W, H), (250, 248, 240))
    d = ImageDraw.Draw(img)
    pf, pb, ph = font(PRINT_FONTS, 22), font(PRINT_FONTS, 30), font(PRINT_FONTS, 44)
    hf = font(HAND_FONTS, 34)
    ink = (20, 30, 110)
    hand = lambda xy, text, f=hf: d.text((xy[0] + rnd.randint(-4, 4), xy[1] + rnd.randint(-3, 3)),
                                         text, font=f, fill=ink)

    d.rectangle([40, 40, W - 40, H - 40], outline="black", width=3)
    d.text((W // 2, 80), "SYNTHETIC SAMPLE SELLER", font=ph, fill="black", anchor="mm")
    d.text((W // 2, 125), "Not a real business  -  generated test document",
           font=pf, fill="black", anchor="mm")
    d.text((60, 170), f"GST IN : {SELLER_GSTIN}", font=pb, fill="black")
    for x, t in ((60, "CASH MEMO"), (300, "CREDIT MEMO")):
        d.rectangle([x, 220, x + 210, 262], outline="black", width=2)
        d.text((x + 105, 241), t, font=pf, fill="black", anchor="mm")
    mx = 60 if label["memo_type"] == "CASH MEMO" else 300
    d.ellipse([mx - 8, 212, mx + 218, 270], outline=ink, width=3)

    d.text((60, 300), "INVOICE No. /GS", font=pf, fill="black")
    hand((260, 290), label["invoice_no"])
    d.text((700, 300), "Date", font=pf, fill="black")
    hand((780, 290), label["date_raw"])
    d.text((60, 370), "Buyer :", font=pf, fill="black")
    hand((170, 360), label["buyer_name"])
    hand((170, 410), label["buyer_address"], font(HAND_FONTS, 28))
    d.text((60, 480), "GST IN", font=pf, fill="black")
    hand((170, 470), label["buyer_gstin"])
    d.text((800, 480), "State Code", font=pf, fill="black")
    hand((950, 470), label["state_code"])
    d.text((60, 540), "Broker Shree :", font=pf, fill="black")
    hand((240, 530), "-")
    d.text((640, 540), "Despatch Through :", font=pf, fill="black")
    hand((880, 530), label["despatch_through"])

    cols = [60, 140, 520, 660, 820, 980, W - 60]
    heads = ["S.No.", "Particular of Goods", "HSN CODE", "Weight", "Rate Per M/T", "Amount"]
    top, row_h = 600, 60
    for r in range(6):
        d.line([cols[0], top + r * row_h, cols[-1], top + r * row_h], fill="black", width=2)
    for x in cols:
        d.line([x, top, x, top + 5 * row_h], fill="black", width=2)
    for i, h in enumerate(heads):
        d.text(((cols[i] + cols[i + 1]) // 2, top + 30), h, font=pf, fill="black", anchor="mm")
    item = label["line_items"][0]
    y = top + row_h + 8
    for i, val in enumerate(["1", item["particulars"], item["hsn_code"], item["weight"],
                             item["rate_per_mt"], item["amount"]]):
        hand((cols[i] + 12, y), val, font(HAND_FONTS, 30))

    ty = top + 5 * row_h + 30
    for i, (name, key) in enumerate((("Total", "total"), ("CGST 9%", "cgst_9"), ("SGST 9%", "sgst_9"),
                                     ("Total AMT.GST", "total_amt_gst"), ("Grand Total", "grand_total"))):
        yy = ty + i * 58
        d.text((700, yy + 12), name, font=pf, fill="black")
        d.rectangle([900, yy, W - 60, yy + 50], outline="black", width=2)
        hand((915, yy + 4), label[key], font(HAND_FONTS, 30))

    wy = ty + 5 * 58 + 40
    d.text((60, wy), "Value in words Rupees", font=pf, fill="black")
    hand((60, wy + 40), label["amount_in_words"], font(HAND_FONTS, 30))
    d.text((60, H - 180), "SYNTHETIC SAMPLE - NOT A REAL INVOICE - for software testing only", font=pf, fill="black")
    d.text((W - 360, H - 120), "Authorised Signatory", font=pf, fill="black")

    # Diagonal watermark so the image can never pass for a real invoice.
    mark = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mark).text((W // 2, H // 2), "SYNTHETIC SAMPLE", font=font(PRINT_FONTS, 105),
                              fill=70, anchor="mm")
    mark = mark.rotate(35, resample=Image.BICUBIC)
    img.paste((200, 60, 60), (0, 0), mark)

    # Make it look photographed: slight rotation, blur and sensor noise.
    img = img.rotate(rnd.uniform(-1.2, 1.2), resample=Image.BICUBIC, expand=False, fillcolor=(235, 232, 225))
    img = img.filter(ImageFilter.GaussianBlur(0.6))
    noise = Image.effect_noise((W, H), 12).convert("RGB")
    return Image.blend(img, noise, 0.06)


def misread(label: dict) -> dict:
    """A plausible extraction of a bill with two typical handwriting misreads."""
    bad = json.loads(json.dumps(label))
    item = bad["line_items"][0]
    item["amount"] = item["amount"].replace("6", "0", 1)  # '6' read as '0'
    last = bad["buyer_gstin"][14]
    bad["buyer_gstin"] = bad["buyer_gstin"][:14] + ("2" if last != "2" else "Z")  # 'Z' <-> '2'
    bad["extraction_notes"] = "Demo: a plausible-looking extraction with two misreads."
    return bad


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    labels = []
    for i, bill in enumerate(BILLS, start=1):
        label = label_for(i, bill)
        labels.append(label)
        draw_bill(label, seed=i).save(OUT / f"bill_{i:02d}.jpg", "JPEG", quality=85)
        # newline="\n": identical bytes on every OS, so regeneration is reproducible
        (OUT / f"bill_{i:02d}.json").write_text(json.dumps(label, indent=2) + "\n",
                                                encoding="utf-8", newline="\n")
        print(f"bill_{i:02d}: grand_total {label['grand_total']}")
    (OUT.parent / "misread_bill_02.json").write_text(
        json.dumps(misread(labels[1]), indent=2) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
