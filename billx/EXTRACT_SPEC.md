# Extraction spec: handwritten cash/credit memo (one page)

You are extracting ONE page of a batch of cash/credit memos (sales bills) issued by a
steel and scrap trader. The page is a photo or scan of a printed form with HANDWRITTEN
entries. There is no text layer.

## Your input: exactly ONE image
The whole form, rendered at the highest resolution that will not be downscaled by the
viewer (about 1330 x 1800 px). Work from this single image.

## Hard image budget
The run fails if too many images are processed. You get this one page. If you are
re-asked to re-read specific fields, you may receive at most 2 zoomed crops of the same
page. Never ask for more.

## Form layout (top to bottom)
1. Top-left box: seller GSTIN (printed constant); "CASH MEMO" / "CREDIT MEMO" tick boxes,
   exactly one is marked; "INVOICE No." + handwritten serial; "Date" + handwritten date.
2. "Buyer:": handwritten name and address (1-3 lines).
3. "GST IN": buyer's handwritten GSTIN; "State Code": handwritten 2-digit code.
4. "Broker Shree :" and "Despatch Through :" (usually a vehicle number).
5. Line item table: S.No. | Particular of Goods | HSN CODE | Weight | Rate Per M/T | Amount.
   Usually exactly ONE line item is written; the remaining rows are empty.
6. Totals column at the right: Total, CGST 9%, SGST 9%, Total AMT.GST, Grand Total.
7. "Value in words Rupees ....": the grand total written out in English words.
8. Declaration / bank details / signature block: ignore.

## Required output JSON (exact keys, exact types)
```json
{
  "page": 1,
  "invoice_no": "000101",
  "memo_type": "CASH MEMO",
  "date_raw": "03 APR 2025",
  "date_iso": "2025-04-03",
  "buyer_name": "",
  "buyer_address": "",
  "buyer_gstin": "",
  "state_code": "00",
  "broker": "",
  "despatch_through": "",
  "line_items": [
    {
      "s_no": 1,
      "particulars": "M.S. Scrap",
      "hsn_code": "7204",
      "weight": "18.420",
      "weight_unit": "MT",
      "rate_per_mt": "38500",
      "amount": "709170.00"
    }
  ],
  "total": "709170.00",
  "cgst_9": "63825.30",
  "sgst_9": "63825.30",
  "total_amt_gst": "127650.60",
  "grand_total": "836820.60",
  "amount_in_words": "",
  "seller_gstin": "",
  "page_is_blank": false,
  "unreadable_fields": [],
  "extraction_notes": ""
}
```

## Rules
- Numbers: return as STRINGS, plain digits with `.` decimal separator, no thousands
  separators, no currency symbols, no units. Two decimals for money ("709170.00").
- NEVER invent a value. If a handwritten value cannot be read with reasonable confidence,
  use "" and add the field name to `unreadable_fields`.
- `weight` is the quantity in tonnes (M/T): the big number in the Weight cell. A smaller
  number written underneath it is NOT the weight; ignore it.
- ARITHMETIC IS YOUR BEST OCR CHECK. Use it to resolve ambiguous digits:
  * amount == weight * rate_per_mt (tolerance 0.05)
  * total == sum of line item amounts
  * cgst_9 == round(total * 0.09, 2) and sgst_9 == round(total * 0.09, 2)
  * total_amt_gst == cgst_9 + sgst_9
  * grand_total == total + total_amt_gst
  If a digit you read breaks the arithmetic, re-read that cell and pick the digits that
  make the arithmetic consistent, but only if the ink supports them. Record any residual
  mismatch in `extraction_notes`.
- `amount_in_words` cross-checks `grand_total`: transcribe the handwritten words and note
  any disagreement with the digits in `extraction_notes`.
- If the page has no handwritten bill content, set `"page_is_blank": true`, leave
  `line_items` as [] and the money fields as "".
- Do not add extra keys. Do not wrap the JSON in commentary.

## Deliverable
Your reply must contain ONLY the JSON object, nothing else.
