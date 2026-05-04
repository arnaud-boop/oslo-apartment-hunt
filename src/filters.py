"""Hard filters — exclude listings outside our criteria.

What runs in v1
---------------
Only filters that can be applied from search-result data:
  * total_price ≤ config.hard_filters.total_price_max_nok
  * fellesutgifter_month ≤ config.hard_filters.fellesutgifter_max_nok_per_month
  * area_m2 ≥ config.hard_filters.area_min_m2
  * bedrooms ≥ config.hard_filters.bedrooms_min
  * title does not contain "oppussingsobjekt" (case-insensitive)
  * school proximity (non-negotiable): ≤25 min Haversine-proxy to at least one
    French school, OR ≤30 min to both (in-between case).

What's deferred
---------------
Everything that needs detail-page fetch or LLM (floor, bod, washing machine,
wet-room count, sold status, orientation, layout dealbreakers, …) is written
into scoring_config.yaml with `active: false` and is NOT applied in v1.
Instead, the unverified-fields list goes into the listing's ⚠️ tag so the
user knows what we couldn't check.

Output
------
For each input listing, returns a FilterResult with:
  - passed: did all active filters pass?
  - failed: list of human-readable rejection reasons (active rules that excluded it)
  - unverified: list of inactive rules we couldn't apply (turn into ⚠️ on the card)
  - listing: the original Listing object (or its dict form)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from src.routing import commute_minutes

logger = logging.getLogger(__name__)


@dataclass
class FilterResult:
    listing: Any                            # Listing or dict
    passed: bool = True
    failed: list[str] = field(default_factory=list)
    unverified: list[str] = field(default_factory=list)


def load_config(path: str | Path) -> dict:
    with open(path, "r") as f:
        return yaml.safe_load(f)


def _get_field(listing: Any, name: str) -> Any:
    if hasattr(listing, name):
        return getattr(listing, name)
    if isinstance(listing, dict):
        return listing.get(name)
    return None


# Active filter names → human-readable descriptions for the ⚠️ tag and logs.
# Only includes filters NOT yet implemented in code. Filters listed here that
# also appear in scoring_config with `active: true` get applied as hard filters
# instead of becoming ⚠️ unverified labels (handled in apply_hard_filters).
DEFERRED_FILTER_LABELS = {
    "bedroom_min_m2":                 "bedroom sizes (each ≥ 7 m²)",
    "wet_rooms_min":                  "wet rooms (≥ 2 bath + 1 toilet)",
    "storage_bod_required":           "bod (storage room) present",
    "washing_machine_required":       "washing-machine connection",
    "exclude_north_facing_main_rooms":"main rooms not north-facing",
    "grocery_within_walk_minutes":    "grocery store ≤ 10 min walk",
    # exclude_layout_dealbreakers is now active when LLM analysis runs;
    # the active code path adds a per-listing unverified flag if the LLM
    # call failed, so it's no longer here as a blanket deferred label.
}


def _is_active(cfg_entry, default_when_missing=False) -> bool:
    """Some hard_filter entries are bare values (always active);
    others are dicts of the form {value, active}. Detect either shape."""
    if cfg_entry is None:
        return default_when_missing
    if isinstance(cfg_entry, dict):
        return bool(cfg_entry.get("active"))
    return True


def _value_of(cfg_entry):
    if isinstance(cfg_entry, dict):
        return cfg_entry.get("value")
    return cfg_entry


# Greater Oslo region bounding box. Listings outside it are tagged as
# bad-data and dropped — without sane coords we can't apply commute / school
# filters, and the listing is almost certainly mis-geocoded by Finn (we've
# seen 6725 km Haversine distances from null-island sentinel coords).
# Box is generous: covers Asker, Bærum, central Oslo, Lørenskog, Nittedal.
OSLO_BBOX = {"lat_min": 59.5, "lat_max": 60.3, "lon_min": 10.0, "lon_max": 11.5}


def _coords_valid_for_oslo(coords) -> bool:
    if not isinstance(coords, dict):
        return False
    try:
        lat = float(coords["lat"])
        lon = float(coords["lon"])
    except (KeyError, TypeError, ValueError):
        return False
    return (
        OSLO_BBOX["lat_min"] <= lat <= OSLO_BBOX["lat_max"]
        and OSLO_BBOX["lon_min"] <= lon <= OSLO_BBOX["lon_max"]
    )


# ----------------------------------------------------------------------------


def apply_hard_filters(
    listings: list, config: dict
) -> tuple[list[FilterResult], list[FilterResult]]:
    """Run hard filters over listings.

    Returns (kept, dropped). Each is a list of FilterResult.
    """
    hf = config.get("hard_filters", {}) or {}

    total_max = hf.get("total_price_max_nok")
    felles_max = hf.get("fellesutgifter_max_nok_per_month")
    area_min = hf.get("area_min_m2")
    beds_min = hf.get("bedrooms_min")
    exclude_oppus = hf.get("exclude_oppussingsobjekt_in_title", False)

    # Detail-page filters (apply only when listing has been enriched).
    floor_filter_active = _is_active(hf.get("exclude_ground_floor"))
    elevator_floor_threshold = (
        _value_of(hf.get("elevator_required_at_or_above_floor"))
        if _is_active(hf.get("elevator_required_at_or_above_floor"))
        else None
    )
    sold_filter_active = _is_active(hf.get("exclude_sold_or_under_offer"))
    fixer_desc_active = _is_active(hf.get("exclude_fixer_upper_in_description"))

    # LLM-based filter (only fires on listings that went through the LLM pass).
    layout_dealbreaker_cfg = hf.get("exclude_layout_dealbreakers", {}) or {}
    layout_dealbreaker_active = _is_active(layout_dealbreaker_cfg)
    layout_dealbreaker_min_confidence = float(
        layout_dealbreaker_cfg.get("min_confidence", 0.7)
        if isinstance(layout_dealbreaker_cfg, dict)
        else 0.7
    )

    # Three independent location caps, AND-combined. Each is a hard filter.
    caps_cfg = hf.get("location_caps", {}) or {}
    caps_active = bool(caps_cfg.get("active"))
    cur_cap = caps_cfg.get("current_school_max_minutes")
    nxt_cap = caps_cfg.get("future_school_max_minutes")
    celine_cap = caps_cfg.get("celine_commute_max_minutes")
    school_coords = config.get("location", {}).get("schools", {})
    cur_coords = school_coords.get("current", {}).get("coordinates")
    nxt_coords = school_coords.get("next", {}).get("coordinates")
    celine_coords = (config.get("location", {}).get("commute_celine", {}) or {}).get(
        "coordinates"
    )
    if caps_active and not (cur_coords or nxt_coords or celine_coords):
        logger.warning(
            "location_caps.active=true but missing coordinates — disabling filter"
        )
        caps_active = False

    # Deferred (inactive) rules → unverified flags, in a stable order.
    deferred_active_keys = [
        k
        for k, v in hf.items()
        if isinstance(v, dict) and v.get("active") is False
    ]
    deferred_labels = [
        DEFERRED_FILTER_LABELS.get(k, k) for k in deferred_active_keys
    ]

    # Both words mean the same thing (fixer-upper). Description-only matches
    # require detail-page text — handled in v1.0.5.
    oppus_re = re.compile(r"oppussingsobjekt|renoveringsobjekt", re.IGNORECASE)

    kept: list[FilterResult] = []
    dropped: list[FilterResult] = []

    for listing in listings:
        result = FilterResult(listing=listing)

        total = _get_field(listing, "total_price")
        if total is None:
            result.unverified.append("could not verify total price")
        elif total_max is not None and total > total_max:
            result.failed.append(
                f"total price {total:,} > {total_max:,} NOK"
            )

        felles = _get_field(listing, "fellesutgifter_month")
        if felles is None:
            result.unverified.append("could not verify monthly felleskostnader")
        elif felles_max is not None and felles > felles_max:
            result.failed.append(
                f"felleskostnader {felles:,}/mo > {felles_max:,} NOK/mo"
            )

        area = _get_field(listing, "area_m2")
        if area is None:
            result.unverified.append("could not verify area")
        elif area_min is not None and area < area_min:
            result.failed.append(f"area {area} m² < {area_min} m²")

        beds = _get_field(listing, "bedrooms")
        if beds is None:
            result.unverified.append("could not verify bedroom count")
        elif beds_min is not None and beds < beds_min:
            result.failed.append(f"bedrooms {beds} < {beds_min}")

        title = _get_field(listing, "title") or ""
        if exclude_oppus and oppus_re.search(title):
            result.failed.append(
                'title contains "oppussingsobjekt"/"renoveringsobjekt"'
            )

        if caps_active:
            coords = _get_field(listing, "coordinates")
            if not coords or "lat" not in coords or "lon" not in coords:
                result.unverified.append(
                    "could not verify location caps (no coords)"
                )
            elif not _coords_valid_for_oslo(coords):
                # Bogus coordinates (likely null-island sentinel or lat/lon
                # swap from Finn). We can't trust this listing's location at
                # all — drop rather than let it slip past as unverified.
                result.failed.append(
                    f"invalid coordinates ({coords.get('lat')}, "
                    f"{coords.get('lon')}) — outside Greater Oslo region"
                )
            else:
                # Each cap is independent and AND-combined. commute_minutes
                # uses real Entur transit data with Haversine fallback.
                if cur_coords and cur_cap is not None:
                    cur_min = commute_minutes(coords, cur_coords)
                    if cur_min is not None and cur_min > cur_cap:
                        result.failed.append(
                            f"current school {cur_min:.0f} min > {cur_cap} cap"
                        )
                if nxt_coords and nxt_cap is not None:
                    nxt_min = commute_minutes(coords, nxt_coords)
                    if nxt_min is not None and nxt_min > nxt_cap:
                        result.failed.append(
                            f"future school {nxt_min:.0f} min > {nxt_cap} cap"
                        )
                if celine_coords and celine_cap is not None:
                    celine_min = commute_minutes(coords, celine_coords)
                    if celine_min is not None and celine_min > celine_cap:
                        result.failed.append(
                            f"Céline's commute {celine_min:.0f} min > {celine_cap} cap"
                        )

        # Detail-page filters. Each requires the listing to be enriched. If
        # the corresponding field is absent, the filter goes to ⚠️ unverified.
        if floor_filter_active:
            floor = _get_field(listing, "floor")
            if floor is None:
                result.unverified.append("could not verify floor (no detail page)")
            elif floor == 1:
                result.failed.append("floor 1 (1. etasje / ground floor)")

        if elevator_floor_threshold is not None:
            floor = _get_field(listing, "floor")
            has_elevator = _get_field(listing, "has_elevator")
            if floor is None or has_elevator is None:
                # Don't double-flag if floor was already unverified above.
                if not floor_filter_active:
                    result.unverified.append(
                        "could not verify elevator-by-floor rule"
                    )
            elif floor >= elevator_floor_threshold and not has_elevator:
                result.failed.append(
                    f"floor {floor} ≥ {elevator_floor_threshold} but no elevator"
                )

        if sold_filter_active:
            disposed = _get_field(listing, "disposed")
            if disposed is None:
                result.unverified.append(
                    "could not verify sold/under-offer status"
                )
            elif disposed:
                result.failed.append("listing is sold or withdrawn")

        if fixer_desc_active:
            in_desc = _get_field(listing, "fixer_upper_in_description")
            if in_desc is None:
                # Will be verified after enrichment runs.
                result.unverified.append(
                    "could not verify fixer-upper in description"
                )
            elif in_desc:
                result.failed.append(
                    'description contains "oppussingsobjekt"/"renoveringsobjekt"'
                )

        if layout_dealbreaker_active:
            llm = _get_field(listing, "llm") or {}
            if not llm:
                result.unverified.append(
                    "could not check layout dealbreakers (no LLM analysis)"
                )
            else:
                dealbreaker = llm.get("layout_dealbreaker")
                conf = llm.get("confidence")
                if dealbreaker is True and isinstance(conf, (int, float)) and conf >= layout_dealbreaker_min_confidence:
                    reason = llm.get("layout_dealbreaker_reason") or "layout dealbreaker"
                    result.failed.append(
                        f"LLM-detected layout dealbreaker (conf {conf:.2f}): {reason}"
                    )

        # Deferred filters → ⚠️ unverified flags.
        result.unverified.extend(deferred_labels)

        result.passed = not result.failed
        (kept if result.passed else dropped).append(result)

    logger.info(
        "Filtered: %d kept, %d dropped (of %d input)",
        len(kept),
        len(dropped),
        len(listings),
    )
    return kept, dropped


# ----------------------------------------------------------------------------
# CLI for ad-hoc inspection: filter the latest scrape, print summary.
# ----------------------------------------------------------------------------


def main() -> int:
    import json
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    repo_root = Path(__file__).resolve().parent.parent

    config = load_config(repo_root / "config" / "scoring_config.yaml")
    listings_path = repo_root / "data" / "listings_latest.json"
    if not listings_path.exists():
        print(
            f"Missing {listings_path}. Run `python -m src.scraper` first.",
            file=sys.stderr,
        )
        return 1

    listings = json.loads(listings_path.read_text())
    kept, dropped = apply_hard_filters(listings, config)

    # Aggregate failure reasons
    from collections import Counter

    reasons = Counter()
    for r in dropped:
        for f in r.failed:
            # bucket by the first phrase before the digit
            bucket = f.split(" ")[0] + " " + f.split(" ")[1]
            reasons[bucket] += 1

    print(f"\n=== Filter results: {len(kept)} kept, {len(dropped)} dropped ===\n")
    print("Top failure reasons:")
    for reason, count in reasons.most_common(10):
        print(f"  {count:4d}  {reason}")

    print("\n=== Sample of 5 KEPT (with ⚠️ unverified flags) ===\n")
    for r in kept[:5]:
        l = r.listing
        title = (l.get("title") or "")[:90]
        print(f"  finn={l.get('finn_id')} | {l.get('property_type')} | "
              f"area={l.get('area_m2')}m² | total={l.get('total_price'):,} NOK")
        print(f"    {title}")
        if r.unverified:
            print(f"    ⚠️  unverified: {len(r.unverified)} item(s) "
                  f"(first 3: {r.unverified[:3]})")
        print()

    print("\n=== Sample of 5 DROPPED (with reasons) ===\n")
    for r in dropped[:5]:
        l = r.listing
        title = (l.get("title") or "")[:90]
        print(f"  finn={l.get('finn_id')} | {l.get('property_type')}")
        print(f"    {title}")
        print(f"    ✗ failed: {r.failed}")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
