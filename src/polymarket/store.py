"""SQLite persistence for market metadata and order-book snapshots."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from .book import Snapshot
from .config import BOOK_DEPTH, STATISTICS
from .discovery import TennisMarket
from .scores import StatPeriod


@dataclass(frozen=True)
class ScoreRow:
    """One match's score as the tables want it written.

    The score poll no longer reads a Polymarket event, so it has no
    ``TennisMarket`` to hand over -- just a condition id and what the score feed
    said about it.
    """

    condition_id: str
    state: str
    period: str | None
    score: str | None
    game: str | None = None  # points in the game being played, "30-40"
    serving: int | None = None  # which outcome is serving, by index

    @classmethod
    def of(cls, market: TennisMarket) -> "ScoreRow":
        return cls(
            market.condition_id,
            market.state,
            market.period,
            market.score,
            market.game,
            market.serving,
        )


@dataclass(frozen=True)
class StatRow:
    """One period of one match's statistics, ready to be written.

    `values` is keyed by ``STAT_COLUMNS`` and already in the market's own
    outcome order -- the flip that re-expresses a Flashscore reading as
    Polymarket lists its players happens in ``of``, once, exactly as it does
    for the score. A statistic the feed did not report is absent from the dict
    and stored as NULL; it is not zero.
    """

    condition_id: str
    period: str  # "Match", "Set 1", ...
    values: dict[str, float | None]
    digest: str | None = None

    @classmethod
    def of(
        cls,
        condition_id: str,
        period: StatPeriod,
        flip: bool,
        digest: str | None = None,
    ) -> "StatRow":
        first, second = period.sides(flip)
        values: dict[str, float | None] = {}
        for statistic in STATISTICS:
            for index, side in enumerate((first, second)):
                entry = side.get(statistic.key)
                values[f"{statistic.key}_{index}"] = None if entry is None else entry.value
                if statistic.of:
                    values[f"{statistic.key}_{index}_of"] = None if entry is None else entry.of
        return cls(condition_id, period.period, values, digest)

    def row(self) -> tuple[float | None, ...]:
        """The statistics as ``STAT_COLUMNS`` orders them."""
        return tuple(self.values.get(col) for col in STAT_COLUMNS)


def _depth_columns(depth: int) -> list[str]:
    cols: list[str] = []
    for side in ("bid", "ask"):
        for i in range(1, depth + 1):
            cols += [f"{side}_px_{i}", f"{side}_sz_{i}"]
    return cols


DEPTH_COLUMNS = _depth_columns(BOOK_DEPTH)

BOOK_COLUMNS = [
    "ts",
    # One id made by the capture loop and handed to the book, score and
    # statistics writes in that pass. Unlike their individual `ts` values it
    # is exactly equal across feeds, so joining a point to its market read does
    # not need a nearest-time guess. NULL marks rows written before it existed
    # and score rows written by a discovery refresh rather than a capture tick.
    "tick_id",
    "condition_id",
    "token_id",
    "outcome_index",
    "outcome",
    "best_bid",
    "best_ask",
    "mid",
    "spread",
    *DEPTH_COLUMNS,
    # Per-MARKET, not per-outcome: the API reports the same value on both tokens,
    # oriented to whichever side traded last. Comparing it to this row's own mid
    # is wrong for one of the two rows. See parse_book.
    "market_last_trade",
    "book_hash",
    # When the book last changed *upstream*, as opposed to `ts`, which is when we
    # read it. `ts - book_ts` is the staleness of the quote, and the only way to
    # tell a market nobody is trading from a feed that has stopped moving.
    "book_ts",
    # 0 when book_ts came from the API, 1 when `backfill-book-ts` reconstructed it
    # from runs of equal book_hash -- accurate only to one polling interval, and
    # blind to changes that happened while nothing was being recorded. NULL for
    # rows written before this column existed and never backfilled.
    "book_ts_derived",
]


def _stat_columns() -> list[str]:
    """Two columns per statistic per player, generated from the catalogue.

    Named by outcome index, not by Flashscore's home and away, because that is
    the order everything else in this file is written in -- `books` has an
    `outcome_index` and `score` reads left to right in the same order. A
    statistic reported as a made-of-attempted pair gets a second column for the
    denominator; see ``config.Statistic``.
    """
    cols: list[str] = []
    for statistic in STATISTICS:
        for index in (0, 1):
            cols.append(f"{statistic.key}_{index}")
            if statistic.of:
                cols.append(f"{statistic.key}_{index}_of")
    return cols


STAT_COLUMNS = _stat_columns()

MARKET_COLUMNS = [
    ("condition_id", "TEXT PRIMARY KEY"),
    ("question", "TEXT"),
    ("slug", "TEXT"),
    ("event_title", "TEXT"),
    ("event_slug", "TEXT"),
    ("tournament", "TEXT"),
    ("tier", "TEXT"),
    ("tour", "TEXT"),
    ("match_date", "TEXT"),
    ("market_type", "TEXT"),
    ("state", "TEXT"),
    ("start_time", "TEXT"),
    ("period", "TEXT"),
    ("score", "TEXT"),
    ("outcome_0", "TEXT"),
    ("outcome_1", "TEXT"),
    ("token_0", "TEXT"),
    ("token_1", "TEXT"),
    ("start_date", "TEXT"),
    ("end_date", "TEXT"),
    ("first_seen", "REAL"),
    ("last_seen", "REAL"),
    # Which Flashscore match this is, and whether its home player is this
    # market's second outcome. Stored rather than re-derived because the
    # statistics of a finished match are collected an hour after it ends, by
    # which time the market has usually been resolved and dropped from the API
    # -- there is nothing left to pair against, only what was written down.
    # A NULL flip means the two names fitted each other's side equally well, so
    # nothing that reads home from away may be attributed to a player at all.
    ("flashscore_id", "TEXT"),
    ("flashscore_flip", "INTEGER"),
    ("raw", "TEXT"),
]

SCORE_EVENT_COLUMNS = [
    ("ts", "REAL"),
    ("tick_id", "INTEGER"),
    ("condition_id", "TEXT"),
    ("state", "TEXT"),
    ("period", "TEXT"),
    ("score", "TEXT"),
    ("game", "TEXT"),
    ("serving", "INTEGER"),
]


@dataclass(frozen=True)
class TradeRow:
    """One taker fill, in what the Data API reported it in.

    `ts` is the venue's own timestamp, not the capture's -- a print exists on the
    datetime Polymarket settled it, and the capture may only learn of it seconds
    later. `side` is the taker's side (BUY/SELL) on the outcome named by
    `outcome_index`; the same match's complementary half lives on the other
    token and is *not* a second row, so one row is one trade, not two.

    A print is the venue's record of a crossing: `size` at `price` took
    liquidity *from the side the taker attacked*. There is no maker fill model
    without per-print size and direction, which is why this row carries both
    rather than only the price the CLOB's `last_trade_price` offers.
    """

    condition_id: str
    transaction_hash: str
    wallet: str | None
    side: str  # "BUY" / "SELL"
    price: float
    size: float
    outcome_index: int | None
    outcome: str | None
    ts: float

    @classmethod
    def of(cls, payload: dict[str, Any]) -> "TradeRow | None":
        """Parse one Data-API trade, tolerating a malformed row by refusing it.

        The endpoint is not a strict contract and a row missing its own hash
        cannot be deduplicated against its neighbours, so an unparseable row is
        dropped and counted rather than stored half-known.
        """
        try:
            tx = str(payload["transactionHash"])
            side = str(payload["side"])
            price = float(payload["price"])
            size = float(payload["size"])
            ts = float(payload["timestamp"])
            if not tx or side not in ("BUY", "SELL") or price <= 0 or size <= 0 or ts <= 0:
                return None
            wallet = payload.get("proxyWallet") or None
            outcome = payload.get("outcome") or None
            index = payload.get("outcomeIndex")
            return cls(
                condition_id=str(payload["conditionId"]),
                transaction_hash=tx,
                wallet=str(wallet) if wallet else None,
                side=side,
                price=price,
                size=size,
                outcome_index=int(index) if index is not None else None,
                outcome=str(outcome) if outcome else None,
                ts=ts,
            )
        except (KeyError, TypeError, ValueError):
            return None


TRADE_COLUMNS = [
    # The venue's own timestamp for the print, in epoch seconds -- one-second
    # resolution. NOT the capture tick's clock: prints are a stream the capture
    # observes, and forcing them onto the tick grid would fold the timing
    # structure a fill model reads into whatever moment the poll landed on.
    ("ts", "REAL"),
    ("tick_id", "INTEGER"),  # the tick that first recorded it, if any
    ("condition_id", "TEXT"),
    ("transaction_hash", "TEXT"),
    ("wallet", "TEXT"),
    ("side", "TEXT"),
    ("price", "REAL"),
    ("size", "REAL"),
    ("outcome_index", "INTEGER"),
    ("outcome", "TEXT"),
]

_TEXT_COLUMNS = {"condition_id", "token_id", "outcome", "book_hash"}
_INT_COLUMNS = {"tick_id", "outcome_index", "book_ts_derived"}


def _books_ddl() -> str:
    def kind(col: str) -> str:
        if col in _TEXT_COLUMNS:
            return "TEXT"
        return "INTEGER" if col in _INT_COLUMNS else "REAL"

    return ",\n".join(f"    {col:<16} {kind(col)}" for col in BOOK_COLUMNS)


SCHEMA = f"""
CREATE TABLE IF NOT EXISTS markets (
{",".join(chr(10) + f"    {name:<16} {kind}" for name, kind in MARKET_COLUMNS)}
);

