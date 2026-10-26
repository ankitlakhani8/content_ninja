"""Hand-rolled crawler: fetches a competitor's homepage, discovers its blog via
feeds, sitemaps, nav links and common paths, then extracts title / publish
date / meta description for each article. No third-party scraping API."""
from __future__ import annotations

import asyncio
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable
from urllib.parse import urljoin, urlparse, urlunparse
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36 CompetitorMonitor/1.0"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
TIMEOUT = httpx.Timeout(15.0, connect=10.0)
MAX_ARTICLES = 40          # articles checked per wave / kept per run (newest first)
MIN_ARTICLES = 15          # stop fetching further waves once this many real posts are confirmed
MAX_LISTING_PAGES = 6      # listing pages scanned for article links
CONCURRENCY = 10
MAX_BODY_BYTES = 3 * 1024 * 1024  # never read more than 3 MB of any one response


def _sniff_charset(raw: bytes) -> str | None:
    head = raw[:4096].lower()
    m = re.search(rb"charset=[\"']?([a-z0-9_\-]+)", head)
    return m.group(1).decode("ascii", "ignore") if m else None

BLOG_PATH_WORDS = (
    "blog", "blogs", "article", "articles", "news", "insights", "resources", "learn",
    "guides", "guide", "stories", "updates", "journal", "posts", "post", "magazine",
    "knowledge", "library", "newsroom", "press", "case-studies", "tutorials",
)
NON_ARTICLE_SEGMENTS = (
    "tag", "tags", "category", "categories", "author", "authors", "page", "topic", "topics",
    "search", "feed", "rss", "wp-content", "wp-json", "cdn-cgi", "login", "signup", "cart",
)
FEED_PATHS = ("/feed", "/feed/", "/rss", "/rss.xml", "/feed.xml", "/atom.xml", "/index.xml",
              "/blog/feed", "/blog/feed/", "/blog/rss.xml", "/blog/feed.xml", "/blog/atom.xml",
              "/blog/index.xml", "/news/feed", "/articles/feed")
SITEMAP_PATHS = ("/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml", "/post-sitemap.xml",
                 "/blog-sitemap.xml", "/sitemap/blog.xml", "/sitemaps/sitemap.xml")
LISTING_PATHS = ("/blog", "/blog/", "/articles", "/news", "/insights", "/resources", "/resources/blog",
                 "/learn", "/guides", "/stories", "/updates", "/journal", "/posts", "/magazine")

BLOCK_MARKERS = (
    # Specific challenge-page phrases only; plain words like "captcha" appear on normal sites too.
    "just a moment...", "attention required! | cloudflare", "cf-browser-verification", "cf-chl-",
    "verify you are human", "ddos-guard", "request unsuccessful. incapsula", "pardon our interruption",
    "enable javascript and cookies to continue", "px-captcha", "are you a robot", "access denied</h1>",
    "access denied</title>", "bot detection", "please verify you are a human", "checking your browser",
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class FetchResult:
    url: str
    status: int = 0
    text: str = ""
    final_url: str = ""
    error: str | None = None
    blocked: bool = False
    content_type: str = ""

    @property
    def ok(self) -> bool:
        return self.status == 200 and not self.blocked and bool(self.text)


@dataclass
class Article:
    url: str
    title: str | None = None
    published_at: str | None = None   # ISO-8601 (UTC) or None
    description: str | None = None
    source: str = ""                  # feed | sitemap | listing
    lastmod: str | None = None        # sitemap <lastmod>, used only for ordering / last-resort date
    order: int = 0                    # position on the listing page (newest first)
    fetched: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url, "title": self.title, "published_at": self.published_at,
            "description": self.description, "source": self.source,
        }


@dataclass
class CrawlResult:
    domain: str
    status: str = "ok"              # ok | no_blog | blocked | unreachable | error
    detail: str = ""
    home_url: str = ""
    blog_url: str | None = None
    feed_url: str | None = None
    articles: list[Article] = field(default_factory=list)
    homepage_html: str = ""
    notes: list[str] = field(default_factory=list)
    robots_disallowed: int = 0


