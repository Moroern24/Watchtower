"""API contract + state machine tests with a fake learner (no model calls)."""
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from tests import recipes as R
from watchtower.db import DB
from watchtower.fetcher import PortalError
from watchtower.main import create_app
from watchtower.orchestrator import Orchestrator, normalize_url

CALLS = {"learn": 0, "repair": 0}


class FakeLearner:
    def __init__(self, db, fetcher, llm, steps, max_items):
        self.steps = steps

    async def learn(self, url):
        CALLS["learn"] += 1
        base = url.split("/", 3)[0] + "//" + url.split("/", 3)[2]
        self.steps.append("fake learn")
        if "/ssr/" in url:
            return R.ssr_v1(base)
        if "/spa/" in url:
            return R.spa(base)
        if "/login/" in url:
            return R.simple(base, "login")
        if "/captcha/" in url:
            return R.simple(base, "captcha")
        raise PortalError("blocked", "Portal requires login: sign-in form")

    async def repair(self, url, old, problems, ids):
        CALLS["repair"] += 1
        base = url.split("/", 3)[0] + "//" + url.split("/", 3)[2]
        if any("count dropped" in p.lower() for p in problems):
            return old  # the honest model answer: nothing about the reading changed
        return R.ssr_v2(base)


@pytest.fixture
def client(mock_base):
    db = DB(":memory:")
    app = create_app(db, lambda d: Orchestrator(d, FakeLearner, llm_available=lambda: True))
    with TestClient(app) as c:
        yield c