CREATE TABLE IF NOT EXISTS books (
{_books_ddl()},
    PRIMARY KEY (token_id, ts)
) WITHOUT ROWID;

-- markets.period/score hold only the latest value, which says where a match
-- stands but not when it got there. Reading a price move against the point that
-- caused it needs the score to carry a timestamp, so every change is appended
-- here as well -- including `game`, the points inside it, which turn over
-- several times a game and are what the chart reads out on hover.
CREATE TABLE IF NOT EXISTS score_events (
{",".join(chr(10) + f"    {name:<14} {kind}" for name, kind in SCORE_EVENT_COLUMNS)},
    PRIMARY KEY (condition_id, ts)
) WITHOUT ROWID;

-- The match's running statistics, appended every time one of them moves. Same
-- grain and the same reasoning as `books`: the feed is re-read on the tick, the
-- numbers change on nearly every point, and writing every read unchanged would
-- bury the changes under thousands of copies. What is stored here is the
-- feed's "Match" block only -- the totals as they stood at `ts` -- because
-- that is the block that moves while the match is on.
--
-- There is no heartbeat. A book that stops moving is ambiguous (a calm market
-- and a dead collector look alike), which is why one gets written anyway; a
-- statistic that stops moving is not, because `books` and `score_events` are
-- already recording on the same tick and say whether anything was running.
CREATE TABLE IF NOT EXISTS stat_events (
    ts             REAL,
    tick_id        INTEGER,
    condition_id   TEXT,
    -- The feed's own digest of the response this came from. Not what decides a
    -- write -- that is the stored values -- but it ties a row to one read.
    digest         TEXT,
{",".join(chr(10) + f"    {col:<22} REAL" for col in STAT_COLUMNS)},
    PRIMARY KEY (condition_id, ts)
) WITHOUT ROWID;

