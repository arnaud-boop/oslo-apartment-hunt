"""HTML generator — render scored listings to a static digest page.

Reads `data/scored_latest.json`, renders templates/index.html.j2 with
helpers, copies static/style.css, writes everything into dist/.

That `dist/` folder is what the GitHub Action publishes to Pages.
"""

from __future__ import annotations

import json
import logging
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------- helpers ----


def fmt_nok(value) -> str:
    if value is None:
        return "—"
    try:
        n = int(value)
    except (TypeError, ValueError):
        return str(value)
    if abs(n) >= 1_000_000:
        return f"{n/1_000_000:.2f} M"
    if abs(n) >= 1_000:
        return f"{n/1_000:,.0f} k"
    return f"{n:,}"


def fmt_visning(iso: str) -> str:
    """Format an ISO timestamp into 'Sat 10 May 10:30 (Oslo)'."""
    try:
        t = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return str(iso)
    # Convert to local Oslo time. timezone-naive fallback if datetime lib lacks ZoneInfo.
    try:
        from zoneinfo import ZoneInfo
        t_local = t.astimezone(ZoneInfo("Europe/Oslo"))
    except Exception:
        t_local = t
    return t_local.strftime("%a %d %b %H:%M")


def score_tier(score) -> str:
    """CSS class for score badge color: high / mid / low."""
    try:
        s = float(score)
    except (TypeError, ValueError):
        return "tier-mid"
    if s >= 70:
        return "tier-high"
    if s < 50:
        return "tier-low"
    return "tier-mid"


# --------------------------------------------------------------- generate ----


def render(
    scored: list[dict],
    repo_root: Path,
    *,
    scraped_count: int,
    dropped_count: int,
    run_dt: datetime,
    out_dir: Path,
) -> Path:
    template_dir = repo_root / "templates"
    static_dir = repo_root / "static"

    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=select_autoescape(["html"]),
        trim_blocks=False,
        lstrip_blocks=False,
    )
    env.globals.update(
        fmt_nok=fmt_nok,
        fmt_visning=fmt_visning,
        score_tier=score_tier,
    )
    template = env.get_template("index.html.j2")

    html = template.render(
        scored=scored,
        scraped_count=scraped_count,
        dropped_count=dropped_count,
        kept_count=len(scored),
        run_iso=run_dt.isoformat(timespec="seconds"),
        run_human=run_dt.strftime("%a %d %b %Y, %H:%M"),
        run_date=run_dt.strftime("%Y-%m-%d"),
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    index_path = out_dir / "index.html"
    index_path.write_text(html, encoding="utf-8")

    # Copy static assets next to the HTML.
    css_src = static_dir / "style.css"
    if css_src.exists():
        shutil.copy2(css_src, out_dir / "style.css")

    logger.info(
        "Wrote %s (%d listings, %d KB)",
        index_path,
        len(scored),
        len(html) // 1024,
    )
    return index_path


# ------------------------------------------------------------------- main ----


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    repo_root = Path(__file__).resolve().parent.parent

    scored_path = repo_root / "data" / "scored_latest.json"
    if not scored_path.exists():
        print(
            f"Missing {scored_path}. Run `python -m src.scorer` first.",
            file=sys.stderr,
        )
        return 1

    scored = json.loads(scored_path.read_text())

    # Counts come from a separate metadata file or are passed in by main.py
    # in the orchestrated flow. Standalone, we infer:
    listings_path = repo_root / "data" / "listings_latest.json"
    scraped_count = (
        len(json.loads(listings_path.read_text())) if listings_path.exists() else len(scored)
    )
    dropped_count = max(0, scraped_count - len(scored))

    out_dir = repo_root / "dist"
    render(
        scored,
        repo_root,
        scraped_count=scraped_count,
        dropped_count=dropped_count,
        run_dt=datetime.now(timezone.utc),
        out_dir=out_dir,
    )
    print(f"Wrote {out_dir / 'index.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