def wait_state(c, pid, states, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        p = c.get(f"/portals/{pid}").json()
        if p["state"] in states and not c.get(f"/portals/{pid}?full=true").json()["activeRunId"]:
            return p
        time.sleep(0.2)
    raise AssertionError(f"state stuck: {c.get(f'/portals/{pid}').json()}")


def test_contract_and_attach(client, mock_base):
    r = client.post("/portals", json={"url": f"{mock_base}/ssr/tenders"})
    assert r.status_code == 201
    pid = r.json()["id"]
    assert client.post("/portals", json={"url": f"{mock_base}/ssr/tenders/"}).json()["id"] == pid
    assert client.get(f"/portals/{pid}").json()["state"] in ("integrating", "serving")
    p = wait_state(client, pid, {"serving", "failed"})
    assert p["state"] == "serving", p
    assert p["tenderCount"] == 12 and p["lastSuccessAt"]
    assert set(p) == {"id", "url", "state", "reason", "tenderCount", "lastSuccessAt"}
    tenders = client.get(f"/portals/{pid}/tenders").json()
    assert len(tenders) == 12
    t = tenders[0]
    for k in ("id", "url", "title", "buyerName", "publicationDate", "submissionDeadline",
              "status", "documents", "lastChangedAt"):
        assert k in t
    doc = t["documents"][0]
    body = client.get(doc["path"])
    assert body.status_code == 200 and len(body.content) == doc["sizeBytes"]
    runs = client.get(f"/portals/{pid}/runs").json()
    assert runs[-1]["kind"] == "read" and runs[-1]["result"] == "ok" and runs[-1]["finishedAt"]
    # changedSince
    later = client.get(f"/portals/{pid}/tenders",
                       params={"changedSince": t["lastChangedAt"]}).json()
    assert later == []
    assert client.get("/portals/nope").status_code == 404
    assert client.get("/portals/nope/tenders").status_code == 404
    assert client.post("/portals/nope/refresh").status_code == 404
    assert client.get("/").status_code == 200


def test_refresh_returns_running_run(client, mock_base):
    pid = client.post("/portals", json={"url": f"{mock_base}/spa/"}).json()["id"]
    r1 = client.post(f"/portals/{pid}/refresh")
    r2 = client.post(f"/portals/{pid}/refresh")
    assert r1.status_code == 202 and r1.json()["runId"] == r2.json()["runId"]
    wait_state(client, pid, {"serving", "failed"})
    r3 = client.post(f"/portals/{pid}/refresh").json()["runId"]
    assert r3 != r1.json()["runId"]
    wait_state(client, pid, {"serving"})


def test_layout_change_heals(client, mock_base):
    pid = client.post("/portals", json={"url": f"{mock_base}/ssr/tenders?x=heal"}).json()["id"]
    wait_state(client, pid, {"serving"})
    ids_before = {t["id"] for t in client.get(f"/portals/{pid}/tenders").json()}
    httpx.get(mock_base + "/control/layout", params={"v": 2})
    client.post(f"/portals/{pid}/refresh")
    # while healing the old tenders stay available
    p = wait_state(client, pid, {"serving", "failed"}, timeout=60)
    assert p["state"] == "serving", p
    runs = client.get(f"/portals/{pid}/runs").json()
    kinds = [(r["kind"], r["result"]) for r in runs]
    assert ("repair", "ok") in kinds and ("read", "failed") in kinds
    ids_after = {t["id"] for t in client.get(f"/portals/{pid}/tenders").json()}
    assert ids_before == ids_after  # identity preserved across the repair
    # portal switches back → earlier recipe version is reused without a model repair
    repairs = CALLS["repair"]
    httpx.get(mock_base + "/control/layout", params={"v": 1})
    client.post(f"/portals/{pid}/refresh")
    p = wait_state(client, pid, {"serving", "failed"}, timeout=60)
    assert p["state"] == "serving" and CALLS["repair"] == repairs
    assert any(r["reason"] and "earlier recipe" in r["reason"]
               for r in client.get(f"/portals/{pid}/runs").json())


def test_blocked_portal_failed(client, mock_base):
    pid = client.post("/portals", json={"url": f"{mock_base}/members-only"}).json()["id"]
    p = wait_state(client, pid, {"failed"})
    assert "login" in p["reason"].lower()


def test_docs_behind_login_reported(client, mock_base):
    pid = client.post("/portals", json={"url": f"{mock_base}/login/tenders"}).json()["id"]
    p = wait_state(client, pid, {"failed", "serving"})
    assert p["state"] == "failed" and "login" in p["reason"]


def test_captcha_but_docs_ok(client, mock_base):
    pid = client.post("/portals", json={"url": f"{mock_base}/captcha/tenders"}).json()["id"]
    p = wait_state(client, pid, {"failed", "serving"})
    assert p["state"] == "serving", p


def test_delete(client, mock_base):
    pid = client.post("/portals", json={"url": f"{mock_base}/ssr/tenders?del=1"}).json()["id"]
    assert client.delete(f"/portals/{pid}").status_code == 204
    assert client.get(f"/portals/{pid}").status_code == 404
    assert all(p["id"] != pid for p in client.get("/portals").json())


def test_normalize_url():
    assert normalize_url("HTTPS://Example.com:443/a/") == "https://example.com/a"
    assert normalize_url("example.com") == "https://example.com/"
    assert normalize_url("https://x.de/app#/list") == "https://x.de/app#/list"


def test_count_drop_does_not_auto_close(client, mock_base):
    """C1: a repair that returns the same recipe must not accept a sharp drop and close
    tenders; the portal is failed with an honest reason and keeps its open tenders."""
    pid = client.post("/portals", json={"url": f"{mock_base}/ssr/tenders?c1=1"}).json()["id"]
    wait_state(client, pid, {"serving"})
    for i in range(1000, 1008):
        httpx.get(mock_base + "/control/hide", params={"id": f"VG-2026-{i}"})
    client.post(f"/portals/{pid}/refresh")
    p = wait_state(client, pid, {"serving", "failed"}, timeout=60)
    assert p["state"] == "failed", p
    assert "not auto-closing" in p["reason"].lower()
    statuses = [t["status"] for t in client.get(f"/portals/{pid}/tenders").json()]
    assert statuses.count("open") == 11 and "closed" not in statuses
    recipes = client.get(f"/portals/{pid}/recipes").json()
    assert len(recipes) == 1  # m5: no duplicate recipe versions


def test_readd_after_delete_is_fresh(client, mock_base):
    """C3: delete removes everything; adding the URL again attaches from scratch."""
    url = f"{mock_base}/spa/?c3=1"
    pid = client.post("/portals", json={"url": url}).json()["id"]
    wait_state(client, pid, {"serving"})
    learns = CALLS["learn"]
    assert client.delete(f"/portals/{pid}").status_code == 204
    pid2 = client.post("/portals", json={"url": url}).json()["id"]
    assert pid2 != pid
    p = wait_state(client, pid2, {"serving"})
    assert CALLS["learn"] == learns + 1
    runs = client.get(f"/portals/{pid2}/runs").json()
    assert len(runs) == 1 and runs[0]["kind"] == "read"
    assert client.get(f"/portals/{pid}/runs").status_code == 404


def test_bad_urls_rejected(client):
    """M7"""
    for bad in ["javascript:alert(1)", "ftp://x.example/a", "http://", "not a url", ""]:
        assert client.post("/portals", json={"url": bad}).status_code == 400, bad
    assert client.post("/portals", json={}).status_code == 400


def test_file_download_has_name(client, mock_base):
    """m3"""
    pid = client.post("/portals", json={"url": f"{mock_base}/ssr/tenders?m3=1"}).json()["id"]
    wait_state(client, pid, {"serving"})
    doc = client.get(f"/portals/{pid}/tenders").json()[0]["documents"][0]
    r = client.get(doc["path"])
    assert "attachment" in r.headers["content-disposition"]
    assert doc["fileName"].rsplit("/", 1)[-1] in r.headers["content-disposition"]
    full = client.get(f"/portals/{pid}?full=true").json()
    assert full["openCount"] == 11
    assert full["recentRuns"][-1]["kind"] == "read" and full["recentRuns"][-1]["result"] == "ok"


def test_dashboard_served(client):
    r = client.get("/")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    html = r.text
    for needle in ('id="strip"', 'id="list"', 'id="detail"', 'data-tab="runs"', 'data-tab="tenders"',
                   "/portals?full=true",
                   "/refresh", "/runs?steps=true", "/tenders", 'method: "DELETE"'):
        assert needle in html, needle
    assert "<script src=" not in html  # no external bundle: one self-contained page
