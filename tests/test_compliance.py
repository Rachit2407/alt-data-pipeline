"""Tests for robots.txt enforcement, rate limiting, and auth-wall guardrails."""

from __future__ import annotations

import httpx

from alt_data.compliance import PoliteHttpClient


def _mock_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_robots_disallow_blocks_target_request() -> None:
    requested_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private-pricing\n")
        return httpx.Response(200, text="should not reach")

    with PoliteHttpClient(client=_mock_client(handler), min_delay_sec=0.0, max_jitter_sec=0.0) as polite:
        res = polite.get("https://example.com/private-pricing")

    assert res.robots_allowed is False
    assert res.is_success is False
    assert requested_urls == ["https://example.com/robots.txt"]


def test_robots_403_fails_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(403, text="Forbidden")
        return httpx.Response(200, text="ok")

    with PoliteHttpClient(client=_mock_client(handler), min_delay_sec=0.0, max_jitter_sec=0.0) as polite:
        res = polite.get("https://example.com/pricing")

    assert res.robots_allowed is False
    assert res.status_code == 403


def test_robots_404_allows_public_page() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404, text="Not Found")
        return httpx.Response(200, text="<html><body>Pricing</body></html>")

    with PoliteHttpClient(client=_mock_client(handler), min_delay_sec=0.0, max_jitter_sec=0.0) as polite:
        res = polite.get("https://example.com/pricing")

    assert res.robots_allowed is True
    assert res.is_success is True
    assert res.status_code == 200


def test_linkedin_domain_is_blocked_without_network_call() -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200)

    with PoliteHttpClient(client=_mock_client(handler), min_delay_sec=0.0, max_jitter_sec=0.0) as polite:
        res = polite.get("https://www.linkedin.com/company/supabase/jobs")

    assert res.robots_allowed is False
    assert called is False
    assert "compliance policy" in (res.error_msg or "")


def test_auth_redirect_and_challenge_halt_cleanly() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if request.url.path == "/pricing":
            return httpx.Response(302, headers={"Location": "https://example.com/login"})
        if request.url.path == "/login":
            return httpx.Response(200, text="Sign in")
        return httpx.Response(200, headers={"cf-mitigated": "challenge"}, text="Challenge")

    with PoliteHttpClient(
        client=httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True),
        min_delay_sec=0.0,
        max_jitter_sec=0.0,
    ) as polite:
        login_res = polite.get("https://example.com/pricing")
        chal_res = polite.get("https://example.com/other")

    assert login_res.is_success is False
    assert "authentication path" in (login_res.error_msg or "")
    assert chal_res.is_success is False
    assert "challenge" in (chal_res.error_msg or "").lower()


def test_rate_limiter_respects_crawl_delay() -> None:
    sleeps: list[float] = []
    now = [100.0]

    def fake_monotonic() -> float:
        return now[0]

    def fake_sleep(secs: float) -> None:
        sleeps.append(secs)
        now[0] += secs

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nCrawl-delay: 3\nAllow: /\n")
        return httpx.Response(200, text="ok")

    with PoliteHttpClient(
        client=_mock_client(handler),
        min_delay_sec=1.0,
        max_jitter_sec=0.0,
        sleep_fn=fake_sleep,
        monotonic_fn=fake_monotonic,
    ) as polite:
        polite.get("https://example.com/a")
        now[0] += 0.5
        polite.get("https://example.com/b")

    assert len(sleeps) == 1
    assert sleeps[0] == 2.5
