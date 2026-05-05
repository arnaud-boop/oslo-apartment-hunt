"""Public-transport routing via Entur (Norway's national journey planner).

Replaces the Haversine × 10 km/h proxy with real door-to-door transit time
using Entur's free GraphQL API: https://developer.entur.org/

Per Entur's free-use policy, requests must include an ET-Client-Name header
identifying the integration. We set it to a project-specific value.

Caching
-------
Transit times rarely change. Cached at .cache/transit.json, keyed by
rounded coordinates with a 30-day TTL. Daily reruns hit cache for almost
everything; only newly-appeared listings trigger fresh queries.

Reference time
--------------
Queries are sent for the next upcoming Monday at 07:30 Oslo time — a
realistic school-morning commute. The reference rolls forward day-to-day
but the cache keeps actual API hits rare.

Failure mode
------------
On any error (network, GraphQL, malformed response), `transit_minutes`
returns None. Callers should fall back to the Haversine proxy via
`commute_minutes`, which handles that automatically.
"""

from __future__ import annotations

import json
import logging
import math
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)

ENTUR_URL = "https://api.entur.io/journey-planner/v3/graphql"
ET_CLIENT_NAME = "arnaud-boop-oslo-apartment-hunt"

CACHE_DIR = Path(".cache")
CACHE_FILE = CACHE_DIR / "transit.json"
CACHE_TTL_DAYS = 30
COORDS_PRECISION = 4   # decimal places for cache key (≈11 m at this latitude)

# ~3 req/sec — well under Entur's documented limits, polite for an unfunded
# free service.
RATE_LIMIT_S = 0.3
PROXY_KMH = 10.0  # fallback "speed" for Haversine-based estimate

# Connection + read timeouts. Tighter on connect so a network block fails
# fast rather than hanging.
CONNECT_TIMEOUT_S = 5
READ_TIMEOUT_S = 15

# Circuit breaker: if Entur is unreachable, don't grind through 200+ slow
# failures — switch to proxy-only mode after this many consecutive failures.
CIRCUIT_FAILURE_THRESHOLD = 3

# Module-level singletons. Lazy-initialised on first call.
_session: Optional[requests.Session] = None
_cache: Optional[dict] = None
_last_fetch_t: float = 0.0
_consecutive_failures: int = 0
_circuit_open: bool = False


# ----------------------------------------------------------- haversine ----


def haversine_km(a: dict, b: dict) -> float:
    """Great-circle distance in km between two {lat, lon} dicts."""
    lat1, lon1 = math.radians(a["lat"]), math.radians(a["lon"])
    lat2, lon2 = math.radians(b["lat"]), math.radians(b["lon"])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(h))


def proxy_transit_minutes(km: float) -> float:
    return km / PROXY_KMH * 60.0


# ----------------------------------------------------------- caching -----


def _load_cache() -> dict:
    if not CACHE_FILE.exists():
        return {}
    try:
        return json.loads(CACHE_FILE.read_text())
    except json.JSONDecodeError:
        logger.warning("transit cache corrupt; ignoring")
        return {}


def _save_cache(cache: dict) -> None:
    CACHE_DIR.mkdir(exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache, indent=2))


