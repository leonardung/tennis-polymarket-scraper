"""The capture loop: snapshot every tracked order book on a fixed grid.

Each match is read at its own cadence -- every tick while it is being played,
every `idle_interval` before it starts, not at all once it is over. The grid
itself never changes; `_due_matches` decides who is on it. Books and score for
one match are read in the same pass, so they share a timestamp.
"""

from __future__ import annotations

import logging
import signal
import time
from dataclasses import dataclass

import httpx

from .api import Polymarket
from .book import Snapshot, parse_book
from .config import (
    HEARTBEAT,
    IDLE_INTERVAL,
    MIN_REFRESH_GAP,
    OVERDUE_RECHECK,
    OVERDUE_WINDOW,
    POLL_INTERVAL,
    REFRESH_INTERVAL,
    SCORE_LEAD,
    START_GRACE,
)
from .discovery import discover, seconds_from_now
from .scores import Flashscore, Paired, Ratchet, ScoreBoard
from .store import ScoreRow, Store

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Tracked:
    condition_id: str
    outcome_index: int
    outcome: str
    question: str


@dataclass(frozen=True)
class Watched:
    """A match whose score can be re-read from its own Flashscore feed."""

    condition_id: str
    pairing: Paired
    start_time: str | None
    question: str


def _fingerprint(snap: Snapshot) -> tuple:
    """What we actually store. Deduplication compares this, not the API's own
    book hash, which also changes for levels deeper than we keep."""
    return (
        snap.best_bid,
        snap.best_ask,
        tuple(snap.bids),
        tuple(snap.asks),
        snap.market_last_trade,
    )


