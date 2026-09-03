"""Command line interface: discover / run / stats."""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx

from . import resolver
from .api import Polymarket
from .config import (
    BOOK_DEPTH,
    CLOB,
    FINAL_STATS_DELAY,
    FLASHSCORE_HOST,
    GAMMA,
    HEARTBEAT,
    IDLE_INTERVAL,
    POLL_INTERVAL,
    REFRESH_INTERVAL,
    STALE_AFTER,
    STATS_INTERVAL,
    TOURS,
)
from .discovery import discover
from .poller import Poller
from .scores import Flashscore
from .store import Store

DEFAULT_DB = "data/tennis.db"


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _tours(args: argparse.Namespace) -> tuple[str, ...]:
    """Which circuits this invocation captures. Both unless told otherwise."""
    return TOURS if args.tour == "both" else (args.tour,)


def cmd_discover(args: argparse.Namespace) -> int:
    with Polymarket() as api, Flashscore() as scores:
        kept, skipped = discover(
            api,
            scores.board(),
            all_markets=args.all_markets,
            include_qualifying=args.include_qualifying,
            live_only=args.live_only,
            tours=_tours(args),
        )

    if args.json:
        print(
            json.dumps(
                {"kept": [_as_dict(m) for m in kept], "skipped": [vars(s) for s in skipped]},
                indent=2,
            )
        )
        return 0

    print(f"\n{len(kept)} market(s) to capture:\n")
    # Keyed by tour as well as name: a combined event runs both draws under one
    # tournament name, and they are two different draws.
    by_tournament: dict[tuple[str, str], list] = {}
    for market in kept:
        by_tournament.setdefault((market.tour, market.tournament), []).append(market)
    for (tour, tournament), markets in sorted(by_tournament.items()):
        print(f"  [{tour.upper()}] {tournament} ({markets[0].tier}) -- {len(markets)} market(s)")
        for market in markets:
            status = market.state.upper() if market.state == "live" else market.state
            detail = f" {market.period} {market.score}" if market.state == "live" else ""
            print(f"    [{status}{detail}]  {market.question}")
            print(f"                {market.outcomes[0]}  vs  {market.outcomes[1]}")
        print()
    if skipped:
        print(f"Skipped ({len(skipped)}):")
        for skip in skipped[:20]:
            print(f"  [{skip.reason}] {skip.title}")
        if len(skipped) > 20:
            print(f"  ... and {len(skipped) - 20} more")
    if not kept:
        print("  nothing matched -- no tour-level matches open right now,")
        print("  or a tournament is missing from the calendars in config.py")
    print()
    return 0


def _as_dict(market: object) -> dict[str, object]:
    data = dict(vars(market))
    data.pop("raw", None)
    return data


def cmd_run(args: argparse.Namespace) -> int:
    with Polymarket() as api, Flashscore() as scores, Store(args.db) as store:
        poller = Poller(
            api,
            store,
            scores=scores,
            interval=args.interval,
            idle_interval=args.idle_interval,
            refresh_interval=args.refresh,
            all_markets=args.all_markets,
            include_qualifying=args.include_qualifying,
            live_only=not args.include_upcoming,
            only_changes=not args.every_tick,
            record_stats=not args.no_stats,
            stats_interval=args.stats_interval,
            heartbeat=args.heartbeat,
            stale_after=args.stale_after,
            tours=_tours(args),
        )
        logging.info(
            "capturing depth-%d %s books, scores%s into %s: every %.0fs while a match "
            "is in play, every %.0fs before it starts, never once it is over "
            "(%s matches, %s)",
            BOOK_DEPTH,
            "/".join(t.upper() for t in _tours(args)),
            "" if args.no_stats else " and match statistics",
            args.db,
            args.interval,
            args.idle_interval,
            "live + upcoming" if args.include_upcoming else "live",
            "every tick" if args.every_tick else "on change",
        )
        poller.run()
    return 0


