"""DuckDB Point-in-Time (PIT) warehouse with SCD Type 2 versioning and Parquet/CSV export."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import duckdb

from alt_data.models import (
    ChangelogEntrySnapshot,
    CrawlRun,
    EntityConfig,
    JobPostingSnapshot,
    PricingTierSnapshot,
    SignalDiff,
)
from alt_data.storage.diff_engine import (
    build_hiring_velocity_diff,
    compute_pct_delta,
    make_diff,
)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS dim_entities (
    entity_id VARCHAR PRIMARY KEY,
    company_name VARCHAR NOT NULL,
    domain VARCHAR NOT NULL,
    ticker VARCHAR,
    sector VARCHAR NOT NULL,
    ats_provider VARCHAR,
    ats_slug VARCHAR,
    pricing_url VARCHAR,
    changelog_url VARCHAR,
    is_active BOOLEAN NOT NULL,
    updated_at_utc TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS fact_crawl_runs (
    run_id VARCHAR PRIMARY KEY,
    entity_id VARCHAR NOT NULL,
    source_type VARCHAR NOT NULL,
    target_url VARCHAR NOT NULL,
    robots_allowed BOOLEAN NOT NULL,
    http_status INTEGER,
    content_sha256 VARCHAR,
    started_at_utc TIMESTAMP NOT NULL,
    completed_at_utc TIMESTAMP NOT NULL,
    error_msg VARCHAR
);

CREATE TABLE IF NOT EXISTS fact_job_postings_pit (
    posting_uid VARCHAR PRIMARY KEY,
    entity_id VARCHAR NOT NULL,
    ats_provider VARCHAR NOT NULL,
    external_job_id VARCHAR NOT NULL,
    title VARCHAR NOT NULL,
    department_raw VARCHAR,
    role_family VARCHAR NOT NULL,
    location_raw VARCHAR,
    workplace_type VARCHAR,
    job_url VARCHAR,
    source_updated_at TIMESTAMP,
    first_seen_utc TIMESTAMP NOT NULL,
    valid_from_utc TIMESTAMP NOT NULL,
    valid_to_utc TIMESTAMP,
    closed_at_utc TIMESTAMP,
    is_active BOOLEAN NOT NULL,
    is_current BOOLEAN NOT NULL,
    content_sha256 VARCHAR NOT NULL,
    run_id VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS fact_pricing_tiers_pit (
    tier_uid VARCHAR PRIMARY KEY,
    entity_id VARCHAR NOT NULL,
    tier_name VARCHAR NOT NULL,
    billing_period VARCHAR NOT NULL,
    price_amount DOUBLE,
    currency VARCHAR NOT NULL,
    price_unit VARCHAR,
    is_contact_sales BOOLEAN NOT NULL,
    features_json VARCHAR NOT NULL,
    features_hash VARCHAR NOT NULL,
    valid_from_utc TIMESTAMP NOT NULL,
    valid_to_utc TIMESTAMP,
    is_current BOOLEAN NOT NULL,
    run_id VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS fact_changelogs_pit (
    entry_uid VARCHAR PRIMARY KEY,
    entity_id VARCHAR NOT NULL,
    external_entry_id VARCHAR NOT NULL,
    title VARCHAR NOT NULL,
    entry_url VARCHAR,
    published_at_utc TIMESTAMP,
    category VARCHAR,
    first_seen_utc TIMESTAMP NOT NULL,
    valid_from_utc TIMESTAMP NOT NULL,
    valid_to_utc TIMESTAMP,
    is_current BOOLEAN NOT NULL,
    content_sha256 VARCHAR NOT NULL,
    run_id VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS fact_signal_diffs (
    diff_id VARCHAR PRIMARY KEY,
    entity_id VARCHAR NOT NULL,
    source_type VARCHAR NOT NULL,
    signal_type VARCHAR NOT NULL,
    external_ref VARCHAR NOT NULL,
    field_name VARCHAR NOT NULL,
    old_value VARCHAR,
    new_value VARCHAR,
    pct_delta DOUBLE,
    detected_at_utc TIMESTAMP NOT NULL,
    run_id VARCHAR NOT NULL
);
"""