# ---------------------------------------------------------------------------
# Fetcher with bot-block detection and robots.txt
# ---------------------------------------------------------------------------

class Fetcher:
    def __init__(self, progress: Callable[[str], None] | None = None):
        self.client = httpx.AsyncClient(headers=HEADERS, timeout=TIMEOUT, follow_redirects=True, http2=False)
        self.sem = asyncio.Semaphore(CONCURRENCY)
        self.robots: RobotFileParser | None = None
        self.robots_sitemaps: list[str] = []
        self.progress = progress or (lambda msg: None)
        self.cache: dict[str, FetchResult] = {}

    async def close(self) -> None:
        await self.client.aclose()

    async def load_robots(self, base: str) -> None:
        res = await self.fetch(urljoin(base, "/robots.txt"), use_cache=False)
        rp = RobotFileParser()
        if res.status == 200 and res.text and "<html" not in res.text[:500].lower():
            rp.parse(res.text.splitlines())
            self.robots_sitemaps = [
                line.split(":", 1)[1].strip() for line in res.text.splitlines()
                if line.lower().startswith("sitemap:")
            ]
        else:
            rp.parse([])  # no robots.txt -> everything allowed
        self.robots = rp

    def allowed(self, url: str) -> bool:
        if self.robots is None:
            return True
        try:
            return self.robots.can_fetch("*", url)
        except Exception:
            return True

    async def fetch(self, url: str, use_cache: bool = True) -> FetchResult:
        if use_cache and url in self.cache:
            return self.cache[url]
        result = FetchResult(url=url)
        async with self.sem:
            try:
                async with self.client.stream("GET", url) as resp:
                    result.status = resp.status_code
                    result.final_url = str(resp.url)
                    result.content_type = resp.headers.get("content-type", "")
                    textual = (not result.content_type) or any(
                        t in result.content_type for t in ("text", "xml", "json", "rss", "atom"))
                    if textual:
                        chunks: list[bytes] = []
                        size = 0
                        async for chunk in resp.aiter_bytes():
                            chunks.append(chunk)
                            size += len(chunk)
                            if size > MAX_BODY_BYTES:
                                break
                        raw = b"".join(chunks)
                        encoding = resp.charset_encoding or _sniff_charset(raw) or "utf-8"
                        try:
                            result.text = raw.decode(encoding, errors="replace")
                        except LookupError:
                            result.text = raw.decode("utf-8", errors="replace")
                snippet = result.text[:4000].lower()
                looks_like_challenge = any(m in snippet for m in BLOCK_MARKERS)
                if result.status in (401, 403, 429, 503):
                    result.blocked = True
                elif result.status == 200 and len(result.text) < 20000 and looks_like_challenge:
                    result.blocked = True
                elif result.status in (202, 203) and len(result.text) < 5000:
                    # Akamai / PerimeterX / DataDome style: 202 with a tiny HTML shell that only runs a JS challenge.
                    result.blocked = True
            except httpx.TimeoutException:
                result.error = "timeout"
            except httpx.HTTPError as exc:
                result.error = f"{type(exc).__name__}: {exc}"[:200]
            except Exception as exc:  # pragma: no cover - defensive
                result.error = f"{type(exc).__name__}: {exc}"[:200]
        if use_cache:
            self.cache[url] = result
        return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def normalize_domain(raw: str) -> str:
    raw = raw.strip().lower()
    if not raw:
        raise ValueError("Domain is empty")
    if "://" not in raw:
        raw = "https://" + raw
    host = urlparse(raw).netloc.split("@")[-1].split(":")[0]
    host = host.strip(".")
    if host.startswith("www."):
        host = host[4:]  # one record per site whether the user types www or not
    if not host or "." not in host or " " in host or len(host) > 253:
        raise ValueError("That does not look like a valid domain")
    if not re.fullmatch(r"[a-z0-9 -￿.-]+", host):
        raise ValueError("Domain contains invalid characters")
    if host.endswith((".local", ".internal", ".localhost")) or host == "localhost":
        raise ValueError("Internal hostnames cannot be monitored")
    if _is_private_host(host):
        raise ValueError("Private or loopback addresses cannot be monitored")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        raise ValueError("That does not look like a valid domain")
    return host


