"""Salgsoppgave extraction.

Fetches each listing's salgsoppgave URL (the broker's full sales prospectus)
and extracts structured facts via Claude. Feeds the criteria checklist's
deferred items (bedroom sizes, wet-room count, bod, washing-machine
connection) plus richer LLM context (TG ratings, renovation history,
heating details).

Sources
-------
The salgsoppgave URL comes from `listing.prospectus_url` (set by the
detail-page enricher from `ad.prospectusView`). Brokers are heterogeneous:
- privatmegleren.no, dnb.no, eie.no, krogsveen.no, em1.no, ...
- Sometimes HTML pages, sometimes downloadable PDFs.

Pipeline
--------
1. Fetch the URL — auto-detect HTML vs PDF from Content-Type or extension
2. Strip to text — BeautifulSoup for HTML, pypdf for PDFs
3. Send up to 60 K characters of text to Claude Haiku with a structured
   extraction tool (no JSON-string parsing — Anthropic validates the
   schema)
4. Cache per finn_id for 30 days at .cache/salgsoppgave.json

Cost
----
Haiku 4.5 is ~12× cheaper than Sonnet for similar structured-extraction
tasks. Estimated cost: ~$0.50-1 per fresh full run for ~70 listings.
With cache, near zero day-to-day.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)

CACHE_DIR = Path(".cache")
CACHE_FILE = CACHE_DIR / "salgsoppgave.json"
CACHE_TTL_DAYS = 30

USER_AGENT = (
    "oslo-apartment-hunt/0.1 (personal use; arnaud.dupuis@farmforce.com)"
)
REQUEST_TIMEOUT_S = 30
RATE_LIMIT_S = 2.0  # broker-side politeness
LLM_RATE_LIMIT_S = 0.4

DEFAULT_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_TEMPERATURE = 0.1
DEFAULT_MAX_TOKENS = 2048

# Cap input at ~20 K tokens — many salgsoppgaves are 50-200 pages of legal
# boilerplate; bumping from 60K to 80K so red-flag-bearing sections
# (sameie disputes, planned assessments) further into the doc make it in.
TEXT_CHAR_LIMIT = 80000

# Module singletons.
_session: Optional[requests.Session] = None
_cache: Optional[dict] = None
_anthropic_client: Any = None
_last_fetch_t: float = 0.0
_last_llm_t: float = 0.0


# ----------------------------------------------------- extraction tool ----

EXTRACTION_TOOL = {
    "name": "submit_salgsoppgave_extraction",
    "description": (
        "Submit structured data extracted from a Norwegian salgsoppgave "
        "(real-estate sales prospectus). Use null when the document does "
        "not state a value clearly. Be conservative — don't infer."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "bedrooms_count": {
                "type": ["integer", "null"],
                "description": "Number of bedrooms (soverom).",
            },
            "bedroom_sizes_m2": {
                "type": ["array", "null"],
                "items": {"type": "number"},
                "description": (
                    "Each bedroom's size in m². Order doesn't matter. Null "
                    "if the doc lacks per-room sizes."
                ),
            },
            "smallest_bedroom_m2": {
                "type": ["number", "null"],
                "description": "Size of the smallest bedroom in m².",
            },
            "bedroom_quality_descriptor": {
                "type": ["string", "null"],
                "description": (
                    "Qualitative descriptor when m² sizes aren't given. "
                    "E.g. 'gode soverom', 'romslige soverom', 'luftige', "
                    "'små soverom', 'store soverom'. Quote the salgsoppgave "
                    "wording where possible. Null only if no descriptor."
                ),
            },
            "wet_rooms_count": {
                "type": ["integer", "null"],
                "description": (
                    "Total wet rooms: bathrooms + WCs + standalone showers. "
                    "Each separate wet room counts once."
                ),
            },
            "bathrooms_count": {
                "type": ["integer", "null"],
                "description": "Bathrooms (bad), excluding WC-only rooms.",
            },
            "has_bod": {
                "type": ["boolean", "null"],
                "description": (
                    "Listing has dedicated storage room (bod), even if "
                    "outside the unit (kjellerbod, loftbod). True iff "
                    "explicitly mentioned."
                ),
            },
            "bod_size_m2": {
                "type": ["number", "null"],
                "description": "Bod size in m², if stated.",
            },
            "has_washing_machine_connection": {
                "type": ["boolean", "null"],
                "description": (
                    "In-unit washing-machine connection (vaskemaskinopplegg). "
                    "Look in technical specs / room descriptions."
                ),
            },
            "floor": {
                "type": ["integer", "null"],
                "description": (
                    "Floor number (etasje). 1 = ground floor in Norway. "
                    "0 = basement. Null if not stated."
                ),
            },
            "construction_year": {"type": ["integer", "null"]},
            "renovations": {
                "type": ["array", "null"],
                "items": {
                    "type": "object",
                    "properties": {
                        "what": {"type": "string"},
                        "year": {"type": "integer"},
                    },
                    "required": ["what", "year"],
                },
                "description": (
                    "Notable renovations with year. Skip cosmetic (painting). "
                    "Focus on kitchen, bath, electrical, plumbing, roof, "
                    "windows, façade, heating system."
                ),
            },
            "tg_issues": {
                "type": ["array", "null"],
                "items": {
                    "type": "object",
                    "properties": {
                        "what": {"type": "string"},
                        "tg": {"type": "integer", "minimum": 1, "maximum": 3},
                        "note": {"type": "string"},
                    },
                    "required": ["what", "tg"],
                },
                "description": (
                    "Tilstandsgrad issues. TG2 = needs attention, TG3 = "
                    "needs immediate action. List notable TG2/TG3 only "
                    "(skip TG1 which is normal wear)."
                ),
            },
            "heating_type": {
                "type": ["string", "null"],
                "enum": [
                    "collective", "individual_electric", "mixed",
                    "district_heating", "heat_pump", None,
                ],
            },
            "heating_details": {
                "type": ["string", "null"],
                "description": "1-sentence heating setup summary.",
            },
            "felleskostnader_includes": {
                "type": ["array", "null"],
                "items": {
                    "type": "string",
                    "enum": [
                        "heat", "hot_water", "internet", "cable_tv",
                        "communal_garage", "buildings_insurance",
                        "loan_repayment", "kommunale_avgifter",
                        "vaktmester", "trappevask", "other",
                    ],
                },
                "description": "What's included in fellesutgifter.",
            },
            "plot_size_m2": {"type": ["number", "null"]},
            "energy_class": {
                "type": ["string", "null"],
                "description": "A-G if stated.",
            },
            "primary_address": {
                "type": ["string", "null"],
                "description": "Full address as stated in salgsoppgave.",
            },
            "red_flags": {
                "type": ["array", "null"],
                "items": {
                    "type": "object",
                    "properties": {
                        "category": {
                            "type": "string",
                            "enum": ["technical", "legal", "economic", "sameie", "other"],
                        },
                        "severity": {
                            "type": "string",
                            "enum": ["low", "medium", "high"],
                        },
                        "title": {
                            "type": "string",
                            "description": "Short title (≤ 8 words).",
                        },
                        "description": {
                            "type": "string",
                            "description": (
                                "1-2 sentence summary of the issue and "
                                "why a buyer should care."
                            ),
                        },
                    },
                    "required": ["category", "severity", "title", "description"],
                },
                "description": (
                    "Significant issues a buyer should know about. "
                    "TECHNICAL: TG3 ratings, structural problems, leaks, "
                    "mold, urgent work needed.  LEGAL: lawsuits, "
                    "encumbrances, easements, ongoing disputes.  ECONOMIC: "
                    "planned major assessments the buyer will share, "
                    "rapid fellesgjeld growth, large deferred maintenance, "
                    "special-purpose collective loans.  SAMEIE: governance "
                    "dysfunction, restrictive vedtekter that materially "
                    "affect use, conflicts.  Skip routine TG2 wear unless "
                    "it implies near-term cost. Be conservative — only "
                    "list issues that genuinely matter to a buyer's "
                    "decision."
                ),
            },
            "extraction_confidence": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
                "description": (
                    "Your overall confidence (0-1) in this extraction, "
                    "based on how much the doc states clearly vs is ambiguous."
                ),
            },
        },
        "required": ["extraction_confidence"],
    },
}


_SYSTEM = (
    "You're extracting structured data from a Norwegian salgsoppgave "
    "(real-estate sales prospectus). Call the submit_salgsoppgave_extraction "
    "tool with the structured fields.\n"
    "\n"
    "Rule of thumb: be CONSERVATIVE about inference, but TRUST explicit "
    "statements even when brief.\n"
    "\n"
    "EXTRACTION PATTERNS:\n"
    "\n"
    "Wet rooms / bathrooms:\n"
    "  - 'Bad 1' alone (no Bad 2 anywhere) → bathrooms_count=1\n"
    "  - 'Bad 1' + 'Bad 2' → bathrooms_count=2\n"
    "  - 'WC' or 'separat toalett' as separate room → adds to wet_rooms_count\n"
    "  - wet_rooms_count = bathrooms + standalone WCs\n"
    "  - When the doc lists exactly one bathroom and no separate WC, set "
    "wet_rooms_count=1 (don't return null).\n"
    "\n"
    "Bod (storage):\n"
    "  - 'Bod', 'Kjellerbod', 'Loftbod', 'Sportsbod', 'Innvendig bod' anywhere "
    "→ has_bod=true\n"
    "  - 'Stor kjellerbod (10 kvm)' → has_bod=true, bod_size_m2=10\n"
    "  - Only return false if the doc explicitly says no storage exists\n"
    "\n"
    "Bedrooms:\n"
    "  - Per-room sizes given (e.g. 'Soverom 1: 12,5 kvm') → bedroom_sizes_m2 "
    "with each value, smallest_bedroom_m2 with the min\n"
    "  - Only count given (e.g. '4 soverom', '4 gode soverom') → "
    "bedroom_sizes_m2=null AND bedroom_quality_descriptor='gode soverom' "
    "(quote the wording)\n"
    "  - 'hvorav 1 med hemsløsning' is meaningful — note that the smallest "
    "bedroom may be a loft/mezzanine; reflect in the descriptor\n"
    "\n"
    "Red flags — items a buyer would want to know upfront:\n"
    "  - TG3 anywhere → high-severity technical flag\n"
    "  - TG2 implying near-term cost (roof, foundation, plumbing, electrical) "
    "→ medium technical flag\n"
    "  - Routine TG2 wear (paint, surface scratches) → skip\n"
    "  - 'Pålegg' from kommune, ongoing or recent lawsuits, easements that "
    "constrain use → legal flag\n"
    "  - Planned major sameie work the buyer will pay for → economic flag\n"
    "  - High collective debt or rapid debt growth → economic flag\n"
    "  - Sameie governance problems / restrictive vedtekter → sameie flag\n"
    "  Each red_flag: category, severity, short title, 1-2 sentence "
    "description with concrete details from the doc.\n"
    "\n"
    "Use null only when the doc is genuinely silent — not when you have "
    "to do light interpretation. The document may be 50-200 pages of "
    "legal boilerplate — focus on Tilstand, Standard, Areal, Rom-for-rom, "
    "Sameiet, Økonomi, Servitutter, and Diverse sections."
)


# ----------------------------------------------------------- fetch --------


def _build_session() -> requests.Session:
    global _session
    if _session is None:
        s = requests.Session()
        s.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/pdf,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,nb;q=0.8",
        })
        _session = s
    return _session


def _rate_limited_get(url: str, session: requests.Session) -> requests.Response:
    global _last_fetch_t
    elapsed = time.monotonic() - _last_fetch_t
    if elapsed < RATE_LIMIT_S:
        time.sleep(RATE_LIMIT_S - elapsed)
    _last_fetch_t = time.monotonic()
    return session.get(url, timeout=REQUEST_TIMEOUT_S, allow_redirects=True)


def _extract_text_html(html: str) -> str:
    """Strip HTML to readable text, dropping nav/script/style noise."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    for tag in soup.find_all(["script", "style", "nav", "header", "footer", "form"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def _extract_text_pdf(pdf_bytes: bytes) -> str:
    """Extract text from PDF bytes via pypdf."""
    try:
        from pypdf import PdfReader
    except ImportError:
        logger.error(
            "pypdf not installed — can't process PDF salgsoppgaver. "
            "Add pypdf to requirements.txt."
        )
        return ""
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
    except Exception as e:
        logger.warning("Failed to open PDF: %s", e)
        return ""
    pages: list[str] = []
    for i, page in enumerate(reader.pages):
        try:
            pages.append(page.extract_text() or "")
        except Exception as e:
            logger.warning("PDF page %d extract failed: %s", i, e)
    text = "\n\n".join(pages)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def fetch_salgsoppgave_text(url: str) -> Optional[str]:
    """Fetch URL, return extracted text. None on failure or empty."""
    if not url:
        return None
    session = _build_session()
    try:
        resp = _rate_limited_get(url, session)
    except requests.RequestException as e:
        logger.warning("Salgsoppgave fetch failed for %s: %s", url, e)
        return None
    if resp.status_code != 200:
        logger.warning(
            "Salgsoppgave returned HTTP %d for %s", resp.status_code, url
        )
        return None

    content_type = (resp.headers.get("content-type") or "").lower()
    if "pdf" in content_type or url.lower().split("?")[0].endswith(".pdf"):
        text = _extract_text_pdf(resp.content)
    else:
        text = _extract_text_html(resp.text)
    if not text or len(text) < 200:
        logger.warning(
            "Salgsoppgave at %s yielded suspiciously little text (%d chars)",
            url, len(text or ""),
        )
        return None
    return text


