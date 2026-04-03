"""
Tests for scanner.py — wallet qualification logic.
No network calls; business logic only.
"""
import pytest

from app.scanner import WalletStats, _is_qualified


def _make_stats(**kwargs) -> WalletStats:
    defaults = dict(
        proxy_wallet_address="0xabc",
        closed_positions=100,
        win_rate=0.60,
        roi_history=[{"roi": 0.10}, {"roi": 0.05}, {"roi": 0.08}],
    )
    defaults.update(kwargs)
    return WalletStats(**defaults)


# ---------------------------------------------------------------------------
# Qualification passes
# ---------------------------------------------------------------------------

def test_qualifies_when_all_thresholds_met():
    ok, wr, roi = _is_qualified(_make_stats())
    assert ok is True
    assert wr == pytest.approx(0.60)
    assert roi > 0


# ---------------------------------------------------------------------------
# Closed positions gate
# ---------------------------------------------------------------------------

def test_fails_too_few_positions():
    ok, _, _ = _is_qualified(_make_stats(closed_positions=30))
    assert ok is False


def test_fails_exactly_at_threshold():
    # threshold is > 50, so exactly 50 should fail
    ok, _, _ = _is_qualified(_make_stats(closed_positions=50))
    assert ok is False


def test_passes_one_above_threshold():
    ok, _, _ = _is_qualified(_make_stats(closed_positions=51))
    assert ok is True


# ---------------------------------------------------------------------------
# Win rate gate
# ---------------------------------------------------------------------------

def test_fails_low_win_rate():
    ok, _, _ = _is_qualified(_make_stats(win_rate=0.50))
    assert ok is False


def test_fails_exactly_at_win_rate_threshold():
    ok, _, _ = _is_qualified(_make_stats(win_rate=0.55))
    assert ok is False


# ---------------------------------------------------------------------------
# ROI / drawdown gate
# ---------------------------------------------------------------------------

def test_fails_negative_roi():
    ok, _, _ = _is_qualified(_make_stats(roi_history=[{"roi": -0.05}, {"roi": -0.10}]))
    assert ok is False


def test_fails_deep_drawdown_window():
    # One window below −30 % should disqualify even if average is positive
    ok, _, _ = _is_qualified(
        _make_stats(roi_history=[{"roi": 0.20}, {"roi": -0.35}, {"roi": 0.15}])
    )
    assert ok is False


def test_passes_drawdown_just_above_floor():
    ok, _, _ = _is_qualified(
        _make_stats(roi_history=[{"roi": 0.10}, {"roi": -0.29}, {"roi": 0.10}])
    )
    assert ok is True


def test_missing_roi_history_uses_pnl_fallback_positive():
    ok, _, _ = _is_qualified(_make_stats(roi_history=None, pnl_per_market=5.0))
    assert ok is True


def test_missing_roi_history_uses_pnl_fallback_negative():
    ok, _, _ = _is_qualified(_make_stats(roi_history=None, pnl_per_market=-1.0))
    assert ok is False
