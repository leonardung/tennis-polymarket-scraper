"""Find ATP tour-level (250+) tennis match markets on Polymarket.

The filter is three independent gates, in order:

1. the event slug must parse as a head-to-head with an ``atp`` tour prefix and no
   ``doubles`` segment  -> men's singles;
2. the tournament name (the event title up to the first colon) must be on the ATP
   calendar in ``config.TOURNAMENTS``                        -> 250 or above;
3. the market must be open and accepting orders               -> actually tradeable.

Gate 1 is what separates ATP from WTA at combined events like Cincinnati, where
both draws share a tournament name. Gate 2 is what drops the Challenger circuit,
which shares the ``atp`` slug prefix.

Whether a match that passes is being played comes from Flashscore rather than
from Polymarket -- ``scores.py`` explains why -- so ``discover`` takes a
``ScoreBoard`` and pairs each market against it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

import httpx

from .api import Polymarket
from .config import (
    EXCLUDE,
    MATCH_SLUG,
    OVERDUE_WINDOW,
    QUALIFYING,
    TENNIS_TAG_ID,
    match_tournament,
)
from .scores import Paired, ScoreBoard

log = logging.getLogger(__name__)


def parse_iso(iso: str | None) -> float | None:
    """An ISO timestamp as unix seconds, or None if it is missing or malformed."""
    if not iso:
        return None
    try:
        when = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.timestamp()


def seconds_from_now(iso: str | None) -> float | None:
    """Signed seconds from now to an ISO timestamp; negative if past."""
    when = parse_iso(iso)
    if when is None:
        return None
    return when - datetime.now(timezone.utc).timestamp()


@dataclass
class TennisMarket:
    condition_id: str
    question: str
    slug: str
    event_slug: str
    event_title: str
    tournament: str
    tier: str
    tour: str
    match_date: str
    market_type: str  # "moneyline" or "derivative"
    state: str  # "live", "upcoming" or "ended"
    start_time: str | None
    period: str | None
    score: str | None
    game: str | None  # points in the game being played, "30-40"
    outcomes: list[str]
    tokens: list[str]
    start_date: str | None
    end_date: str | None
    # Which Flashscore match this is, filled in by apply_score. It lets the
    # score poll go straight to the right feed, and read what comes back in the
    # order this market lists its players, without pairing the board again.
    pairing: Paired | None = None
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    @property
    def label(self) -> str:
        return f"[{self.tournament}] {self.question}"


def unpaired_state(start_time: str | None) -> str:
    """What to assume about a match Flashscore does not have.

    There is no honest answer, so this picks the one that loses least. Before
    its slot it has not started. After it, the market is assumed to be in play
    for as long as a tennis match can credibly last: capturing a finished match
    for a few hours costs disk, whereas calling a live one finished loses the
    only copy of its book that will ever exist. Past that window the assumption
    stops, otherwise an abandoned match would be captured forever.
    """
    delta = seconds_from_now(start_time)
    if delta is None or delta > 0:
        return "upcoming"
    return "live" if -delta <= OVERDUE_WINDOW else "ended"


def apply_score(market: TennisMarket, board: ScoreBoard) -> bool:
    """Fill in a market's state, period and score from the board. True if paired."""
    paired = board.pair(market.tournament, market.outcomes, parse_iso(market.start_time))
    market.pairing = paired
    if paired is None:
        market.state = unpaired_state(market.start_time)
        market.period = market.score = market.game = None
        return False
    market.state = paired.reading.state
    market.period = paired.reading.period
    market.score = paired.score
    market.game = paired.game
    return True


@dataclass
class Skipped:
    title: str
    slug: str
    reason: str
    start_time: str | None = None


def _tournament_of(title: str) -> str:
    return title.split(":")[0].strip()


def _binary_tokens(market: dict[str, Any]) -> tuple[list[str], list[str]] | None:
    """Pull the two outcome labels and their CLOB token ids, or None if malformed."""
    outcomes = market.get("outcomes") or []
    tokens = market.get("clobTokenIds") or []
    if len(tokens) != 2 or not all(tokens):
        return None
    if len(outcomes) != 2:
        outcomes = ["Yes", "No"]
    return [str(o) for o in outcomes], [str(t) for t in tokens]


def _is_tradeable(market: dict[str, Any]) -> bool:
    return bool(
        market.get("acceptingOrders")
        and market.get("active")
        and not market.get("closed")
        and not market.get("archived")
    )