def _is_private_host(host: str) -> bool:
    """SSRF guard for the hosted tool: refuse IPs and names that resolve to private ranges."""
    import ipaddress
    import socket
    try:
        ip = ipaddress.ip_address(host)
        return not ip.is_global
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return False  # unresolvable: let the crawl report "unreachable" with the real error
    for info in infos:
        try:
            if not ipaddress.ip_address(info[4][0]).is_global:
                return True
        except ValueError:
            continue
    return False


def registered_domain(host: str) -> str:
    """Rough eTLD+1 so blog.example.com and www.example.com count as one site."""
    host = host.lower().lstrip("www.")
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net", "gov", "ac", "edu") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def canonical_url(url: str) -> str:
    p = urlparse(url)
    path = re.sub(r"/{2,}", "/", p.path) or "/"
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    # Drop tracking params entirely; article identity rarely depends on the query string.
    return urlunparse((p.scheme.lower(), p.netloc.lower(), path, "", "", ""))


def same_site(url: str, base_host: str) -> bool:
    try:
        return registered_domain(urlparse(url).netloc) == registered_domain(base_host)
    except Exception:
        return False


def parse_date(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        dt = dateparser.parse(value, fuzzy=False)
    except Exception:
        try:
            dt = dateparser.parse(value, fuzzy=True)
        except Exception:
            return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    if dt.year < 1995 or dt > datetime.now(timezone.utc).replace(year=datetime.now(timezone.utc).year + 1):
        return None
    return dt.replace(microsecond=0).isoformat()


def path_segments(url: str) -> list[str]:
    return [s for s in urlparse(url).path.split("/") if s]


def looks_like_article(url: str, blog_prefixes: Iterable[str]) -> bool:
    p = urlparse(url)
    segs = path_segments(url)
    if not segs:
        return False
    lower = [s.lower() for s in segs]
    if any(s in NON_ARTICLE_SEGMENTS for s in lower):
        return False
    if re.search(r"\.(xml|pdf|png|jpe?g|gif|svg|css|js|zip|mp4|webp|ico)$", p.path, re.I):
        return False
    if re.search(r"/page/\d+|[?&]page=|[?&]p=\d", url, re.I):
        return False
    last = lower[-1]
    in_blog_section = any(p.path.lower().startswith(prefix) and len(segs) > len(path_segments(prefix)) for prefix in blog_prefixes)
    has_date = bool(re.search(r"/20\d{2}/\d{1,2}/", p.path))
    slug_like = "-" in last and len(last) >= 12
    if has_date and (slug_like or len(segs) >= 3):
        return True
    if in_blog_section and (slug_like or len(segs) >= 2):
        return True
    return False


def soup_of(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def clean_text(value: str | None) -> str | None:
    if not value:
        return None
    value = re.sub(r"\s+", " ", value).strip()
    return value or None


# ---------------------------------------------------------------------------
# Article extraction
# ---------------------------------------------------------------------------

def extract_article_meta(html: str, url: str) -> dict[str, Any]:
    soup = soup_of(html)
    meta: dict[str, str] = {}
    for tag in soup.find_all("meta"):
        key = (tag.get("property") or tag.get("name") or tag.get("itemprop") or "").lower()
        content = tag.get("content")
        if key and content and key not in meta:
            meta[key] = content.strip()

    title = meta.get("og:title") or meta.get("twitter:title")
    if not title:
        h1 = soup.find("h1")
        title = h1.get_text(" ", strip=True) if h1 else None
    if not title and soup.title:
        title = soup.title.get_text(" ", strip=True)
    title = clean_text(title)
    if title:
        title = re.split(r"\s+[|\-–—]\s+(?=[^|–—-]{2,40}$)", title)[0].strip() or title

    description = clean_text(meta.get("description") or meta.get("og:description") or meta.get("twitter:description"))

    date_raw = (
        meta.get("article:published_time") or meta.get("datepublished") or meta.get("date")
        or meta.get("pubdate") or meta.get("publish-date") or meta.get("dc.date.issued")
        or meta.get("dc.date") or meta.get("parsely-pub-date") or meta.get("sailthru.date")
        or meta.get("article:modified_time")
    )
    published = parse_date(date_raw)

    if not published:
        for script in soup.find_all("script", type=lambda t: t and "ld+json" in t):
            try:
                data = json.loads(script.string or "")
            except Exception:
                continue
            found = _find_ld_date(data)
            if found:
                published = parse_date(found)
                if published:
                    break

    if not published:
        t = soup.find("time", attrs={"datetime": True})
        if t:
            published = parse_date(t["datetime"])
        if not published:
            t = soup.find("time")
            if t:
                published = parse_date(t.get_text(" ", strip=True))

    if not published:
        m = re.search(r"/(20\d{2})/(\d{1,2})(?:/(\d{1,2}))?/", urlparse(url).path)
        if m:
            y, mo, d = m.group(1), m.group(2), m.group(3) or "1"
            published = parse_date(f"{y}-{int(mo):02d}-{int(d):02d}")

    if not published:
        # Client-rendered sites (Next.js, Gatsby, Contentful) ship the date inside embedded JSON.
        m = re.search(
            r"\"(?:datePublished|publishedAt|publishDate|publish_date|published_at|published|date|createdAt|created_at|firstPublishedAt)\""
            r"\s*:\s*\"((?:19|20)\d{2}-\d{2}-\d{2}[^\"]{0,30})\"",
            html,
        )
        if m:
            published = parse_date(m.group(1))

    if not published:
        # Last resort: a human-readable date printed near the top of the article body.
        body = soup.find("article") or soup.find("main") or soup.body or soup
        head_text = re.sub(r"\s+", " ", body.get_text(" ", strip=True))[:1500] if body else ""
        m = re.search(
            r"\b((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.? \d{1,2}(?:st|nd|rd|th)?,? (?:19|20)\d{2}"
            r"|\d{1,2}(?:st|nd|rd|th)? (?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.? (?:19|20)\d{2}"
            r"|(?:19|20)\d{2}-\d{2}-\d{2})\b",
            head_text,
        )
        if m:
            published = parse_date(m.group(1))

    explicit_date = bool(meta.get("article:published_time") or meta.get("datepublished") or meta.get("pubdate"))
    if not explicit_date:
        for script in soup.find_all("script", type=lambda t: t and "ld+json" in t):
            if "datePublished" in (script.string or ""):
                explicit_date = True
                break
    is_article = meta.get("og:type", "").lower() == "article" or explicit_date

    # An article has one container holding many long paragraphs. A category/listing page spreads
    # short excerpts across many cards, so its largest paragraph block stays small.
    body_len = 0
    for parent in soup.find_all(True):
        total = 0
        for child in parent.find_all("p", recursive=False):
            text = child.get_text(" ", strip=True)
            if len(text) >= 60:
                total += len(text)
        body_len = max(body_len, total)

    return {
        "title": title, "description": description, "published_at": published, "is_article": is_article,
        "explicit_date": explicit_date, "body_text_len": body_len,
        "og_article": meta.get("og:type", "").lower() == "article",
    }


GENERIC_TITLE_RE = re.compile(
    r"^(all (posts|articles)|articles?|posts?|blog|news|resources?|insights|guides?|podcasts?|page \d+|.*,\s*page \d+|"
    r"(latest|recent) (posts|articles)|archives?|.* archives?|category:.*|tag:.*|.*\bpage \d+ of \d+.*|"
    r"site ?map|site ?map .*|.*\bsite ?map\b.*|search|search results.*|404.*|page not found.*)$",
    re.I,
)


def is_listing_page(meta: dict[str, Any], title: str | None) -> bool:
    if title and GENERIC_TITLE_RE.match(title.strip()):
        return True
    body = meta.get("body_text_len", 0)
    if body >= 700:
        return False
    # Category hubs on CMS-driven sites often carry og:type=article or a datePublished too, so those
    # signals only count when there is at least some body text.
    if (meta.get("og_article") or meta.get("explicit_date")) and body >= 250:
        return False
    return True


def _find_ld_date(data: Any) -> str | None:
    if isinstance(data, dict):
        for key in ("datePublished", "dateCreated", "uploadDate"):
            if isinstance(data.get(key), str):
                return data[key]
        for value in data.values():
            found = _find_ld_date(value)
            if found:
                return found
    elif isinstance(data, list):
        for item in data:
            found = _find_ld_date(item)
            if found:
                return found
    return None


# ---------------------------------------------------------------------------
# Feeds and sitemaps
# ---------------------------------------------------------------------------

def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1].lower()


def parse_feed(xml_text: str, base_host: str) -> list[Article]:
    try:
        root = ET.fromstring(xml_text.strip().encode("utf-8", "ignore"))
    except ET.ParseError:
        return []
    articles: list[Article] = []
    root_tag = _strip_ns(root.tag)
    if root_tag == "rss" or root_tag == "rdf":
        items = [el for el in root.iter() if _strip_ns(el.tag) == "item"]
    elif root_tag == "feed":
        items = [el for el in root.iter() if _strip_ns(el.tag) == "entry"]
    else:
        return []
    for item in items:
        link = title = date = desc = None
        for child in item:
            tag = _strip_ns(child.tag)
            text = (child.text or "").strip()
            if tag == "link":
                href = child.get("href")
                rel = child.get("rel", "alternate")
                if href and rel == "alternate" and not link:
                    link = href
                elif text and not link:
                    link = text
            elif tag == "title" and not title:
                title = clean_text(BeautifulSoup(text, "html.parser").get_text(" ")) if text else None
            elif tag in ("pubdate", "published", "updated", "date") and not date:
                date = text
            elif tag in ("description", "summary", "encoded", "content") and not desc and text:
                desc = clean_text(BeautifulSoup(text, "html.parser").get_text(" "))
                if desc and len(desc) > 300:
                    desc = desc[:297] + "..."
        if link and same_site(link, base_host) and title:
            articles.append(Article(url=canonical_url(link), title=title, published_at=parse_date(date),
                                    description=desc, source="feed"))
    return articles


def parse_sitemap(xml_text: str) -> tuple[list[str], list[tuple[str, str | None]]]:
    """Returns (child_sitemaps, [(url, lastmod)])."""
    try:
        root = ET.fromstring(xml_text.strip().encode("utf-8", "ignore"))
    except ET.ParseError:
        return [], []
    children: list[str] = []
    urls: list[tuple[str, str | None]] = []
    root_tag = _strip_ns(root.tag)
    for el in root:
        tag = _strip_ns(el.tag)
        loc = lastmod = None
        for sub in el:
            st = _strip_ns(sub.tag)
            if st == "loc":
                loc = (sub.text or "").strip()
            elif st == "lastmod":
                lastmod = (sub.text or "").strip()
        if not loc:
            continue
        if root_tag == "sitemapindex" or tag == "sitemap":
            children.append(loc)
        else:
            urls.append((loc, lastmod))
    return children, urls


# ---------------------------------------------------------------------------
# Main crawl
# ---------------------------------------------------------------------------

async def crawl_site(domain: str, progress: Callable[[str], None] | None = None) -> CrawlResult:
    report = progress or (lambda msg: None)
    result = CrawlResult(domain=domain)
    fetcher = Fetcher(progress)
    try:
        # 1. Homepage (https first, then http)
        home: FetchResult | None = None
        for scheme in ("https", "http"):
            candidate = f"{scheme}://{domain}/"
            report(f"Fetching homepage {candidate}")
            res = await fetcher.fetch(candidate)
            if res.ok or res.blocked:
                home = res
                break
            if res.status and res.status < 500 and not res.error:
                home = res
                break
            home = home or res
        assert home is not None
        if home.blocked:
            result.status = "blocked"
            result.detail = (f"The site refused automated access (HTTP {home.status}). It is likely behind a bot "
                             f"challenge such as Cloudflare. Nothing was scraped.")
            return result
        if not home.ok:
            result.status = "unreachable"
            result.detail = f"Could not load the homepage ({home.error or 'HTTP ' + str(home.status)})."
            return result

        base = home.final_url or home.url
        base_host = urlparse(base).netloc
        result.home_url = base
        result.homepage_html = home.text
        await fetcher.load_robots(base)
        if not fetcher.allowed(base):
            result.status = "blocked"
            result.detail = "robots.txt disallows crawling this site, so it was skipped out of courtesy."
            return result

        soup = soup_of(home.text)

        # 2. Candidate feeds, listing pages, sitemaps
        feed_candidates: list[str] = []
        for link in soup.find_all("link", attrs={"rel": True, "href": True}):
            rels = " ".join(link.get("rel") or []).lower()
            typ = (link.get("type") or "").lower()
            if "alternate" in rels and ("rss" in typ or "atom" in typ or "xml" in typ):
                feed_candidates.append(urljoin(base, link["href"]))
        feed_candidates += [urljoin(base, p) for p in FEED_PATHS]

        listing_candidates: list[str] = []
        for a in soup.find_all("a", href=True):
            href = urljoin(base, a["href"].strip())
            if not same_site(href, base_host):
                continue
            path = urlparse(href).path.lower()
            text = a.get_text(" ", strip=True).lower()
            segs = path_segments(href)
            if segs and (segs[0].lower() in BLOG_PATH_WORDS or (len(segs) == 2 and segs[1].lower() in BLOG_PATH_WORDS)
                         or text in BLOG_PATH_WORDS):
                if len(segs) <= 2:
                    listing_candidates.append(canonical_url(href))
        listing_candidates += [urljoin(base, p) for p in LISTING_PATHS]
        # Also try the conventional content subdomains (blog.x, news.x, newsroom.x, engineering.x).
        reg = registered_domain(base_host)
        for sub in ("blog", "news", "newsroom", "engineering"):
            listing_candidates.append(f"https://{sub}.{reg}/")
        listing_candidates = list(dict.fromkeys(canonical_url(u) for u in listing_candidates))

        def listing_priority(u: str) -> int:
            pu = urlparse(u)
            path = pu.path.lower() + ("//" + pu.netloc if pu.netloc != base_host else "")
            if "blog" in path:
                return 0
            if re.search(r"news|articles|insights|posts|journal|stories", path):
                return 1
            return 2
        # Stable sort: blog-ish paths first, shallow before deep (so /blog beats /blog/category), nav order kept.
        listing_candidates.sort(key=lambda u: (listing_priority(u), len(path_segments(u))))
        home_canon = canonical_url(base)
        listing_candidates = [u for u in listing_candidates if u != home_canon]

        sitemap_candidates = list(dict.fromkeys(fetcher.robots_sitemaps + [urljoin(base, p) for p in SITEMAP_PATHS]))

        # 3. Feeds -------------------------------------------------------
        report("Looking for RSS/Atom feeds")
        feed_articles: list[Article] = []
        feed_results = await asyncio.gather(*(fetcher.fetch(u) for u in feed_candidates[:16]))
        for res in feed_results:
            if res.ok and ("xml" in res.content_type or res.text.lstrip().startswith("<")):
                arts = parse_feed(res.text, base_host)
                if arts:
                    feed_articles = arts
                    result.feed_url = res.final_url or res.url
                    result.notes.append(f"Feed found: {result.feed_url} ({len(arts)} entries)")
                    break

        # 4. Sitemaps ----------------------------------------------------
        report("Scanning sitemaps")
        sitemap_urls: dict[str, str | None] = {}
        seen_sitemaps: set[str] = set()
        queue = list(sitemap_candidates)
        fetched_sitemaps = 0
        while queue and fetched_sitemaps < 12:
            batch, queue = queue[:6], queue[6:]
            batch = [u for u in batch if u not in seen_sitemaps]
            seen_sitemaps.update(batch)
            fetched_sitemaps += len(batch)
            results = await asyncio.gather(*(fetcher.fetch(u) for u in batch))
            for res in results:
                if not res.ok or "<" not in res.text[:200]:
                    continue
                children, urls = parse_sitemap(res.text)
                for loc, lastmod in urls:
                    if same_site(loc, base_host):
                        sitemap_urls.setdefault(canonical_url(loc), lastmod)
                # Prioritise child sitemaps that sound like posts.
                children.sort(key=lambda c: 0 if re.search(r"post|blog|article|news", c, re.I) else 1)
                queue.extend(c for c in children if c not in seen_sitemaps)
        if sitemap_urls:
            result.notes.append(f"Sitemap URLs collected: {len(sitemap_urls)}")

        # 5. Listing pages -----------------------------------------------
        report("Checking blog / resource listing pages")
        listing_articles: dict[str, Article] = {}
        blog_prefixes: list[str] = []
        listing_pages: dict[str, str] = {}  # prefix -> listing page URL that produced it
        listing_results = await asyncio.gather(*(fetcher.fetch(u) for u in listing_candidates[:14]))
        live_listings = [r for r in listing_results if r.ok and "html" in r.content_type]
        for res in live_listings[:MAX_LISTING_PAGES]:
            lp = urlparse(res.final_url or res.url)
            prefix = lp.path if lp.path not in ("", "/") else "/"
            foreign_host = not same_site(res.final_url or res.url, base_host)
            if foreign_host:
                # e.g. loom.com/blog -> atlassian.com/blog/app/loom: the blog lives on another company's
                # domain. Accept that host, scoped to its first path segment.
                segs0 = path_segments(res.final_url or res.url)
                prefix = "/" + segs0[0] if segs0 else "/"
                result.notes.append(f"Blog is hosted on another domain: {lp.netloc}{prefix}")
            elif lp.netloc != base_host:
                prefix = "/"  # blog subdomain: whole host is the blog
            elif prefix == "/":
                continue  # a "blog" link that just redirects home; every slug would count as a post
            lsoup = soup_of(res.text)
            found_here = 0
            for a in lsoup.find_all("a", href=True):
                href = urljoin(res.final_url or res.url, a["href"].strip())
                if not same_site(href, base_host) and urlparse(href).netloc != lp.netloc:
                    continue
                cu = canonical_url(href)
                host_prefixes = [prefix] if (lp.netloc == base_host or foreign_host) else ["/"]
                if urlparse(cu).netloc == lp.netloc and looks_like_article(cu, host_prefixes) and cu != canonical_url(res.final_url or res.url):
                    title = clean_text(a.get_text(" ", strip=True))
                    if cu not in listing_articles:
                        listing_articles[cu] = Article(url=cu, title=title if title and len(title) > 8 else None,
                                                       source="listing", order=len(listing_articles))
                    elif title and len(title) > 8 and not listing_articles[cu].title:
                        listing_articles[cu].title = title
                    found_here += 1
            if found_here >= 3:
                key = prefix if lp.netloc == base_host else f"//{lp.netloc}"
                blog_prefixes.append(key)
                listing_pages.setdefault(key, res.final_url or res.url)
        if listing_articles:
            result.notes.append(f"Listing pages yielded {len(listing_articles)} article links")

        # 6. Merge candidates ---------------------------------------------
        candidates: dict[str, Article] = {}
        for art in feed_articles:
            candidates[art.url] = art
        for url, art in listing_articles.items():
            if url not in candidates:
                candidates[url] = art
        effective_prefixes = [p for p in blog_prefixes if not p.startswith("//")] or ["/blog", "/articles", "/news", "/insights", "/resources", "/posts"]
        for url, lastmod in sitemap_urls.items():
            if url in candidates:
                candidates[url].lastmod = candidates[url].lastmod or parse_date(lastmod)
                continue
            if looks_like_article(url, effective_prefixes):
                candidates[url] = Article(url=url, lastmod=parse_date(lastmod), source="sitemap")

        if not candidates:
            result.status = "no_blog"
            result.detail = ("No blog, news or resource section could be found: no feed, no article-like sitemap "
                             "URLs and no listing page with article links.")
            return result

        # Check order: feed entries (real dates) first, then listing links in page order (blogs list
        # newest first), then sitemap URLs by lastmod. Sitemaps are huge and lastmod is not a publish
        # date, so they only fill in what the feed and listing pages missed.
        pool_all = list(candidates.values())
        feed_tier = sorted((a for a in pool_all if a.source == "feed"), key=lambda a: a.published_at or "", reverse=True)
        listing_tier = sorted((a for a in pool_all if a.source == "listing"), key=lambda a: a.order)
        sitemap_tier = sorted((a for a in pool_all if a.source == "sitemap"), key=lambda a: a.lastmod or "", reverse=True)
        ordered = feed_tier + listing_tier + sitemap_tier
        skipped_by_robots = 0

        # 7. Fetch article pages for anything missing title/date/description ----
        async def enrich(a: Article) -> None:
            nonlocal skipped_by_robots
            if a.title and a.published_at and a.description and a.source == "feed":
                return  # feed entries are trusted as-is
            if not fetcher.allowed(a.url):
                skipped_by_robots += 1
                a.title = None
                return
            res = await fetcher.fetch(a.url)
            if not res.ok or ("html" not in res.content_type and "<html" not in res.text[:500].lower()):
                a.title = None
                return
            meta = extract_article_meta(res.text, a.url)
            a.fetched = True
            if meta["title"] and a.source in ("listing", "sitemap"):
                a.title = meta["title"]
            a.title = a.title or meta["title"]
            a.published_at = a.published_at or meta["published_at"] or a.lastmod
            a.description = a.description or meta["description"]
            last = (path_segments(a.url) or [""])[-1].lower()
            slug_like = ("-" in last and len(last) >= 12) or bool(re.search(r"/20\d{2}/\d{1,2}/", a.url))
            if is_listing_page(meta, a.title):
                a.title = None  # category / index page, not a post
            elif not meta["is_article"] and a.source == "sitemap" and not a.published_at:
                a.title = None  # generic site page that slipped through the URL heuristic
            elif a.source in ("sitemap", "listing") and not slug_like and not meta["og_article"]:
                a.title = None  # /blog/product, /blog/integrations: section pages, not posts

        # Candidates are checked in waves: category pages carry fresh sitemap lastmod dates and
        # would otherwise crowd out real posts from the top of the list.
        checked: list[Article] = []
        pool = list(ordered)
        waves = 0
        while pool and waves < 3 and sum(1 for a in checked if a.title) < MIN_ARTICLES:
            wave, pool = pool[:MAX_ARTICLES], pool[MAX_ARTICLES:]
            report(f"Extracting metadata from {len(wave)} article pages")
            await asyncio.gather(*(enrich(a) for a in wave))
            checked.extend(wave)
            waves += 1
        result.robots_disallowed = skipped_by_robots
        if skipped_by_robots:
            result.notes.append(f"{skipped_by_robots} URLs skipped because robots.txt disallows them")

        articles = [a for a in checked if a.title][:MAX_ARTICLES]
        articles.sort(key=lambda a: a.published_at or "", reverse=True)
        result.articles = articles
        if not articles:
            result.status = "no_blog"
            result.detail = ("No blog or news section found. Some pages looked like they might be articles but none "
                             "had an article body with a title and date, so nothing was indexed.")
        # The blog URL shown to the user is the listing page that produced the most real articles.
        if listing_pages and articles:
            counts: dict[str, int] = {}
            for a in articles:
                pa = urlparse(a.url)
                for key in listing_pages:
                    hit = (pa.netloc == key[2:]) if key.startswith("//") else (pa.netloc == base_host and pa.path.startswith(key))
                    if hit:
                        counts[key] = counts.get(key, 0) + 1
            if counts:
                best = max(counts, key=lambda k: (counts[k], -len(k)))
                result.blog_url = listing_pages[best]
        if not result.blog_url:
            result.blog_url = next(iter(listing_pages.values()), None) or result.feed_url or (
                articles[0].url.rsplit("/", 1)[0] if articles else None)
        return result
    except Exception as exc:  # keep the run alive and surface the error
        result.status = "error"
        result.detail = f"Crawler error: {type(exc).__name__}: {exc}"[:300]
        return result
    finally:
        await fetcher.close()
