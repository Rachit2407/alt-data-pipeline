"""Tests for SCD Type 2 Point-in-Time queries, diff detection, and Parquet/CSV export."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pyarrow.parquet as pq

from alt_data.models import (
    ChangelogEntrySnapshot,
    EntityConfig,
    JobPostingSnapshot,
    PricingTierSnapshot,
    canonical_json_hash,
)
from alt_data.storage.duckdb_store import PitWarehouse


def _job(job_id: str, title: str) -> JobPostingSnapshot:
    return JobPostingSnapshot(
        entity_id="supabase",
        ats_provider="greenhouse",
        external_job_id=job_id,
        title=title,
        department_raw="Engineering",
        role_family="engineering",
        location_raw="Remote",
        content_sha256=canonical_json_hash({"title": title}),
    )


def _tier(name: str, price: float | None, features: list[str], contact: bool = False) -> PricingTierSnapshot:
    return PricingTierSnapshot(
        entity_id="supabase",
        tier_name=name,
        billing_period="custom" if contact else "monthly",
        price_amount=price,
        currency="USD",
        price_unit="flat_monthly" if not contact else None,
        is_contact_sales=contact,
        features_json=features,
        features_hash=canonical_json_hash(features),
    )


def test_pit_jobs_scd2_zero_lookahead_and_exports(tmp_path: Path) -> None:
    db_path = tmp_path / "warehouse" / "test.duckdb"
    export_dir = tmp_path / "exports"

    t1 = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)

    with PitWarehouse(db_path) as wh:
        wh.init_schema()
        wh.sync_entities(
            [
                EntityConfig(
                    entity_id="supabase",
                    company_name="Supabase",
                    domain="supabase.com",
                    sector="developer_infra",
                    ats_provider="greenhouse",
                    ats_slug="supabase",
                    pricing_url="https://supabase.com/pricing",
                )
            ],
            t1,
        )

        diffs_t1 = wh.ingest_job_snapshot(
            "supabase",
            [_job("1", "Backend Engineer"), _job("2", "Frontend Engineer")],
            t1,
            "run_1",
        )
        assert [d.signal_type for d in diffs_t1] == ["ROLE_OPENED", "ROLE_OPENED"]

        # T2: job 1 updated, job 2 closed, jobs 3, 4, 5, 6 opened (active count 2 -> 5, +150%)
        diffs_t2 = wh.ingest_job_snapshot(
            "supabase",
            [
                _job("1", "Senior Backend Engineer"),
                _job("3", "Staff SRE"),
                _job("4", "AI Engineer"),
                _job("5", "Security Engineer"),
                _job("6", "Data Engineer"),
            ],
            t2,
            "run_2",
        )
        sig_types = {d.signal_type for d in diffs_t2}
        assert {"ROLE_UPDATED", "ROLE_CLOSED", "ROLE_OPENED", "HIRING_VELOCITY_SHIFT"}.issubset(sig_types)

        # Point-in-Time check at T1 + 1 hour must reflect ONLY T1 state (no look-ahead bias)
        as_of_t1 = wh.query_jobs_as_of(datetime(2026, 11, 1, 13, 0, tzinfo=timezone.utc))
        assert {j["external_job_id"]: j["title"] for j in as_of_t1} == {
            "1": "Backend Engineer",
            "2": "Frontend Engineer",
        }

        # Point-in-Time check at T2 + 1 hour reflects T2 state
        as_of_t2 = wh.query_jobs_as_of(datetime(2026, 11, 8, 13, 0, tzinfo=timezone.utc))
        assert {j["external_job_id"]: j["title"] for j in as_of_t2} == {
            "1": "Senior Backend Engineer",
            "3": "Staff SRE",
            "4": "AI Engineer",
            "5": "Security Engineer",
            "6": "Data Engineer",
        }

        written = wh.export_pit_datasets(export_dir)
        assert len(written) == 12
        jobs_pq = pq.read_table(export_dir / "parquet" / "fact_job_postings_pit.parquet")
        # 2 initial rows + 1 updated + 1 closed tombstone + 4 opened = 8 immutable PIT rows
        assert jobs_pq.num_rows == 8

        mix = wh.get_hiring_mix_summary(datetime(2026, 11, 8, 13, 0, tzinfo=timezone.utc))
        assert len(mix) == 1
        assert mix[0]["total_active"] == 5
        assert mix[0]["tech_share_pct"] == 100.0


def test_pit_pricing_price_increase_diff(tmp_path: Path) -> None:
    db_path = tmp_path / "warehouse" / "pricing.duckdb"
    t1 = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)

    with PitWarehouse(db_path) as wh:
        wh.init_schema()
        wh.ingest_pricing_snapshot(
            "supabase",
            [_tier("Free", 0.0, ["50k MAU"]), _tier("Pro", 20.0, ["100k MAU"])],
            t1,
            "p_run_1",
        )
        diffs = wh.ingest_pricing_snapshot(
            "supabase",
            [
                _tier("Free", 0.0, ["50k MAU"]),
                _tier("Pro", 25.0, ["100k MAU"]),
                _tier("Enterprise", None, ["SOC2"], contact=True),
            ],
            t2,
            "p_run_2",
        )

        price_diffs = [d for d in diffs if d.signal_type == "PRICE_CHANGE"]
        assert len(price_diffs) == 1
        assert price_diffs[0].pct_delta == 25.0
        assert any(d.signal_type == "TIER_ADDED" and "Enterprise" in d.external_ref for d in diffs)

        # Querying as of T1 still returns Pro at $20 and no Enterprise tier
        t1_tiers = wh.query_pricing_as_of(datetime(2026, 11, 2, 0, 0, tzinfo=timezone.utc))
        assert {t["tier_name"]: t["price_amount"] for t in t1_tiers} == {"Free": 0.0, "Pro": 20.0}


def test_pit_changelogs_and_release_velocity(tmp_path: Path) -> None:
    db_path = tmp_path / "warehouse" / "changelogs.duckdb"
    t1 = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)

    with PitWarehouse(db_path) as wh:
        wh.init_schema()
        e1 = ChangelogEntrySnapshot(
            entity_id="vercel",
            external_entry_id="rel-1",
            title="Edge Config v2",
            entry_url="https://vercel.com/changelog/edge-config-v2",
            published_at_utc=datetime(2026, 10, 25, 10, 0, tzinfo=timezone.utc),
            content_sha256=canonical_json_hash({"title": "Edge Config v2"}),
        )
        e2 = ChangelogEntrySnapshot(
            entity_id="vercel",
            external_entry_id="rel-2",
            title="Zero-Downtime Rollbacks",
            entry_url="https://vercel.com/changelog/rollbacks",
            published_at_utc=datetime(2026, 11, 9, 10, 0, tzinfo=timezone.utc),
            content_sha256=canonical_json_hash({"title": "Zero-Downtime Rollbacks"}),
        )
        diffs1 = wh.ingest_changelog_snapshot("vercel", [e1], t1, "c_run_1")
        diffs2 = wh.ingest_changelog_snapshot("vercel", [e1, e2], t2, "c_run_2")

        assert len(diffs1) == 1
        assert len(diffs2) == 1
        assert diffs2[0].signal_type == "CHANGELOG_PUBLISHED"

        vel_t1 = wh.get_release_velocity_summary(t1, window_days=30)
        assert vel_t1[0]["releases_last_30d"] == 1
        vel_t2 = wh.get_release_velocity_summary(t2, window_days=30)
        assert vel_t2[0]["releases_last_30d"] == 2
