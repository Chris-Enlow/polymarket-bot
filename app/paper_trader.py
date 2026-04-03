"""
paper_trader.py — Simulated execution engine.

When the monitor fires a trade event for a leader wallet, it drops a
TradeSignal into the shared asyncio.Queue. A pool of worker coroutines
drains the queue in parallel — each worker fetches the CLOB mid-price
for its market and inserts a SimulatedTrade row.

Speed notes
-----------
* TRADE_WORKER_COUNT workers run concurrently (default 8), so up to 8
  CLOB lookups can be in-flight simultaneously without blocking each other.
* The CLOB /midpoint endpoint is a single lightweight GET — roundtrip
  is typically <100 ms on a hosted VPS.
* DB insert uses a short-lived session per trade to maximise pool throughput.
"""
import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import aiohttp
import structlog
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.db_models import Leader, SimulatedTrade

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Signal dataclass — what the monitor puts on the queue
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class TradeSignal:
    leader: Leader
    market_id: str          # Polymarket condition ID / token ID
    token_side: str         # "YES" | "NO"
    token_id: str           # CLOB token ID for price lookup


# ---------------------------------------------------------------------------
# CLOB price fetch
# ---------------------------------------------------------------------------

class MidpointResponse(BaseModel):
    mid: float


async def _fetch_mid_price(
    session: aiohttp.ClientSession,
    token_id: str,
) -> float | None:
    """
    GET /midpoint?token_id=<id>

    Returns the mid-price (0.0–1.0) or None on failure.
    Uses exponential back-off on transient errors.
    """
    url = f"{settings.clob_api_base}/midpoint"
    for attempt in range(4):
        try:
            async with session.get(
                url,
                params={"token_id": token_id},
                timeout=aiohttp.ClientTimeout(total=settings.http_timeout),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
                parsed = MidpointResponse.model_validate(data)
                return parsed.mid
        except Exception as exc:
            wait = 2 ** attempt
            log.warning("clob_price_error", token_id=token_id, attempt=attempt, error=str(exc))
            await asyncio.sleep(wait)
    return None


# ---------------------------------------------------------------------------
# Trade worker
# ---------------------------------------------------------------------------

async def _trade_worker(
    worker_id: int,
    queue: asyncio.Queue[TradeSignal | None],
    http_session: aiohttp.ClientSession,
    db_factory: async_sessionmaker,
) -> None:
    """
    Drain the trade queue.  Receives TradeSignal items; None is a poison pill.
    """
    log.debug("trade_worker_start", worker_id=worker_id)
    while True:
        signal = await queue.get()
        if signal is None:
            queue.task_done()
            break

        try:
            await _execute_paper_trade(signal, http_session, db_factory)
        except Exception as exc:
            log.error("trade_worker_error", worker_id=worker_id, error=str(exc))
        finally:
            queue.task_done()


async def _execute_paper_trade(
    signal: TradeSignal,
    http_session: aiohttp.ClientSession,
    db_factory: async_sessionmaker,
) -> None:
    """
    Core paper-trade logic:
      1. Fetch current CLOB mid-price for the token.
      2. Insert a SimulatedTrade row with status=OPEN.
    """
    price = await _fetch_mid_price(http_session, signal.token_id)
    if price is None:
        log.error(
            "paper_trade_skipped_no_price",
            market_id=signal.market_id,
            leader=str(signal.leader.wallet_address),
        )
        return

    trade = SimulatedTrade(
        id=uuid.uuid4(),
        leader_id=signal.leader.id,
        market_id=signal.market_id,
        token_side=signal.token_side,
        simulated_price=price,
        simulated_size_usd=settings.trade_size_usd,
        opened_at=datetime.now(tz=timezone.utc),
        status="OPEN",
    )

    async with db_factory() as session:
        session.add(trade)
        await session.commit()

    log.info(
        "paper_trade_opened",
        market_id=signal.market_id,
        token_side=signal.token_side,
        price=price,
        size_usd=settings.trade_size_usd,
        leader=signal.leader.wallet_address,
        trade_id=str(trade.id),
    )


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

async def run_trade_workers(
    queue: asyncio.Queue[TradeSignal | None],
    http_session: aiohttp.ClientSession,
    db_factory: async_sessionmaker,
) -> None:
    """
    Spin up TRADE_WORKER_COUNT concurrent workers and await them all.
    Called once by bot.py; lives until the process exits.
    """
    workers = [
        asyncio.create_task(
            _trade_worker(i, queue, http_session, db_factory),
            name=f"trade-worker-{i}",
        )
        for i in range(settings.trade_worker_count)
    ]
    await asyncio.gather(*workers)