# ----------------------------------------------------- LLM extraction -----


def _get_anthropic_client():
    global _anthropic_client
    if _anthropic_client is None:
        try:
            import anthropic
        except ImportError:
            logger.error(
                "anthropic package not installed — salgsoppgave extraction disabled"
            )
            return None
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            logger.warning(
                "ANTHROPIC_API_KEY not set — salgsoppgave extraction disabled"
            )
            return None
        _anthropic_client = anthropic.Anthropic(api_key=api_key)
    return _anthropic_client


def extract_with_claude(
    text: str,
    listing_context: dict,
    *,
    model: str = DEFAULT_MODEL,
    temperature: float = DEFAULT_TEMPERATURE,
) -> Optional[dict]:
    """Send text + listing context to Claude, return structured extraction
    or None on any failure."""
    global _last_llm_t
    client = _get_anthropic_client()
    if client is None:
        return None

    elapsed = time.monotonic() - _last_llm_t
    if elapsed < LLM_RATE_LIMIT_S:
        time.sleep(LLM_RATE_LIMIT_S - elapsed)

    truncated = text[:TEXT_CHAR_LIMIT]
    if len(text) > TEXT_CHAR_LIMIT:
        logger.info(
            "Truncated salgsoppgave for finn=%s from %d → %d chars",
            listing_context.get("finn_id"), len(text), TEXT_CHAR_LIMIT,
        )

    user_prompt = (
        "=== Listing context (from Finn) ===\n"
        f"finn_id: {listing_context.get('finn_id')}\n"
        f"address: {listing_context.get('address')}\n"
        f"property_type: {listing_context.get('property_type')}\n"
        f"area_finn: {listing_context.get('area_m2')} m²\n"
        f"bedrooms_finn: {listing_context.get('bedrooms')}\n"
        "\n"
        "=== Salgsoppgave text ===\n"
        f"{truncated}\n"
        "\n"
        "Now call submit_salgsoppgave_extraction with the structured fields."
    )

    try:
        response = client.messages.create(
            model=model,
            max_tokens=DEFAULT_MAX_TOKENS,
            temperature=temperature,
            system=_SYSTEM,
            tools=[EXTRACTION_TOOL],
            tool_choice={"type": "tool", "name": EXTRACTION_TOOL["name"]},
            messages=[{"role": "user", "content": user_prompt}],
        )
    except Exception as e:
        logger.warning(
            "Claude error for salgsoppgave finn=%s: %s",
            listing_context.get("finn_id"), e,
        )
        return None
    finally:
        _last_llm_t = time.monotonic()

    for block in response.content:
        if getattr(block, "type", None) == "tool_use":
            return dict(block.input or {})
    return None


