"""Tests for entity config loading, ATS parsers, and pricing tier extraction."""

from __future__ import annotations

from pathlib import Path

from alt_data.collectors.ats_jobs import (
    classify_role_family,
    parse_greenhouse_jobs,
    parse_lever_jobs,
)
from alt_data.collectors.changelogs import parse_changelog_feed
from alt_data.collectors.pricing_pages import extract_pricing_tiers
from alt_data.models import load_entities


def test_load_entities_yaml() -> None:
    cfg_path = Path(__file__).resolve().parents[1] / "config" / "entities.yaml"
    entities = load_entities(cfg_path)
    assert len(entities) >= 7
    ids = {e.entity_id for e in entities}
    assert {"supabase", "anthropic", "wealthfront"}.issubset(ids)


def test_classify_role_family() -> None:
    assert classify_role_family("Senior Applied AI Engineer", "Research") == "ai_ml"
    assert classify_role_family("Staff Backend Engineer", "Core Infra") == "engineering"
    assert classify_role_family("Enterprise Account Executive", "Sales") == "gtm"
    assert classify_role_family("Application Security Engineer", "Security") == "security"


def test_parse_greenhouse_and_lever_jobs() -> None:
    gh_payload = {
        "jobs": [
            {
                "id": 101,
                "title": "Senior Platform Engineer",
                "departments": [{"name": "Engineering"}],
                "location": {"name": "Remote - US"},
                "absolute_url": "https://boards.greenhouse.io/supabase/jobs/101",
                "updated_at": "2026-11-10T10:00:00Z",
            }
        ]
    }
    gh_rows = parse_greenhouse_jobs(gh_payload, "supabase")
    assert len(gh_rows) == 1
    assert gh_rows[0].external_job_id == "101"
    assert gh_rows[0].role_family == "engineering"

    lever_payload = [
        {
            "id": "abc-99",
            "text": "Machine Learning Engineer",
            "categories": {"department": "AI", "location": "Bengaluru"},
            "workplaceType": "hybrid",
            "hostedUrl": "https://jobs.lever.co/palantir/abc-99",
            "createdAt": 1762800000000,
        }
    ]
    lever_rows = parse_lever_jobs(lever_payload, "palantir")
    assert len(lever_rows) == 1
    assert lever_rows[0].role_family == "ai_ml"
    assert lever_rows[0].location_raw == "Bengaluru"


def test_extract_pricing_tiers_html() -> None:
    html = """
    <html><body>
      <div class="pricing-grid">
        <section class="pricing-card">
          <h3>Free</h3>
          <p>$0 / month</p>
          <ul><li>50,000 MAUs</li><li>Community support</li></ul>
        </section>
        <section class="pricing-card">
          <h3>Pro</h3>
          <p>$25 per seat / month</p>
          <ul><li>100,000 MAUs</li><li>Daily backups</li><li>Email support</li></ul>
        </section>
        <section class="pricing-card">
          <h3>Enterprise</h3>
          <p>Contact sales for custom pricing</p>
          <ul><li>SOC2 &amp; SSO</li><li>Dedicated SLA</li></ul>
        </section>
      </div>
    </body></html>
    """
    tiers = extract_pricing_tiers(html, "supabase")
    by_name = {t.tier_name: t for t in tiers}

    assert set(by_name.keys()) == {"Free", "Pro", "Enterprise"}
    assert by_name["Free"].price_amount == 0.0
    assert by_name["Pro"].price_amount == 25.0
    assert by_name["Pro"].price_unit == "per_seat"
    assert by_name["Pro"].billing_period == "monthly"
    assert len(by_name["Pro"].features_json) == 3
    assert by_name["Enterprise"].is_contact_sales is True
    assert by_name["Enterprise"].billing_period == "custom"


def test_extract_pricing_tiers_rejects_faq_and_keeps_monthly_with_custom_features() -> None:
    html = """
    <html><body>
      <section class="pricing-card">
        <h3>Free</h3>
        <p>$0 / month</p>
        <ul><li>Custom domains included</li><li>Usage-based add-ons optional</li></ul>
      </section>
      <section class="pricing-card">
        <h3>Can I use this free forever?</h3>
        <p>Yes, free for $0.</p>
      </section>
      <section class="pricing-card">
        <h3>Scale compute up to 64 vCPUs and 256 GB RAM</h3>
        <p>Starting at $10/month</p>
      </section>
    </body></html>
    """
    tiers = extract_pricing_tiers(html, "example")
    assert len(tiers) == 1
    assert tiers[0].tier_name == "Free"
    assert tiers[0].billing_period == "monthly"


def test_free_tier_with_usage_example_stays_zero() -> None:
    html = """
    <html><body>
      <section class="pricing-card">
        <h3>Free</h3>
        <p>$0 / month — Free forever. Paid add-ons start at $2.00 per 1M events.</p>
        <ul><li>1M events/month</li></ul>
      </section>
    </body></html>
    """
    tiers = extract_pricing_tiers(html, "posthog")
    assert len(tiers) == 1
    assert tiers[0].tier_name == "Free"
    assert tiers[0].price_amount == 0.0


def test_parse_changelog_feed_rss_and_atom() -> None:
    rss_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel><title>Supabase</title>
      <item>
        <title>Postgres 17 Support</title>
        <link>https://supabase.com/blog/pg17</link>
        <guid>pg17-guid</guid>
        <pubDate>Wed, 11 Nov 2026 10:00:00 GMT</pubDate>
        <category>Database</category>
      </item>
    </channel></rss>"""
    atom_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom"><title>Vercel</title>
      <entry>
        <title>Fluid Compute GA</title>
        <id>tag:vercel.com,2026:fluid</id>
        <link rel="alternate" href="https://vercel.com/changelog/fluid-compute" />
        <updated>2026-11-10T15:30:00Z</updated>
      </entry>
    </feed>"""
    rss_entries = parse_changelog_feed(rss_xml, "supabase")
    atom_entries = parse_changelog_feed(atom_xml, "vercel")

    assert len(rss_entries) == 1
    assert rss_entries[0].external_entry_id == "pg17-guid"
    assert rss_entries[0].category == "Database"
    assert len(atom_entries) == 1
    assert atom_entries[0].title == "Fluid Compute GA"
    assert atom_entries[0].entry_url == "https://vercel.com/changelog/fluid-compute"