def cmd_backfill_book_ts(args: argparse.Namespace) -> int:
    """Reconstruct book_ts for rows recorded before it was stored."""
    if args.apply and not _capture_is_idle(args.db):
        print(
            "\na capture looks like it is writing to this database.\n"
            "stop it first: two writers would fight over the same rows.\n",
            file=sys.stderr,
        )
        return 2

    with Store(args.db) as store:
        summary = store.backfill_book_ts(apply=args.apply, batch=args.batch)

    rows, pending = int(summary["rows"]), int(summary["pending"])
    if not rows:
        print("\nno books recorded yet\n")
        return 0
    if not pending:
        print(f"\n{rows} book rows, all already carry book_ts -- nothing to do\n")
        return 0
    if args.apply:
        print(f"\n{rows} book rows, filled {summary['filled']} from runs of equal book_hash")
        print("marked book_ts_derived = 1: accurate to one polling interval,")
        print("and it cannot see a change that happened while nothing was recording\n")
    else:
        print(f"\n{rows} book rows, {pending} without book_ts")
        print("nothing changed -- pass --apply to do it\n")
    return 0


def cmd_clean_scores(args: argparse.Namespace) -> int:
    """Undo the damage stale feed reads did to score_events before the ratchet."""
    if args.apply and not _capture_is_idle(args.db):
        print(
            "\na capture looks like it is writing to this database.\n"
            "stop it first: two writers would fight over the same rows.\n",
            file=sys.stderr,
        )
        return 2

    with Store(args.db) as store:
        summary = store.prune_score_events(apply=args.apply)

    rows, removing = summary["rows"], summary["removing"]
    if not rows:
        print("\nno score history recorded yet\n")
        return 0
    share = 100 * int(removing) / int(rows)
    verb = "removed" if args.apply else "would remove"
    print(f"\n{rows} score rows, {verb} {removing} ({share:.0f}%) across {summary['matches']} match(es)")
    if args.apply:
        print("markets brought back in step with what survives\n")
    else:
        print("nothing changed -- pass --apply to do it\n")
    return 0


def _capture_is_idle(db: str) -> bool:
    """True if nothing has written to the database in the last minute.

    Not a lock: SQLite would let both write and simply interleave them. This is
    the cheap check that catches the real mistake, which is running the cleanup
    with the capture still going.
    """
    try:
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
            latest = conn.execute("SELECT MAX(ts) FROM books").fetchone()[0]
    except sqlite3.Error:
        return True
    return latest is None or (time.time() - latest) > 60


def cmd_stats(args: argparse.Namespace) -> int:
    with Store(args.db) as store:
        stats = store.stats()

    def fmt(ts: object) -> str:
        if not isinstance(ts, (int, float)):
            return "-"
        return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")

    print(f"\ndb        {stats['db']}")
    print(f"markets   {stats['markets']}")
    print(f"snapshots {stats['snapshots']}")
    print(f"stats     {stats['stat_events']} change(s), {stats['set_stats']} match(es) with final per-set")
    print(f"window    {fmt(stats['first_ts'])} -> {fmt(stats['last_ts'])}\n")
    rows = stats["by_tournament"]
    if isinstance(rows, list) and rows:
        print(f"  {'tour':<5} {'tournament':<20} {'markets':>8} {'snapshots':>10}")
        for tour, tournament, markets, snaps in rows:
            label = (tour or "-").upper()
            print(f"  {label:<5} {(tournament or '-'):<20} {markets:>8} {snaps:>10}")
    print()
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    """Serve the web dashboard over an existing capture database.

    Read-only, like `sql`, so this is safe to leave open beside a running
    capture -- it can neither block a write nor damage the recording.
    """
    from .dashboard import serve

    path = Path(args.db)
    if not path.exists():
        print(f"no database at {path} -- has `polymarket run` been started?", file=sys.stderr)
        return 1

    serve(path, host=args.host, port=args.port, open_browser=not args.no_open)
    return 0


