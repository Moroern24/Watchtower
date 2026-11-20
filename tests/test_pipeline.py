import asyncio
import json

import httpx
import pytest

from tests import recipes as R
from watchtower import validate
from watchtower.config import settings
from watchtower.db import DB
from watchtower.fetcher import Fetcher
from watchtower.pipeline import commit, execute, public_tender
from watchtower.recipe import parse_recipe


def run(coro):
    return asyncio.run(coro)


async def _read(db, recipe_dict, pid="p1", previous=None):
    recipe = parse_recipe(recipe_dict)
    steps = []
    async with Fetcher() as f:
        res = await execute(db, pid, recipe, f, steps, 100, previous=previous)
    return recipe, res, steps


@pytest.fixture
def db():
    return DB(":memory:")


def test_ssr_full_read(mock_base, db):
    recipe, res, steps = run(_read(db, R.ssr_v1(mock_base)))
    assert len(res.tenders) == 12, steps
    t = next(x for x in res.tenders if x["id"] == "VG-2026-1000")
    assert t["title"] == "Neubau Grundschule – Rohbauarbeiten"
    assert t["buyerName"] == "Stadt Musterstadt"
    assert t["url"].endswith("/ssr/tender/1000")
    assert t["submissionDeadline"][-6:] in ("+01:00", "+02:00")
    assert t["cpvCodes"] == ["45214200"]
    assert t["procedureType"] == "restricted"
    assert t["contractNature"] == "works"
    assert t["placeOfPerformance"] == {"postalCode": "12345", "city": "Musterstadt",
                                       "countryCode": "DE"}
    names = sorted(d["fileName"] for d in t["documents"])
    # ZIP is unpacked: inner files instead of the zip
    assert names == ["Bekanntmachung.pdf", "Formblaetter/Angebotsschreiben.docx",
                     "Leistungsverzeichnis.pdf"]
    for d in t["documents"]:
        assert (settings.files_dir / d["sha256"]).exists()
    ok, problems, _ = validate.check(res.tenders, None, 0, True)
    assert ok, problems
    stats = commit(db, "p1", recipe, res)
    assert stats["new"] == 12
    stored = {x["id"]: x for x in db.tender_payloads("p1")}
    assert stored["VG-2026-1005"]["status"] == "withdrawn"
    assert stored["VG-2026-1000"]["status"] == "open"
    pub = public_tender(stored["VG-2026-1000"], stored["VG-2026-1000"]["lastChangedAt"])
    assert not any(k.startswith("_") for k in pub)


def test_incremental_and_unlisted_closed(mock_base, db):
    recipe, res, _ = run(_read(db, R.ssr_v1(mock_base)))
    commit(db, "p1", recipe, res)
    before = {k: v["last_changed_at"] for k, v in db.get_tenders("p1").items()}
    httpx.get(mock_base + "/control/hide", params={"id": "VG-2026-1003"})
    recipe, res2, steps = run(_read(db, R.ssr_v1(mock_base), previous=db.get_tenders("p1")))
    assert any("reused" in s for s in steps)
    stats = commit(db, "p1", recipe, res2)
    assert stats["closedUnlisted"] == 1 and stats["changed"] == 0
    rows = db.get_tenders("p1")
    assert json.loads(rows["VG-2026-1003"]["data"])["status"] == "closed"
    assert rows["VG-2026-1000"]["last_changed_at"] == before["VG-2026-1000"]


def test_layout_switch_is_noticed(mock_base, db):
    recipe, res, _ = run(_read(db, R.ssr_v1(mock_base)))
    prof = validate.profile(res.tenders)
    httpx.get(mock_base + "/control/layout", params={"v": 2})
    recipe, res2, _ = run(_read(db, R.ssr_v1(mock_base)))
    ok, problems, _ = validate.check(res2.tenders, prof, 12, False)
    assert not ok and problems
    recipe, res3, _ = run(_read(db, R.ssr_v2(mock_base)))
    ok, problems, _ = validate.check(res3.tenders, prof, 12, False)
    assert ok, problems
    assert len(res3.tenders) == 12


