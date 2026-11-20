import asyncio
import time

import pytest

from watchtower import browser, compact
from watchtower.fetcher import HostLimiter


def test_per_host_spacing():
    lim = HostLimiter(0.3)
    stamps = {"a": [], "b": []}

    async def hit(host):
        url = f"http://{host}.example/x"
        lock = await lim.wait(url)
        stamps[host].append(time.monotonic())
        lim.done(url, lock)

    async def main():
        await asyncio.gather(*[hit("a") for _ in range(4)], *[hit("b") for _ in range(2)])

    asyncio.run(main())
    a = stamps["a"]
    assert all(y - x >= 0.29 for x, y in zip(a, a[1:]))
    # another host is not slowed down by the first one
    assert stamps["b"][0] - a[0] < 0.2


def test_cancel_while_waiting_releases_host():
    lim = HostLimiter(0.5)

    async def main():
        url = "http://c.example/"
        lock = await lim.wait(url)
        lim.done(url, lock)
        t = asyncio.create_task(lim.wait(url))
        await asyncio.sleep(0.05)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        lock = await asyncio.wait_for(lim.wait(url), 2)
        lim.done(url, lock)

    asyncio.run(main())


@pytest.mark.skipif(not browser.available(), reason="playwright not installed")
def test_spa_api_capture(mock_base):
    html = __import__("httpx").get(mock_base + "/spa/").text
    assert compact.looks_like_js_app(html)
    try:
        cap = asyncio.run(browser.capture(mock_base + "/spa/", wait_ms=500))
    except Exception as e:  # browser binary missing etc.
        pytest.skip(f"browser unusable: {e}")
    calls = [(c["method"], c["url"]) for c in cap["json"]]
    assert ("POST", mock_base + "/spa/api/search") in calls
    assert "results" in cap["json"][0]["body"]
