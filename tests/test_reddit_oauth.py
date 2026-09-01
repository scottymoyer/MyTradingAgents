"""Unit tests for the authenticated Reddit fetcher (repo-root ``reddit_oauth.py``).

Covers the credential gate, query building, and graceful degradation. The
network paths (token fetch, search) are not exercised live; the focus is that
the module is a safe no-op without credentials and returns [] on any failure so
callers keep working.
"""

import sys
import urllib.parse
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import reddit_oauth  # noqa: E402

pytestmark = pytest.mark.unit


def test_credentials_present_requires_both(monkeypatch):
    monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)
    monkeypatch.delenv("REDDIT_CLIENT_SECRET", raising=False)
    assert reddit_oauth.credentials_present() is False

    monkeypatch.setenv("REDDIT_CLIENT_ID", "abc")
    assert reddit_oauth.credentials_present() is False  # secret still missing

    monkeypatch.setenv("REDDIT_CLIENT_SECRET", "xyz")
    assert reddit_oauth.credentials_present() is True


def test_install_is_noop_without_credentials(monkeypatch):
    monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)
    monkeypatch.delenv("REDDIT_CLIENT_SECRET", raising=False)
    # Must return False and not touch tradingagents (the import only happens
    # after the credential check passes).
    assert reddit_oauth.install() is False


def test_search_qs_builds_expected_query():
    qs = reddit_oauth._search_qs("NVDA", 10)
    parsed = urllib.parse.parse_qs(qs)
    assert parsed["q"] == ["NVDA"]
    assert parsed["restrict_sr"] == ["on"]
    assert parsed["sort"] == ["new"]
    assert parsed["t"] == ["week"]      # last-7-days window
    assert parsed["limit"] == ["10"]


def test_fetch_returns_empty_without_credentials(monkeypatch):
    monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)
    monkeypatch.delenv("REDDIT_CLIENT_SECRET", raising=False)
    # No token can be obtained -> graceful empty list, never an exception.
    assert reddit_oauth.fetch_subreddit_oauth("NVDA", "stocks", 5, 5.0) == []
