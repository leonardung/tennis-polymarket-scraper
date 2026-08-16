"""Order-book parsing: top-of-book quotes plus N levels of depth per side."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import BOOK_DEPTH

Level = tuple[float, float]  # (price, size)


@dataclass
class Snapshot:
    token_id: str
    best_bid: float | None
    best_ask: float | None
    bids: list[Level]  # descending price, best first
    asks: list[Level]  # ascending price, best first
    last_trade_price: float | None
    book_hash: str | None
    api_timestamp: str | None

    @property
    def mid(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2

    @property
    def spread(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid


def _levels(raw: Any, *, best_first_high: bool, depth: int) -> list[Level]:
    """Normalize a side of the book to `depth` levels, best price first.

    The API's own ordering is not relied on -- levels are re-sorted here, so the
    same code is correct whether it returns bids ascending or descending.
    """
    parsed: list[Level] = []
    for level in raw or []:
        try:
            if isinstance(level, dict):
                price, size = float(level["price"]), float(level["size"])
            else:
                price, size = float(level[0]), float(level[1])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if size > 0:
            parsed.append((price, size))
    parsed.sort(key=lambda lvl: lvl[0], reverse=best_first_high)
    return parsed[:depth]


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_book(token_id: str, book: dict[str, Any], depth: int = BOOK_DEPTH) -> Snapshot:
    # The API returns bids ascending and asks descending (best price last in both).
    # _levels re-sorts rather than relying on that, so a change upstream cannot
    # silently invert every quote in the dataset.
    bids = _levels(book.get("bids"), best_first_high=True, depth=depth)
    asks = _levels(book.get("asks"), best_first_high=False, depth=depth)
    return Snapshot(
        token_id=token_id,
        best_bid=bids[0][0] if bids else None,
        best_ask=asks[0][0] if asks else None,
        bids=bids,
        asks=asks,
        last_trade_price=_as_float(book.get("last_trade_price")),
        book_hash=book.get("hash"),
        api_timestamp=book.get("timestamp"),
    )
