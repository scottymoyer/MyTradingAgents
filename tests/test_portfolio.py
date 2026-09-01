"""Unit tests for the portfolio loader/validator (repo-root ``portfolio.py``).

Covers the happy path plus every validation branch: the loader must reject bad
or missing data loudly (naming file, row, and field) rather than skipping rows.
Pure logic — no network, no LLM, no tradingagents import.
"""

import datetime as dt
import sys
from pathlib import Path

import pytest

# portfolio.py lives at the repo root, not inside the tradingagents package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import portfolio  # noqa: E402

pytestmark = pytest.mark.unit


def _write(tmp_path: Path, name: str, text: str) -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


# --------------------------------------------------------------------------- #
# happy path
# --------------------------------------------------------------------------- #
def test_valid_holdings_loads(tmp_path):
    f = _write(tmp_path, "holdings.yaml", """
holdings:
  - ticker: ddog
    account: etrade_taxable
    shares: 10
    cost_basis: 100.5
    acquire_date: 2024-01-01
""")
    (h,) = portfolio.load_holdings(f)
    assert h.ticker == "DDOG"                 # uppercased
    assert h.account == "etrade_taxable"
    assert h.shares == 10.0 and isinstance(h.shares, float)
    assert h.cost_basis == 100.5
    assert h.acquire_date == dt.date(2024, 1, 1)


def test_valid_watchlist_loads(tmp_path):
    f = _write(tmp_path, "watchlist.yaml", """
watchlist:
  - ticker: copx
    asset_type: etf
    thesis: "copper"
    tag: metals
""")
    (w,) = portfolio.load_watchlist(f)
    assert w.ticker == "COPX"
    assert w.asset_type == "etf"
    assert w.thesis == "copper"
    assert w.tag == "metals"


def test_acquire_date_accepts_quoted_string(tmp_path):
    # A quoted YYYY-MM-DD stays a str in YAML; loader must still parse it.
    f = _write(tmp_path, "holdings.yaml", """
holdings:
  - ticker: X
    account: robinhood_active
    shares: 1
    cost_basis: 0
    acquire_date: "2023-06-15"
""")
    (h,) = portfolio.load_holdings(f)
    assert h.acquire_date == dt.date(2023, 6, 15)


@pytest.mark.parametrize("declared,expected", [
    ("stock", "stock"),
    ("etf", "stock"),      # ETFs run through the stock pipeline
    ("crypto", "crypto"),  # crypto maps straight through
])
def test_engine_asset_type_mapping(declared, expected):
    item = portfolio.WatchlistItem(ticker="X", asset_type=declared, thesis="t", tag="g")
    assert item.engine_asset_type() == expected


def test_empty_list_is_allowed(tmp_path):
    f = _write(tmp_path, "watchlist.yaml", "watchlist: []\n")
    assert portfolio.load_watchlist(f) == []


# --------------------------------------------------------------------------- #
# holdings validation branches
# --------------------------------------------------------------------------- #
_HOLDINGS_BAD = {
    "bad_account": ("""
holdings:
  - {ticker: X, account: schwab, shares: 1, cost_basis: 1, acquire_date: 2024-01-01}
""", "account"),
    "missing_field": ("""
holdings:
  - {ticker: X, account: etrade_taxable, shares: 1, acquire_date: 2024-01-01}
""", "cost_basis"),
    "non_numeric_shares": ("""
holdings:
  - {ticker: X, account: etrade_taxable, shares: "ten", cost_basis: 1, acquire_date: 2024-01-01}
""", "shares"),
    "negative_number": ("""
holdings:
  - {ticker: X, account: etrade_taxable, shares: -5, cost_basis: 1, acquire_date: 2024-01-01}
""", "shares"),
    "bool_rejected": ("""
holdings:
  - {ticker: X, account: etrade_taxable, shares: true, cost_basis: 1, acquire_date: 2024-01-01}
""", "shares"),
    "bad_date": ("""
holdings:
  - {ticker: X, account: etrade_taxable, shares: 1, cost_basis: 1, acquire_date: "01/15/2024"}
""", "acquire_date"),
}


@pytest.mark.parametrize("name", list(_HOLDINGS_BAD))
def test_holdings_bad_rows_raise(tmp_path, name):
    text, field = _HOLDINGS_BAD[name]
    f = _write(tmp_path, "holdings.yaml", text)
    with pytest.raises(portfolio.PortfolioError) as exc:
        portfolio.load_holdings(f)
    msg = str(exc.value)
    assert "holdings.yaml" in msg and "row 1" in msg and field in msg


def test_holdings_duplicate_same_account(tmp_path):
    f = _write(tmp_path, "holdings.yaml", """
holdings:
  - {ticker: X, account: etrade_taxable, shares: 1, cost_basis: 1, acquire_date: 2024-01-01}
  - {ticker: x, account: etrade_taxable, shares: 2, cost_basis: 2, acquire_date: 2024-01-02}
""")
    with pytest.raises(portfolio.PortfolioError, match="duplicate"):
        portfolio.load_holdings(f)


