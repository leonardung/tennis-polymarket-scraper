"""Read-only queries behind the dashboard.

Every connection is opened ``mode=ro``, so the dashboard can never block or
damage a capture that is running against the same file -- the same guarantee
``polymarket sql`` gives.

Two reconstructions happen here rather than in the browser, because both depend
on how the capture writes rather than on how a chart draws:

* **Forward fill.** A snapshot is only written when the book moved, so the two
  players have different timestamps and a gap means "unchanged", not "unknown".
  Carrying the last row forward across a shared time grid is the faithful way to
  read the table back, not a smoothing choice.
* **Last-trade orientation.** ``market_last_trade`` is per match and oriented to
  whichever side traded last, so the stored number is the opponent's price on one
  of the two rows and the column alone cannot say which. See ``_orient``.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from ..config import BOOK_DEPTH, STATISTICS

# A capture refreshes its market list every 5 minutes and writes a heartbeat on
# the same cadence, so nothing older than this can be from a collector that is
# still running.
STALE_AFTER = 900.0

# Tennis start times are "not before" times, so a match sitting past its slot is
# normal for a while. Past this, an upcoming match that never went live was not
# postponed -- it was played and finished while nothing was watching.
OVERDUE_LIMIT = 12 * 3600.0

_LEVELS = range(1, BOOK_DEPTH + 1)
_DEPTH_COLUMNS = [f"{side}_{kind}_{i}" for side in ("bid", "ask") for kind in ("px", "sz") for i in _LEVELS]

_BOOK_FIELDS = ["ts", "outcome_index", "outcome", "best_bid", "best_ask", "mid", "spread", *_DEPTH_COLUMNS, "market_last_trade"]


class MissingDatabase(Exception):
    """No capture database at the configured path."""


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    if not path.exists():
        raise MissingDatabase(str(path))
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ?", (name,)
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------- state


def _epoch(iso: str | None) -> float | None:
    """Parse an ISO timestamp from the API into a unix epoch, or None."""
    if not iso:
        return None
    try:
        when = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.timestamp()


def classify(state: str | None, start_time: str | None, last_seen: float | None, now: float) -> str:
    """Sort a match into the live / upcoming / past tab.

    ``markets.state`` is the score feed's own verdict, but it is only as fresh as
    the last refresh that saw the match. A match that finished, or that Polymarket
    resolved and dropped from the API, keeps whatever state it had when it was
    last seen -- so "live" on a row nothing has touched for an hour means the
    capture stopped watching, not that play is still going on.
    """
    fresh = last_seen is not None and (now - last_seen) < STALE_AFTER
    starts = _epoch(start_time)

    if state == "ended":
        return "past"
    if state == "live":
        return "live" if fresh else "past"
    # upcoming, or a state the feed has not given us
    if starts is None:
        return "upcoming" if fresh else "past"
    if starts > now:
        return "upcoming"
    # Past its slot: still upcoming while the delay is plausible, otherwise it
    # was played out of sight.
    if fresh and (now - starts) < OVERDUE_LIMIT:
        return "upcoming"
    return "past"


# ---------------------------------------------------------------- overview


# Everything a card needs is read per match, as index seeks: the newest book per
# player (books_by_outcome, walked backwards), the capture span (two seeks on
# books_by_market), the sparkline tail, and the newest point score. Nothing here
# aggregates over `books` -- a COUNT(*) or GROUP BY across it walks a >1 GB index,
# tens of seconds on a cold page cache, and the overview is refetched on every
# tick. The snapshot count lives in match_detail, where it is one match's range.
# Per match rather than set-based so a page of past matches costs only its page.
_LATEST_BOOK = """
SELECT ts, outcome_index, outcome, best_bid, best_ask, mid, spread, market_last_trade
FROM books WHERE condition_id = ? AND outcome_index = ?
ORDER BY ts DESC LIMIT 1
"""

# MIN and MAX in separate subqueries: SQLite only turns a lone MIN or MAX into
# a single seek; together they would scan the match's range.
_SPAN = """
SELECT (SELECT MIN(ts) FROM books WHERE condition_id = :cid) AS first_ts,
       (SELECT MAX(ts) FROM books WHERE condition_id = :cid) AS last_ts
