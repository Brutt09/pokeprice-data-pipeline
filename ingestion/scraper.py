"""Polite browser-automation scraper (Playwright).

Rules enforced in code, not just in docs:
  * a site is scraped only if it is in ALLOWED_SITES, i.e. its terms/licence
    were reviewed and recorded here;
  * robots.txt is checked for every URL, and its Crawl-delay is honoured;
  * requests identify the project in the User-Agent;
  * every page is snapshotted (HTML file + hash in raw_ingestions) before parsing,
    so parsers can be re-run and tested without hitting the site again.
"""
import hashlib
import logging
import time
import urllib.robotparser
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

from common.config import RAW_DIR
from ingestion.http import save_raw

log = logging.getLogger(__name__)

USER_AGENT = "pokeprice-pipeline/0.1 (portfolio data-engineering project; +https://github.com/)"

ALLOWED_SITES = {
    "bulbapedia.bulbagarden.net": {
        "licence": "CC BY-NC-SA 2.5 (page footer)",
        "reviewed_on": "2026-10-19",
        "notes": "robots.txt allows /wiki/ with Crawl-delay 5. Non-commercial use with attribution.",
    },
}


class ScrapeNotAllowed(RuntimeError):
    pass


class PoliteScraper:
    def __init__(self, source: str, min_delay: float = 5.0):
        self.source = source
        self.min_delay = min_delay
        self._robots: dict[str, urllib.robotparser.RobotFileParser] = {}
        self._last_request: dict[str, float] = {}
        self._pw = self._browser = self._page = None

    # --- compliance ---------------------------------------------------------
    def _robots_for(self, host: str) -> urllib.robotparser.RobotFileParser:
        if host not in self._robots:
            rp = urllib.robotparser.RobotFileParser()
            resp = httpx.get(f"https://{host}/robots.txt", headers={"User-Agent": USER_AGENT},
                             timeout=20, follow_redirects=True)
            rp.parse(resp.text.splitlines() if resp.status_code == 200 else [])
            self._robots[host] = rp
        return self._robots[host]

    def check_allowed(self, url: str) -> float:
        """Raise unless the URL may be fetched. Returns the delay to respect."""
        host = urlparse(url).netloc
        if host not in ALLOWED_SITES:
            raise ScrapeNotAllowed(f"{host} has no reviewed terms in ALLOWED_SITES")
        rp = self._robots_for(host)
        if not rp.can_fetch(USER_AGENT, url):
            raise ScrapeNotAllowed(f"robots.txt disallows {url}")
        return max(self.min_delay, float(rp.crawl_delay(USER_AGENT) or 0))

    def _wait(self, host: str, delay: float) -> None:
        elapsed = time.monotonic() - self._last_request.get(host, 0)
        if elapsed < delay:
            time.sleep(delay - elapsed)
        self._last_request[host] = time.monotonic()

    # --- browser ------------------------------------------------------------
    def __enter__(self):
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch()
        self._page = self._browser.new_page(user_agent=USER_AGENT)
        return self

    def __exit__(self, *exc):
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()

    def fetch(self, url: str, retries: int = 2):
        """Navigate to a URL and return the Playwright page, after snapshotting its HTML."""
        delay = self.check_allowed(url)
        host = urlparse(url).netloc
        for attempt in range(retries + 1):
            self._wait(host, delay)
            try:
                resp = self._page.goto(url, wait_until="domcontentloaded", timeout=45000)
                if resp is None or resp.status >= 400:
                    raise RuntimeError(f"HTTP {resp.status if resp else '?'}")
                break
            except Exception as exc:   # network / timeout / 5xx
                if attempt == retries:
                    raise
                log.warning("retry %s for %s: %s", attempt + 1, url, exc)
                delay *= 2
        html = self._page.content()
        digest = hashlib.sha256(html.encode()).hexdigest()
        out = RAW_DIR / self.source / datetime.now(timezone.utc).strftime("%Y%m%d")
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{digest[:16]}.html").write_text(html, encoding="utf-8")
        save_raw(self.source, url, {"url": url, "html_sha256": digest, "bytes": len(html),
                                    "licence": ALLOWED_SITES[host]["licence"]})
        return self._page
