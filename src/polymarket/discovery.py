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
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable

import httpx

from .api import Polymarket
from .config import EXCLUDE, MATCH_SLUG, QUALIFYING, TENNIS_TAG_ID, match_tournament

log = logging.getLogger(__name__)


# Values of the event's "period" field once play is over. Anything else that
# looks like a set marker (S1..S5) means the match is under way.
FINISHED_PERIODS = {"FT", "CAN", "RET", "WO", "ABD", "POST"}


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
    outcomes: list[str]
    tokens: list[str]
    start_date: str | None
    end_date: str | None
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    @property
    def label(self) -> str:
        return f"[{self.tournament}] {self.question}"


def match_state(event: dict[str, Any]) -> str:
    """Classify a match as live / upcoming / ended from the event's score feed.

    A finished match keeps `acceptingOrders` true until it is resolved, sometimes
    for days, so "still tradeable" is not a usable stand-in for "still playing".
    """
    period = str(event.get("period") or "").upper()
    if event.get("ended") or period in FINISHED_PERIODS:
        return "ended"
    if event.get("live"):
        return "live"
    # Trust an in-progress set marker even if `live` flickers between polls.
    if period.startswith("S") and period[1:].isdigit():
        return "live"
    return "upcoming"


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


def discover(
    api: Polymarket,
    all_markets: bool = False,
    include_qualifying: bool = False,
    live_only: bool = False,
    max_pages: int = 60,
) -> tuple[list[TennisMarket], list[Skipped]]:
    """Return (markets to capture, notable skips).

    ``all_markets`` also captures the per-match derivatives (set winner, total
    sets / games over-under, completed-match) rather than just the moneyline.
    ``live_only`` keeps only matches that are actually being played.
    """
    kept: list[TennisMarket] = []
    skipped: list[Skipped] = []

    for event in _events(api, max_pages):
        title = str(event.get("title") or "")
        slug = str(event.get("slug") or "")

        parsed = MATCH_SLUG.match(slug)
        if parsed is None:
            continue  # outright/futures event, not a head-to-head
        if parsed.group("tour") != "atp" or parsed.group("doubles"):
            continue  # WTA, ITF, or doubles

        name = _tournament_of(title)
        tournament = match_tournament(name)
        if tournament is None:
            continue  # Challenger or unrecognised event
        if EXCLUDE.search(title):
            skipped.append(Skipped(title, slug, "non-tour format"))
            continue
        if QUALIFYING.search(title) and not include_qualifying:
            skipped.append(Skipped(title, slug, "qualifying"))
            continue

        state = match_state(event)
        if live_only and state != "live":
            # start_time rides along so the poller knows when to look again.
            skipped.append(Skipped(title, slug, state, event.get("startTime")))
            continue

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
                    state=state,
                    start_time=event.get("startTime"),
                    period=event.get("period"),
                    score=event.get("score"),
                    outcomes=outcomes,
                    tokens=token_ids,
                    start_date=market.get("startDate") or event.get("startDate"),
                    end_date=market.get("endDate") or event.get("endDate"),
                    raw=market,
                )
            )

    return kept, skipped
