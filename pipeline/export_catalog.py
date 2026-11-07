"""Bundle sanitized public reports (report.public.json only) for shipping with the catalog."""

import sys
import tarfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import evaluate as ev

with tarfile.open(ev.ROOT / "catalog" / "public-reports.tar.gz", "w:gz") as t:
    for p in sorted(ev.REPORTS.glob("*/*/report.public.json")):
        t.add(p, arcname=str(p.relative_to(ev.REPORTS)))
print("ok")
