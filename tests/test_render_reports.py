"""Unit tests for the report viewer generator (repo-root ``render_reports.py``).

Covers the field-extraction helpers and ``collect()``: parsing the decision
fields out of report markdown, deduping to the latest report per ticker, and
sorting by the 5-tier rating. Uses temp report dirs; no network/LLM.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import render_reports as rr  # noqa: E402

pytestmark = pytest.mark.unit


def _make_report(reports_dir: Path, ticker: str, stamp: str, decision: str,
                 report_body: str = "# report\nbody") -> Path:
    d = reports_dir / f"{ticker}_{stamp}"
    (d / "5_portfolio").mkdir(parents=True)
    (d / "5_portfolio" / "decision.md").write_text(decision, encoding="utf-8")
    (d / "complete_report.md").write_text(report_body, encoding="utf-8")
    return d


# --------------------------------------------------------------------------- #
# field helpers
# --------------------------------------------------------------------------- #
def test_field_extracts_labeled_value():
    assert rr._field("**Rating**: Overweight", "Rating") == "Overweight"
    assert rr._field("**Time Horizon**: 3-6 months", "Time Horizon") == "3-6 months"


def test_field_missing_returns_none():
    assert rr._field("no such label here", "Rating") is None


def test_price_target_extraction():
    assert rr._price_target("**Price Target**: 305.0") == "$305.0"
    assert rr._price_target("**Price Target**: $84,000") == "$84,000"
    assert rr._price_target("no target mentioned") is None


# --------------------------------------------------------------------------- #
# collect()
# --------------------------------------------------------------------------- #
def test_collect_parses_decision_fields(tmp_path):
    _make_report(tmp_path, "DDOG", "20260101_120000",
                 "**Rating**: Overweight\n**Price Target**: 305.0\n"
                 "**Time Horizon**: 3-6 months\n**Executive Summary**: buy it")
    (row,) = rr.collect(tmp_path)
    assert row["ticker"] == "DDOG"
    assert row["rating"] == "Overweight"
    assert row["price_target"] == "$305.0"
    assert row["horizon"] == "3-6 months"
    assert row["summary"] == "buy it"


def test_collect_dedupes_to_latest_per_ticker(tmp_path):
    _make_report(tmp_path, "INTC", "20260101_090000", "**Rating**: Buy")
    _make_report(tmp_path, "INTC", "20260102_090000", "**Rating**: Sell")  # newer
    rows = rr.collect(tmp_path)
    assert len(rows) == 1
    assert rows[0]["rating"] == "Sell"  # the newer report wins


def test_collect_handles_legacy_rating_formats(tmp_path):
    # parse_rating must cope with the older prose format, not just "**Rating**:".
    _make_report(tmp_path, "AAOI", "20260101_120000",
                 "**Final Recommendation - SELL** ...")
    (row,) = rr.collect(tmp_path)
    assert row["rating"] == "Sell"


def test_collect_sorts_by_rating_bull_to_bear(tmp_path):
    _make_report(tmp_path, "AAA", "20260101_120000", "**Rating**: Sell")
    _make_report(tmp_path, "BBB", "20260101_120000", "**Rating**: Buy")
    _make_report(tmp_path, "CCC", "20260101_120000", "**Rating**: Hold")
    ratings = [r["rating"] for r in rr.collect(tmp_path)]
    assert ratings == ["Buy", "Hold", "Sell"]  # RATING_ORDER


def test_collect_missing_target_is_dash(tmp_path):
    _make_report(tmp_path, "XYZ", "20260101_120000", "**Rating**: Hold")
    (row,) = rr.collect(tmp_path)
    assert row["price_target"] == "—"
    assert row["horizon"] == "—"
