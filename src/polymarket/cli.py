"""Command line interface: discover / run / stats."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

from . import resolver
from .api import Polymarket
from .config import BOOK_DEPTH, CLOB, GAMMA, POLL_INTERVAL, REFRESH_INTERVAL
from .discovery import discover
from .poller import Poller
from .store import Store

DEFAULT_DB = "data/tennis.db"


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def cmd_discover(args: argparse.Namespace) -> int:
    with Polymarket() as api:
        kept, skipped = discover(
            api, all_markets=args.all_markets, include_qualifying=args.include_qualifying
        )

    if args.json:
        print(
            json.dumps(
                {"kept": [_as_dict(m) for m in kept], "skipped": [vars(s) for s in skipped]},
                indent=2,
            )
        )
        return 0

    print(f"\n{len(kept)} ATP market(s) to capture:\n")
    by_tournament: dict[str, list] = {}
    for market in kept:
        by_tournament.setdefault(market.tournament, []).append(market)
    for tournament, markets in sorted(by_tournament.items()):
        print(f"  {tournament} ({markets[0].tier}) -- {len(markets)} market(s)")
        for market in markets:
            print(f"    {market.match_date}  {market.question}")
            print(f"                {market.outcomes[0]}  vs  {market.outcomes[1]}")
        print()
    if skipped:
        print(f"Skipped ({len(skipped)}):")
        for skip in skipped[:20]:
            print(f"  [{skip.reason}] {skip.title}")
        if len(skipped) > 20:
            print(f"  ... and {len(skipped) - 20} more")
    if not kept:
        print("  nothing matched -- no ATP tour matches open right now,")
        print("  or a tournament is missing from TOURNAMENTS in config.py")
    print()
    return 0


def _as_dict(market: object) -> dict[str, object]:
    data = dict(vars(market))
    data.pop("raw", None)
    return data


def cmd_run(args: argparse.Namespace) -> int:
    with Polymarket() as api, Store(args.db) as store:
        poller = Poller(
            api,
            store,
            interval=args.interval,
            refresh_interval=args.refresh,
            all_markets=args.all_markets,
            include_qualifying=args.include_qualifying,
            only_changes=args.only_changes,
        )
        logging.info(
            "capturing depth-%d books every %.0fs into %s",
            BOOK_DEPTH,
            args.interval,
            args.db,
        )
        poller.run()
    return 0


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
    print(f"window    {fmt(stats['first_ts'])} -> {fmt(stats['last_ts'])}\n")
    rows = stats["by_tournament"]
    if isinstance(rows, list) and rows:
        print(f"  {'tournament':<20} {'markets':>8} {'snapshots':>10}")
        for tournament, markets, snaps in rows:
            print(f"  {(tournament or '-'):<20} {markets:>8} {snaps:>10}")
    print()
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
        "--dns",
        choices=("auto", "always", "never"),
        default="auto",
        help="resolve API hostnames over DNS-over-HTTPS when the system resolver "
        "cannot (default: auto)",
    )

    parser = argparse.ArgumentParser(
        prog="polymarket", description="Capture ATP tennis order books from Polymarket"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_discover = sub.add_parser(
        "discover", parents=[common], help="list the matches that would be captured"
    )
    p_discover.add_argument("--json", action="store_true")
    p_discover.set_defaults(func=cmd_discover, needs_network=True)

    p_run = sub.add_parser("run", parents=[common], help="start the capture loop")
    p_run.add_argument("--interval", type=float, default=POLL_INTERVAL)
    p_run.add_argument("--refresh", type=float, default=REFRESH_INTERVAL)
    p_run.add_argument(
        "--only-changes",
        action="store_true",
        help="skip writing a snapshot when the book hash is unchanged",
    )
    p_run.set_defaults(func=cmd_run, needs_network=True)

    p_stats = sub.add_parser(
        "stats", parents=[common], help="summarize what has been captured"
    )
    p_stats.set_defaults(func=cmd_stats, needs_network=False)

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    if args.needs_network:
        resolver.ensure(
            [urlparse(GAMMA).hostname or "", urlparse(CLOB).hostname or ""], args.dns
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