def _coord_key(from_c: dict, to_c: dict) -> str:
    return (
        f"{from_c['lat']:.{COORDS_PRECISION}f},"
        f"{from_c['lon']:.{COORDS_PRECISION}f}"
        f"->"
        f"{to_c['lat']:.{COORDS_PRECISION}f},"
        f"{to_c['lon']:.{COORDS_PRECISION}f}"
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
    age = datetime.now(timezone.utc) - fetched
    return age < timedelta(days=CACHE_TTL_DAYS)


# ----------------------------------------------------------- entur API ---


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        s = requests.Session()
        s.headers.update(
            {
                "ET-Client-Name": ET_CLIENT_NAME,
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        )
        _session = s
    return _session


def _next_monday_morning_utc_iso() -> str:
    """Stable reference time: next Mon 07:30 Oslo (= 05:30 or 06:30 UTC)."""
    now = datetime.now(timezone.utc)
    days_ahead = (0 - now.weekday()) % 7  # 0 == Monday
    if days_ahead == 0 and now.hour >= 7:
        days_ahead = 7
    target = (now + timedelta(days=days_ahead)).replace(
        hour=5, minute=30, second=0, microsecond=0
    )
    # 'Z' suffix for clarity (Entur accepts both forms).
    return target.strftime("%Y-%m-%dT%H:%M:%SZ")


_QUERY = """
query ($from: Location!, $to: Location!, $dateTime: DateTime!) {
  trip(
    from: $from
    to: $to
    dateTime: $dateTime
    numTripPatterns: 3
  ) {
    tripPatterns {
      duration
      legs {
        mode
        duration
        line {
          publicCode
          transportMode
        }
        fromPlace { name }
        toPlace { name }
      }
    }
  }
}
"""

# Compact icons per Entur transport mode.
_MODE_ICON = {
    "bus": "🚌",
    "tram": "🚊",
    "metro": "🚇",
    "rail": "🚆",
    "water": "⛴️",
    "foot": "🚶",
    "bicycle": "🚲",
    "car": "🚗",
}


def _summarize_legs(legs: list) -> str:
    """Compact one-liner like '🚌 21 + 🚇 5' from a tripPattern's legs.
    Walking legs are omitted unless the entire trip is on foot.
    """
    transit = [l for l in legs if l.get("mode") != "foot"]
    if not transit:
        # All-foot trip.
        total_min = sum((l.get("duration") or 0) for l in legs) / 60.0
        return f"🚶 {total_min:.0f} min walk"
    parts = []
    for leg in transit:
        icon = _MODE_ICON.get(leg.get("mode", ""), "🚉")
        line_code = ((leg.get("line") or {}).get("publicCode") or "?")
        parts.append(f"{icon} {line_code}")
    return " + ".join(parts)


def _query_entur(from_c: dict, to_c: dict) -> Optional[dict]:
    """Hit Entur. Returns {minutes, summary, legs} for the shortest trip
    pattern, or None on any failure."""
    global _last_fetch_t
    # Rate limit between actual fetches.
    elapsed = time.monotonic() - _last_fetch_t
    if elapsed < RATE_LIMIT_S:
        time.sleep(RATE_LIMIT_S - elapsed)
    _last_fetch_t = time.monotonic()

    variables = {
        "from": {
            "coordinates": {
                "latitude": float(from_c["lat"]),
                "longitude": float(from_c["lon"]),
            }
        },
        "to": {
            "coordinates": {
                "latitude": float(to_c["lat"]),
                "longitude": float(to_c["lon"]),
            }
        },
        "dateTime": _next_monday_morning_utc_iso(),
    }
    try:
        r = _get_session().post(
            ENTUR_URL,
            json={"query": _QUERY, "variables": variables},
            timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S),
        )
        r.raise_for_status()
        data = r.json()
    except requests.RequestException as e:
        logger.warning("Entur request failed: %s", e)
        return None
    except ValueError as e:
        logger.warning("Entur response not JSON: %s", e)
        return None

    if "errors" in data and data["errors"]:
        logger.warning("Entur GraphQL errors: %s", data["errors"][:2])
        return None
    patterns = (data.get("data") or {}).get("trip", {}).get("tripPatterns") or []
    if not patterns:
        return None
    valid = [
        p for p in patterns if isinstance(p.get("duration"), (int, float))
    ]
    if not valid:
        return None
    best = min(valid, key=lambda p: p["duration"])
    legs = best.get("legs") or []
    return {
        "minutes": best["duration"] / 60.0,
        "summary": _summarize_legs(legs),
        "legs": legs,
    }


# ----------------------------------------------------------- public API --


def transit_details(
    from_c: Optional[dict], to_c: Optional[dict]
) -> Optional[dict]:
    """Real Entur trip data, cached. Returns {minutes, summary, legs} or None.

    Cache may also hold older entries written before the legs/summary fields
    were added — those return just {minutes} (summary will be missing).
    """
    if not isinstance(from_c, dict) or not isinstance(to_c, dict):
        return None
    if "lat" not in from_c or "lon" not in from_c:
        return None
    if "lat" not in to_c or "lon" not in to_c:
        return None
    global _cache, _consecutive_failures, _circuit_open
    if _cache is None:
        _cache = _load_cache()

    key = _coord_key(from_c, to_c)
    entry = _cache.get(key)
    if entry and _fresh(entry) and entry.get("minutes") is not None:
        # Post-v1.1.x cache entries include legs; older entries are
        # minutes-only. For old entries we'd like to re-fetch to get the
        # leg summary, but only if Entur is currently reachable.
        if "legs" in entry:
            return {
                "minutes": entry["minutes"],
                "summary": entry.get("summary"),
                "legs": entry.get("legs"),
            }
        if _circuit_open:
            # Entur is currently unreachable — return what we have (no summary).
            return {"minutes": entry["minutes"], "summary": None, "legs": None}
        # else: fall through to re-fetch and replace this entry.

    # Circuit breaker — once Entur has failed enough times in a row, stop
    # trying for the rest of this process. Caller falls back to proxy.
    if _circuit_open:
        return None

    result = _query_entur(from_c, to_c)
    if result is not None:
        _consecutive_failures = 0
        _cache[key] = {
            **result,
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        _save_cache(_cache)
    else:
        _consecutive_failures += 1
        if _consecutive_failures >= CIRCUIT_FAILURE_THRESHOLD:
            logger.warning(
                "Entur: %d consecutive failures — opening circuit breaker, "
                "remaining queries will use Haversine proxy",
                _consecutive_failures,
            )
            _circuit_open = True
    return result


def transit_minutes(from_c: Optional[dict], to_c: Optional[dict]) -> Optional[float]:
    """Convenience wrapper around `transit_details` returning just minutes."""
    d = transit_details(from_c, to_c)
    return d["minutes"] if d else None


def commute_minutes(from_c: Optional[dict], to_c: Optional[dict]) -> Optional[float]:
    """Best-effort transit minutes: Entur if reachable, Haversine proxy otherwise.

    Returns None only if input coords are missing.
    """
    if not isinstance(from_c, dict) or not isinstance(to_c, dict):
        return None
    real = transit_minutes(from_c, to_c)
    if real is not None:
        return real
    try:
        return proxy_transit_minutes(haversine_km(from_c, to_c))
    except (KeyError, TypeError, ValueError):
        return None


def commute_details(
    from_c: Optional[dict], to_c: Optional[dict]
) -> Optional[dict]:
    """Best-effort details: Entur transit_details if reachable, Haversine
    proxy {minutes only} otherwise.
    """
    if not isinstance(from_c, dict) or not isinstance(to_c, dict):
        return None
    real = transit_details(from_c, to_c)
    if real is not None:
        return real
    try:
        mins = proxy_transit_minutes(haversine_km(from_c, to_c))
    except (KeyError, TypeError, ValueError):
        return None
    return {"minutes": mins, "summary": None, "legs": None}


# --------------------------- post-2029 projection (Fornebubanen) -----------


def projected_post2029_to_fornebu(
    listing_coords: Optional[dict],
    scenario2_cfg: Optional[dict],
) -> Optional[dict]:
    """Project the post-2029 commute from a listing to Snarøyveien 30 (the
    future French school in Fornebu) via the new Fornebubanen metro line.

    Modelled as:
      min(transit→Majorstua, transit→Skøyen) + metro_minutes + final_walk_minutes

    Lysaker and the Fornebu terminus itself are intentionally NOT used as
    interchange candidates — they're effectively at the destination, so
    routing through them would just be wrong arithmetic.

    Reuses `commute_minutes` (Entur cache + Haversine fallback) for the
    listing→interchange leg, so this is essentially free after first run.

    Returns a dict with the breakdown (for display on eval pages), or None
    if input coords are missing or every interchange lookup failed.
    """
    if not isinstance(listing_coords, dict):
        return None
    fcfg = (scenario2_cfg or {}).get("fornebubanen") or {}
    interchanges = fcfg.get("interchanges") or {}
    if not interchanges:
        return None
    metro_min = float(fcfg.get("metro_minutes", 12))
    walk_min = float(fcfg.get("final_walk_minutes", 5))

    best_name: Optional[str] = None
    best_to_interchange: Optional[float] = None
    for name, coords in interchanges.items():
        m = commute_minutes(listing_coords, coords)
        if m is None:
            continue
        if best_to_interchange is None or m < best_to_interchange:
            best_to_interchange = m
            best_name = name

    if best_to_interchange is None:
        return None

    return {
        "best_interchange": best_name,
        "transit_to_interchange_min": float(best_to_interchange),
        "metro_min": metro_min,
        "walk_min": walk_min,
        "total_min": float(best_to_interchange) + metro_min + walk_min,
    }


def projected_post2029_minutes(
    listing_coords: Optional[dict],
    scenario2_cfg: Optional[dict],
) -> Optional[float]:
    """Convenience wrapper returning just the total minutes."""
    d = projected_post2029_to_fornebu(listing_coords, scenario2_cfg)
    return d["total_min"] if d else None
