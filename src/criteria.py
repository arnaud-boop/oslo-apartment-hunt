"""Per-listing criteria checklist for the eval page.

Computes a structured rundown of every hard filter and quality criterion
applied (or deferred) to a listing. Each row carries its own status:

  - "pass"       : rule applied and the listing passed
  - "fail"       : rule applied and the listing failed (should be rare here
                   since the eval page is rendered for kept listings only,
                   but kept defensively)
  - "unverified" : rule active but the data needed wasn't extracted for
                   this listing (e.g. coords missing → can't compute commute)
  - "deferred"   : rule not yet implemented in v1 — explanation included

Renderer maps statuses to icons (✓ ✗ ⚠️ —).
"""

from __future__ import annotations

import re
from typing import Any, Optional

from src.routing import commute_minutes


# ---- formatting helpers --------------------------------------------------


def _fmt_nok(amount: Optional[int]) -> str:
    if amount is None:
        return "?"
    if abs(amount) >= 1_000_000:
        return f"{amount/1_000_000:.2f} M"
    if abs(amount) >= 1_000:
        return f"{amount/1_000:,.0f} k"
    return f"{amount:,}"


def _row(key: str, label: str, status: str, detail: str) -> dict:
    return {"key": key, "label": label, "status": status, "detail": detail}


# ---- search-result-data filters ----------------------------------------


def _check_total_price(listing: dict, config: dict) -> dict:
    cap = (config.get("hard_filters") or {}).get("total_price_max_nok")
    total = listing.get("total_price")
    if cap is None:
        return _row("total_price", "Total price", "deferred", "no cap configured")
    if total is None:
        return _row("total_price", "Total price", "unverified", "price not extracted")
    if total <= cap:
        return _row("total_price", "Total price", "pass",
                    f"{_fmt_nok(total)} NOK ≤ {_fmt_nok(cap)} NOK cap")
    return _row("total_price", "Total price", "fail",
                f"{_fmt_nok(total)} NOK > {_fmt_nok(cap)} NOK cap")


def _check_felles(listing: dict, config: dict) -> dict:
    cap = (config.get("hard_filters") or {}).get("fellesutgifter_max_nok_per_month")
    val = listing.get("fellesutgifter_month")
    if cap is None:
        return _row("felles", "Felleskostnader/mo", "deferred", "no cap configured")
    if val is None:
        return _row("felles", "Felleskostnader/mo", "unverified", "not extracted")
    if val <= cap:
        return _row("felles", "Felleskostnader/mo", "pass",
                    f"{val:,} NOK/mo ≤ {cap:,} NOK/mo cap")
    return _row("felles", "Felleskostnader/mo", "fail",
                f"{val:,} NOK/mo > {cap:,} NOK/mo cap")


def _check_area(listing: dict, config: dict) -> dict:
    floor_min = (config.get("hard_filters") or {}).get("area_min_m2")
    area = listing.get("area_m2")
    if floor_min is None:
        return _row("area", "Area", "deferred", "no minimum configured")
    if area is None:
        return _row("area", "Area", "unverified", "not extracted")
    if area >= floor_min:
        return _row("area", "Area", "pass", f"{area:.0f} m² ≥ {floor_min} m² floor")
    return _row("area", "Area", "fail", f"{area:.0f} m² < {floor_min} m² floor")


def _check_bedrooms(listing: dict, config: dict) -> dict:
    floor_min = (config.get("hard_filters") or {}).get("bedrooms_min")
    beds = listing.get("bedrooms")
    if floor_min is None:
        return _row("bedrooms", "Bedrooms", "deferred", "no minimum configured")
    if beds is None:
        return _row("bedrooms", "Bedrooms", "unverified", "not extracted")
    if beds >= floor_min:
        return _row("bedrooms", "Bedrooms", "pass", f"{beds} ≥ {floor_min}")
    return _row("bedrooms", "Bedrooms", "fail", f"{beds} < {floor_min}")


_FIXER_RE = re.compile(r"\b(oppussingsobjekt|renoveringsobjekt)\b", re.IGNORECASE)


def _check_fixer_title(listing: dict, config: dict) -> dict:
    if not (config.get("hard_filters") or {}).get("exclude_oppussingsobjekt_in_title"):
        return _row("fixer_title", 'No "oppussingsobjekt" in title', "deferred", "rule disabled")
    title = listing.get("title") or ""
    if _FIXER_RE.search(title):
        return _row("fixer_title", 'No "oppussingsobjekt" in title', "fail",
                    f'"{ _FIXER_RE.search(title).group(0) }" found in title')
    return _row("fixer_title", 'No "oppussingsobjekt"/"renoveringsobjekt" in title',
                "pass", "neither keyword present")


