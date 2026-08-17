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
    OVERDUE_RECHECK,
    OVERDUE_WINDOW,
    POLL_INTERVAL,
    REFRESH_INTERVAL,
    START_GRACE,
)
from .discovery import discover
from .store import Store

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Tracked:
    condition_id: str
    outcome_index: int
    outcome: str
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
        self.tracked: dict[str, Tracked] = {}
        self._last_fingerprint: dict[str, tuple] = {}
        self._last_write: dict[str, float] = {}
        self._next_start: float | None = None  # monotonic deadline
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

        self.store.upsert_markets(kept)
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
        if time.monotonic() - last_refresh >= self.refresh_interval:
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
