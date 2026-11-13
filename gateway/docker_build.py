"""Trial image builds for the Docker socket proxy (#137, PR B): the proxy, not the client, writes the build.

The eval runner has no Docker access of its own; it sends a trial's build context (an uncompressed tar) to the
proxy's ``POST /_evalskills/build``, and gets back only an image tag. The host operator's conditions (#137):
- **No rewriting.** ``allowed-dockerfiles.txt`` maps each allowed original Dockerfile (by sha256, as SkillEvaluator's
  pinned template generates it) to a committed final Dockerfile: ``FROM`` the trial base image, then the template's
  fixed steps. The proxy adds that final file to the context under its own name (``FINAL_NAME``) and builds it with
  ``dockerfile=``; a context that already holds that name is refused.
- **Read, never unpack.** The context is read as a tar stream, never extracted. Its size and file count are capped,
  the top-level ``Dockerfile`` must be a single regular file, and members may not be absolute, contain ``..``, or be
  devices or FIFOs. Links stay link entries in the re-written tar; the daemon jails them to the context.
- **The proxy writes the request.** Docker's legacy builder (``version=1``, so no BuildKit ``/session`` or
  ``/grpc``), ``networkmode=none``, ``pull=0``, the owner label, a tag the proxy picks, ``rm``/``forcerm``, and
  nothing from the client. If the daemon no longer has the legacy builder, the build fails: there is no fallback.
- **The base image must already be on the host** with the pinned image ID. ``pull=0`` alone doesn't stop the
  builder from pulling a missing ``FROM`` image, so the proxy checks the base by ID before every build.
Standard library only. The proxy (gateway/docker_proxy.py) does the HTTP side and calls ``build``.
"""

from __future__ import annotations

import hashlib
import http.client
import io
import json
import os
import posixpath
import re
import socket
import tarfile
import tempfile
import uuid
from pathlib import Path
from urllib.parse import quote, urlencode

FINAL_NAME = ".evalskills-final.Dockerfile"
TAG_REPO = "evalskills-trial"
OWNER_LABEL = "io.evalskills.owned"
MAX_DOCKERFILE = 256 * 1024
MAX_BUILD_LOG = 4 * 1024 * 1024
SHA256 = re.compile(r"[0-9a-f]{64}")


class BuildError(Exception):
    """A build the proxy won't run, or one that failed; the message is safe to return and log."""

    def __init__(self, why: str, code: int = 403):
        super().__init__(why)
        self.code = code


def load_allowlist(path: Path, base_image: str) -> dict[str, bytes]:
    """original sha256 → final Dockerfile bytes. Every final file must start ``FROM <base_image>``."""
    out = {}
    for n, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 2 or not SHA256.fullmatch(parts[0]) or parts[0] in out:
            raise ValueError(f"{path.name}:{n}: expected '<sha256> <final Dockerfile>', once per sha256")
        final = (path.parent / parts[1]).resolve()
        if path.parent.resolve() not in final.parents:
            raise ValueError(f"{path.name}:{n}: the final Dockerfile must be next to the list")
        data = final.read_bytes()
        if not data.startswith(f"FROM {base_image}\n".encode()):
            raise ValueError(f"{path.name}:{n}: the final Dockerfile must start with FROM {base_image}")
        out[parts[0]] = data
    if not out:
        raise ValueError(f"{path.name} lists no Dockerfile")
    return out


def _member_name(m: tarfile.TarInfo) -> str:
    name = m.name
    while name.startswith("./"):
        name = name[2:]
    name = name.rstrip("/")
    if not name or name.startswith("/") or ".." in name.split("/") or "\\" in name:
        raise BuildError("build context has an unsafe path")
    return posixpath.normpath(name)


def _link_target(linkname: str) -> str | None:
    """A hard link's target as a context path, or None if it is absolute or climbs out."""
    try:
        return _member_name(tarfile.TarInfo(linkname))
    except BuildError:
        return None