def _check_fixer_desc(listing: dict, config: dict) -> dict:
    cfg = (config.get("hard_filters") or {}).get("exclude_fixer_upper_in_description")
    active = bool(cfg.get("active") if isinstance(cfg, dict) else cfg)
    if not active:
        return _row("fixer_desc", 'No "oppussingsobjekt" in description', "deferred", "rule disabled")
    flag = listing.get("fixer_upper_in_description")
    if flag is None:
        return _row("fixer_desc", 'No "oppussingsobjekt" in description',
                    "unverified", "no detail-page text available")
    if flag:
        return _row("fixer_desc", 'No "oppussingsobjekt" in description',
                    "fail", "keyword found in description")
    return _row("fixer_desc", 'No "oppussingsobjekt"/"renoveringsobjekt" in description',
                "pass", "neither keyword present")


# ---- detail-page filters -----------------------------------------------


def _check_floor(listing: dict, config: dict) -> dict:
    cfg = (config.get("hard_filters") or {}).get("exclude_ground_floor")
    active = bool(cfg.get("active") if isinstance(cfg, dict) else cfg)
    if not active:
        return _row("floor_ground", "Not ground floor", "deferred", "rule disabled")
    floor = listing.get("floor")
    source = listing.get("floor_source")
    if floor is None:
        return _row("floor_ground", "Not ground floor", "unverified", "floor not extracted")
    label = f"floor {floor}" + (f" (from {source})" if source and source != "structured" else "")
    if floor == 1:
        return _row("floor_ground", "Not ground floor", "fail", f"{label} = 1. etasje (ground)")
    return _row("floor_ground", "Not ground floor", "pass", label)


def _check_elevator(listing: dict, config: dict) -> dict:
    cfg = (config.get("hard_filters") or {}).get("elevator_required_at_or_above_floor")
    active = bool(cfg.get("active") if isinstance(cfg, dict) else cfg is not None)
    threshold = cfg.get("value") if isinstance(cfg, dict) else cfg
    if not active or threshold is None:
        return _row("elevator", "Elevator if floor ≥ N", "deferred", "rule disabled")
    floor = listing.get("floor")
    has_elev = listing.get("has_elevator")
    if floor is None:
        return _row("elevator", f"Elevator if floor ≥ {threshold}",
                    "unverified", "floor not extracted")
    if floor < threshold:
        return _row("elevator", f"Elevator if floor ≥ {threshold}",
                    "pass", f"floor {floor} < {threshold} (rule N/A)")
    if has_elev is None:
        return _row("elevator", f"Elevator if floor ≥ {threshold}",
                    "unverified", "elevator status unknown")
    if has_elev:
        return _row("elevator", f"Elevator if floor ≥ {threshold}",
                    "pass", f"floor {floor}, elevator present")
    return _row("elevator", f"Elevator if floor ≥ {threshold}",
                "fail", f"floor {floor} ≥ {threshold} but no elevator")


def _check_sold(listing: dict, config: dict) -> dict:
    cfg = (config.get("hard_filters") or {}).get("exclude_sold_or_under_offer")
    active = bool(cfg.get("active") if isinstance(cfg, dict) else cfg)
    if not active:
        return _row("sold", "Available (not sold/under offer)", "deferred", "rule disabled")
    disposed = listing.get("disposed")
    if disposed is None:
        return _row("sold", "Available (not sold/under offer)", "unverified", "status unknown")
    if disposed:
        return _row("sold", "Available (not sold/under offer)", "fail", "disposed=true")
    return _row("sold", "Available (not sold/under offer)", "pass", "still on the market")


# ---- location caps -----------------------------------------------------


def _location_check_for_target(listing, target, cap, key, label):
    target_coords = target.get("coordinates") if target else None
    coords = listing.get("coordinates")
    if cap is None or not target_coords:
        return _row(key, label, "deferred", "missing config")
    if not coords or "lat" not in coords or "lon" not in coords:
        return _row(key, label, "unverified", "no coordinates")
    minutes = commute_minutes(coords, target_coords)
    if minutes is None:
        return _row(key, label, "unverified", "transit time unavailable")
    if minutes <= cap:
        return _row(key, label, "pass", f"{minutes:.0f} min ≤ {cap} min cap")
    return _row(key, label, "fail", f"{minutes:.0f} min > {cap} min cap")


