"""Public ATS job board collectors (Greenhouse & Lever unauthenticated JSON feeds)."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from alt_data.compliance import PoliteHttpClient
from alt_data.models import (
    AtsProvider,
    CrawlRun,
    EntityConfig,
    JobPostingSnapshot,
    RoleFamily,
    canonical_json_hash,
    compute_sha256,
    utc_now,
)


@dataclass(frozen=True)
class AtsCollectionResult:
    """Output of collecting a single entity's ATS board."""

    crawl_run: CrawlRun
    postings: list[JobPostingSnapshot]
    raw_path: Optional[Path]


def build_ats_url(provider: AtsProvider, slug: str) -> str:
    """Return the official public JSON endpoint for a Greenhouse or Lever board."""
    if provider == "greenhouse":
        return f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    if provider == "lever":
        return f"https://api.lever.co/v0/postings/{slug}?mode=json"
    raise ValueError(f"Unsupported ATS provider: {provider}")


def classify_role_family(title: str, department: Optional[str] = None) -> RoleFamily:
    """Deterministically bucket a job title and department into a role family."""
    haystack = f" {title or ''} {department or ''} ".lower()

    if any(
        kw in haystack
        for kw in (
            "machine learning",
            "applied ai",
            "generative ai",
            "research scientist",
            "deep learning",
            " ai ",
            " ml ",
            " llm",
            " nlp",
        )
    ):
        return "ai_ml"
    if any(
        kw in haystack
        for kw in ("security", "appsec", "infosec", "secops", "red team", "trust & safety")
    ):
        return "security"
    if any(
        kw in haystack
        for kw in ("data engineer", "data scientist", "analytics", "business intelligence")
    ):
        return "data"
    if any(
        kw in haystack
        for kw in (
            "engineer",
            "developer",
            "sre",
            "devops",
            "infrastructure",
            "platform",
            "backend",
            "frontend",
            "full stack",
            "fullstack",
            "software",
            "systems",
            "architect",
            "mobile",
            "ios",
            "android",
        )
    ):
        return "engineering"
    if any(kw in haystack for kw in ("product manager", "product design", "ux", "ui designer", "design")):
        return "product"
    if any(
        kw in haystack
        for kw in (
            "sales",
            "account executive",
            "sdr",
            "bdr",
            "marketing",
            "growth",
            "revenue",
            "customer success",
            "solutions architect",
            "solutions engineer",
            "partnerships",
        )
    ):
        return "gtm"
    if any(
        kw in haystack
        for kw in ("finance", "legal", "people", "recruiter", "talent", "operations", "payroll", " hr ")
    ):
        return "ops"
    return "other"


def _parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def parse_greenhouse_jobs(payload: dict[str, Any], entity_id: str) -> list[JobPostingSnapshot]:
    """Parse Greenhouse boards-api JSON into normalized JobPostingSnapshot models."""
    jobs = payload.get("jobs", [])
    results: list[JobPostingSnapshot] = []
    for item in jobs:
        if not isinstance(item, dict) or "id" not in item:
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        departments = item.get("departments") or []
        dept_raw = (
            str(departments[0].get("name")).strip()
            if departments and isinstance(departments[0], dict) and departments[0].get("name")
            else None
        )
        location_obj = item.get("location") or {}
        location_raw = (
            str(location_obj.get("name")).strip()
            if isinstance(location_obj, dict) and location_obj.get("name")
            else None
        )
        job_url = str(item.get("absolute_url")).strip() if item.get("absolute_url") else None
        role_family = classify_role_family(title, dept_raw)
        content_hash = canonical_json_hash(
            {
                "title": title,
                "department_raw": dept_raw,
                "role_family": role_family,
                "location_raw": location_raw,
                "job_url": job_url,
            }
        )
        results.append(
            JobPostingSnapshot(
                entity_id=entity_id,
                ats_provider="greenhouse",
                external_job_id=str(item["id"]),
                title=title,
                department_raw=dept_raw,
                role_family=role_family,
                location_raw=location_raw,
                workplace_type=None,
                job_url=job_url,
                source_updated_at=_parse_iso_datetime(item.get("updated_at")),
                content_sha256=content_hash,
            )
        )
    return results


