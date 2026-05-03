"""Scorer — compute a 0-100 score for each filtered listing.

What runs in v1
---------------
Search-result data only. Active sub-scores:
  * Wife's commute (Helsfyr): Haversine distance × 10 km/h ≈ transit minutes,
    mapped to a 0-100 score using the full/partial thresholds in config.
  * Price per m² vs dataset median: cheaper-than-median raises the score,
    pricier lowers it.

Inactive (configured but not applied):
  * Apartment bucket (light, peis, ceiling, …) → needs detail page / LLM
  * Building bucket (family-friendly signals) → needs LLM
  * Quiet-street penalty → needs LLM
  * Real transit time → needs Entur

Weighting
---------
Each sub-score has a budget weight from `weights` and `location` config.
Final score is a *weighted average over active sub-scores only*, renormalized
to 0-100. This way v1 scores span a meaningful range instead of being capped
at the active-weight ceiling.

Tags (no score impact, just labels)
-----------------------------------
  🏫    ≤25 min (Haversine proxy) to current school (Skovveien 9)
  🏫➡️  ≤25 min to next school (Snarøyveien 30)
  ⚖️    ≤30 min to BOTH schools — in-between
  ❌    no school is reachable within thresholds
  🔥    a viewing in the next 48h
  ⚠️    one or more hard filters could not be verified (count from FilterResult)
"""

from __future__ import annotations

import json
import logging
import math
import statistics
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# Walking-and-transit hybrid proxy: 10 km/h means a 3-km commute → 18 min,
# a 5-km one → 30 min. Calibrated against Bolteløkka → Helsfyr (~3 km / 25 min
# real). Replaced with real Entur routing in a later milestone.
KMH_PROXY = 10.0


# ---------------------------------------------------------------- types ----


@dataclass
class SubScore:
    name: str           # short id, e.g. "commute_wife"
    value: float        # 0-100
    weight: float       # weight contribution (from config)
    detail: str         # human-readable line, e.g. "~26 min (Haversine proxy)"


@dataclass
class ScoredListing:
    listing: Any                        # dict (from filter step)
    score: float                        # 0-100, weighted average over active sub-scores
    sub_scores: list[SubScore] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)  # emoji tags, ordered for display
    headline: str = ""                  # one-line "why this scored…" deterministic v1
    unverified: list[str] = field(default_factory=list)  # passed through from filter
    distance_to_current_home_km: float | None = None  # for centrality tiebreaker
    details: dict = field(default_factory=dict)        # ordered dict of computed metrics shown on card

    def to_dict(self) -> dict:
        d = asdict(self)
        d["sub_scores"] = [asdict(s) for s in self.sub_scores]
        return d


# ------------------------------------------------------------- helpers ----


def haversine_km(a: dict, b: dict) -> float:
    """Great-circle distance in km between two {lat, lon} dicts."""
    lat1, lon1 = math.radians(a["lat"]), math.radians(a["lon"])
    lat2, lon2 = math.radians(b["lat"]), math.radians(b["lon"])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(h))


def proxy_transit_minutes(km: float) -> float:
    return km / KMH_PROXY * 60.0


# ----------------------------------------------------------- sub-scores ----


def score_commute_wife(listing: dict, config: dict) -> SubScore | None:
    coords = listing.get("coordinates")
    if not coords or "lat" not in coords or "lon" not in coords:
        return None
    cw = config["location"]["commute_wife"]
    dest = cw["coordinates"]
    km = haversine_km(coords, dest)
    minutes = proxy_transit_minutes(km)

    full_max = cw["full_score_max_minutes"]
    partial_max = cw["partial_score_max_minutes"]

    # Map minutes → 0-100. ≤full_max = 100. (full_max, partial_max] = 50-100 linear.
    # > partial_max: falls off linearly, 0 at partial_max + 30 min.
    if minutes <= full_max:
        value = 100.0
    elif minutes <= partial_max:
        ratio = (partial_max - minutes) / (partial_max - full_max)
        value = 50.0 + 50.0 * ratio
    else:
        over = minutes - partial_max
        value = max(0.0, 50.0 - over * (50.0 / 30.0))

    weight = float(config["weights"]["location_and_commute"])
    return SubScore(
        name="commute_wife",
        value=round(value, 1),
        weight=weight,
        detail=f"~{minutes:.0f} min to Helsfyr ({km:.1f} km, walking+transit proxy)",
    )


