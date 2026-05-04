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
import statistics
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

# Real public-transport routing with a Haversine proxy fallback.
from src.routing import (
    commute_details,
    commute_minutes,
    haversine_km,
    proxy_transit_minutes,
)

logger = logging.getLogger(__name__)


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


# ----------------------------------------------------------- sub-scores ----


def score_commute_celine(listing: dict, config: dict) -> SubScore | None:
    coords = listing.get("coordinates")
    if not coords or "lat" not in coords or "lon" not in coords:
        return None
    cc = config["location"]["commute_celine"]
    dest = cc["coordinates"]
    info = commute_details(coords, dest)
    if not info or info.get("minutes") is None:
        return None
    minutes = info["minutes"]
    summary = info.get("summary")
    km = haversine_km(coords, dest)

    full_max = cc["full_score_max_minutes"]
    partial_max = cc["partial_score_max_minutes"]

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
    detail = f"~{minutes:.0f} min to Helsfyr ({km:.1f} km)"
    if summary:
        detail += f" via {summary}"
    return SubScore(
        name="commute_celine",
        value=round(value, 1),
        weight=weight,
        detail=detail,
    )


def score_grocery(listing: dict, config: dict) -> SubScore | None:
    """Distance-based grocery score, fed by Overpass (src/grocery.py).

    Curve:
      ≤ 400 m → 100
      400–800 m → linear 100 → 50  (the 10-min walk boundary)
      800–1500 m → linear 50 → 0
      > 1500 m → 0
      Excluded-only nearby (Bunnpris/Joker/etc.) → 25
      No supermarket at all → 0

    Returns None if grocery_check data is missing — bucket is then
    excluded from the weighted average rather than penalising the listing.
    """
    grocery = listing.get("grocery_check")
    if not grocery:
        return None

    weight = float(config.get("weights", {}).get("grocery", 8))
    approved = grocery.get("approved_list") or []
    excluded = grocery.get("excluded_list") or []

    if approved:
        d = approved[0]["distance_m"]
        if d <= 400:
            value = 100.0
        elif d <= 800:
            value = 100.0 - (d - 400) / 400.0 * 50.0
        elif d <= 1500:
            value = 50.0 - (d - 800) / 700.0 * 50.0
        else:
            value = 0.0
        more = grocery.get("approved_count", 1) - 1
        more_suffix = f" (+{more} more)" if more > 0 else ""
        detail = (
            f"{approved[0]['name']} ({approved[0]['chain']}) "
            f"{d} m{more_suffix}"
        )
    elif excluded:
        nearest = excluded[0]
        value = 25.0
        detail = (
            f"only {nearest['name']} ({nearest['chain']}) "
            f"{nearest['distance_m']} m — no real supermarket nearby"
        )
    else:
        value = 0.0
        detail = "no supermarket within 1 km"

    return SubScore(
        name="grocery",
        value=round(value, 1),
        weight=weight,
        detail=detail,
    )