def _events(api: Polymarket, max_pages: int) -> Iterable[dict[str, Any]]:
    try:
        tag = api.tag_id("tennis") or TENNIS_TAG_ID
    except httpx.HTTPError:
        tag = TENNIS_TAG_ID
    return api.events(closed=False, tag_id=tag, max_pages=max_pages)


def markets_from_event(
    event: dict[str, Any],
    all_markets: bool = False,
    include_qualifying: bool = False,
) -> tuple[list[TennisMarket], list[Skipped]]:
    """Apply the three gates to a single event and build the markets it yields.

    The state, period and score fields are left at their defaults here; nothing
    in the Polymarket payload is trusted to fill them. ``discover`` sets them
    from the score board.
    """
    kept: list[TennisMarket] = []
    skipped: list[Skipped] = []

    title = str(event.get("title") or "")
    slug = str(event.get("slug") or "")

    parsed = MATCH_SLUG.match(slug)
    if parsed is None:
        return kept, skipped  # outright/futures event, not a head-to-head
    if parsed.group("tour") != "atp" or parsed.group("doubles"):
        return kept, skipped  # WTA, ITF, or doubles

    name = _tournament_of(title)
    tournament = match_tournament(name)
    if tournament is None:
        return kept, skipped  # Challenger or unrecognised event
    if EXCLUDE.search(title):
        skipped.append(Skipped(title, slug, "non-tour format"))
        return kept, skipped
    if QUALIFYING.search(title) and not include_qualifying:
        skipped.append(Skipped(title, slug, "qualifying"))
        return kept, skipped

    for market in event.get("markets") or []:
        if not isinstance(market, dict):
            continue
        question = str(market.get("question") or "")
        is_moneyline = question == title
        if not is_moneyline and not all_markets:
            continue
        if not _is_tradeable(market):
            if is_moneyline:
                skipped.append(Skipped(title, slug, "not accepting orders"))
            continue
        tokens = _binary_tokens(market)
        if tokens is None:
            skipped.append(Skipped(question, slug, "malformed token ids"))
            continue
        outcomes, token_ids = tokens
        condition_id = str(market.get("conditionId") or "")
        if not condition_id:
            continue

        kept.append(
            TennisMarket(
                condition_id=condition_id,
                question=question,
                slug=str(market.get("slug") or ""),
                event_slug=slug,
                event_title=title,
                tournament=tournament.name,
                tier=tournament.tier,
                tour="atp",
                match_date=parsed.group("date"),
                market_type="moneyline" if is_moneyline else "derivative",
                state="upcoming",
                start_time=event.get("startTime"),
                period=None,
                score=None,
                game=None,
                outcomes=outcomes,
                tokens=token_ids,
                start_date=market.get("startDate") or event.get("startDate"),
                end_date=market.get("endDate") or event.get("endDate"),
                raw=market,
            )
        )

    return kept, skipped


def discover(
    api: Polymarket,
    board: ScoreBoard | None = None,
    all_markets: bool = False,
    include_qualifying: bool = False,
    live_only: bool = False,
    max_pages: int = 60,
) -> tuple[list[TennisMarket], list[Skipped]]:
    """Return (markets to capture, notable skips).

    ``all_markets`` also captures the per-match derivatives (set winner, total
    sets / games over-under, completed-match) rather than just the moneyline.
    ``live_only`` keeps only matches that are actually being played, which is
    the board's verdict -- pass one, or every match reads as not started.
    """
    board = board if board is not None else ScoreBoard()
    kept: list[TennisMarket] = []
    skipped: list[Skipped] = []
    # Matches past their start time, split by whether the board could name them.
    guessed: list[TennisMarket] = []
    read = 0

    for event in _events(api, max_pages):
        found, missed = markets_from_event(
            event,
            all_markets=all_markets,
            include_qualifying=include_qualifying,
        )
        skipped.extend(missed)
        for market in found:
            paired = apply_score(market, board)
            if market.state != "upcoming":
                if paired:
                    read += 1
                else:
                    guessed.append(market)
            if live_only and market.state != "live":
                # start_time rides along so the poller knows when to look again.
                skipped.append(
                    Skipped(market.event_title, market.event_slug, market.state, market.start_time)
                )
                continue
            kept.append(market)

    # Worth saying out loud: these are running on the guess in unpaired_state
    # rather than on a score, and a run of them means the feed's names or its
    # tournament headings have moved.
    if guessed:
        log.warning(
            "score feed: no Flashscore match for %d of %d started match(es): %s",
            len(guessed),
            len(guessed) + read,
            ", ".join(sorted(m.question for m in guessed)[:5]),
        )

    return kept, skipped