class Poller:
    def __init__(
        self,
        api: Polymarket,
        store: Store,
        interval: float = POLL_INTERVAL,
        idle_interval: float = IDLE_INTERVAL,
        refresh_interval: float = REFRESH_INTERVAL,
        all_markets: bool = False,
        include_qualifying: bool = False,
        live_only: bool = True,
        only_changes: bool = True,
        heartbeat: float = HEARTBEAT,
        scores: Flashscore | None = None,
    ) -> None:
        self.api = api
        self.store = store
        self.scores = scores if scores is not None else Flashscore()
        self.board = ScoreBoard()
        # Every score, from the day card or from a per-match read, goes through
        # here before it is written, so a stale copy cannot rewind one already
        # recorded. See Ratchet.
        self.ratchet = Ratchet()
        self.interval = interval
        self.idle_interval = idle_interval
        self.refresh_interval = refresh_interval
        self.all_markets = all_markets
        self.include_qualifying = include_qualifying
        self.live_only = live_only
        self.only_changes = only_changes
        self.heartbeat = heartbeat
        self.tracked: dict[str, Tracked] = {}
        self.watched: dict[str, Watched] = {}  # by condition_id
        self._state: dict[str, str] = {}  # last seen live/upcoming/ended
        self._last_fingerprint: dict[str, tuple] = {}
        self._last_write: dict[str, float] = {}
        self._last_poll: dict[str, float] = {}  # by condition_id: when it was last read
        self._retired: set[str] = set()  # condition ids already logged as finished
        self._next_start: float | None = None  # monotonic deadline
        self._state_changed = False
        self._stop = False

    # ---------------- lifecycle ----------------

    def install_signal_handlers(self) -> None:
        def handler(signum: int, _frame: object) -> None:
            log.info("received signal %s, finishing current tick and exiting", signum)
            self._stop = True

        signal.signal(signal.SIGINT, handler)
        signal.signal(signal.SIGTERM, handler)

    def refresh(self) -> None:
        # The day card is what turns a market into a Flashscore id, so it is
        # read here rather than on the tick. A board that cannot be loaded at
        # all keeps the previous one: stale ids still point at the right
        # per-match feeds, and those are what the score poll actually reads.
        try:
            self.board = self.scores.board()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("score board unavailable (%s), reusing the previous one", exc)

        kept, skipped = discover(
            self.api,
            self.board,
            all_markets=self.all_markets,
            include_qualifying=self.include_qualifying,
            live_only=self.live_only,
        )
        tracked: dict[str, Tracked] = {}
        for market in kept:
            for index, (token, outcome) in enumerate(zip(market.tokens, market.outcomes)):
                tracked[token] = Tracked(
                    condition_id=market.condition_id,
                    outcome_index=index,
                    outcome=outcome,
                    question=market.question,
                )

        added = set(tracked) - set(self.tracked)
        dropped = set(self.tracked) - set(tracked)
        finished = {self.tracked[token].question for token in dropped}
        self.tracked = tracked
        for token in dropped:
            self._last_fingerprint.pop(token, None)
            self._last_write.pop(token, None)

        # discover() has just read the score feed for everything it returned, so
        # its verdict is the authority here -- overwrite rather than merge, and
        # forget matches that are no longer being followed. A match the board
        # could not identify has no feed of its own to poll, so it is left out.
        self.watched = {
            market.condition_id: Watched(
                condition_id=market.condition_id,
                pairing=market.pairing,
                start_time=market.start_time,
                question=market.question,
            )
            for market in kept
            if market.pairing
        }
        for condition_id in set(self._last_poll) - self._followed():
            self._last_poll.pop(condition_id, None)
            self._retired.discard(condition_id)
        self._state_changed = False

        # The day card is minutes old, so for a match already being read on the
        # tick it is usually behind. Where it is, keep what is already known
        # rather than letting the refresh rewind the score every five minutes.
        for market in kept:
            if market.pairing is None:
                continue
            if not self.ratchet.accept(market.condition_id, market.pairing.reading):
                best = self.ratchet.latest(market.condition_id)
                if best is not None:
                    market.state = best.state
                    market.period = best.period
                    market.score = market.pairing.render(best)
                    market.game = market.pairing.render_game(best)
                    market.serving = market.pairing.render_server(best)
        self.ratchet.forget(m.condition_id for m in kept)
        self._state = {m.condition_id: m.state for m in kept}

        self.store.upsert_markets(kept)
        self.store.record_score_events(ScoreRow.of(m) for m in kept)
        self._schedule_next_start(skipped)

        # Only say "live" when that is what is being tracked; with
        # --include-upcoming most of these matches have not started.
        noun = "live match(es)" if self.live_only else "match(es)"
        log.info(
            "refresh: %d %s, %d tokens | +%d -%d",
            len({t.condition_id for t in tracked.values()}),
            noun,
            len(tracked),
            len(added),
            len(dropped),
        )
        for question in sorted({tracked[t].question for t in added}):
            log.info("  %s: %s", "live now" if self.live_only else "tracking", question)
        for question in sorted(finished):
            log.info("  %s: %s", "no longer live" if self.live_only else "dropped", question)
        if not tracked:
            waiting = sum(1 for s in skipped if s.reason == "upcoming")
            log.info("  nothing playing right now (%d match(es) scheduled)", waiting)

    def _schedule_next_start(self, skipped: list) -> None:
        """Refresh again when the next scheduled match is due to begin.

        Without this, a match starting between two refreshes would go unnoticed
        for up to a full refresh interval, losing the opening of its book.

        Tennis start times are "not before" times and matches routinely run late,
        so a match that is past its scheduled start but not yet live is re-checked
        more often -- but only for a bounded window, otherwise a postponed match
        would keep the fast cadence going indefinitely.
        """
        soonest: float | None = None
        overdue = False
        for skip in skipped:
            if skip.reason != "upcoming":
                continue
            delta = seconds_from_now(getattr(skip, "start_time", None))
            if delta is None:
                continue
            if delta > 0:
                soonest = delta if soonest is None else min(soonest, delta)
            elif -delta <= OVERDUE_WINDOW:
                overdue = True

        now = time.monotonic()
        if soonest is not None:
            self._next_start = now + soonest + START_GRACE
            log.info("  next match starts in %s", _human(soonest))
        elif overdue:
            self._next_start = now + OVERDUE_RECHECK
            log.info("  a match is past its start time, re-checking shortly")
        else:
            self._next_start = None

    # ---------------- cadence ----------------

    def _followed(self) -> set[str]:
        """Every match the last refresh left us reading, by condition id."""
        return {t.condition_id for t in self.tracked.values()} | set(self.watched)

    def _question(self, condition_id: str) -> str:
        watched = self.watched.get(condition_id)
        if watched is not None:
            return watched.question
        for meta in self.tracked.values():
            if meta.condition_id == condition_id:
                return meta.question
        return condition_id

    def _due_matches(self) -> set[str]:
        """Which matches to read on this tick, remembering that they were read.

        A match in play is read every tick, because that is what the five-second
        cadence is for: a point turns over about every 26 seconds and the book
        moves with it. A match that has not started is read every
        `idle_interval` instead -- its book drifts, its score has nothing to say
        until it starts, and polling a whole day's card with the live matches is
        most of the request volume for none of the data. A match that has
        finished is not read at all; its market lingers open for a while yet,
        but there is nothing left in it to record.

        Nothing is lost at the start of a match: going live moves it onto the
        fast cadence within one idle poll, and the refresh that a state change
        triggers reads the card again anyway.

        Call this once per tick. It stamps what it returns, so a second call in
        the same tick would find the idle matches not yet due.
        """
        now = time.monotonic()
        due: set[str] = set()
        retiring: list[str] = []
        for condition_id in self._followed():
            state = self._state.get(condition_id, "upcoming")
            if state == "ended":
                if condition_id not in self._retired:
                    self._retired.add(condition_id)
                    retiring.append(condition_id)
                continue
            self._retired.discard(condition_id)
            last = self._last_poll.get(condition_id)
            if state != "live" and last is not None and now - last < self.idle_interval:
                continue
            self._last_poll[condition_id] = now
            due.add(condition_id)
        for condition_id in retiring:
            log.info("finished, no longer polling: %s", self._question(condition_id))
        return due

    # ---------------- score feed ----------------

    def _score_targets(self, due: set[str] | None = None) -> list[Watched]:
        """Which matches are worth re-reading the score for right now.

        Each read is one small request, so this is aimed at the matches whose
        score can actually move: those in play, and those close enough to their
        slot to start at any moment. For everything else the day card has
        already said all there is to say, and the next refresh re-reads it.

        `due` is the set of matches this tick is reading, so a score and the
        book beside it stay on one cadence and in one pass. None means no
        cadence filter, which is what a one-off call wants.
        """
        targets = []
        for condition_id, watched in self.watched.items():
            if due is not None and condition_id not in due:
                continue
            state = self._state.get(condition_id, "upcoming")
            if state == "live":
                targets.append(watched)
                continue
            if state == "ended":
                continue
            delta = seconds_from_now(watched.start_time)
            # No scheduled time means no way to rule it out: keep watching.
            if delta is None or -OVERDUE_WINDOW <= delta <= SCORE_LEAD:
                targets.append(watched)
        return targets

    def poll_scores(self, due: set[str] | None = None) -> int:
        """Re-read the score for the matches in play. Returns rows written.

        `due` comes from `_due_matches`; see `_score_targets`.
        """
        targets = self._score_targets(due)
        if not targets:
            return 0

        # With --all-markets a match's derivatives share its Flashscore id, so
        # the feed is read once and applied to each market that wants it.
        readings = self.scores.readings(sorted({w.pairing.id for w in targets}))

        rows = []
        for watched in targets:
            reading = readings.get(watched.pairing.id)
            # No reading is not the same as a blank one: a match that has not
            # started, and one settled without play, both answer with nothing.
            # Keep what the board last said rather than inventing a state.
            if reading is None:
                continue
            # A read that has gone backwards is a stale copy of the feed, not
            # news; writing it turns one game into three score changes.
            if not self.ratchet.accept(watched.condition_id, reading):
                continue
            rows.append(
                ScoreRow(
                    condition_id=watched.condition_id,
                    state=reading.state,
                    period=reading.period,
                    score=watched.pairing.render(reading),
                    game=watched.pairing.render_game(reading),
                    serving=watched.pairing.render_server(reading),
                )
            )
        if not rows:
            return 0

        self.store.update_scores(rows)
        written = self.store.record_score_events(rows)

        moved = []
        for row in rows:
            previous = self._state.get(row.condition_id)
            if previous is not None and previous != row.state:
                moved.append((self.watched[row.condition_id].question, previous, row.state))
            self._state[row.condition_id] = row.state
        for question, before, after in moved:
            log.info("score feed: %s is now %s (was %s)", question, after, before)
        if moved:
            self._state_changed = True

        # Quiet on the common case -- most polls of a live match find the same
        # game still in progress -- but say so when the score actually moves.
        live = [r for r in rows if r.state == "live"]
        log.log(
            logging.INFO if written else logging.DEBUG,
            "score: %d match(es) polled, %d change(s)%s",
            len(targets),
            written,
            "".join(f" | {r.period} {r.score}" for r in live) if written and live else "",
        )
        return written

    # ---------------- one snapshot ----------------

    def tick(self, due: set[str] | None = None) -> int:
        """Snapshot every book due this tick. Returns rows written.

        `due` comes from `_due_matches`, which the loop calls once and hands to
        the score poll as well; None asks it here instead, which is what a
        one-off call wants.
        """
        if not self.tracked:
            return 0
        if due is None:
            due = self._due_matches()
        tokens = [token for token, meta in self.tracked.items() if meta.condition_id in due]
        if not tokens:
            return 0
        ts = time.time()
        started = time.monotonic()
        books = self.api.books(tokens)

        rows = []
        for token, book in books.items():
            meta = self.tracked.get(token)
            if meta is None:
                continue
            snap = parse_book(token, book)
            if self.only_changes and not self._changed(token, snap, ts):
                continue
            rows.append((snap, meta.condition_id, meta.outcome_index, meta.outcome))

        written = self.store.insert_snapshots(ts, rows) if rows else 0
        missing = len(tokens) - len(books)
        unchanged = len(books) - written
        waiting = len(self.tracked) - len(tokens)
        log.info(
            "tick: %d/%d books, %d rows%s, %.2fs%s%s",
            len(books),
            len(tokens),
            written,
            f" ({unchanged} unchanged)" if unchanged > 0 else "",
            time.monotonic() - started,
            f", {missing} missing" if missing else "",
            f", {waiting} token(s) not due" if waiting else "",
        )
        return written

    def _changed(self, token: str, snap: Snapshot, ts: float) -> bool:
        """True if this snapshot should be written.

        Unchanged books are skipped, except every `heartbeat` seconds: without
        that, a quiet market is indistinguishable from a stopped collector when
        you come to read the data back.
        """
        fingerprint = _fingerprint(snap)
        due = ts - self._last_write.get(token, 0.0) >= self.heartbeat
        if not due and self._last_fingerprint.get(token) == fingerprint:
            return False
        self._last_fingerprint[token] = fingerprint
        self._last_write[token] = ts
        return True

    # ---------------- loop ----------------

    def _refresh_due(self, last_refresh: float) -> bool:
        elapsed = time.monotonic() - last_refresh
        if elapsed >= self.refresh_interval:
            return True
        # A match starting or finishing changes what should be captured, so act
        # on it rather than waiting out the interval. Rate-limited: `live` is
        # known to flicker between polls, and a refresh pages the whole tennis
        # catalog -- a flapping match must not turn that into a loop.
        if self._state_changed and elapsed >= MIN_REFRESH_GAP:
            return True
        return self._next_start is not None and time.monotonic() >= self._next_start

    def run(self) -> None:
        self.install_signal_handlers()
        self.refresh()
        last_refresh = time.monotonic()
        grid_start = time.monotonic()
        tick_index = 0

        while not self._stop:
            # Decided once, for the books and the score alike: a match is read
            # by both or by neither, so its price and its score never drift
            # onto different clocks.
            due = self._due_matches()
            try:
                self.tick(due)
            except httpx.HTTPError as exc:
                log.error("tick failed (%s), continuing", exc)
            except Exception:  # noqa: BLE001 - a bad tick must not kill the capture
                log.exception("unexpected error in tick, continuing")

            # In the same pass as the books, so a price and the point it moved
            # on share a timestamp.
            try:
                self.poll_scores(due)
            except httpx.HTTPError as exc:
                log.warning("score poll failed (%s), continuing", exc)
            except Exception:  # noqa: BLE001 - the books matter more than the score
                log.exception("unexpected error in score poll, continuing")

            if self._refresh_due(last_refresh):
                try:
                    self.refresh()
                except Exception as exc:  # noqa: BLE001
                    log.error("refresh failed (%s), keeping previous market list", exc)
                last_refresh = time.monotonic()

            # Fixed grid rather than sleep(interval), so latency never accumulates.
            tick_index += 1
            target = grid_start + tick_index * self.interval
            now = time.monotonic()
            if target < now:  # fell behind: skip ahead to the next future slot
                missed = int((now - target) // self.interval) + 1
                tick_index += missed
                target = grid_start + tick_index * self.interval
                log.warning("behind schedule, skipped %d slot(s)", missed)
            while not self._stop and time.monotonic() < target:
                time.sleep(max(0.0, min(0.25, target - time.monotonic())))

        log.info("stopped")


def _human(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"
