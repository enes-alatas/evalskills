import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))

import server  # noqa: E402


@pytest.fixture
def app(tmp_path, monkeypatch):
    """The real HTTP handler on an ephemeral localhost port, with a fresh database and no worker thread."""
    monkeypatch.setattr(server, "DB_PATH", tmp_path / "inventory.db")
    server.init_db()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.H)
    t = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    yield httpd.server_address
    httpd.shutdown()
    httpd.server_close()