def _check_current_school(listing, config):
    caps = (config.get("hard_filters") or {}).get("location_caps") or {}
    if not caps.get("active"):
        return _row("current_school", "Current school (Skovveien 9)", "deferred", "location_caps disabled")
    cap = caps.get("current_school_max_minutes")
    target = ((config.get("location") or {}).get("schools") or {}).get("current") or {}
    return _location_check_for_target(
        listing, target, cap, "current_school", "Current school (Skovveien 9)"
    )


def _check_future_school(listing, config):
    caps = (config.get("hard_filters") or {}).get("location_caps") or {}
    if not caps.get("active"):
        return _row("future_school", "Future school (Snarøyveien 30)", "deferred", "location_caps disabled")
    cap = caps.get("future_school_max_minutes")
    target = ((config.get("location") or {}).get("schools") or {}).get("next") or {}
    return _location_check_for_target(
        listing, target, cap, "future_school", "Future school (Snarøyveien 30)"
    )


def _check_celine_commute(listing, config):
    caps = (config.get("hard_filters") or {}).get("location_caps") or {}
    if not caps.get("active"):
        return _row("celine_commute", "Céline's commute (Helsfyr)", "deferred", "location_caps disabled")
    cap = caps.get("celine_commute_max_minutes")
    target = (config.get("location") or {}).get("commute_celine") or {}
    return _location_check_for_target(
        listing, target, cap, "celine_commute", "Céline's commute (Helsfyr)"
    )


# ---- LLM-based filter --------------------------------------------------


def _check_layout_dealbreaker(listing: dict, config: dict) -> dict:
    cfg = (config.get("hard_filters") or {}).get("exclude_layout_dealbreakers") or {}
    active = bool(cfg.get("active"))
    threshold = float(cfg.get("min_confidence", 0.7) if isinstance(cfg, dict) else 0.7)
    if not active:
        return _row("layout_dealbreaker", "No layout dealbreaker (LLM)", "deferred", "rule disabled")
    llm = listing.get("llm") or {}
    if not llm:
        return _row("layout_dealbreaker", "No layout dealbreaker (LLM)",
                    "unverified", "LLM analysis unavailable")
    flag = llm.get("layout_dealbreaker")
    conf = llm.get("confidence")
    conf_str = f"conf {conf:.2f}" if isinstance(conf, (int, float)) else "conf ?"
    if flag is True and isinstance(conf, (int, float)) and conf >= threshold:
        reason = llm.get("layout_dealbreaker_reason") or "(reason omitted)"
        return _row("layout_dealbreaker", "No layout dealbreaker (LLM)",
                    "fail", f"{reason} ({conf_str})")
    if flag is True:
        return _row("layout_dealbreaker", "No layout dealbreaker (LLM)",
                    "unverified", f"flagged but {conf_str} < threshold {threshold}")
    return _row("layout_dealbreaker", "No layout dealbreaker (LLM)",
                "pass", f"clean ({conf_str})")


# ---- explicitly deferred items ----------------------------------------


