"""
report.py — Terminal trade report.

Usage (from host):
    docker compose exec app python -m app.report

Optional flags:
    --open        show only OPEN trades
    --resolved    show only RESOLVED/INVALID trades (default: all)
    --limit N     cap rows per table (default 50)
    --leader 0x…  filter to a single leader wallet
"""
import argparse
import os
import sys
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box

console = Console()


# ---------------------------------------------------------------------------
# DB connection
# ---------------------------------------------------------------------------

def _connect() -> psycopg2.extensions.connection:
    url = os.environ.get(
        "DATABASE_URL",
        "postgresql+asyncpg://poly:poly@db:5432/polymarket",
    )
    # psycopg2 needs the plain postgresql:// scheme
    dsn = url.replace("+asyncpg", "").replace("postgresql://", "postgresql://")
    return psycopg2.connect(dsn, cursor_factory=psycopg2.extras.RealDictCursor)


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

_SUMMARY_SQL = """
SELECT
    COUNT(*)                                            AS total,
    COUNT(*) FILTER (WHERE status = 'OPEN')             AS open,
    COUNT(*) FILTER (WHERE status = 'RESOLVED')         AS resolved,
    COUNT(*) FILTER (WHERE status = 'INVALID')          AS invalid,
    COUNT(*) FILTER (WHERE pnl_usd > 0)                 AS wins,
    COUNT(*) FILTER (WHERE pnl_usd < 0)                 AS losses,
    ROUND(SUM(pnl_usd)::numeric, 2)                     AS total_pnl,
    ROUND(AVG(pnl_usd) FILTER (WHERE pnl_usd IS NOT NULL)::numeric, 4) AS avg_pnl,
    ROUND(
        100.0 * COUNT(*) FILTER (WHERE pnl_usd > 0)
        / NULLIF(COUNT(*) FILTER (WHERE status = 'RESOLVED'), 0),
        1
    )                                                   AS win_rate_pct
FROM simulated_trades
"""

_TRADES_SQL = """
SELECT
    t.id,
    l.wallet_address,
    t.market_id,
    t.token_side,
    t.simulated_price,
    t.simulated_size_usd,
    t.pnl_usd,
    t.status,
    t.resolution_outcome,
    t.opened_at,
    t.resolved_at
FROM simulated_trades t
JOIN leaders l ON l.id = t.leader_id
{where}
ORDER BY t.opened_at DESC
LIMIT %(limit)s
"""


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _pnl_str(pnl) -> str:
    if pnl is None:
        return "[dim]—[/dim]"
    v = float(pnl)
    colour = "green" if v > 0 else "red"
    sign = "+" if v > 0 else ""
    return f"[{colour}]{sign}{v:.4f}[/{colour}]"


def _status_str(status: str) -> str:
    colours = {"OPEN": "yellow", "RESOLVED": "green", "INVALID": "dim"}
    c = colours.get(status, "white")
    return f"[{c}]{status}[/{c}]"


def _ts(dt) -> str:
    if dt is None:
        return "—"
    if isinstance(dt, datetime):
        return dt.strftime("%Y-%m-%d %H:%M")
    return str(dt)


def _short(s: str, n: int = 10) -> str:
    return s[:n] + "…" if len(s) > n else s


# ---------------------------------------------------------------------------
# Display sections
# ---------------------------------------------------------------------------

def _show_summary(cur) -> None:
    cur.execute(_SUMMARY_SQL)
    r = cur.fetchone()

    total_pnl = float(r["total_pnl"] or 0)
    pnl_colour = "green" if total_pnl >= 0 else "red"
    pnl_sign = "+" if total_pnl > 0 else ""

    lines = [
        f"[bold]Trades:[/bold]  {r['total']} total  "
        f"([yellow]{r['open']} open[/yellow] / "
        f"[green]{r['resolved']} resolved[/green] / "
        f"[dim]{r['invalid']} invalid[/dim])",
        f"[bold]Wins/Losses:[/bold]  [green]{r['wins']} wins[/green] / "
        f"[red]{r['losses']} losses[/red]  "
        f"([bold]Win rate: {r['win_rate_pct'] or 0:.1f}%[/bold])",
        f"[bold]Total PnL:[/bold]  [{pnl_colour}]{pnl_sign}{total_pnl:.2f} USD[/{pnl_colour}]  "
        f"[dim](avg per resolved: {float(r['avg_pnl'] or 0):+.4f})[/dim]",
    ]
    console.print(Panel("\n".join(lines), title="[bold white]Paper Trading Summary[/bold white]", box=box.ROUNDED))


