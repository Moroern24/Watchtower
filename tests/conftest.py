import os
import socket
import tempfile
import threading
import time

os.environ.setdefault("MIN_REQUEST_INTERVAL_SEC", "0.01")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="wt_test_"))
os.environ.setdefault("SCHEDULER_TICK_SEC", "0.5")

import httpx  # noqa: E402
import pytest  # noqa: E402
import uvicorn  # noqa: E402

MOCK_PORT = int(os.environ.get("MOCK_PORT", "8765"))
MOCK = f"http://127.0.0.1:{MOCK_PORT}"


def _port_open(port):
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


@pytest.fixture(scope="session")
def mock_base():
    if not _port_open(MOCK_PORT):
        from mockportals.server import app
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=MOCK_PORT,
                                               log_level="warning"))
        t = threading.Thread(target=server.run, daemon=True)
        t.start()
        for _ in range(100):
            if _port_open(MOCK_PORT):
                break
            time.sleep(0.05)
    yield MOCK
    httpx.get(MOCK + "/control/reset")


@pytest.fixture(autouse=True)
def _reset_mock(request):
    if "mock_base" in request.fixturenames:
        try:
            httpx.get(MOCK + "/control/reset")
        except httpx.HTTPError:
            pass
    yield