LATEST_QUERY = """
SELECT utc_time, question, outcome, sell_price AS bid, buy_price AS ask, spread
FROM quotes
WHERE ts = (SELECT MAX(ts) FROM books)
ORDER BY question, outcome
"""


def cmd_sql(args: argparse.Namespace) -> int:
    """Run a query against the database while it is being written to.

    Opened read-only, so this can never interfere with a running capture -- and
    a typo that happens to be valid SQL cannot damage the recording.
    """
    query = args.query or LATEST_QUERY
    path = Path(args.db)
    if not path.exists():
        print(f"no database at {path} -- has `polymarket run` been started?", file=sys.stderr)
        return 1

    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        cursor = conn.execute(query)
        rows = cursor.fetchall()
        headers = [d[0] for d in cursor.description or []]
    except sqlite3.Error as exc:
        print(f"query failed: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    if not rows:
        print("(no rows)")
        return 0

    def cell(value: object) -> str:
        if isinstance(value, float):
            value = round(value, 6)
        text = "NULL" if value is None else str(value)
        return text[:40]

    table = [headers] + [[cell(v) for v in row] for row in rows]
    widths = [max(len(r[i]) for r in table) for i in range(len(headers))]
    print()
    print("  " + "  ".join(h.ljust(w) for h, w in zip(table[0], widths)))
    print("  " + "  ".join("-" * w for w in widths))
    for row in table[1:]:
        print("  " + "  ".join(v.ljust(w) for v, w in zip(row, widths)))
    print(f"\n{len(rows)} row(s)\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    # Shared flags live on the subcommands, not on the top-level parser, so that
    # every flag is written after the command: "polymarket run --db x --interval 5".
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-v", "--verbose", action="store_true")
    common.add_argument("--db", default=DEFAULT_DB, help=f"SQLite path (default {DEFAULT_DB})")
    common.add_argument(
        "--all-markets",
        action="store_true",
        help="also capture per-match derivatives (set winner, games/sets over-under)",
    )
    common.add_argument(
        "--include-qualifying",
        action="store_true",
        help="also capture qualifying-round matches",
    )
    common.add_argument(
        "--tour",
        choices=("atp", "wta", "both"),
        default="both",
        help="which circuit to capture (default: both)",
    )
    common.add_argument(
        "--dns",
        choices=("auto", "always", "never"),
        default="auto",
        help="resolve API hostnames over DNS-over-HTTPS when the system resolver "
        "cannot (default: auto)",
    )

    parser = argparse.ArgumentParser(
        prog="polymarket",
        description="Capture ATP and WTA tennis order books from Polymarket",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_discover = sub.add_parser(
        "discover", parents=[common], help="list the matches that would be captured"
    )
    p_discover.add_argument("--json", action="store_true")
    p_discover.add_argument(
        "--live-only",
        action="store_true",
        help="show only matches currently being played (what `run` captures)",
    )
    p_discover.set_defaults(func=cmd_discover, needs_network=True)

    p_run = sub.add_parser("run", parents=[common], help="start the capture loop")
    p_run.add_argument(
        "--interval",
        type=float,
        default=POLL_INTERVAL,
        help="seconds between polls of a match in play -- its order books and its "
        f"score, read together on the same tick (default {POLL_INTERVAL:.0f})",
    )
    p_run.add_argument(
        "--idle-interval",
        type=float,
        default=IDLE_INTERVAL,
        help="seconds between polls of a match that has not started; a finished "
        f"match is not polled at all (default {IDLE_INTERVAL:.0f})",
    )
    p_run.add_argument("--refresh", type=float, default=REFRESH_INTERVAL)
    p_run.add_argument(
        "--include-upcoming",
        action="store_true",
        help="also poll matches that have not started yet (default: live matches only)",
    )
    p_run.add_argument(
        "--every-tick",
        action="store_true",
        help="write a snapshot every tick, including when the book has not moved",
    )
    p_run.add_argument(
        "--no-stats",
        action="store_true",
        help="do not record match statistics -- aces, winners, points won and the rest. "
        "They cost one extra Flashscore read per live match per tick, and a final "
        f"per-set reading {FINAL_STATS_DELAY / 3600:.0f}h after each match ends",
    )
    p_run.add_argument(
        "--stats-interval",
        type=float,
        default=STATS_INTERVAL,
        help="floor between statistics reads of one match, seconds; a point takes about "
        f"26s, so this is oversampling already (default {STATS_INTERVAL:.0f})",
    )
    p_run.add_argument(
        "--heartbeat",
        type=float,
        default=HEARTBEAT,
        help=f"write an unchanged book at least this often, seconds (default {HEARTBEAT:.0f})",
    )
    p_run.add_argument(
        "--stale-after",
        type=float,
        default=STALE_AFTER,
        help="warn when every in-play book on a tick is at least this far behind its own"
        f" upstream timestamp, seconds (default {STALE_AFTER:.0f})",
    )
    p_run.set_defaults(func=cmd_run, needs_network=True)

    p_stats = sub.add_parser(
        "stats", parents=[common], help="summarize what has been captured"
    )
    p_stats.set_defaults(func=cmd_stats, needs_network=False)

    p_clean = sub.add_parser(
        "clean-scores",
        parents=[common],
        help="remove score rows that a stale feed read wrote backwards",
    )
    p_clean.add_argument(
        "--apply",
        action="store_true",
        help="actually delete them; without this it only reports what it would do",
    )
    p_clean.set_defaults(func=cmd_clean_scores, needs_network=False)

    p_fill = sub.add_parser(
        "backfill-book-ts",
        parents=[common],
        help="reconstruct book_ts for rows recorded before it was stored",
    )
    p_fill.add_argument(
        "--apply",
        action="store_true",
        help="actually write them; without this it only reports what it would do",
    )
    p_fill.add_argument(
        "--batch",
        type=int,
        default=50_000,
        help="rows per transaction, so a running capture is never blocked for long"
        " (default 50000)",
    )
    p_fill.set_defaults(func=cmd_backfill_book_ts, needs_network=False)

    p_sql = sub.add_parser(
        "sql", parents=[common], help="query the database (read-only, safe while recording)"
    )
    p_sql.add_argument(
        "query",
        nargs="?",
        help="SQL to run; omit to show the most recent quote for every match",
    )
    p_sql.set_defaults(func=cmd_sql, needs_network=False)

    p_dash = sub.add_parser(
        "dashboard", parents=[common], help="browse the capture in a browser (read-only)"
    )
    p_dash.add_argument("--port", type=int, default=8787)
    p_dash.add_argument("--host", default="127.0.0.1", help="bind address (default: localhost only)")
    p_dash.add_argument("--no-open", action="store_true", help="do not open a browser")
    p_dash.set_defaults(func=cmd_dashboard, needs_network=False)

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    if args.needs_network:
        resolver.ensure(
            [
                urlparse(GAMMA).hostname or "",
                urlparse(CLOB).hostname or "",
                urlparse(FLASHSCORE_HOST).hostname or "",
            ],
            args.dns,
        )
    try:
        return int(args.func(args))
    except httpx.ConnectError as exc:
        # Overwhelmingly this is DNS: several ISPs return NXDOMAIN for
        # polymarket.com, so the name fails to resolve while everything else works.
        print(f"\ncannot reach the Polymarket API: {exc}", file=sys.stderr)
        print("check whether the name resolves at all:", file=sys.stderr)
        print("    getent hosts gamma-api.polymarket.com", file=sys.stderr)
        print("    nslookup gamma-api.polymarket.com 1.1.1.1", file=sys.stderr)
        print(
            "if the second works and the first does not, your resolver is "
            "blocking the domain -- see DNS.md.\n",
            file=sys.stderr,
        )
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
