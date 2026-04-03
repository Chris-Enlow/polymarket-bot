# polymarket-bot

A fully automated, **paper-trading only** Polymarket copy-trading bot. No real money, no blockchain execution, no wallet signing. The bot discovers high-performing wallets, monitors them in real time, simulates trades, and tracks PnL — all logged to PostgreSQL.

---

## How It Works

### Step 1 — Find Good Traders (scanner.py)

Every 6 hours, the bot pages through Polymarket's public leaderboard (`data-api.polymarket.com/v1/leaderboard`) ordered by all-time profit. For every wallet with positive PnL, it fetches that wallet's full trade history (`/v1/trades`) and computes two metrics:

**Closed Positions**
Only markets where the wallet placed both a BUY and a SELL are counted. A buy-and-hold never resolves, so it doesn't prove anything. The number of unique markets they actually exited is their "closed positions" count.

**Win Rate**
For each closed market:
```
net = (total sell proceeds) - (total buy cost)
```
If `net > 0`, that market is a win. Win rate = wins / total closed markets.

A wallet becomes a **Leader** only if all three gates pass:

| Criterion | Threshold |
|-----------|-----------|
| Closed positions | > 50 |
| Win rate | > 55% |
| All-time PnL | Positive |

Leaders are saved to the `leaders` table. Wallets that drop below thresholds on the next scan are marked `active = false` and removed from monitoring.

---

### Step 2 — Watch Them in Real Time (monitor.py)

Every 60 seconds the monitor polls `data-api.polymarket.com/v1/trades` for each active leader wallet (10 wallets concurrently). It compares trade timestamps against the last-seen timestamp per wallet — any new **BUY** trade is a signal.

When a new leader BUY is detected:
1. The monitor extracts the market ID, token side (YES/NO), and leader wallet.
2. It drops a lightweight signal onto an `asyncio.Queue` — never blocks the poll loop.
3. The poll cycle continues immediately.

Detection latency is ~30–60 seconds, which is acceptable for paper trading since fills are simulated, not executed.

> **Why not WebSocket?** The Polymarket CLOB `user` channel requires per-wallet authentication (API key + secret tied to the wallet owner). Since we monitor third-party wallets whose credentials we don't hold, the connection is always rejected. The Data API is fully public.

---

### Step 3 — Simulate the Trade (paper_trader.py)

Eight worker coroutines run in parallel, all draining the same queue. When a worker picks up a signal:

1. It fetches the current mid-price from the CLOB order book (`clob.polymarket.com`).
2. It inserts a row into `simulated_trades` recording:
   - Which leader triggered it
   - Which market and which side (YES or NO)
   - The price at the moment of detection
   - A fixed simulated stake (default: $10)

Nothing is actually bought or sold. This is a record of *what would have happened* if you had copied the trade at that moment.

The queue+workers architecture keeps the WebSocket hot path completely free of I/O. The monitor never waits for a price fetch.

---

### Step 4 — Track Outcomes (pnl_tracker.py)

Every 15 minutes, the tracker queries all `OPEN` trades and checks whether those markets have resolved.

**If resolved:**

WIN — you predicted correctly:
```
pnl = (stake / entry_price) * 1.0 - stake
```
Example: bought YES at $0.40 with $10 → got 25 shares → paid out $25 → profit = **+$15**

LOSS — you predicted incorrectly:
```
pnl = -stake
```
You lose your full simulated stake: **−$10**

INVALID — market was voided:
```
pnl = 0
```

The trade row is updated with `resolved_at`, `resolution_outcome`, `pnl_usd`, and `status = RESOLVED`.

---

## Data Flow Diagram

```
Every 6h  ──► Leaderboard API
               └── filter by closed positions, win rate, PnL
                   └── upsert leaders table

Every 60s ──► Data API /v1/trades for each leader wallet (10 concurrent)
               └── new BUY trade detected → put signal on asyncio.Queue
                   └── 8 parallel workers drain queue
                       └── fetch mid-price from CLOB
                           └── insert row into simulated_trades

Every 15m ──► query OPEN simulated trades
               └── check market resolution via Data API
                   └── compute PnL, update row to RESOLVED
```

---

## Database Schema

### `leaders`
| Column | Type | Description |
|--------|------|-------------|
| `id` | UUID | Primary key |
| `wallet_address` | TEXT | Polymarket proxy wallet (lowercase) |
| `win_rate` | NUMERIC | 0.0 – 1.0 |
| `roi_6m` | NUMERIC | All-time PnL used as ROI proxy |
| `closed_positions` | INTEGER | Unique exited markets |
| `qualified_at` | TIMESTAMPTZ | When first promoted |
| `active` | BOOLEAN | False = demoted, no longer monitored |