def test_holdings_same_ticker_different_account_ok(tmp_path):
    # Same ticker in two different accounts is allowed (keyed on ticker+account).
    f = _write(tmp_path, "holdings.yaml", """
holdings:
  - {ticker: X, account: etrade_taxable, shares: 1, cost_basis: 1, acquire_date: 2024-01-01}
  - {ticker: X, account: robinhood_active, shares: 2, cost_basis: 2, acquire_date: 2024-01-02}
""")
    assert len(portfolio.load_holdings(f)) == 2


# --------------------------------------------------------------------------- #
# watchlist validation branches
# --------------------------------------------------------------------------- #
_WATCHLIST_BAD = {
    "bad_asset_type": ("""
watchlist:
  - {ticker: X, asset_type: mutual_fund, thesis: t, tag: g}
""", "asset_type"),
    "missing_thesis": ("""
watchlist:
  - {ticker: X, asset_type: stock, tag: g}
""", "thesis"),
    "blank_tag": ("""
watchlist:
  - {ticker: X, asset_type: stock, thesis: t, tag: "   "}
""", "tag"),
}


@pytest.mark.parametrize("name", list(_WATCHLIST_BAD))
def test_watchlist_bad_rows_raise(tmp_path, name):
    text, field = _WATCHLIST_BAD[name]
    f = _write(tmp_path, "watchlist.yaml", text)
    with pytest.raises(portfolio.PortfolioError) as exc:
        portfolio.load_watchlist(f)
    assert field in str(exc.value)


def test_watchlist_duplicate(tmp_path):
    f = _write(tmp_path, "watchlist.yaml", """
watchlist:
  - {ticker: X, asset_type: stock, thesis: t, tag: g}
  - {ticker: x, asset_type: etf, thesis: t2, tag: g2}
""")
    with pytest.raises(portfolio.PortfolioError, match="duplicate"):
        portfolio.load_watchlist(f)


def test_bad_asset_type_lists_valid_choices(tmp_path):
    f = _write(tmp_path, "watchlist.yaml", """
watchlist:
  - {ticker: X, asset_type: mutual_fund, thesis: t, tag: g}
""")
    with pytest.raises(portfolio.PortfolioError) as exc:
        portfolio.load_watchlist(f)
    assert "stock" in str(exc.value) and "crypto" in str(exc.value)


# --------------------------------------------------------------------------- #
# file-level / structural errors
# --------------------------------------------------------------------------- #
def test_missing_file(tmp_path):
    with pytest.raises(portfolio.PortfolioError, match="not found"):
        portfolio.load_holdings(tmp_path / "nope.yaml")


def test_empty_file(tmp_path):
    f = _write(tmp_path, "holdings.yaml", "")
    with pytest.raises(portfolio.PortfolioError, match="empty"):
        portfolio.load_holdings(f)


def test_missing_top_key(tmp_path):
    f = _write(tmp_path, "holdings.yaml", "something_else: []\n")
    with pytest.raises(portfolio.PortfolioError, match="holdings"):
        portfolio.load_holdings(f)


def test_top_key_not_a_list(tmp_path):
    f = _write(tmp_path, "watchlist.yaml", "watchlist: not-a-list\n")
    with pytest.raises(portfolio.PortfolioError, match="must be a list"):
        portfolio.load_watchlist(f)


def test_non_mapping_row(tmp_path):
    f = _write(tmp_path, "watchlist.yaml", """
watchlist:
  - just a string
""")
    with pytest.raises(portfolio.PortfolioError, match="mapping"):
        portfolio.load_watchlist(f)


def test_invalid_yaml(tmp_path):
    f = _write(tmp_path, "holdings.yaml", "holdings: [unbalanced\n")
    with pytest.raises(portfolio.PortfolioError, match="not valid YAML"):
        portfolio.load_holdings(f)


def test_error_message_names_file_row_and_field(tmp_path):
    # The second row is the bad one — error must point at row 2 and the field.
    f = _write(tmp_path, "watchlist.yaml", """
watchlist:
  - {ticker: A, asset_type: stock, thesis: t, tag: g}
  - {ticker: B, asset_type: nope, thesis: t, tag: g}
""")
    with pytest.raises(portfolio.PortfolioError) as exc:
        portfolio.load_watchlist(f)
    msg = str(exc.value)
    assert "watchlist.yaml" in msg and "row 2" in msg and "asset_type" in msg


def test_load_all(tmp_path):
    h = _write(tmp_path, "holdings.yaml", """
holdings:
  - {ticker: X, account: etrade_taxable, shares: 1, cost_basis: 1, acquire_date: 2024-01-01}
""")
    w = _write(tmp_path, "watchlist.yaml", """
watchlist:
  - {ticker: Y, asset_type: crypto, thesis: t, tag: g}
""")
    holdings, watch = portfolio.load_all(h, w)
    assert holdings[0].ticker == "X"
    assert watch[0].ticker == "Y" and watch[0].engine_asset_type() == "crypto"