def score_apartment(listing: dict, config: dict) -> SubScore | None:
    """Apartment-quality subscore. Sum of binary signals (each weighted by
    config), normalised to 0-100. Skips entirely if `apartment.active_in_v1`
    is false or if there's no LLM data to score against."""
    apt_cfg = config.get("apartment", {}) or {}
    if not apt_cfg.get("active_in_v1"):
        return None
    llm = listing.get("llm") or {}
    if not llm:
        return None  # no LLM data → can't meaningfully score this bucket

    facilities = set(listing.get("facilities") or [])

    out_cfg = apt_cfg.get("outdoor_space", {}) or {}
    light_cfg = apt_cfg.get("light_orientation", {}) or {}
    ceiling_cfg = apt_cfg.get("ceiling_height", {}) or {}
    reno_cfg = apt_cfg.get("recent_renovation", {}) or {}
    heat_cfg = apt_cfg.get("heating", {}) or {}

    # (label, condition, points). Negative points are penalties; they are NOT
    # added to max_points (so the realistic max is the sum of positives).
    signals = [
        ("balcony/terrace", "Balkong/Terrasse" in facilities,
         out_cfg.get("balcony_or_terrace_bonus", 0)),
        ("bakgård/garden", llm.get("has_bakgaard") is True,
         out_cfg.get("private_garden_bonus", 0)),
        ("south-facing", llm.get("main_orientation") == "south",
         light_cfg.get("south_main_bonus", 0)),
        ("west-facing", llm.get("main_orientation") == "west",
         light_cfg.get("west_main_bonus", 0)),
        ("high ceilings", llm.get("ceiling_height_high") is True,
         ceiling_cfg.get("bonus", 0)),
        ("peis", "Peis/Ildsted" in facilities,
         apt_cfg.get("fireplace_peis_bonus", 0)),
        ("bathtub", llm.get("has_bathtub") is True,
         apt_cfg.get("bathtub_bonus", 0)),
        ("open-plan kitchen", llm.get("has_open_plan_kitchen") is True,
         apt_cfg.get("open_plan_kitchen_bonus", 0)),
        ("recent renovation", llm.get("renovated_within_5_years") is True,
         reno_cfg.get("bonus", 0)),
        ("parking", "Garasje/P-plass" in facilities,
         apt_cfg.get("parking_bonus", 0)),
        ("clean layout", llm.get("layout_clean") is True,
         apt_cfg.get("clean_layout_bonus", 0)),
        ("no vis-à-vis", llm.get("has_visavi") is False,
         apt_cfg.get("no_visavi_bonus", 0)),
        ("collective heat", llm.get("heating_type") == "collective",
         heat_cfg.get("collective_bonus", 0)),
    ]
    penalties = [
        ("individual electric heat",
         llm.get("heating_type") == "individual_electric",
         heat_cfg.get("individual_electric_penalty", 0)),
    ]

    raw = 0
    hits: list[str] = []
    for label, present, bonus in signals:
        if present and bonus:
            raw += bonus
            hits.append(label)
    for label, present, penalty in penalties:
        if present and penalty:
            raw += penalty   # penalty is negative
            hits.append(f"{label} (-)")

    max_pos = sum(b for _, _, b in signals if b > 0)
    if max_pos <= 0:
        return None

    value = max(0.0, min(100.0, raw / max_pos * 100.0))
    weight = float(config["weights"]["apartment"])
    return SubScore(
        name="apartment",
        value=round(value, 1),
        weight=weight,
        detail=(
            f"{len(hits)} signal(s): {', '.join(hits[:5])}"
            + (f" (+{len(hits)-5} more)" if len(hits) > 5 else "")
            if hits
            else "no positive signals detected"
        ),
    )


def score_building(listing: dict, config: dict) -> SubScore | None:
    """Building/neighbourhood subscore. Family-friendly + quiet-street."""
    bld_cfg = config.get("building", {}) or {}
    if not bld_cfg.get("active_in_v1"):
        return None
    llm = listing.get("llm") or {}
    if not llm:
        return None
    facilities = set(listing.get("facilities") or [])

    family_friendly = (
        llm.get("family_friendly") is True
        or "Barnevennlig" in facilities
    )
    on_busy = llm.get("on_busy_street")  # True / False / None

    fam_bonus = bld_cfg.get("family_friendly_bonus", 8)
    quiet_bonus = 4
    busy_penalty = -8

    raw = 0
    hits: list[str] = []
    if family_friendly:
        raw += fam_bonus
        hits.append("family-friendly")
    if on_busy is False:
        raw += quiet_bonus
        hits.append("quiet street")
    elif on_busy is True:
        raw += busy_penalty
        hits.append("busy street (-)")

    max_pos = fam_bonus + quiet_bonus
    if max_pos <= 0:
        return None
    value = max(0.0, min(100.0, raw / max_pos * 100.0))
    weight = float(config["weights"]["building"])
    return SubScore(
        name="building",
        value=round(value, 1),
        weight=weight,
        detail=(", ".join(hits) if hits else "no positive signals"),
    )


