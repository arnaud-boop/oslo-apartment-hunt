"""Vote-fetching for the daily pipeline.

Calls the Google Apps Script Web App's GET endpoint and returns the current
votes/notes keyed by finn_id. The result is baked into the rendered HTML at
build time so that even users with JS disabled see the latest known state.
The page also fetches votes live on load via votes.js — this is the
belt-and-suspenders path.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Optional

import requests

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_S = 15


def fetch_votes(web_app_url: Optional[str]) -> dict:
    """Return votes structured for template rendering:

        {
            "<finn_id>": {
                "arnaud": {"vote": "up"|"down"|"", "note": "...", "updated_at": "..."},
                "celine": {...},
            }
        }

    Empty dict on any failure (missing URL, network error, JSON parse error).
    The pipeline must not abort just because the voting backend is down.
    """
    if not web_app_url:
        logger.info("voting.web_app_url not configured — skipping vote fetch")
        return {}

    try:
        r = requests.get(web_app_url, timeout=REQUEST_TIMEOUT_S)
        r.raise_for_status()
        data = r.json()
    except requests.RequestException as e:
        logger.warning("Failed to fetch votes from Apps Script: %s", e)
        return {}
    except ValueError as e:
        logger.warning("Apps Script returned non-JSON: %s", e)
        return {}

    rows = data.get("votes") or []
    out: dict[str, dict[str, dict]] = defaultdict(dict)
    for row in rows:
        finn_id = str(row.get("finn_id") or "").strip()
        voter = str(row.get("voter") or "").strip().lower()
        if not finn_id or not voter:
            continue
        out[finn_id][voter] = {
            "vote": str(row.get("vote") or ""),
            "note": str(row.get("note") or ""),
            "updated_at": str(row.get("updated_at") or ""),
        }
    logger.info("Fetched votes for %d listing(s)", len(out))
    return dict(out)