### `simulated_trades`
| Column | Type | Description |
|--------|------|-------------|
| `id` | UUID | Primary key |
| `leader_id` | UUID | FK → leaders |
| `market_id` | TEXT | Polymarket condition ID |
| `token_side` | TEXT | `YES` or `NO` |
| `simulated_price` | NUMERIC | Mid-price at time of detection |
| `simulated_size_usd` | NUMERIC | Fixed stake per trade (default $10) |
| `opened_at` | TIMESTAMPTZ | When the simulated trade was recorded |
| `resolved_at` | TIMESTAMPTZ | When the market settled (nullable) |
| `resolution_outcome` | TEXT | `YES`, `NO`, or `INVALID` |
| `pnl_usd` | NUMERIC | Profit/loss in USD (nullable until resolved) |
| `status` | TEXT | `OPEN`, `RESOLVED`, or `INVALID` |

---

## APIs Used

| Endpoint | Purpose |
|----------|---------|
| `data-api.polymarket.com/v1/leaderboard` | Ranked wallet list by all-time PnL |
| `data-api.polymarket.com/v1/trades?user=<wallet>` | Per-wallet trade history |
| `clob.polymarket.com` | Live order book mid-prices |

All endpoints are public and require no authentication.

---

## Running the Bot

**Prerequisites:** Docker Desktop running with WSL integration enabled.

```bash
cp .env.example .env
docker compose up --build
```

The app will:
1. Run Alembic migrations automatically on startup
2. Perform an initial leader scan (this must complete before monitoring begins)
3. Launch the scanner loop, polling monitor, trade workers, and PnL tracker concurrently

**Stop with:** `Ctrl+C` — tasks are cancelled gracefully on SIGINT/SIGTERM.

---

## Configuration

All settings are in `.env`. Key values:

```env
TRADE_SIZE_USD=10.0                  # Simulated stake per trade
LEADER_REFRESH_INTERVAL_HOURS=6      # How often to re-qualify wallets
PNL_CHECK_INTERVAL_MINUTES=15        # How often to check market resolutions
MONITOR_POLL_INTERVAL_SECONDS=60     # How often to poll each leader wallet
MIN_CLOSED_POSITIONS=50              # Leader qualification threshold
MIN_WIN_RATE=0.55                    # Leader qualification threshold
DATABASE_URL=postgresql+asyncpg://poly:poly@db:5432/polymarket
LOG_LEVEL=INFO
```

---

## Trade Report

Run the report from your host terminal at any time while the bot is running:

```bash
# Full report — summary + wins + losses + open positions
docker compose exec app python -m app.report

# Only open (unresolved) positions
docker compose exec app python -m app.report --open

# Only closed trades (wins, losses, invalid)
docker compose exec app python -m app.report --resolved

# Filter to a single leader wallet
docker compose exec app python -m app.report --leader 0xabc123...

# Increase row limit (default: 50 per table)
docker compose exec app python -m app.report --limit 100
```

The report prints a summary panel followed by colour-coded tables:

```
╭─────────────────────── Paper Trading Summary ───────────────────────╮
│ Trades:  142 total  (98 open / 38 resolved / 6 invalid)             │
│ Wins/Losses:  24 wins / 14 losses  (Win rate: 63.2%)                │
│ Total PnL:  +18.42 USD  (avg per resolved: +0.4847)                 │
╰─────────────────────────────────────────────────────────────────────╯

  Winning Trades (24)
  Opened            Leader        Market   Side  Price   Size $  PnL $
  2026-04-03 21:10  0x3f9a1b2c…  0x7fa3…  YES   0.3200  10.00  +21.25
  ...

  Losing Trades (14)
  Opened            Leader        Market   Side  Price   Size $  PnL $
  2026-04-03 21:15  0x8d2e4f1a…  0x9c21…  NO    0.7100  10.00  -10.00
  ...
```

---

## Module Reference

| File | Role |
|------|------|
| `app/bot.py` | Entry point; wires all modules together, manages task lifecycle |
| `app/scanner.py` | Leader wallet discovery and qualification |
| `app/monitor.py` | Polls Data API every 60s per leader wallet; feeds trade signals to the queue |
| `app/report.py` | CLI trade report — wins, losses, open positions, PnL summary |
| `app/paper_trader.py` | Drains queue, fetches prices, records simulated trades |
| `app/pnl_tracker.py` | Resolves open trades and computes final PnL |
| `app/db_models.py` | SQLAlchemy ORM models (`leaders`, `simulated_trades`) |
| `app/config.py` | Settings loaded from `.env` via pydantic-settings |
| `alembic/` | DB migration scripts |
