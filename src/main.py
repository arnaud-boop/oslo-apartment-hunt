"""Daily orchestration entrypoint.

Pipeline:
  1. Read finn-search-url.txt
  2. Scrape all pages of the Finn search
  3. Filter pass 1: search-result-only filters (price, area, beds, school, etc.)
  4. Enrich kept listings: detail-page fetches (cached, rate-limited)
  5. Filter pass 2: detail-page filters now apply (floor, sold, fixer-upper-desc)
  6. Score the survivors
  7. Render dist/index.html

Run locally:        python -m src.main
GitHub Actions:     same command, daily cron in CI.

Set SKIP_SCRAPE=1 to reuse data/listings_latest.json (faster iteration).
Set SKIP_ENRICH=1 to reuse data/listings_enriched.json.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from src.email_digest import send_digest_email
from src.enricher import enrich_listings
from src.filters import apply_hard_filters, load_config
from src.generator import render
from src.grocery import enrich_with_grocery
from src.history import update_history
from src.llm import analyze_listings
from src.salgsoppgave import enrich_with_salgsoppgave
from src.scorer import score_listings
from src.votes import fetch_votes

logger = logging.getLogger(__name__)


def _read_search_url(repo_root: Path) -> str:
    p = repo_root / "finn-search-url.txt"
    if not p.exists():
        raise FileNotFoundError(
            f"Missing {p}. Drop the live Finn search URL in there."
        )
    return p.read_text().strip().splitlines()[0].strip()


def _save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    repo_root = Path(__file__).resolve().parent.parent
    os.chdir(repo_root)

    config = load_config(repo_root / "config" / "scoring_config.yaml")
    data_dir = repo_root / "data"
    data_dir.mkdir(exist_ok=True)

    skip_scrape = os.environ.get("SKIP_SCRAPE") == "1"
    skip_enrich = os.environ.get("SKIP_ENRICH") == "1"

    # ---------------------------------------------------- 1. SCRAPE ----
    listings_path = data_dir / "listings_latest.json"
    if skip_scrape and listings_path.exists():
        listings_dicts = json.loads(listings_path.read_text())
        logger.info(
            "SKIP_SCRAPE=1 — reusing %s (%d listings)",
            listings_path,
            len(listings_dicts),
        )
    else:
        from src.scraper import fetch_search_results
        url = _read_search_url(repo_root)
        logger.info("=== 1/6 Scrape ===")
        logger.info("Search URL: %s", url)
        listings = fetch_search_results(url, debug=False)
        listings_dicts = [l.to_dict() for l in listings]
        _save_json(listings_path, listings_dicts)
        logger.info("Scraped %d listings", len(listings_dicts))
    scraped_count = len(listings_dicts)

    # ----------------------------------------------- 1.5 HISTORY UPDATE ----
    # Track first/last-seen per finn_id; identify the new arrivals in this
    # batch. This runs against the raw scrape (pre-filter) so we don't lose
    # newness signal for listings dropped by hard filters.
    _history, new_in_batch = update_history(listings_dicts)
    logger.info(
        "History: %d new arrivals in today's batch (of %d scraped)",
        len(new_in_batch),
        scraped_count,
    )

    # --------------------------------------- 2. FILTER PASS 1 (cheap) ----
    logger.info("=== 2/6 Filter pass 1 (search-result fields) ===")
    kept_p1, dropped_p1 = apply_hard_filters(listings_dicts, config)
    logger.info(
        "Pass 1: %d kept, %d dropped (of %d)",
        len(kept_p1),
        len(dropped_p1),
        scraped_count,
    )
    pass1_kept = [r.listing for r in kept_p1]
    _save_json(data_dir / "listings_kept.json", pass1_kept)

    # ---------------------------------------------------- 3. ENRICH ----
    enriched_path = data_dir / "listings_enriched.json"
    if skip_enrich and enriched_path.exists():
        enriched = json.loads(enriched_path.read_text())
        logger.info(
            "SKIP_ENRICH=1 — reusing %s (%d listings)",
            enriched_path,
            len(enriched),
        )
    else:
        logger.info(
            "=== 3/6 Enrich (detail-page fetches, ~%d×rate-limit s) ===",
            len(pass1_kept),
        )
        enriched = enrich_listings(pass1_kept)
        _save_json(enriched_path, enriched)

    # ------------------------------------------- 3.3 GROCERY (Overpass) ----
    # Query OpenStreetMap for nearby supermarkets, classify by chain.
    # Annotation-only (doesn't drop listings); score contribution via
    # `weights.grocery`.
    gro_cfg = config.get("grocery", {}) or {}
    if gro_cfg.get("active", True):
        logger.info("=== 3.3/6 Grocery proximity (Overpass / OSM) ===")
        enriched = enrich_with_grocery(enriched, config=config)
        _save_json(enriched_path, enriched)
    else:
        logger.info("Grocery filter disabled in config — skipping")

    # -------------------------------------- 3.4 SALGSOPPGAVE EXTRACTION ----
    # Fetch + LLM-extract structured facts from each listing's broker
    # salgsoppgave (full sales prospectus). Feeds the criteria checklist
    # (bedroom sizes, wet rooms, bod, washing machine) and provides richer
    # context (TG ratings, renovation history, heating details, etc.).
    salgs_cfg = config.get("salgsoppgave", {}) or {}
    if salgs_cfg.get("active", True):
        logger.info("=== 3.4/6 Salgsoppgave extraction (broker prospectus) ===")
        enriched = enrich_with_salgsoppgave(enriched, config=config)
        _save_json(enriched_path, enriched)
    else:
        logger.info("Salgsoppgave extraction disabled in config — skipping")

    # ------------------------------------------ 3.5 LLM ANALYSIS PASS ----
    # Optional. Skipped silently if ANTHROPIC_API_KEY is not set or the
    # llm config section is `active: false`.
    llm_cfg = config.get("llm", {}) or {}
    if llm_cfg.get("active", True):
        logger.info(
            "=== 3.5/6 LLM analysis (vibe + layout dealbreaker + apartment signals) ==="
        )
        enriched = analyze_listings(enriched, config=llm_cfg)
        _save_json(enriched_path, enriched)
    else:
        logger.info("LLM analysis disabled in config — skipping")

    # ----------------------------------- 4. FILTER PASS 2 (post-enrich) ----
    logger.info("=== 4/6 Filter pass 2 (detail-page + LLM filters) ===")
    kept_p2, dropped_p2 = apply_hard_filters(enriched, config)
    logger.info(
        "Pass 2: %d kept, %d dropped (of %d)",
        len(kept_p2),
        len(dropped_p2),
        len(enriched),
    )

    # Aggregate dropped reasons for the log so we can see what the new filters caught.
    from collections import Counter
    reasons = Counter()
    for r in dropped_p2:
        for f in r.failed:
            head = f.split("(")[0].strip()
            reasons[head] += 1
    if reasons:
        logger.info("Pass 2 drop reasons (top):")
        for reason, count in reasons.most_common(10):
            logger.info("  %4d  %s", count, reason)

    # ----------------------------------------------------- 5. SCORE ----
    logger.info("=== 5/6 Score ===")
    kept_p2_dicts = [
        {
            "listing": r.listing,
            "passed": True,
            "failed": r.failed,
            "unverified": r.unverified,
        }
        for r in kept_p2
    ]
    # Pass the full scrape as the neighborhood-median baseline — gives a
    # denser geographic surface than the ~tens of post-filter survivors.
    scored = score_listings(
        kept_p2_dicts, config, baseline_listings=listings_dicts
    )
    _save_json(
        data_dir / "scored_latest.json",
        [s.to_dict() for s in scored],
    )

    # ---------------------------------------------------- 6. RENDER ----
    logger.info("=== 6/6 Render HTML ===")
    out_dir = repo_root / "dist"
    final_dropped = scraped_count - len(scored)
    voting_cfg = config.get("voting", {}) or {}
    voting_endpoint = voting_cfg.get("web_app_url", "") or ""
    votes = fetch_votes(voting_endpoint) if voting_endpoint else {}
    run_dt = datetime.now(timezone.utc)
    render(
        [s.to_dict() for s in scored],
        repo_root,
        scraped_count=scraped_count,
        dropped_count=final_dropped,
        run_dt=run_dt,
        out_dir=out_dir,
        votes=votes,
        voting_endpoint=voting_endpoint,
        new_in_batch=new_in_batch,
        config=config,
    )

    # ---------------------------------------------- 7. EMAIL DIGEST ----
    # Send a mini-digest email with NEW arrivals (heartbeat email if zero
    # new). Per-recipient ?v= URL personalization. Skips silently if env
    # vars are missing. Same scored list as the renderer; we recompute the
    # visible/hidden split inside email_digest.py to keep email and web
    # in sync.
    scored_dicts = [s.to_dict() for s in scored]
    visible_for_email = []
    for s in scored_dicts:
        fid = str((s.get("listing") or {}).get("finn_id") or "")
        v = (votes or {}).get(fid) or {}
        a = ((v.get("arnaud") or {}).get("vote") or "").lower()
        c = ((v.get("celine") or {}).get("vote") or "").lower()
        if not (a == "down" and c == "down"):
            visible_for_email.append(s)
    try:
        send_digest_email(
            scored_visible=visible_for_email,
            new_in_batch=new_in_batch,
            run_dt=run_dt,
            config=config,
            repo_root=repo_root,
        )
    except Exception as e:
        # Email failure must not fail the workflow — the web digest is the
        # primary deliverable.
        logger.warning("Email digest step failed (continuing): %s", e)

    logger.info(
        "DONE. Pipeline: %d scraped → %d after pass 1 → %d enriched → %d final.",
        scraped_count,
        len(kept_p1),
        len(enriched),
        len(scored),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