def _deferred_rows(listing: dict, config: dict) -> list[dict]:
    """Annotation-only rows.

    These rules are not (yet) hard filters — they don't drop listings.
    Instead the row reflects what we know:
      - "pass"   if salgsoppgave-extracted data clearly satisfies the rule
      - "fail"   if salgsoppgave-extracted data clearly violates it
                 (the listing still appears in the digest — annotation only)
      - "unverified" if the salgsoppgave didn't say (or failed to fetch)
      - "deferred"   if we have no path to the data at all (e.g. grocery)
    """
    out = []
    salgs = listing.get("salgsoppgave") or {}

    # ---- Bedroom min m² ----
    sizes = salgs.get("bedroom_sizes_m2") or []
    smallest = salgs.get("smallest_bedroom_m2")
    if smallest is None and sizes:
        smallest = min(sizes)
    descriptor = salgs.get("bedroom_quality_descriptor")
    if smallest is not None:
        sizes_str = ", ".join(f"{s:.1f}" for s in sizes) if sizes else ""
        if smallest >= 7:
            detail = f"smallest = {smallest:.1f} m²"
            if sizes_str:
                detail += f" (sizes: {sizes_str})"
            out.append(_row("bedroom_min_m2", "Bedrooms ≥ 7 m² each", "pass", detail))
        else:
            detail = f"smallest = {smallest:.1f} m² < 7 m²"
            if sizes_str:
                detail += f" (sizes: {sizes_str})"
            detail += "  · annotation only, listing not auto-dropped"
            out.append(_row("bedroom_min_m2", "Bedrooms ≥ 7 m² each", "fail", detail))
    elif descriptor:
        out.append(_row(
            "bedroom_min_m2", "Bedrooms ≥ 7 m² each", "unverified",
            f'salgsoppgave describes them as "{descriptor}" — sizes not given',
        ))
    else:
        out.append(_row(
            "bedroom_min_m2", "Bedrooms ≥ 7 m² each", "unverified",
            "per-room sizes not in salgsoppgave",
        ))

    # ---- Wet rooms ≥ 3 ----
    wet = salgs.get("wet_rooms_count")
    baths = salgs.get("bathrooms_count")
    if wet is not None:
        breakdown = f" ({baths} bath{'s' if (baths or 0) != 1 else ''})" if baths is not None else ""
        if wet >= 3:
            out.append(_row("wet_rooms", "Wet rooms ≥ 3", "pass",
                            f"{wet} wet rooms{breakdown}"))
        else:
            out.append(_row("wet_rooms", "Wet rooms ≥ 3", "fail",
                            f"only {wet} wet rooms{breakdown}  · annotation only"))
    else:
        out.append(_row(
            "wet_rooms", "Wet rooms ≥ 3", "unverified",
            "wet-room count not extracted from salgsoppgave",
        ))

    # ---- Bod ----
    has_bod = salgs.get("has_bod")
    bod_size = salgs.get("bod_size_m2")
    bod_evidence = listing.get("has_bod_evidence")
    if has_bod is True:
        detail = "explicit yes" + (f" ({bod_size:.1f} m²)" if bod_size else "")
        out.append(_row("bod", "Bod (storage room) present", "pass", detail))
    elif has_bod is False:
        out.append(_row("bod", "Bod (storage room) present", "fail",
                        "explicit no  · annotation only"))
    elif bod_evidence:
        # LLM extraction missed it but the description regex caught the
        # word — strong enough to call it a pass with a provenance note.
        out.append(_row(
            "bod", "Bod (storage room) present", "pass",
            'description mentions "bod"/"kjellerbod"/etc. (LLM missed it; '
            "verify on the salgsoppgave)",
        ))
    else:
        out.append(_row(
            "bod", "Bod (storage room) present", "unverified",
            "no mention in description or salgsoppgave",
        ))

    # ---- Washing machine connection ----
    has_wash = salgs.get("has_washing_machine_connection")
    wash_evidence = listing.get("has_washing_machine_evidence")
    if has_wash is True:
        out.append(_row("washing", "Washing-machine connection", "pass",
                        "explicit yes"))
    elif has_wash is False:
        out.append(_row("washing", "Washing-machine connection", "fail",
                        "explicit no  · annotation only"))
    elif wash_evidence:
        out.append(_row(
            "washing", "Washing-machine connection", "pass",
            'description mentions "vaskemaskin"/"vaskerom" (LLM missed it; '
            "verify on the salgsoppgave)",
        ))
    else:
        out.append(_row(
            "washing", "Washing-machine connection", "unverified",
            "no mention in description or salgsoppgave",
        ))

    # ---- North-facing — still deferred (no salgsoppgave field for it) ----
    orientations = listing.get("orientation_mentions") or []
    north_only = listing.get("primary_orientation_north_only")
    out.append(_row(
        "north_facing", "Main rooms not north-facing", "deferred",
        "LLM signal too noisy to gate on; orientations mentioned: " +
        (", ".join(orientations) if orientations else "(none detected)") +
        ("; flagged as north-only" if north_only else ""),
    ))

    # ---- Grocery — still deferred (needs OSM) ----
    out.append(_row(
        "grocery", "Grocery store ≤ 10 min walk", "deferred",
        "needs OSM lookup (v1.x)",
    ))

    return out


# ---- the public function ----------------------------------------------


_ACTIVE_CHECKS = [
    _check_total_price,
    _check_felles,
    _check_area,
    _check_bedrooms,
    _check_fixer_title,
    _check_fixer_desc,
    _check_floor,
    _check_elevator,
    _check_sold,
    _check_layout_dealbreaker,
    _check_current_school,
    _check_future_school,
    _check_celine_commute,
]


def compute_checklist(listing: dict, config: dict) -> list[dict]:
    """Compute the criteria checklist for a single listing."""
    rows = []
    for fn in _ACTIVE_CHECKS:
        try:
            row = fn(listing, config)
        except Exception as e:  # defensive — never let a check crash the page
            row = _row(fn.__name__, fn.__name__.lstrip("_"), "unverified", f"check error: {e}")
        if row:
            rows.append(row)
    rows.extend(_deferred_rows(listing, config))
    return rows