"""

# When a match's book was last written: what a finished card shows, and what the
# Past tab orders by. `start_time` is Polymarket's slot, which for a finished
# match is often wrong -- a cancelled match can keep a slot in the future.
_LAST_CAPTURE = "SELECT MAX(ts) FROM books WHERE condition_id = ?"

# The tail of one player's mid, for a card sparkline.
_SPARK = """
SELECT mid FROM books
WHERE condition_id = ? AND outcome_index = 0
ORDER BY ts DESC LIMIT ?
"""

# `markets` keeps only the set score, so the points on a card come from the tail
# of `score_events` -- (condition_id, ts) is its primary key.
_LATEST_POINTS = "SELECT game FROM score_events WHERE condition_id = ? ORDER BY ts DESC LIMIT 1"

# How many finished matches /api/past returns at most in one page.
PAGE_LIMIT = 200

SORTS = ("natural", "move", "span", "spread")


def _has_points(conn: sqlite3.Connection) -> bool:
    """`score_events` and its `game` column both arrived after the first captures,
    so an older file shows cards with no points rather than failing to load."""
    if not _has_table(conn, "score_events"):
        return False
    return "game" in {row[1] for row in conn.execute("PRAGMA table_info(score_events)")}


def _card(
    conn: sqlite3.Connection, market: sqlite3.Row, state: str, points: bool, spark_points: int
) -> dict[str, Any]:
    """One match card, from the market row plus a handful of seeks."""
    cid = market["condition_id"]
    books = {}
    for i in (0, 1):
        row = conn.execute(_LATEST_BOOK, (cid, i)).fetchone()
        if row is not None:
            books[i] = row
    span = conn.execute(_SPAN, {"cid": cid}).fetchone()
    spark = [r["mid"] for r in reversed(conn.execute(_SPARK, (cid, spark_points)).fetchall())]
    game = None
    if points:
        row = conn.execute(_LATEST_POINTS, (cid,)).fetchone()
        game = row["game"] if row else None

    return {
        "condition_id": cid,
        "question": market["question"],
        "tour": market["tour"],
        "tournament": market["tournament"],
        "tier": market["tier"],
        "market_type": market["market_type"],
        "players": [market["outcome_0"], market["outcome_1"]],
        "feed_state": market["state"],
        "state": state,
        "period": market["period"],
        "score": market["score"],
        "game": game,
        "start_time": market["start_time"],
        "start_epoch": _epoch(market["start_time"]),
        "last_seen": market["last_seen"],
        "prices": [_price_summary(books.get(i)) for i in (0, 1)],
        "last_trade": _oriented_last_trade(books),
        "first_ts": span["first_ts"],
        "last_ts": span["last_ts"],
        "spark": spark,
        "move": _move(spark),
    }


def _classified(conn: sqlite3.Connection, now: float) -> list[tuple[sqlite3.Row, str]]:
    """Every market with its tab. Reads `markets` only -- a few hundred rows."""
    return [
        (m, classify(m["state"], m["start_time"], m["last_seen"], now))
        for m in conn.execute("SELECT * FROM markets")
    ]


def overview(conn: sqlite3.Connection, spark_points: int = 100) -> dict[str, Any]:
    """Counts and filter lists for every match; cards for live and upcoming only.

    Finished matches are the bulk of a season's list and grow without bound, so
    their cards are served a page at a time by `past()` instead.
    """
    now = time.time()
    markets = _classified(conn, now)
    points = _has_points(conn)

    counts = {"live": 0, "upcoming": 0, "past": 0}
    for _, state in markets:
        counts[state] += 1

    matches = [_card(conn, m, state, points, spark_points) for m, state in markets if state != "past"]
    last_tick = conn.execute("SELECT MAX(ts) FROM books").fetchone()[0]

    return {
        "generated_at": now,
        "last_tick": last_tick,
        "capturing": last_tick is not None and (now - last_tick) < STALE_AFTER,
        "counts": counts,
        # Tournament names are shared across the two draws of a combined event,
        # so the two filters are independent: picking a name does not decide
        # which draw.
        "tours": sorted({m["tour"] for m, _ in markets if m["tour"]}),
        "tournaments": _tournaments(m for m, _ in markets),
        # The same list split by draw, so picking a tour narrows the tournament
        # list to that tour's events. A combined event appears under both, each
        # dated by its own draw.
        "tournaments_by_tour": {
            tour: _tournaments(m for m, _ in markets if m["tour"] == tour)
            for tour in sorted({m["tour"] for m, _ in markets if m["tour"]})
        },
        "matches": matches,
    }


def _tournaments(markets: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    """Tournament names with a start date, newest first -- this week's events on top.

    The date is the earliest scheduled slot among the tournament's matches. A slot
    can be wrong for one finished match (a cancelled match keeps a future one),
    but the earliest across a draw is a stable reading of when the event began.
    """
    start: dict[str, float | None] = {}
    for market in markets:
        name = market["tournament"]
        if not name:
            continue
        slot = _epoch(market["start_time"])
        known = start.get(name)
        start[name] = slot if known is None else (known if slot is None else min(known, slot))
    ordered = sorted(start.items(), key=lambda item: item[0])
    ordered.sort(key=lambda item: item[1] if item[1] is not None else float("-inf"), reverse=True)
    return [{"name": name, "start": first} for name, first in ordered]


def _matcher(tour: str, tournament: str, search: str):
    """The card list's filters as a predicate over a `markets` row.

    Tour and tournament are exact; search is a case-insensitive substring of the
    question or either player. Mirrors filterMatches() in App.tsx, which applies
    the same filters to the live and upcoming cards in the browser.
    """
    needle = search.strip().lower()

    def keep(market: sqlite3.Row) -> bool:
        if tour and market["tour"] != tour:
            return False
        if tournament and market["tournament"] != tournament:
            return False
        if needle:
            text = " ".join(filter(None, (market["question"], market["outcome_0"], market["outcome_1"])))
            return needle in text.lower()
        return True

    return keep


def counts(conn: sqlite3.Connection, tour: str = "", tournament: str = "", search: str = "") -> dict[str, int]:
    """How many matches each tab holds under the given filters -- `markets` only."""
    keep = _matcher(tour, tournament, search)
    tally = {"live": 0, "upcoming": 0, "past": 0}
    for market, state in _classified(conn, time.time()):
        if keep(market):
            tally[state] += 1
    return tally


def _sort_key(sort: str):
    """Descending keys, mirroring sortMatches() in App.tsx for the other tabs."""
    if sort == "move":
        return lambda c: abs(c["move"] or 0.0)
    if sort == "span":
        return lambda c: c["last_ts"] - c["first_ts"] if c["first_ts"] is not None and c["last_ts"] is not None else -1.0
    if sort == "spread":
        return lambda c: max((p["spread"] for p in c["prices"] if p and p["spread"] is not None), default=-1.0)
    raise ValueError(sort)


def past(
    conn: sqlite3.Connection,
    offset: int = 0,
    limit: int = 48,
    tour: str = "",
    tournament: str = "",
    search: str = "",
    sort: str = "natural",
    spark_points: int = 100,
) -> dict[str, Any]:
    """One page of finished matches, filtered and sorted before it is cut.

    The filters are the card list's own (tour, tournament, a case-insensitive
    substring of the question or either player) and run on `markets` alone. The
    default order -- most recently captured first, the time the card shows --
    costs one seek per filtered match, and only the page's cards are built. The
    other orders rank on prices or capture span, which means building every
    filtered card first; that is still seeks, not scans.
    """
    points = _has_points(conn)
    limit = max(1, min(limit, PAGE_LIMIT))
    offset = max(0, offset)

    keep = _matcher(tour, tournament, search)
    rows = [m for m, state in _classified(conn, time.time()) if state == "past" and keep(m)]

    # Last captured first, ties by id, so a page boundary never lands between
    # equals differently on the next request; the other orders are stable on top
    # of it. A match with no book at all sorts last.
    last = {m["condition_id"]: conn.execute(_LAST_CAPTURE, (m["condition_id"],)).fetchone()[0] for m in rows}
    rows.sort(key=lambda m: (last[m["condition_id"]] or 0.0, m["condition_id"]), reverse=True)
    if sort == "natural":
        cards = [_card(conn, m, "past", points, spark_points) for m in rows[offset : offset + limit]]
    else:
        every = [_card(conn, m, "past", points, spark_points) for m in rows]
        every.sort(key=_sort_key(sort), reverse=True)
        cards = every[offset : offset + limit]

    return {"total": len(rows), "offset": offset, "limit": limit, "matches": cards}


def _price_summary(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "ts": row["ts"],
        "bid": row["best_bid"],
        "ask": row["best_ask"],
        "mid": row["mid"],
        "spread": row["spread"],
    }


def _move(spark: Sequence[float | None]) -> float | None:
    """Change in outcome 0's mid across the sparkline window."""
    values = [v for v in spark if v is not None]
    if len(values) < 2:
        return None
    return values[-1] - values[0]


