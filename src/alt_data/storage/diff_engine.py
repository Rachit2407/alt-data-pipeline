"""Automated diff-detection and hiring velocity signal computation."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from alt_data.models import JobPostingSnapshot, PricingTierSnapshot, SignalDiff


def compute_pct_delta(old_val: Optional[float], new_val: Optional[float]) -> Optional[float]:
    """Compute percentage change rounded to 2 decimals when old_val > 0."""
    if old_val is None or new_val is None or old_val == 0:
        return None
    return round(((new_val - old_val) / old_val) * 100.0, 2)


def make_diff(
    entity_id: str,
    source_type: str,
    signal_type: str,
    external_ref: str,
    field_name: str,
    old_value: Optional[str],
    new_value: Optional[str],
    detected_at_utc: datetime,
    run_id: str,
    pct_delta: Optional[float] = None,
) -> SignalDiff:
    """Create a normalized SignalDiff record."""
    return SignalDiff(
        diff_id=uuid.uuid4().hex[:16],
        entity_id=entity_id,
        source_type=source_type,  # type: ignore[arg-type]
        signal_type=signal_type,
        external_ref=external_ref,
        field_name=field_name,
        old_value=old_value,
        new_value=new_value,
        pct_delta=pct_delta,
        detected_at_utc=detected_at_utc,
        run_id=run_id,
    )


def build_hiring_velocity_diff(
    entity_id: str,
    previous_active_count: int,
    current_active_count: int,
    detected_at_utc: datetime,
    run_id: str,
    min_abs_delta: int = 3,
    min_pct_delta: float = 15.0,
) -> Optional[SignalDiff]:
    """Emit HIRING_VELOCITY_SHIFT when active openings shift meaningfully vs prior snapshot."""
    if previous_active_count <= 0:
        return None
    abs_delta = current_active_count - previous_active_count
    pct_delta = compute_pct_delta(float(previous_active_count), float(current_active_count))
    if pct_delta is None:
        return None
    if abs(abs_delta) < min_abs_delta or abs(pct_delta) < min_pct_delta:
        return None
    return make_diff(
        entity_id=entity_id,
        source_type="ats_jobs",
        signal_type="HIRING_VELOCITY_SHIFT",
        external_ref=f"{entity_id}:active_postings",
        field_name="active_job_count",
        old_value=str(previous_active_count),
        new_value=str(current_active_count),
        pct_delta=pct_delta,
        detected_at_utc=detected_at_utc,
        run_id=run_id,
    )


def summarize_job_snapshot(postings: list[JobPostingSnapshot]) -> dict[str, int]:
    """Return counts by role_family for a single ATS snapshot."""
    counts: dict[str, int] = {"total": len(postings)}
    for p in postings:
        counts[p.role_family] = counts.get(p.role_family, 0) + 1
    return counts


def summarize_pricing_snapshot(tiers: list[PricingTierSnapshot]) -> list[dict[str, object]]:
    """Return compact summary dicts for CLI logging."""
    return [
        {
            "tier_name": t.tier_name,
            "billing_period": t.billing_period,
            "price_amount": t.price_amount,
            "currency": t.currency,
            "is_contact_sales": t.is_contact_sales,
            "feature_count": len(t.features_json),
        }
        for t in tiers
    ]


def render_signal_digest_markdown(
    as_of_utc: datetime,
    hiring_mix: list[dict[str, object]],
    pricing_tiers: list[dict[str, object]],
    recent_diffs: list[dict[str, object]],
    crawl_health: list[dict[str, object]],
    release_velocity: Optional[list[dict[str, object]]] = None,
) -> str:
    """Render a clean Markdown digest of PIT hiring mix, pricing tiers, and signal diffs."""
    lines: list[str] = [
        "# Alternative Data Signal Digest",
        "",
        f"**Point-in-Time Snapshot (`as_of_utc`):** `{as_of_utc.isoformat()}`",
        "",
        "## 1. Hiring Velocity & Technical Role Mix",
        "",
        "| Entity | Active Roles | Engineering | AI / ML | Data | Security | GTM | Tech Share % |",
        "| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in hiring_mix:
        lines.append(
            f"| {row['entity_id']} | {row['total_active']} | {row['engineering']} | "
            f"{row['ai_ml']} | {row['data']} | {row['security']} | {row['gtm']} | {row['tech_share_pct']}% |"
        )
    if not hiring_mix:
        lines.append("| _none_ | 0 | 0 | 0 | 0 | 0 | 0 | 0.0% |")

    lines.extend(
        [
            "",
            "## 2. Active SaaS Pricing Tiers (PIT)",
            "",
            "| Entity | Tier | Billing | Price | Contact Sales |",
            "| :--- | :--- | :--- | ---: | :--- |",
        ]
    )
    for t in pricing_tiers:
        price_str = "Custom" if t["is_contact_sales"] else f"{t['currency']} {t['price_amount']}"
        lines.append(
            f"| {t['entity_id']} | {t['tier_name']} | {t['billing_period']} | {price_str} | {t['is_contact_sales']} |"
        )
    if not pricing_tiers:
        lines.append("| _none_ | - | - | - | - |")

    lines.extend(
        [
            "",
            "## 3. High-Signal Diffs (Top 25)",
            "",
            "| Entity | Signal | Ref | Old | New | Delta % |",
            "| :--- | :--- | :--- | :--- | :--- | ---: |",
        ]
    )
    for d in recent_diffs:
        delta_str = f"{d['pct_delta']}%" if d["pct_delta"] is not None else "-"
        lines.append(
            f"| {d['entity_id']} | {d['signal_type']} | `{d['external_ref']}` | "
            f"{d['old_value'] or '-'} | {d['new_value'] or '-'} | {delta_str} |"
        )
    if not recent_diffs:
        lines.append("| _none_ | - | - | - | - | - |")

    lines.extend(
        [
            "",
            "## 4. Compliance & Crawl Audit",
            "",
            "| Source Type | robots.txt Allowed | HTTP Status | Runs |",
            "| :--- | :--- | ---: | ---: |",
        ]
    )
    for ch in crawl_health:
        lines.append(
            f"| {ch['source_type']} | {ch['robots_allowed']} | {ch['http_status'] or 'ERR'} | {ch['runs']} |"
        )

    if release_velocity is not None:
        lines.extend(
            [
                "",
                "## 5. Product Release Velocity (Last 30 Days PIT)",
                "",
                "| Entity | Releases (30d) | Total Tracked | Latest Release (UTC) |",
                "| :--- | ---: | ---: | :--- |",
            ]
        )
        for rv in release_velocity:
            lines.append(
                f"| {rv['entity_id']} | {rv['releases_last_30d']} | {rv['total_tracked']} | "
                f"{rv['latest_release_utc'] or '-'} |"
            )
        if not release_velocity:
            lines.append("| _none_ | 0 | 0 | - |")

    return "\n".join(lines) + "\n"