def parse_lever_jobs(payload: list[Any], entity_id: str) -> list[JobPostingSnapshot]:
    """Parse Lever public postings JSON array into normalized JobPostingSnapshot models."""
    results: list[JobPostingSnapshot] = []
    for item in payload:
        if not isinstance(item, dict) or "id" not in item:
            continue
        title = str(item.get("text") or "").strip()
        if not title:
            continue
        categories = item.get("categories") or {}
        dept_raw = None
        location_raw = None
        if isinstance(categories, dict):
            dept_val = categories.get("department") or categories.get("team")
            dept_raw = str(dept_val).strip() if dept_val else None
            loc_val = categories.get("location")
            location_raw = str(loc_val).strip() if loc_val else None

        workplace_type = str(item.get("workplaceType")).strip() if item.get("workplaceType") else None
        job_url = str(item.get("hostedUrl")).strip() if item.get("hostedUrl") else None

        created_ms = item.get("createdAt")
        source_updated_at = (
            datetime.fromtimestamp(created_ms / 1000.0, tz=timezone.utc)
            if isinstance(created_ms, (int, float))
            else None
        )
        role_family = classify_role_family(title, dept_raw)
        content_hash = canonical_json_hash(
            {
                "title": title,
                "department_raw": dept_raw,
                "role_family": role_family,
                "location_raw": location_raw,
                "workplace_type": workplace_type,
                "job_url": job_url,
            }
        )
        results.append(
            JobPostingSnapshot(
                entity_id=entity_id,
                ats_provider="lever",
                external_job_id=str(item["id"]),
                title=title,
                department_raw=dept_raw,
                role_family=role_family,
                location_raw=location_raw,
                workplace_type=workplace_type,
                job_url=job_url,
                source_updated_at=source_updated_at,
                content_sha256=content_hash,
            )
        )
    return results


def collect_ats_board(
    entity: EntityConfig,
    http_client: PoliteHttpClient,
    raw_root: Path,
) -> Optional[AtsCollectionResult]:
    """Fetch, archive raw JSON, and parse public ATS job postings for one entity."""
    if not entity.ats_provider or not entity.ats_slug:
        return None

    run_id = uuid.uuid4().hex[:12]
    started_at = utc_now()
    target_url = build_ats_url(entity.ats_provider, entity.ats_slug)
    fetch_res = http_client.get(target_url)

    raw_path: Optional[Path] = None
    content_sha256: Optional[str] = None
    postings: list[JobPostingSnapshot] = []
    error_msg = fetch_res.error_msg

    if fetch_res.is_success and fetch_res.body_bytes is not None:
        content_sha256 = compute_sha256(fetch_res.body_bytes)
        date_dir = raw_root / "ats" / started_at.strftime("%Y-%m-%d")
        date_dir.mkdir(parents=True, exist_ok=True)
        raw_path = date_dir / f"{entity.entity_id}_{run_id}.json"
        raw_path.write_bytes(fetch_res.body_bytes)

        try:
            payload = json.loads(fetch_res.body_bytes.decode("utf-8"))
            if entity.ats_provider == "greenhouse" and isinstance(payload, dict):
                postings = parse_greenhouse_jobs(payload, entity.entity_id)
            elif entity.ats_provider == "lever" and isinstance(payload, list):
                postings = parse_lever_jobs(payload, entity.entity_id)
            else:
                error_msg = f"Unexpected JSON payload shape for {entity.ats_provider}."
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            error_msg = f"JSON decode error: {exc}"

    completed_at = utc_now()
    crawl_run = CrawlRun(
        run_id=run_id,
        entity_id=entity.entity_id,
        source_type="ats_jobs",
        target_url=target_url,
        robots_allowed=fetch_res.robots_allowed,
        http_status=fetch_res.status_code,
        content_sha256=content_sha256,
        started_at_utc=started_at,
        completed_at_utc=completed_at,
        error_msg=error_msg,
    )
    return AtsCollectionResult(crawl_run=crawl_run, postings=postings, raw_path=raw_path)