def _orient(last_trade: float | None, mid0: float | None) -> float | None:
    """Re-express a per-match last trade as outcome 0's price.

    The API gives one last-traded price per match on both tokens, oriented to
    whichever side traded last, and the row does not record which side that was.
    The two outcomes price a partition of the same event, so their mids sum to
    about 1 -- meaning the stored number sits near either ``mid0`` or ``1 - mid0``,
    and whichever it is nearer to is the side it belongs to. That is an inference,
    not a recorded fact, and it is shown as one in the UI: it is unreliable exactly
    when the two mids are close, because then both readings are equally plausible.
    """
    if last_trade is None or mid0 is None:
        return last_trade
    return last_trade if abs(last_trade - mid0) <= abs((1.0 - last_trade) - mid0) else 1.0 - last_trade


def _oriented_last_trade(books: dict[int, sqlite3.Row]) -> dict[str, Any] | None:
    row = books.get(0) or books.get(1)
    if row is None or row["market_last_trade"] is None:
        return None
    mid0 = books[0]["mid"] if 0 in books else None
    return {
        "raw": row["market_last_trade"],
        "p0": _orient(row["market_last_trade"], mid0),
        "certain": mid0 is not None,
    }


# ---------------------------------------------------------------- one match


def match_detail(conn: sqlite3.Connection, condition_id: str) -> dict[str, Any] | None:
    market = conn.execute(
        "SELECT * FROM markets WHERE condition_id = ?", (condition_id,)
    ).fetchone()
    if market is None:
        return None

    now = time.time()
    books = {}
    for row in conn.execute(
        "SELECT * FROM (SELECT *, ROW_NUMBER() OVER "
        "(PARTITION BY outcome_index ORDER BY ts DESC) AS rn FROM books WHERE condition_id = ?) "
        "WHERE rn = 1",
        (condition_id,),
    ):
        books[row["outcome_index"]] = row

    cover = conn.execute(
        "SELECT COUNT(*) AS snapshots, MIN(ts) AS first_ts, MAX(ts) AS last_ts "
        "FROM books WHERE condition_id = ?",
        (condition_id,),
    ).fetchone()

    return {
        "condition_id": condition_id,
        "question": market["question"],
        "tour": market["tour"],
        "tournament": market["tournament"],
        "tier": market["tier"],
        "market_type": market["market_type"],
        "slug": market["slug"],
        "event_slug": market["event_slug"],
        "players": [market["outcome_0"], market["outcome_1"]],
        "feed_state": market["state"],
        "state": classify(market["state"], market["start_time"], market["last_seen"], now),
        "period": market["period"],
        "score": market["score"],
        "start_time": market["start_time"],
        "start_epoch": _epoch(market["start_time"]),
        "last_seen": market["last_seen"],
        "snapshots": cover["snapshots"],
        "first_ts": cover["first_ts"],
        "last_ts": cover["last_ts"],
        "depth": BOOK_DEPTH,
        "books": [_ladder(books.get(i)) for i in (0, 1)],
        "last_trade": _oriented_last_trade(books),
        "score_events": score_events(conn, condition_id),
        "stats": match_stats(conn, condition_id),
        "odds": match_odds(conn, condition_id),
    }