def score_price_per_m2(
    listing: dict, config: dict, dataset_stats: dict
) -> SubScore | None:
    """Compare listing price/m² to its neighborhood baseline.

    v1.x: per-listing local median computed via K-nearest-neighbors over
    the broader scrape baseline. Falls back to the global median when no
    local median is available (no coords or too few neighbors).

    Score range: 0-100 linear over ±30% deviation from local median.
    No cap — the suburb-bias problem is solved by comparing apples to
    apples within neighborhoods.
    """
    area = listing.get("area_m2")
    total = listing.get("total_price")
    if not area or not total:
        return None
    ppm = total / area

    # Prefer per-listing local median, fall back to global.
    local_medians = dataset_stats.get("local_medians") or {}
    finn_id = listing.get("finn_id")
    local_median = local_medians.get(finn_id)
    used_local = local_median is not None
    median_ppm = local_median if used_local else dataset_stats.get("median_ppm")
    if not median_ppm:
        return None

    pct_diff = (ppm - median_ppm) / median_ppm * 100.0
    # 0% diff = 50.  -30% = 100. +30% = 0. Clamped to [0, 100].
    value = max(0.0, min(100.0, 50.0 - pct_diff * (50.0 / 30.0)))
    weight = float(config["weights"]["financials"])
    sign = "+" if pct_diff >= 0 else ""
    baseline_label = "neighborhood median" if used_local else "Oslo dataset median"
    return SubScore(
        name="price_per_m2",
        value=round(value, 1),
        weight=weight,
        detail=f"{ppm:,.0f} NOK/m² ({sign}{pct_diff:.0f}% vs {baseline_label})",
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
    cur_min = commute_minutes(coords, cur["coordinates"])
    nxt_min = commute_minutes(coords, nxt["coordinates"])
    if cur_min is None or nxt_min is None:
        return []
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


def compute_dataset_stats(
    listings: list[dict],
    baseline_listings: list[dict] | None = None,
    k_nearest: int = 15,
) -> dict:
    """Aggregate stats used by relative scoring.

    Computes:
      - Global median price/m² over the baseline (fallback baseline).
      - Per-listing local median price/m² via K-nearest-neighbors over the
        baseline. Indexed by finn_id.

    `baseline_listings` should ideally be the broader scraped set (~hundreds
    of listings), giving a denser geographic surface than the post-filter
    survivors alone. Defaults to `listings` if not provided.
    """
    if baseline_listings is None:
        baseline_listings = listings

    # Pre-extract baseline coords + ppm (skip listings with bad data).
    baseline: list[tuple[str, float, float, float]] = []  # (finn_id, lat, lon, ppm)
    for b in baseline_listings:
        coords = b.get("coordinates") or {}
        a = b.get("area_m2")
        t = b.get("total_price")
        if (
            isinstance(a, (int, float)) and a > 0
            and isinstance(t, (int, float)) and t > 0
            and "lat" in coords and "lon" in coords
            and isinstance(coords["lat"], (int, float))
            and isinstance(coords["lon"], (int, float))
        ):
            baseline.append((str(b.get("finn_id") or ""), coords["lat"], coords["lon"], t / a))

    all_ppm = [ppm for _, _, _, ppm in baseline]
    if not all_ppm:
        return {"median_ppm": None, "local_medians": {}, "n": 0}

    global_median = statistics.median(all_ppm)

    # Per-listing local medians (K-nearest neighbours by Haversine).
    local_medians: dict[str, float] = {}
    for l in listings:
        finn_id = str(l.get("finn_id") or "")
        coords = l.get("coordinates")
        if not coords or "lat" not in coords or "lon" not in coords:
            continue
        if not finn_id:
            continue
        # Distance to every baseline listing (excluding self).
        distances = []
        for b_id, b_lat, b_lon, b_ppm in baseline:
            if b_id == finn_id:
                continue
            d = haversine_km(coords, {"lat": b_lat, "lon": b_lon})
            distances.append((d, b_ppm))
        if len(distances) < 3:
            # Too few peers for a meaningful local median.
            continue
        distances.sort(key=lambda x: x[0])
        nearest_ppms = [ppm for _, ppm in distances[:k_nearest]]
        local_medians[finn_id] = statistics.median(nearest_ppms)

    return {
        "median_ppm": global_median,
        "n": len(all_ppm),
        "min_ppm": min(all_ppm),
        "max_ppm": max(all_ppm),
        "local_medians": local_medians,
        "k_nearest": k_nearest,
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
        lambda: score_commute_celine(listing, config),
        lambda: score_price_per_m2(listing, config, dataset_stats),
        lambda: score_apartment(listing, config),
        lambda: score_building(listing, config),
        lambda: score_grocery(listing, config),
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
    def _detail_value(target_coords) -> str | None:
        d = commute_details(coords, target_coords)
        if d is None or d.get("minutes") is None:
            return None
        km = haversine_km(coords, target_coords)
        line = f"~{d['minutes']:.0f} min · {km:.1f} km"
        # Real Entur runs include the leg summary; proxy fallback omits it.
        if d.get("summary"):
            line += f" · {d['summary']}"
        return line

    if coords and cur_school.get("coordinates"):
        v = _detail_value(cur_school["coordinates"])
        if v:
            details[f"→ Current school ({cur_school.get('address','')})"] = v
    if coords and nxt_school.get("coordinates"):
        v = _detail_value(nxt_school["coordinates"])
        if v:
            details[f"→ Future school ({nxt_school.get('address','')})"] = v
    cc = config.get("location", {}).get("commute_celine", {})
    if coords and cc.get("coordinates"):
        v = _detail_value(cc["coordinates"])
        if v:
            details[f"→ Céline's commute ({cc.get('destination','')})"] = v
    if listing.get("area_m2") and listing.get("total_price"):
        ppm = listing["total_price"] / listing["area_m2"]
        details["Price per m²"] = f"{ppm:,.0f} NOK"
    if listing.get("plot_m2"):
        details["Plot size"] = f"{listing['plot_m2']:.0f} m²"
    grocery = listing.get("grocery_check")
    if grocery:
        approved = grocery.get("approved_list") or []
        if approved:
            n = approved[0]
            more = grocery.get("approved_count", 1) - 1
            details["🛒 Nearest supermarket"] = (
                f"{n['name']} ({n['chain']}) {n['distance_m']} m"
                + (f" · +{more} more approved within "
                   f"{grocery.get('radius_m', 1000)} m" if more > 0 else "")
            )
        else:
            excl = (grocery.get("excluded_list") or [{}])[0]
            if excl:
                details["🛒 Nearest supermarket"] = (
                    f"⚠️ only {excl.get('name', '?')} "
                    f"({excl.get('chain', '?')}) "
                    f"{excl.get('distance_m', '?')} m — not a real supermarket"
                )
            else:
                details["🛒 Nearest supermarket"] = "none within 1 km"
    if listing.get("construction_year"):
        details["Built"] = str(listing["construction_year"])
    if listing.get("energy_class"):
        details["Energy class"] = str(listing["energy_class"])
    ts = listing.get("timestamp_ms")
    if ts:
        try:
            posted = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
            details["Listed"] = posted.strftime("%Y-%m-%d")
        except (OSError, ValueError, OverflowError):
            pass

    # Headline: prefer LLM's one-sentence vibe summary when available; fall
    # back to the deterministic strong/weak/balanced summary otherwise.
    llm = listing.get("llm") or {}
    vibe = llm.get("vibe_summary")
    headline = vibe if isinstance(vibe, str) and vibe.strip() else _make_headline(sub_scores)

    return ScoredListing(
        listing=listing,
        score=round(score, 1),
        sub_scores=sub_scores,
        tags=tags,
        headline=headline,
        unverified=list(filter_result.get("unverified", [])),
        distance_to_current_home_km=dist_home,
        details=details,
    )


def score_listings(
    filter_results: list[dict],
    config: dict,
    now: datetime | None = None,
    baseline_listings: list[dict] | None = None,
) -> list[ScoredListing]:
    """Score and rank kept listings.

    `baseline_listings` is the broader scraped set used to compute
    neighborhood-aware price/m² medians. Pass the full scrape (post-`scrape`,
    pre-filter) for best coverage.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    listings = [r["listing"] for r in filter_results]

    k = int(
        config.get("financials", {})
        .get("price_per_m2", {})
        .get("neighborhood_k_nearest", 15)
    )
    stats = compute_dataset_stats(listings, baseline_listings, k_nearest=k)
    if stats.get("median_ppm"):
        logger.info(
            "Dataset stats: baseline n=%d, global median=%.0f NOK/m², "
            "local medians for %d/%d listings (K=%d)",
            stats["n"],
            stats["median_ppm"],
            len(stats.get("local_medians") or {}),
            len(listings),
            stats.get("k_nearest", k),
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
