"""
scanner.py — Leader wallet discovery.

Data source: data-api.polymarket.com/v1/leaderboard (public, no auth)

Qualification flow
------------------
1. Page the leaderboard ordered by all-time PnL → filters for positive ROI.
2. For each candidate, concurrently fetch their trade history
   (data-api /v1/trades) to derive:
     - closed_positions  = unique markets where they placed a SELL trade
     - win_rate          = profitable exits / total exits
   A "profitable exit" means the net PnL for that market (sell proceeds
   minus buy cost) is positive.
3. Wallets that pass all three gates are upserted into the `leaders` table.
4. Previously active wallets no longer qualifying are demoted.

Speed notes
-----------
* Per-wallet trade fetches are fired concurrently (asyncio.gather) up to
  a batch size of 20 so we don't hammer the API.
* The leaderboard is paged until either the API returns no next page or
  PnL hits zero (wallets after that point can't qualify on ROI).
"""
import asyncio
import json
from typing import Any

import httpx
import structlog
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.db_models import Leader

log = structlog.get_logger(__name__)

# Max wallets fetched concurrently for per-wallet trade history
_TRADE_FETCH_CONCURRENCY = 20
# Only evaluate the top N wallets by all-time PnL — prevents scanning 65k+ entries
_MAX_LEADERBOARD_CANDIDATES = 2000


# ---------------------------------------------------------------------------
# Pydantic response models
# ---------------------------------------------------------------------------

class LeaderboardEntry(BaseModel):
    proxyWallet: str
    pnl: float
    vol: float = 0.0

    @field_validator("proxyWallet")
    @classmethod
    def lower_address(cls, v: str) -> str:
        return v.lower()


class TradeRecord(BaseModel):
    """One trade from /v1/trades."""
    conditionId: str                # condition ID (market)
    side: str                       # "BUY" or "SELL"
    price: float
    size: float


# ---------------------------------------------------------------------------
# Qualification logic
# ---------------------------------------------------------------------------

def _calc_metrics(trades: list[TradeRecord]) -> tuple[int, float]:
    """
    From a wallet's trade history compute (closed_positions, win_rate).

    closed_positions — unique markets where the wallet placed at least one
                       SELL (i.e. they exited some or all of the position).
    win_rate         — fraction of those exits where net PnL > 0.
                       net PnL per market = Σ(sell proceeds) - Σ(buy cost)
    """
    buy_cost: dict[str, float] = {}
    sell_proceeds: dict[str, float] = {}

    for t in trades:
        market = t.conditionId
        notional = t.price * t.size
        if t.side.upper() == "BUY":
            buy_cost[market] = buy_cost.get(market, 0.0) + notional
        else:
            sell_proceeds[market] = sell_proceeds.get(market, 0.0) + notional

    # Only markets where they have exited count as "closed"
    closed_markets = set(sell_proceeds.keys())
    closed_positions = len(closed_markets)

    if closed_positions == 0:
        return 0, 0.0

    wins = sum(
        1 for m in closed_markets
        if sell_proceeds[m] > buy_cost.get(m, 0.0)
    )
    win_rate = wins / closed_positions
    return closed_positions, win_rate


def _is_qualified(
    closed_positions: int, win_rate: float, roi: float
) -> bool:
    return (
        closed_positions > settings.min_closed_positions
        and win_rate > settings.min_win_rate
        and roi > 0
    )


# ---------------------------------------------------------------------------
# Data API client helpers (httpx sync — connect timeout covers DNS + TCP + SSL)
# ---------------------------------------------------------------------------

_REQ_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Encoding": "identity",
    "Connection": "close",
}
# connect=12 covers the full DNS + TCP + SSL phase; read=20 covers response streaming
_HTTPX_TIMEOUT = httpx.Timeout(connect=12, read=20, write=10, pool=5)


def _sync_get(url: str, params: dict) -> bytes:
    """Blocking HTTP GET with hard connect timeout — run via run_in_executor."""
    with httpx.Client(timeout=_HTTPX_TIMEOUT, follow_redirects=True) as client:
        resp = client.get(url, params=params, headers=_REQ_HEADERS)
        resp.raise_for_status()
        return resp.content


async def _fetch_leaderboard_page(offset: int) -> list[LeaderboardEntry]:
    """Fetch one page of the leaderboard ordered by all-time PnL."""
    params = {"orderBy": "PNL", "timePeriod": "ALL", "limit": 100, "offset": offset}
    url = f"{settings.data_api_base}/v1/leaderboard"
    loop = asyncio.get_running_loop()
    for attempt in range(5):
        try:
            raw = await loop.run_in_executor(None, _sync_get, url, params)
            data = json.loads(raw)
            rows = data if isinstance(data, list) else data.get("data", [])
            return [LeaderboardEntry.model_validate(r) for r in rows]
        except Exception as exc:
            wait = 2 ** attempt
            log.warning(
                "leaderboard_fetch_error",
                attempt=attempt,
                wait=wait,
                exc_type=type(exc).__name__,
                error=str(exc),
            )
            await asyncio.sleep(wait)
    raise RuntimeError("Leaderboard API unreachable after 5 attempts")


