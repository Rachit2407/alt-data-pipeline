"""Unit tests for the local PIT interactive web dashboard."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from alt_data.dashboard import DashboardRequestHandler, ThreadingHTTPServer
from alt_data.models import (
    ChangelogEntrySnapshot,
    EntityConfig,
    JobPostingSnapshot,
    PricingTierSnapshot,
    canonical_json_hash,
)
from alt_data.storage.duckdb_store import PitWarehouse


def test_dashboard_http_server(tmp_path: Path) -> None:
    db_path = tmp_path / "dash.duckdb"
    now_utc = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)

    with PitWarehouse(db_path) as wh:
        wh.init_schema()
        wh.sync_entities(
            [
                EntityConfig(
                    entity_id="anthropic",
                    company_name="Anthropic",
                    domain="anthropic.com",
                    sector="foundation_models",
                    ats_provider="greenhouse",
                    ats_slug="anthropic",
                    pricing_url="https://anthropic.com/pricing",
                    changelog_url=None,
                )
            ],
            now_utc,
        )
        wh.ingest_job_snapshot(
            "anthropic",
            [
                JobPostingSnapshot(
                    entity_id="anthropic",
                    ats_provider="greenhouse",
                    external_job_id="1",
                    title="Research Scientist, Alignment",
                    role_family="ai_ml",
                    content_sha256=canonical_json_hash({"title": "Research Scientist"}),
                )
            ],
            now_utc,
            "run_dash_1",
        )
        wh.ingest_pricing_snapshot(
            "anthropic",
            [
                PricingTierSnapshot(
                    entity_id="anthropic",
                    tier_name="Pro",
                    billing_period="monthly",
                    price_amount=20.0,
                    features_hash=canonical_json_hash(["feature_1"]),
                )
            ],
            now_utc,
            "run_dash_2",
        )

    handler_factory = lambda *args, **kwargs: DashboardRequestHandler(  # noqa: E731
        *args, db_path=db_path, **kwargs
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_factory)
    port = server.server_address[1]

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.1)

    try:
        base_url = f"http://127.0.0.1:{port}"

        # 1. Root page
        res = httpx.get(f"{base_url}/")
        assert res.status_code == 200
        assert "Alternative Data PIT Signal Feed" in res.text

        # 2. Entities API
        res_ent = httpx.get(f"{base_url}/api/entities")
        assert res_ent.status_code == 200
        entities = res_ent.json()
        assert len(entities) == 1
        assert entities[0]["entity_id"] == "anthropic"

        # 3. Snapshot API
        res_snap = httpx.get(f"{base_url}/api/snapshot?as_of=2026-11-13T15:00:00Z")
        assert res_snap.status_code == 200
        data = res_snap.json()
        assert len(data["hiring_mix"]) == 1
        assert data["hiring_mix"][0]["total_active"] == 1
        assert data["hiring_mix"][0]["ai_ml"] == 1
        assert len(data["pricing_tiers"]) == 1
        assert data["pricing_tiers"][0]["tier_name"] == "Pro"
        assert len(data["recent_diffs"]) >= 2

        # 4. Filter by entity
        res_filt = httpx.get(f"{base_url}/api/snapshot?entity=anthropic")
        assert res_filt.status_code == 200
        assert res_filt.json()["entity_filter"] == "anthropic"

        # 404 test
        res_404 = httpx.get(f"{base_url}/unknown")
        assert res_404.status_code == 404
    finally:
        server.shutdown()
        server.server_close()
