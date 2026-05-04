"""Grocery proximity check via Overpass API (OpenStreetMap).

For each listing, queries Overpass for `shop=supermarket` within the
configured radius, classifies each find by chain (approved / excluded /
other), and merges the result into the listing dict under
`grocery_check`. Drives the criteria checklist's grocery row and
contributes to the overall score via `score_grocery` in the scorer.

Approved / excluded chain lists are configurable in scoring_config.yaml
(`grocery.approved_chains`, `grocery.excluded_chains`). Defaults match
what the user described as "real supermarkets" vs convenience stores.

Data source: https://overpass-api.de — free, no auth. Be polite (we use
~1 req/sec). Cache: .cache/grocery.json keyed by rounded coords (4 dp ≈
11 m), 30-day TTL.

Failure mode: any per-listing Overpass error or parse failure passes the
listing through without a `grocery_check` field; downstream code (scorer
+ criteria) treats that as "unverified" and doesn't penalise the listing.
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
CACHE_DIR = Path(".cache")
CACHE_FILE = CACHE_DIR / "grocery.json"
CACHE_TTL_DAYS = 30

USER_AGENT = (
    "oslo-apartment-hunt/0.1 (personal use; arnaud.dupuis@farmforce.com)"
)
RATE_LIMIT_S = 1.0
COORDS_PRECISION = 4

DEFAULT_RADIUS_M = 1000  # query a bit wider than the walking threshold
DEFAULT_WALKING_THRESHOLD_M = 800  # ~10 min at 4.8 km/h

# Defaults — overridable in scoring_config.yaml.
DEFAULT_APPROVED = [
    "kiwi", "meny", "coop mega", "rema 1000", "coop extra", "spar",
]
DEFAULT_EXCLUDED = [
    "joker", "bunnpris", "coop prix",
    "narvesen", "7-eleven", "deli de luca",
]

CONNECT_TIMEOUT_S = 5
READ_TIMEOUT_S = 25
CIRCUIT_FAILURE_THRESHOLD = 3

_session: Optional[requests.Session] = None
_cache: Optional[dict] = None
_last_fetch_t: float = 0.0
_consecutive_failures: int = 0
_circuit_open: bool = False


# ---------------------------------------------------------- helpers ----


def _haversine_km(a: dict, b: dict) -> float:
    lat1, lon1 = math.radians(a["lat"]), math.radians(a["lon"])
    lat2, lon2 = math.radians(b["lat"]), math.radians(b["lon"])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(h))


def _norm(s: str) -> str:
    return (s or "").lower().strip()


def _match_chain(text: str, chain: str) -> bool:
    """Return True if all tokens of `chain` appear as whole words in `text`.

    Word-boundary matching (not substring) so "spar" doesn't match
    "Sparhaugen", "rema" doesn't match "Cinema", etc.
    """
    text_l = _norm(text)
    for token in chain.lower().split():
        if not re.search(r"\b" + re.escape(token) + r"\b", text_l):
            return False
    return True


def classify_shop(
    shop_tags: dict,
    approved: list[str],
    excluded: list[str],
) -> tuple[str, Optional[str]]:
    """Classify a shop by its OSM tags. Returns (class, matched_chain).

    `class`: "approved" / "excluded" / "other"
    `matched_chain`: which chain string from the lists matched (or None)
    """
    name = shop_tags.get("name") or ""
    brand = shop_tags.get("brand") or ""
    haystack = f"{name} {brand}"

    # Try longer chain names first so "coop mega" matches before a
    # hypothetical generic "coop" entry.
    for chain in sorted(approved, key=len, reverse=True):
        if _match_chain(haystack, chain):
            return ("approved", chain)
    for chain in sorted(excluded, key=len, reverse=True):
        if _match_chain(haystack, chain):
            return ("excluded", chain)
    return ("other", None)


# ---------------------------------------------------------- caching ----


def _load_cache() -> dict:
    if not CACHE_FILE.exists():
        return {}
    try:
        return json.loads(CACHE_FILE.read_text())
    except json.JSONDecodeError:
        logger.warning("grocery cache corrupt; starting fresh")
        return {}


def _save_cache(cache: dict) -> None:
    CACHE_DIR.mkdir(exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache, indent=2, ensure_ascii=False))


def _coord_key(coords: dict, radius_m: int) -> str:
    return (
        f"{coords['lat']:.{COORDS_PRECISION}f},"
        f"{coords['lon']:.{COORDS_PRECISION}f}@{radius_m}"
    )


def _fresh(entry: dict) -> bool:
    ts = entry.get("fetched_at")
    if not ts:
        return False
    try:
        fetched = datetime.fromisoformat(ts)
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return datetime.now(timezone.utc) - fetched < timedelta(days=CACHE_TTL_DAYS)


# ---------------------------------------------------------- fetch ----


def _build_session() -> requests.Session:
    global _session
    if _session is None:
        s = requests.Session()
        s.headers.update({"User-Agent": USER_AGENT})
        _session = s
    return _session


def _fetch_overpass(coords: dict, radius_m: int) -> Optional[list]:
    """Query Overpass for shop=supermarket near `coords`. Returns list of
    OSM elements, or None on failure.
    """
    global _last_fetch_t
    elapsed = time.monotonic() - _last_fetch_t
    if elapsed < RATE_LIMIT_S:
        time.sleep(RATE_LIMIT_S - elapsed)
    _last_fetch_t = time.monotonic()

    query = (
        f"[out:json][timeout:25];"
        f"("
        f'  node["shop"="supermarket"](around:{radius_m},{coords["lat"]},{coords["lon"]});'
        f'  way["shop"="supermarket"](around:{radius_m},{coords["lat"]},{coords["lon"]});'
        f");"
        f"out tags center;"
    )

    global _consecutive_failures, _circuit_open
    if _circuit_open:
        return None
    try:
        r = _build_session().post(
            OVERPASS_URL,
            data={"data": query},
            timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S),
        )
        r.raise_for_status()
        data = r.json()
    except requests.RequestException as e:
        logger.warning("Overpass request failed for %s: %s", coords, e)
        _consecutive_failures += 1
        if _consecutive_failures >= CIRCUIT_FAILURE_THRESHOLD:
            logger.warning(
                "Overpass: %d consecutive failures — opening circuit "
                "breaker, remaining listings skip the grocery check",
                _consecutive_failures,
            )
            _circuit_open = True
        return None
    except ValueError as e:
        logger.warning("Overpass response not JSON: %s", e)
        return None
    _consecutive_failures = 0
    return data.get("elements") or []


# --------------------------------------------------------- analyze ----


def analyze_listing(
    coords: dict,
    approved: list[str],
    excluded: list[str],
    *,
    radius_m: int = DEFAULT_RADIUS_M,
) -> Optional[dict]:
    """Run a full per-listing grocery proximity analysis.

    Returns a dict with classified shops sorted by distance, or None on
    Overpass failure.
    """
    elements = _fetch_overpass(coords, radius_m)
    if elements is None:
        return None

    approved_list: list[dict] = []
    excluded_list: list[dict] = []
    other_list: list[dict] = []

    for el in elements:
        tags = el.get("tags") or {}
        if "lat" in el and "lon" in el:
            shop_lat, shop_lon = el["lat"], el["lon"]
        else:
            center = el.get("center") or {}
            shop_lat = center.get("lat")
            shop_lon = center.get("lon")
        if shop_lat is None or shop_lon is None:
            continue
        dist_m = int(round(
            _haversine_km(coords, {"lat": shop_lat, "lon": shop_lon}) * 1000
        ))
        cls, chain = classify_shop(tags, approved, excluded)
        item = {
            "name": tags.get("name") or tags.get("brand") or "(unnamed)",
            "chain": chain,
            "distance_m": dist_m,
        }
        if cls == "approved":
            approved_list.append(item)
        elif cls == "excluded":
            excluded_list.append(item)
        else:
            other_list.append(item)

    approved_list.sort(key=lambda x: x["distance_m"])
    excluded_list.sort(key=lambda x: x["distance_m"])
    other_list.sort(key=lambda x: x["distance_m"])

    return {
        "approved_count": len(approved_list),
        "approved_list": approved_list,
        "excluded_count": len(excluded_list),
        "excluded_list": excluded_list,
        "other_count": len(other_list),
        "other_list": other_list,
        "radius_m": radius_m,
    }


# --------------------------------------------------------- pipeline ---


def enrich_with_grocery(
    listings: list[dict],
    *,
    config: Optional[dict] = None,
) -> list[dict]:
    """For each listing with coordinates, run the grocery check and merge
    the result. Failures pass the listing through without `grocery_check`
    (downstream treats as 'unverified')."""
    global _cache
    if _cache is None:
        _cache = _load_cache()

    cfg = (config or {}).get("grocery") or {}
    if not cfg.get("active", True):
        logger.info("Grocery filter disabled in config")
        return listings

    approved = cfg.get("approved_chains") or DEFAULT_APPROVED
    excluded = cfg.get("excluded_chains") or DEFAULT_EXCLUDED
    radius = int(cfg.get("query_radius_m", DEFAULT_RADIUS_M))

    out: list[dict] = []
    fetched = 0
    cached = 0
    skipped = 0
    failed = 0

    for l in listings:
        merged = dict(l)
        coords = l.get("coordinates")
        if not coords or "lat" not in coords or "lon" not in coords:
            skipped += 1
            out.append(merged)
            continue

        key = _coord_key(coords, radius)
        if key in _cache and _fresh(_cache[key]):
            data = _cache[key].get("data")
            if data:
                merged["grocery_check"] = data
                cached += 1
            out.append(merged)
            continue

        result = analyze_listing(coords, approved, excluded, radius_m=radius)
        if result is None:
            failed += 1
            out.append(merged)
            continue

        _cache[key] = {
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "data": result,
        }
        merged["grocery_check"] = result
        out.append(merged)
        fetched += 1
        if fetched % 10 == 0:
            _save_cache(_cache)

    _save_cache(_cache)
    logger.info(
        "Grocery: %d fetched, %d cached, %d skipped (no coords), %d failed",
        fetched, cached, skipped, failed,
    )
    return out
