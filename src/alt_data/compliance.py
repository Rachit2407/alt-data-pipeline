"""Compliance controls: robots.txt enforcement, polite rate limiting, and auth-wall guardrails."""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

DEFAULT_USER_AGENT = (
    "AltDataPipeline/0.1 (+https://github.com/Rachit2407; public-research-pit-collector)"
)

BLOCKED_DOMAINS = (
    "linkedin.com",
    "lnkd.in",
)

AUTH_PATH_HINTS = (
    "/login",
    "/signin",
    "/sign-in",
    "/auth/",
    "/oauth",
    "/checkpoint",
)


@dataclass(frozen=True)
class RobotsDecision:
    """Result of checking robots.txt for a target URL."""

    allowed: bool
    crawl_delay: Optional[float]
    robots_url: str
    status_code: Optional[int]
    reason: str


@dataclass(frozen=True)
class FetchResult:
    """Outcome of a compliance-checked HTTP fetch."""

    url: str
    final_url: str
    robots_allowed: bool
    status_code: Optional[int]
    body_bytes: Optional[bytes]
    content_type: Optional[str]
    etag: Optional[str]
    last_modified: Optional[str]
    error_msg: Optional[str]

    @property
    def is_success(self) -> bool:
        return self.robots_allowed and self.status_code == 200 and self.body_bytes is not None


def _is_blocked_domain(netloc: str) -> bool:
    host = netloc.split(":")[0].lower()
    return any(host == blocked or host.endswith(f".{blocked}") for blocked in BLOCKED_DOMAINS)


def _looks_like_auth_or_captcha_gate(response: httpx.Response) -> Optional[str]:
    """Detect login walls, rate-limit blocks, or anti-bot challenges without bypassing them."""
    if response.status_code in (401, 403, 429):
        return f"HTTP {response.status_code} access/rate restriction; halting cleanly."
    final_path = urlparse(str(response.url)).path.lower()
    if any(hint in final_path for hint in AUTH_PATH_HINTS):
        return f"Redirected to authentication path ({final_path}); login bypass prohibited."
    if response.headers.get("cf-mitigated", "").lower() == "challenge":
        return "Anti-bot challenge header detected; challenge bypass prohibited."
    return None


