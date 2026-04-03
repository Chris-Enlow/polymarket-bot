"""
Tests for pnl_tracker.py — PnL calculation logic.
No network calls; pure arithmetic.
"""
import uuid
import pytest

from app.db_models import SimulatedTrade
from app.pnl_tracker import MarketResolution, _compute_pnl


def _make_trade(token_side: str = "YES", price: float = 0.50, size: float = 10.0) -> SimulatedTrade:
    t = SimulatedTrade()
    t.id = uuid.uuid4()
    t.token_side = token_side
    t.simulated_price = price
    t.simulated_size_usd = size
    t.status = "OPEN"
    return t


def _resolution(price: float | None, closed: bool = True) -> MarketResolution:
    return MarketResolution(
        condition_id="test-market",
        closed=closed,
        resolution_price=price,
    )


# ---------------------------------------------------------------------------
# WIN scenarios
# ---------------------------------------------------------------------------

def test_yes_win():
    trade = _make_trade(token_side="YES", price=0.50, size=10.0)
    pnl, outcome = _compute_pnl(trade, _resolution(1.0))
    # 10 * (1/0.5) * 1.0 − 10 = 10
    assert outcome == "YES"
    assert pnl == pytest.approx(10.0)


def test_no_win():
    trade = _make_trade(token_side="NO", price=0.80, size=10.0)
    pnl, outcome = _compute_pnl(trade, _resolution(0.0))
    # 10 * (1/0.8) − 10 = 2.50
    assert outcome == "NO"
    assert pnl == pytest.approx(2.50)


# ---------------------------------------------------------------------------
# LOSS scenarios
# ---------------------------------------------------------------------------

def test_yes_loss():
    trade = _make_trade(token_side="YES", price=0.60, size=10.0)
    pnl, outcome = _compute_pnl(trade, _resolution(0.0))   # NO won
    assert pnl == pytest.approx(-10.0)
    assert outcome == "NO"


def test_no_loss():
    trade = _make_trade(token_side="NO", price=0.40, size=10.0)
    pnl, outcome = _compute_pnl(trade, _resolution(1.0))   # YES won
    assert pnl == pytest.approx(-10.0)
    assert outcome == "YES"


# ---------------------------------------------------------------------------
# INVALID / edge cases
# ---------------------------------------------------------------------------

def test_invalid_resolution():
    trade = _make_trade()
    pnl, outcome = _compute_pnl(trade, _resolution(None))
    assert pnl == 0.0
    assert outcome == "INVALID"


def test_fractional_resolution_price_treated_as_invalid():
    # Polymarket sometimes emits 0.5 for voided markets
    trade = _make_trade()
    pnl, outcome = _compute_pnl(trade, _resolution(0.5))
    assert outcome == "INVALID"
    assert pnl == 0.0


def test_high_price_win():
    # Price near 1.0 → barely any upside
    trade = _make_trade(token_side="YES", price=0.98, size=10.0)
    pnl, outcome = _compute_pnl(trade, _resolution(1.0))
    assert pnl == pytest.approx(10 * (1 / 0.98) - 10, rel=1e-4)
    assert outcome == "YES"


def test_low_price_win():
    # Price near 0.01 → large upside
    trade = _make_trade(token_side="YES", price=0.01, size=10.0)
    pnl, outcome = _compute_pnl(trade, _resolution(1.0))
    assert pnl == pytest.approx(990.0, rel=1e-4)
