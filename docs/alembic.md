# Alembic Guide

## What Alembic Is

Alembic is a database migration tool for SQLAlchemy. It tracks the history of changes to your database schema — think of it like Git, but for your database structure.

Without it, if you add a new column or table, you'd have to manually write and run SQL against every environment (local, staging, prod). Alembic automates that.

---

## The Files

### `alembic.ini`
The config file. The only important line is:
```ini
sqlalchemy.url = postgresql+psycopg2://poly:poly@localhost:5432/polymarket
```
This is overridden at runtime by `alembic/env.py` using the `DATABASE_URL` from `.env`, so the hardcoded value here mostly doesn't matter.

---

### `alembic/env.py`
The script Alembic runs when you execute any `alembic` command. It:
1. Reads your `DATABASE_URL` from `.env`
2. Connects to Postgres using a **synchronous** psycopg2 connection (not async — Alembic doesn't support async)
3. Imports your ORM models from `app/db_models.py` so it knows what the schema should look like
4. Runs pending migrations in order

---

### `alembic/versions/0001_initial_schema.py`
The first (and currently only) migration. It contains two functions:

- **`upgrade()`** — runs when migrating forward. Creates the `leaders` and `simulated_trades` tables with all their columns, types, and constraints.
- **`downgrade()`** — runs if you ever roll back. Drops both tables.

Each migration file has a revision ID (e.g. `0001`) and a `down_revision` pointer to the previous one. This chain is how Alembic knows what order to apply them.

---

## How It Gets Used

In `bot.py`, before the async event loop starts:
```python
_run_migrations()   # runs "alembic upgrade head"
asyncio.run(main())
```

`upgrade head` means "apply every migration that hasn't been applied yet." If the tables already exist, it does nothing. Alembic tracks what's been run in a `alembic_version` table it creates in your database.

---

## If You Change the Schema

1. Edit `app/db_models.py` with your new columns/tables
2. Run: `alembic revision --autogenerate -m "describe the change"`
3. Alembic diffs your models against the live DB and generates a new file in `alembic/versions/`
4. Review the generated file, then it will be applied automatically on the next `docker compose up`
