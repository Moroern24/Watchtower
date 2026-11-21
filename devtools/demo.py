"""DEV ONLY (not in the Docker image): the real app, but with the test suite's scripted learner
instead of a live model, so the dashboard can be explored without an API key.

    python -m uvicorn mockportals.server:app --port 8090
    python -m uvicorn devtools.demo:app --port 8080
"""
import os

os.environ.setdefault("DATA_DIR", "./data-demo")
os.environ.setdefault("MIN_REQUEST_INTERVAL_SEC", "0.2")

from tests.test_api import FakeLearner  # noqa: E402
from watchtower.db import DB  # noqa: E402
from watchtower.config import settings  # noqa: E402
from watchtower.main import create_app  # noqa: E402
from watchtower.orchestrator import Orchestrator  # noqa: E402

app = create_app(DB(settings.db_path),
                 lambda d: Orchestrator(d, FakeLearner, llm_available=lambda: True))
