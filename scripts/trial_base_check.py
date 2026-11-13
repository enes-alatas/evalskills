"""Check the trial base image on a real Docker, through the real socket proxy (#137; .github/workflows/trial-base.yml).

For each allowlisted task shape, it sends SkillEvaluator's original Dockerfile with a synthetic skill to the proxy's
POST /_evalskills/build. The proxy then checks the base image ID and runs the legacy builder (version=1, pull=0,
network none) on the committed final Dockerfile. So one run answers, on that Docker and image store:
- does version=1 still build?
- does FROM evalskills/trial-base:<tag> resolve to a `docker load`ed image with pull=0?
- does every shape build from the base image?
- with the same tag pinned to another ID, does the proxy refuse the build (503) and leave no new image behind?

Needs root (the proxy chowns its socket) and a Docker daemon at /var/run/docker.sock. Prints one JSON line per shape
and exits 1 if any shape fails. Never run it on the shared host: there, the host operator loads the image.

  sudo python3 -I scripts/trial_base_check.py --base-image evalskills/trial-base:d136a597 --base-id sha256:...
"""

import argparse
import http.client
import io
import json
import os
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "gateway"))
import docker_proxy as dp  # noqa: E402

BUILD = ROOT / "deploy" / "gvisor" / "build"


class _Unix(http.client.HTTPConnection):
    def __init__(self, path: str):
        super().__init__("localhost", timeout=900)
        self._path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._path)


def context(dockerfile: bytes) -> bytes:
    """A trial context with every folder a shape may COPY; the skill is synthetic."""
    files = {
        "Dockerfile": dockerfile,
        "skills/demo/SKILL.md": b"---\nname: demo\ndescription: base image check\n---\n",
        "input/case.txt": b"input\n",
        "repo/README.md": b"repo\n",
    }
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w", format=tarfile.PAX_FORMAT) as t:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    return out.getvalue()


def build(sock: str, body: bytes) -> tuple[int, dict]:
    conn = _Unix(sock)
    try:
        conn.request("POST", "/_evalskills/build", body=body, headers={"Host": "docker"})
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read() or b"{}")
    finally:
        conn.close()


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-image", required=True)
    ap.add_argument("--base-id", required=True)
    args = ap.parse_args(argv)
    shapes = [
        line.split()[0]
        for line in (BUILD / "allowed-dockerfiles.txt").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    cfg, proxy = start_proxy(args.base_image, args.base_id)
    failed = 0
    try:
        for sha in shapes:
            status, doc = build(cfg["listen"], context((BUILD / "original" / f"{sha}.Dockerfile").read_bytes()))
            row = {"shape": sha[:12], "status": status, "message": doc.get("message")}
            if status == 200:
                tag = doc["tag"]
                labels = json.loads(
                    subprocess.run(
                        ["docker", "image", "inspect", "--format", "{{json .Config.Labels}}", tag],
                        capture_output=True,
                        text=True,
                        check=True,
                    ).stdout
                )
                # runc here, on a throwaway CI runner only: the skill copy is in place and the verifier imports
                probe = subprocess.run(
                    [
                        "docker",
                        "run",
                        "--rm",
                        "--network",
                        "none",
                        tag,
                        "sh",
                        "-c",
                        "test -f /workspace/skills/demo/SKILL.md && python -c 'import ragas' && echo ok",
                    ],
                    capture_output=True,
                    text=True,
                )
                row.update(tag=tag, owned=labels.get(dp.OWNER_LABEL) == "1", probe=probe.stdout.strip())
                subprocess.run(["docker", "image", "rm", tag], capture_output=True)
            ok = status == 200 and row.get("owned") and row.get("probe") == "ok"
            failed += not ok
            print(json.dumps({**row, "ok": bool(ok)}))
    finally:
        proxy.shutdown()
    failed += not refuses_wrong_base(args.base_image, args.base_id, shapes[0])
    return 1 if failed else 0


def start_proxy(base_image: str, base_id: str):
    work = Path(tempfile.mkdtemp(prefix="trial-base-check-"))
    (work / "run").mkdir()
    cfg = dp.load_config(
        {
            "listen": str(work / "proxy.sock"),
            "listen_group": "",
            "upstream": "/var/run/docker.sock",
            "bind_root": str(work / "run"),
            "eval_uid": int(os.environ.get("SUDO_UID") or 1000),
            "builds": {
                "allowed_dockerfiles": str(BUILD / "allowed-dockerfiles.txt"),
                "base_image": base_image,
                "base_image_id": base_id,
            },
        }
    )
    proxy = dp.make_server(cfg)
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    return cfg, proxy


def owned_images() -> set[str]:
    out = subprocess.run(
        ["docker", "image", "ls", "-q", "--no-trunc", "--filter", f"label={dp.OWNER_LABEL}=1"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return set(out.split())


def refuses_wrong_base(base_image: str, base_id: str, sha: str) -> bool:
    """The same tag pinned to another ID: the proxy must refuse with 503 and build nothing."""
    wrong = "sha256:" + ("1" if base_id.endswith("0" * 64) else "0") * 64
    before = owned_images()
    cfg, proxy = start_proxy(base_image, wrong)
    try:
        status, doc = build(cfg["listen"], context((BUILD / "original" / f"{sha}.Dockerfile").read_bytes()))
    finally:
        proxy.shutdown()
    ok = status == 503 and owned_images() == before
    row = {"shape": sha[:12], "check": "wrong base id", "status": status, "message": doc.get("message"), "ok": ok}
    print(json.dumps(row))
    return ok


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
