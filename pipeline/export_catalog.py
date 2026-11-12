"""Bundle sanitized public reports (report.public.json only) for shipping with the catalog.

Each report is passed through the current sanitizer on the way in, so the bundle never lags behind it. Only reports
referenced by catalog/results.jsonl are bundled."""

import io
import json
import sys
import tarfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import evaluate as ev


def main() -> None:
    records = ev.load_results()
    ev.refuse_pilot(records, str(ev.RESULTS))  # the public build never ships a pilot result (ADR-0007)
    dirs = {rec["report_dir"] for rec in records if rec.get("report_dir")}
    with tarfile.open(ev.ROOT / "catalog" / "public-reports.tar.gz", "w:gz") as t:
        for p in sorted(ev.REPORTS.glob("*/*/report.public.json")):
            if str(p.parent.relative_to(ev.ROOT)) not in dirs:
                continue
            data = json.dumps(ev.sanitize_report(json.loads(p.read_text())), indent=1).encode()
            info = tarfile.TarInfo(str(p.relative_to(ev.REPORTS)))
            info.size, info.mtime, info.mode = len(data), int(p.stat().st_mtime), 0o644
            t.addfile(info, io.BytesIO(data))
    print("ok")


if __name__ == "__main__":
    main()
