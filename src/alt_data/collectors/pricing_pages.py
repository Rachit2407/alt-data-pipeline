"""Public SaaS pricing page collector and deterministic tier extractor."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from bs4 import BeautifulSoup, Tag

from alt_data.compliance import DEFAULT_USER_AGENT, PoliteHttpClient
from alt_data.models import (
    BillingPeriod,
    CrawlRun,
    EntityConfig,
    PricingTierSnapshot,
    canonical_json_hash,
    compute_sha256,
    utc_now,
)

PRICE_PATTERN = re.compile(
    r"(?P<currency>[$€£])\s*(?P<amount>\d+(?:,\d{3})*(?:\.\d{1,2})?)",
    re.IGNORECASE,
)

CURRENCY_SYMBOLS = {
    "$": "USD",
    "€": "EUR",
    "£": "GBP",
}

KNOWN_TIER_NAMES = (
    "free",
    "hobby",
    "starter",
    "basic",
    "pro",
    "plus",
    "team",
    "teams",
    "growth",
    "business",
    "scale",
    "enterprise",
    "custom",
    "pay as you go",
)


@dataclass(frozen=True)
class PricingCollectionResult:
    """Output of collecting and parsing a single entity's public pricing page."""

    crawl_run: CrawlRun
    tiers: list[PricingTierSnapshot]
    raw_path: Optional[Path]


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _detect_billing_period(text: str, is_contact_sales: bool = False) -> BillingPeriod:
    lowered = text.lower()
    if is_contact_sales:
        return "custom"
    if any(tok in lowered for tok in ("/yr", "per year", "annually", "billed yearly", "/year")):
        return "annual"
    if any(tok in lowered for tok in ("/mo", "per month", "/month", "monthly", "$0 forever", "free forever")):
        return "monthly"
    if any(tok in lowered for tok in ("per gb", "per 1m", "per million", "per request", "pay as you go", "per host")):
        return "usage"
    if any(tok in lowered for tok in ("custom pricing", "contact sales", "talk to sales", "request a quote")):
        return "custom"
    return "monthly"


def _detect_price_unit(text: str) -> Optional[str]:
    lowered = text.lower()
    if any(tok in lowered for tok in ("per seat", "/seat", "per user", "/user", "per member")):
        return "per_seat"
    if any(tok in lowered for tok in ("per project", "/project")):
        return "per_project"
    if any(tok in lowered for tok in ("/mo", "per month", "/month")):
        return "flat_monthly"
    if any(tok in lowered for tok in ("/yr", "per year", "/year")):
        return "flat_annual"
    return None


def _is_candidate_card(tag: Tag) -> bool:
    heading = tag.find(["h2", "h3", "h4"])
    if not heading:
        return False
    heading_text = _clean_text(heading.get_text())
    if not heading_text or heading_text.endswith("?") or len(heading_text.split()) > 6:
        return False

    attrs_blob = " ".join(
        [
            " ".join(tag.get("class", [])),
            str(tag.get("id") or ""),
            str(tag.get("data-testid") or ""),
        ]
    ).lower()
    if any(hint in attrs_blob for hint in ("tier", "plan", "pricing-card", "price-card", "package")):
        # Avoid matching huge section wrappers even if classed as pricing/plans.
        return len(_clean_text(tag.get_text())) <= 2200

    # Heuristic fallback: compact section/article/div with a known tier heading and price/CTA cue.
    text = _clean_text(tag.get_text())
    if len(text) > 1800 or len(text) < 12:
        return False
    # Reject outer wrapper containers that contain multiple distinct tier headings.
    sub_headings = [_clean_text(h.get_text()).lower() for h in tag.find_all(["h2", "h3", "h4"])]
    tier_hits = sum(1 for h in sub_headings if any(k == h or k in h.split() for k in KNOWN_TIER_NAMES))
    if tier_hits > 1:
        return False
    lowered_heading = heading_text.lower()
    has_known_name = any(k == lowered_heading or k in lowered_heading.split() for k in KNOWN_TIER_NAMES)
    has_price_or_cta = bool(PRICE_PATTERN.search(text)) or any(
        cue in text.lower() for cue in ("contact sales", "free", "$0", "talk to us")
    )
    return has_known_name and has_price_or_cta


