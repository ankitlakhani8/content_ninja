"""Alert delivery: Slack incoming webhook and/or SMTP email.

Both are optional. The run summary is always shown in the dashboard, so a
missing channel is reported, not treated as a failure."""
from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage
from typing import Any

import httpx


def _fmt_volume(post: dict[str, Any]) -> str:
    vol = post.get("search_volume")
    if vol is None:
        return "vol n/a"
    cpc = post.get("cpc")
    extra = f", CPC ${cpc:.2f}" if isinstance(cpc, (int, float)) else ""
    return f"{int(vol):,} searches/mo{extra}"


def build_summary(domain: str, run: dict[str, Any]) -> tuple[str, str]:
    """Returns (headline, body_markdown) used for Slack, email and the UI."""
    new_posts: list[dict[str, Any]] = run.get("new_posts") or []
    pricing = run.get("pricing") or {}
    status = run.get("status")

    if status in ("blocked", "no_blog", "unreachable", "error"):
        headline = f"Competitor {domain}: check could not complete ({status.replace('_', ' ')})"
        return headline, run.get("status_detail") or ""

    if run.get("is_baseline"):
        headline = (f"Competitor {domain}: baseline captured, {run.get('total_posts_found', 0)} posts indexed, "
                    f"{len(new_posts)} published in the last 7 days")
    else:
        headline = f"Competitor {domain} published {len(new_posts)} new post{'s' if len(new_posts) != 1 else ''} since the last check"

    lines: list[str] = []
    for p in new_posts[:15]:
        date = (p.get("published_at") or "")[:10] or "date unknown"
        kw = p.get("keyword") or "-"
        lines.append(f"• <{p['url']}|{p.get('title') or p['url']}> ({date})\n    keyword: _{kw}_ · {_fmt_volume(p)}")
    if len(new_posts) > 15:
        lines.append(f"…and {len(new_posts) - 15} more")
    if not new_posts:
        lines.append("No new posts.")

    if pricing.get("found"):
        if pricing.get("changed"):
            diff = pricing.get("diff") or {}
            added = ", ".join(f"{d['plan']} {d['price']}".strip() for d in diff.get("added", [])) or "none"
            removed = ", ".join(f"{d['plan']} {d['price']}".strip() for d in diff.get("removed", [])) or "none"
            lines.append(f"\n*Pricing changed* (<{pricing['url']}|pricing page>)\n    added: {added}\n    removed: {removed}")
        else:
            lines.append(f"\nPricing: {pricing.get('detail', 'unchanged')}")
    else:
        lines.append("\nPricing: no pricing page found")

    kw_status = run.get("keyword_status") or {}
    if kw_status and not kw_status.get("ok"):
        lines.append(f"\n_Keyword data: {kw_status.get('detail')}_")
    return headline, "\n".join(lines)


async def send_slack(webhook: str, headline: str, body: str) -> tuple[bool, str]:
    payload = {
        "text": headline,
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": headline[:150]}},
            {"type": "section", "text": {"type": "mrkdwn", "text": body[:2900] or "(no details)"}},
        ],
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(webhook, json=payload)
    except httpx.HTTPError as exc:
        return False, f"Slack webhook unreachable: {type(exc).__name__}"
    if resp.status_code == 200:
        return True, "Slack alert delivered"
    return False, f"Slack webhook rejected the message (HTTP {resp.status_code}: {resp.text[:120]})"


def send_email(headline: str, body: str) -> tuple[bool, str]:
    host = os.environ.get("SMTP_HOST")
    to = os.environ.get("ALERT_EMAIL_TO")
    if not host or not to:
        return False, "Email not configured"
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER", "")
    password = os.environ.get("SMTP_PASSWORD", "")
    sender = os.environ.get("SMTP_FROM", user or "competitor-monitor@localhost")
    msg = EmailMessage()
    msg["Subject"] = headline
    msg["From"] = sender
    msg["To"] = to
    # Convert Slack link syntax to plain "title - url" for email.
    import re
    plain = re.sub(r"<([^|>]+)\|([^>]+)>", r"\2 - \1", body).replace("*", "").replace("_", "")
    msg.set_content(plain)
    try:
        with smtplib.SMTP(host, port, timeout=20) as smtp:
            smtp.starttls()
            if user:
                smtp.login(user, password)
            smtp.send_message(msg)
        return True, f"Email sent to {to}"
    except Exception as exc:
        return False, f"Email failed: {type(exc).__name__}: {exc}"[:200]


async def dispatch(domain: str, run: dict[str, Any], slack_webhook: str | None) -> dict[str, Any]:
    headline, body = build_summary(domain, run)
    webhook = (slack_webhook or os.environ.get("SLACK_WEBHOOK_URL") or "").strip()
    results: list[str] = []
    sent = False
    if webhook:
        ok, detail = await send_slack(webhook, headline, body)
        sent = sent or ok
        results.append(detail)
    if os.environ.get("SMTP_HOST") and os.environ.get("ALERT_EMAIL_TO"):
        ok, detail = send_email(headline, body)
        sent = sent or ok
        results.append(detail)
    if not results:
        results.append("No alert channel configured (set SLACK_WEBHOOK_URL or paste a webhook in the form); summary shown here instead.")
    return {"sent": sent, "detail": "; ".join(results), "headline": headline, "body": body}
