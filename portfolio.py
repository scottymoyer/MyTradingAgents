#!/usr/bin/env python3
"""Loader and validator for the portfolio input files.

Reads ``holdings.yaml`` and ``watchlist.yaml`` and returns simple typed
structures. Validation is strict and loud: a bad or missing field raises
``PortfolioError`` naming the file, the row, and the field. Bad rows are never
silently skipped.

Scope note: this module stores only what is written in the files. It does not
fetch or store prices, market values, or any other live market data — those are
computed at runtime by the analysis engine.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

REPO_DIR = Path(__file__).resolve().parent

HOLDINGS_FILE = REPO_DIR / "holdings.yaml"
WATCHLIST_FILE = REPO_DIR / "watchlist.yaml"

# Canonical account keys. Anything else is a validation error.
VALID_ACCOUNTS = ("etrade_taxable", "fidelity_retirement", "robinhood_active")

# Asset types accepted in watchlist.yaml. Note the analysis engine itself only
# distinguishes "stock" vs "crypto", so "etf" is analyzed via the stock
# pipeline — see engine_asset_type().
VALID_ASSET_TYPES = ("stock", "etf")


class PortfolioError(ValueError):
    """Raised when an input file is missing, malformed, or fails validation."""


@dataclass(frozen=True)
class Holding:
    ticker: str
    account: str
    shares: float
    cost_basis: float          # average cost per share
    acquire_date: _dt.date


@dataclass(frozen=True)
class WatchlistItem:
    ticker: str
    asset_type: str            # "stock" | "etf"
    thesis: str
    tag: str

    def engine_asset_type(self) -> str:
        """Map to what the analysis engine understands.

        The engine's ``propagate(..., asset_type=)`` accepts only "stock" or
        "crypto"; it has no ETF concept, so ETFs run through the stock pipeline.
        """
        return "stock"


def _fail(path: Path, row_num: int | None, field: str | None, problem: str) -> None:
    where = f"{path.name}"
    if row_num is not None:
        where += f", row {row_num}"
    if field is not None:
        where += f", field '{field}'"
    raise PortfolioError(f"{where}: {problem}")


def _load_yaml(path: Path, top_key: str) -> list[Any]:
    """Read ``path`` and return the list under ``top_key``, validating shape."""
    if not path.exists():
        raise PortfolioError(f"{path.name}: file not found at {path}")

    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise PortfolioError(f"{path.name}: not valid YAML ({exc})") from exc

    if data is None:
        raise PortfolioError(f"{path.name}: file is empty; expected a top-level '{top_key}:' list")
    if not isinstance(data, dict):
        raise PortfolioError(
            f"{path.name}: expected a mapping at the top level with a '{top_key}:' key, "
            f"got {type(data).__name__}"
        )
    if top_key not in data:
        raise PortfolioError(f"{path.name}: missing required top-level key '{top_key}:'")

    rows = data[top_key]
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise PortfolioError(
            f"{path.name}: '{top_key}' must be a list, got {type(rows).__name__}"
        )
    return rows


def _require_mapping(path: Path, row_num: int, row: Any) -> dict:
    if not isinstance(row, dict):
        _fail(path, row_num, None, f"expected a mapping of fields, got {type(row).__name__}")
    return row


def _get_str(path: Path, row_num: int, row: dict, field: str) -> str:
    if field not in row:
        _fail(path, row_num, field, "missing required field")
    value = row[field]
    if value is None:
        _fail(path, row_num, field, "is empty; expected a string")
    if not isinstance(value, str):
        _fail(path, row_num, field, f"expected a string, got {type(value).__name__} ({value!r})")
    if not value.strip():
        _fail(path, row_num, field, "is blank; expected a non-empty string")
    return value.strip()


def _get_number(path: Path, row_num: int, row: dict, field: str) -> float:
    if field not in row:
        _fail(path, row_num, field, "missing required field")
    value = row[field]
    if value is None:
        _fail(path, row_num, field, "is empty; expected a number")
    # bool is a subclass of int — reject it explicitly so `shares: true` fails.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(path, row_num, field, f"expected a number, got {type(value).__name__} ({value!r})")
    if value < 0:
        _fail(path, row_num, field, f"expected a non-negative number, got {value!r}")
    return float(value)


def _get_date(path: Path, row_num: int, row: dict, field: str) -> _dt.date:
    if field not in row:
        _fail(path, row_num, field, "missing required field")
    value = row[field]
    if value is None:
        _fail(path, row_num, field, "is empty; expected a date (YYYY-MM-DD)")
    # PyYAML parses an unquoted YYYY-MM-DD into a date; a quoted one stays a str.
    if isinstance(value, _dt.datetime):
        return value.date()
    if isinstance(value, _dt.date):
        return value
    if isinstance(value, str):
        try:
            return _dt.datetime.strptime(value.strip(), "%Y-%m-%d").date()
        except ValueError:
            _fail(path, row_num, field, f"expected a date as YYYY-MM-DD, got {value!r}")
    _fail(path, row_num, field, f"expected a date (YYYY-MM-DD), got {type(value).__name__} ({value!r})")


def _get_choice(path: Path, row_num: int, row: dict, field: str, choices: tuple[str, ...]) -> str:
    value = _get_str(path, row_num, row, field)
    if value not in choices:
        _fail(
            path, row_num, field,
            f"{value!r} is not a valid value; expected one of: {', '.join(choices)}",
        )
    return value


def load_holdings(path: Path | str = HOLDINGS_FILE) -> list[Holding]:
    """Load and validate holdings.yaml. Raises PortfolioError on any bad row."""
    path = Path(path)
    rows = _load_yaml(path, "holdings")

    holdings: list[Holding] = []
    seen: dict[tuple[str, str], int] = {}
    for i, raw in enumerate(rows, start=1):
        row = _require_mapping(path, i, raw)
        ticker = _get_str(path, i, row, "ticker").upper()
        account = _get_choice(path, i, row, "account", VALID_ACCOUNTS)

        key = (ticker, account)
        if key in seen:
            _fail(path, i, "ticker",
                  f"duplicate entry for {ticker} in account '{account}' (first seen at row {seen[key]})")
        seen[key] = i

        holdings.append(Holding(
            ticker=ticker,
            account=account,
            shares=_get_number(path, i, row, "shares"),
            cost_basis=_get_number(path, i, row, "cost_basis"),
            acquire_date=_get_date(path, i, row, "acquire_date"),
        ))
    return holdings


def load_watchlist(path: Path | str = WATCHLIST_FILE) -> list[WatchlistItem]:
    """Load and validate watchlist.yaml. Raises PortfolioError on any bad row."""
    path = Path(path)
    rows = _load_yaml(path, "watchlist")

    items: list[WatchlistItem] = []
    seen: dict[str, int] = {}
    for i, raw in enumerate(rows, start=1):
        row = _require_mapping(path, i, raw)
        ticker = _get_str(path, i, row, "ticker").upper()

        if ticker in seen:
            _fail(path, i, "ticker", f"duplicate entry for {ticker} (first seen at row {seen[ticker]})")
        seen[ticker] = i

        items.append(WatchlistItem(
            ticker=ticker,
            asset_type=_get_choice(path, i, row, "asset_type", VALID_ASSET_TYPES),
            thesis=_get_str(path, i, row, "thesis"),
            tag=_get_str(path, i, row, "tag"),
        ))
    return items


def load_all(
    holdings_path: Path | str = HOLDINGS_FILE,
    watchlist_path: Path | str = WATCHLIST_FILE,
) -> tuple[list[Holding], list[WatchlistItem]]:
    """Load and validate both files."""
    return load_holdings(holdings_path), load_watchlist(watchlist_path)


if __name__ == "__main__":
    # Cheap self-check: python portfolio.py
    h, w = load_all()
    print(f"holdings.yaml : {len(h)} row(s) OK")
    for item in h:
        print(f"  {item.ticker:<8} {item.account:<22} shares={item.shares:<10g} "
              f"cost_basis={item.cost_basis:<10g} acquired={item.acquire_date}")
    print(f"watchlist.yaml: {len(w)} row(s) OK")
    for item in w:
        print(f"  {item.ticker:<8} {item.asset_type:<6} tag={item.tag:<18} thesis={item.thesis!r}")