def test_spa_json(mock_base, db):
    recipe, res, steps = run(_read(db, R.spa(mock_base)))
    assert len(res.tenders) == 12, steps
    t = next(x for x in res.tenders if x["id"] == "SPA-2026-1001")
    assert t["cpvCodes"] == ["38000000"]
    assert t["procedureType"] == "open"
    assert t["submissionDeadline"].endswith(("+01:00", "+02:00"))
    assert len(t["documents"]) == 2
    assert "_num" not in public_tender(t, "x")
    commit(db, "p1", recipe, res)
    assert {x["id"]: x for x in db.tender_payloads("p1")}["SPA-2026-1005"]["status"] == "withdrawn"


def test_multistep_documents(mock_base, db):
    recipe, res, steps = run(_read(db, R.multi(mock_base)))
    assert len(res.tenders) == 7
    t = res.tenders[0]
    assert sorted(d["fileName"] for d in t["documents"]) == ["CCAP.pdf", "RC.pdf"], steps
    assert t["submissionDeadline"].endswith(("+01:00", "+02:00"))
    assert t["procedureType"] == "other"  # "procédure adaptée" (MAPA) is not the EU open one


def test_captcha_page_docs_ok(mock_base, db):
    recipe, res, _ = run(_read(db, R.simple(mock_base, "captcha")))
    assert len(res.tenders) == 6
    assert all(t["documents"] for t in res.tenders)
    assert not res.doc_blocked
    # M2: the list has no identifier, the detail page has "Kennung" -> real portal ids
    assert sorted(t["id"] for t in res.tenders)[0] == "CP-2026-1000"
    assert not any(t.get("_syntheticId") for t in res.tenders)
    commit(db, "p1", recipe, res)
    recipe, res2, steps = run(_read(db, R.simple(mock_base, "captcha"),
                                    previous=db.get_tenders("p1")))
    assert {t["id"] for t in res2.tenders} == {t["id"] for t in res.tenders}
    assert any("reused" in s for s in steps)


def test_login_docs_blocked(mock_base, db):
    recipe, res, _ = run(_read(db, R.simple(mock_base, "login")))
    assert len(res.tenders) == 6
    assert res.doc_blocked.get("login") == 6


def test_header_login_box_does_not_block_public_docs(mock_base, db):
    """C2: a download page with a login form + captcha widget in its chrome still has files."""
    from watchtower.documents import fetch_documents

    async def go():
        async with Fetcher() as f:
            return await fetch_documents(db, f, [(f"{mock_base}/captcha/docpage/1001", "")])
    res = run(go())
    assert res.blocked is None
    assert [e["fileName"] for e in res.entries] == ["Leistungsbeschreibung.pdf"]


def test_redirect_to_login_is_blocked_and_403_is_not_login(mock_base, db):
    from watchtower.documents import fetch_documents

    async def go(url):
        async with Fetcher() as f:
            return await fetch_documents(db, f, [(url, "")], use_cache=False)
    assert run(go(f"{mock_base}/login/doc/1.pdf")).blocked == "login"
    assert run(go(f"{mock_base}/forbidden/doc/1.pdf")).blocked == "forbidden"   # M6


def test_zip_limits_are_reported(tmp_path, db, monkeypatch):
    """M5: oversized members and the archive size guard are counted, not silent."""
    import zipfile as zf
    from watchtower import documents
    monkeypatch.setattr(settings, "max_file_bytes", 1000)
    z = tmp_path / "a.zip"
    with zf.ZipFile(z, "w") as w:
        w.writestr("lot1/LV.pdf", b"x" * 100)
        w.writestr("lot2/LV.pdf", b"y" * 100)
        w.writestr("big.bin", b"z" * 5000)
    entries, skipped, truncated = documents._unzip(db, z)
    assert sorted(e["fileName"] for e in entries) == ["lot1/LV.pdf", "lot2/LV.pdf"]  # m6
    assert skipped == 1 and truncated is False


def test_blocker_heuristics():
    from watchtower.blockers import classify_block, is_login_url
    assert is_login_url("https://x.de/login?next=/a") and not is_login_url("https://x.de/blog")
    page = '<footer><a href="/captcha-policy">Captcha policy</a></footer>'
    assert classify_block(page) is None  # a word in a link is not a captcha
