"""Public developer changelog / release velocity collector (RSS 2.0 & Atom)."""

from __future__ import annotations

import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Optional

from alt_data.compliance import PoliteHttpClient
from alt_data.models import (
    ChangelogEntrySnapshot,
    CrawlRun,
    EntityConfig,
    canonical_json_hash,
    compute_sha256,
    utc_now,
)


@dataclass(frozen=True)
class ChangelogCollectionResult:
    """Output of collecting a single entity's public RSS/Atom changelog."""

    crawl_run: CrawlRun
    entries: list[ChangelogEntrySnapshot]
    raw_path: Optional[Path]


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _parse_feed_datetime(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    text = raw.strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(text)
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def parse_changelog_feed(xml_text: str, entity_id: str) -> list[ChangelogEntrySnapshot]:
    """Parse RSS 2.0 or Atom XML text into normalized ChangelogEntrySnapshot items."""
    root = ET.fromstring(xml_text)
    entries: list[ChangelogEntrySnapshot] = []
    seen_ids: set[str] = set()

    for elem in root.iter():
        local_name = _strip_ns(elem.tag)
        if local_name not in ("item", "entry"):
            continue

        title = ""
        link: Optional[str] = None
        guid: Optional[str] = None
        pub_raw: Optional[str] = None
        category: Optional[str] = None

        for child in elem:
            cname = _strip_ns(child.tag)
            ctext = (child.text or "").strip()
            if cname == "title" and ctext:
                title = " ".join(ctext.split())
            elif cname == "link":
                href = child.attrib.get("href")
                if href and (link is None or child.attrib.get("rel", "alternate") == "alternate"):
                    link = href.strip()
                elif ctext and link is None:
                    link = ctext
            elif cname in ("guid", "id") and ctext:
                guid = ctext
            elif cname in ("pubdate", "published", "updated") and ctext and pub_raw is None:
                pub_raw = ctext
            elif cname == "category":
                cat_val = child.attrib.get("term") or ctext
                if cat_val and category is None:
                    category = cat_val.strip()

        if not title:
            continue

        ext_id = guid or link or compute_sha256(f"{entity_id}:{title}")[:24]
        if ext_id in seen_ids:
            continue
        seen_ids.add(ext_id)

        pub_dt = _parse_feed_datetime(pub_raw)
        content_hash = canonical_json_hash(
            {
                "title": title,
                "entry_url": link,
                "published_at_utc": pub_dt.isoformat() if pub_dt else None,
                "category": category,
            }
        )
        entries.append(
            ChangelogEntrySnapshot(
                entity_id=entity_id,
                external_entry_id=ext_id[:240],
                title=title[:300],
                entry_url=link,
                published_at_utc=pub_dt,
                category=category,
                content_sha256=content_hash,
            )
        )

    return entries


def collect_changelog_feed(
    entity: EntityConfig,
    http_client: PoliteHttpClient,
    raw_root: Path,
) -> Optional[ChangelogCollectionResult]:
    """Fetch, archive raw XML, and parse public RSS/Atom changelog for one entity."""
    if not entity.changelog_url:
        return None

    run_id = uuid.uuid4().hex[:12]
    started_at = utc_now()
    fetch_res = http_client.get(entity.changelog_url)

    raw_path: Optional[Path] = None
    content_sha256: Optional[str] = None
    entries: list[ChangelogEntrySnapshot] = []
    error_msg = fetch_res.error_msg

    if fetch_res.is_success and fetch_res.body_bytes is not None:
        content_sha256 = compute_sha256(fetch_res.body_bytes)
        date_dir = raw_root / "changelogs" / started_at.strftime("%Y-%m-%d")
        date_dir.mkdir(parents=True, exist_ok=True)
        raw_path = date_dir / f"{entity.entity_id}_{run_id}.xml"
        raw_path.write_bytes(fetch_res.body_bytes)

        try:
            xml_text = fetch_res.body_bytes.decode("utf-8", errors="replace")
            entries = parse_changelog_feed(xml_text, entity.entity_id)
        except ET.ParseError as exc:
            error_msg = f"XML parse error: {exc}"

    completed_at = utc_now()
    crawl_run = CrawlRun(
        run_id=run_id,
        entity_id=entity.entity_id,
        source_type="changelog",
        target_url=entity.changelog_url,
        robots_allowed=fetch_res.robots_allowed,
        http_status=fetch_res.status_code,
        content_sha256=content_sha256,
        started_at_utc=started_at,
        completed_at_utc=completed_at,
        error_msg=error_msg,
    )
    return ChangelogCollectionResult(crawl_run=crawl_run, entries=entries, raw_path=raw_path)