# ----------------------------------------------------- caching ------------


def _load_cache() -> dict:
    if not CACHE_FILE.exists():
        return {}
    try:
        return json.loads(CACHE_FILE.read_text())
    except json.JSONDecodeError:
        logger.warning("salgsoppgave cache corrupt; starting fresh")
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


# ----------------------------------------------------- pipeline -----------


def enrich_with_salgsoppgave(
    listings: list[dict],
    *,
    config: Optional[dict] = None,
) -> list[dict]:
    """For each listing with a prospectus_url, fetch + analyze its
    salgsoppgave. Merge result into listing dict under `salgsoppgave`.

    Failures pass the listing through unchanged (no `salgsoppgave` key).
    Listings without a prospectus_url also pass through unchanged.
    """
    global _cache
    if _cache is None:
        _cache = _load_cache()

    cfg = (config or {}).get("salgsoppgave", {}) or {}
    if not cfg.get("active", True):
        logger.info("Salgsoppgave extraction disabled in config")
        return listings

    model = cfg.get("model", DEFAULT_MODEL)
    if _get_anthropic_client() is None:
        logger.info(
            "Salgsoppgave extraction skipped (no API key / client) — "
            "passing %d listings through", len(listings),
        )
        return listings

    out: list[dict] = []
    fetched_count = 0
    cached_count = 0
    failed_count = 0
    no_url_count = 0

    for i, l in enumerate(listings):
        finn_id = str(l.get("finn_id") or "")
        merged = dict(l)
        prospectus_url = l.get("prospectus_url")

        if not prospectus_url:
            no_url_count += 1
            out.append(merged)
            continue

        if finn_id and finn_id in _cache and _fresh(_cache[finn_id]):
            data = _cache[finn_id].get("data")
            if data:
                merged["salgsoppgave"] = data
                cached_count += 1
            out.append(merged)
            continue

        text = fetch_salgsoppgave_text(prospectus_url)
        if not text:
            failed_count += 1
            out.append(merged)
            continue

        listing_context = {
            "finn_id": finn_id,
            "address": l.get("address"),
            "property_type": l.get("property_type"),
            "area_m2": l.get("area_m2"),
            "bedrooms": l.get("bedrooms"),
        }
        data = extract_with_claude(text, listing_context, model=model)
        if data is None:
            failed_count += 1
            out.append(merged)
            continue

        if finn_id:
            _cache[finn_id] = {
                "url": prospectus_url,
                "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "model": model,
                "data": data,
            }
            if (fetched_count + 1) % 5 == 0:
                _save_cache(_cache)
        merged["salgsoppgave"] = data
        out.append(merged)
        fetched_count += 1

    _save_cache(_cache)
    logger.info(
        "Salgsoppgave: %d fetched fresh, %d from cache, %d failed, %d no URL",
        fetched_count, cached_count, failed_count, no_url_count,
    )
    return out
