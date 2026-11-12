"""Example script demonstrating how downstream analysts query PIT datasets with zero look-ahead bias.

Can query either the local DuckDB database or the exported Parquet tables directly.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import duckdb

def _print_table(conn: duckdb.DuckDBPyConnection, query: str) -> None:
    rel = conn.execute(query)
    cols = [desc[0] for desc in rel.description] if rel.description else []
    rows = rel.fetchall()
    if not rows:
        print("(no rows)")
        return
    # Compute column widths
    str_rows = [[str(val) if val is not None else "NULL" for val in r] for r in rows]
    widths = [len(c) for c in cols]
    for r in str_rows:
        for idx, val in enumerate(r):
            widths[idx] = max(widths[idx], len(val))

    header = " | ".join(c.ljust(widths[i]) for i, c in enumerate(cols))
    sep = "-+-".join("-" * widths[i] for i in range(len(cols)))
    print(header)
    print(sep)
    for r in str_rows:
        print(" | ".join(r[i].ljust(widths[i]) for i in range(len(cols))))
ROOT = Path(__file__).resolve().parents[1]
PARQUET_DIR = ROOT / "data" / "exports" / "parquet"
DB_PATH = ROOT / "data" / "warehouse" / "alt_data.duckdb"


def run_analysis() -> None:
    conn = duckdb.connect()

    # Prefer direct Parquet query if exported, fallback to DuckDB warehouse
    if (PARQUET_DIR / "fact_job_postings_pit.parquet").exists():
        print(f"[INFO] Querying exported Parquet files from {PARQUET_DIR}\n")
        conn.execute(f"""
            CREATE VIEW jobs AS SELECT * FROM read_parquet('{str(PARQUET_DIR / "fact_job_postings_pit.parquet").replace("'", "''")}');
            CREATE VIEW pricing AS SELECT * FROM read_parquet('{str(PARQUET_DIR / "fact_pricing_tiers_pit.parquet").replace("'", "''")}');
            CREATE VIEW changelogs AS SELECT * FROM read_parquet('{str(PARQUET_DIR / "fact_changelogs_pit.parquet").replace("'", "''")}');
            CREATE VIEW diffs AS SELECT * FROM read_parquet('{str(PARQUET_DIR / "fact_signal_diffs.parquet").replace("'", "''")}');
        """)
    elif DB_PATH.exists():
        print(f"[INFO] Querying DuckDB warehouse at {DB_PATH}\n")
        conn.execute(f"ATTACH '{str(DB_PATH).replace("'", "''")}' AS alt_data (READ_ONLY);")
        conn.execute("""
            CREATE VIEW jobs AS SELECT * FROM alt_data.fact_job_postings_pit;
            CREATE VIEW pricing AS SELECT * FROM alt_data.fact_pricing_tiers_pit;
            CREATE VIEW changelogs AS SELECT * FROM alt_data.fact_changelogs_pit;
            CREATE VIEW diffs AS SELECT * FROM alt_data.fact_signal_diffs;
        """)
    else:
        print("[ERROR] Neither exports/parquet nor alt_data.duckdb found. Run `alt-data export` first.")
        return

    # Query 1: Point-in-Time Job Openings & Role Mix (Zero Look-ahead Bias)
    print("=" * 80)
    print("QUERY 1: Point-in-Time Active Tech Roles by Company")
    print("=" * 80)
    q1 = """
    SELECT
        entity_id,
        COUNT(*) AS active_jobs,
        SUM(CASE WHEN role_family = 'engineering' THEN 1 ELSE 0 END) AS eng_count,
        SUM(CASE WHEN role_family = 'ai_ml' THEN 1 ELSE 0 END) AS ai_ml_count,
        SUM(CASE WHEN role_family = 'security' THEN 1 ELSE 0 END) AS sec_count,
        ROUND(SUM(CASE WHEN role_family IN ('engineering', 'ai_ml', 'data', 'security') THEN 1.0 ELSE 0.0 END) / COUNT(*) * 100, 1) AS tech_pct
    FROM jobs
    WHERE is_active = TRUE
      AND (valid_to_utc IS NULL OR valid_to_utc > CURRENT_TIMESTAMP)
    GROUP BY entity_id
    ORDER BY active_jobs DESC;
    """
    _print_table(conn, q1)

    # Query 2: Active SaaS Pricing Tiers (Monthly vs Annual vs Custom)
    print("\n" + "=" * 80)
    print("QUERY 2: Active SaaS Pricing Tiers Snapshot")
    print("=" * 80)
    q2 = """
    SELECT
        entity_id,
        tier_name,
        billing_period,
        price_amount,
        currency,
        is_contact_sales
    FROM pricing
    WHERE is_current = TRUE
      AND (valid_to_utc IS NULL OR valid_to_utc > CURRENT_TIMESTAMP)
    ORDER BY entity_id, price_amount NULLS LAST
    LIMIT 15;
    """
    _print_table(conn, q2)

    # Query 3: Product Release Velocity vs Engineering Openings Correlation
    print("\n" + "=" * 80)
    print("QUERY 3: Engineering Roles vs 30-Day Changelog Release Cadence")
    print("=" * 80)
    q3 = """
    WITH eng_counts AS (
        SELECT entity_id, COUNT(*) AS active_eng_roles
        FROM jobs
        WHERE is_active = TRUE AND role_family IN ('engineering', 'ai_ml')
        GROUP BY entity_id
    ),
    release_counts AS (
        SELECT entity_id, COUNT(*) AS releases_last_30d
        FROM changelogs
        WHERE published_at_utc >= CURRENT_TIMESTAMP - INTERVAL 30 DAY
        GROUP BY entity_id
    )
    SELECT
        COALESCE(e.entity_id, r.entity_id) AS entity_id,
        COALESCE(e.active_eng_roles, 0) AS active_eng_roles,
        COALESCE(r.releases_last_30d, 0) AS releases_last_30d
    FROM eng_counts e
    FULL OUTER JOIN release_counts r ON e.entity_id = r.entity_id
    ORDER BY releases_last_30d DESC;
    """
    _print_table(conn, q3)


if __name__ == "__main__":
    run_analysis()
