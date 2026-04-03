"""
monitor.py — Leader wallet activity polling.

Replaces the WebSocket approach. The Polymarket CLOB 'user' channel
requires per-wallet authentication (apiKey/secret/passphrase tied to
the wallet owner). Since we monitor third-party leader wallets whose
credentials we do not hold, the WS connection is always rejected.

Instead, we poll the Data API /v1/trades endpoint for each active
leader wallet on a staggered schedule. New BUY trades detected since
the last poll are converted to TradeSignals and pushed onto the shared
queue for the paper_trader workers.

Latency vs WebSocket
--------------------
Polling introduces ~30–60 s detection latency (acceptable for paper
trading — we simulate fills, not race for execution).

Request volume
--------------
With _POLL_CONCURRENCY = 10 and a 60 s interval, 289 wallets take
~9 s to scan (≈ 5 req/s average). Each request fetches only the
most recent _TRADE_LIMIT trades, keeping payloads small.
"""
import asyncio
from datetime import datetime, timezone
from time import monotonic

import aiohttp
import structlog
from pydantic import BaseModel, field_validator

from app.config import settings
from app.db_models import Leader
from app.paper_trader import TradeSignal

log = structlog.get_logger(__name__)

# Recent trades to fetch per wallet per cycle (keeps payloads small)
_TRADE_LIMIT = 20
# Concurrent wallet fetches per batch
_POLL_CONCURRENCY = 10
# Seconds between full poll cycles (overridden by settings at runtime)
_POLL_INTERVAL = 60.0  # default; replaced by settings.monitor_poll_interval_seconds


# ---------------------------------------------------------------------------
# Response model
# ---------------------------------------------------------------------------

class _RecentTrade(BaseModel):
    """Fields we need from a single /v1/trades entry."""

    conditionId: str
    side: str                       # "BUY" | "SELL"
    price: float
    size: float
    asset_id: str = ""              # CLOB token ID for price lookup
    outcome: str = "YES"            # "YES" | "NO"
    # Polymarket returns Unix seconds (int) or an ISO-8601 string
    timestamp: int | float | str | None = None

    model_config = {"extra": "ignore"}

    @field_validator("side", "outcome", mode="before")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper() if isinstance(v, str) else v

    def parsed_timestamp(self) -> datetime | None:
        """Return an aware UTC datetime or None."""
        ts = self.timestamp
        if ts is None:
            return None
        if isinstance(ts, (int, float)):
            return datetime.fromtimestamp(float(ts), tz=timezone.utc)
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None


# ---------------------------------------------------------------------------
# Per-wallet HTTP fetch
# ---------------------------------------------------------------------------

async def _fetch_recent_trades(
    session: aiohttp.ClientSession,
    wallet: str,
) -> list[_RecentTrade]:
    """GET /v1/trades?user=<wallet>&limit=<n> with exponential back-off."""
    url = f"{settings.data_api_base}/v1/trades"
    for attempt in range(4):
        try:
            async with session.get(
                url,
                params={"user": wallet, "limit": _TRADE_LIMIT},
                timeout=aiohttp.ClientTimeout(total=settings.http_timeout),
            ) as resp:
                if resp.status == 404:
                    return []
                resp.raise_for_status()
                data = await resp.json()
                rows = data if isinstance(data, list) else data.get("data", [])
                out: list[_RecentTrade] = []
                for r in rows:
                    try:
                        out.append(_RecentTrade.model_validate(r))
                    except Exception:
                        pass
                return out
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            wait = 2 ** attempt
            log.warning(
                "monitor_fetch_error",
                wallet=wallet[:10],
                attempt=attempt,
                error=str(exc),
            )
            await asyncio.sleep(wait)
    return []


# ---------------------------------------------------------------------------
# Per-wallet signal detection
# ---------------------------------------------------------------------------

async def _poll_wallet(
    session: aiohttp.ClientSession,
    wallet: str,
    leader: Leader,
    last_seen: dict[str, datetime],
    startup_time: datetime,
    queue: asyncio.Queue[TradeSignal | None],
) -> None:
    """
    Fetch recent trades for one wallet; emit TradeSignals for new BUYs.

    `last_seen[wallet]` tracks the newest trade timestamp processed so
    far. On first encounter the startup_time is used as the cutoff so
    we ignore historical trades and only act on live activity.
    """
    trades = await _fetch_recent_trades(session, wallet)
    if not trades:
        return

    cutoff = last_seen.get(wallet, startup_time)
    newest: datetime = cutoff
    signals = 0

    for trade in trades:
        ts = trade.parsed_timestamp()
        if ts is None:
            continue
        if ts <= cutoff:
            continue  # already processed or pre-startup
        if trade.side != "BUY":
            continue  # only mirror opens; exits handled by pnl_tracker
        if not trade.conditionId or not trade.asset_id:
            log.warning("monitor_incomplete_trade", wallet=wallet[:10], trade=trade.model_dump())
            continue

        queue.put_nowait(TradeSignal(
            leader=leader,
            market_id=trade.conditionId,
            token_side=trade.outcome,
            token_id=trade.asset_id,
        ))
        signals += 1

        if ts > newest:
            newest = ts

        log.debug(
            "monitor_signal_queued",
            market_id=trade.conditionId,
            side=trade.outcome,
            leader=wallet,
        )

    if newest > cutoff:
        last_seen[wallet] = newest

    if signals:
        log.info("monitor_new_signals", wallet=wallet[:10], count=signals)


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

async def run_monitor(
    leaders: dict[str, Leader],
    queue: asyncio.Queue[TradeSignal | None],
) -> None:
    """
    Polling loop: scan every active leader wallet for new BUY trades
    every _POLL_INTERVAL seconds. Runs until cancelled.
    """
    startup_time = datetime.now(tz=timezone.utc)
    last_seen: dict[str, datetime] = {}
    poll_interval = float(settings.monitor_poll_interval_seconds)

    async with aiohttp.ClientSession() as session:
        while True:
            wallets = list(leaders.items())

            if not wallets:
                log.debug("monitor_no_leaders_sleeping")
                await asyncio.sleep(poll_interval)
                continue

            log.info("monitor_poll_start", wallet_count=len(wallets))
            t0 = monotonic()

            for i in range(0, len(wallets), _POLL_CONCURRENCY):
                batch = wallets[i : i + _POLL_CONCURRENCY]
                try:
                    await asyncio.gather(*[
                        _poll_wallet(session, wallet, leader, last_seen, startup_time, queue)
                        for wallet, leader in batch
                    ])
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.error("monitor_batch_error", batch_start=i, error=str(exc))

            elapsed = monotonic() - t0
            sleep_for = max(0.0, poll_interval - elapsed)
            log.info(
                "monitor_poll_done",
                elapsed_s=round(elapsed, 1),
                next_poll_in_s=round(sleep_for, 1),
            )
            await asyncio.sleep(sleep_for)
