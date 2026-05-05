"""Scenario classification (v1.4).

Each listing that passes the non-location hard filters is assigned to
exactly one of:
  * "main"        — passes the existing location_caps (current school ≤ 25,
                    future ≤ 33, Céline ≤ 45)
  * "scenario_1"  — relaxed current-school cap (≤35), rationale: current
                    school is only relevant until Sept 2028
  * "scenario_2"  — relaxed future-school cap (≤40 today + ≤25 projected
                    post-2029 via Fornebubanen), rationale: new metro line
                    Majorstua/Skøyen ↔ Fornebu opens late 2029
  * None          — no scenario matches, drop

The classifier evaluates main → scenario_1 → scenario_2 in order and
returns the FIRST match. A listing never appears in more than one lane.

Céline's commute cap (45 min) is unchanged across all scenarios — only the
school-related caps relax. Same for non-location filters (price, area,
sold, dealbreaker, …): those run before classification and a non-location
failure means the listing drops outright, regardless of scenario.

This module is purely classification — no side effects, no I/O. It reuses
`commute_minutes` and `projected_post2029_minutes` from src.routing, both
of which transparently use the Entur cache.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from src.routing import commute_minutes, projected_post2029_minutes

logger = logging.getLogger(__name__)


def _evaluate_caps(
    coords: dict,
    schools: dict,
    celine_coords: Optional[dict],
    current_max: Optional[float],
    future_max: Optional[float],
    celine_max: Optional[float],
) -> list[str]:
    """Evaluate the three commute caps. Return list of failure messages
    (empty list = all caps satisfied).

    A None cap means "skip this check". A None commute_minutes result
    (couldn't even compute Haversine) is treated as "skip" too — we don't
    fail a listing on a routing error.
    """
    failures: list[str] = []

    cur_coords = (schools.get("current") or {}).get("coordinates")
    if cur_coords and current_max is not None:
        m = commute_minutes(coords, cur_coords)
        if m is not None and m > current_max:
            failures.append(
                f"current school {m:.0f} min > {current_max} cap"
            )

    nxt_coords = (schools.get("next") or {}).get("coordinates")
    if nxt_coords and future_max is not None:
        m = commute_minutes(coords, nxt_coords)
        if m is not None and m > future_max:
            failures.append(
                f"future school {m:.0f} min > {future_max} cap"
            )

    if celine_coords and celine_max is not None:
        m = commute_minutes(coords, celine_coords)
        if m is not None and m > celine_max:
            failures.append(
                f"Céline's commute {m:.0f} min > {celine_max} cap"
            )

    return failures


def classify(
    coords: dict,
    config: dict,
) -> tuple[Optional[str], list[str]]:
    """Assign a listing to a scenario based on its location caps.

    Tries main → scenario_1 → scenario_2 in order. Returns the FIRST match.

    Args:
        coords: listing coordinates dict {lat, lon}
        config: full scoring_config.yaml dict

    Returns:
        (scenario_name, failures): scenario_name is "main" /
        "scenario_1" / "scenario_2" / None. failures is the list of reasons
        why the MAIN scenario didn't match — used as the dropped-listing
        reason when scenario_name is None, or as info when the listing
        was rescued by an alt scenario.
    """
    if not isinstance(coords, dict) or "lat" not in coords or "lon" not in coords:
        return (None, ["no coordinates"])

    hf = config.get("hard_filters") or {}
    location = config.get("location") or {}
    schools = location.get("schools") or {}
    celine_coords = (location.get("commute_celine") or {}).get("coordinates")

    caps = hf.get("location_caps") or {}
    caps_active = bool(caps.get("active"))
    celine_max = caps.get("celine_commute_max_minutes") if caps_active else None

    # If location_caps is disabled altogether, default everything to main
    # and skip alt-scenario logic — preserves backwards compatibility.
    if not caps_active:
        return ("main", [])

    main_fails = _evaluate_caps(
        coords,
        schools,
        celine_coords,
        caps.get("current_school_max_minutes"),
        caps.get("future_school_max_minutes"),
        celine_max,
    )
    if not main_fails:
        return ("main", [])

    scenarios = config.get("scenarios") or {}

    # Scenario 1: relaxed current-school cap, future + Céline unchanged.
    s1 = scenarios.get("scenario_1") or {}
    if s1.get("active"):
        s1_fails = _evaluate_caps(
            coords,
            schools,
            celine_coords,
            s1.get("current_school_max_minutes"),
            s1.get("future_school_max_minutes"),
            celine_max,
        )
        if not s1_fails:
            return ("scenario_1", main_fails)

    # Scenario 2: relaxed future-school cap (today) + projected ≤ post-2029.
    s2 = scenarios.get("scenario_2") or {}
    if s2.get("active"):
        s2_fails = _evaluate_caps(
            coords,
            schools,
            celine_coords,
            s2.get("current_school_max_minutes"),
            s2.get("future_school_max_minutes_today"),
            celine_max,
        )
        post_max = s2.get("future_school_max_minutes_post_2029")
        if post_max is not None:
            projected = projected_post2029_minutes(coords, s2)
            if projected is not None and projected > post_max:
                s2_fails.append(
                    f"future school post-2029 projected {projected:.0f} min > "
                    f"{post_max} cap"
                )
        if not s2_fails:
            return ("scenario_2", main_fails)

    return (None, main_fails)
