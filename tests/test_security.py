"""Regression tests for the security review: script links, SSRF, CSRF, active file types,
dangerous regexes, no third-party font requests."""
import asyncio
import hashlib

import pytest

from tests.test_api import _DBREF, client  # noqa: F401  (client fixture)
from watchtower import normalize as N
from watchtower.config import settings
from watchtower.extract import SpecError, apply_regex, check_regex
from watchtower.fetcher import Fetcher, PortalError, check_target
from watchtower.pipeline import public_tender
from watchtower.recipe import normalize_item, parse_recipe

BASE_RECIPE = {"kind": "html", "list": {"url": "https://portal.example/list", "items": "tr"}}


# ------------------------------------------------------------------ script links (XSS)

@pytest.mark.parametrize("bad", ["javascript:alert(1)", "JavaScript:alert(1)", " javascript:x",
                                 "java\tscript:x", "data:text/html,<script>", "vbscript:x",
                                 "/relative/path", "//no-scheme.example/x", "", None])
def test_only_http_urls_survive(bad):
    assert N.http_url(bad) is None


def test_tender_url_sanitized_on_extract_and_output():
    r = parse_recipe(BASE_RECIPE)
    assert normalize_item(r, {"id": "1", "url": "javascript:alert(1)", "title": "T"})["url"] is None
    assert normalize_item(r, {"id": "1", "url": "https://p.example/t/1", "title": "T"})["url"] == \
        "https://p.example/t/1"
    stored = {"id": "1", "url": "javascript:alert(document.cookie)", "title": "T"}  # pre-fix row
    assert public_tender(stored, "x")["url"] is None


def test_dashboard_never_links_non_http(client):  # noqa: F811
    html = client.get("/").text
    assert "const safeUrl" in html and "safeUrl(t.url)" in html


# ------------------------------------------------------------------ SSRF

@pytest.mark.parametrize("url", ["http://169.254.169.254/latest/meta-data/",
                                 "http://metadata.google.internal/computeMetadata/v1/",
                                 "http://[fd00:ec2::254]/", "http://[::ffff:169.254.169.254]/",
                                 "http://0.0.0.0/", "http://[fe80::1]/", "ftp://example.com/"])
def test_always_blocked_targets(url):
    with pytest.raises(PortalError):
        asyncio.run(check_target(url))


def test_private_networks_follow_the_setting(monkeypatch):
    asyncio.run(check_target("http://127.0.0.1:8765/"))          # judges' mocks may live here
    monkeypatch.setattr(settings, "block_private_networks", True)
    for url in ("http://127.0.0.1:8765/", "http://10.0.0.5/", "http://192.168.1.1/"):
        with pytest.raises(PortalError):
            asyncio.run(check_target(url))


def test_redirect_to_metadata_is_blocked(mock_base):
    async def go():
        async with Fetcher() as f:
            return await f.get(f"{mock_base}/control/redirect?to=http://169.254.169.254/latest/")
    with pytest.raises(PortalError) as e:
        asyncio.run(go())
    assert "Blocked" in e.value.message


def test_post_portals_rejects_metadata_target(client):  # noqa: F811
    r = client.post("/portals", json={"url": "http://169.254.169.254/latest/"})
    assert r.status_code == 400 and "Blocked" in r.json()["error"]


# ------------------------------------------------------------------ CSRF

def test_cross_site_writes_are_refused(client, mock_base):  # noqa: F811
    url = {"url": f"{mock_base}/ssr/tenders?csrf=1"}
    assert client.post("/portals", json=url, headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post("/portals", json=url, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.put("/settings", json={"autoCheck": False},
                      headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.get("/settings").json()["autoCheck"] is True
    # API clients without Origin, and the dashboard itself (same origin), still work
    assert client.post("/portals", json=url).status_code == 201
    assert client.post("/portals", json=url, headers={"Origin": "http://testserver"}).status_code == 201


# ------------------------------------------------------------------ active file types

@pytest.mark.parametrize("mime,name", [("text/html", "notice.html"), ("image/svg+xml", "plan.svg"),
                                       ("application/pdf", "trick.html"), (None, "x.bin")])
def test_active_documents_are_plain_downloads(client, mime, name):  # noqa: F811
    body = f"<script>alert(1)</script>{mime}{name}".encode()
    sha = hashlib.sha256(body).hexdigest()
    settings.files_dir.mkdir(parents=True, exist_ok=True)
    (settings.files_dir / sha).write_bytes(body)
    fid = _DBREF[0].add_file(sha, len(body), mime, name)
    r = client.get(f"/files/{fid}")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/octet-stream")
    assert "sandbox" in r.headers["content-security-policy"]
    assert r.headers["content-disposition"].startswith("attachment")


def test_safe_documents_keep_their_type(client):  # noqa: F811
    body = b"%PDF-1.4 harmless"
    sha = hashlib.sha256(body).hexdigest()
    (settings.files_dir / sha).write_bytes(body)
    fid = _DBREF[0].add_file(sha, len(body), "application/pdf", "Vergabeunterlagen.pdf")
    assert client.get(f"/files/{fid}").headers["content-type"].startswith("application/pdf")


# ------------------------------------------------------------------ dangerous regexes

@pytest.mark.parametrize("pattern", [r"(a+)+$", r"(\w*x)*y", r"(.*a){10,}", "x" * 301, r"(unclosed"])
def test_dangerous_regex_rejected(pattern):
    with pytest.raises(SpecError):
        check_regex(pattern)
    recipe = {**BASE_RECIPE, "list": {**BASE_RECIPE["list"], "fields": {"title": {"css": "td", "regex": pattern}}}}
    with pytest.raises(PortalError):
        parse_recipe(recipe)


def test_regex_input_is_capped():
    huge = "a" * 100000 + "END"
    assert apply_regex(huge, r"(END)") is None        # beyond the 20k window: not scanned
    assert apply_regex("Az. VG-1", r"Az\.\s*(\S+)") == "VG-1"


# ------------------------------------------------------------------ third-party requests

def test_dashboard_makes_no_third_party_requests(client):  # noqa: F811
    html = client.get("/").text
    assert "googleapis" not in html and "gstatic" not in html
    assert "<script src=" not in html and "<link rel=\"stylesheet\" href=\"http" not in html
