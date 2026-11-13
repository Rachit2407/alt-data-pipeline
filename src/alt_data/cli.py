"""Command-line entrypoint for the Alt-Data PIT pipeline."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from alt_data.collectors.ats_jobs import collect_ats_board
from alt_data.collectors.changelogs import collect_changelog_feed
from alt_data.collectors.pricing_pages import collect_pricing_page
from alt_data.compliance import PoliteHttpClient
from alt_data.dashboard import run_dashboard_server
from alt_data.models import EntityConfig, load_entities, utc_now
from alt_data.storage.diff_engine import render_signal_digest_markdown
from alt_data.storage.duckdb_store import PitWarehouse


def _default_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _filter_entities(
    entities: list[EntityConfig], entity_id: Optional[str]
) -> list[EntityConfig]:
    active = [e for e in entities if e.is_active]
    if entity_id:
        return [e for e in active if e.entity_id == entity_id]
    return active


def _parse_iso(ts_str: str) -> datetime:
    parsed = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="alt-data",
        description="Automated Point-in-Time (PIT) Alternative Data Pipeline",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=_default_root(),
        help="Project root containing config/ and data/ directories.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="Initialize DuckDB PIT schema and sync entities.yaml.")

    ats_cmd = sub.add_parser("run-ats", help="Collect public ATS job board feeds.")
    ats_cmd.add_argument("--entity", type=str, default=None, help="Filter to a single entity_id.")

    pricing_cmd = sub.add_parser("run-pricing", help="Collect public SaaS pricing pages.")
    pricing_cmd.add_argument("--entity", type=str, default=None, help="Filter to a single entity_id.")
    pricing_cmd.add_argument(
        "--playwright",
        action="store_true",
        help="Use Playwright headless render fallback after robots.txt passes.",
    )

    cl_cmd = sub.add_parser("run-changelogs", help="Collect public RSS/Atom changelog feeds.")
    cl_cmd.add_argument("--entity", type=str, default=None, help="Filter to a single entity_id.")

    query_cmd = sub.add_parser("query-pit", help="Query Point-in-Time state as of a UTC timestamp.")
    query_cmd.add_argument("--as-of", required=True, help="ISO-8601 UTC timestamp, e.g. 2026-11-11T15:00:00Z")
    query_cmd.add_argument("--entity", type=str, default=None, help="Optional entity_id filter.")

    sub.add_parser("export", help="Export all PIT warehouse tables to Parquet and CSV.")

    report_cmd = sub.add_parser("report", help="Generate Markdown and JSON PIT signal digests.")
    report_cmd.add_argument(
        "--as-of",
        default=None,
        help="Optional ISO-8601 UTC timestamp (defaults to current UTC time).",
    )

    dash_cmd = sub.add_parser("dashboard", help="Start local interactive PIT web dashboard.")
    dash_cmd.add_argument("--host", default="127.0.0.1", help="Host address (default: 127.0.0.1).")
    dash_cmd.add_argument("--port", type=int, default=8501, help="Port to listen on (default: 8501).")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    root: Path = args.root
    config_path = root / "config" / "entities.yaml"
    db_path = root / "data" / "warehouse" / "alt_data.duckdb"
    raw_root = root / "data" / "raw"
    export_dir = root / "data" / "exports"

    entities = load_entities(config_path)

    with PitWarehouse(db_path) as wh:
        wh.init_schema()
        synced = wh.sync_entities(entities, utc_now())

        if args.command == "init-db":
            print(f"Initialized DuckDB warehouse at {db_path} ({synced} entities synced).")
            return 0

        if args.command == "run-ats":
            targets = _filter_entities(entities, args.entity)
            with PoliteHttpClient() as client:
                for ent in targets:
                    res = collect_ats_board(ent, client, raw_root)
                    if res is None:
                        continue
                    wh.record_crawl_run(res.crawl_run)
                    if res.crawl_run.error_msg is None and res.crawl_run.http_status == 200:
                        diffs = wh.ingest_job_snapshot(
                            ent.entity_id,
                            res.postings,
                            res.crawl_run.completed_at_utc,
                            res.crawl_run.run_id,
                        )
                        print(
                            f"[ATS] {ent.entity_id}: status={res.crawl_run.http_status} "
                            f"jobs={len(res.postings)} diffs={len(diffs)}"
                        )
                    else:
                        print(
                            f"[ATS] {ent.entity_id}: skipped/error "
                            f"robots={res.crawl_run.robots_allowed} "
                            f"status={res.crawl_run.http_status} msg={res.crawl_run.error_msg}"
                        )
            return 0

        if args.command == "run-pricing":
            targets = _filter_entities(entities, args.entity)
            with PoliteHttpClient() as client:
                for ent in targets:
                    res = collect_pricing_page(
                        ent, client, raw_root, use_playwright=args.playwright
                    )
                    if res is None:
                        continue
                    wh.record_crawl_run(res.crawl_run)
                    if res.crawl_run.http_status == 200 and res.crawl_run.robots_allowed:
                        diffs = wh.ingest_pricing_snapshot(
                            ent.entity_id,
                            res.tiers,
                            res.crawl_run.completed_at_utc,
                            res.crawl_run.run_id,
                        )
                        print(
                            f"[PRICING] {ent.entity_id}: status={res.crawl_run.http_status} "
                            f"tiers={len(res.tiers)} diffs={len(diffs)}"
                        )
                    else:
                        print(
                            f"[PRICING] {ent.entity_id}: skipped/error "
                            f"robots={res.crawl_run.robots_allowed} "
                            f"status={res.crawl_run.http_status} msg={res.crawl_run.error_msg}"
                        )
            return 0

        if args.command == "run-changelogs":
            targets = _filter_entities(entities, args.entity)
            with PoliteHttpClient() as client:
                for ent in targets:
                    res = collect_changelog_feed(ent, client, raw_root)
                    if res is None:
                        continue
                    wh.record_crawl_run(res.crawl_run)
                    if res.crawl_run.error_msg is None and res.crawl_run.http_status == 200:
                        diffs = wh.ingest_changelog_snapshot(
                            ent.entity_id,
                            res.entries,
                            res.crawl_run.completed_at_utc,
                            res.crawl_run.run_id,
                        )
                        print(
                            f"[CHANGELOG] {ent.entity_id}: status={res.crawl_run.http_status} "
                            f"entries={len(res.entries)} diffs={len(diffs)}"
                        )
                    else:
                        print(
                            f"[CHANGELOG] {ent.entity_id}: skipped/error "
                            f"robots={res.crawl_run.robots_allowed} "
                            f"status={res.crawl_run.http_status} msg={res.crawl_run.error_msg}"
                        )
            return 0

        if args.command == "query-pit":
            as_of = _parse_iso(args.as_of)
            jobs = wh.query_jobs_as_of(as_of, entity_id=args.entity)
            tiers = wh.query_pricing_as_of(as_of, entity_id=args.entity)
            print(
                json.dumps(
                    {
                        "as_of_utc": as_of.isoformat(),
                        "active_jobs_count": len(jobs),
                        "pricing_tiers_count": len(tiers),
                        "jobs": jobs[:20],
                        "pricing_tiers": tiers,
                    },
                    default=str,
                    indent=2,
                )
            )
            return 0

        if args.command == "export":
            paths = wh.export_pit_datasets(export_dir)
            print(f"Exported {len(paths)} Parquet/CSV files to {export_dir}")
            return 0

        if args.command == "report":
            as_of = _parse_iso(args.as_of) if args.as_of else utc_now()
            hiring_mix = wh.get_hiring_mix_summary(as_of)
            pricing_tiers = wh.query_pricing_as_of(as_of)
            recent_diffs = wh.get_recent_signal_diffs(limit=25)
            crawl_health = wh.get_crawl_health_summary()
            release_velocity = wh.get_release_velocity_summary(as_of, window_days=30)

            export_dir.mkdir(parents=True, exist_ok=True)
            md_path = export_dir / "signal_digest.md"
            json_path = export_dir / "signal_digest.json"
            md_text = render_signal_digest_markdown(
                as_of_utc=as_of,
                hiring_mix=hiring_mix,
                pricing_tiers=pricing_tiers,
                recent_diffs=recent_diffs,
                crawl_health=crawl_health,
                release_velocity=release_velocity,
            )
            md_path.write_text(md_text, encoding="utf-8")
            json_path.write_text(
                json.dumps(
                    {
                        "as_of_utc": as_of.isoformat(),
                        "hiring_mix": hiring_mix,
                        "pricing_tiers": pricing_tiers,
                        "recent_diffs": recent_diffs,
                        "crawl_health": crawl_health,
                        "release_velocity": release_velocity,
                    },
                    default=str,
                    indent=2,
                ),
                encoding="utf-8",
            )
            print(f"Generated signal digest at {md_path} and {json_path}")
            return 0

        if args.command == "dashboard":
            run_dashboard_server(db_path=db_path, host=args.host, port=args.port)
            return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
