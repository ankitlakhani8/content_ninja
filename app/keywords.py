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
    # Only words that never belong in a search query. Prepositions and "best"/"top" stay: they are
    # part of real keywords ("best time to post on tiktok").
    "a", "an", "the", "your", "you", "is", "are", "how", "what", "why", "when", "which", "who", "it", "its",
    "this", "that", "we", "our", "ultimate", "complete", "guide", "introducing", "announcing", "new", "into",
    "do", "does", "can", "should", "will", "about", "every", "everything", "need", "know", "tips", "ways",
    "here", "heres", "there", "i", "my", "me", "us",
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
    tokens = [w for w in t.split() if w not in STOPWORDS and not re.fullmatch(r"\d+", w)]
    if not tokens:
        tokens = t.split()
    tokens = tokens[:6]
    edge = {"to", "for", "in", "on", "of", "with", "and", "at", "by", "from", "or", "vs"}
    while tokens and tokens[0] in edge:
        tokens.pop(0)
    while tokens and tokens[-1] in edge:
        tokens.pop()
    keyword = " ".join(tokens).strip()
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


class GoogleTrends(KeywordProvider):
    """Keyless fallback. Google Trends returns relative interest (0-100) per keyword, so each batch
    includes an anchor keyword with a known US monthly volume and the others are scaled against it.
    The result is an order-of-magnitude estimate and is labelled as such in the UI."""

    name = "google_trends"
    ANCHOR = ("affiliate marketing", 90_000)   # ~US Google Ads monthly volume, used only for scaling
    EXPLORE = "https://trends.google.com/trends/api/explore"
    MULTILINE = "https://trends.google.com/trends/api/widgetdata/multiline"
    HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
               "Accept-Language": "en-US,en;q=0.9"}

    def configured(self) -> bool:
        return os.environ.get("DISABLE_GOOGLE_TRENDS", "").lower() not in ("1", "true", "yes")

    @staticmethod
    def _strip(text: str) -> Any:
        import json
        return json.loads(text.split("\n", 1)[1] if text.startswith(")]}'") else text)

    async def _interest(self, client: httpx.AsyncClient, keywords: list[str]) -> dict[str, float]:
        import json
        req = {"comparisonItem": [{"keyword": k, "geo": "", "time": "today 3-m"} for k in keywords],
               "category": 0, "property": ""}
        resp = await client.get(self.EXPLORE, params={"hl": "en-US", "tz": "0", "req": json.dumps(req)})
        if resp.status_code != 200:
            raise RuntimeError(f"explore HTTP {resp.status_code}")
        widgets = self._strip(resp.text)["widgets"]
        widget = next(w for w in widgets if w.get("id") == "TIMESERIES")
        resp = await client.get(self.MULTILINE, params={"hl": "en-US", "tz": "0", "req": json.dumps(widget["request"]),
                                                        "token": widget["token"]})
        if resp.status_code != 200:
            raise RuntimeError(f"multiline HTTP {resp.status_code}")
        timeline = self._strip(resp.text)["default"]["timelineData"]
        sums = [0.0] * len(keywords)
        for point in timeline:
            for i, v in enumerate(point.get("value", [])):
                sums[i] += float(v)
        n = max(len(timeline), 1)
        return {k: sums[i] / n for i, k in enumerate(keywords)}

    async def volumes(self, keywords: list[str]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        anchor_kw, anchor_vol = self.ANCHOR
        try:
            async with httpx.AsyncClient(headers=self.HEADERS, timeout=30, follow_redirects=True) as client:
                await client.get("https://trends.google.com/?geo=US")  # sets the NID cookie Trends expects
                for i in range(0, len(keywords), 4):          # Trends allows 5 terms per request
                    batch = keywords[i:i + 4]
                    interest = await self._interest(client, [anchor_kw] + batch)
                    base = interest.get(anchor_kw) or 0.0
                    for k in batch:
                        if base <= 0:
                            continue
                        est = anchor_vol * interest.get(k, 0.0) / base
                        out[k.lower()] = {
                            "search_volume": int(round(est, -2)) if est >= 100 else (int(round(est, -1)) if est >= 10 else 0),
                            "cpc": None, "competition": None,
                        }
        except RuntimeError as exc:
            self.error = f"Google Trends refused the request ({exc}); try again in a few minutes or add a DataForSEO key."
        except (httpx.HTTPError, KeyError, StopIteration, ValueError) as exc:
            self.error = f"Google Trends unavailable: {type(exc).__name__}"
        return out


def pick_provider() -> KeywordProvider:
    for cls in (DataForSEO, KeywordsEverywhere, GoogleTrends):
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
    if provider.name == "google_trends":
        return {"provider": provider.name, "ok": True,
                "detail": f"Estimated volume for {hits}/{len(keywords)} keywords from Google Trends interest "
                          f"(free, keyless; order-of-magnitude only). Add a DataForSEO key for exact Google Ads volumes."}
    return {"provider": provider.name, "ok": True,
            "detail": f"{provider.name}: volume returned for {hits}/{len(keywords)} keywords"}
