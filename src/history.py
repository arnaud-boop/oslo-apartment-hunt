"""Listing history tracker.

Maintains `.cache/history.json` across runs:

    {
        "last_run_at": "<iso>",
        "entries": {
            "<finn_id>": {"first_seen": "<iso>", "last_seen": "<iso>"},
            ...
        }
    }

`update_history(scraped, now)` returns `(history_dict, set_of_new_finn_ids)`,
where `set_of_new_finn_ids` is the listings whose `first_seen` was set by
THIS run — i.e., the new arrivals in today's batch. The pipeline passes
that set to the renderer so the digest can show:

    - a banner counter at the top of the page ("🆕 N new in today's batch")
    - the data-new-in-batch attribute on each newly-arrived card

The per-listing 🆕 NEW tag itself is governed by *voter ack state* (read
from the Sheet votes), not by this history — the tag persists until each
voter has voted or saved a non-empty note. History is what powers the
*banner*, plus longer-term analytics if we ever build a "what changed"
view.

Persisted in `.cache/` so it's covered by the same actions/cache
preservation as enrichment, transit, and llm caches.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

CACHE_DIR = Path(".cache")
HISTORY_FILE = CACHE_DIR / "history.json"


def _load_history() -> dict:
    if not HISTORY_FILE.exists():
        return {"last_run_at": None, "entries": {}}
    try:
        data = json.loads(HISTORY_FILE.read_text())
    except json.JSONDecodeError:
        logger.warning("history.json corrupt; starting fresh")
        return {"last_run_at": None, "entries": {}}
    if "entries" not in data:
        data["entries"] = {}
    if "last_run_at" not in data:
        data["last_run_at"] = None
    return data


def _save_history(history: dict) -> None:
    CACHE_DIR.mkdir(exist_ok=True)
    HISTORY_FILE.write_text(json.dumps(history, indent=2, ensure_ascii=False))


def update_history(
    scraped_listings: list,
    now: Optional[datetime] = None,
) -> tuple[dict, set[str]]:
    """Update history with the current scrape.

    Returns:
        (history, new_in_batch) — `new_in_batch` is the set of finn_ids
        whose `first_seen` was set by this call (i.e., not previously known).
    """
    if now is None:
        now = datetime.now(timezone.utc)
    iso = now.isoformat(timespec="seconds")

    history = _load_history()
    new_in_batch: set[str] = set()

    for l in scraped_listings:
        finn_id = str(l.get("finn_id") or "").strip()
        if not finn_id:
            continue
        entry = history["entries"].get(finn_id)
        if entry is None:
            history["entries"][finn_id] = {"first_seen": iso, "last_seen": iso}
            new_in_batch.add(finn_id)
        else:
            entry["last_seen"] = iso

    history["last_run_at"] = iso
    _save_history(history)

    logger.info(
        "History updated: %d total entries; %d new arrivals in this batch",
        len(history["entries"]),
        len(new_in_batch),
    )
    return history, new_in_batch
