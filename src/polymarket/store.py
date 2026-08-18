"""SQLite persistence for market metadata and order-book snapshots."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from .book import Snapshot
from .config import BOOK_DEPTH
from .discovery import TennisMarket


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


def _depth_columns(depth: int) -> list[str]:
    cols: list[str] = []
    for side in ("bid", "ask"):
        for i in range(1, depth + 1):
            cols += [f"{side}_px_{i}", f"{side}_sz_{i}"]
    return cols


DEPTH_COLUMNS = _depth_columns(BOOK_DEPTH)

BOOK_COLUMNS = [
    "ts",
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
]

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
    ("raw", "TEXT"),
]

SCORE_EVENT_COLUMNS = [
    ("ts", "REAL"),
    ("condition_id", "TEXT"),
    ("state", "TEXT"),
    ("period", "TEXT"),
    ("score", "TEXT"),
    ("game", "TEXT"),
    ("serving", "INTEGER"),
]

_TEXT_COLUMNS = {"condition_id", "token_id", "outcome", "book_hash"}
_INT_COLUMNS = {"outcome_index"}


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

DROP VIEW IF EXISTS quotes;
CREATE VIEW quotes AS
SELECT
    b.ts,
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

    def record_score_events(self, scores: Iterable[ScoreRow]) -> int:
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
            rows.append((now, entry.condition_id, *current))

        if rows:
            self.conn.executemany(
                "INSERT OR REPLACE INTO score_events "
                "(ts, condition_id, state, period, score, game, serving) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        return len(rows)

    def insert_snapshots(
        self, ts: float, rows: Sequence[tuple[Snapshot, str, int, str]]
    ) -> int:
        """rows: (snapshot, condition_id, outcome_index, outcome_label)."""
        payload = []
        for snap, condition_id, index, outcome in rows:
            values: list[object] = [
                ts,
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
            values += [snap.market_last_trade, snap.book_hash]
            payload.append(tuple(values))

        placeholders = ",".join("?" * len(BOOK_COLUMNS))
        self.conn.executemany(
            f"INSERT OR REPLACE INTO books ({', '.join(BOOK_COLUMNS)}) VALUES ({placeholders})",
            payload,
        )
        return len(payload)

    def stats(self) -> dict[str, object]:
        cur = self.conn.cursor()
        markets = cur.execute("SELECT COUNT(*) FROM markets").fetchone()[0]
        snaps, first, last = cur.execute(
            "SELECT COUNT(*), MIN(ts), MAX(ts) FROM books"
        ).fetchone()
        by_tournament = cur.execute(
            """
            SELECT m.tournament, COUNT(DISTINCT m.condition_id), COUNT(b.ts)
            FROM markets m LEFT JOIN books b ON b.condition_id = m.condition_id
            GROUP BY m.tournament ORDER BY 3 DESC
            """
        ).fetchall()
        return {
            "db": str(self.path),
            "markets": markets,
            "snapshots": snaps,
            "first_ts": first,
            "last_ts": last,
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
