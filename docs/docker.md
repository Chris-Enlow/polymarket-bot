# Docker Guide

## Why Docker?

The bot needs two things running together: the **Python app** and a **PostgreSQL database**. Without Docker, you'd have to manually install Postgres on your machine, create the database and user, configure the connection, and make sure the DB is running before the app starts. Docker automates all of that.

Docker packages everything into isolated containers — the app gets one container, the database gets another. They can talk to each other over a private network Docker creates automatically.

---

## What `docker-compose.yml` Does Specifically

- **`db` service** — starts a Postgres 16 container with a pre-created database (`polymarket`), user (`poly`), and password (`poly`). Data is saved to a named volume (`pgdata`) so it persists when you restart.
- **`app` service** — builds the Python image from the `Dockerfile`, waits for Postgres to pass a health check, then runs the bot.
- **Health check** — Docker pings Postgres every 5 seconds until it accepts connections, then starts the app. This prevents the race condition where the app starts before the DB is ready.

---

## Commands

**First time (or after code changes):**
```bash
docker compose up --build
```
The `--build` flag rebuilds the Python image so your latest code is included.

**Subsequent runs (no code changes):**
```bash
docker compose up
```

**Run in background:**
```bash
docker compose up -d
```
Then check logs with:
```bash
docker compose logs -f app
```

**Stop everything:**
```bash
docker compose down
```

**Stop and wipe the database:**
```bash
docker compose down -v
```
The `-v` flag deletes the `pgdata` volume. Use this if you want a clean slate.

---

## What Happens on Startup (in order)

1. Postgres container starts and passes health check
2. Python container starts, runs `alembic upgrade head` — creates the `leaders` and `simulated_trades` tables if they don't exist
3. Bot performs the initial leader scan (blocks until complete)
4. WebSocket monitor, scanner loop, trade workers, and PnL tracker all launch concurrently

If you see `bot_running` in the logs, everything started correctly.
