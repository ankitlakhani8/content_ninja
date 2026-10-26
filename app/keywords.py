"""Keyword enrichment.

Target keyword comes from the article itself (meta keywords, else a cleaned
title). Search volume comes from whichever keyword API is configured:

  * DataForSEO  (DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD) - free trial credits
  * Keywords Everywhere (KEYWORDS_EVERYWHERE_API_KEY)

If no key is configured, or the key is rejected, every post still gets a
keyword and the run reports exactly why volume is missing. The crawl never
fails because of the keyword API."""
from __future__ import annotations

import base64
import os
import re
from typing import Any

import httpx

STOPWORDS = {
    "a", "an", "the", "and", "or", "of", "to", "in", "on", "for", "with", "your", "you", "is", "are", "how",
    "what", "why", "when", "which", "who", "vs", "vs.", "from", "by", "at", "as", "it", "its", "this", "that",
    "we", "our", "ultimate", "complete", "guide", "introducing", "announcing", "new", "best", "top", "into",
    "do", "does", "can", "should", "will", "about", "every", "everything", "need", "know", "tips", "ways",
}
LEADING_PATTERNS = re.compile(
    r"^(how to|how|what is|what are|why|the ultimate guide to|a guide to|guide to|introducing|announcing|"
    r"\d+ (best|top|ways to|tips for|reasons))\s+",
    re.I,
)