def score_price_per_m2(
    listing: dict, config: dict, dataset_stats: dict
) -> SubScore | None:
    """Soft tiebreaker only.

    v1 uses the *global* dataset median, which over-rewards distant suburbs
    where prices/m² are structurally lower regardless of fit. To keep this
    from dominating the ranking, the value is capped to 50 ± 15 (range
    35-65) — meaning price/m² nudges the final score by at most a few
    points, never drives it. v1.x replaces the global median with a
    neighborhood-aware baseline and we can widen the range then.
    """
    area = listing.get("area_m2")
    total = listing.get("total_price")
    if not area or not total:
        return None
    ppm = total / area
    median_ppm = dataset_stats.get("median_ppm")
    if not median_ppm:
        return None
    pct_diff = (ppm - median_ppm) / median_ppm * 100.0
    # 0% diff = 50.  Cap deviation to ±15 over the range ±30%.
    raw = -pct_diff * (15.0 / 30.0)
    value = max(35.0, min(65.0, 50.0 + raw))
    weight = float(config["weights"]["financials"])
    sign = "+" if pct_diff >= 0 else ""
    return SubScore(
        name="price_per_m2",
        value=round(value, 1),
        weight=weight,
        detail=f"{ppm:,.0f} NOK/m² ({sign}{pct_diff:.0f}% vs dataset median, capped ±15)",
    )


# ----------------------------------------------------------------- tags ----


def assign_school_tags(listing: dict, config: dict) -> list[str]:
    """Tags for kept listings (school proximity is now a hard filter, so any
    listing reaching this point is in at least one of the three good zones).

    Priority:
      ⚖️  if both schools within in-between threshold (30 min)
      🏫  if current ≤ 25 min (only)
      🏫➡️ if next ≤ 25 min (only)

    ❌ shouldn't fire — if it does, the filter wasn't applied (config bug
    or coords missing from filter step). Kept as defense-in-depth.
    """
    coords = listing.get("coordinates")
    if not coords:
        return []
    schools = config["location"]["schools"]
    cur = schools["current"]
    nxt = schools["next"]
    cur_min = proxy_transit_minutes(haversine_km(coords, cur["coordinates"]))
    nxt_min = proxy_transit_minutes(haversine_km(coords, nxt["coordinates"]))
    cur_thresh = cur["good_threshold_minutes"]
    nxt_thresh = nxt["good_threshold_minutes"]
    in_between_thresh = schools["in_between_threshold_minutes"]

    cur_ok = cur_min <= cur_thresh
    nxt_ok = nxt_min <= nxt_thresh
    in_between = cur_min <= in_between_thresh and nxt_min <= in_between_thresh

    if cur_ok and nxt_ok:
        return ["⚖️ In between"]
    if cur_ok:
        return ["🏫 Current school"]
    if nxt_ok:
        return ["🏫➡️ Future school"]
    if in_between:
        return ["⚖️ In between"]
    # Defense-in-depth: filter should have caught this.
    return ["❌ Poor for both schools"]


def assign_visning_tag(listing: dict, config: dict, now: datetime) -> str | None:
    hours = config.get("tags", {}).get("visning_hot_within_hours", 48)
    cutoff = now + timedelta(hours=hours)
    for v in listing.get("viewing_times", []):
        try:
            t = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        except ValueError:
            continue
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        if now <= t <= cutoff:
            return f"🔥 visning < {hours}h"
    return None


def assign_unverified_tag(unverified: list[str]) -> str | None:
    if not unverified:
        return None
    return f"⚠️ {len(unverified)} unverified"


# ---------------------------------------------------- dataset statistics ----


