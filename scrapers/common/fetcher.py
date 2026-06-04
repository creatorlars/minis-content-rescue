"""Transport abstraction. Merges the two ways the old scripts fetched bytes:

  * UrllibFetcher    — plain urllib (Solegends, a static 1990s HTML site).
  * PlaywrightFetcher — a reused headless-Chromium session (Lost Minis, whose
                        Sucuri WAF blocks plain urllib).

Both expose the same `fetch_bytes(url, timeout) -> bytes` / `close()` interface so
the engine never cares which one a site uses. Retry/backoff is layered on top in
the engine via retry.py, not here — one attempt per call.
"""
from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.request import Request, urlopen

if TYPE_CHECKING:
    from playwright.sync_api import Browser, BrowserContext, Page, Playwright


class Fetcher:
    def fetch_bytes(self, url: str, timeout: int = 60) -> bytes:
        raise NotImplementedError

    def close(self) -> None:
        pass


class UrllibFetcher(Fetcher):
    def __init__(self, user_agent: str) -> None:
        self.user_agent = user_agent

    def fetch_bytes(self, url: str, timeout: int = 60) -> bytes:
        req = Request(url, headers={"User-Agent": self.user_agent})
        with urlopen(req, timeout=timeout) as resp:
            return resp.read()


class PlaywrightFetcher(Fetcher):
    """Lazy, reused Chromium page. A real browser session is required to get past
    the WAF; spinning one up per request would be far too slow."""

    def __init__(self, user_agent: str) -> None:
        self.user_agent = user_agent
        self._playwright: "Playwright | None" = None
        self._browser: "Browser | None" = None
        self._context: "BrowserContext | None" = None
        self._page: "Page | None" = None

    def _ensure(self) -> "Page":
        if self._page is not None:
            return self._page
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=True)
        self._context = self._browser.new_context(user_agent=self.user_agent)
        self._page = self._context.new_page()
        return self._page

    def fetch_bytes(self, url: str, timeout: int = 60) -> bytes:
        page = self._ensure()
        response = page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
        if response is None:
            raise OSError(f"no response for {url}")
        if response.status >= 400:
            # Encode the status in the message so retry.classify() can read it.
            raise OSError(f"HTTP {response.status} for {url}")
        return page.content().encode("utf-8")

    def close(self) -> None:
        for obj in (self._page, self._context, self._browser):
            if obj is not None:
                try:
                    obj.close()
                except Exception:
                    pass
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
        self._page = self._context = self._browser = self._playwright = None
