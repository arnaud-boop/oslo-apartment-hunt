# Oslo Apartment Hunt

Personal real estate research agent. Scrapes Finn.no daily, scores listings against
our criteria, publishes a ranked HTML digest to GitHub Pages.

## Local run

```bash
pip install -r requirements.txt
python -m src.main
```

Output is written to `dist/index.html`.

## Layout

- `src/` — scraper, filters, scorer, HTML generator
- `config/scoring_config.yaml` — hand-editable weights and thresholds
- `templates/` — Jinja2 HTML template
- `static/` — CSS
- `data/` — listing history (committed)
- `finn-search-url.txt` — the live Finn search query
- `.github/workflows/daily-digest.yml` — daily cron + manual trigger

## Status

v1 in progress. See project instructions for scope and milestones.
