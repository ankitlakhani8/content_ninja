# Competitor Monitoring Alert Tool

Enter a competitor's domain. The tool crawls the site with its own scraper, finds the blog, records every
article it can see, flags what is new since the last check, watches the pricing page for changes, enriches
each new post with a target keyword and search volume, and sends a Slack / email alert with the summary.

Built for the Content Ninja assignment. Stack: Python 3.12, FastAPI, httpx, BeautifulSoup, SQLite, vanilla JS.

## How it works

```
domain ──► homepage ──► robots.txt + feeds + sitemaps + nav links + common paths
                              │
                              ▼
                    candidate article URLs (feed › listing page › sitemap)
                              │  fetch in waves, drop category/listing pages
                              ▼
                 title · publish date · meta description  ──► SQLite (posts)
                              │
                 diff against stored URLs  ──► "new posts"
                              │
                 keyword (from title) + volume (DataForSEO / Keywords Everywhere)
                              │
                 pricing page snapshot diff  ──► "pricing changed"
                              │
                              ▼
                 Slack webhook / SMTP email / dashboard preview
```

**Blog discovery** (`app/crawler.py`). Four independent signals are merged so one missing piece does not sink
the crawl: `<link rel="alternate">` and conventional RSS/Atom paths; `robots.txt` sitemaps plus common sitemap
paths (sitemap indexes are followed, post/blog children first); nav links whose path or text is blog-like;
and a probe of `/blog`, `/resources`, `/news`, `blog.<domain>` etc. Listing pages are scanned for links that
look like articles (slug under the blog prefix, or a dated path, not a tag/category/author/page URL).

**Article extraction.** Title from `og:title` → `<h1>` → `<title>` (site-name suffix stripped). Publish date
from `article:published_time`, other date metas, JSON-LD `datePublished`, `<time datetime>`, or a dated URL.
Description from `meta description` / `og:description`. Category and index pages are rejected with a
content heuristic: a real post has one container holding several long paragraphs; a listing page spreads
short excerpts across cards. Feed entries are trusted as-is, everything else is fetched and verified.

**New-post detection** (`app/main.py`). Posts are keyed on canonical URL per competitor. The first run on a
domain is a *baseline*: every post is stored and only posts published in the last 7 days
(`NEW_WINDOW_DAYS`) are flagged. Every later run flags only URLs not already in the database.

**Pricing changes** (`app/pricing.py`). The pricing page is found from nav links or `/pricing`, `/plans`.
Every price-like string (`$129/mo`, `€49`, `₹999/year`) is extracted with the plan heading of its card, the
normalised page text is hashed, and both are stored. The next run reports prices added / removed, and
separately whether the page copy changed while the prices stayed the same.

**Keyword enrichment** (`app/keywords.py`). The target keyword is taken from the title (SEO titles lead
with it: "Affiliate Marketing: What It Is…" → `affiliate marketing`; otherwise the first meaningful words).
Search volume, CPC and competition come from DataForSEO (Google Ads search volume endpoint) or Keywords
Everywhere, whichever is configured. Only new posts are sent to the API to conserve credits.

**Alerts** (`app/alerts.py`). Slack incoming webhook (server-wide `SLACK_WEBHOOK_URL` or pasted per run in
the UI) and/or SMTP email. The same summary is always shown in the dashboard, so the run is useful without
either channel.

## Failure handling (what the assignment tests)

| Situation | Behaviour |
|---|---|
| Site has no blog | Run ends with status `no_blog` and a plain-English reason; an alert still goes out saying the check could not complete. |
| Site blocks bots | 401/403/429/503 or a Cloudflare / captcha challenge page → status `blocked`, nothing stored. `robots.txt` is honoured: disallowed URLs are skipped and counted. |
| Site unreachable / bad DNS / timeout | Status `unreachable` with the underlying error. |
| Invalid domain input | HTTP 400 before any crawl starts. |
| Keyword API key missing | Posts still get a keyword; volume is `n/a`; the run says which env vars to set. |
| Keyword API key invalid / out of credits | The provider's error (e.g. `DataForSEO rejected the credentials (HTTP 401)`) is shown on the run and in the alert. The crawl and alert still complete. |
| Slack webhook rejected | Alert status shows the HTTP error; the summary stays visible in the dashboard. |
| Same domain checked twice at once | HTTP 409. |
| Crawler exception | Caught, run marked `error` with the message, never left in `running`. |

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                   # add API keys / webhook (all optional)
uvicorn app.main:app --reload
```

Open http://localhost:8000.

## Deploy

The repo ships a `Dockerfile` and `render.yaml`. On Render: *New → Blueprint*, point at the repo, add the
secret env vars, deploy. Any Docker host works the same way (`docker build -t monitor . && docker run -p
8000:8000 monitor`). Mount a persistent disk at `/app/data` so run history survives restarts; without one the
database resets on each deploy (fine for a demo, not for production).

## API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/check` `{domain, slack_webhook?}` | Start a check; returns `run_id`. |
| `GET` | `/api/runs/{id}` | Poll a run: status, progress message, full summary when finished. |
| `GET` | `/api/competitors` | Tracked domains with post/run counts. |
| `GET` | `/api/competitors/{id}/posts` | Every stored post with keyword data. |
| `GET` | `/api/competitors/{id}/runs` | Run history. |
| `GET` | `/api/health` | Which keyword provider and alert channels are configured. |

## Limits and trade-offs

- Crawls are polite (10 concurrent requests, 15 s timeout) and capped at ~40 verified posts per run, newest
  first. A typical run takes 10–45 s depending on the site.
- JavaScript-only blogs (content rendered client-side with no feed or sitemap) cannot be read without a
  headless browser; they surface as `no_blog`.
- Plan names on pricing pages are a heuristic (nearest heading); the price set itself is what is diffed, so a
  wrong plan label never causes a false alert.
- Search volume is only as good as the free tier of the provider; DataForSEO's trial returns real Google Ads
  volumes, Keywords Everywhere needs purchased credits.
