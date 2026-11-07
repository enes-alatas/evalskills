"""Write tiny, valid single/multi-page text PDFs with the stdlib (for shared-task fixtures)."""

import sys


def pdf(pages: list[str]) -> bytes:
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", None, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for text in pages:
        lines = text.split("\n")
        stream = (
            "BT /F1 12 Tf 72 720 Td 16 TL "
            + " ".join("(" + l.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") + ") '" for l in lines)
            + " ET"
        )
        objs.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
        content = len(objs)
        objs.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {content} 0 R "
            f"/Resources << /Font << /F1 3 0 R >> >> >>"
        )
        kids.append(len(objs))
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] /Count {len(kids)} >>"
    out, offs = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offs)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


if __name__ == "__main__":
    d = sys.argv[1]
    open(f"{d}/invoice.pdf", "wb").write(
        pdf(
            [
                "ACME Tooling Ltd.  Invoice INV-20417\nDate: 2026-10-03\nBill to: Northwind Labs\n"
                "Item: Calibration service   2 x 410.00\nItem: Spare probe   1 x 95.50\nSubtotal: 915.50\n"
                "VAT 20%: 183.10\nTotal due: EUR 1098.60\nPayment terms: 30 days"
            ]
        )
    )
    open(f"{d}/report-a.pdf", "wb").write(pdf(["Quarterly report A - page 1", "Quarterly report A - page 2"]))
    open(f"{d}/report-b.pdf", "wb").write(pdf(["Quarterly report B - page 1"]))
