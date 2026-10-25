"""SQLite persistence. Plain sqlite3 keeps the dependency list short and the
schema obvious; everything the tool remembers between runs lives here."""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

DB_PATH = os.environ.get(
    "DB_PATH",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "monitor.db"),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS competitors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    domain TEXT NOT NULL UNIQUE,
    blog_url TEXT,
    feed_url TEXT,
    pricing_url TEXT,
    slack_webhook TEXT,
    created_at TEXT NOT NULL,
    last_checked_at TEXT,
    last_status TEXT
);
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    competitor_id INTEGER NOT NULL REFERENCES competitors(id),
    url TEXT NOT NULL,
    title TEXT,
    published_at TEXT,
    description TEXT,
    first_seen_at TEXT NOT NULL,
    first_seen_run_id INTEGER,
    keyword TEXT,
    search_volume INTEGER,
    cpc REAL,
    competition TEXT,
    keyword_source TEXT,
    UNIQUE(competitor_id, url)
);
CREATE TABLE IF NOT EXISTS pricing_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    competitor_id INTEGER NOT NULL REFERENCES competitors(id),
    run_id INTEGER,
    url TEXT,
    content_hash TEXT,
    prices_json TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    competitor_id INTEGER NOT NULL REFERENCES competitors(id),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    status_detail TEXT,
    is_baseline INTEGER DEFAULT 0,
    total_posts_found INTEGER DEFAULT 0,
    new_posts_count INTEGER DEFAULT 0,
    pricing_changed INTEGER DEFAULT 0,
    alert_sent INTEGER DEFAULT 0,
    alert_detail TEXT,
    summary_json TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    d = dict(row)
    if "summary_json" in d:
        d["summary"] = json.loads(d.pop("summary_json") or "null")
    if "prices_json" in d:
        d["prices"] = json.loads(d.pop("prices_json") or "[]")
    return d


# ---- competitors -----------------------------------------------------------

def upsert_competitor(domain: str, slack_webhook: str | None) -> dict[str, Any]:
    with connect() as conn:
        conn.execute(
            "INSERT INTO competitors (domain, created_at, slack_webhook) VALUES (?, ?, ?) "
            "ON CONFLICT(domain) DO UPDATE SET slack_webhook = COALESCE(excluded.slack_webhook, competitors.slack_webhook)",
            (domain, now_iso(), slack_webhook or None),
        )
        row = conn.execute("SELECT * FROM competitors WHERE domain = ?", (domain,)).fetchone()
        return row_to_dict(row)


def get_competitor(competitor_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        return row_to_dict(conn.execute("SELECT * FROM competitors WHERE id = ?", (competitor_id,)).fetchone())


def update_competitor(competitor_id: int, **fields: Any) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE competitors SET {cols} WHERE id = ?", (*fields.values(), competitor_id))


def list_competitors() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT c.*,
                   (SELECT COUNT(*) FROM posts p WHERE p.competitor_id = c.id) AS post_count,
                   (SELECT COUNT(*) FROM runs r WHERE r.competitor_id = c.id) AS run_count,
                   (SELECT id FROM runs r WHERE r.competitor_id = c.id ORDER BY id DESC LIMIT 1) AS last_run_id
            FROM competitors c ORDER BY last_checked_at DESC, id DESC
            """
        ).fetchall()
        return [row_to_dict(r) for r in rows]


# ---- posts -----------------------------------------------------------------

def known_post_urls(competitor_id: int) -> set[str]:
    with connect() as conn:
        rows = conn.execute("SELECT url FROM posts WHERE competitor_id = ?", (competitor_id,)).fetchall()
        return {r["url"] for r in rows}


def insert_post(competitor_id: int, run_id: int, post: dict[str, Any]) -> None:
    with connect() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO posts
               (competitor_id, url, title, published_at, description, first_seen_at, first_seen_run_id,
                keyword, search_volume, cpc, competition, keyword_source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                competitor_id, post["url"], post.get("title"), post.get("published_at"), post.get("description"),
                now_iso(), run_id, post.get("keyword"), post.get("search_volume"), post.get("cpc"),
                post.get("competition"), post.get("keyword_source"),
            ),
        )


def list_posts(competitor_id: int, limit: int = 200) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM posts WHERE competitor_id = ? ORDER BY COALESCE(published_at, '') DESC, id DESC LIMIT ?",
            (competitor_id, limit),
        ).fetchall()
        return [row_to_dict(r) for r in rows]


# ---- pricing ---------------------------------------------------------------

def latest_pricing_snapshot(competitor_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        return row_to_dict(conn.execute(
            "SELECT * FROM pricing_snapshots WHERE competitor_id = ? ORDER BY id DESC LIMIT 1", (competitor_id,)
        ).fetchone())


def insert_pricing_snapshot(competitor_id: int, run_id: int, url: str, content_hash: str, prices: list[dict]) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO pricing_snapshots (competitor_id, run_id, url, content_hash, prices_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (competitor_id, run_id, url, content_hash, json.dumps(prices), now_iso()),
        )


# ---- runs ------------------------------------------------------------------

def create_run(competitor_id: int) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO runs (competitor_id, started_at, status, status_detail) VALUES (?, ?, 'running', 'Starting crawl')",
            (competitor_id, now_iso()),
        )
        return int(cur.lastrowid)


def update_run(run_id: int, **fields: Any) -> None:
    if "summary" in fields:
        fields["summary_json"] = json.dumps(fields.pop("summary"))
    cols = ", ".join(f"{k} = ?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE runs SET {cols} WHERE id = ?", (*fields.values(), run_id))


def get_run(run_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        return row_to_dict(conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone())


def list_runs(competitor_id: int, limit: int = 20) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM runs WHERE competitor_id = ? ORDER BY id DESC LIMIT ?", (competitor_id, limit)
        ).fetchall()
        return [row_to_dict(r) for r in rows]