-- The same statistics broken down by period, taken once, an hour after the
-- match ended. Flashscore goes on revising a finished match for a while --
-- an unforced error is reclassified as a winner, the radar's serve speeds are
-- corrected -- so this is the settled version, and the per-set rows sum to the
-- "Match" row, which is what makes it a check on the live capture above.
--
-- One row per period, replaced rather than appended: this is a final reading,
-- not a history, and re-running it must not accumulate copies.
CREATE TABLE IF NOT EXISTS set_stats (
    condition_id   TEXT,
    period         TEXT,  -- "Match", "Set 1", "Set 2", ...
    ts             REAL,  -- when it was collected, not when the set was played
    digest         TEXT,
{",".join(chr(10) + f"    {col:<22} REAL" for col in STAT_COLUMNS)},
    PRIMARY KEY (condition_id, period)
) WITHOUT ROWID;

-- The trade tape: every taker fill on a tracked market, as the venue recorded
-- it. The CLOB book's `last_trade_price` says that *something* traded and at
-- what price; it says neither how much nor which side, and a fill model that
-- asks "would a resting order at this price have been filled?" needs exactly
-- those two. Taker rows only (`takerOnly` on the source): the taker is the
-- aggressor, so these are the prints that trade through a resting price --
-- which is both what the question needs and the adverse-selection bias recorded
-- rather than hidden: a fill you get is one the market chose to move through.
--
-- NOT the capture tick's clock. `tick_id` is when we first saw the row; `ts` is
-- when the venue settled it. Reading price against trades joins on the venue
-- clock by time window, deliberately unlike the books/score/stat joins, which
-- use `tick_id` -- here the facts being joined are all venue-time facts.
--
-- Primary key on the venue's own trade identity, so replaying a poll or a
-- backfill neither duplicates nor overwrites: the same print twice is one
-- print, and that is why the write is INSERT OR IGNORE.
CREATE TABLE IF NOT EXISTS trades (
{", ".join(f"    {name:<18} {kind}" for name, kind in TRADE_COLUMNS)},
    PRIMARY KEY (transaction_hash, wallet, side, outcome_index)
) WITHOUT ROWID;

