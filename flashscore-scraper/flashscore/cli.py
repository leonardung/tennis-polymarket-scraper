"""Print live tennis scores from Flashscore.

    python -m flashscore.cli              # live matches, once
    python -m flashscore.cli --watch 15   # refresh every 15 seconds
    python -m flashscore.cli --all        # every match today, not just live
    python -m flashscore.cli --json       # machine-readable
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import time

import httpx

from .feed import fetch_tennis, refresh_live
from .models import Match


def render(matches: list[Match]) -> str:
    if not matches:
        return "No matches."
    lines = []
    tournament = None
    for match in matches:
        if match.tournament != tournament:
            tournament = match.tournament
            lines.append(f"\n{tournament}")
        home = f"{match.home.name}{'*' if match.home.serving else ''}"
        away = f"{match.away.name}{'*' if match.away.serving else ''}"
        sets = ""
        if match.home_sets is not None:
            sets = f"{match.home_sets}-{match.away_sets}"
        points = match.point_line()
        lines.append(
            f"  {match.status:<12} {home:>26} {sets:^5} {away:<26}"
            f"  {match.score_line():<24} {points}"
        )
    return "\n".join(lines)


def as_json(matches: list[Match]) -> str:
    def encode(obj):
        if hasattr(obj, "isoformat"):
            return obj.isoformat()
        raise TypeError(type(obj))

    return json.dumps([dataclasses.asdict(m) for m in matches], default=encode, indent=2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Live tennis scores from Flashscore")
    parser.add_argument("--all", action="store_true", help="include non-live matches")
    parser.add_argument("--day", type=int, default=0, help="day offset from today")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument(
        "--watch",
        type=float,
        metavar="SECONDS",
        help="refresh on an interval instead of printing once",
    )
    parser.add_argument(
        "--no-refresh",
        action="store_true",
        help="skip the per-match feeds; one request, but minutes behind on live scores",
    )
    args = parser.parse_args(argv)

    with httpx.Client(timeout=20.0) as client:
        while True:
            try:
                matches = fetch_tennis(day=args.day, client=client)
                if not args.no_refresh:
                    refresh_live(matches, client=client)
            except httpx.HTTPError as exc:
                print(f"fetch failed: {exc}", file=sys.stderr)
                if args.watch is None:
                    return 1
                time.sleep(args.watch)
                continue

            if not args.all:
                matches = [m for m in matches if m.is_live]

            output = as_json(matches) if args.json else render(matches)
            if args.watch is not None and not args.json:
                # Clear screen so the table refreshes in place.
                print("\033[2J\033[H", end="")
                print(f"{len(matches)} matches", end="")
            print(output)

            if args.watch is None:
                return 0
            time.sleep(args.watch)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyboardInterrupt, BrokenPipeError):
        # Ctrl-C out of --watch, or a downstream `head` closing the pipe.
        sys.stderr.close()
        raise SystemExit(130)
