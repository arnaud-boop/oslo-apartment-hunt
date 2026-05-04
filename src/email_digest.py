"""Daily digest email — sends a mini-digest of NEW listings via Resend.

Triggered at the end of the pipeline after rendering. Per-recipient
personalisation: each address gets the ?v=<voter> URL flavour baked
into the digest link and per-listing eval-page links, so clicking through
takes them straight into the page as the right voter (votes, NEW-tag
acknowledgements, etc. all attribute correctly).

Behavior
--------
- New listings in batch  → mini-digest email with cards + total digest link
- Zero new listings      → minimal heartbeat email + digest link

Auth
----
Reads three env vars (set as GitHub Secrets in CI):
    RESEND_API_KEY     — Resend API key
    EMAIL_TO_ARNAUD    — Arnaud's address
    EMAIL_TO_CELINE    — Céline's address (optional; if unset, only Arnaud)

Failure mode
------------
If RESEND_API_KEY is missing OR all recipient addresses are missing, the
email step skips with a warning. Network or API failures log a warning
but do NOT fail the workflow — the web digest is the primary deliverable.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from jinja2 import Environment, FileSystemLoader, select_autoescape

logger = logging.getLogger(__name__)

RESEND_API_URL = "https://api.resend.com/emails"
REQUEST_TIMEOUT_S = 20


def _voter_url(base_url: str, voter: str) -> str:
    sep = "&" if "?" in base_url else "?"
    return f"{base_url}{sep}v={voter}"


def _listing_url_template(base_url: str, voter: str) -> str:
    """Returns a Python format-string with {finn_id} placeholder."""
    base = base_url.rstrip("/")
    return f"{base}/listings/{{finn_id}}.html?v={voter}"


def _fmt_nok(amount) -> str:
    if amount is None:
        return "—"
    try:
        n = int(amount)
    except (TypeError, ValueError):
        return str(amount)
    if abs(n) >= 1_000_000:
        return f"{n/1_000_000:.2f} M"
    if abs(n) >= 1_000:
        return f"{n/1_000:,.0f} k"
    return f"{n:,}"


def build_email_payload(
    *,
    new_listings: list[dict],
    visible_count: int,
    digest_url: str,
    listing_eval_url_template: str,
    run_dt: datetime,
    template_env: Environment,
) -> dict:
    """Render subject + html + text for one recipient."""
    new_count = len(new_listings)
    if new_count > 0:
        subject = (
            f"🆕 Oslo digest — {new_count} new today "
            f"({visible_count} in your digest)"
        )
    else:
        subject = (
            f"Oslo digest — nothing new today "
            f"({visible_count} in your digest)"
        )

    template = template_env.get_template("email.html.j2")
    html = template.render(
        new_listings=new_listings,
        new_count=new_count,
        visible_count=visible_count,
        digest_url=digest_url,
        listing_eval_url_template=listing_eval_url_template,
        run_iso=run_dt.isoformat(timespec="seconds"),
        run_human=run_dt.strftime("%a %d %b %Y, %H:%M"),
    )

    # Plain-text fallback (some clients prefer it; spam scorers like seeing both)
    text_lines = [
        ("🆕 " + str(new_count) + " new" if new_count else "Nothing new")
        + " in today's Oslo batch",
        f"{run_dt.strftime('%a %d %b %Y, %H:%M')} · {visible_count} visible in your digest",
        "",
    ]
    for s in new_listings:
        l = s.get("listing") or {}
        text_lines.append(
            f"  {s.get('score', 0):.0f}/100  {l.get('title') or '(no title)'}"
        )
        if l.get("address"):
            text_lines.append(f"    {l['address']}")
        text_lines.append(
            f"    {_fmt_nok(l.get('total_price'))} NOK · "
            f"{l.get('area_m2')} m² · {l.get('bedrooms')} BR"
        )
        text_lines.append(
            f"    → {listing_eval_url_template.format(finn_id=l.get('finn_id'))}"
        )
        text_lines.append("")
    text_lines.append(f"→ Full digest: {digest_url}")
    text_lines.append("")
    text_lines.append("Personal use only.")
    text = "\n".join(text_lines)

    return {"subject": subject, "html": html, "text": text}


def _send_via_resend(
    *,
    api_key: str,
    sender: str,
    to: str,
    subject: str,
    html: str,
    text: Optional[str] = None,
) -> bool:
    """POST a single email. Returns True on success, False on any failure."""
    body = {"from": sender, "to": [to], "subject": subject, "html": html}
    if text:
        body["text"] = text
    try:
        r = requests.post(
            RESEND_API_URL,
            json=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=REQUEST_TIMEOUT_S,
        )
    except requests.RequestException as e:
        logger.warning("Resend request failed for %s: %s", to, e)
        return False
    if r.status_code >= 300:
        logger.warning(
            "Resend returned HTTP %d for %s: %s",
            r.status_code,
            to,
            r.text[:200],
        )
        return False
    logger.info("Sent digest email to %s", to)
    return True


def send_digest_email(
    *,
    scored_visible: list[dict],
    new_in_batch: set[str],
    run_dt: datetime,
    config: dict,
    repo_root: Path,
) -> None:
    """Send the daily digest email to each configured recipient.

    `scored_visible` is the same set the web digest renders (already
    excludes both-downvoted listings). New listings are derived from
    `new_in_batch` (set of finn_ids first seen in this batch).

    Recipients and API key come from env vars (see module docstring).
    Anything missing → graceful skip with a single warning.
    """
    cfg = (config.get("email") or {})
    if not cfg.get("active", True):
        logger.info("Email digest disabled in config")
        return

    api_key = os.environ.get("RESEND_API_KEY") or ""
    if not api_key:
        logger.warning("RESEND_API_KEY not set — skipping email digest")
        return

    base_url = (cfg.get("base_url") or "").rstrip("/")
    sender = cfg.get("sender") or "Oslo digest <onboarding@resend.dev>"
    send_on_zero_new = bool(cfg.get("send_on_zero_new", True))

    if not base_url:
        logger.warning("email.base_url not configured — skipping email digest")
        return

    # Recipients — env-only (PII; don't store in repo even as placeholders).
    recipients_by_voter = {
        "arnaud": (os.environ.get("EMAIL_TO_ARNAUD") or "").strip(),
        "celine": (os.environ.get("EMAIL_TO_CELINE") or "").strip(),
    }
    active = {v: addr for v, addr in recipients_by_voter.items() if addr}
    if not active:
        logger.warning(
            "No recipient addresses configured (set EMAIL_TO_ARNAUD / "
            "EMAIL_TO_CELINE env vars) — skipping email digest"
        )
        return

    # Build the new-listings subset, ordered by score desc (already the
    # ordering scored_visible has).
    new_in_batch_set = set(str(x) for x in (new_in_batch or set()))
    new_listings = [
        s for s in scored_visible
        if str((s.get("listing") or {}).get("finn_id") or "") in new_in_batch_set
    ]

    if not new_listings and not send_on_zero_new:
        logger.info("Zero new listings and send_on_zero_new=false — skipping email")
        return

    # Set up Jinja for the email template.
    template_env = Environment(
        loader=FileSystemLoader(str(repo_root / "templates")),
        autoescape=select_autoescape(["html"]),
    )
    template_env.globals.update(fmt_nok=_fmt_nok)

    sent_count = 0
    for voter, addr in active.items():
        digest_url = _voter_url(base_url, voter)
        listing_template = _listing_url_template(base_url, voter)
        payload = build_email_payload(
            new_listings=new_listings,
            visible_count=len(scored_visible),
            digest_url=digest_url,
            listing_eval_url_template=listing_template,
            run_dt=run_dt,
            template_env=template_env,
        )
        if _send_via_resend(
            api_key=api_key,
            sender=sender,
            to=addr,
            subject=payload["subject"],
            html=payload["html"],
            text=payload["text"],
        ):
            sent_count += 1

    logger.info(
        "Email digest: sent %d of %d email(s) (new=%d, visible=%d)",
        sent_count, len(active), len(new_listings), len(scored_visible),
    )