def compute_dataset_stats(listings: list[dict]) -> dict:
    """Aggregate stats used by relative scoring (price/m² median, etc.)."""
    ppm = []
    for l in listings:
        a = l.get("area_m2")
        t = l.get("total_price")
        if a and t and a > 0:
            ppm.append(t / a)
    if not ppm:
        return {"median_ppm": None}
    return {
        "median_ppm": statistics.median(ppm),
        "n": len(ppm),
        "min_ppm": min(ppm),
        "max_ppm": max(ppm),
    }


# ----------------------------------------------------------- compose -----


def _make_headline(sub_scores: list[SubScore]) -> str:
    """Deterministic v1 'why this scored…' line.

    Don't fire strong/weak when sub-scores are within 10 points of each other
    — saying one is "weak" at 100/100 alongside another at 100/100 is silly.
    LLM-generated vibe summary lands in v1.1.
    """
    if not sub_scores:
        return "no scoring signals available"
    if len(sub_scores) == 1:
        s = sub_scores[0]
        return f"only signal: {s.name} {s.value:.0f}/100"
    ordered = sorted(sub_scores, key=lambda s: s.value)
    weak = ordered[0]
    strong = ordered[-1]
    spread = strong.value - weak.value
    if spread < 10:
        avg = sum(s.value for s in sub_scores) / len(sub_scores)
        if avg >= 75:
            return f"balanced: all signals strong (avg {avg:.0f}/100)"
        if avg <= 35:
            return f"balanced: all signals weak (avg {avg:.0f}/100)"
        return f"balanced: {avg:.0f}/100 across signals"
    return (
        f"strong: {strong.name} {strong.value:.0f}/100, "
        f"weak: {weak.name} {weak.value:.0f}/100"
    )


def score_listing(
    filter_result: dict,
    config: dict,
    dataset_stats: dict,
    now: datetime,
) -> ScoredListing:
    """`filter_result` is a kept FilterResult flattened to dict
    ({listing, passed, failed, unverified})."""
    listing = filter_result["listing"]

    sub_scores: list[SubScore] = []
    for fn in (
        lambda: score_commute_wife(listing, config),
        lambda: score_price_per_m2(listing, config, dataset_stats),
    ):
        s = fn()
        if s is not None:
            sub_scores.append(s)

    if sub_scores:
        total_w = sum(s.weight for s in sub_scores)
        score = sum(s.value * s.weight for s in sub_scores) / total_w
    else:
        score = 0.0

    tags: list[str] = []
    tags.extend(assign_school_tags(listing, config))
    vt = assign_visning_tag(listing, config, now)
    if vt:
        tags.insert(0, vt)
    ut = assign_unverified_tag(filter_result.get("unverified", []))
    if ut:
        tags.append(ut)

    # Distance to current home (centrality tiebreaker).
    dist_home: float | None = None
    coords = listing.get("coordinates")
    home = config.get("location", {}).get("current_home", {}).get("coordinates")
    if coords and home:
        dist_home = round(haversine_km(coords, home), 2)

    # Detail metrics rendered alongside the card (insertion order preserved).
    details: dict[str, str] = {}
    if coords and home:
        details["From current home"] = f"{haversine_km(coords, home):.1f} km"
    schools = config.get("location", {}).get("schools", {})
    cur_school = schools.get("current") or {}
    nxt_school = schools.get("next") or {}
    if coords and cur_school.get("coordinates"):
        km = haversine_km(coords, cur_school["coordinates"])
        details[f"→ Current school ({cur_school.get('address','')})"] = (
            f"~{proxy_transit_minutes(km):.0f} min · {km:.1f} km"
        )
    if coords and nxt_school.get("coordinates"):
        km = haversine_km(coords, nxt_school["coordinates"])
        details[f"→ Future school ({nxt_school.get('address','')})"] = (
            f"~{proxy_transit_minutes(km):.0f} min · {km:.1f} km"
        )
    cw = config.get("location", {}).get("commute_wife", {})
    if coords and cw.get("coordinates"):
        km = haversine_km(coords, cw["coordinates"])
        details[f"→ Wife's commute ({cw.get('destination','')})"] = (
            f"~{proxy_transit_minutes(km):.0f} min · {km:.1f} km"
        )
    if listing.get("area_m2") and listing.get("total_price"):
        ppm = listing["total_price"] / listing["area_m2"]
        details["Price per m²"] = f"{ppm:,.0f} NOK"
    if listing.get("plot_m2"):
        details["Plot size"] = f"{listing['plot_m2']:.0f} m²"
    ts = listing.get("timestamp_ms")
    if ts:
        try:
            posted = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
            details["Listed"] = posted.strftime("%Y-%m-%d")
        except (OSError, ValueError, OverflowError):
            pass

    return ScoredListing(
        listing=listing,
        score=round(score, 1),
        sub_scores=sub_scores,
        tags=tags,
        headline=_make_headline(sub_scores),
        unverified=list(filter_result.get("unverified", [])),
        distance_to_current_home_km=dist_home,
        details=details,
    )


