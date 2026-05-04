"""LLM analysis pass for Norwegian real-estate listings.

Sends each listing's description and structured Norwegian fact table to
Claude. Returns a structured analysis covering:

  - vibe_summary       : one-sentence "why this scored well/poorly" line
  - layout_dealbreaker : long hallways, gjennomgangsrom, kitchen no window,
                         bath off kitchen — used as a hard filter when
                         confidence is high
  - light/orientation  : main_orientation, light_quality, has_visavi
  - noise/street       : on_busy_street
  - renovation/state   : renovated_within_5_years
  - layout flags       : has_open_plan_kitchen, ceiling_height_high
  - features           : has_bathtub, has_bakgaard
  - heating            : heating_type, felleskostnader_includes_*
  - family signals     : family_friendly, layout_clean

Uses Claude's tools API for structured output (no JSON-string parsing).

Configuration
-------------
Reads `llm` section from scoring_config.yaml:
  active: true|false
  model: claude-sonnet-4-6
  temperature: 0.2

Authentication
--------------
ANTHROPIC_API_KEY env var must be set. In CI, expose via the
`ANTHROPIC_API_KEY` repo secret. Locally, set in your shell or .env.
If missing, the pipeline logs a warning and skips the LLM step entirely.

Caching
-------
Per-listing cache at .cache/llm.json keyed by finn_id with a 14-day TTL.
Daily reruns hit cache; only newly-appeared listings trigger fresh API calls.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

CACHE_DIR = Path(".cache")
CACHE_FILE = CACHE_DIR / "llm.json"
CACHE_TTL_DAYS = 14

DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_TEMPERATURE = 0.2
DEFAULT_MAX_TOKENS = 1024

RATE_LIMIT_S = 0.4  # ~2.5 req/sec — well under any tier limit

_client: Any = None
_cache: Optional[dict] = None
_last_call_t: float = 0.0


# ----------------------------------------------------------------- tool ----
# We use Claude's tools API to enforce structured output. The schema below
# IS the contract — Anthropic validates the model's response against it.

ANALYSIS_TOOL = {
    "name": "submit_listing_analysis",
    "description": (
        "Submit your structured analysis of this Oslo real-estate listing. "
        "Use null for any field where the listing doesn't give you enough "
        "to judge confidently."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "vibe_summary": {
                "type": "string",
                "description": (
                    "ONE sentence (max 22 English words). What's the actual "
                    "deal beyond the marketing copy? Be honest, concrete, "
                    "and useful to a buyer."
                ),
            },
            "light_quality": {
                "type": ["string", "null"],
                "enum": ["good", "ok", "poor", None],
            },
            "main_orientation": {
                "type": ["string", "null"],
                "enum": ["south", "west", "east", "north", "mixed", None],
            },
            "has_visavi": {
                "type": ["boolean", "null"],
                "description": "True if a building right opposite blocks light/privacy.",
            },
            "on_busy_street": {
                "type": ["boolean", "null"],
                "description": (
                    "True if the listing is on Ring 2/3, Kirkeveien, "
                    "Bogstadveien, a tram line, or otherwise notably traffic-y."
                ),
            },
            "renovated_within_5_years": {
                "type": ["boolean", "null"],
                "description": "Substantial renovation completed within 5 years of May 2026.",
            },
            "has_open_plan_kitchen": {
                "type": ["boolean", "null"],
                "description": "Kitchen integrated with living, not a separate room.",
            },
            "ceiling_height_high": {
                "type": ["boolean", "null"],
                "description": "Ceilings ≥ 2.7 m. Often stated as 'takhøyde X cm' or 'høye tak'.",
            },
            "has_bathtub": {"type": ["boolean", "null"]},
            "has_bakgaard": {
                "type": ["boolean", "null"],
                "description": "Private/shared courtyard or garden the residents can use.",
            },
            "heating_type": {
                "type": ["string", "null"],
                "enum": ["collective", "individual_electric", "mixed", None],
            },
            "felleskostnader_includes_heat": {"type": ["boolean", "null"]},
            "felleskostnader_includes_internet": {"type": ["boolean", "null"]},
            "family_friendly": {
                "type": ["boolean", "null"],
                "description": (
                    "Building or neighborhood signals suggesting it's good for "
                    "families with kids: playgrounds, mixed-family building, "
                    "barnevennlig descriptor."
                ),
            },
            "layout_clean": {
                "type": ["boolean", "null"],
                "description": "Straightforward layout, good room flow, no awkward dead-ends.",
            },
            "layout_dealbreaker": {
                "type": ["boolean", "null"],
                "description": (
                    "True if ANY of: long narrow hallway eating m², bedrooms "
                    "only accessible through other rooms (gjennomgangsrom), "
                    "kitchen with no window, bathroom directly off kitchen."
                ),
            },
            "layout_dealbreaker_reason": {
                "type": ["string", "null"],
                "description": "If layout_dealbreaker is true, briefly state which.",
            },
            "confidence": {
                "type": "number",
                "description": (
                    "Your overall confidence in this analysis (0-1), "
                    "given how much the listing actually states vs what "
                    "had to be guessed."
                ),
                "minimum": 0,
                "maximum": 1,
            },
        },
        "required": ["vibe_summary", "confidence"],
    },
}


# ------------------------------------------------------- prompt builder ----


_SYSTEM = (
    "You are analyzing a Norwegian real-estate listing for a family in Oslo "
    "(parents + 6-year-old daughter) considering buying. The user will share "
    "the listing data; you call the submit_listing_analysis tool with your "
    "structured assessment.\n"
    "\n"
    "Be honest. Sellers' descriptions are marketing copy — read between the "
    "lines. Don't infer features that aren't actually mentioned or visible. "
    "Use null when truly unclear; don't guess. Today's date is May 2026."
)


def _build_user_prompt(listing: dict) -> str:
    facilities = listing.get("facilities") or []
    general = listing.get("general_text") or {}
    desc = (listing.get("description") or "")[:3500]

    sections = [
        "=== Listing snapshot ===",
        f"Title: {listing.get('title') or '(none)'}",
        f"Property type: {listing.get('property_type_detail') or listing.get('property_type') or '?'}",
        f"Ownership: {listing.get('ownership_type_detail') or listing.get('owner_type') or '?'}",
        f"Address: {listing.get('address') or '?'}",
        f"Area: {listing.get('area_m2') or '?'} m², {listing.get('bedrooms') or '?'} bedrooms",
        f"Built: {listing.get('construction_year') or '?'}",
        f"Total price: {listing.get('total_price') or '?'} NOK",
        f"Felleskostnader: {listing.get('fellesutgifter_month') or '?'} NOK/month",
        f"Felleskostnader includes (per ad): {(listing.get('shared_cost_includes') or '')[:300]}",
        f"Energy class: {listing.get('energy_class') or '?'}",
        f"Facilities flags from Finn (already verified, don't re-derive): {', '.join(facilities) if facilities else '(none)'}",
        "",
        "=== Description (sammendrag) ===",
        desc,
    ]

    # Selected structured sections from the detail page.
    if general:
        sections.append("")
        sections.append("=== Structured fact sections ===")
        for heading in (
            "Standard",
            "Tilstand",
            "Beliggenhet / servicetilbud",
            "Beskrivelse av bebyggelsen",
            "Oppvarming / Teknisk",
            "Faste kostnader",
            "Uteområde",
            "Adkomst",
            "Sammendrag fra selgers egenerklæring",
        ):
            text = general.get(heading)
            if text:
                sections.append(f"\n--- {heading} ---")
                sections.append(text[:1500])

    sections.append("")
    sections.append(
        "Now call submit_listing_analysis with your structured analysis. "
        "Be concise, be honest."
    )
    return "\n".join(sections)


# ---------------------------------------------------------------- cache ----


def _load_cache() -> dict:
    if not CACHE_FILE.exists():
        return {}
    try:
        return json.loads(CACHE_FILE.read_text())
    except json.JSONDecodeError:
        logger.warning("LLM cache corrupt; ignoring")
        return {}


def _save_cache(cache: dict) -> None:
    CACHE_DIR.mkdir(exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache, indent=2, ensure_ascii=False))


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


# --------------------------------------------------------------- client ----


def _get_client():
    global _client
    if _client is None:
        try:
            import anthropic
        except ImportError:
            logger.error(
                "anthropic package not installed. Add `anthropic>=0.40` to "
                "requirements.txt and reinstall."
            )
            return None
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            logger.warning(
                "ANTHROPIC_API_KEY not set — LLM analysis will be skipped"
            )
            return None
        _client = anthropic.Anthropic(api_key=api_key)
    return _client


# -------------------------------------------------------------- analyze ----


def analyze_listing(
    listing: dict,
    *,
    model: str = DEFAULT_MODEL,
    temperature: float = DEFAULT_TEMPERATURE,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> Optional[dict]:
    """Run analysis for a single listing. Returns the dict or None on failure."""
    global _last_call_t
    client = _get_client()
    if client is None:
        return None

    # Rate limit
    elapsed = time.monotonic() - _last_call_t
    if elapsed < RATE_LIMIT_S:
        time.sleep(RATE_LIMIT_S - elapsed)

    user_prompt = _build_user_prompt(listing)
    try:
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=_SYSTEM,
            tools=[ANALYSIS_TOOL],
            tool_choice={"type": "tool", "name": ANALYSIS_TOOL["name"]},
            messages=[{"role": "user", "content": user_prompt}],
        )
    except Exception as e:
        logger.warning(
            "Claude API error for finn=%s: %s", listing.get("finn_id"), e
        )
        return None
    finally:
        _last_call_t = time.monotonic()

    # Find the tool_use block in the response.
    for block in response.content:
        if getattr(block, "type", None) == "tool_use":
            data = dict(block.input or {})
            # Guard against weirdly-shaped responses.
            if not isinstance(data.get("vibe_summary"), str):
                logger.warning(
                    "LLM returned tool_use without vibe_summary for finn=%s",
                    listing.get("finn_id"),
                )
                return None
            return data
    logger.warning(
        "LLM response had no tool_use block for finn=%s", listing.get("finn_id")
    )
    return None


def analyze_listings(
    listings: list[dict],
    *,
    config: Optional[dict] = None,
) -> list[dict]:
    """Batch-analyze. Caches by finn_id with a 14-day TTL.

    Returns the listings list with an `llm` key merged onto each. If no API
    key is available or the call fails, the listing passes through unchanged
    (no `llm` key added).
    """
    global _cache
    if _cache is None:
        _cache = _load_cache()

    cfg = config or {}
    model = cfg.get("model", DEFAULT_MODEL)
    temperature = float(cfg.get("temperature", DEFAULT_TEMPERATURE))

    out: list[dict] = []
    fetched = 0
    cached_hits = 0
    failed = 0

    # If no client (no key), just pass through with a single warning.
    client_ready = _get_client() is not None

    for i, listing in enumerate(listings):
        finn_id = str(listing.get("finn_id") or "")
        merged = dict(listing)

        if not finn_id:
            out.append(merged)
            continue

        cached = _cache.get(finn_id) if client_ready or _cache else None
        if cached and _fresh(cached) and cached.get("data"):
            merged["llm"] = cached["data"]
            cached_hits += 1
        elif client_ready:
            llm_data = analyze_listing(
                listing, model=model, temperature=temperature
            )
            if llm_data is not None:
                _cache[finn_id] = {
                    "data": llm_data,
                    "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "model": model,
                }
                merged["llm"] = llm_data
                fetched += 1
                # Periodic save so a crash doesn't lose work.
                if fetched % 5 == 0:
                    _save_cache(_cache)
            else:
                failed += 1

        out.append(merged)

    _save_cache(_cache)
    if not client_ready:
        logger.info(
            "LLM analysis skipped (no client/key) — %d listings passed through",
            len(listings),
        )
    else:
        logger.info(
            "LLM analysis: %d cache hits, %d fetched, %d failed (model: %s)",
            cached_hits,
            fetched,
            failed,
            model,
        )
    return out


# ------------------------------------------------------------------ main ----


def main() -> int:
    """Inspect mode: run analysis for one cached listing, print result."""
    import logging
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )

    if len(sys.argv) < 2:
        print(
            "Usage: python -m src.llm <finn_id>\n"
            "  Analyzes a listing from data/listings_enriched.json.",
            file=sys.stderr,
        )
        return 2
    finn_id = sys.argv[1].strip()

    repo_root = Path(__file__).resolve().parent.parent
    os.chdir(repo_root)
    enriched_path = repo_root / "data" / "listings_enriched.json"
    if not enriched_path.exists():
        print(f"Missing {enriched_path}. Run main.py first.", file=sys.stderr)
        return 1
    enriched = json.loads(enriched_path.read_text())
    target = next((l for l in enriched if str(l.get("finn_id")) == finn_id), None)
    if not target:
        print(f"finn_id {finn_id} not in enriched data.", file=sys.stderr)
        return 1

    result = analyze_listing(target)
    if result is None:
        print("LLM analysis failed (check ANTHROPIC_API_KEY and logs).", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