"""

# Applied after _migrate(): indexes and the view both reference columns that an
# older database does not have yet. A view carries no data, so rebuilding it on
# every open is free and keeps its definition in step with the code.
VIEWS = """
CREATE INDEX IF NOT EXISTS books_by_market ON books (condition_id, ts);
CREATE INDEX IF NOT EXISTS books_by_ts ON books (ts);
CREATE INDEX IF NOT EXISTS score_events_by_market ON score_events (condition_id, ts);
-- Lets a reader seek straight to one player's latest rows instead of scanning
-- the table. Without it the dashboard's per-match lookups degrade into a full
-- scan once a season's worth of ticks has accumulated.
CREATE INDEX IF NOT EXISTS books_by_outcome ON books (condition_id, outcome_index, ts);
-- The trade tape is read by market against its own clock: "what the book looked
-- like when this print went through" is a condition-id + venue-timestamp seek.
CREATE INDEX IF NOT EXISTS trades_by_market ON trades (condition_id, ts);
-- stat_events and set_stats get no index: both are WITHOUT ROWID keyed on
-- exactly what a reader seeks by, so the table *is* that index. (score_events
-- above has one restating its own primary key; it predates the WITHOUT ROWID.)
-- `trades` is the same shape but gets the index above for the opposite reason:
-- its primary key is the venue's trade identity, while the join a reader makes
-- is (condition_id, venue-time window) to the book it moved.

DROP VIEW IF EXISTS quotes;
CREATE VIEW quotes AS
SELECT
    b.ts,
    b.tick_id,
    datetime(b.ts, 'unixepoch') AS utc_time,
    m.tournament,
    m.match_date,
    m.question,
    b.outcome,
    b.best_ask AS buy_price,   -- price you pay to buy this outcome
    b.best_bid AS sell_price,  -- price you receive to sell it
    b.mid,
    b.spread,
    b.market_last_trade,
    b.condition_id,
    b.token_id