def _stat_period(row: sqlite3.Row, columns: set[str]) -> list[dict[str, Any]]:
    """One stored statistics row as a list the browser can lay out directly.

    Statistics neither side has a number for are dropped rather than sent as a
    pair of nulls. A tournament without ball tracking reports no serve speed and
    no distance covered, and eight empty rows in the table would read as data
    that failed to arrive rather than a measurement nobody took.
    """
    out = []
    for statistic in STATISTICS:
        keys = [f"{statistic.key}_{i}" for i in (0, 1)]
        if any(key not in columns for key in keys):
            continue  # a statistic added after this database was last written
        values = [row[key] for key in keys]
        if all(value is None for value in values):
            continue
        totals: list[float | None] = [None, None]
        if statistic.of:
            totals = [row[f"{key}_of"] if f"{key}_of" in columns else None for key in keys]
        out.append(
            {
                "key": statistic.key,
                "label": statistic.label,
                "group": statistic.group,
                "unit": statistic.unit,
                # [value, out of] per player, in outcome order. `of` is null for
                # a plain count; where it is set the percentage is the quotient,
                # which is why it is not stored or sent.
                "values": [[values[i], totals[i]] for i in (0, 1)],
            }
        )
    return out


def match_stats(conn: sqlite3.Connection, condition_id: str) -> dict[str, Any]:
    """The match's statistics: the latest live totals, and the settled per-set set.

    Two different things, and the payload keeps them apart because they are not
    equally trustworthy. `live` is the last row the capture wrote on the tick --
    current to within a poll, and whatever Flashscore believed at that moment.
    `sets` is the reading taken an hour after the match, once the site had
    stopped reclassifying winners and correcting serve speeds, so its "Match"
    row can legitimately differ from the last live one. It is absent until then.
    """
    empty: dict[str, Any] = {
        "ts": None,
        "tick_id": None,
        "live": [],
        "final_ts": None,
        "sets": [],
    }
    if not _has_table(conn, "stat_events"):
        return empty
    columns = {row[1] for row in conn.execute("PRAGMA table_info(stat_events)")}
    latest = conn.execute(
        "SELECT * FROM stat_events WHERE condition_id = ? ORDER BY ts DESC LIMIT 1",
        (condition_id,),
    ).fetchone()

    sets: list[dict[str, Any]] = []
    final_ts: float | None = None
    if _has_table(conn, "set_stats"):
        final_columns = {row[1] for row in conn.execute("PRAGMA table_info(set_stats)")}
        for row in conn.execute(
            "SELECT * FROM set_stats WHERE condition_id = ? ORDER BY period", (condition_id,)
        ):
            final_ts = row["ts"]
            sets.append({"period": row["period"], "stats": _stat_period(row, final_columns)})

    return {
        "ts": latest["ts"] if latest is not None else None,
        "tick_id": (
            latest["tick_id"] if latest is not None and "tick_id" in columns else None
        ),
        "live": _stat_period(latest, columns) if latest is not None else [],
        "final_ts": final_ts,
        "sets": sets,
    }


