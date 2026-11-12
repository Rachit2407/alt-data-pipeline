"""Pydantic v2 schemas and deterministic hashing helpers for PIT alt-data."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

import yaml
from pydantic import BaseModel, Field, field_validator


def utc_now() -> datetime:
    """Return current timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


def compute_sha256(payload: bytes | str) -> str:
    """Compute hex SHA-256 digest of bytes or UTF-8 string."""
    raw = payload.encode("utf-8") if isinstance(payload, str) else payload
    return hashlib.sha256(raw).hexdigest()


def canonical_json_hash(data: Any) -> str:
    """Hash JSON-serializable data deterministically regardless of key order."""
    serialized = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return compute_sha256(serialized)


SourceType = Literal["ats_jobs", "pricing_page", "changelog"]
AtsProvider = Literal["greenhouse", "lever"]
RoleFamily = Literal[
    "engineering",
    "ai_ml",
    "data",
    "product",
    "gtm",
    "security",
    "ops",
    "other",
]
BillingPeriod = Literal["monthly", "annual", "usage", "custom"]


class EntityConfig(BaseModel):
    """Canonical company entity mapping across public ATS boards and pricing pages."""

    entity_id: str = Field(..., min_length=1, pattern=r"^[a-z0-9_-]+$")
    company_name: str = Field(..., min_length=1)
    domain: str = Field(..., min_length=1)
    ticker: Optional[str] = None
    sector: str = Field(..., min_length=1)
    ats_provider: Optional[AtsProvider] = None
    ats_slug: Optional[str] = None
    pricing_url: Optional[str] = None
    changelog_url: Optional[str] = None
    is_active: bool = True

    @field_validator("ticker")
    @classmethod
    def normalize_ticker(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        cleaned = value.strip().upper()
        return cleaned or None


def load_entities(config_path: Path) -> list[EntityConfig]:
    """Load and validate entity registry from YAML."""
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    items = raw.get("entities", [])
    return [EntityConfig.model_validate(item) for item in items]


class CrawlRun(BaseModel):
    """Audit record for every attempted target fetch."""

    run_id: str
    entity_id: str
    source_type: SourceType
    target_url: str
    robots_allowed: bool
    http_status: Optional[int] = None
    content_sha256: Optional[str] = None
    started_at_utc: datetime
    completed_at_utc: datetime
    error_msg: Optional[str] = None


class JobPostingSnapshot(BaseModel):
    """Normalized public ATS job posting extracted at a single crawl timestamp."""

    entity_id: str
    ats_provider: AtsProvider
    external_job_id: str
    title: str
    department_raw: Optional[str] = None
    role_family: RoleFamily = "other"
    location_raw: Optional[str] = None
    workplace_type: Optional[str] = None
    job_url: Optional[str] = None
    source_updated_at: Optional[datetime] = None
    content_sha256: str


class PricingTierSnapshot(BaseModel):
    """Normalized SaaS pricing tier extracted from a public pricing page."""

    entity_id: str
    tier_name: str
    billing_period: BillingPeriod = "monthly"
    price_amount: Optional[float] = None
    currency: str = "USD"
    price_unit: Optional[str] = None
    is_contact_sales: bool = False
    features_json: list[str] = Field(default_factory=list)
    features_hash: str


class ChangelogEntrySnapshot(BaseModel):
    """Normalized public changelog/release entry from RSS or Atom feed."""

    entity_id: str
    external_entry_id: str
    title: str
    entry_url: Optional[str] = None
    published_at_utc: Optional[datetime] = None
    category: Optional[str] = None
    content_sha256: str


class SignalDiff(BaseModel):
    """Structured change event detected between consecutive Point-in-Time states."""

    diff_id: str
    entity_id: str
    source_type: SourceType
    signal_type: str
    external_ref: str
    field_name: str
    old_value: Optional[str] = None
    new_value: Optional[str] = None
    pct_delta: Optional[float] = None
    detected_at_utc: datetime
    run_id: str
