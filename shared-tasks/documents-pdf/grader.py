#!/usr/bin/env python3
"""Shared deterministic grader for the documents/pdf category (default_plus_custom).
Checks output files only; stdlib so it runs in any task container."""

import json
import os
import re
import zlib
from pathlib import Path

ENTRY = Path(os.environ.get("HARBOR_ENTRY_JSON", "/tests/entry.json"))
REWARD_JSON = Path(os.environ.get("HARBOR_REWARD_JSON", "/logs/verifier/reward.json"))
REWARD_TXT = Path(os.environ.get("HARBOR_REWARD_TXT", "/logs/verifier/reward.txt"))
OUT = Path("/workspace/output")


def pdf_pages_and_text(p: Path):
    data = p.read_bytes()
    if not data.startswith(b"%PDF"):
        return None, ""
    pages = len(re.findall(rb"/Type\s*/Page(?!s)", data))
    text = []
    for m in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", data, re.S):
        raw = m.group(1)
        try:
            raw = zlib.decompress(raw)
        except zlib.error:
            pass
        text += re.findall(rb"\(((?:\\.|[^\\)])*)\)", raw)
    return pages, b" ".join(text).decode("latin-1", "replace")


def grade(eid: str):
    if eid == "pdf-shared-extract-total":
        try:
            j = json.loads((OUT / "invoice.json").read_text())
        except Exception as e:
            return 0.0, f"invoice.json missing or invalid: {type(e).__name__}"
        checks = [
            str(j.get("invoice_number", "")).strip() == "INV-20417",
            abs(float(str(j.get("total_due", "nan")).replace(",", "")) - 1098.60) < 0.005
            if re.match(r"^[\d.,]+$", str(j.get("total_due", "")))
            else False,
            str(j.get("currency", "")).upper() in ("EUR", "€"),
            "northwind" in str(j.get("bill_to", "")).lower(),
        ]
        return sum(checks) / 4, f"{sum(checks)}/4 fields correct"
    if eid == "pdf-shared-merge":
        p = OUT / "merged.pdf"
        if not p.is_file():
            return 0.0, "merged.pdf missing"
        pages, text = pdf_pages_and_text(p)
        if pages is None:
            return 0.0, "not a PDF"
        order = [text.find(s) for s in ("report A - page 1", "report A - page 2", "report B - page 1")]
        ok_order = all(i >= 0 for i in order) and order == sorted(order)
        # Text may be compressed/re-encoded by some tools; fall back to page count only.
        return (1.0 if pages == 3 and ok_order else 0.75 if pages == 3 else 0.0), f"pages={pages} order_ok={ok_order}"
    if eid == "pdf-shared-create":
        p = OUT / "memo.pdf"
        if not p.is_file():
            return 0.0, "memo.pdf missing"
        pages, text = pdf_pages_and_text(p)
        if pages is None:
            return 0.0, "not a PDF"
        hits = sum(s in text for s in ("Release memo", "Version 2.4 ships on 14 November."))
        return (0.5 * (pages == 1) + 0.25 * hits), f"pages={pages} strings_found={hits}/2"
    return None, "no deterministic check for this case"


def main():
    entry = json.loads(ENTRY.read_text()) if ENTRY.exists() else {}
    eid = str(entry.get("id") or entry.get("entry_id") or "")
    score, reason = grade(eid)
    reward = {"custom_metrics": {}, "details": {}}
    if score is not None:
        reward["custom_metrics"]["pdf_task_success"] = score
        reward["details"]["pdf_task_success"] = {"score": score, "reason": reason}
    REWARD_JSON.parent.mkdir(parents=True, exist_ok=True)
    REWARD_JSON.write_text(json.dumps(reward, indent=2))
    REWARD_TXT.write_text(str(score if score is not None else 0.0))


if __name__ == "__main__":
    main()