class RobotsComplianceChecker:
    """Fetches, caches, and evaluates robots.txt per (scheme, netloc)."""

    def __init__(
        self,
        client: httpx.Client,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        self._client = client
        self._user_agent = user_agent
        self._cache: dict[tuple[str, str], tuple[Optional[RobotFileParser], Optional[int], str]] = {}

    def check(self, target_url: str) -> RobotsDecision:
        parsed = urlparse(target_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return RobotsDecision(
                allowed=False,
                crawl_delay=None,
                robots_url="",
                status_code=None,
                reason="Invalid URL scheme or missing host.",
            )

        if _is_blocked_domain(parsed.netloc):
            return RobotsDecision(
                allowed=False,
                crawl_delay=None,
                robots_url="",
                status_code=None,
                reason=f"Domain {parsed.netloc} is explicitly disallowed by compliance policy.",
            )

        origin_key = (parsed.scheme, parsed.netloc)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"

        if origin_key not in self._cache:
            self._cache[origin_key] = self._fetch_and_parse_robots(robots_url)

        parser, status_code, fetch_note = self._cache[origin_key]
        if parser is None:
            # 404 means no robots.txt restrictions per RFC 9309; other failures fail closed.
            if status_code == 404:
                return RobotsDecision(
                    allowed=True,
                    crawl_delay=None,
                    robots_url=robots_url,
                    status_code=status_code,
                    reason="No robots.txt found (404); allowed by RFC 9309.",
                )
            return RobotsDecision(
                allowed=False,
                crawl_delay=None,
                robots_url=robots_url,
                status_code=status_code,
                reason=fetch_note,
            )

        allowed = parser.can_fetch(self._user_agent, target_url)
        delay_val = parser.crawl_delay(self._user_agent)
        crawl_delay = float(delay_val) if delay_val is not None else None
        reason = "Allowed by robots.txt." if allowed else "Disallowed by robots.txt."
        return RobotsDecision(
            allowed=allowed,
            crawl_delay=crawl_delay,
            robots_url=robots_url,
            status_code=status_code,
            reason=reason,
        )

    def _fetch_and_parse_robots(
        self, robots_url: str
    ) -> tuple[Optional[RobotFileParser], Optional[int], str]:
        try:
            resp = self._client.get(robots_url, follow_redirects=True)
        except httpx.HTTPError as exc:
            return None, None, f"Failed to fetch robots.txt ({exc}); failing closed."

        if resp.status_code == 404:
            return None, 404, "robots.txt returned 404."
        if resp.status_code != 200:
            return (
                None,
                resp.status_code,
                f"robots.txt returned HTTP {resp.status_code}; failing closed.",
            )

        parser = RobotFileParser()
        parser.set_url(robots_url)
        parser.parse(resp.text.splitlines())
        return parser, resp.status_code, "Parsed robots.txt."


class PoliteHttpClient:
    """Rate-limited, robots-enforcing synchronous HTTP client."""

    def __init__(
        self,
        client: Optional[httpx.Client] = None,
        user_agent: str = DEFAULT_USER_AGENT,
        min_delay_sec: float = 1.2,
        max_jitter_sec: float = 0.4,
        sleep_fn: Callable[[float], None] = time.sleep,
        monotonic_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(20.0),
            headers={"User-Agent": user_agent, "Accept-Language": "en-US,en;q=0.9"},
            follow_redirects=True,
        )
        self._user_agent = user_agent
        self._min_delay_sec = min_delay_sec
        self._max_jitter_sec = max_jitter_sec
        self._sleep = sleep_fn
        self._monotonic = monotonic_fn
        self._last_request_by_host: dict[str, float] = {}
        self.robots = RobotsComplianceChecker(self._client, user_agent=user_agent)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "PoliteHttpClient":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def get(
        self,
        url: str,
        etag: Optional[str] = None,
        last_modified: Optional[str] = None,
    ) -> FetchResult:
        decision = self.robots.check(url)
        if not decision.allowed:
            return FetchResult(
                url=url,
                final_url=url,
                robots_allowed=False,
                status_code=decision.status_code,
                body_bytes=None,
                content_type=None,
                etag=None,
                last_modified=None,
                error_msg=decision.reason,
            )

        netloc = urlparse(url).netloc.lower()
        self._enforce_rate_limit(netloc, decision.crawl_delay)

        headers: dict[str, str] = {"User-Agent": self._user_agent}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        try:
            response = self._client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            return FetchResult(
                url=url,
                final_url=url,
                robots_allowed=True,
                status_code=None,
                body_bytes=None,
                content_type=None,
                etag=None,
                last_modified=None,
                error_msg=f"HTTP request failed: {exc}",
            )
        finally:
            self._last_request_by_host[netloc] = self._monotonic()

        gate_reason = _looks_like_auth_or_captcha_gate(response)
        if gate_reason:
            return FetchResult(
                url=url,
                final_url=str(response.url),
                robots_allowed=True,
                status_code=response.status_code,
                body_bytes=None,
                content_type=response.headers.get("content-type"),
                etag=response.headers.get("etag"),
                last_modified=response.headers.get("last-modified"),
                error_msg=gate_reason,
            )

        return FetchResult(
            url=url,
            final_url=str(response.url),
            robots_allowed=True,
            status_code=response.status_code,
            body_bytes=response.content if response.status_code == 200 else None,
            content_type=response.headers.get("content-type"),
            etag=response.headers.get("etag"),
            last_modified=response.headers.get("last-modified"),
            error_msg=None if response.status_code in (200, 304) else f"HTTP {response.status_code}",
        )

    def _enforce_rate_limit(self, netloc: str, crawl_delay: Optional[float]) -> None:
        required_delay = max(self._min_delay_sec, crawl_delay or 0.0)
        last_ts = self._last_request_by_host.get(netloc)
        if last_ts is None:
            return
        elapsed = self._monotonic() - last_ts
        remaining = required_delay - elapsed
        if remaining > 0:
            jitter = random.uniform(0.0, self._max_jitter_sec) if self._max_jitter_sec > 0 else 0.0
            self._sleep(remaining + jitter)
