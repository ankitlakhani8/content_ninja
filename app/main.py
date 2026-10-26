"""FastAPI app: the orchestration of crawl -> diff -> enrich -> alert lives in
run_check(); everything else is thin HTTP plumbing."""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()

from . import db  # noqa: E402
from .alerts import dispatch  # noqa: E402
from .crawler import Fetcher, crawl_site, normalize_domain  # noqa: E402
from .keywords import enrich_posts, pick_provider  # noqa: E402
from .pricing import check_pricing  # noqa: E402

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(BASE_DIR, "static")
NEW_WINDOW_DAYS = int(os.environ.get("NEW_WINDOW_DAYS", "7"))
CRAWL_BUDGET_SECONDS = int(os.environ.get("CRAWL_BUDGET_SECONDS", "240"))

app = FastAPI(title="Competitor Monitoring Alert Tool", version="1.0.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
_running: dict[int, asyncio.Task] = {}


@app.on_event("startup")
async def _startup() -> None:
    db.init_db()


class CheckRequest(BaseModel):
    domain: str
    slack_webhook: str | None = None


# ---------------------------------------------------------------------------
# Core pipeline
# ---------------------------------------------------------------------------

async def run_check(competitor_id: int, run_id: int) -> None:
    comp = db.get_competitor(competitor_id)
    assert comp
    domain = comp["domain"]

    def progress(msg: str) -> None:
        db.update_run(run_id, status_detail=msg)

    summary: dict[str, Any] = {"domain": domain, "notes": []}
    try:
        try:
            crawl = await asyncio.wait_for(crawl_site(domain, progress), timeout=CRAWL_BUDGET_SECONDS)
        except asyncio.TimeoutError:
            from .crawler import CrawlResult
            crawl = CrawlResult(domain=domain, status="error",
                                detail=f"Crawl exceeded the {CRAWL_BUDGET_SECONDS}s time budget; the site is very slow or very large.")
        summary["notes"] = crawl.notes
        summary["blog_url"] = crawl.blog_url
        summary["feed_url"] = crawl.feed_url

        if crawl.status != "ok":
            summary.update({"status": crawl.status, "status_detail": crawl.detail, "new_posts": [], "pricing": {}})
            alert = await dispatch(domain, summary, comp.get("slack_webhook"))
            summary["alert"] = alert
            db.update_run(run_id, status=crawl.status, status_detail=crawl.detail, finished_at=db.now_iso(),
                          alert_sent=int(alert["sent"]), alert_detail=alert["detail"], summary=summary)
            db.update_competitor(competitor_id, last_checked_at=db.now_iso(), last_status=crawl.status,
                                 blog_url=crawl.blog_url, feed_url=crawl.feed_url)
            return

        # ---- Diff against what we already know ---------------------------
        known = db.known_post_urls(competitor_id)
        is_baseline = len(known) == 0
        found = [a.to_dict() for a in crawl.articles]
        cutoff = (datetime.now(timezone.utc) - timedelta(days=NEW_WINDOW_DAYS)).isoformat()
        if is_baseline:
            new_posts = [p for p in found if p["published_at"] and p["published_at"] >= cutoff]
        else:
            new_posts = [p for p in found if p["url"] not in known]
        progress(f"{len(found)} posts found, {len(new_posts)} new; enriching keywords")

        # ---- Keyword enrichment (new posts only: that is what we alert on) ----
        keyword_status = await enrich_posts(new_posts)
        summary["keyword_status"] = keyword_status
        # Baseline posts that are not "new" still get a heuristic keyword so the table is complete.
        from .keywords import derive_keyword
        for p in found:
            if p not in new_posts:
                p.setdefault("keyword", derive_keyword(p.get("title")))
                p.setdefault("keyword_source", "heuristic")

        for p in found:
            db.insert_post(competitor_id, run_id, p)

        # ---- Pricing ------------------------------------------------------
        progress("Checking pricing page")
        fetcher = Fetcher()
        try:
            await fetcher.load_robots(crawl.home_url)
            previous = db.latest_pricing_snapshot(competitor_id)
            pricing = await check_pricing(fetcher, crawl.home_url, crawl.homepage_html, previous, comp.get("pricing_url"))
        finally:
            await fetcher.close()
        if pricing.get("found") and pricing.get("hash"):
            db.insert_pricing_snapshot(competitor_id, run_id, pricing["url"], pricing["hash"], pricing["prices"])
        summary["pricing"] = pricing

        # ---- Alert --------------------------------------------------------
        progress("Sending alert")
        summary.update({
            "status": "ok", "is_baseline": is_baseline, "total_posts_found": len(found),
            "new_posts": new_posts, "all_posts": found,
        })
        alert = await dispatch(domain, summary, comp.get("slack_webhook"))
        summary["alert"] = alert
        detail = (f"Baseline: indexed {len(found)} posts, {len(new_posts)} from the last {NEW_WINDOW_DAYS} days"
                  if is_baseline else f"{len(new_posts)} new post(s) since last check; {len(found)} scanned")
        db.update_run(run_id, status="ok", status_detail=detail, finished_at=db.now_iso(), is_baseline=int(is_baseline),
                      total_posts_found=len(found), new_posts_count=len(new_posts), pricing_changed=int(bool(pricing.get("changed"))),
                      alert_sent=int(alert["sent"]), alert_detail=alert["detail"], summary=summary)
        db.update_competitor(competitor_id, last_checked_at=db.now_iso(), last_status="ok", blog_url=crawl.blog_url,
                             feed_url=crawl.feed_url, pricing_url=pricing.get("url"))
    except Exception as exc:  # never leave a run stuck in "running"
        detail = f"Unexpected error: {type(exc).__name__}: {exc}"[:300]
        summary.update({"status": "error", "status_detail": detail, "new_posts": []})
        db.update_run(run_id, status="error", status_detail=detail, finished_at=db.now_iso(), summary=summary)
        db.update_competitor(competitor_id, last_checked_at=db.now_iso(), last_status="error")
    finally:
        _running.pop(competitor_id, None)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@app.get("/")
async def index() -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/api/health")
async def health() -> dict[str, Any]:
    provider = pick_provider()
    return {
        "ok": True,
        "keyword_provider": provider.name if provider.configured() else None,
        "slack_default": bool(os.environ.get("SLACK_WEBHOOK_URL")),
        "email": bool(os.environ.get("SMTP_HOST") and os.environ.get("ALERT_EMAIL_TO")),
    }


@app.post("/api/check")
async def start_check(req: CheckRequest) -> dict[str, Any]:
    try:
        domain = normalize_domain(req.domain)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    webhook = (req.slack_webhook or "").strip() or None
    if webhook and not webhook.startswith("https://hooks.slack.com/"):
        raise HTTPException(status_code=400, detail="Slack webhook must start with https://hooks.slack.com/")
    comp = db.upsert_competitor(domain, webhook)
    if comp["id"] in _running and not _running[comp["id"]].done():
        raise HTTPException(status_code=409, detail="A check for this domain is already running")
    run_id = db.create_run(comp["id"])
    _running[comp["id"]] = asyncio.create_task(run_check(comp["id"], run_id))
    return {"run_id": run_id, "competitor_id": comp["id"], "domain": domain}


@app.get("/api/runs/{run_id}")
async def get_run(run_id: int) -> dict[str, Any]:
    run = db.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


@app.get("/api/competitors")
async def competitors() -> list[dict[str, Any]]:
    return db.list_competitors()


@app.get("/api/competitors/{competitor_id}/posts")
async def competitor_posts(competitor_id: int) -> list[dict[str, Any]]:
    if not db.get_competitor(competitor_id):
        raise HTTPException(status_code=404, detail="Competitor not found")
    return db.list_posts(competitor_id)


@app.get("/api/competitors/{competitor_id}/runs")
async def competitor_runs(competitor_id: int) -> list[dict[str, Any]]:
    if not db.get_competitor(competitor_id):
        raise HTTPException(status_code=404, detail="Competitor not found")
    return db.list_runs(competitor_id)


@app.exception_handler(Exception)
async def _unhandled(_, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=500, content={"detail": f"{type(exc).__name__}: {exc}"[:300]})
