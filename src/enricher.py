"""Detail-page enricher (v1.0.5).

Fetches each kept listing's individual Finn ad page, extracts structured
fields the search-results page can't give us, and merges them back into
the listing dict.

Pipeline position
-----------------
    scrape (537) → filter pass 1 (search-result only, ~73 kept) →
    THIS (~73 detail-page fetches, rate-limited) →
    filter pass 2 (detail-page filters now applicable) → score → render.

Caching
-------
Detail pages don't change often. Cached at .cache/enrichment.json keyed
by finn_id with a 7-day TTL. Daily reruns only fetch newly-appeared
listings.

What we extract (high confidence — usable as hard filters)
----------------------------------------------------------
    floor (int)                        — 1 = ground floor (1. etasje)
    disposed (bool)                    — True = sold/withdrawn
    has_elevator (bool)                — "Heis" in facilities
    description (str)                  — summaryUnsafe with HTML stripped
    fixer_upper_in_description (bool)  — "oppussingsobjekt"/"renoveringsobjekt"
                                         in description text
    construction_year (int)
    energy_class (str)                 — "A".."G"
    facilities (list[str])             — feature flags

What we extract (lower confidence — keep as ⚠️ unverified)
----------------------------------------------------------
    has_bod_evidence (bool)            — keyword scan, false-positive prone
    has_washing_machine_evidence (bool)
    orientation_mentions (list[str])

Inspect mode
------------
    python -m src.enricher <finn_id>
        Fetches one detail page, dumps debug artifacts to .cache/.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup

# Reuse from the search-results scraper.
from src.scraper import (
    USER_AGENT,
    REQUEST_TIMEOUT_S,
    RATE_LIMIT_S,
    ScrapeError,
    _build_session,
    _resolve_turbo_stream,
)

CACHE_DIR = Path(".cache")
CACHE_FILE = CACHE_DIR / "enrichment.json"
CACHE_TTL_DAYS = 7
AD_URL_TEMPLATE = "https://www.finn.no/realestate/homes/ad.html?finnkode={finn_id}"

logger = logging.getLogger(__name__)


# Non-anchored regex (the search-results page anchored to end-of-string,
# but detail pages have multiple statements after the enqueue() call).
_ENQUEUE_RE = re.compile(
    r'streamController\.enqueue\((".*?(?<!\\)")\)', re.DOTALL
)

_FIXER_UPPER_RE = re.compile(
    r"\b(oppussingsobjekt|renoveringsobjekt)\b", re.IGNORECASE
)

# Floor-mention regex — used as a fallback when the structured `floor` field
# is blank. Norwegian agents sometimes leave the structured field empty
# (bypassing Finn's floor filter) but still write "1. etg" / "2. etasje" /
# "i 5. etasje" in the description, since it's required to be disclosed.
# Matches the most common forms; intentionally tight to avoid false positives
# from things like "i 1. halvår" or "16,5 m².etasje".
_FLOOR_RE = re.compile(
    r"\b(\d{1,2})\.?\s*(?:etg|etasje)\b",
    re.IGNORECASE,
)

# Heuristic bod keywords. False-positive prone (could match "boder" in a
# negative context, or other "bod*" words). Kept as ⚠️ evidence, not a filter.
_BOD_RE = re.compile(r"\b(bod(?:er|areal|en)?|kjellerbod|loftbod)\b", re.IGNORECASE)

_WASHING_RE = re.compile(
    r"\b(vaskemaskin|vaskeromopplegg|opplegg\s+for\s+vask|vaskerom)\b",
    re.IGNORECASE,
)

_ORIENTATION_PATTERNS = [
    ("south", re.compile(r"sør\s*vendt|sørvendt|sør-vendt|sydvendt", re.IGNORECASE)),
    ("west",  re.compile(r"vest\s*vendt|vestvendt|vest-vendt", re.IGNORECASE)),
    ("east",  re.compile(r"øst\s*vendt|østvendt|øst-vendt", re.IGNORECASE)),
    ("north", re.compile(r"nord\s*vendt|nordvendt|nord-vendt", re.IGNORECASE)),
]


# ----------------------------------------------------------------- fetch ----


def fetch_ad_html(finn_id: str, session: requests.Session) -> str:
    url = AD_URL_TEMPLATE.format(finn_id=finn_id)
    logger.info("GET %s", url)
    resp = session.get(url, timeout=REQUEST_TIMEOUT_S)
    if resp.status_code != 200:
        raise ScrapeError(
            f"Finn returned HTTP {resp.status_code} for {url} "
            f"(body len {len(resp.text)})"
        )
    if not resp.text or len(resp.text) < 1000:
        raise ScrapeError(
            f"Finn returned suspiciously small body ({len(resp.text)} chars) for {url}"
        )
    return resp.text


def parse_ad_tree(html: str) -> dict:
    """Decode the React-Router turbo-stream payload into a Python dict tree."""
    soup = BeautifulSoup(html, "lxml")
    last_err: Exception | None = None
    for s in soup.find_all("script"):
        if s.get("src") or s.get("type"):
            continue
        text = s.string or ""
        if "streamController.enqueue" not in text:
            continue
        # A detail-page script may contain multiple enqueue() chunks; pick the
        # first one that successfully decodes. Loader payloads are large.
        for m in _ENQUEUE_RE.finditer(text):
            try:
                inner = json.loads(m.group(1))
                arr = json.loads(inner)
                if isinstance(arr, list) and len(arr) > 100:
                    return _resolve_turbo_stream(arr)
            except (json.JSONDecodeError, AttributeError) as e:
                last_err = e
                continue
    raise ScrapeError(
        f"No usable streamController.enqueue() payload on detail page "
        f"(last error: {last_err})"
    )


def find_ad(tree: dict) -> dict | None:
    """Locate objectData.ad in the resolved tree."""
    ld = tree.get("loaderData") or {}
    for key, val in ld.items():
        if "homes.ad" not in key or not isinstance(val, dict):
            continue
        obj = val.get("objectData") or {}
        ad = obj.get("ad")
        if isinstance(ad, dict):
            return ad
    return None


# --------------------------------------------------------- field plucking ----


def _strip_html(s: str | None) -> str:
    if not s:
        return ""
    return re.sub(r"<[^>]+>", " ", s).strip()


def _general_text_map(ad: dict) -> dict[str, str]:
    """Flatten generalText (list of {heading, textUnsafe}) to {heading: text}."""
    out: dict[str, str] = {}
    for g in ad.get("generalText") or []:
        h = (g.get("heading") or "").strip()
        if not h:
            continue
        out[h] = _strip_html(g.get("textUnsafe", "")).strip()
    return out


def extract_enrichment(ad: dict) -> dict:
    """Pull v1.0.5 fields from a Finn ad detail-page dict."""
    facilities = list(ad.get("facilities") or [])
    description = _strip_html(ad.get("summaryUnsafe", ""))
    general = _general_text_map(ad)

    out: dict[str, Any] = {}

    # Floor — prefer the structured field; fall back to a regex over the
    # description and the structured fact-table when the agent left the
    # floor field blank (a known cheat to bypass Finn's floor filter).
    structured_floor = ad.get("floor")
    out["floor"] = structured_floor
    out["floor_source"] = "structured" if structured_floor is not None else None
    if structured_floor is None:
        haystack = description + "\n" + "\n".join(general.values())
        # First match wins; floor mentions in actual context tend to come early.
        m = _FLOOR_RE.search(haystack)
        if m:
            try:
                candidate = int(m.group(1))
            except ValueError:
                candidate = None
            if candidate is not None and 1 <= candidate <= 30:
                out["floor"] = candidate
                out["floor_source"] = "description_regex"
                logger.info(
                    "floor extracted from description (no structured field): "
                    "ad %s → floor %d (matched: %r)",
                    ad.get("adId"),
                    candidate,
                    m.group(0),
                )

    out["disposed"] = bool(ad.get("disposed", False))
    out["has_elevator"] = "Heis" in facilities
    out["construction_year"] = ad.get("constructionYear")
    out["facilities"] = facilities
    out["description"] = description
    el = ad.get("energyLabel") or {}
    out["energy_class"] = el.get("class")

    # Hard filter: fixer-upper keyword in description.
    out["fixer_upper_in_description"] = bool(_FIXER_UPPER_RE.search(description))

    # Soft / advisory signals (kept as ⚠️ context, not hard filters in v1.0.5).
    out["has_bod_evidence"] = bool(
        _BOD_RE.search(description) or any(_BOD_RE.search(v or "") for v in general.values())
    )
    out["has_washing_machine_evidence"] = bool(
        _WASHING_RE.search(description) or any(_WASHING_RE.search(v or "") for v in general.values())
    )
    orientations = [name for name, pat in _ORIENTATION_PATTERNS if pat.search(description)]
    out["orientation_mentions"] = orientations
    out["primary_orientation_north_only"] = (
        bool(orientations)
        and "north" in orientations
        and "south" not in orientations
        and "west" not in orientations
    )

    # Useful contextual info, surface-level passthrough.
    out["shared_cost_includes"] = ((ad.get("sharedCost") or {}).get("includesUnsafe") or "").strip()
    out["property_type_detail"] = ad.get("propertyType")
    out["ownership_type_detail"] = ad.get("ownershipType")

    # Detail-page image URLs — far richer than the 3-photo set the search
    # results page exposes. Ad has up to 50+ images. Override the search-
    # result value with this richer list (downstream eval-page carousel
    # uses up to 10).
    detail_images = ad.get("images") or []
    detail_image_urls = []
    for img in detail_images:
        if isinstance(img, dict) and img.get("url"):
            detail_image_urls.append(img["url"])
        elif isinstance(img, str):
            detail_image_urls.append(img)
    if detail_image_urls:
        out["image_urls"] = detail_image_urls

    # Selected generalText sections — useful context for LLM analysis.
    # (Not all sections; the boring legal/admin ones are skipped.)
    INTERESTING = {
        "Standard", "Tilstand", "Beliggenhet / servicetilbud",
        "Beskrivelse av bebyggelsen", "Oppvarming / Teknisk",
        "Faste kostnader", "Adkomst", "Uteområde",
        "Innbo og løsøre", "Parkering / Garasje",
        "Sammendrag fra selgers egenerklæring",
    }
    out["general_text"] = {k: v for k, v in general.items() if k in INTERESTING and v}

    return out


# ----------------------------------------------------------------- cache ----


def _load_cache() -> dict:
    if not CACHE_FILE.exists():
        return {}
    try:
        cache = json.loads(CACHE_FILE.read_text())
    except json.JSONDecodeError:
        logger.warning("enrichment cache corrupt; ignoring")
        return {}
    # Backfill the floor_source field on entries written before the v1.1
    # description-regex fallback. Avoids needing to re-fetch detail pages
    # just to apply the new logic to cached entries.
    _backfill_floor_source(cache)
    return cache


def _backfill_floor_source(cache: dict) -> int:
    """Apply the description-regex floor fallback to cached entries that
    don't have it yet. In-place. Returns count of entries patched."""
    patched = 0
    rescued = 0
    for entry in cache.values():
        if not isinstance(entry, dict) or "floor_source" in entry:
            continue
        floor = entry.get("floor")
        if floor is not None:
            entry["floor_source"] = "structured"
            patched += 1
            continue
        haystack = (entry.get("description") or "") + "\n" + "\n".join(
            (entry.get("general_text") or {}).values()
        )
        m = _FLOOR_RE.search(haystack)
        if m:
            try:
                candidate = int(m.group(1))
            except ValueError:
                candidate = None
            if candidate is not None and 1 <= candidate <= 30:
                entry["floor"] = candidate
                entry["floor_source"] = "description_regex"
                patched += 1
                rescued += 1
                continue
        entry["floor_source"] = "none"
        patched += 1
    if rescued:
        logger.info(
            "Backfilled floor from description for %d cached listing(s) "
            "(of %d cache entries patched)", rescued, patched
        )
    return patched