def score_listings(
    filter_results: list[dict], config: dict, now: datetime | None = None
) -> list[ScoredListing]:
    if now is None:
        now = datetime.now(timezone.utc)
    listings = [r["listing"] for r in filter_results]
    stats = compute_dataset_stats(listings)
    if stats.get("median_ppm"):
        logger.info(
            "Dataset stats: n=%d, median_ppm=%.0f NOK/m²",
            stats["n"],
            stats["median_ppm"],
        )
    out = [score_listing(r, config, stats, now) for r in filter_results]
    # Primary: score desc.
    # Tiebreaker: distance to current home asc (closer = more central = higher).
    # listings with no coords sort to the end of any tie.
    out.sort(
        key=lambda s: (
            -s.score,
            s.distance_to_current_home_km
            if s.distance_to_current_home_km is not None
            else float("inf"),
        )
    )
    return out


# ----------------------------------------------------------------- main ----


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    repo_root = Path(__file__).resolve().parent.parent

    # Load config
    config = yaml.safe_load(
        (repo_root / "config" / "scoring_config.yaml").read_text()
    )

    # Get filter output by re-running filters in-process
    sys.path.insert(0, str(repo_root))
    from src.filters import apply_hard_filters

    listings_path = repo_root / "data" / "listings_latest.json"
    if not listings_path.exists():
        print(
            f"Missing {listings_path}. Run `python -m src.scraper` first.",
            file=sys.stderr,
        )
        return 1

    listings = json.loads(listings_path.read_text())
    kept, _dropped = apply_hard_filters(listings, config)

    kept_dicts = [
        {"listing": k.listing, "passed": True, "failed": k.failed,
         "unverified": k.unverified}
        for k in kept
    ]

    scored = score_listings(kept_dicts, config)

    print(f"\n=== Top 10 ranked listings ===\n")
    for s in scored[:10]:
        l = s.listing
        title = (l.get("title") or "")[:80]
        dist = (f"{s.distance_to_current_home_km:.1f}km from home"
                if s.distance_to_current_home_km is not None else "no coords")
        print(
            f"  [{s.score:5.1f}] finn={l.get('finn_id')} | "
            f"{l.get('property_type')} | {l.get('area_m2')}m² | "
            f"{l.get('total_price'):,} NOK | {dist}"
        )
        print(f"      {title}")
        print(f"      tags: {' '.join(s.tags) or '(none)'}")
        for sub in s.sub_scores:
            print(f"      {sub.name:14s} {sub.value:5.1f}  ({sub.detail})")
        print(f"      {s.headline}")
        print()

    print(f"\n=== Bottom 5 ranked listings ===\n")
    for s in scored[-5:]:
        l = s.listing
        title = (l.get("title") or "")[:80]
        print(
            f"  [{s.score:5.1f}] finn={l.get('finn_id')} | "
            f"{l.get('property_type')} | {l.get('area_m2')}m² | "
            f"{l.get('total_price'):,} NOK"
        )
        print(f"      {title}")
        for sub in s.sub_scores:
            print(f"      {sub.name:14s} {sub.value:5.1f}  ({sub.detail})")
        print()

    # Write scored output
    out_path = repo_root / "data" / "scored_latest.json"
    out_path.write_text(
        json.dumps([s.to_dict() for s in scored], indent=2, ensure_ascii=False)
    )
    print(f"Wrote {out_path} ({len(scored)} scored listings)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
