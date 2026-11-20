"""Optional headless browser (Playwright) used only while learning/repairing JS apps:
render the page and capture the JSON API calls it makes. Regular reads never use it."""
from __future__ import annotations

import asyncio
import json
import logging

from .config import settings
from .fetcher import limiter

log = logging.getLogger("watchtower.browser")
_LOCK = asyncio.Lock()  # one browser session at a time (memory + politeness)


def available() -> bool:
    if not settings.use_browser:
        return False
    try:
        import playwright.async_api  # noqa: F401
        return True
    except ImportError:
        return False


async def capture(url: str, wait_ms: int = 4000, max_responses: int = 25) -> dict:
    """Return {"html": rendered_html, "final_url": str, "json": [ {url, method, postData,
    status, body} ]}. Raises RuntimeError if the browser is unavailable."""
    if not available():
        raise RuntimeError("Browser not available")
    from playwright.async_api import async_playwright
    found: list[dict] = []
    async with _LOCK:
        lock = await limiter.wait(url)  # count the session as a request to this host
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
                try:
                    ctx = await browser.new_context(user_agent=settings.user_agent,
                                                    locale="de-DE")
                    page = await ctx.new_page()

                    async def block(route):
                        if route.request.resource_type in ("image", "font", "media",
                                                           "stylesheet"):
                            await route.abort()
                        else:
                            await route.continue_()
                    await page.route("**/*", block)

                    async def on_response(resp):
                        try:
                            ct = (resp.headers or {}).get("content-type", "")
                            if "json" not in ct or len(found) >= max_responses:
                                return
                            body = await resp.text()
                            data = json.loads(body)
                            req = resp.request
                            found.append({"url": resp.url, "method": req.method,
                                          "postData": req.post_data, "status": resp.status,
                                          "body": data})
                        except Exception:  # noqa: BLE001
                            pass
                    page.on("response", on_response)
                    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=12000)
                    except Exception:  # noqa: BLE001
                        pass
                    await page.wait_for_timeout(wait_ms)
                    html = await page.content()
                    final_url = page.url
                finally:
                    await browser.close()
        finally:
            limiter.done(url, lock)
    return {"html": html, "final_url": final_url, "json": found}