async def _fetch_wallet_trades(wallet: str) -> list[TradeRecord]:
    """Fetch up to 500 most recent trades for a wallet."""
    params = {"user": wallet, "limit": 500}
    url = f"{settings.data_api_base}/v1/trades"
    loop = asyncio.get_running_loop()
    for attempt in range(4):
        try:
            raw = await loop.run_in_executor(None, _sync_get, url, params)
            data = json.loads(raw)
            rows = data if isinstance(data, list) else data.get("data", [])
            trades = []
            for r in rows:
                try:
                    trades.append(TradeRecord.model_validate(r))
                except Exception:
                    pass
            return trades
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return []
            wait = 2 ** attempt
            log.warning("trades_fetch_error", wallet=wallet[:10], attempt=attempt, exc_type=type(exc).__name__, error=str(exc))
            await asyncio.sleep(wait)
        except Exception as exc:
            wait = 2 ** attempt
            log.warning("trades_fetch_error", wallet=wallet[:10], attempt=attempt, exc_type=type(exc).__name__, error=str(exc))
            await asyncio.sleep(wait)
    return []


# ---------------------------------------------------------------------------
# DB upsert helpers
# ---------------------------------------------------------------------------

async def _upsert_leader(
    session: AsyncSession,
    wallet: str,
    win_rate: float,
    roi: float,
    closed_positions: int,
) -> Leader:
    stmt = (
        pg_insert(Leader)
        .values(
            wallet_address=wallet,
            win_rate=win_rate,
            roi_6m=roi,
            closed_positions=closed_positions,
            active=True,
        )
        .on_conflict_do_update(
            index_elements=["wallet_address"],
            set_={
                "win_rate": win_rate,
                "roi_6m": roi,
                "closed_positions": closed_positions,
                "active": True,
            },
        )
        .returning(Leader)
    )
    result = await session.execute(stmt)
    await session.commit()
    return result.scalar_one()


async def _demote_stale(
    session: AsyncSession, active_addresses: set[str]
) -> None:
    result = await session.execute(
        select(Leader).where(Leader.active.is_(True))
    )
    stale = [r for r in result.scalars() if r.wallet_address not in active_addresses]
    for leader in stale:
        leader.active = False
        log.info("leader_demoted", wallet=leader.wallet_address)
    if stale:
        await session.commit()


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

async def scan_leaders(
    db_factory: async_sessionmaker,
) -> dict[str, Leader]:
    """
    Full scan cycle. Returns {wallet_address: Leader} of active leaders.

    HTTP fetches use urllib.request via run_in_executor — avoids aiohttp SSL
    hangs observed in Docker/WSL2 environments.
    """
    log.info("scanner_start")

    # -- Step 1: page the leaderboard until PnL turns negative or cap is reached --
    candidates: list[LeaderboardEntry] = []
    offset = 0
    while len(candidates) < _MAX_LEADERBOARD_CANDIDATES:
        page = await _fetch_leaderboard_page(offset)
        if not page:
            break
        positive = [e for e in page if e.pnl > 0]
        candidates.extend(positive)
        if len(positive) < len(page):
            # Hit the zero-PnL boundary; no point fetching further pages
            break
        offset += len(page)
    candidates = candidates[:_MAX_LEADERBOARD_CANDIDATES]

    log.info("scanner_candidates", count=len(candidates))

    # -- Step 2: fetch per-wallet trade history concurrently in batches --
    qualified: dict[str, tuple[float, float, int]] = {}  # wallet → (wr, roi, cp)

    for i in range(0, len(candidates), _TRADE_FETCH_CONCURRENCY):
        batch = candidates[i : i + _TRADE_FETCH_CONCURRENCY]
        trade_lists = await asyncio.gather(
            *[_fetch_wallet_trades(e.proxyWallet) for e in batch]
        )
        for entry, trades in zip(batch, trade_lists):
            cp, wr = _calc_metrics(trades)
            if _is_qualified(cp, wr, entry.pnl):
                qualified[entry.proxyWallet] = (wr, entry.pnl, cp)
                log.debug(
                    "leader_qualified",
                    wallet=entry.proxyWallet[:10],
                    win_rate=round(wr, 3),
                    closed_positions=cp,
                    pnl=entry.pnl,
                )

    log.info("scanner_qualified", count=len(qualified))

    # -- Step 3: upsert to DB --
    active_leaders: dict[str, Leader] = {}
    async with db_factory() as session:
        for wallet, (wr, roi, cp) in qualified.items():
            leader = await _upsert_leader(session, wallet, wr, roi, cp)
            active_leaders[wallet] = leader
        await _demote_stale(session, set(qualified.keys()))

    log.info("scanner_done", active_leaders=len(active_leaders))
    return active_leaders


async def run_scanner_loop(
    db_factory: async_sessionmaker,
    leaders_ref: dict[str, Leader],
) -> None:
    """Background task: refresh leader list every LEADER_REFRESH_INTERVAL_HOURS."""
    interval = settings.leader_refresh_interval_hours * 3600
    while True:
        try:
            fresh = await scan_leaders(db_factory)
            leaders_ref.clear()
            leaders_ref.update(fresh)
        except Exception as exc:
            log.error("scanner_loop_error", error=str(exc))
        await asyncio.sleep(interval)
