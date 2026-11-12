# Alt-Data Pipeline: Automated Tech & SaaS Signal Feed

A lean, compliance-first **Point-in-Time (PIT)** alternative data engineering pipeline built in Python, Pydantic, `httpx`, and DuckDB/Parquet.

It tracks two legally clean, publicly accessible signal streams:
1. **Public ATS Job Board Feeds** (Greenhouse & Lever public JSON career endpoints) — hiring velocity shifts, engineering/AI role mix, and role open/close lifecycles.
2. **Public SaaS Pricing Pages** (`/pricing`) — seat/usage price changes, billing cadence shifts, free-tier modifications, and enterprise gating.

## Compliance & Data Hygiene Rules

- **Public-only ingestion:** Strictly unauthenticated public pages/feeds. Zero login bypassing, zero CAPTCHA solving, zero LinkedIn scraping.
- **`robots.txt` enforcement:** Every domain's `robots.txt` is fetched and evaluated before any target request; disallowed paths or inaccessible `robots.txt` (`401`/`403`/`5xx`) fail closed.
- **Polite crawling:** Per-domain rate limiting with jitter, honoring `Crawl-delay`, explicit `User-Agent` identification, and conditional `ETag`/`If-Modified-Since` headers.
- **Point-in-Time (PIT) / SCD Type 2 Warehouse:** Historical rows are never overwritten. State changes close the prior version (`valid_to_utc`) and append a new version (`valid_from_utc`), preventing look-ahead bias in backtests or research feeds.

## Folder Layout

```text
alt-data-pipeline/
├── config/entities.yaml         # Tracked company registry & entity mapping
├── src/alt_data/
│   ├── compliance.py            # robots.txt checker & polite HTTP client
│   ├── models.py                # Pydantic v2 schemas
│   ├── collectors/
│   │   ├── ats_jobs.py          # Greenhouse & Lever public JSON collectors
│   │   └── pricing_pages.py     # Public pricing HTML (+ optional Playwright) collector
│   ├── storage/
│   │   ├── duckdb_store.py      # SCD Type 2 PIT warehouse & Parquet/CSV exporter
│   │   └── diff_engine.py       # Hiring velocity & snapshot diff analytics
│   └── cli.py                   # CLI entrypoint
├── data/
│   ├── raw/                     # Immutable timestamped JSON/HTML snapshots
│   ├── warehouse/               # alt_data.duckdb
│   └── exports/                 # PIT Parquet & CSV outputs
└── tests/                       # Compliance, parser, and PIT/SCD2 unit tests
```

## Quickstart

```powershell
cd c:\Users\Mayank yadav\Downloads\ways\alt-data-pipeline
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"

# Run unit tests
pytest

# Initialize DuckDB schema & sync entity registry
alt-data init-db

# Collect public ATS job feeds & SaaS pricing snapshots
alt-data run-ats
alt-data run-pricing

# Query Point-in-Time state as of a specific UTC timestamp
alt-data query-pit --as-of 2026-11-11T12:00:00Z

# Export Parquet & CSV datasets
alt-data export

# Generate Markdown & JSON Point-in-Time signal digest
alt-data report
```
