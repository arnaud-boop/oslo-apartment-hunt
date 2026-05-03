"""Finn.no scraper — fetch search results, decode the React-Router turbo-stream
payload, return structured Listings.

Approach
--------
Finn.no migrated from Next.js to React Router 7. Search-result data lives in
an inline <script> that calls
    window.__reactRouterContext.streamController.enqueue("...")
with a JSON-encoded turbo-stream array (graph encoding: dict keys like "_N"
point at array index N).

Pipeline:
  fetch HTML → find the streamController.enqueue call → JSON-decode twice
  → resolve the turbo-stream graph → drill into
    loaderData["routes/realestate+/_search+/$subvertical.search[.html]"]
    .results
  which has `docs` (the listings) and `metadata.paging.last` (page count).

If Finn's route key changes, we fall back to scanning loaderData for any
route whose value looks like a search-results object (has a `results.docs`
list).

Failure mode: HTTP errors or unparseable page → raise ScrapeError so the
GH Actions workflow goes red. Empty result set → return [] with a warning.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

USER_AGENT = (
    "oslo-apartment-hunt/0.1 (personal use; arnaud.dupuis@farmforce.com)"
)
REQUEST_TIMEOUT_S = 20
RATE_LIMIT_S = 2.0
MAX_PAGES = 50  # safety cap

CACHE_DIR = Path(".cache")

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ types ----


class ScrapeError(Exception):
    """Hard failure — propagate to fail the workflow."""


@dataclass
class Listing:
    """Subset of fields available from Finn search results.

    Detail-page enrichment (floor, orientation, wet rooms, bod, washing
    machine, sold status, full description) is a follow-up.
    """

    finn_id: str
    title: str | None = None
    url: str | None = None
    address: str | None = None
    local_area: str | None = None
    asking_price: int | None = None
    total_price: int | None = None
    fellesgjeld: int | None = None
    fellesutgifter_month: int | None = None
    area_m2: float | None = None
    plot_m2: float | None = None
    bedrooms: int | None = None
    image_url: str | None = None
    image_urls: list[str] = field(default_factory=list)
    property_type: str | None = None  # "Leilighet" / "Enebolig" / ...
    owner_type: str | None = None     # "Selveier" / "Andel" / ...
    viewing_times: list[str] = field(default_factory=list)
    coordinates: dict | None = None   # {lat, lon}
    timestamp_ms: int | None = None
    ad_type: int | None = None        # 1 = single listing, 12 = project/range
    flags: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


# ----------------------------------------------------------------- fetch ----


def _build_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,nb;q=0.8",
        }
    )
    return s


def _set_page_param(url: str, page: int) -> str:
    parsed = urlparse(url)
    qs = dict(parse_qsl(parsed.query, keep_blank_values=True))
    qs["page"] = str(page)
    return urlunparse(parsed._replace(query=urlencode(qs)))


def _fetch_html(url: str, session: requests.Session) -> str:
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


# ---------------------------------------------------------- turbo-stream ----


_ENQUEUE_RE = re.compile(
    r'streamController\.enqueue\((".*?")\)\s*;?\s*$', re.DOTALL
)


def _extract_stream_payload(html: str) -> list:
    """Pull the turbo-stream array out of the inline streamController script."""
    soup = BeautifulSoup(html, "lxml")
    candidate = None
    for s in soup.find_all("script"):
        if s.get("src") or s.get("type"):
            continue
        text = s.string or ""
        if "streamController.enqueue" in text:
            candidate = text
            break
    if candidate is None:
        raise ScrapeError(
            "No streamController.enqueue() inline <script> found. "
            "Finn page shape may have changed."
        )
    m = _ENQUEUE_RE.search(candidate)
    if not m:
        raise ScrapeError(
            "streamController.enqueue() found but argument failed to match. "
            "Format change?"
        )
    js_string = m.group(1)
    try:
        decoded_string = json.loads(js_string)   # unescape JS string literal
        arr = json.loads(decoded_string)         # parse array
    except json.JSONDecodeError as e:
        raise ScrapeError(f"Failed to parse turbo-stream payload: {e}") from e
    if not isinstance(arr, list) or not arr:
        raise ScrapeError("turbo-stream payload is not a non-empty list")
    return arr


def _resolve_turbo_stream(arr: list) -> Any:
    """Resolve the indexed-graph encoding into nested Python objects.

    Format: dict keys "_N" mean "key name is at arr[N]"; dict values that are
    non-negative ints are array indices to recurse into. Negative ints are
    sentinels (None / undefined). Strings/numbers/lists pass through.
    """

    def resolve(idx_or_val: Any, seen: frozenset[int]) -> Any:
        if isinstance(idx_or_val, int):
            if idx_or_val < 0:
                return None  # sentinel
            if idx_or_val in seen or idx_or_val >= len(arr):
                return None
            return walk(arr[idx_or_val], seen | {idx_or_val})
        return walk(idx_or_val, seen)

    def walk(val: Any, seen: frozenset[int]) -> Any:
        if isinstance(val, dict):
            out: dict = {}
            for k, v in val.items():
                if k.startswith("_"):
                    try:
                        key_idx = int(k[1:])
                        key_name = arr[key_idx] if 0 <= key_idx < len(arr) else k
                    except ValueError:
                        key_name = k
                    if not isinstance(key_name, str):
                        key_name = str(key_name)
                else:
                    key_name = k
                out[key_name] = resolve(v, seen) if isinstance(v, int) else walk(v, seen)
            return out
        if isinstance(val, list):
            return [resolve(e, seen) if isinstance(e, int) else walk(e, seen) for e in val]
        return val

    return resolve(0, frozenset())


def _find_search_results(loader_data: dict) -> dict:
    """Locate the search route's results dict.

    Primary: the well-known route key. Fallback: scan loaderData values for any
    dict containing a `results.docs` list.
    """
    primary = "routes/realestate+/_search+/$subvertical.search[.html]"
    if primary in loader_data and isinstance(loader_data[primary], dict):
        node = loader_data[primary]
        if "results" in node and isinstance(node["results"], dict):
            return node["results"]
    # Fallback scan
    for key, val in loader_data.items():
        if not isinstance(val, dict):
            continue
        if (
            "results" in val
            and isinstance(val["results"], dict)
            and isinstance(val["results"].get("docs"), list)
        ):
            logger.warning(
                "Primary search route key not found; using fallback %r", key
            )
            return val["results"]
    raise ScrapeError(
        "Could not find search results in loaderData. "
        f"Available routes: {list(loader_data.keys())}"
    )


# ------------------------------------------------------------ doc → Listing ----


def _doc_to_listing(doc: dict) -> Listing:
    """Map a Finn search-result doc to our Listing."""
    finn_id = str(doc.get("ad_id") or doc.get("id") or "")
    asking = (doc.get("price_suggestion") or {}).get("amount")
    total = (doc.get("price_total") or {}).get("amount")
    monthly = (doc.get("price_shared_cost") or {}).get("amount")
    fellesgjeld = None
    if isinstance(asking, (int, float)) and isinstance(total, (int, float)):
        fellesgjeld = int(total - asking)

    area_range = doc.get("area_range") or {}
    area_m2 = area_range.get("size_from") or area_range.get("size_to")

    plot = (doc.get("area_plot") or {}).get("size")

    image = doc.get("image") or {}
    image_url = image.get("url") if isinstance(image, dict) else None

    return Listing(
        finn_id=finn_id,
        title=doc.get("heading"),
        url=doc.get("canonical_url"),
        address=doc.get("location"),
        local_area=doc.get("local_area_name"),
        asking_price=int(asking) if asking is not None else None,
        total_price=int(total) if total is not None else None,
        fellesgjeld=fellesgjeld,
        fellesutgifter_month=int(monthly) if monthly is not None else None,
        area_m2=float(area_m2) if area_m2 is not None else None,
        plot_m2=float(plot) if plot is not None else None,
        bedrooms=doc.get("number_of_bedrooms"),
        image_url=image_url,
        image_urls=list(doc.get("image_urls") or []),
        property_type=doc.get("property_type_description"),
        owner_type=(doc.get("owner_type_description") or "").strip() or None,
        viewing_times=list(doc.get("viewing_times") or []),
        coordinates=doc.get("coordinates"),
        timestamp_ms=doc.get("timestamp"),
        ad_type=doc.get("ad_type"),
        flags=list(doc.get("flags") or []),
        labels=list(doc.get("labels") or []),
        raw=doc,
    )


# ----------------------------------------------------------- public api ----


def fetch_search_results(
    search_url: str,
    *,
    max_pages: int = MAX_PAGES,
    rate_limit_s: float = RATE_LIMIT_S,
    debug: bool = False,
    include_projects: bool = False,
) -> list[Listing]:
    """Fetch all pages of a Finn search and return parsed Listings.

    By default, ad_type 12 (range/project listings) are excluded — they
    represent multi-unit projects, not single buyable apartments.

    Raises ScrapeError on hard failures (HTTP errors, missing payload).
    Returns [] only when Finn legitimately has no results.
    """
    session = _build_session()
    all_listings: list[Listing] = []
    seen_ids: set[str] = set()
    last_page: int | None = None
    skipped_projects = 0

    if debug:
        CACHE_DIR.mkdir(exist_ok=True)

    for page in range(1, max_pages + 1):
        page_url = _set_page_param(search_url, page)
        html = _fetch_html(page_url, session)

        if debug and page == 1:
            (CACHE_DIR / "page1.html").write_text(html)
            logger.info("debug: wrote .cache/page1.html (%d bytes)", len(html))

        arr = _extract_stream_payload(html)
        tree = _resolve_turbo_stream(arr)

        if debug and page == 1:
            (CACHE_DIR / "page1.resolved.json").write_text(
                json.dumps(tree, indent=2, ensure_ascii=False, default=str)
            )
            logger.info("debug: wrote .cache/page1.resolved.json")

        loader_data = (tree or {}).get("loaderData") or {}
        if not isinstance(loader_data, dict):
            raise ScrapeError("loaderData not present in resolved tree")

        results = _find_search_results(loader_data)
        docs = results.get("docs") or []

        if last_page is None:
            paging = results.get("metadata", {}).get("paging", {})
            last_page = paging.get("last")
            if last_page:
                logger.info(
                    "Page 1: %d docs on page, total pages = %d",
                    len(docs),
                    last_page,
                )

        page_new = 0
        for doc in docs:
            listing = _doc_to_listing(doc)
            if not include_projects and listing.ad_type == 12:
                skipped_projects += 1
                continue
            if not listing.finn_id or listing.finn_id in seen_ids:
                continue
            seen_ids.add(listing.finn_id)
            all_listings.append(listing)
            page_new += 1

        logger.info(
            "Page %d: %d docs, %d new (running total: %d)",
            page,
            len(docs),
            page_new,
            len(all_listings),
        )

        if not docs:
            logger.info("Page %d returned 0 docs, stopping", page)
            break
        if last_page and page >= last_page:
            logger.info("Reached last page %d", page)
            break

        time.sleep(rate_limit_s)

    if skipped_projects:
        logger.info(
            "Skipped %d ad_type=12 (project/range) listings", skipped_projects
        )
    if not all_listings:
        logger.warning("Finn returned 0 listings for this search")
    return all_listings


# ----------------------------------------------------------------- main ----


def _read_search_url(repo_root: Path) -> str:
    p = repo_root / "finn-search-url.txt"
    if not p.exists():
        raise ScrapeError(f"Missing {p}. Drop the live Finn search URL in there.")
    url = p.read_text().strip().splitlines()[0].strip()
    if not url.startswith("http"):
        raise ScrapeError(f"finn-search-url.txt does not start with http: {url!r}")
    return url


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    repo_root = Path(__file__).resolve().parent.parent
    os.chdir(repo_root)

    debug = os.environ.get("DEBUG_SCRAPER", "1") == "1"  # default on for dev
    only_first_page = os.environ.get("FIRST_PAGE_ONLY", "0") == "1"
    url = _read_search_url(repo_root)
    logger.info("Search URL: %s", url)

    max_pages = 1 if only_first_page else MAX_PAGES
    listings = fetch_search_results(url, max_pages=max_pages, debug=debug)

    print(f"\n=== {len(listings)} listings ===\n")
    for l in listings[:5]:
        print(
            f"  finn={l.finn_id} | {l.property_type} | {l.owner_type}\n"
            f"    {l.title[:100] if l.title else ''}\n"
            f"    addr={l.address!r}  area={l.area_m2}m²  beds={l.bedrooms}\n"
            f"    ask={l.asking_price:,}  total={l.total_price:,}  "
            f"gjeld={l.fellesgjeld:,}  felles/mnd={l.fellesutgifter_month}\n"
            if l.asking_price and l.total_price and l.fellesgjeld is not None
            else f"    addr={l.address!r}  area={l.area_m2}m²  beds={l.bedrooms}\n"
                 f"    ask={l.asking_price}  total={l.total_price}  "
                 f"gjeld={l.fellesgjeld}  felles/mnd={l.fellesutgifter_month}\n"
        )
        print(
            f"    visning={l.viewing_times}\n"
            f"    coords={l.coordinates}  ad_type={l.ad_type}\n"
            f"    {l.url}\n"
        )
    if len(listings) > 5:
        print(f"  ... and {len(listings) - 5} more")

    # Write a JSON dump for downstream pipeline + manual inspection
    out_path = repo_root / "data" / "listings_latest.json"
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(
        json.dumps(
            [l.to_dict() for l in listings], indent=2, ensure_ascii=False
        )
    )
    print(f"\nWrote {out_path} ({len(listings)} listings)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
