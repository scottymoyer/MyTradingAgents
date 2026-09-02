#!/usr/bin/env python3
"""Authenticated Reddit fetcher, used in place of the app's anonymous RSS path.

Why this exists
---------------
The installed app fetches Reddit via the public RSS search feed with no
credentials. From this host that endpoint is throttled hard: the first request
after an idle period returns 200, then everything 429s -- even with several
seconds of pacing. Since the sentiment analyst queries three subreddits per
ticker, at best one lands, so Reddit contributes almost nothing (StockTwits,
the analyst's other source, works fine).

Reddit's OAuth API lifts that limit substantially (roughly 100 requests/min for
a registered client versus the anonymous per-IP budget), and it returns real
``score`` / ``num_comments`` -- which the app's existing formatter already knows
how to render, so we only replace the *fetch*, never the formatting.

Integration
-----------
``install()`` monkeypatches ``tradingagents.dataflows.reddit._fetch_subreddit``.
That is the single internal seam the public ``fetch_reddit_posts()`` calls, so
patching it leaves the ticker handling, time window, formatting, and graceful
"no posts found" behaviour untouched. Nothing in site-packages is edited, so a
reinstall cannot silently reintroduce the anonymous path.

Credentials come from the environment (REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET),
never from source. When they are absent or auth fails, ``install()`` is a no-op
and the app keeps its original RSS behaviour.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
import urllib.parse
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
_SEARCH_URL = "https://oauth.reddit.com/r/{sub}/search?{qs}"

# Reddit requires a descriptive, identified User-Agent; generic tokens are blocked.
_UA = "tradingagents-oauth/0.1 (+https://github.com/TauricResearch/TradingAgents)"

# Refresh a little before actual expiry so a long run never races the boundary.
_EXPIRY_MARGIN_S = 120

_lock = threading.Lock()          # token fetch is shared across analysis threads
_token: str | None = None
_token_expires_at: float = 0.0


class RedditAuthError(RuntimeError):
    """Raised when credentials are present but Reddit refuses to issue a token."""


def credentials_present() -> bool:
    return bool(os.environ.get("REDDIT_CLIENT_ID") and os.environ.get("REDDIT_CLIENT_SECRET"))


def _get_token(timeout: float = 15.0) -> str:
    """Return a cached application-only OAuth token, fetching/refreshing as needed.

    Uses the ``client_credentials`` grant (application-only auth), which is
    sufficient for reading public subreddit search results and needs no user
    password.
    """
    global _token, _token_expires_at
    with _lock:
        if _token and time.time() < _token_expires_at:
            return _token

        cid = os.environ.get("REDDIT_CLIENT_ID", "")
        secret = os.environ.get("REDDIT_CLIENT_SECRET", "")
        if not (cid and secret):
            raise RedditAuthError("REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET not set")

        basic = base64.b64encode(f"{cid}:{secret}".encode()).decode()
        data = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
        req = Request(
            _TOKEN_URL,
            data=data,
            headers={
                "Authorization": f"Basic {basic}",
                "User-Agent": _UA,
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        try:
            with urlopen(req, timeout=timeout) as resp:
                payload = json.load(resp)
        except HTTPError as exc:
            # Do not include the response body verbatim: it can echo request
            # details. The status code is what distinguishes the failure modes
            # (401 = bad credentials, 429 = throttled).
            raise RedditAuthError(f"token request failed: HTTP {exc.code} {exc.reason}") from None
        except URLError as exc:
            raise RedditAuthError(f"token request failed: {exc.reason}") from None

        tok = payload.get("access_token")
        if not tok:
            raise RedditAuthError("token response contained no access_token")
        _token = tok
        _token_expires_at = time.time() + max(0, int(payload.get("expires_in", 3600))) - _EXPIRY_MARGIN_S
        logger.info("Reddit OAuth token acquired (expires in %ss)", payload.get("expires_in"))
        return _token


def _search_qs(ticker: str, limit: int) -> str:
    # Mirrors the app's own query shape so results stay comparable to the RSS path.
    return urllib.parse.urlencode({
        "q": ticker,
        "restrict_sr": "on",
        "sort": "new",
        "t": "week",          # last 7 days
        "limit": limit,
    })


def fetch_subreddit_oauth(ticker: str, sub: str, limit: int, timeout: float) -> list[dict]:
    """Fetch one subreddit's recent posts for ``ticker`` via the OAuth API.

    Returns the same list-of-dicts shape the app's RSS path produces, so the
    existing formatter renders it unchanged. Unlike RSS, ``score`` and
    ``num_comments`` are real values rather than None.

    Returns [] on any failure -- the caller already treats an empty list as
    "no posts found" and degrades gracefully.
    """
    try:
        token = _get_token(timeout=timeout)
    except RedditAuthError as exc:
        logger.warning("Reddit OAuth unavailable for r/%s: %s", sub, exc)
        return []

    url = _SEARCH_URL.format(sub=sub, qs=_search_qs(ticker, limit))
    req = Request(url, headers={"Authorization": f"Bearer {token}", "User-Agent": _UA})
    try:
        with urlopen(req, timeout=timeout) as resp:
            payload = json.load(resp)
    except HTTPError as exc:
        if exc.code == 401:
            # Token rejected mid-run: drop the cache so the next call re-auths.
            global _token, _token_expires_at
            with _lock:
                _token, _token_expires_at = None, 0.0
        logger.warning("Reddit OAuth search failed for r/%s · %s: HTTP %s", sub, ticker, exc.code)
        return []
    except (URLError, json.JSONDecodeError) as exc:
        logger.warning("Reddit OAuth search failed for r/%s · %s: %s", sub, ticker, type(exc).__name__)
        return []

    posts: list[dict] = []
    for child in (payload.get("data") or {}).get("children") or []:
        d = child.get("data") or {}
        if d.get("stickied"):
            continue
        posts.append({
            "title": d.get("title") or "",
            "score": d.get("score"),
            "num_comments": d.get("num_comments"),
            "created_utc": d.get("created_utc"),
            "selftext": (d.get("selftext") or "").strip(),
            "source": "oauth",
        })
    return posts[:limit]


def install() -> bool:
    """Patch the app's internal fetcher to use OAuth. Returns True if patched.

    No-op when credentials are absent, so behaviour falls back to the app's
    original anonymous RSS path.
    """
    if not credentials_present():
        logger.info("Reddit OAuth not installed: credentials not set; using app default (RSS)")
        return False

    from tradingagents.dataflows import reddit as _reddit

    if getattr(_reddit, "_oauth_installed", False):
        return True

    _reddit._fetch_subreddit_original = _reddit._fetch_subreddit
    _reddit._fetch_subreddit = fetch_subreddit_oauth
    _reddit._oauth_installed = True
    logger.info("Reddit OAuth fetcher installed (replacing anonymous RSS path)")
    return True


if __name__ == "__main__":
    # Standalone check: python reddit_oauth.py [TICKER]
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ticker = (sys.argv[1] if len(sys.argv) > 1 else "TSLA").upper()

    print(f"credentials present: {credentials_present()}")
    if not credentials_present():
        raise SystemExit("Set REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET first.")
    try:
        _get_token()
        print("token: acquired OK")
    except RedditAuthError as exc:
        raise SystemExit(f"token: FAILED -- {exc}") from exc

    total = 0
    for sub in ("wallstreetbets", "stocks", "investing"):
        posts = fetch_subreddit_oauth(ticker, sub, 5, 15.0)
        total += len(posts)
        print(f"  r/{sub:16} posts={len(posts)}")
        for p in posts[:2]:
            when = time.strftime("%Y-%m-%d", time.gmtime(p["created_utc"])) if p.get("created_utc") else "?"
            print(f"      [{when}] {p['title'][:66]}  ({p.get('score')}^, {p.get('num_comments')}c)")
    print(f"total posts: {total}")
