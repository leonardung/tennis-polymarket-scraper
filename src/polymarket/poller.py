"""The capture loop: snapshot every tracked order book on a fixed interval."""

from __future__ import annotations

import logging
import signal
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from .api import Polymarket
from .book import Snapshot, parse_book
from .config import (
    HEARTBEAT,
    MIN_REFRESH_GAP,
    OVERDUE_RECHECK,
    OVERDUE_WINDOW,
    POLL_INTERVAL,
    REFRESH_INTERVAL,
    SCORE_INTERVAL,
    SCORE_LEAD,
    START_GRACE,
)
from .discovery import discover, markets_from_event
from .store import Store

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Tracked:
    condition_id: str
    outcome_index: int
    outcome: str
    question: str


@dataclass(frozen=True)
class Watched:
    """A match whose score feed can be re-read without a catalog page."""

    condition_id: str
    event_slug: str
    start_time: str | None
    question: str


def _seconds_from_now(iso: str | None) -> float | None:
    """Signed seconds from now to an ISO timestamp; negative if past, None if unusable."""
    if not iso:
        return None
    try:
        when = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (when - datetime.now(timezone.utc)).total_seconds()


def _seconds_until(iso: str | None) -> float | None:
    """Seconds until a future ISO timestamp, or None if unusable or already past."""
    delta = _seconds_from_now(iso)
    return delta if delta is not None and delta > 0 else None


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
        refresh_interval: float = REFRESH_INTERVAL,
        all_markets: bool = False,
        include_qualifying: bool = False,
        live_only: bool = True,
        only_changes: bool = True,
        heartbeat: float = HEARTBEAT,
        score_interval: float = SCORE_INTERVAL,
    ) -> None:
        self.api = api
        self.store = store
        self.interval = interval
        self.refresh_interval = refresh_interval
        self.all_markets = all_markets
        self.include_qualifying = include_qualifying
        self.live_only = live_only
        self.only_changes = only_changes
        self.heartbeat = heartbeat
        self.score_interval = score_interval
        self.tracked: dict[str, Tracked] = {}
        self.watched: dict[str, Watched] = {}  # by condition_id
        self._state: dict[str, str] = {}  # last seen live/upcoming/ended
        self._last_fingerprint: dict[str, tuple] = {}
        self._last_write: dict[str, float] = {}
        self._next_start: float | None = None  # monotonic deadline
        self._last_score: float = 0.0  # monotonic; 0 = poll on the first tick
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
        kept, skipped = discover(
            self.api,
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
        # forget matches that are no longer being followed.
        self.watched = {
            market.condition_id: Watched(
                condition_id=market.condition_id,
                event_slug=market.event_slug,
                start_time=market.start_time,
                question=market.question,
            )
            for market in kept
        }
        self._state = {m.condition_id: m.state for m in kept}
        self._state_changed = False

        self.store.upsert_markets(kept)
        self.store.record_score_events(kept)
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
            delta = _seconds_from_now(getattr(skip, "start_time", None))
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

    # ---------------- score feed ----------------

    def _score_slugs(self) -> list[str]:
        """Which matches are worth re-reading the score feed for right now.

        Every event comes back with its whole market list attached and there is
        no parameter to trim it, so the request costs roughly half a megabyte per
        eight matches. Asking only for matches whose score can actually move --
        those in play, and those close enough to their slot to start at any
        moment -- keeps a 10-second poll proportionate to what is on court
        rather than to how much of the draw happens to be in the database.
        """
        slugs = []
        for condition_id, watched in self.watched.items():
            state = self._state.get(condition_id, "upcoming")
            if state == "live":
                slugs.append(watched.event_slug)
                continue
            if state == "ended":
                continue
            delta = _seconds_from_now(watched.start_time)
            if delta is None:
                slugs.append(watched.event_slug)  # no scheduled time: keep watching
            elif -OVERDUE_WINDOW <= delta <= SCORE_LEAD:
                slugs.append(watched.event_slug)
        return sorted(set(slugs))

    def poll_scores(self) -> int:
        """Re-read the score feed for the matches in play. Returns rows written."""
        slugs = self._score_slugs()
        if not slugs:
            return 0

        readings = []
        for event in self.api.events_by_slug(slugs):
            found, _ = markets_from_event(
                event,
                all_markets=self.all_markets,
                include_qualifying=self.include_qualifying,
                # Never live_only: the whole point is to catch a match the moment
                # it leaves that state, which filtering it out would hide.
                live_only=False,
            )
            readings.extend(found)
        if not readings:
            return 0

        self.store.update_scores(readings)
        written = self.store.record_score_events(readings)

        moved = []
        for market in readings:
            previous = self._state.get(market.condition_id)
            if previous is not None and previous != market.state:
                moved.append((market.question, previous, market.state))
            self._state[market.condition_id] = market.state
        for question, before, after in moved:
            log.info("score feed: %s is now %s (was %s)", question, after, before)
        if moved:
            self._state_changed = True

        # Quiet on the common case -- most polls of a live match find the same
        # game still in progress -- but say so when the score actually moves.
        live = [m for m in readings if m.state == "live"]
        log.log(
            logging.INFO if written else logging.DEBUG,
            "score: %d match(es) polled, %d change(s)%s",
            len(slugs),
            written,
            "".join(f" | {m.period} {m.score}" for m in live) if written and live else "",
        )
        return written

    def _score_due(self) -> bool:
        """True when the score feed is due to be re-read.

        The poll can only run between ticks, so a deadline landing a hair after
        the tick that should have served it would wait out a whole extra tick --
        with the two intervals equal, that halves the sampling rate. Allowing
        half a tick of slack takes the nearest tick instead.
        """
        if self.score_interval <= 0:
            return False
        slack = self.interval / 2
        return time.monotonic() - self._last_score >= self.score_interval - slack

    # ---------------- one snapshot ----------------

    def tick(self) -> int:
        if not self.tracked:
            return 0
        ts = time.time()
        started = time.monotonic()
        books = self.api.books(list(self.tracked))

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
        missing = len(self.tracked) - len(books)
        unchanged = len(books) - written
        log.info(
            "tick: %d/%d books, %d rows%s, %.2fs%s",
            len(books),
            len(self.tracked),
            written,
            f" ({unchanged} unchanged)" if unchanged > 0 else "",
            time.monotonic() - started,
            f", {missing} missing" if missing else "",
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
            try:
                self.tick()
            except httpx.HTTPError as exc:
                log.error("tick failed (%s), continuing", exc)
            except Exception:  # noqa: BLE001 - a bad tick must not kill the capture
                log.exception("unexpected error in tick, continuing")

            if self._score_due():
                # Stamped before the call, not after, so a slow or failing poll
                # cannot push the next one further and further out.
                self._last_score = time.monotonic()
                try:
                    self.poll_scores()
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
