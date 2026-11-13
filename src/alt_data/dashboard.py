"""Zero-dependency local interactive web dashboard for Point-in-Time alt-data exploration."""

from __future__ import annotations

import json
import urllib.parse
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

from alt_data.storage.duckdb_store import PitWarehouse

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Alt-Data Point-in-Time Intelligence Dashboard</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <style>
    body { background-color: #0b0f17; color: #e2e8f0; font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
    .glass { background: rgba(18, 24, 38, 0.7); backdrop-filter: blur(12px); border: 1px solid rgba(255, 255, 255, 0.08); }
    .glass-card { background: #131b2e; border: 1px solid #1e293b; }
    ::-webkit-scrollbar { width: 6px; height: 6px; }
    ::-webkit-scrollbar-track { background: #0b0f17; }
    ::-webkit-scrollbar-thumb { background: #334155; border-radius: 3px; }
  </style>
</head>
<body class="min-h-screen p-4 md:p-8">
  <div class="max-w-7xl mx-auto space-y-6">

    <!-- Header -->
    <header class="flex flex-col md:flex-row md:items-center justify-between gap-4 border-b border-slate-800 pb-6">
      <div>
        <div class="flex items-center gap-3">
          <div class="w-3.5 h-3.5 rounded-full bg-emerald-500 animate-pulse"></div>
          <h1 class="text-2xl font-bold tracking-tight text-white">Alternative Data PIT Signal Feed</h1>
        </div>
        <p class="text-sm text-slate-400 mt-1">Bitemporal Point-in-Time tracking: ATS hiring mix, SaaS pricing shifts & release cadence with zero look-ahead bias.</p>
      </div>
      <div class="flex items-center gap-2 text-xs font-mono bg-slate-900 border border-slate-800 px-3 py-1.5 rounded-lg text-emerald-400">
        <span class="text-slate-500">Warehouse:</span> DuckDB + Parquet SCD2
      </div>
    </header>

    <!-- Controls Bar -->
    <div class="glass rounded-xl p-4 flex flex-col md:flex-row items-center justify-between gap-4">
      <div class="flex flex-wrap items-center gap-4 w-full md:w-auto">
        <div>
          <label class="block text-xs font-semibold text-slate-400 mb-1">Company Filter</label>
          <select id="entitySelect" class="bg-slate-900 border border-slate-700 text-white text-sm rounded-lg px-3 py-2 focus:ring-2 focus:ring-emerald-500 outline-none">
            <option value="">All Companies (Aggregate)</option>
          </select>
        </div>

        <div>
          <label class="block text-xs font-semibold text-slate-400 mb-1">Point-in-Time "As-Of" Date</label>
          <input type="datetime-local" id="asOfInput" class="bg-slate-900 border border-slate-700 text-white text-sm rounded-lg px-3 py-2 focus:ring-2 focus:ring-emerald-500 outline-none">
        </div>

        <div class="self-end">
          <button id="btnNow" class="bg-slate-800 hover:bg-slate-700 text-slate-300 text-sm font-medium px-3 py-2 rounded-lg transition-colors">
            Reset to Latest
          </button>
        </div>
      </div>

      <div class="text-right w-full md:w-auto">
        <div class="text-xs text-slate-400">Current As-Of Filter (UTC)</div>
        <div id="asOfDisplay" class="text-sm font-mono text-emerald-400 font-semibold">-</div>
      </div>
    </div>

    <!-- KPI Metric Cards -->
    <div class="grid grid-cols-2 md:grid-cols-5 gap-4">
      <div class="glass-card rounded-xl p-4">
        <div class="text-xs text-slate-400 font-medium">Active Job Openings</div>
        <div id="statTotalJobs" class="text-2xl font-bold text-white mt-1">-</div>
        <div class="text-[11px] text-slate-500 mt-1">Unauthenticated ATS boards</div>
      </div>
      <div class="glass-card rounded-xl p-4">
        <div class="text-xs text-slate-400 font-medium">Engineering & AI/ML</div>
        <div id="statTechRoles" class="text-2xl font-bold text-indigo-400 mt-1">-</div>
        <div id="statTechPct" class="text-[11px] text-indigo-300/70 mt-1">-% tech share</div>
      </div>
      <div class="glass-card rounded-xl p-4">
        <div class="text-xs text-slate-400 font-medium">Dedicated AI/ML Roles</div>
        <div id="statAiRoles" class="text-2xl font-bold text-cyan-400 mt-1">-</div>
        <div class="text-[11px] text-cyan-300/70 mt-1">Research & applied AI</div>
      </div>
      <div class="glass-card rounded-xl p-4">
        <div class="text-xs text-slate-400 font-medium">Active SaaS Pricing Tiers</div>
        <div id="statPricingTiers" class="text-2xl font-bold text-amber-400 mt-1">-</div>
        <div class="text-[11px] text-amber-300/70 mt-1">Free, Pro, Team & Ent</div>
      </div>
      <div class="glass-card rounded-xl p-4 col-span-2 md:col-span-1">
        <div class="text-xs text-slate-400 font-medium">Releases (Last 30d)</div>
        <div id="statReleases" class="text-2xl font-bold text-emerald-400 mt-1">-</div>
        <div class="text-[11px] text-emerald-300/70 mt-1">RSS/Atom changelog feeds</div>
      </div>
    </div>

    <!-- Main Content Grid -->
    <div class="grid grid-cols-1 lg:grid-cols-3 gap-6">

      <!-- Left Column: Role Mix Chart & Pricing Tiers -->
      <div class="lg:col-span-2 space-y-6">

        <!-- Role Mix Chart -->
        <div class="glass-card rounded-xl p-5">
          <div class="flex items-center justify-between mb-4">
            <h2 class="text-base font-semibold text-white">Technical Role Mix by Company</h2>
            <span class="text-xs text-slate-400 font-mono">Point-in-Time (SCD Type 2)</span>
          </div>
          <div class="h-64">
            <canvas id="roleMixChart"></canvas>
          </div>
        </div>

        <!-- Active Pricing Tiers Grid -->
        <div class="glass-card rounded-xl p-5">
          <div class="flex items-center justify-between mb-4">
            <h2 class="text-base font-semibold text-white">Active SaaS Pricing Tiers</h2>
            <span class="text-xs text-slate-400">Valid as of selected timestamp</span>
          </div>
          <div id="pricingGrid" class="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 gap-3">
            <div class="text-sm text-slate-500 col-span-full">Loading pricing tiers...</div>
          </div>
        </div>

      </div>

      <!-- Right Column: Chronological Signal Diffs Feed -->
      <div class="space-y-6">
        <div class="glass-card rounded-xl p-5 flex flex-col h-full max-h-[760px]">
          <div class="flex items-center justify-between mb-4 border-b border-slate-800 pb-3">
            <div>
              <h2 class="text-base font-semibold text-white">Signal Diffs Feed</h2>
              <p class="text-xs text-slate-400">Automated state transitions & alerts</p>
            </div>
            <span id="diffCountBadge" class="text-xs font-mono bg-slate-800 text-slate-300 px-2 py-0.5 rounded">0 events</span>
          </div>
          <div id="diffsList" class="flex-1 overflow-y-auto space-y-3 pr-1 text-sm">
            <div class="text-xs text-slate-500">Loading events...</div>
          </div>
        </div>
      </div>

    </div>

  </div>

  <script>
    let roleChart = null;

    function getSignalBadge(type) {
      switch (type) {
        case 'ROLE_OPENED':
          return '<span class="bg-emerald-950 text-emerald-400 border border-emerald-800 text-[10px] font-semibold px-2 py-0.5 rounded">ROLE OPENED</span>';
        case 'ROLE_CLOSED':
          return '<span class="bg-rose-950 text-rose-400 border border-rose-800 text-[10px] font-semibold px-2 py-0.5 rounded">ROLE CLOSED</span>';
        case 'ROLE_UPDATED':
          return '<span class="bg-blue-950 text-blue-400 border border-blue-800 text-[10px] font-semibold px-2 py-0.5 rounded">ROLE UPDATED</span>';
        case 'CHANGELOG_PUBLISHED':
          return '<span class="bg-purple-950 text-purple-400 border border-purple-800 text-[10px] font-semibold px-2 py-0.5 rounded">RELEASE</span>';
        case 'PRICE_CHANGE':
          return '<span class="bg-amber-950 text-amber-400 border border-amber-800 text-[10px] font-semibold px-2 py-0.5 rounded">PRICE CHANGE</span>';
        case 'TIER_ADDED':
          return '<span class="bg-teal-950 text-teal-400 border border-teal-800 text-[10px] font-semibold px-2 py-0.5 rounded">TIER ADDED</span>';
        case 'HIRING_VELOCITY_SHIFT':
          return '<span class="bg-fuchsia-950 text-fuchsia-400 border border-fuchsia-800 text-[10px] font-semibold px-2 py-0.5 rounded">VELOCITY SHIFT</span>';
        default:
          return `<span class="bg-slate-800 text-slate-300 text-[10px] font-semibold px-2 py-0.5 rounded">${type}</span>`;
      }
    }

    async function loadEntities() {
      try {
        const res = await fetch('/api/entities');
        const data = await res.json();
        const sel = document.getElementById('entitySelect');
        data.forEach(e => {
          const opt = document.createElement('option');
          opt.value = e.entity_id;
          opt.textContent = `${e.company_name} (${e.sector})`;
          sel.appendChild(opt);
        });
      } catch (err) {
        console.error('Failed to load entities:', err);
      }
    }

    async function fetchSnapshot() {
      const entity = document.getElementById('entitySelect').value;
      const asOfVal = document.getElementById('asOfInput').value;
      const asOfIso = asOfVal ? new Date(asOfVal).toISOString() : new Date().toISOString();

      document.getElementById('asOfDisplay').textContent = asOfIso.replace('T', ' ').slice(0, 19) + ' UTC';

      const url = `/api/snapshot?as_of=${encodeURIComponent(asOfIso)}&entity=${encodeURIComponent(entity)}`;
      try {
        const res = await fetch(url);
        const data = await res.json();
        renderKPIs(data);
        renderChart(data.hiring_mix);
        renderPricing(data.pricing_tiers);
        renderDiffs(data.recent_diffs);
      } catch (err) {
        console.error('Failed to fetch snapshot:', err);
      }
    }

    function renderKPIs(data) {
      let totalJobs = 0;
      let engRoles = 0;
      let aiRoles = 0;
      (data.hiring_mix || []).forEach(r => {
        totalJobs += r.total_active || 0;
        engRoles += (r.engineering || 0) + (r.ai_ml || 0);
        aiRoles += r.ai_ml || 0;
      });

      let totalReleases = 0;
      (data.release_velocity || []).forEach(r => {
        totalReleases += r.releases_last_30d || 0;
      });

      document.getElementById('statTotalJobs').textContent = totalJobs.toLocaleString();
      document.getElementById('statTechRoles').textContent = engRoles.toLocaleString();
      const techPct = totalJobs > 0 ? ((engRoles / totalJobs) * 100).toFixed(1) : '0.0';
      document.getElementById('statTechPct').textContent = `${techPct}% engineering & AI`;
      document.getElementById('statAiRoles').textContent = aiRoles.toLocaleString();
      document.getElementById('statPricingTiers').textContent = (data.pricing_tiers || []).length;
      document.getElementById('statReleases').textContent = totalReleases.toLocaleString();
    }

    function renderChart(hiringMix) {
      const ctx = document.getElementById('roleMixChart').getContext('2d');
      const labels = (hiringMix || []).map(r => r.entity_id);
      const engineering = (hiringMix || []).map(r => r.engineering || 0);
      const aiMl = (hiringMix || []).map(r => r.ai_ml || 0);
      const security = (hiringMix || []).map(r => r.security || 0);
      const gtm = (hiringMix || []).map(r => r.gtm || 0);

      if (roleChart) {
        roleChart.destroy();
      }

      roleChart = new Chart(ctx, {
        type: 'bar',
        data: {
          labels: labels,
          datasets: [
            { label: 'Engineering', data: engineering, backgroundColor: '#6366f1' },
            { label: 'AI / ML', data: aiMl, backgroundColor: '#06b6d4' },
            { label: 'Security', data: security, backgroundColor: '#10b981' },
            { label: 'GTM / Sales', data: gtm, backgroundColor: '#f59e0b' }
          ]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          scales: {
            x: { stacked: true, grid: { color: '#1e293b' }, ticks: { color: '#94a3b8' } },
            y: { stacked: true, grid: { color: '#1e293b' }, ticks: { color: '#94a3b8' } }
          },
          plugins: {
            legend: { labels: { color: '#cbd5e1', font: { size: 11 } } }
          }
        }
      });
    }

    function renderPricing(tiers) {
      const container = document.getElementById('pricingGrid');
      container.innerHTML = '';
      if (!tiers || tiers.length === 0) {
        container.innerHTML = '<div class="text-sm text-slate-500 col-span-full">No active pricing tiers for selected filter.</div>';
        return;
      }

      tiers.slice(0, 18).forEach(t => {
        const card = document.createElement('div');
        card.className = 'bg-slate-900/80 border border-slate-800 rounded-lg p-3 flex flex-col justify-between';
        const priceStr = t.is_contact_sales ? 'Custom / Contact Sales' : `${t.currency} ${t.price_amount} / ${t.billing_period}`;
        card.innerHTML = `
          <div>
            <div class="flex items-center justify-between gap-1">
              <span class="text-xs font-mono text-slate-400 uppercase tracking-wider">${t.entity_id}</span>
              <span class="text-[10px] font-mono text-slate-500">${t.billing_period}</span>
            </div>
            <div class="text-sm font-semibold text-white mt-1 line-clamp-1">${t.tier_name}</div>
          </div>
          <div class="mt-2 pt-2 border-t border-slate-800/80 flex items-center justify-between">
            <span class="text-xs font-mono text-amber-400 font-medium">${priceStr}</span>
          </div>
        `;
        container.appendChild(card);
      });
    }

    function renderDiffs(diffs) {
      const container = document.getElementById('diffsList');
      const badge = document.getElementById('diffCountBadge');
      container.innerHTML = '';
      if (!diffs || diffs.length === 0) {
        container.innerHTML = '<div class="text-xs text-slate-500">No events found.</div>';
        badge.textContent = '0 events';
        return;
      }
      badge.textContent = `${diffs.length} events`;

      diffs.forEach(d => {
        const item = document.createElement('div');
        item.className = 'p-3 bg-slate-900/60 border border-slate-800/80 rounded-lg space-y-1.5';
        const dtStr = d.detected_at_utc ? String(d.detected_at_utc).slice(0, 19).replace('T', ' ') : '';
        const titleText = d.new_value || d.external_ref || d.field_name;
        item.innerHTML = `
          <div class="flex items-center justify-between gap-2">
            <div class="flex items-center gap-1.5">
              ${getSignalBadge(d.signal_type)}
              <span class="text-xs font-mono text-slate-300 font-semibold">${d.entity_id}</span>
            </div>
            <span class="text-[10px] font-mono text-slate-500">${dtStr}</span>
          </div>
          <div class="text-xs text-slate-200 line-clamp-2">${titleText}</div>
          ${d.old_value ? `<div class="text-[10px] text-slate-400 font-mono">prev: ${d.old_value}</div>` : ''}
        `;
        container.appendChild(item);
      });
    }

    // Set default as_of input to current local time
    const now = new Date();
    now.setMinutes(now.getMinutes() - now.getTimezoneOffset());
    document.getElementById('asOfInput').value = now.toISOString().slice(0, 16);

    document.getElementById('entitySelect').addEventListener('change', fetchSnapshot);
    document.getElementById('asOfInput').addEventListener('change', fetchSnapshot);
    document.getElementById('btnNow').addEventListener('click', () => {
      const curr = new Date();
      curr.setMinutes(curr.getMinutes() - curr.getTimezoneOffset());
      document.getElementById('asOfInput').value = curr.toISOString().slice(0, 16);
      fetchSnapshot();
    });

    window.addEventListener('DOMContentLoaded', async () => {
      await loadEntities();
      await fetchSnapshot();
    });
  </script>
</body>
</html>
"""


class DashboardRequestHandler(BaseHTTPRequestHandler):
    """HTTP request handler serving static dashboard and JSON PIT APIs."""

    def __init__(self, *args: Any, db_path: Path, **kwargs: Any) -> None:
        self.db_path = db_path
        super().__init__(*args, **kwargs)

    def log_message(self, format: str, *args: Any) -> None:
        # Keep server log output clean
        return

    def _send_json(self, data: Any, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(data, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        params = urllib.parse.parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            self._send_html(DASHBOARD_HTML)
            return

        with PitWarehouse(self.db_path) as wh:
            if path == "/api/entities":
                rows = wh.conn.execute(
                    "SELECT entity_id, company_name, domain, sector, ticker FROM dim_entities WHERE is_active = TRUE ORDER BY entity_id"
                ).fetchall()
                entities = [
                    {
                        "entity_id": r[0],
                        "company_name": r[1],
                        "domain": r[2],
                        "sector": r[3],
                        "ticker": r[4],
                    }
                    for r in rows
                ]
                self._send_json(entities)
                return

            if path == "/api/snapshot":
                as_of_str = params.get("as_of", [None])[0]
                entity_id = params.get("entity", [None])[0] or None

                if as_of_str:
                    try:
                        as_of_utc = datetime.fromisoformat(as_of_str.replace("Z", "+00:00"))
                        if as_of_utc.tzinfo is None:
                            as_of_utc = as_of_utc.replace(tzinfo=timezone.utc)
                    except ValueError:
                        as_of_utc = datetime.now(timezone.utc)
                else:
                    as_of_utc = datetime.now(timezone.utc)

                hiring_mix = wh.get_hiring_mix_summary(as_of_utc)
                if entity_id:
                    hiring_mix = [h for h in hiring_mix if h["entity_id"] == entity_id]

                pricing_tiers = wh.query_pricing_as_of(as_of_utc, entity_id=entity_id)
                recent_diffs = wh.get_recent_signal_diffs(limit=50)
                if entity_id:
                    recent_diffs = [d for d in recent_diffs if d["entity_id"] == entity_id]

                release_velocity = wh.get_release_velocity_summary(as_of_utc, window_days=30)
                if entity_id:
                    release_velocity = [rv for rv in release_velocity if rv["entity_id"] == entity_id]

                self._send_json(
                    {
                        "as_of_utc": as_of_utc.isoformat(),
                        "entity_filter": entity_id,
                        "hiring_mix": hiring_mix,
                        "pricing_tiers": pricing_tiers,
                        "recent_diffs": recent_diffs,
                        "release_velocity": release_velocity,
                    }
                )
                return

        self.send_error(HTTPStatus.NOT_FOUND, "Not Found")


def run_dashboard_server(
    db_path: Path,
    host: str = "127.0.0.1",
    port: int = 8501,
) -> None:
    """Start local dashboard HTTP server."""
    handler_factory = lambda *args, **kwargs: DashboardRequestHandler(  # noqa: E731
        *args, db_path=db_path, **kwargs
    )
    server = ThreadingHTTPServer((host, port), handler_factory)
    print(f"\n[DASHBOARD] Serving Point-in-Time Dashboard at http://{host}:{port}/")
    print("[DASHBOARD] Press Ctrl+C to stop the server.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[DASHBOARD] Server stopped.")
    finally:
        server.server_close()
