"""
pnl_tracker.py — Background resolution checker.

Every PNL_CHECK_INTERVAL_MINUTES the tracker:
  1. Loads all OPEN simulated trades from the DB.
  2. Deduplicates by market_id and fetches resolution status from the
     Data API — one concurrent request per unique market.
  3. Updates resolved trades with PnL and flips their status.

PnL formula (from CLAUDE.md):
  WIN   → size * (1 / price) * 1.0 − size
  LOSS  → −size
  INVALID → 0

Speed notes
-----------
* Market resolutions are fetched concurrently via asyncio.gather so the
  check cycle time is bounded by the slowest single market lookup, not
  the total number of open markets.
* We deduplicate by market_id to avoid redundant API calls when the same
  market has multiple open trades.
"""
import asyncio
from datetime import datetime, timezone
from typing import Any

import aiohttp
import structlog
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.db_models import SimulatedTrade

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Pydantic model for Data API market response
# ---------------------------------------------------------------------------

class MarketResolution(BaseModel):
    """Minimal fields we need from the Data API /markets/{id} endpoint."""
    condition_id: str
    closed: bool = False
    # Polymarket sets question_id outcome to "0.5" (invalid), "0" or "1"
    # winners_share maps outcome token → payout (0 or 1)
    # We look for 'outcome' or 'resolution_price' depending on endpoint version
    resolution_price: float | None = None   # 1.0 = YES won, 0.0 = NO won
    outcome: str | None = None              # "YES" | "NO" | "UNKNOWN"


# ---------------------------------------------------------------------------
# Data API fetch
# ---------------------------------------------------------------------------

async def _fetch_market_resolution(
    session: aiohttp.ClientSession,
    market_id: str,
) -> MarketResolution | None:
    """GET /markets/{condition_id} from the Data API."""
    url = f"{settings.data_api_base}/markets/{market_id}"
    for attempt in range(4):
        try:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=settings.http_timeout),
            ) as resp:
                if resp.status == 404:
                    return None
                resp.raise_for_status()
                data = await resp.json()
                return MarketResolution.model_validate(data)
        except Exception as exc:
            wait = 2 ** attempt
            log.warning("data_api_error", market_id=market_id, attempt=attempt, error=str(exc))
            await asyncio.sleep(wait)
    return None


# ---------------------------------------------------------------------------
# PnL calculation
# ---------------------------------------------------------------------------

def _compute_pnl(trade: SimulatedTrade, resolution: MarketResolution) -> tuple[float, str]:
    """
    Return (pnl_usd, resolution_outcome).

    resolution_price: 1.0 → YES won; 0.0 → NO won; None → INVALID
    """
    if resolution.resolution_price is None:
        return 0.0, "INVALID"

    size = float(trade.simulated_size_usd)
    price = float(trade.simulated_price)

    # Determine if the trade's token side won
    if resolution.resolution_price == 1.0:
        winning_side = "YES"
    elif resolution.resolution_price == 0.0:
        winning_side = "NO"
    else:
        # Fractional resolution → treat as INVALID
        return 0.0, "INVALID"

    if trade.token_side == winning_side:
        # WIN: shares paid out at $1 each; we bought at `price` per share
        pnl = size * (1.0 / price) * 1.0 - size
        return round(pnl, 4), winning_side
    else:
        return round(-size, 4), winning_side


# ---------------------------------------------------------------------------
# Resolution cycle
# ---------------------------------------------------------------------------

async def _resolve_batch(
    session_factory: async_sessionmaker,
    http_session: aiohttp.ClientSession,
) -> None:
    """One full resolution cycle."""
    async with session_factory() as db:
        result = await db.execute(
            select(SimulatedTrade).where(SimulatedTrade.status == "OPEN")
        )
        open_trades: list[SimulatedTrade] = list(result.scalars())

    if not open_trades:
        log.debug("pnl_tracker_no_open_trades")
        return

    # Deduplicate: fetch each unique market once
    unique_markets: set[str] = {t.market_id for t in open_trades}
    log.info("pnl_tracker_checking", open_trades=len(open_trades), unique_markets=len(unique_markets))

    resolutions: dict[str, MarketResolution | None] = {}
    tasks = {
        market_id: asyncio.create_task(
            _fetch_market_resolution(http_session, market_id),
            name=f"resolve-{market_id[:12]}",
        )
        for market_id in unique_markets
    }
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    for market_id, result in zip(tasks.keys(), results):
        resolutions[market_id] = result if not isinstance(result, Exception) else None

    # Update resolved trades
    resolved_count = 0
    async with session_factory() as db:
        for trade in open_trades:
            res = resolutions.get(trade.market_id)
            if res is None or not res.closed:
                continue

            pnl, outcome = _compute_pnl(trade, res)
            trade.pnl_usd = pnl
            trade.resolution_outcome = outcome
            trade.resolved_at = datetime.now(tz=timezone.utc)
            trade.status = "RESOLVED" if outcome != "INVALID" else "INVALID"
            db.add(trade)
            resolved_count += 1

            log.info(
                "trade_resolved",
                trade_id=str(trade.id),
                market_id=trade.market_id,
                side=trade.token_side,
                outcome=outcome,
                pnl_usd=pnl,
            )

        if resolved_count:
            await db.commit()

    log.info("pnl_tracker_cycle_done", resolved=resolved_count)


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

async def run_pnl_tracker(
    db_factory: async_sessionmaker,
    http_session: aiohttp.ClientSession,
) -> None:
    """Background loop: check resolutions every PNL_CHECK_INTERVAL_MINUTES."""
    interval = settings.pnl_check_interval_minutes * 60
    while True:
        try:
            await _resolve_batch(db_factory, http_session)
        except Exception as exc:
            log.error("pnl_tracker_error", error=str(exc))
        await asyncio.sleep(interval)