def _save_cache(cache: dict) -> None:
    CACHE_DIR.mkdir(exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache, indent=2, ensure_ascii=False))


def _cache_fresh(entry: dict) -> bool:
    ts = entry.get("_fetched_at")
    if not ts:
        return False
    try:
        fetched = datetime.fromisoformat(ts)
    except ValueError:
        return False
    return datetime.now(timezone.utc) - fetched < timedelta(days=CACHE_TTL_DAYS)


# ----------------------------------------------------------- public API ----


def enrich_one(
    listing: dict, session: requests.Session, cache: dict
) -> dict:
    """Return a copy of `listing` merged with detail-page fields."""
    finn_id = str(listing.get("finn_id") or "")
    if not finn_id:
        return dict(listing)

    if finn_id in cache and _cache_fresh(cache[finn_id]):
        enriched = dict(listing)
        enriched.update({k: v for k, v in cache[finn_id].items() if not k.startswith("_")})
        return enriched

    html = fetch_ad_html(finn_id, session)
    tree = parse_ad_tree(html)
    ad = find_ad(tree)
    if ad is None:
        raise ScrapeError(f"Could not locate ad data for {finn_id}")
    fields = extract_enrichment(ad)
    fields["_fetched_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cache[finn_id] = fields

    enriched = dict(listing)
    enriched.update({k: v for k, v in fields.items() if not k.startswith("_")})
    return enriched


def enrich_listings(
    listings: list[dict],
    *,
    rate_limit_s: float = RATE_LIMIT_S,
    save_cache_every: int = 10,
) -> list[dict]:
    """Batch-enrich. Polite rate limit, persisted cache.

    Failures on individual listings are logged and the listing passes through
    un-enriched (with an unverified flag). The whole run does NOT abort.
    """
    cache = _load_cache()
    session = _build_session()
    out: list[dict] = []
    failed = 0
    fetched_count = 0
    cached_count = 0

    for i, l in enumerate(listings):
        finn_id = str(l.get("finn_id") or "")
        cached_hit = finn_id in cache and _cache_fresh(cache[finn_id])
        try:
            enriched = enrich_one(l, session, cache)
            if cached_hit:
                cached_count += 1
            else:
                fetched_count += 1
        except ScrapeError as e:
            logger.warning("Failed to enrich %s: %s", finn_id, e)
            failed += 1
            enriched = dict(l)
            enriched["_enrichment_failed"] = str(e)
            # Treat enrichment failure as "withdrawn/sold" so the existing
            # exclude_sold_or_under_offer filter drops it from the digest.
            # Common case: 404 means the ad was pulled between scrape and
            # detail fetch. Network blips also drop here — accepted trade-off
            # for v1; revisit with retry logic if false-drop rate grows.
            enriched["disposed"] = True
        out.append(enriched)

        # Save cache periodically so a crash doesn't lose progress.
        if (i + 1) % save_cache_every == 0:
            _save_cache(cache)

        # Rate limit only between actual network fetches.
        if not cached_hit and i < len(listings) - 1:
            time.sleep(rate_limit_s)

    _save_cache(cache)
    logger.info(
        "Enrichment summary: %d fetched, %d from cache, %d failed (of %d)",
        fetched_count,
        cached_count,
        failed,
        len(listings),
    )
    return out


# ----------------------------------------------------------- inspect mode ----


def inspect_one(finn_id: str) -> int:
    CACHE_DIR.mkdir(exist_ok=True)
    session = _build_session()
    html = fetch_ad_html(finn_id, session)
    (CACHE_DIR / f"ad_{finn_id}.html").write_text(html)
    tree = parse_ad_tree(html)
    (CACHE_DIR / f"ad_{finn_id}.resolved.json").write_text(
        json.dumps(tree, indent=2, ensure_ascii=False, default=str)
    )
    ad = find_ad(tree)
    if not ad:
        print("Could not locate ad data.", file=sys.stderr)
        return 1
    fields = extract_enrichment(ad)
    print(f"\n=== Extracted enrichment for finn={finn_id} ===\n")
    for k, v in fields.items():
        if isinstance(v, str) and len(v) > 80:
            print(f"  {k}: {v[:80]!r}…")
        elif isinstance(v, list):
            print(f"  {k}: {v[:6]}{' …' if len(v) > 6 else ''}")
        else:
            print(f"  {k}: {v}")
    return 0


# ------------------------------------------------------------------- main ----


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    if len(sys.argv) < 2:
        print(
            "Usage:\n"
            "  python -m src.enricher <finn_id>            # inspect one ad\n"
            "  python -m src.enricher --enrich-from-file   # enrich data/listings_kept.json",
            file=sys.stderr,
        )
        return 2

    arg = sys.argv[1]
    if arg == "--enrich-from-file":
        repo_root = Path(__file__).resolve().parent.parent
        os.chdir(repo_root)
        path = repo_root / "data" / "listings_kept.json"
        if not path.exists():
            print(f"Missing {path}", file=sys.stderr)
            return 1
        listings = json.loads(path.read_text())
        enriched = enrich_listings(listings)
        out = repo_root / "data" / "listings_enriched.json"
        out.write_text(json.dumps(enriched, indent=2, ensure_ascii=False))
        print(f"Wrote {out} ({len(enriched)} enriched listings)")
        return 0

    return inspect_one(arg)


if __name__ == "__main__":
    raise SystemExit(main())