def _extract_card(card: Tag, entity_id: str) -> Optional[PricingTierSnapshot]:
    heading = card.find(["h2", "h3", "h4"])
    if not heading:
        return None
    tier_name = _clean_text(heading.get_text())
    if not tier_name or tier_name.endswith("?") or len(tier_name) > 50 or len(tier_name.split()) > 6:
        return None

    card_text = _clean_text(card.get_text(" "))
    lowered = card_text.lower()

    match = PRICE_PATTERN.search(card_text)
    price_amount: Optional[float] = None
    currency = "USD"
    is_contact_sales = False

    if tier_name.lower() in ("free", "hobby") and re.search(
        r"(?:\$0\b|free\s+forever|always\s+free|\bfree\b)", lowered[:240]
    ):
        price_amount = 0.0
    elif match:
        currency = CURRENCY_SYMBOLS.get(match.group("currency"), "USD")
        price_amount = float(match.group("amount").replace(",", ""))
    elif re.search(r"\bfree\b", lowered):
        price_amount = 0.0
    elif any(cue in lowered for cue in ("contact sales", "talk to sales", "custom pricing", "get a quote")):
        is_contact_sales = True

    if price_amount is None and not is_contact_sales:
        return None

    billing_period = _detect_billing_period(card_text, is_contact_sales=is_contact_sales)

    price_unit = _detect_price_unit(card_text)

    features: list[str] = []
    seen_features: set[str] = set()
    for li in card.find_all("li"):
        feat = _clean_text(li.get_text(" "))
        if feat and len(feat) <= 180 and feat not in seen_features:
            seen_features.add(feat)
            features.append(feat)

    features_hash = canonical_json_hash(features)
    return PricingTierSnapshot(
        entity_id=entity_id,
        tier_name=tier_name,
        billing_period=billing_period,
        price_amount=price_amount,
        currency=currency,
        price_unit=price_unit,
        is_contact_sales=is_contact_sales,
        features_json=features,
        features_hash=features_hash,
    )


def extract_pricing_tiers(html: str, entity_id: str) -> list[PricingTierSnapshot]:
    """Deterministically extract structured SaaS pricing tiers from HTML."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()

    candidates: list[Tag] = []
    for tag in soup.find_all(["section", "article", "div"]):
        if _is_candidate_card(tag):
            candidates.append(tag)

    # Deduplicate by (tier_name_lower, billing_period), preferring richer feature lists.
    deduped: dict[tuple[str, BillingPeriod], PricingTierSnapshot] = {}
    for card in candidates:
        tier = _extract_card(card, entity_id)
        if tier is None:
            continue
        key = (tier.tier_name.lower(), tier.billing_period)
        existing = deduped.get(key)
        if existing is None or len(tier.features_json) > len(existing.features_json):
            deduped[key] = tier

    return list(deduped.values())


def _render_with_playwright(url: str) -> bytes:
    """Optional JS-rendered snapshot helper using Playwright (only after robots.txt allows)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright is not installed. Install optional extra with `pip install -e .[browser]`."
        ) from exc

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=DEFAULT_USER_AGENT)
        page = context.new_page()
        page.goto(url, wait_until="networkidle", timeout=30000)
        html = page.content()
        browser.close()
        return html.encode("utf-8")


def collect_pricing_page(
    entity: EntityConfig,
    http_client: PoliteHttpClient,
    raw_root: Path,
    use_playwright: bool = False,
) -> Optional[PricingCollectionResult]:
    """Fetch, archive raw HTML, and extract structured pricing tiers for one entity."""
    if not entity.pricing_url:
        return None

    run_id = uuid.uuid4().hex[:12]
    started_at = utc_now()
    fetch_res = http_client.get(entity.pricing_url)

    raw_path: Optional[Path] = None
    content_sha256: Optional[str] = None
    tiers: list[PricingTierSnapshot] = []
    error_msg = fetch_res.error_msg
    body_bytes = fetch_res.body_bytes

    if fetch_res.is_success and use_playwright:
        try:
            body_bytes = _render_with_playwright(fetch_res.final_url)
        except Exception as exc:  # noqa: BLE001
            error_msg = f"Playwright render failed (using HTTP body fallback): {exc}"

    if fetch_res.is_success and body_bytes is not None:
        content_sha256 = compute_sha256(body_bytes)
        date_dir = raw_root / "pricing" / started_at.strftime("%Y-%m-%d")
        date_dir.mkdir(parents=True, exist_ok=True)
        raw_path = date_dir / f"{entity.entity_id}_{run_id}.html"
        raw_path.write_bytes(body_bytes)

        html = body_bytes.decode("utf-8", errors="replace")
        tiers = extract_pricing_tiers(html, entity.entity_id)

    completed_at = utc_now()
    crawl_run = CrawlRun(
        run_id=run_id,
        entity_id=entity.entity_id,
        source_type="pricing_page",
        target_url=entity.pricing_url,
        robots_allowed=fetch_res.robots_allowed,
        http_status=fetch_res.status_code,
        content_sha256=content_sha256,
        started_at_utc=started_at,
        completed_at_utc=completed_at,
        error_msg=error_msg,
    )
    return PricingCollectionResult(crawl_run=crawl_run, tiers=tiers, raw_path=raw_path)
