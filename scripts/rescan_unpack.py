"""Unpack a catalog-rescan run's output from its log (#110).

Usage: gh run view RUN_ID --log | python3 -I scripts/rescan_unpack.py OUT_DIR
Only regular files are written, under OUT_DIR; links, absolute paths and `..` are skipped.
"""

import base64
import io
import sys
import tarfile
from pathlib import Path


def main(out: Path) -> int:
    lines, inside = [], False
    for line in sys.stdin:
        text = line.rstrip("\n").split("\t")[-1].split(" ", 1)[-1]  # job, step, then "timestamp text"
        if text == "BEGIN catalog-rescan.tar.gz":
            inside, lines = True, []
        elif text == "END catalog-rescan.tar.gz":
            inside = False
        elif inside:
            lines.append(text.strip())
    data = base64.b64decode("".join(lines), validate=True)
    out.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as t:
        for m in t.getmembers():
            parts = Path(m.name).parts
            if not m.isfile() or m.name.startswith("/") or ".." in parts:
                continue
            dest = out.joinpath(*parts)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(t.extractfile(m).read())
    print(f"unpacked {len(data)} bytes into {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