ALL_TABLES = (
    "dim_entities",
    "fact_crawl_runs",
    "fact_job_postings_pit",
    "fact_pricing_tiers_pit",
    "fact_changelogs_pit",
    "fact_signal_diffs",
)


def _normalize_ts(ts: datetime) -> datetime:
    """Convert any datetime into a naive UTC timestamp for consistent DuckDB TIMESTAMP storage."""
    if ts.tzinfo is None:
        return ts
    return ts.astimezone(timezone.utc).replace(tzinfo=None)


class PitWarehouse:
    """Manages the local DuckDB PIT warehouse and Parquet/CSV dataset exports."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = duckdb.connect(str(db_path))

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "PitWarehouse":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def init_schema(self) -> None:
        self.conn.execute(SCHEMA_SQL)
        self.conn.execute(
            "ALTER TABLE dim_entities ADD COLUMN IF NOT EXISTS changelog_url VARCHAR"
        )

    def sync_entities(self, entities: list[EntityConfig], synced_at_utc: datetime) -> int:
        ts = _normalize_ts(synced_at_utc)
        for entity in entities:
            self.conn.execute(
                """
                INSERT OR REPLACE INTO dim_entities (
                    entity_id, company_name, domain, ticker, sector,
                    ats_provider, ats_slug, pricing_url, changelog_url,
                    is_active, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    entity.entity_id,
                    entity.company_name,
                    entity.domain,
                    entity.ticker,
                    entity.sector,
                    entity.ats_provider,
                    entity.ats_slug,
                    entity.pricing_url,
                    entity.changelog_url,
                    entity.is_active,
                    ts,
                ],
            )
        return len(entities)

    def record_crawl_run(self, crawl_run: CrawlRun) -> None:
        self.conn.execute(
            """
            INSERT INTO fact_crawl_runs (
                run_id, entity_id, source_type, target_url, robots_allowed,
                http_status, content_sha256, started_at_utc, completed_at_utc, error_msg
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                crawl_run.run_id,
                crawl_run.entity_id,
                crawl_run.source_type,
                crawl_run.target_url,
                crawl_run.robots_allowed,
                crawl_run.http_status,
                crawl_run.content_sha256,
                _normalize_ts(crawl_run.started_at_utc),
                _normalize_ts(crawl_run.completed_at_utc),
                crawl_run.error_msg,
            ],
        )

    def _insert_diffs(self, diffs: list[SignalDiff]) -> None:
        for d in diffs:
            self.conn.execute(
                """
                INSERT INTO fact_signal_diffs (
                    diff_id, entity_id, source_type, signal_type, external_ref,
                    field_name, old_value, new_value, pct_delta, detected_at_utc, run_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    d.diff_id,
                    d.entity_id,
                    d.source_type,
                    d.signal_type,
                    d.external_ref,
                    d.field_name,
                    d.old_value,
                    d.new_value,
                    d.pct_delta,
                    _normalize_ts(d.detected_at_utc),
                    d.run_id,
                ],
            )

    def ingest_job_snapshot(
        self,
        entity_id: str,
        postings: list[JobPostingSnapshot],
        observed_at_utc: datetime,
        run_id: str,
    ) -> list[SignalDiff]:
        """Apply append-only SCD Type 2 PIT transitions for a full ATS board snapshot."""
        observed_at = _normalize_ts(observed_at_utc)
        rows = self.conn.execute(
            """
            SELECT posting_uid, ats_provider, external_job_id, title, department_raw,
                   role_family, location_raw, workplace_type, job_url,
                   source_updated_at, first_seen_utc, is_active, content_sha256
            FROM fact_job_postings_pit
            WHERE entity_id = ? AND is_current = TRUE
            """,
            [entity_id],
        ).fetchall()

        current_map: dict[str, dict[str, Any]] = {}
        previous_active_count = 0
        for row in rows:
            rec = {
                "posting_uid": row[0],
                "ats_provider": row[1],
                "external_job_id": row[2],
                "title": row[3],
                "department_raw": row[4],
                "role_family": row[5],
                "location_raw": row[6],
                "workplace_type": row[7],
                "job_url": row[8],
                "source_updated_at": row[9],
                "first_seen_utc": row[10],
                "is_active": row[11],
                "content_sha256": row[12],
            }
            current_map[rec["external_job_id"]] = rec
            if rec["is_active"]:
                previous_active_count += 1

        diffs: list[SignalDiff] = []
        incoming_ids: set[str] = set()

        for p in postings:
            incoming_ids.add(p.external_job_id)
            existing = current_map.get(p.external_job_id)

            if existing is None or not existing["is_active"]:
                if existing is not None:
                    self.conn.execute(
                        "UPDATE fact_job_postings_pit SET valid_to_utc = ?, is_current = FALSE WHERE posting_uid = ?",
                        [observed_at, existing["posting_uid"]],
                    )
                self._insert_job_row(p, first_seen=observed_at, valid_from=observed_at, is_active=True, closed_at=None, run_id=run_id)
                diffs.append(
                    make_diff(
                        entity_id=entity_id,
                        source_type="ats_jobs",
                        signal_type="ROLE_OPENED",
                        external_ref=p.external_job_id,
                        field_name="title",
                        old_value=None,
                        new_value=p.title,
                        detected_at_utc=observed_at,
                        run_id=run_id,
                    )
                )
            elif existing["content_sha256"] != p.content_sha256:
                self.conn.execute(
                    "UPDATE fact_job_postings_pit SET valid_to_utc = ?, is_current = FALSE WHERE posting_uid = ?",
                    [observed_at, existing["posting_uid"]],
                )
                self._insert_job_row(
                    p,
                    first_seen=existing["first_seen_utc"],
                    valid_from=observed_at,
                    is_active=True,
                    closed_at=None,
                    run_id=run_id,
                )
                diffs.append(
                    make_diff(
                        entity_id=entity_id,
                        source_type="ats_jobs",
                        signal_type="ROLE_UPDATED",
                        external_ref=p.external_job_id,
                        field_name="title",
                        old_value=existing["title"],
                        new_value=p.title,
                        detected_at_utc=observed_at,
                        run_id=run_id,
                    )
                )

        # Roles previously active but missing in current full-board snapshot -> closed tombstone.
        for ext_id, existing in current_map.items():
            if existing["is_active"] and ext_id not in incoming_ids:
                self.conn.execute(
                    "UPDATE fact_job_postings_pit SET valid_to_utc = ?, is_current = FALSE WHERE posting_uid = ?",
                    [observed_at, existing["posting_uid"]],
                )
                closed_snapshot = JobPostingSnapshot(
                    entity_id=entity_id,
                    ats_provider=existing["ats_provider"],
                    external_job_id=ext_id,
                    title=existing["title"],
                    department_raw=existing["department_raw"],
                    role_family=existing["role_family"],
                    location_raw=existing["location_raw"],
                    workplace_type=existing["workplace_type"],
                    job_url=existing["job_url"],
                    source_updated_at=existing["source_updated_at"],
                    content_sha256=existing["content_sha256"],
                )
                self._insert_job_row(
                    closed_snapshot,
                    first_seen=existing["first_seen_utc"],
                    valid_from=observed_at,
                    is_active=False,
                    closed_at=observed_at,
                    run_id=run_id,
                )
                diffs.append(
                    make_diff(
                        entity_id=entity_id,
                        source_type="ats_jobs",
                        signal_type="ROLE_CLOSED",
                        external_ref=ext_id,
                        field_name="is_active",
                        old_value="true",
                        new_value="false",
                        detected_at_utc=observed_at,
                        run_id=run_id,
                    )
                )

        velocity_diff = build_hiring_velocity_diff(
            entity_id=entity_id,
            previous_active_count=previous_active_count,
            current_active_count=len(postings),
            detected_at_utc=observed_at,
            run_id=run_id,
        )
        if velocity_diff is not None:
            diffs.append(velocity_diff)

        self._insert_diffs(diffs)
        return diffs

    def _insert_job_row(
        self,
        p: JobPostingSnapshot,
        first_seen: datetime,
        valid_from: datetime,
        is_active: bool,
        closed_at: Optional[datetime],
        run_id: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO fact_job_postings_pit (
                posting_uid, entity_id, ats_provider, external_job_id, title,
                department_raw, role_family, location_raw, workplace_type, job_url,
                source_updated_at, first_seen_utc, valid_from_utc, valid_to_utc,
                closed_at_utc, is_active, is_current, content_sha256, run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                uuid.uuid4().hex[:16],
                p.entity_id,
                p.ats_provider,
                p.external_job_id,
                p.title,
                p.department_raw,
                p.role_family,
                p.location_raw,
                p.workplace_type,
                p.job_url,
                _normalize_ts(p.source_updated_at) if p.source_updated_at else None,
                _normalize_ts(first_seen),
                _normalize_ts(valid_from),
                None,
                _normalize_ts(closed_at) if closed_at else None,
                is_active,
                True,
                p.content_sha256,
                run_id,
            ],
        )

    def ingest_pricing_snapshot(
        self,
        entity_id: str,
        tiers: list[PricingTierSnapshot],
        observed_at_utc: datetime,
        run_id: str,
    ) -> list[SignalDiff]:
        """Apply SCD Type 2 PIT transitions for extracted SaaS pricing tiers."""
        if not tiers:
            return []

        observed_at = _normalize_ts(observed_at_utc)
        rows = self.conn.execute(
            """
            SELECT tier_uid, tier_name, billing_period, price_amount, currency,
                   price_unit, is_contact_sales, features_hash
            FROM fact_pricing_tiers_pit
            WHERE entity_id = ? AND is_current = TRUE
            """,
            [entity_id],
        ).fetchall()

        current_map: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            key = (str(row[1]).lower(), str(row[2]))
            current_map[key] = {
                "tier_uid": row[0],
                "tier_name": row[1],
                "billing_period": row[2],
                "price_amount": row[3],
                "currency": row[4],
                "price_unit": row[5],
                "is_contact_sales": row[6],
                "features_hash": row[7],
            }

        diffs: list[SignalDiff] = []
        incoming_keys: set[tuple[str, str]] = set()

        for t in tiers:
            key = (t.tier_name.lower(), t.billing_period)
            incoming_keys.add(key)
            existing = current_map.get(key)
            ref = f"{t.tier_name}:{t.billing_period}"

            if existing is None:
                self._insert_pricing_row(t, valid_from=observed_at, run_id=run_id)
                diffs.append(
                    make_diff(
                        entity_id=entity_id,
                        source_type="pricing_page",
                        signal_type="TIER_ADDED",
                        external_ref=ref,
                        field_name="price_amount",
                        old_value=None,
                        new_value="contact_sales" if t.is_contact_sales else str(t.price_amount),
                        detected_at_utc=observed_at,
                        run_id=run_id,
                    )
                )
                continue

            price_changed = existing["price_amount"] != t.price_amount
            meta_changed = (
                existing["features_hash"] != t.features_hash
                or existing["is_contact_sales"] != t.is_contact_sales
                or existing["price_unit"] != t.price_unit
                or existing["currency"] != t.currency
            )

            if price_changed or meta_changed:
                self.conn.execute(
                    "UPDATE fact_pricing_tiers_pit SET valid_to_utc = ?, is_current = FALSE WHERE tier_uid = ?",
                    [observed_at, existing["tier_uid"]],
                )
                self._insert_pricing_row(t, valid_from=observed_at, run_id=run_id)

                if price_changed:
                    diffs.append(
                        make_diff(
                            entity_id=entity_id,
                            source_type="pricing_page",
                            signal_type="PRICE_CHANGE",
                            external_ref=ref,
                            field_name="price_amount",
                            old_value=str(existing["price_amount"]),
                            new_value=str(t.price_amount),
                            pct_delta=compute_pct_delta(existing["price_amount"], t.price_amount),
                            detected_at_utc=observed_at,
                            run_id=run_id,
                        )
                    )
                if meta_changed:
                    diffs.append(
                        make_diff(
                            entity_id=entity_id,
                            source_type="pricing_page",
                            signal_type="TIER_UPDATED",
                            external_ref=ref,
                            field_name="features_hash",
                            old_value=existing["features_hash"],
                            new_value=t.features_hash,
                            detected_at_utc=observed_at,
                            run_id=run_id,
                        )
                    )

        for key, existing in current_map.items():
            if key not in incoming_keys:
                self.conn.execute(
                    "UPDATE fact_pricing_tiers_pit SET valid_to_utc = ?, is_current = FALSE WHERE tier_uid = ?",
                    [observed_at, existing["tier_uid"]],
                )
                diffs.append(
                    make_diff(
                        entity_id=entity_id,
                        source_type="pricing_page",
                        signal_type="TIER_REMOVED",
                        external_ref=f"{existing['tier_name']}:{existing['billing_period']}",
                        field_name="tier_name",
                        old_value=existing["tier_name"],
                        new_value=None,
                        detected_at_utc=observed_at,
                        run_id=run_id,
                    )
                )

        self._insert_diffs(diffs)
        return diffs

    def _insert_pricing_row(
        self,
        t: PricingTierSnapshot,
        valid_from: datetime,
        run_id: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO fact_pricing_tiers_pit (
                tier_uid, entity_id, tier_name, billing_period, price_amount,
                currency, price_unit, is_contact_sales, features_json,
                features_hash, valid_from_utc, valid_to_utc, is_current, run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                uuid.uuid4().hex[:16],
                t.entity_id,
                t.tier_name,
                t.billing_period,
                t.price_amount,
                t.currency,
                t.price_unit,
                t.is_contact_sales,
                json.dumps(t.features_json, ensure_ascii=True),
                t.features_hash,
                _normalize_ts(valid_from),
                None,
                True,
                run_id,
            ],
        )

    def query_jobs_as_of(
        self, as_of_utc: datetime, entity_id: Optional[str] = None
    ) -> list[dict[str, Any]]:
        """Return exact active job postings known at `as_of_utc` (zero look-ahead bias)."""
        ts = _normalize_ts(as_of_utc)
        sql = """
            SELECT entity_id, external_job_id, title, role_family, location_raw, valid_from_utc
            FROM fact_job_postings_pit
            WHERE valid_from_utc <= ?
              AND (valid_to_utc IS NULL OR valid_to_utc > ?)
              AND is_active = TRUE
        """
        params: list[Any] = [ts, ts]
        if entity_id:
            sql += " AND entity_id = ?"
            params.append(entity_id)
        sql += " ORDER BY entity_id, external_job_id"
        rows = self.conn.execute(sql, params).fetchall()
        return [
            {
                "entity_id": r[0],
                "external_job_id": r[1],
                "title": r[2],
                "role_family": r[3],
                "location_raw": r[4],
                "valid_from_utc": r[5],
            }
            for r in rows
        ]

    def query_pricing_as_of(
        self, as_of_utc: datetime, entity_id: Optional[str] = None
    ) -> list[dict[str, Any]]:
        """Return exact SaaS pricing tiers known at `as_of_utc` (zero look-ahead bias)."""
        ts = _normalize_ts(as_of_utc)
        sql = """
            SELECT entity_id, tier_name, billing_period, price_amount, currency,
                   is_contact_sales, valid_from_utc
            FROM fact_pricing_tiers_pit
            WHERE valid_from_utc <= ?
              AND (valid_to_utc IS NULL OR valid_to_utc > ?)
        """
        params: list[Any] = [ts, ts]
        if entity_id:
            sql += " AND entity_id = ?"
            params.append(entity_id)
        sql += " ORDER BY entity_id, tier_name, billing_period"
        rows = self.conn.execute(sql, params).fetchall()
        return [
            {
                "entity_id": r[0],
                "tier_name": r[1],
                "billing_period": r[2],
                "price_amount": r[3],
                "currency": r[4],
                "is_contact_sales": r[5],
                "valid_from_utc": r[6],
            }
            for r in rows
        ]

    def export_pit_datasets(self, export_dir: Path) -> list[Path]:
        """Export every warehouse table to Parquet and CSV inside `export_dir`."""
        parquet_dir = export_dir / "parquet"
        csv_dir = export_dir / "csv"
        parquet_dir.mkdir(parents=True, exist_ok=True)
        csv_dir.mkdir(parents=True, exist_ok=True)

        written: list[Path] = []
        for table in ALL_TABLES:
            pq_path = parquet_dir / f"{table}.parquet"
            csv_path = csv_dir / f"{table}.csv"
            pq_sql = str(pq_path).replace("'", "''")
            csv_sql = str(csv_path).replace("'", "''")
            self.conn.execute(f"COPY {table} TO '{pq_sql}' (FORMAT PARQUET)")
            self.conn.execute(f"COPY {table} TO '{csv_sql}' (HEADER, DELIMITER ',')")
            written.extend([pq_path, csv_path])
        return written

    def get_hiring_mix_summary(self, as_of_utc: datetime) -> list[dict[str, Any]]:
        """Aggregate active jobs and technical/AI/GTM role-family mix as of `as_of_utc`."""
        ts = _normalize_ts(as_of_utc)
        rows = self.conn.execute(
            """
            SELECT
                entity_id,
                COUNT(*) AS total_active,
                SUM(CASE WHEN role_family = 'engineering' THEN 1 ELSE 0 END) AS engineering,
                SUM(CASE WHEN role_family = 'ai_ml' THEN 1 ELSE 0 END) AS ai_ml,
                SUM(CASE WHEN role_family = 'data' THEN 1 ELSE 0 END) AS data,
                SUM(CASE WHEN role_family = 'security' THEN 1 ELSE 0 END) AS security,
                SUM(CASE WHEN role_family = 'gtm' THEN 1 ELSE 0 END) AS gtm
            FROM fact_job_postings_pit
            WHERE valid_from_utc <= ?
              AND (valid_to_utc IS NULL OR valid_to_utc > ?)
              AND is_active = TRUE
            GROUP BY entity_id
            ORDER BY total_active DESC, entity_id
            """,
            [ts, ts],
        ).fetchall()
        results: list[dict[str, Any]] = []
        for r in rows:
            total = int(r[1])
            tech_count = int(r[2]) + int(r[3]) + int(r[4]) + int(r[5])
            results.append(
                {
                    "entity_id": r[0],
                    "total_active": total,
                    "engineering": int(r[2]),
                    "ai_ml": int(r[3]),
                    "data": int(r[4]),
                    "security": int(r[5]),
                    "gtm": int(r[6]),
                    "tech_share_pct": round((tech_count / total) * 100.0, 1) if total else 0.0,
                }
            )
        return results

    def get_recent_signal_diffs(self, limit: int = 25) -> list[dict[str, Any]]:
        """Return recent high-signal diffs (prioritizing pricing and velocity events)."""
        rows = self.conn.execute(
            """
            SELECT entity_id, source_type, signal_type, external_ref,
                   field_name, old_value, new_value, pct_delta, detected_at_utc
            FROM fact_signal_diffs
            ORDER BY
                CASE WHEN signal_type IN ('PRICE_CHANGE', 'HIRING_VELOCITY_SHIFT', 'TIER_ADDED', 'TIER_REMOVED') THEN 0 ELSE 1 END,
                detected_at_utc DESC
            LIMIT ?
            """,
            [limit],
        ).fetchall()
        return [
            {
                "entity_id": r[0],
                "source_type": r[1],
                "signal_type": r[2],
                "external_ref": r[3],
                "field_name": r[4],
                "old_value": r[5],
                "new_value": r[6],
                "pct_delta": r[7],
                "detected_at_utc": r[8],
            }
            for r in rows
        ]

    def get_crawl_health_summary(self) -> list[dict[str, Any]]:
        """Summarize crawl runs by source_type, robots_allowed, and http_status."""
        rows = self.conn.execute(
            """
            SELECT source_type, robots_allowed, http_status, COUNT(*) AS runs
            FROM fact_crawl_runs
            GROUP BY source_type, robots_allowed, http_status
            ORDER BY source_type, runs DESC
            """
        ).fetchall()
        return [
            {
                "source_type": r[0],
                "robots_allowed": bool(r[1]),
                "http_status": r[2],
                "runs": int(r[3]),
            }
            for r in rows
        ]

    def ingest_changelog_snapshot(
        self,
        entity_id: str,
        entries: list[ChangelogEntrySnapshot],
        observed_at_utc: datetime,
        run_id: str,
    ) -> list[SignalDiff]:
        """Apply append-only SCD Type 2 PIT ingestion for RSS/Atom changelog entries."""
        observed_at = _normalize_ts(observed_at_utc)
        rows = self.conn.execute(
            """
            SELECT entry_uid, external_entry_id, title, first_seen_utc, content_sha256
            FROM fact_changelogs_pit
            WHERE entity_id = ? AND is_current = TRUE
            """,
            [entity_id],
        ).fetchall()

        current_map: dict[str, dict[str, Any]] = {
            r[1]: {
                "entry_uid": r[0],
                "title": r[2],
                "first_seen_utc": r[3],
                "content_sha256": r[4],
            }
            for r in rows
        }

        diffs: list[SignalDiff] = []
        for entry in entries:
            existing = current_map.get(entry.external_entry_id)
            if existing is None:
                self._insert_changelog_row(
                    entry, first_seen=observed_at, valid_from=observed_at, run_id=run_id
                )
                diffs.append(
                    make_diff(
                        entity_id=entity_id,
                        source_type="changelog",
                        signal_type="CHANGELOG_PUBLISHED",
                        external_ref=entry.external_entry_id,
                        field_name="title",
                        old_value=None,
                        new_value=entry.title,
                        detected_at_utc=observed_at,
                        run_id=run_id,
                    )
                )
            elif existing["content_sha256"] != entry.content_sha256:
                self.conn.execute(
                    "UPDATE fact_changelogs_pit SET valid_to_utc = ?, is_current = FALSE WHERE entry_uid = ?",
                    [observed_at, existing["entry_uid"]],
                )
                self._insert_changelog_row(
                    entry,
                    first_seen=existing["first_seen_utc"],
                    valid_from=observed_at,
                    run_id=run_id,
                )
                diffs.append(
                    make_diff(
                        entity_id=entity_id,
                        source_type="changelog",
                        signal_type="CHANGELOG_UPDATED",
                        external_ref=entry.external_entry_id,
                        field_name="title",
                        old_value=existing["title"],
                        new_value=entry.title,
                        detected_at_utc=observed_at,
                        run_id=run_id,
                    )
                )

        self._insert_diffs(diffs)
        return diffs

    def _insert_changelog_row(
        self,
        entry: ChangelogEntrySnapshot,
        first_seen: datetime,
        valid_from: datetime,
        run_id: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO fact_changelogs_pit (
                entry_uid, entity_id, external_entry_id, title, entry_url,
                published_at_utc, category, first_seen_utc, valid_from_utc,
                valid_to_utc, is_current, content_sha256, run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                uuid.uuid4().hex[:16],
                entry.entity_id,
                entry.external_entry_id,
                entry.title,
                entry.entry_url,
                _normalize_ts(entry.published_at_utc) if entry.published_at_utc else None,
                entry.category,
                _normalize_ts(first_seen),
                _normalize_ts(valid_from),
                None,
                True,
                entry.content_sha256,
                run_id,
            ],
        )

    def get_release_velocity_summary(
        self, as_of_utc: datetime, window_days: int = 30
    ) -> list[dict[str, Any]]:
        """Count PIT-visible changelog/release entries published within `window_days` prior to `as_of_utc`."""
        ts = _normalize_ts(as_of_utc)
        window_start = ts - timedelta(days=window_days)
        rows = self.conn.execute(
            """
            SELECT
                entity_id,
                COUNT(*) AS total_tracked,
                SUM(
                    CASE
                        WHEN published_at_utc IS NOT NULL
                         AND published_at_utc >= ?
                         AND published_at_utc <= ?
                        THEN 1 ELSE 0
                    END
                ) AS releases_in_window,
                MAX(published_at_utc) AS latest_release_utc
            FROM fact_changelogs_pit
            WHERE valid_from_utc <= ?
              AND (valid_to_utc IS NULL OR valid_to_utc > ?)
            GROUP BY entity_id
            ORDER BY releases_in_window DESC, entity_id
            """,
            [window_start, ts, ts, ts],
        ).fetchall()
        return [
            {
                "entity_id": r[0],
                "total_tracked": int(r[1]),
                "releases_last_30d": int(r[2]),
                "latest_release_utc": r[3],
            }
            for r in rows
        ]
