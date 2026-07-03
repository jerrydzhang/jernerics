import socket
import threading
import time

import pytest
import uvicorn
from jernerics_server.http import create_app
from jernerics_server.store import Store


def _random_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def http_server(tmp_path):
    """Function-scoped HTTP server on a random port with a fresh SQLite DB."""
    port = _random_port()
    db_path = tmp_path / "test.sqlite"
    artifacts_root = tmp_path / "artifacts"
    store = Store(db_path)
    app = create_app(store, artifacts_root=artifacts_root)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", db_path
    server.should_exit = True
    t.join(timeout=5)