def match_odds(conn: sqlite3.Connection, condition_id: str) -> dict[str, Any]:
    """The latest Home/Away line each bookmaker has on this match.

    One row per bookmaker, the newest `odds` row for that bookmaker, in the
    market's own outcome order -- the capture already flipped anything the page
    listed the other way round. These are pre-match observations only: the last
    one before the match starts is its closing line, and nothing is recorded in
    play. The vig is left in the stored prices; normalizing it is a reader's
    decision, made in the UI, not here.
    """
    empty: dict[str, Any] = {"ts": None, "tennisexplorer_id": None, "bookmakers": []}
    if not _has_table(conn, "odds"):
        return empty
    rows = [
        {
            "bookmaker": r["bookmaker"],
            "price_0": r["price_0"],
            "price_1": r["price_1"],
            "ts": r["ts"],
            "tennisexplorer_id": r["tennisexplorer_id"],
        }
        for r in conn.execute(
            """
            SELECT o.bookmaker, o.price_0, o.price_1, o.ts, o.tennisexplorer_id
            FROM odds o
            WHERE o.condition_id = ?
              AND o.ts = (
                  SELECT MAX(x.ts) FROM odds x
                  WHERE x.condition_id = o.condition_id AND x.bookmaker = o.bookmaker
              )
            ORDER BY o.bookmaker
            """,
            (condition_id,),
        )
    ]
    if not rows:
        return empty
    return {
        "ts": max(row["ts"] for row in rows),
        "tennisexplorer_id": rows[0]["tennisexplorer_id"],
        "bookmakers": [
            {"bookmaker": r["bookmaker"], "price_0": r["price_0"], "price_1": r["price_1"]}
            for r in rows
        ],
    }


def _ladder(row: sqlite3.Row | None) -> dict[str, Any] | None:
    """The stored book as a ladder: asks then bids, best price innermost."""
    if row is None:
        return None
    bids = [(row[f"bid_px_{i}"], row[f"bid_sz_{i}"]) for i in _LEVELS]
    asks = [(row[f"ask_px_{i}"], row[f"ask_sz_{i}"]) for i in _LEVELS]

    def keep(side: Iterable[tuple[float | None, float | None]]) -> list[dict[str, float | None]]:
        return [{"price": p, "size": s} for p, s in side if p is not None]

    return {
        "ts": row["ts"],
        "tick_id": row["tick_id"] if "tick_id" in row.keys() else None,
        "outcome": row["outcome"],
        "bid": row["best_bid"],
        "ask": row["best_ask"],
        "mid": row["mid"],
        "spread": row["spread"],
        "bids": keep(bids),
        "asks": keep(asks),
        "bid_depth": _total(bids),
        "ask_depth": _total(asks),
    }


def _total(side: Iterable[tuple[float | None, float | None]]) -> float | None:
    sizes = [s for _, s in side if s is not None]
    return sum(sizes) if sizes else None