FROM books b
LEFT JOIN markets m ON m.condition_id = b.condition_id;
"""


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, isolation_level=None)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.executescript(VIEWS)

    def _migrate(self) -> None:
        """Add columns an older database is missing.

        CREATE TABLE IF NOT EXISTS is a no-op against an existing table, so a
        database written by an earlier version keeps its old shape and every
        insert fails on the column count. Adding what's missing is enough here:
        the schema only ever grows, and SQLite backfills NULL for old rows.
        """
        expected = {
            "markets": [(name, kind.replace(" PRIMARY KEY", "")) for name, kind in MARKET_COLUMNS],
            "books": [
                (col, "TEXT" if col in _TEXT_COLUMNS else "INTEGER" if col in _INT_COLUMNS else "REAL")
                for col in BOOK_COLUMNS
            ],
            "score_events": SCORE_EVENT_COLUMNS,
            "stat_events": [
                ("ts", "REAL"),
                ("tick_id", "INTEGER"),
                ("condition_id", "TEXT"),
                ("digest", "TEXT"),
            ]
            + [(col, "REAL") for col in STAT_COLUMNS],
            "set_stats": [
                ("condition_id", "TEXT"),
                ("period", "TEXT"),
                ("ts", "REAL"),
                ("digest", "TEXT"),
            ]
            + [(col, "REAL") for col in STAT_COLUMNS],
            "trades": TRADE_COLUMNS,
        }
        for table, columns in expected.items():
            present = {row[1] for row in self.conn.execute(f"PRAGMA table_info({table})")}
            for name, kind in columns:
                if name not in present:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def upsert_markets(self, markets: Iterable[TennisMarket]) -> int:
        now = time.time()
        rows = [
            (
                m.condition_id,
                m.question,
                m.slug,
                m.event_title,
                m.event_slug,
                m.tournament,
                m.tier,
                m.tour,
                m.match_date,
                m.market_type,
                m.state,
                m.start_time,
                m.period,
                m.score,
                m.outcomes[0],
                m.outcomes[1],
                m.tokens[0],
                m.tokens[1],
                m.start_date,
                m.end_date,
                now,
                now,
                m.pairing.id if m.pairing else None,
                # None, not 0, when the pairing could not be oriented: nothing
                # read home-from-away may be attributed to a player then, and a
                # 0 would say "the two are already the right way round".
                (int(m.pairing.flip) if m.pairing.oriented else None) if m.pairing else None,
                json.dumps(m.raw, default=str),
            )
            for m in markets
        ]
        names = [name for name, _ in MARKET_COLUMNS]
        self.conn.executemany(
            f"""
            INSERT INTO markets ({", ".join(names)})
            VALUES ({", ".join("?" * len(names))})
            ON CONFLICT(condition_id) DO UPDATE SET
                question=excluded.question,
                market_type=excluded.market_type,
                state=excluded.state,
                start_time=excluded.start_time,
                period=excluded.period,
                score=excluded.score,
                end_date=excluded.end_date,
                last_seen=excluded.last_seen,
                -- Coalesced, not overwritten: a refresh where the board was
                -- unavailable pairs nothing, and it must not erase the id the
                -- deferred statistics collection is going to need.
                flashscore_id=COALESCE(excluded.flashscore_id, markets.flashscore_id),
                flashscore_flip=COALESCE(excluded.flashscore_flip, markets.flashscore_flip),
                raw=excluded.raw
            """,
            rows,
        )
        return len(rows)

    def update_scores(self, scores: Iterable[ScoreRow]) -> int:
        """Refresh only the score fields on rows that already exist.

        The score poll runs on the tick cadence, so it wants the cheapest write
        that keeps `markets` current -- not upsert_markets, which would also
        re-serialise the raw API payload every few seconds to store it unchanged.
        A match the poll sees before discovery has inserted it is simply skipped;
        the next refresh puts it in.
        """
        now = time.time()
        rows = [(s.state, s.period, s.score, now, s.condition_id) for s in scores]
        self.conn.executemany(
            "UPDATE markets SET state = ?, period = ?, score = ?, last_seen = ? "
            "WHERE condition_id = ?",
            rows,
        )
        return len(rows)

    def record_score_events(
        self, scores: Iterable[ScoreRow], tick_id: int | None = None
    ) -> int:
        """Append a row for every match whose state, period or score has moved.

        Only changes are stored: the score feed is re-read on every poll, so
        writing each reading unconditionally would bury the handful of moments
        that matter under thousands of identical rows. Resolution is therefore
        the score-poll interval, which tracks the book cadence rather than the
        market-list refresh -- fine enough to place individual games.
        """
        now = time.time()
        rows = []
        for entry in scores:
            current = (entry.state, entry.period, entry.score, entry.game, entry.serving)
            previous = self.conn.execute(
                "SELECT state, period, score, game, serving FROM score_events "
                "WHERE condition_id = ? ORDER BY ts DESC LIMIT 1",
                (entry.condition_id,),
            ).fetchone()
            if previous is not None and tuple(previous) == current:
                continue
            rows.append((now, tick_id, entry.condition_id, *current))

        if rows:
            self.conn.executemany(
                "INSERT OR REPLACE INTO score_events "
                "(ts, tick_id, condition_id, state, period, score, game, serving) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        return len(rows)

    def record_stat_events(
        self, rows: Iterable[StatRow], tick_id: int | None = None
    ) -> int:
        """Append a row for every match whose statistics have moved.

        Changes only, for the same reason ``record_score_events`` stores
        changes only: the feed is re-read on the tick and most reads find the
        rally still going. Resolution is therefore the poll interval, which is
        finer than a point -- which is what "the statistics at every point"
        actually needs, since a point is what moves them.

        The comparison is over the stored columns, not the feed's own digest:
        the digest covers the per-set blocks too, and a row here is the match
        totals. Comparing what is written is what keeps the table free of rows
        that differ from their predecessor in nothing.
        """
        now = time.time()
        payload = []
        for entry in rows:
            current = entry.row()
            previous = self.conn.execute(
                f"SELECT {', '.join(STAT_COLUMNS)} FROM stat_events "
                "WHERE condition_id = ? ORDER BY ts DESC LIMIT 1",
                (entry.condition_id,),
            ).fetchone()
            if previous is not None and tuple(previous) == current:
                continue
            payload.append((now, tick_id, entry.condition_id, entry.digest, *current))

        if payload:
            columns = ["ts", "tick_id", "condition_id", "digest", *STAT_COLUMNS]
            self.conn.executemany(
                f"INSERT OR REPLACE INTO stat_events ({', '.join(columns)}) "
                f"VALUES ({', '.join('?' * len(columns))})",
                payload,
            )
        return len(payload)

    def latest_stat_points(self, condition_ids: Iterable[str]) -> dict[str, int]:
        """The persisted monotonic floor for each requested match.

        The live ratchet is memory, while the history must remain monotonic
        across process restarts too. The table's primary key makes these one-row
        backward seeks; doing them for the handful of watched matches is cheaper
        and clearer than a window over the full statistics history.
        """
        out = {}
        for condition_id in condition_ids:
            row = self.conn.execute(
                "SELECT COALESCE(total_points_won_0_of, total_points_won_1_of) "
                "FROM stat_events WHERE condition_id = ? ORDER BY ts DESC LIMIT 1",
                (condition_id,),
            ).fetchone()
            if row is not None and row[0] is not None:
                out[condition_id] = int(row[0])
        return out

    def record_set_stats(self, rows: Sequence[StatRow], ts: float | None = None) -> int:
        """Write a finished match's statistics, one row per period.

        Replaced rather than appended: this is the settled reading taken once,
        an hour after the match, and running it twice must leave one row per
        period rather than two. `ts` is when it was collected -- there is no
        timestamp for when a set was played, and pretending otherwise would
        invite someone to plot it.
        """
        if not rows:
            return 0
        now = time.time() if ts is None else ts
        columns = ["condition_id", "period", "ts", "digest", *STAT_COLUMNS]
        self.conn.executemany(
            f"INSERT OR REPLACE INTO set_stats ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' * len(columns))})",
            [(r.condition_id, r.period, now, r.digest, *r.row()) for r in rows],
        )
        return len(rows)

    def matches_awaiting_set_stats(
        self, now: float, delay: float, window: float, limit: int
    ) -> list[tuple[str, str, int, str]]:
        """Matches that ended long enough ago to collect their final statistics.

        Returns ``(condition_id, flashscore_id, flip, question)``, oldest first.
        The question comes along because by this point the match is an hour gone
        and the poller no longer holds anything that could name it in a log.

        A match qualifies once it has been over for `delay` and has no rows in
        `set_stats` yet, which is what makes the collection idempotent and
        survives a restart -- the queue is the database, not a timer held in
        memory. `window` is the other end of it: a database that has never had
        this run holds a season of finished matches, and without a bound the
        first check after an upgrade would ask Flashscore for all of them.

        The end of a match is the *first* score event that called it ended; the
        ratchet does not let a match come back from that, so the earliest such
        row is the moment itself rather than the last time it was re-read.

        A match whose pairing could not be oriented is left out. Its statistics
        exist, but there is no way to say which player each column belongs to,
        and a mirrored row is worse than no row.
        """
        return [
            (str(cid), str(fid), int(flip), str(question or cid))
            for cid, fid, flip, question in self.conn.execute(
                """
                SELECT m.condition_id, m.flashscore_id, m.flashscore_flip, m.question
                FROM markets m
                JOIN (
                    SELECT condition_id, MIN(ts) AS ended FROM score_events
                    WHERE state = 'ended' GROUP BY condition_id
                ) e ON e.condition_id = m.condition_id
                WHERE m.flashscore_id IS NOT NULL
                  AND m.flashscore_flip IS NOT NULL
                  AND e.ended <= ? AND e.ended >= ?
                  AND NOT EXISTS (
                      SELECT 1 FROM set_stats s WHERE s.condition_id = m.condition_id
                  )
                ORDER BY e.ended
                LIMIT ?
                """,
                (now - delay, now - window, limit),
            )
        ]

    def insert_snapshots(
        self,
        ts: float,
        rows: Sequence[tuple[Snapshot, str, int, str]],
        tick_id: int | None = None,
    ) -> int:
        """rows: (snapshot, condition_id, outcome_index, outcome_label)."""
        payload = []
        for snap, condition_id, index, outcome in rows:
            values: list[object] = [
                ts,
                tick_id,
                condition_id,
                snap.token_id,
                index,
                outcome,
                snap.best_bid,
                snap.best_ask,
                snap.mid,
                snap.spread,
            ]
            for side in (snap.bids, snap.asks):
                for i in range(BOOK_DEPTH):
                    price, size = side[i] if i < len(side) else (None, None)
                    values += [price, size]
            values += [snap.market_last_trade, snap.book_hash, snap.book_ts, 0]
            payload.append(tuple(values))

        placeholders = ",".join("?" * len(BOOK_COLUMNS))
        self.conn.executemany(
            f"INSERT OR REPLACE INTO books ({', '.join(BOOK_COLUMNS)}) VALUES ({placeholders})",
            payload,
        )
        return len(payload)

    def record_trades(self, rows: Iterable[TradeRow], tick_id: int | None = None) -> int:
        """Record taker prints. Returns how many were new.

        INSERT OR IGNORE keyed on the venue's own trade identity: a print the
        poll has seen before is left exactly as it was, so replaying any page --
        or walking the whole history in a backfill -- is idempotent. That also
        makes the return count the honest dedupe signal: zero means every row
        this page carried was already known, which is what lets a poll stop
        walking pages without remembering anything between ticks, or across
        restarts.

        A malformed row is refused upstream (``TradeRow.of`` returns None), so
        everything reaching here is insertable whole.
        """
        rows = [row for row in rows if row is not None]
        if not rows:
            return 0
        names = [name for name, _ in TRADE_COLUMNS]
        cursor = self.conn.executemany(
            f"INSERT OR IGNORE INTO trades ({', '.join(names)}) "
            f"VALUES ({', '.join('?' * len(names))})",
            [(
                row.ts,
                tick_id,
                row.condition_id,
                row.transaction_hash,
                row.wallet,
                row.side,
                row.price,
                row.size,
                row.outcome_index,
                row.outcome,
            ) for row in rows],
        )
        return cursor.rowcount if cursor.rowcount > 0 else 0

    def markets_for_trade_backfill(
        self, tour: str | None = None, limit: int | None = None
    ) -> list[str]:
        """Condition ids to walk for `backfill-trades`, oldest market first.

        Ordered so a partial run interrupted by rate limits advances steadily
        from the beginning of the record rather than scattered through it.
        """
        if tour:
            query = "SELECT condition_id FROM markets WHERE tour = ? ORDER BY first_seen"
            params: tuple = (tour,)
        else:
            query = "SELECT condition_id FROM markets ORDER BY first_seen"
            params = ()
        if limit is not None:
            query += " LIMIT ?"
            params += (limit,)
        return [str(row[0]) for row in self.conn.execute(query, params)]

    def stats(self) -> dict[str, object]:
        cur = self.conn.cursor()
        markets = cur.execute("SELECT COUNT(*) FROM markets").fetchone()[0]
        snaps, first, last = cur.execute(
            "SELECT COUNT(*), MIN(ts), MAX(ts) FROM books"
        ).fetchone()
        # By tour as well as tournament: a combined event runs an ATP and a WTA
        # draw under one name, and merging them would hide which is which.
        by_tournament = cur.execute(
            """
            SELECT m.tour, m.tournament, COUNT(DISTINCT m.condition_id), COUNT(b.ts)
            FROM markets m LEFT JOIN books b ON b.condition_id = m.condition_id
            GROUP BY m.tour, m.tournament ORDER BY 4 DESC
            """
        ).fetchall()
        stat_rows = cur.execute("SELECT COUNT(*) FROM stat_events").fetchone()[0]
        trade_rows = cur.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        # By match rather than by row: the interesting number is how many
        # finished matches have their settled per-set breakdown, not how many
        # periods that came to.
        final = cur.execute(
            "SELECT COUNT(DISTINCT condition_id) FROM set_stats"
        ).fetchone()[0]
        return {
            "db": str(self.path),
            "markets": markets,
            "snapshots": snaps,
            "first_ts": first,
            "last_ts": last,
            "stat_events": stat_rows,
            "trades": trade_rows,
            "set_stats": final,
            "by_tournament": by_tournament,
        }

    def prune_score_events(self, apply: bool = False) -> dict[str, object]:
        """Drop score rows that went backwards, and the repeats they leave behind.

        Captures recorded before the ratchet took every score at face value, and
        Flashscore's edge caches hand out copies of different ages -- so a game
        was written down three times, the middle one a rewind. This replays what
        is stored through the same ratchet the capture now uses and removes what
        it would not have accepted.

        Deleting a rewind usually strands the row after it, which was only a
        change because the rewind had moved the score away and back, so those go
        too. What survives is the sequence of readings that actually advanced.

        Returns a summary and, unless ``apply``, changes nothing.
        """
        from .scores import Ratchet, Reading, parse_line

        rows = self.conn.execute(
            "SELECT ts, condition_id, state, period, score, game, serving "
            "FROM score_events ORDER BY condition_id, ts"
        ).fetchall()

        doomed: list[tuple[str, float]] = []
        by_match: dict[str, int] = {}
        ratchets: dict[str, object] = {}
        previous: dict[str, tuple] = {}
        # One pass, in (condition_id, ts) order as selected, so each match's
        # rows arrive together and in the order the capture wrote them.
        for ts, cid, state, period, score, game, serving in rows:
            ratchet = ratchets.setdefault(cid, Ratchet())
            reading = Reading(state or "upcoming", period, parse_line(score))
            current = (state, period, score, game, serving)
            # Rejected outright, or left as a repeat by an earlier deletion.
            if not ratchet.accept(cid, reading) or current == previous.get(cid):
                doomed.append((cid, ts))
                by_match[cid] = by_match.get(cid, 0) + 1
                continue
            previous[cid] = current

        summary: dict[str, object] = {
            "rows": len(rows),
            "removing": len(doomed),
            "matches": len(by_match),
            "applied": apply,
        }
        if not apply or not doomed:
            return summary

        self.conn.execute("BEGIN")
        self.conn.executemany(
            "DELETE FROM score_events WHERE condition_id = ? AND ts = ?", doomed
        )
        # markets holds the latest reading, and it was written by the same
        # unfiltered path, so bring it back in step with what now survives.
        self.conn.execute(
            """
            UPDATE markets SET state = COALESCE((
                    SELECT s.state FROM score_events s
                    WHERE s.condition_id = markets.condition_id
                    ORDER BY s.ts DESC LIMIT 1), state),
                period = COALESCE((
                    SELECT s.period FROM score_events s
                    WHERE s.condition_id = markets.condition_id
                    ORDER BY s.ts DESC LIMIT 1), period),
                score = COALESCE((
                    SELECT s.score FROM score_events s
                    WHERE s.condition_id = markets.condition_id
                    ORDER BY s.ts DESC LIMIT 1), score)
            WHERE condition_id IN (SELECT DISTINCT condition_id FROM score_events)
            """
        )
        self.conn.execute("COMMIT")
        return summary

    def backfill_book_ts(
        self, apply: bool = False, batch: int = 50_000
    ) -> dict[str, object]:
        """Reconstruct `book_ts` for rows written before it was recorded.

        The true upstream timestamp of a past read is gone -- Polymarket keeps no
        book history -- but it can be inferred from what is stored. Snapshots are
        only written when the book changed, so a run of consecutive rows sharing a
        `book_hash` is one unchanged book seen repeatedly: the heartbeat writing
        it out. The book therefore last moved at the *first* row of that run, and
        every row in the run gets that timestamp.

        Two limits, which is why these rows are marked ``book_ts_derived = 1``:
        the value is the first time the hash was *seen*, so it is late by up to
        one polling interval (2-5s live, 60s before a match starts); and a change
        that happened while nothing was being recorded looks like it happened at
        the next read, understating the staleness across a capture outage.

        Only fills rows where `book_ts` is NULL, so it is safe to re-run and
        never overwrites a value the API actually reported. Applied in batches
        with a commit between each, so a capture writing new rows to the same
        file is never blocked for long.
        """
        pending = self.conn.execute(
            "SELECT COUNT(*) FROM books WHERE book_ts IS NULL"
        ).fetchone()[0]
        summary: dict[str, object] = {
            "rows": self.conn.execute("SELECT COUNT(*) FROM books").fetchone()[0],
            "pending": pending,
            "filled": 0,
            "applied": apply,
        }
        if not pending or not apply:
            return summary

        # The run boundaries have to be computed over a token's whole history, not
        # just the NULL rows, or a run split by an already-filled row would restart
        # and date the second half to the wrong read.
        self.conn.executescript(
            """
            DROP TABLE IF EXISTS _book_ts_fill;
            CREATE TEMP TABLE _book_ts_fill AS
            WITH marked AS (
                SELECT token_id, ts, book_ts, book_hash,
                       LAG(book_hash) OVER w AS previous_hash
                FROM books
                WINDOW w AS (PARTITION BY token_id ORDER BY ts)
            ), runs AS (
                SELECT token_id, ts, book_ts,
                       SUM(book_hash IS NOT previous_hash) OVER (
                           PARTITION BY token_id ORDER BY ts
                       ) AS run
                FROM marked
            )
            SELECT token_id, ts, MIN(ts) OVER (PARTITION BY token_id, run) AS derived
            FROM runs
            WHERE book_ts IS NULL;
            """
        )
        total = self.conn.execute("SELECT COUNT(*) FROM _book_ts_fill").fetchone()[0]
        filled = 0
        for start in range(0, total, batch):
            self.conn.execute("BEGIN IMMEDIATE")
            cur = self.conn.execute(
                """
                UPDATE books SET book_ts = f.derived, book_ts_derived = 1
                FROM _book_ts_fill f
                WHERE books.token_id = f.token_id AND books.ts = f.ts
                  AND f.rowid > ? AND f.rowid <= ?
                """,
                (start, start + batch),
            )
            filled += cur.rowcount if cur.rowcount > 0 else 0
            self.conn.execute("COMMIT")
        self.conn.execute("DROP TABLE IF EXISTS _book_ts_fill")
        summary["filled"] = filled
        return summary
