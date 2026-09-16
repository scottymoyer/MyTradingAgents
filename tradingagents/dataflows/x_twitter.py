"""X/Twitter cashtag search fetcher (via twitterapi.io).

X's first-party API dropped its free tier, so recent-tweet retrieval goes
through twitterapi.io, a third-party aggregator that exposes an advanced-search
endpoint keyed by a simple ``x-api-key`` header:

    https://api.twitterapi.io/twitter/tweet/advanced_search

Unlike StockTwits, X posts carry **no** user-labeled Bullish/Bearish tag, so this
fetcher returns raw posts plus engagement (likes/reposts) and lets the sentiment
analyst infer direction. It mirrors ``stocktwits.py``'s contract exactly: a short
timeout, graceful degradation on any HTTP/parse failure, and a plaintext ``str``
return so the calling agent has a uniform interface. The API key is read from the
environment (``TWITTERAPI_IO_KEY``); when it is absent the fetcher returns a clear
placeholder rather than failing the run.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import time
import urllib.parse
from urllib.request import Request, urlopen

from .symbol_utils import crypto_base

logger = logging.getLogger(__name__)

_API = "https://api.twitterapi.io/twitter/tweet/advanced_search"
_UA = "tradingagents/0.2 (+https://github.com/TauricResearch/TradingAgents)"
_KEY_ENV = "TWITTERAPI_IO_KEY"
_LOOKBACK_DAYS = 7          # matches the sentiment analyst's 7-day window
_PAGE_SIZE = 20             # twitterapi.io returns up to ~20 tweets per page
_MAX_PAGES = 5              # hard cap so a bad cursor loop can't run away / overspend
_BODY_CAP = 280


def _x_cashtag(ticker: str) -> str:
    """Map a ticker to an X cashtag. Crypto pairs (BTC-USD) collapse to their
    base (``$BTC``); everything else upper-cases to ``$AAPL``."""
    base = crypto_base(ticker)
    return f"${base}" if base else f"${ticker.strip().upper()}"


def fetch_x_posts(ticker: str, limit: int = 30, timeout: float = 10.0) -> str:
    """Fetch recent X posts for ``ticker`` (by cashtag) and return them as a
    formatted plaintext block ready for prompt injection.

    Returns a placeholder string when the API key is unset, the endpoint is
    unreachable, or no posts match — the caller never has to special-case None
    or exceptions.
    """
    key = os.environ.get(_KEY_ENV)
    if not key:
        return f"<X sentiment disabled: {_KEY_ENV} not set>"

    cashtag = _x_cashtag(ticker)
    since_time = int(time.time()) - _LOOKBACK_DAYS * 86400
    query = f"{cashtag} lang:en -filter:replies -filter:retweets since_time:{since_time}"

    tweets: list[dict] = []
    cursor = ""
    try:
        for _ in range(_MAX_PAGES):
            params = {"query": query, "queryType": "Latest"}
            if cursor:
                params["cursor"] = cursor
            url = f"{_API}?{urllib.parse.urlencode(params)}"
            req = Request(url, headers={"x-api-key": key, "User-Agent": _UA, "Accept": "application/json"})
            with urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())

            page = data.get("tweets", []) if isinstance(data, dict) else []
            tweets.extend(page)
            if len(tweets) >= limit or not data.get("has_next_page") or not page:
                break
            cursor = data.get("next_cursor") or ""
            if not cursor:
                break
    except (OSError, http.client.HTTPException, json.JSONDecodeError) as exc:
        # OSError covers URLError/HTTPError/TimeoutError/connection resets;
        # HTTPException covers chunked-transfer errors (IncompleteRead/BadStatusLine).
        logger.warning("X (twitterapi.io) fetch failed for %s: %s", ticker, exc)
        # Return what we have if a later page failed mid-collection; else placeholder.
        if not tweets:
            return f"<X unavailable: {type(exc).__name__}>"

    if not tweets:
        return f"<no X posts found for {cashtag}>"

    def _eng(t: dict) -> int:
        return int(t.get("likeCount") or 0) + int(t.get("retweetCount") or 0)

    tweets.sort(key=_eng, reverse=True)
    tweets = tweets[:limit]

    lines = []
    total_likes = total_reposts = 0
    for t in tweets:
        likes = int(t.get("likeCount") or 0)
        reposts = int(t.get("retweetCount") or 0)
        total_likes += likes
        total_reposts += reposts
        author = t.get("author") or {}
        user = author.get("userName", "?")
        verified = "✓" if author.get("isBlueVerified") else ""
        created = t.get("createdAt", "")
        body = (t.get("text") or "").replace("\n", " ").strip()
        if len(body) > _BODY_CAP:
            body = body[:_BODY_CAP] + "…"
        lines.append(f"[{created} · @{user}{verified} · ♥{likes} ⟲{reposts}] {body}")

    top_voices = ", ".join(f"@{(t.get('author') or {}).get('userName', '?')}" for t in tweets[:3])
    summary = (
        f"{len(tweets)} most-engaged posts · "
        f"{total_likes} total likes · {total_reposts} total reposts · "
        f"top voices: {top_voices}"
    )
    return summary + "\n\n" + "\n".join(lines)