def score_events(conn: sqlite3.Connection, condition_id: str) -> list[dict[str, Any]]:
    """Timestamped score changes, or an empty list against an older database."""
    if not _has_table(conn, "score_events"):
        return []
    # `game` and `serving` arrived after the table did, so a capture recorded
    # before them simply has no points to show rather than failing to open.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(score_events)")}
    extra = ", ".join(
        c if c in columns else f"NULL AS {c}"
        for c in ("tick_id", "game", "serving")
    )
    return [
        {
            "ts": r["ts"],
            "tick_id": r["tick_id"],
            "state": r["state"],
            "period": r["period"],
            "score": r["score"],
            "game": r["game"],
            "serving": r["serving"],
        }
        for r in conn.execute(
            f"SELECT ts, state, period, score, {extra} FROM score_events "
            "WHERE condition_id = ? ORDER BY ts",
            (condition_id,),
        )
    ]


# ---------------------------------------------------------------- series


def match_series(
    conn: sqlite3.Connection,
    condition_id: str,
    max_points: int = 900,
    since: float | None = None,
) -> dict[str, Any]:
    """Both players' books over time, on one shared, forward-filled time grid."""
    sql = f"SELECT {', '.join(_BOOK_FIELDS)} FROM books WHERE condition_id = ?"
    params: list[Any] = [condition_id]
    if since is not None:
        sql += " AND ts >= ?"
        params.append(since)
    rows = conn.execute(sql + " ORDER BY ts", params).fetchall()

    grid = sorted({row["ts"] for row in rows})
    by_ts: dict[float, dict[int, sqlite3.Row]] = {}
    for row in rows:
        by_ts.setdefault(row["ts"], {})[row["outcome_index"]] = row

    keep = _decimate(grid, max_points)

    series = {i: {k: [] for k in ("mid", "bid", "ask", "spread", "bid_depth", "ask_depth")} for i in (0, 1)}
    last_trade: list[float | None] = []
    last_trade_raw: list[float | None] = []
    carried: dict[int, sqlite3.Row] = {}
    kept_ts: list[float] = []

    for ts in grid:
        # Carry every timestamp through the fill, but only emit the kept ones:
        # a decimated point still has to inherit the rows it skipped over.
        for index, row in by_ts.get(ts, {}).items():
            carried[index] = row
        if ts not in keep:
            continue

        kept_ts.append(ts)
        for index in (0, 1):
            row = carried.get(index)
            out = series[index]
            out["mid"].append(row["mid"] if row else None)
            out["bid"].append(row["best_bid"] if row else None)
            out["ask"].append(row["best_ask"] if row else None)
            out["spread"].append(row["spread"] if row else None)
            out["bid_depth"].append(_total([(row[f"bid_px_{i}"], row[f"bid_sz_{i}"]) for i in _LEVELS]) if row else None)
            out["ask_depth"].append(_total([(row[f"ask_px_{i}"], row[f"ask_sz_{i}"]) for i in _LEVELS]) if row else None)

        raw = next((carried[i]["market_last_trade"] for i in (0, 1) if carried.get(i) is not None and carried[i]["market_last_trade"] is not None), None)
        last_trade_raw.append(raw)
        last_trade.append(_orient(raw, series[0]["mid"][-1]))

    market = conn.execute(
        "SELECT outcome_0, outcome_1 FROM markets WHERE condition_id = ?", (condition_id,)
    ).fetchone()
    names = [market["outcome_0"], market["outcome_1"]] if market else ["Outcome 0", "Outcome 1"]

    return {
        "condition_id": condition_id,
        "ts": kept_ts,
        "outcomes": [{"index": i, "name": names[i], **series[i]} for i in (0, 1)],
        "last_trade": last_trade,
        "last_trade_raw": last_trade_raw,
        "score_events": score_events(conn, condition_id),
        "points": len(kept_ts),
        "total_points": len(grid),
        "decimated": len(kept_ts) < len(grid),
    }


def _decimate(grid: Sequence[float], max_points: int) -> set[float]:
    """Thin an over-long grid to at most ``max_points``, keeping both ends.

    Takes the last sample in each bucket rather than averaging: an averaged book
    is a book that never existed, and the last one in a bucket is a real reading.
    """
    if len(grid) <= max_points or max_points < 2:
        return set(grid)
    step = len(grid) / max_points
    keep = {grid[min(len(grid) - 1, int((i + 1) * step) - 1)] for i in range(max_points)}
    keep.add(grid[0])
    keep.add(grid[-1])
    return keep
