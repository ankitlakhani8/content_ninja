"""Pricing page discovery and change detection.

We locate the pricing page (nav links or common paths), pull every price-like
string with a little context (the nearest plan heading), hash the normalised
page text and compare with the previous snapshot."""
from __future__ import annotations

import hashlib
import re
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .crawler import Fetcher, canonical_url, same_site, soup_of

PRICING_PATHS = ("/pricing", "/pricing/", "/plans", "/plans-pricing", "/pricing-plans", "/price", "/prices", "/subscribe")
PRICE_RE = re.compile(
    r"(?:(?:US|CA|AU|NZ|SG)?\$|€|£|₹|¥|USD|EUR|GBP|INR)\s?\d{1,3}(?:[,.]\d{3})*(?:\.\d{1,2})?"
    r"(?:\s?(?:/|per)\s?(?:mo|month|yr|year|user|seat|annum|week|day)\b)?",
    re.I,
)


async def find_pricing_url(fetcher: Fetcher, base: str, homepage_html: str) -> str | None:
    base_host = urlparse(base).netloc
    candidates: list[str] = []
    if homepage_html:
        soup = soup_of(homepage_html)
        for a in soup.find_all("a", href=True):
            href = urljoin(base, a["href"].strip())
            text = a.get_text(" ", strip=True).lower()
            if same_site(href, base_host) and (
                re.search(r"pricing|plans|price", urlparse(href).path, re.I) or text in ("pricing", "plans", "plans & pricing")
            ):
                candidates.append(canonical_url(href))
    candidates += [canonical_url(urljoin(base, p)) for p in PRICING_PATHS]
    for url in dict.fromkeys(candidates):
        if not fetcher.allowed(url):
            continue
        res = await fetcher.fetch(url)
        if res.ok and "html" in res.content_type and PRICE_RE.search(res.text):
            return res.final_url or url
    return None


PLAN_CLASS_RE = re.compile(r"plan|tier|package|title|name|heading", re.I)
NOT_A_PLAN = {"why", "add", "how", "what", "the", "for", "get", "start", "save", "only", "from", "per", "monthly",
              "annually", "billed", "pay", "month", "year", "yearly", "price", "pricing", "plans", "usd", "eur"}


def _normalise_price(raw: str) -> str:
    raw = re.sub(r"\s+", " ", raw).strip()
    raw = re.sub(r"([€£₹¥$])\s+", r"\1", raw)
    raw = re.sub(r"\s*/\s*", "/", raw)
    return raw


def _plan_for(node: Any) -> str:
    """Walk up from the element holding a price to the pricing card and read its heading."""
    el = node.parent
    for _ in range(6):
        if el is None or el.name in ("body", "html", "main"):
            break
        heading = el.find(["h1", "h2", "h3", "h4", "h5"])
        if heading is None:
            heading = el.find(attrs={"class": PLAN_CLASS_RE})
        if heading is not None:
            text = re.sub(r"\s+", " ", heading.get_text(" ", strip=True))
            if 2 <= len(text) <= 32 and not PRICE_RE.search(text) and text.lower() not in NOT_A_PLAN:
                return text
        el = el.parent
    return ""


def extract_prices(html: str) -> tuple[str, list[dict[str, Any]]]:
    soup = soup_of(html)
    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer", "header"]):
        tag.decompose()
    main = soup.find("main") or soup.body or soup
    text = re.sub(r"\s+", " ", main.get_text(" ", strip=True))
    content_hash = hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()[:16]

    prices: list[dict[str, Any]] = []
    seen_prices: set[str] = set()

    def add(plan: str, raw: str) -> None:
        raw = _normalise_price(raw)
        key = raw.lower()
        if key in seen_prices or len(prices) >= 40:
            return
        seen_prices.add(key)
        prices.append({"plan": plan, "price": raw})

    # DOM pass: the smallest element whose joined text contains a price is the price element, even
    # when "$" and "129" live in separate spans. Walk up from it to find the plan heading.
    for el in main.find_all(True):
        own = re.sub(r"\s+", " ", el.get_text(" ", strip=True))
        if len(own) > 60 or not PRICE_RE.search(own):
            continue
        if any(PRICE_RE.search(re.sub(r"\s+", " ", c.get_text(" ", strip=True))) for c in el.find_all(True)):
            continue  # a child is the real leaf
        for m in PRICE_RE.finditer(own):
            add(_plan_for(el), m.group(0))

    # Text pass backstop: anything the DOM pass missed, without a plan name.
    for m in PRICE_RE.finditer(text):
        add("", m.group(0))
    return content_hash, prices


def diff_prices(old: list[dict[str, Any]] | None, new: list[dict[str, Any]]) -> dict[str, Any]:
    old_set = {(p.get("plan", ""), p.get("price", "")) for p in (old or [])}
    new_set = {(p.get("plan", ""), p.get("price", "")) for p in new}
    added = [{"plan": a, "price": b} for a, b in sorted(new_set - old_set)]
    removed = [{"plan": a, "price": b} for a, b in sorted(old_set - new_set)]
    return {"added": added, "removed": removed}


async def check_pricing(fetcher: Fetcher, base: str, homepage_html: str, previous: dict[str, Any] | None,
                        known_url: str | None = None) -> dict[str, Any]:
    """Returns a dict describing the pricing page state and any change."""
    url = known_url
    if url:
        res = await fetcher.fetch(url)
        if not (res.ok and PRICE_RE.search(res.text)):
            url = None
    if not url:
        url = await find_pricing_url(fetcher, base, homepage_html)
    if not url:
        return {"found": False, "url": None, "changed": False, "detail": "No pricing page found", "prices": [],
                "diff": {"added": [], "removed": []}, "hash": None}
    res = await fetcher.fetch(url)
    if not res.ok:
        return {"found": True, "url": url, "changed": False, "detail": f"Pricing page not readable (HTTP {res.status})",
                "prices": [], "diff": {"added": [], "removed": []}, "hash": None}
    content_hash, prices = extract_prices(res.text)
    if previous is None:
        return {"found": True, "url": url, "changed": False, "baseline": True, "prices": prices, "hash": content_hash,
                "detail": f"Baseline captured: {len(prices)} price points", "diff": {"added": [], "removed": []}}
    diff = diff_prices(previous.get("prices"), prices)
    changed = bool(diff["added"] or diff["removed"])
    if changed:
        detail = f"{len(diff['added'])} price(s) added, {len(diff['removed'])} removed since last check"
    elif previous.get("content_hash") != content_hash:
        detail = "Pricing page copy changed but listed prices are the same"
    else:
        detail = "No pricing changes"
    return {"found": True, "url": url, "changed": changed, "baseline": False, "prices": prices, "hash": content_hash,
            "detail": detail, "diff": diff, "text_changed": previous.get("content_hash") != content_hash}
