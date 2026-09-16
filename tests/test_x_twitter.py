"""X/Twitter (twitterapi.io) fetcher: key-gating, transport resilience, crypto
cashtag mapping, and the summary + per-post formatting contract.

Mirrors test_stocktwits_resilience.py — the X fetcher must satisfy the same
"always return a str, never raise, <placeholder> on failure" contract, plus an
extra gate: it degrades to a placeholder when TWITTERAPI_IO_KEY is unset.
"""

from __future__ import annotations

import http.client
import json
from unittest.mock import patch
from urllib.error import HTTPError

import pytest

from tradingagents.dataflows import x_twitter


def _raise(exc):
    class _Resp:
        def __enter__(self_inner):
            return self_inner

        def __exit__(self_inner, *a):
            return False

        def read(self_inner):
            raise exc
    return _Resp()


def _resp(payload):
    class _Resp:
        def __enter__(self_inner):
            return self_inner

        def __exit__(self_inner, *a):
            return False

        def read(self_inner):
            return json.dumps(payload).encode()
    return _Resp()


@pytest.mark.unit
class TestXKeyGating:
    def test_missing_key_returns_placeholder(self, monkeypatch):
        monkeypatch.delenv("TWITTERAPI_IO_KEY", raising=False)
        out = x_twitter.fetch_x_posts("AAPL")
        assert out == "<X sentiment disabled: TWITTERAPI_IO_KEY not set>"


@pytest.mark.unit
class TestXResilience:
    @pytest.mark.parametrize(
        "exc",
        [
            http.client.IncompleteRead(b""),
            HTTPError("url", 503, "down", {}, None),
            TimeoutError("slow"),
        ],
    )
    def test_transport_errors_return_placeholder(self, exc, monkeypatch):
        monkeypatch.setenv("TWITTERAPI_IO_KEY", "test-key")
        with patch.object(x_twitter, "urlopen", return_value=_raise(exc)):
            out = x_twitter.fetch_x_posts("NVDA")
        assert out.startswith("<X unavailable")

    def test_empty_returns_placeholder(self, monkeypatch):
        monkeypatch.setenv("TWITTERAPI_IO_KEY", "test-key")
        with patch.object(x_twitter, "urlopen", return_value=_resp({"tweets": [], "has_next_page": False})):
            out = x_twitter.fetch_x_posts("AAPL")
        assert out == "<no X posts found for $AAPL>"


@pytest.mark.unit
class TestXCashtag:
    @pytest.mark.parametrize(
        ("ticker", "expected"),
        [
            ("AAPL", "$AAPL"),
            ("aapl", "$AAPL"),
            ("BTC-USD", "$BTC"),
            ("eth-usd", "$ETH"),
            ("BRK-B", "$BRK-B"),   # dashed class share: not crypto, untouched
        ],
    )
    def test_cashtag_mapping(self, ticker, expected):
        assert x_twitter._x_cashtag(ticker) == expected


@pytest.mark.unit
class TestXFormatting:
    def test_summary_and_engagement_sorted_posts(self, monkeypatch):
        monkeypatch.setenv("TWITTERAPI_IO_KEY", "test-key")
        payload = {
            "tweets": [
                {"text": "low engagement take", "createdAt": "2026-09-10",
                 "likeCount": 1, "retweetCount": 0,
                 "author": {"userName": "small", "isBlueVerified": False}},
                {"text": "big call $AAPL breaking out", "createdAt": "2026-09-11",
                 "likeCount": 100, "retweetCount": 50,
                 "author": {"userName": "whale", "isBlueVerified": True}},
            ],
            "has_next_page": False,
        }
        with patch.object(x_twitter, "urlopen", return_value=_resp(payload)):
            out = x_twitter.fetch_x_posts("AAPL", limit=30)
        lines = out.splitlines()
        assert lines[0].startswith("2 most-engaged posts")
        assert "101 total likes" in lines[0]      # 100 + 1
        assert "50 total reposts" in lines[0]
        # highest-engagement post first, blue-check marked, engagement shown
        assert "@whale✓" in lines[2]
        assert "♥100 ⟲50" in lines[2]

    def test_query_has_cashtag_filters_and_since_time(self, monkeypatch):
        monkeypatch.setenv("TWITTERAPI_IO_KEY", "test-key")
        seen = {}

        def fake_urlopen(req, timeout=None):
            seen["url"] = req.full_url
            raise TimeoutError("stop after capturing the URL")

        with patch.object(x_twitter, "urlopen", side_effect=fake_urlopen):
            x_twitter.fetch_x_posts("AAPL")
        assert "queryType=Latest" in seen["url"]
        assert "%24AAPL" in seen["url"]           # urlencoded "$AAPL"
        assert "since_time" in seen["url"]