def prepare_context(src, out, allowlist: dict[str, bytes], *, max_files: int) -> str:
    """Read the client's tar from ``src``, write the tar to build to ``out``. → the original Dockerfile's sha256."""
    try:
        tin = tarfile.open(fileobj=src, mode="r|")  # a stream: members are read in order, never extracted
    except tarfile.TarError as e:
        raise BuildError("build context must be an uncompressed tar", 400) from e
    original = None
    with tin, tarfile.open(fileobj=out, mode="w|", format=tarfile.PAX_FORMAT) as tout:
        count, files_seen = 0, set()
        for m in tin:
            count += 1
            if count > max_files:
                raise BuildError(f"build context has more than {max_files} files", 413)
            name = _member_name(m)
            if name == FINAL_NAME:
                raise BuildError(f"build context may not contain {FINAL_NAME}")
            if not (m.isreg() or m.isdir() or m.issym() or m.islnk()):
                raise BuildError("build context may only hold files, folders and links")
            if m.islnk() and _link_target(m.linkname) not in files_seen:
                raise BuildError("a hard link must point back to an earlier file in the context")
            if m.isreg():
                files_seen.add(name)
            if name == "Dockerfile":
                if original is not None or not m.isreg() or m.size > MAX_DOCKERFILE:
                    raise BuildError("the top-level Dockerfile must be one regular file")
                original = tin.extractfile(m).read()
                tout.addfile(m, io.BytesIO(original))
                continue
            tout.addfile(m, tin.extractfile(m) if m.isreg() else None)
        if original is None:
            raise BuildError("build context has no top-level Dockerfile")
        sha = hashlib.sha256(original).hexdigest()
        final = allowlist.get(sha)
        if final is None:
            raise BuildError("the Dockerfile is not on the allowlist (unsupported: custom environment or dependencies)")
        info = tarfile.TarInfo(FINAL_NAME)
        info.size, info.mode = len(final), 0o644
        tout.addfile(info, io.BytesIO(final))
    return sha


class _Unix(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._path)


def _get_json(upstream: str, url: str):
    conn = _Unix(upstream, 30)
    try:
        conn.request("GET", url, headers={"Host": "docker"})
        resp = conn.getresponse()
        body = resp.read(4 * 1024 * 1024)
        return resp.status, (json.loads(body) if resp.status == 200 else None)
    except (OSError, ValueError) as e:
        raise BuildError(f"the proxy's check failed ({type(e).__name__})", 502) from e
    finally:
        conn.close()


def build_query(tag: str) -> str:
    return urlencode(
        {
            "version": "1",  # the legacy builder: no BuildKit session; never a fallback
            "dockerfile": FINAL_NAME,
            "t": tag,
            "networkmode": "none",
            "pull": "0",
            "rm": "1",
            "forcerm": "1",
            "q": "1",
            "labels": json.dumps({OWNER_LABEL: "1"}),
        }
    )


def build(context_path: Path, *, upstream: str, base_image: str, base_image_id: str, timeout: float) -> dict:
    """Build the prepared context on the daemon. → {"tag", "id"}"""
    status, doc = _get_json(upstream, f"/images/{quote(base_image, safe=':/')}/json")
    if status != 200 or not doc or doc.get("Id") != base_image_id:
        raise BuildError("the trial base image is missing or isn't the pinned one; load it (deploy/gvisor/build)", 503)
    tag = f"{TAG_REPO}:{uuid.uuid4().hex[:16]}"
    conn = _Unix(upstream, timeout)
    try:
        with open(context_path, "rb") as f:
            conn.request(
                "POST",
                "/build?" + build_query(tag),
                body=f,
                headers={
                    "Host": "docker",
                    "Content-Type": "application/x-tar",
                    "Content-Length": str(os.fstat(f.fileno()).st_size),
                },
            )
        resp = conn.getresponse()
        log = resp.read(MAX_BUILD_LOG)
    except OSError as e:
        raise BuildError(f"the build request failed ({type(e).__name__})", 502) from e
    finally:
        conn.close()
    if resp.status != 200:
        raise BuildError(f"the daemon refused the build ({resp.status})", 502)
    image_id, error = None, None
    for line in log.splitlines():
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if msg.get("error") or msg.get("errorDetail"):
            error = str(msg.get("error") or msg.get("errorDetail", {}).get("message", "error"))[:300]
        aux = msg.get("aux") or {}
        if isinstance(aux, dict) and aux.get("ID"):
            image_id = aux["ID"]
        stream = str(msg.get("stream") or "")
        if image_id is None and stream.startswith("sha256:"):  # q=1 prints only the image ID
            image_id = stream.strip()
    if error or not image_id:
        raise BuildError(f"the build failed: {error or 'no image was produced'}", 502)
    status, doc = _get_json(upstream, f"/images/{tag}/json")
    if status != 200 or not doc or doc.get("Id") != image_id:
        raise BuildError("the built image can't be found under its tag", 502)
    if ((doc.get("Config") or {}).get("Labels") or {}).get(OWNER_LABEL) != "1":
        raise BuildError("the built image lacks the owner label", 502)
    return {"tag": tag, "id": image_id}


def spool(chunks, max_bytes: int):
    """Write the request body pieces to a private temp file, refusing past ``max_bytes``. → an open file at 0."""
    f = tempfile.TemporaryFile()
    size = 0
    for piece in chunks:
        size += len(piece)
        if size > max_bytes:
            f.close()
            raise BuildError(f"build context is larger than {max_bytes} bytes", 413)
        f.write(piece)
    f.seek(0)
    return f