def _clean(text: str) -> str:
    text = text.lower()
    text = re.sub(r"\[.*?\]|\(.*?\)", " ", text)          # "[2026]", "(updated)"
    text = re.sub(r"[\"“”‘’'|;:!?,.&+/]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def derive_keyword(title: str | None, description: str | None = None) -> str | None:
    """SEO titles usually lead with the keyword: 'Affiliate Marketing: What It Is...' -> 'affiliate marketing'."""
    if not title:
        return None
    head = re.split(r"\s*[:\-–—]\s+", title, maxsplit=1)[0]
    head_clean = _clean(head)
    head_tokens = head_clean.split()
    if 1 <= len(head_tokens) <= 5 and head_clean != _clean(title) and not LEADING_PATTERNS.match(head_clean):
        return head_clean[:80]
    t = LEADING_PATTERNS.sub("", _clean(title))
    tokens = [w for w in t.split() if w not in STOPWORDS and not re.fullmatch(r"(19|20)\d{2}", w)]
    if not tokens:
        tokens = t.split()
    keyword = " ".join(tokens[:4]).strip()
    return keyword[:80] or None


class KeywordProvider:
    name = "none"

    def __init__(self) -> None:
        self.error: str | None = None

    def configured(self) -> bool:
        return False

    async def volumes(self, keywords: list[str]) -> dict[str, dict[str, Any]]:
        return {}


class DataForSEO(KeywordProvider):
    name = "dataforseo"

    def __init__(self) -> None:
        super().__init__()
        self.login = os.environ.get("DATAFORSEO_LOGIN", "").strip()
        self.password = os.environ.get("DATAFORSEO_PASSWORD", "").strip()

    def configured(self) -> bool:
        return bool(self.login and self.password)

    async def volumes(self, keywords: list[str]) -> dict[str, dict[str, Any]]:
        token = base64.b64encode(f"{self.login}:{self.password}".encode()).decode()
        payload = [{"keywords": keywords[:700], "location_code": 2840, "language_code": "en"}]
        out: dict[str, dict[str, Any]] = {}
        try:
            async with httpx.AsyncClient(timeout=40) as client:
                resp = await client.post(
                    "https://api.dataforseo.com/v3/keywords_data/google_ads/search_volume/live",
                    json=payload, headers={"Authorization": f"Basic {token}"},
                )
        except httpx.HTTPError as exc:
            self.error = f"DataForSEO unreachable: {type(exc).__name__}"
            return out
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code == 401:
            self.error = "DataForSEO rejected the credentials (HTTP 401). Check DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD."
            return out
        if resp.status_code != 200 or not data.get("tasks"):
            # DataForSEO explains itself in status_message (e.g. 40104 "verify your account", 40200 "no money").
            msg = data.get("status_message") or f"HTTP {resp.status_code}"
            self.error = f"DataForSEO: {msg}"[:220]
            return out
        task = data["tasks"][0]
        if task.get("status_code") != 20000:
            self.error = f"DataForSEO task error {task.get('status_code')}: {task.get('status_message')}"
            return out
        for item in task.get("result") or []:
            kw = (item.get("keyword") or "").lower()
            comp = item.get("competition")
            out[kw] = {
                "search_volume": item.get("search_volume"),
                "cpc": item.get("cpc"),
                "competition": comp if isinstance(comp, str) else (None if comp is None else str(comp)),
            }
        return out


class KeywordsEverywhere(KeywordProvider):
    name = "keywords_everywhere"

    def __init__(self) -> None:
        super().__init__()
        self.key = os.environ.get("KEYWORDS_EVERYWHERE_API_KEY", "").strip()

    def configured(self) -> bool:
        return bool(self.key)

    async def volumes(self, keywords: list[str]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        form = [("kw[]", k) for k in keywords[:100]] + [("country", "us"), ("currency", "usd"), ("dataSource", "gsc")]
        try:
            async with httpx.AsyncClient(timeout=40) as client:
                resp = await client.post(
                    "https://api.keywordseverywhere.com/v1/get_keyword_data",
                    data=form, headers={"Authorization": f"Bearer {self.key}", "Accept": "application/json"},
                )
        except httpx.HTTPError as exc:
            self.error = f"Keywords Everywhere unreachable: {type(exc).__name__}"
            return out
        if resp.status_code in (401, 403):
            self.error = "Keywords Everywhere rejected the API key (HTTP %d)." % resp.status_code
            return out
        if resp.status_code != 200:
            self.error = f"Keywords Everywhere returned HTTP {resp.status_code}"
            return out
        for item in resp.json().get("data") or []:
            kw = (item.get("keyword") or "").lower()
            cpc = item.get("cpc") or {}
            out[kw] = {
                "search_volume": item.get("vol"),
                "cpc": float(cpc.get("value")) if isinstance(cpc, dict) and cpc.get("value") not in (None, "") else None,
                "competition": str(item.get("competition")) if item.get("competition") is not None else None,
            }
        return out


def pick_provider() -> KeywordProvider:
    for cls in (DataForSEO, KeywordsEverywhere):
        p = cls()
        if p.configured():
            return p
    return KeywordProvider()


async def enrich_posts(posts: list[dict[str, Any]]) -> dict[str, Any]:
    """Mutates posts in place adding keyword / search_volume / cpc / competition.
    Returns a status dict for the UI."""
    for p in posts:
        p["keyword"] = derive_keyword(p.get("title"), p.get("description"))
        p["search_volume"] = None
        p["cpc"] = None
        p["competition"] = None
        p["keyword_source"] = "heuristic"
    provider = pick_provider()
    if not provider.configured():
        return {"provider": None, "ok": False,
                "detail": "No keyword API configured; keywords derived from titles, search volume unavailable. "
                          "Set DATAFORSEO_LOGIN/PASSWORD or KEYWORDS_EVERYWHERE_API_KEY."}
    keywords = sorted({p["keyword"] for p in posts if p.get("keyword")})
    if not keywords:
        return {"provider": provider.name, "ok": True, "detail": f"{provider.name} configured; no new posts to enrich this run"}
    volumes = await provider.volumes(keywords)
    if provider.error:
        return {"provider": provider.name, "ok": False, "detail": provider.error}
    hits = 0
    for p in posts:
        kw = (p.get("keyword") or "").lower()
        if kw in volumes:
            p.update(volumes[kw])
            p["keyword_source"] = provider.name
            if p.get("search_volume") is not None:
                hits += 1
    return {"provider": provider.name, "ok": True,
            "detail": f"{provider.name}: volume returned for {hits}/{len(keywords)} keywords"}
