"""
Tests for monitor.py — event parsing logic.
No network calls; pure dict transformation.
"""
import uuid
import pytest

from app.db_models import Leader
from app.monitor import _parse_event


def _make_leader(address: str = "0xabc") -> Leader:
    l = Leader()
    l.id = uuid.uuid4()
    l.wallet_address = address.lower()
    l.active = True
    return l


def _leaders(address: str = "0xabc") -> dict[str, Leader]:
    return {address.lower(): _make_leader(address)}


# ---------------------------------------------------------------------------
# Valid BUY events
# ---------------------------------------------------------------------------

def test_valid_buy_event():
    event = {
        "event_type": "trade",
        "type": "BUY",
        "user": "0xabc",
        "market": "market-001",
        "asset_id": "token-yes-001",
        "outcome": "YES",
    }
    signal = _parse_event(event, _leaders("0xabc"))
    assert signal is not None
    assert signal.market_id == "market-001"
    assert signal.token_side == "YES"
    assert signal.token_id == "token-yes-001"


def test_case_insensitive_wallet_match():
    event = {
        "event_type": "trade",
        "type": "BUY",
        "user": "0xABC",          # uppercase in WS message
        "market": "market-001",
        "asset_id": "token-001",
        "outcome": "NO",
    }
    signal = _parse_event(event, _leaders("0xabc"))
    assert signal is not None
    assert signal.token_side == "NO"


# ---------------------------------------------------------------------------
# Events that should be ignored
# ---------------------------------------------------------------------------

def test_sell_event_ignored():
    event = {
        "event_type": "trade",
        "type": "SELL",
        "user": "0xabc",
        "market": "market-001",
        "asset_id": "token-001",
        "outcome": "YES",
    }
    assert _parse_event(event, _leaders()) is None


def test_unknown_wallet_ignored():
    event = {
        "event_type": "trade",
        "type": "BUY",
        "user": "0xunknown",
        "market": "market-001",
        "asset_id": "token-001",
        "outcome": "YES",
    }
    assert _parse_event(event, _leaders("0xabc")) is None


def test_non_trade_event_ignored():
    event = {"event_type": "status", "status": "connected"}
    assert _parse_event(event, _leaders()) is None


def test_missing_market_id_ignored():
    event = {
        "event_type": "trade",
        "type": "BUY",
        "user": "0xabc",
        # no "market" key
        "asset_id": "token-001",
        "outcome": "YES",
    }
    assert _parse_event(event, _leaders("0xabc")) is None
