# CLAUDE.md — Polymarket Copy-Trading Bot

Read this file in full before writing any code. It is the authoritative spec for this project.

---

## Project Overview

A fully automated, **paper-trading only** Polymarket copy-trading bot. No real money, no blockchain execution, no Web3 signing. The bot discovers high-performing "Leader" wallets via Polymarket's public APIs, monitors them in real time over WebSocket, and simulates trades locally — logging everything to PostgreSQL for PnL analysis.

---

## Hard Constraints (Never Violate)

- **No real execution.** Zero Web3, zero wallet signing, zero on-chain calls. The paper trader simulates fills using live order book mid-prices only.
- **No credentials in code.** All secrets go in `.env` (never committed). Provide `.env.example` instead.
- **Async throughout.** Use `asyncio` + `aiohttp` for all I/O. No blocking calls on the event loop.
- **PostgreSQL only.** SQLAlchemy async ORM (`asyncpg` driver). No SQLite fallbacks.
- **Docker-first.** The app must start cleanly with `docker compose up`. No host-level dependencies assumed.
- **No speculative features.** Build exactly what is specified. No extra endpoints, no UI, no alerting integrations unless asked.

---

## Module Layout

```
polymarket-bot/
├── CLAUDE.md               ← this file
├── .env.example
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── alembic/                ← DB migrations
│   └── ...
├── app/
│   ├── __init__.py
│   ├── bot.py              ← main entry point; wires everything together
│   ├── scanner.py          ← Leader wallet discovery via Gamma/Data API
│   ├── monitor.py          ← WebSocket listener for Leader wallet activity
│   ├── paper_trader.py     ← simulated execution; fetches CLOB price & logs trade
│   ├── pnl_tracker.py      ← background task; resolves markets & updates PnL
│   ├── db_models.py        ← SQLAlchemy ORM models
│   └── config.py           ← loads .env via pydantic-settings
└── tests/
    └── ...
```

---

## API Surface (Polymarket)

| Purpose | Base URL / Endpoint |
|---|---|
| Wallet/market stats (Gamma API) | `https://gamma-api.polymarket.com` |
| Order book / CLOB prices | `https://clob.polymarket.com` |
| Markets data (Data API) | `https://data-api.polymarket.com` |
| Real-time events (WebSocket) | `wss://ws-subscriptions-clob.polymarket.com/ws/` |

All HTTP calls use `aiohttp.ClientSession` with a shared session per service. Respect rate limits with exponential backoff.

---

## Leader Wallet Qualification Criteria

A wallet is promoted to "Leader" status only when **all three** of the following are true:

| Criterion | Threshold |
|---|---|
| Closed positions | > 50 |
| Win rate | > 55% |
| 6-month ROI | Positive and consistent (defined as: ROI > 0% with no single 30-day window showing > −30% drawdown) |

Leader list is refreshed on a configurable interval (default: every 6 hours). Wallets that drop below thresholds are demoted and no longer monitored.

---

## Database Schema

### `leaders` table
| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `wallet_address` | TEXT UNIQUE | checksummed |
| `win_rate` | NUMERIC | 0.0–1.0 |
| `roi_6m` | NUMERIC | |
| `closed_positions` | INTEGER | |
| `qualified_at` | TIMESTAMPTZ | when first qualified |
| `active` | BOOLEAN | false = demoted |

### `simulated_trades` table
| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `leader_id` | UUID FK → leaders | |
| `market_id` | TEXT | Polymarket condition ID |
| `token_side` | TEXT | `YES` or `NO` |
| `simulated_price` | NUMERIC | mid-price from CLOB at time of detection |
| `simulated_size_usd` | NUMERIC | configurable fixed size per trade |
| `opened_at` | TIMESTAMPTZ | |
| `resolved_at` | TIMESTAMPTZ | nullable |
| `resolution_outcome` | TEXT | `YES`, `NO`, or `INVALID` |
| `pnl_usd` | NUMERIC | nullable until resolved |
| `status` | TEXT | `OPEN`, `RESOLVED`, `INVALID` |

---

## Async Data Flow

```
bot.py (main)
  │
  ├─► scanner.py          HTTP polling (every 6h)
  │     └─► qualifies wallets → upserts leaders table
  │
  ├─► monitor.py          WebSocket (persistent, auto-reconnect)
  │     └─► on new position event for a Leader wallet
  │           └─► paper_trader.py
  │                 ├─► fetch mid-price from CLOB API (HTTP)
  │                 └─► insert row into simulated_trades
  │
  └─► pnl_tracker.py      asyncio background task (every 15 min)
        └─► query OPEN trades → check market resolution via Data API
              └─► if resolved: compute PnL, update row status
```

### PnL Calculation
- **WIN:** `pnl_usd = simulated_size_usd * (1 / simulated_price) * resolution_value - simulated_size_usd`
  - resolution_value = 1.0 for correct outcome
- **LOSS:** `pnl_usd = -simulated_size_usd`
- **INVALID:** `pnl_usd = 0`

---

## Configuration (`.env`)

```
# Polymarket
GAMMA_API_BASE=https://gamma-api.polymarket.com
CLOB_API_BASE=https://clob.polymarket.com
DATA_API_BASE=https://data-api.polymarket.com
WS_URL=wss://ws-subscriptions-clob.polymarket.com/ws/

# Paper trading
TRADE_SIZE_USD=10.0
LEADER_REFRESH_INTERVAL_HOURS=6
PNL_CHECK_INTERVAL_MINUTES=15

# PostgreSQL
DATABASE_URL=postgresql+asyncpg://poly:poly@db:5432/polymarket

# Logging
LOG_LEVEL=INFO
```

---

## Docker Setup

- `docker-compose.yml` defines two services: `app` and `db` (postgres:16-alpine).
- `app` depends on `db` with a healthcheck wait.
- DB migrations run automatically on container startup via Alembic (`alembic upgrade head`).
- The app image is built from a `Dockerfile` using `python:3.12-slim`.
- A named volume persists Postgres data between restarts.

---

## Code Style & Quality Rules

- Type hints on every function signature.
- `structlog` for structured JSON logging (not `print`, not bare `logging`).
- Pydantic models for all API response parsing (fail fast on schema changes).
- No global mutable state. Pass dependencies (DB session, HTTP session) explicitly.
- Each module is independently testable. Business logic must not depend on live network.
- Tests live in `tests/` and use `pytest-asyncio` + `respx` for HTTP mocking.

---

## Build Order

When implementing, follow this sequence:

1. `config.py` — env/settings first
2. `db_models.py` + Alembic migration
3. `scanner.py` — wallet discovery
4. `paper_trader.py` — simulated execution
5. `monitor.py` — WebSocket listener
6. `pnl_tracker.py` — background resolution checker
7. `bot.py` — wires all modules together
8. `Dockerfile` + `docker-compose.yml` + `requirements.txt`

Do not skip ahead. Each module must be complete and correct before the next begins.

---

## What NOT to Build

- No REST API or web dashboard
- No Telegram/Discord/email alerts
- No real wallet signing or MATIC/USDC transfers
- No ML models or predictive scoring
- No frontend of any kind
- No Redis, Kafka, or message queues
- No multi-exchange support
