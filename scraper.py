"""
scraper.py
Fetches a Flipkart product page and extracts price + Bajaj Finserv EMI
availability.

IMPORTANT: Flipkart does not offer a public product API. This scrapes
the HTML directly, which means:
  - It WILL occasionally break when Flipkart changes their page layout.
  - Flipkart may show a captcha/block page to server IPs (like Render's).
    If that happens consistently, you'll need to route requests through
    a rotating proxy/residential proxy service. Test this first before
    relying on it.

Selectors below are current as of testing patterns commonly seen on
Flipkart product pages (mid-2025/2026 layout) but ARE NOT GUARANTEED
to be exact — inspect a live page and adjust the CSS selectors below
if extraction starts failing.
"""

import re
import cloudscraper
import requests
from bs4 import BeautifulSoup

# Reused across calls so cookies persist between requests, mimicking a
# real browser session. cloudscraper wraps requests and automatically
# handles Cloudflare/Akamai-style JS anti-bot challenges — pure Python,
# no native compilation needed, so it installs cleanly on Termux/Android
# as well as on Render.
#
# IMPORTANT: don't add custom headers on top of cloudscraper's own
# browser-impersonation headers — testing showed Flipkart serves a
# stripped-down/blocked page (no price data at all) when our headers
# and cloudscraper's internal ones don't match exactly. Trust
# cloudscraper's defaults alone.
def _new_session():
    return cloudscraper.create_scraper(browser={"browser": "chrome", "platform": "windows", "mobile": False})


_session = _new_session()


class ScrapeError(Exception):
    pass


def fetch_product(url: str, _retrying: bool = False) -> dict:
    """
    Returns {"title": str, "price": float, "bajaj_emi": bool}
    Raises ScrapeError if the page can't be parsed (blocked, layout
    changed, invalid URL, etc.)

    403s from Flipkart are often transient (shared hosting IPs get
    flagged inconsistently) — retries once with a fresh session before
    giving up.
    """
    global _session
    try:
        resp = _session.get(url, timeout=15, allow_redirects=True)
    except requests.RequestException as e:
        raise ScrapeError(f"Network error fetching page: {e}")

    if resp.status_code == 403:
        if not _retrying:
            _session = _new_session()  # fresh cookies/fingerprint, worth one retry
            return fetch_product(url, _retrying=True)
        raise ScrapeError(
            "Flipkart returned 403 (blocked this request) even after a "
            "retry. This is usually transient on shared hosting IPs — "
            "the next scheduled check will likely succeed. If it persists "
            "for hours, Flipkart may be blocking Render's IP range "
            "entirely, which would need a proxy service to fix."
        )
    if resp.status_code != 200:
        raise ScrapeError(f"Flipkart returned status {resp.status_code} (possibly blocked)")

    soup = BeautifulSoup(resp.text, "html.parser")

    title = _extract_title(soup)
    price = _extract_price(soup, resp.text)
    bajaj_emi = _extract_bajaj_emi(soup, resp.text)

    if price is None:
        raise ScrapeError(
            "Could not find price on page — Flipkart may have blocked "
            "the request or changed their page layout."
        )

    return {"title": title, "price": price, "bajaj_emi": bajaj_emi}


def _extract_title(soup) -> str:
    # Flipkart product title is usually in a span with class starting "VU-ZEz" or similar,
    # but those class names are auto-generated/change often. Fall back to <title> tag.
    h1 = soup.find("span", class_=re.compile("B_NuCI|VU-ZEz"))
    if h1:
        return h1.get_text(strip=True)
    if soup.title:
        return soup.title.get_text(strip=True).split("Price in India")[0].strip()
    return "Unknown product"


def _extract_price(soup, raw_html) -> float | None:
    # PRIMARY: Flipkart embeds structured JSON-LD product data with the
    # real selling price at "offers":{"price":N,...} — confirmed via
    # live inspection (2026-09-07) to be far more reliable than scanning
    # for ₹ symbols, which picks up bank offers, EMI totals, protection
    # plans, and similar/related product carousels that appear before
    # the real price in the raw HTML.
    match = re.search(r'"offers"\s*:\s*\{\s*"price"\s*:\s*([\d.]+)', raw_html)
    if match:
        return float(match.group(1))

    # FALLBACK 1: common Flipkart price container classes (auto-generated,
    # changes often — may not match on a given page).
    price_tag = soup.find("div", class_=re.compile("Nx9bqj|_30jeq3"))
    if price_tag:
        return _parse_price_text(price_tag.get_text())

    # FALLBACK 2 (least reliable): first ₹ amount anywhere on the page.
    match = re.search(r"₹\s?([\d,]+)", raw_html)
    if match:
        return _parse_price_text(match.group(0))

    return None


def _parse_price_text(text: str) -> float | None:
    digits = re.sub(r"[^\d]", "", text)
    return float(digits) if digits else None


def _extract_bajaj_emi(soup, raw_html) -> bool | None:
    """
    CONFIRMED (2026-08-30, via live traffic inspection): Flipkart loads
    Bajaj EMI availability through a client-side JS API call, not the
    static HTML this scraper fetches. A text-search here always returns
    False regardless of true availability — which is worse than not
    reporting it at all, since it looks like a real "not available"
    result. Returns None (unknown) rather than a false negative.

    If you want this automated later, the fix is finding Flipkart's
    internal EMI/offers API endpoint (via a proxied traffic capture with
    a trusted MITM cert) and calling it directly — not scraping HTML.
    Until then, check Bajaj EMI manually on the product page when a
    price alert fires.
    """
    return None
