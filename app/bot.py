"""
bot.py — Main entry point.

Wires all modules together and launches the concurrent task tree:

  ┌─ scanner loop   (HTTP polling, every 6 h)
  ├─ monitor        (WebSocket, persistent)
  ├─ trade workers  (N concurrent paper-trade executors)
  └─ pnl tracker    (HTTP polling, every 15 min)

A single shared asyncio.Queue connects the monitor to the trade workers,
keeping the hot path (WS event → queue) free of any I/O.

Startup sequence
----------------
1. Configure structlog.
2. Build DB engine, run Alembic migrations.
3. Build shared aiohttp session.
4. Run initial leader scan (blocks until complete so the monitor has
   something to subscribe to immediately).
5. Launch all background tasks.
6. On SIGINT/SIGTERM, cancel tasks gracefully.
"""
import asyncio
import logging
import signal
import sys
from contextlib import asynccontextmanager

import aiohttp
import structlog
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.db_models import Leader, build_engine, build_session_factory
from app.monitor import run_monitor
from app.paper_trader import TradeSignal, run_trade_workers
from app.pnl_tracker import run_pnl_tracker
from app.scanner import run_scanner_loop, scan_leaders

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def _configure_logging() -> None:
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.dev.ConsoleRenderer()
            if settings.log_level == "DEBUG"
            else structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, settings.log_level, logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
    )


# ---------------------------------------------------------------------------
# DB migration
# ---------------------------------------------------------------------------

def _run_migrations() -> None:
    """Run `alembic upgrade head` synchronously at startup."""
    cfg = AlembicConfig("alembic.ini")
    # Override the URL from env so alembic.ini doesn't need it hardcoded
    # Alembic needs the sync URL; swap the driver prefix
    sync_url = settings.database_url.replace("+asyncpg", "")
    cfg.set_main_option("sqlalchemy.url", sync_url)
    alembic_command.upgrade(cfg, "head")
    log.info("migrations_applied")


# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------

def _install_signal_handlers(tasks: list[asyncio.Task]) -> None:
    loop = asyncio.get_running_loop()

    def _shutdown(sig: signal.Signals) -> None:
        log.info("shutdown_signal", signal=sig.name)
        for task in tasks:
            task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown, sig)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    log.info("bot_starting")

    # -- DB session factory --------------------------------------------------
    engine = build_engine()
    db_factory: async_sessionmaker = build_session_factory(engine)

    # -- Shared HTTP session (one TCP connection pool for all modules) --------
    connector = aiohttp.TCPConnector(
        limit=100,          # total connection pool size
        limit_per_host=20,  # per-host cap; avoids hammering a single endpoint
        ttl_dns_cache=300,  # cache DNS for 5 min — avoids repeated lookups
        enable_cleanup_closed=True,
        force_close=True,   # close connection after each request; prevents TransferEncodingError from reused connections
    )
    async with aiohttp.ClientSession(connector=connector) as http_session:

        # -- Initial leader scan (must succeed before monitor starts) --------
        log.info("initial_scan_start")
        leaders: dict[str, Leader] = await scan_leaders(db_factory)
        if not leaders:
            log.warning("no_leaders_found_on_startup")

        # -- Shared queue between monitor and trade workers ------------------
        trade_queue: asyncio.Queue[TradeSignal | None] = asyncio.Queue(maxsize=1000)

        # -- Launch all background tasks -------------------------------------
        tasks: list[asyncio.Task] = [
            # Scanner: refreshes leaders dict in-place every 6 h
            asyncio.create_task(
                run_scanner_loop(db_factory, leaders),
                name="scanner",
            ),
            # Monitor: WebSocket listener; puts signals on the queue
            asyncio.create_task(
                run_monitor(leaders, trade_queue),
                name="monitor",
            ),
            # Trade workers: parallel CLOB fetch + DB insert
            asyncio.create_task(
                run_trade_workers(trade_queue, http_session, db_factory),
                name="trade-workers",
            ),
            # PnL tracker: checks market resolutions in the background
            asyncio.create_task(
                run_pnl_tracker(db_factory, http_session),
                name="pnl-tracker",
            ),
        ]

        _install_signal_handlers(tasks)
        log.info("bot_running", leaders=len(leaders), workers=settings.trade_worker_count)

        # Run until cancelled
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for task, result in zip(tasks, results):
            if isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError):
                log.error("task_failed", task=task.get_name(), error=str(result))

    await engine.dispose()
    log.info("bot_stopped")


if __name__ == "__main__":
    _configure_logging()
    _run_migrations()
    asyncio.run(main())