def _make_table(title: str, colour: str) -> Table:
    t = Table(
        title=title,
        box=box.SIMPLE_HEAD,
        show_lines=False,
        header_style=f"bold {colour}",
        title_style=f"bold {colour}",
        expand=False,
    )
    t.add_column("Opened",       style="dim",    min_width=16)
    t.add_column("Leader",       min_width=12)
    t.add_column("Market",       min_width=14)
    t.add_column("Side",         min_width=4)
    t.add_column("Price",        justify="right", min_width=6)
    t.add_column("Size $",       justify="right", min_width=6)
    t.add_column("PnL $",        justify="right", min_width=10)
    t.add_column("Status",       min_width=8)
    t.add_column("Resolved",     style="dim",    min_width=16)
    return t


def _add_row(table: Table, r: dict) -> None:
    table.add_row(
        _ts(r["opened_at"]),
        _short(r["wallet_address"], 12),
        _short(r["market_id"], 14),
        r["token_side"],
        f"{float(r['simulated_price']):.4f}",
        f"{float(r['simulated_size_usd']):.2f}",
        _pnl_str(r["pnl_usd"]),
        _status_str(r["status"]),
        _ts(r["resolved_at"]),
    )


def _show_trades(cur, title: str, colour: str, where_clause: str, params: dict) -> None:
    sql = _TRADES_SQL.format(where=f"WHERE {where_clause}" if where_clause else "")
    cur.execute(sql, params)
    rows = cur.fetchall()

    table = _make_table(f"{title} ({len(rows)})", colour)
    for r in rows:
        _add_row(table, r)

    if rows:
        console.print(table)
    else:
        console.print(f"[dim]No {title.lower()} found.[/dim]")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket paper-trade report")
    parser.add_argument("--open",     action="store_true", help="Show only OPEN trades")
    parser.add_argument("--resolved", action="store_true", help="Show only closed trades")
    parser.add_argument("--limit",    type=int, default=50, metavar="N")
    parser.add_argument("--leader",   type=str, default=None, metavar="0x…")
    args = parser.parse_args()

    try:
        conn = _connect()
    except Exception as exc:
        console.print(f"[red]DB connection failed:[/red] {exc}")
        sys.exit(1)

    with conn, conn.cursor() as cur:
        _show_summary(cur)

        leader_filter = "l.wallet_address = %(leader)s" if args.leader else ""
        base_params = {"limit": args.limit, "leader": args.leader}

        show_open     = args.open or (not args.open and not args.resolved)
        show_resolved = args.resolved or (not args.open and not args.resolved)

        if show_resolved:
            # Wins
            win_where = "t.pnl_usd > 0"
            if leader_filter:
                win_where = f"{leader_filter} AND {win_where}"
            _show_trades(cur, "Winning Trades", "green", win_where, base_params)

            # Losses
            loss_where = "t.pnl_usd < 0"
            if leader_filter:
                loss_where = f"{leader_filter} AND {loss_where}"
            _show_trades(cur, "Losing Trades", "red", loss_where, base_params)

            # Invalid
            inv_where = "t.status = 'INVALID'"
            if leader_filter:
                inv_where = f"{leader_filter} AND {inv_where}"
            _show_trades(cur, "Invalid / Void Trades", "dim", inv_where, base_params)

        if show_open:
            open_where = "t.status = 'OPEN'"
            if leader_filter:
                open_where = f"{leader_filter} AND {open_where}"
            _show_trades(cur, "Open Trades", "yellow", open_where, base_params)

    conn.close()


if __name__ == "__main__":
    main()
