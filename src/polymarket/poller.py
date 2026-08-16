"""The capture loop: snapshot every tracked order book on a fixed interval."""

from __future__ import annotations

import logging
import signal
import time
from dataclasses import dataclass

import httpx

from .api import Polymarket
from .book import parse_book
from .config import POLL_INTERVAL, REFRESH_INTERVAL
from .discovery import discover
from .store import Store

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Tracked:
    condition_id: str
    outcome_index: int
    outcome: str
    question: str


class Poller:
    def __init__(
        self,
        api: Polymarket,
        store: Store,
        interval: float = POLL_INTERVAL,
        refresh_interval: float = REFRESH_INTERVAL,
        all_markets: bool = False,
        include_qualifying: bool = False,
        only_changes: bool = False,
    ) -> None:
        self.api = api
        self.store = store
        self.interval = interval
        self.refresh_interval = refresh_interval
        self.all_markets = all_markets
        self.include_qualifying = include_qualifying
        self.only_changes = only_changes
        self.tracked: dict[str, Tracked] = {}
        self._last_hash: dict[str, str] = {}
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
        self.tracked = tracked
        for token in dropped:
            self._last_hash.pop(token, None)

        self.store.upsert_markets(kept)
        tournaments = sorted({m.tournament for m in kept})
        log.info(
            "refresh: tracking %d markets (%d tokens) | +%d -%d | %s",
            len(kept),
            len(tracked),
            len(added),
            len(dropped),
            ", ".join(tournaments) or "no ATP matches open",
        )
        for question in sorted({tracked[t].question for t in added})[:10]:
            log.info("  + %s", question)
        for skip in skipped[:10]:
            log.debug("skipped (%s): %s", skip.reason, skip.title)
        if len(skipped) > 10:
            log.debug("... and %d more skipped", len(skipped) - 10)

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
            if self.only_changes and snap.book_hash:
                if self._last_hash.get(token) == snap.book_hash:
                    continue
                self._last_hash[token] = snap.book_hash
            rows.append((snap, meta.condition_id, meta.outcome_index, meta.outcome))

        written = self.store.insert_snapshots(ts, rows) if rows else 0
        missing = len(self.tracked) - len(books)
        log.info(
            "tick: %d/%d books, %d rows, %.2fs%s",
            len(books),
            len(self.tracked),
            written,
            time.monotonic() - started,
            f", {missing} missing" if missing else "",
        )
        return written

    # ---------------- loop ----------------

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

            if time.monotonic() - last_refresh >= self.refresh_interval:
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
