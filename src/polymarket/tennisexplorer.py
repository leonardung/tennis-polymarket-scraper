"""Betting odds from TennisExplorer, and the match ids they hang from.

TennisExplorer publishes, on each match's detail page, the pre-match odds every
bookmaker it tracks offered and the whole history of each one -- the opening
price, every move, and the time of the move. The odds sit in server-rendered
HTML on ``match-detail/?id=<id>``; there is no feed and no JSON. The same site's
daily list, ``matches/?type=<tour>-single&year=&month=&day=``, carries every
match of one day's draw with both players and its own id, which is what lets a
Polymarket market be paired to the page its odds live on.

Two facts about the data drive how this is used, and both were measured rather
than assumed:

* **The odds are pre-match.** Across a sample of finished matches the newest
  timestamp in every odds history fell at or before the scheduled start, and
  the whole odds block of a match that was in play was byte-identical across
  repeated reads minutes apart. Bookmakers close these markets at the first
  ball and TennisExplorer does not carry an in-play feed.

* **The page is large.** Every read is ~330 KB of HTML carrying the fixture
  list, both players' profiles, the head-to-head and four odds tabs, of which
  only the Home/Away tab is wanted. There is no lighter endpoint.

So this is a *slow* source being polled on the capture's fast tick, which is a
deliberate choice rather than an oversight: what it records is the same value
over and over until the match starts. It exists so the pre-match drift of the
bookmaker consensus can be read against Polymarket's own drift, and it is kept
out of the value feeds' telemetry because it is a page read rather than a
per-tick value feed.

The parsers here are regexes over that HTML, in the same spirit as the
Flashscore feed readers -- and carry the same warning: the markup is
undocumented and a site redesign breaks them silently. ``parse_odds`` returning
None rather than an empty list is the signal that it did.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Sequence

import httpx

from .config import (
    TENNISEXPLORER_DAYS,
    TENNISEXPLORER_HOST,
    TENNISEXPLORER_TIMEOUT,
    TENNISEXPLORER_TZ,
)
from .scores import _distinctive, name_tokens

log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en",
}

# The daily list's two URL shapes: one draw, one calendar day.
_TOUR_TYPES = {"atp": "atp-single", "wta": "wta-single"}

# A match's two rows in the daily list. The first carries the time, the first
# player and the match-detail link; the second (same id with a "b" suffix)
# carries the second player. `s` rows are scheduled and `r` rows have a result,
# and both are matches -- only the `s`-only sample would miss every match that
# has already been played today.
_DAY_ROW = re.compile(
    r'<tr[^>]*id="([sr]\d+)"[^>]*>(.*?)</tr>\s*<tr[^>]*id="\1b"[^>]*>(.*?)</tr>',
    re.S,
)
_DAY_TIME = re.compile(r'class="first time"[^>]*>(.*?)</td>', re.S)
_DAY_NAME = re.compile(
    r'class="t-name">(?:<span[^>]*>.*?</span>)?\s*<a href="/player/[^"]*">([^<]+)</a>'
)
_DAY_ID = re.compile(r'match-detail/\?id=(\d+)')

# The Home/Away odds tab, and one bookmaker row inside it. Splitting on the row
# opener rather than matching `<tr>...</tr>` is deliberate: each odds cell holds
# a nested table of that bookmaker's moves, so a non-greedy `</tr>` stops at the
# first history entry instead of the end of the row.
_ODDS_TAB = re.compile(r'id="oddsMenu-(\d+)-data"')
_ODDS_ROW = re.compile(r'<tr class="(?:one|two)">')
_ODDS_BOOK = re.compile(r'<span class="t">([^<]+)</span>')
_ODDS_PRICE = re.compile(r'<td class="(k1|k2)[^"]*"><div class="odds-in[^"]*">([0-9]+(?:\.[0-9]+)?)')


@dataclass(frozen=True)
class OddsQuote:
    """One bookmaker's Home/Away price, in TennisExplorer's own player order."""

    bookmaker: str
    home: float
    away: float


@dataclass(frozen=True)
class TeMatch:
    """One match on a day's list, ready to be paired with a Polymarket market."""

    id: str
    tour: str
    home: str
    away: str
    home_tokens: frozenset[str]
    away_tokens: frozenset[str]
    starts_at: float | None

    @property
    def label(self) -> str:
        return f"{self.home} vs {self.away}"


@dataclass(frozen=True)
class TePaired:
    """A Polymarket market matched to a TennisExplorer match.

    ``flip`` is True when TennisExplorer lists Polymarket's second outcome as
    its home player, so the page's two prices have to be swapped before they are
    stored in the market's own outcome order. ``oriented`` is False when the two
    names fit each other's side equally well; a mirrored price is worse than no
    price, so nothing is stored then -- the same call ``Paired`` makes.
    """

    id: str
    flip: bool
    label: str
    oriented: bool = True

    def render(self, quote: OddsQuote) -> tuple[float, float]:
        """The quote as (outcome 0, outcome 1) prices."""
        return (quote.away, quote.home) if self.flip else (quote.home, quote.away)


def parse_day(
    raw: str, tour: str, date: datetime, tz: int = TENNISEXPLORER_TZ
) -> list[TeMatch]:
    """Read a daily list into the matches it carries.

    The page's times are wall-clock in the site's own timezone, so the date the
    list was requested for is combined with them to give a start instant. That
    instant is only ever used to break a pairing tie, where a constant offset
    across all candidates cancels out -- but it is applied anyway so the value
    means what it says.
    """
    matches: list[TeMatch] = []
    for _match_id, first, second in _DAY_ROW.findall(raw):
        home = _DAY_NAME.search(first)
        away = _DAY_NAME.search(second)
        link = _DAY_ID.search(first) or _DAY_ID.search(second)
        if home is None or away is None or link is None:
            continue
        matches.append(
            TeMatch(
                id=link.group(1),
                tour=tour,
                home=home.group(1).strip(),
                away=away.group(1).strip(),
                home_tokens=name_tokens(home.group(1)),
                away_tokens=name_tokens(away.group(1)),
                starts_at=_start_of(first, date, tz),
            )
        )
    return matches


def _start_of(row: str, date: datetime, tz: int) -> float | None:
    found = _DAY_TIME.search(row)
    if found is None:
        return None
    text = re.sub(r"<[^>]+>", "", found.group(1))
    clock = re.search(r"(\d{1,2}):(\d{2})", text)
    if clock is None:
        return None
    zone = timezone(timedelta(hours=tz))
    when = datetime(date.year, date.month, date.day, tzinfo=zone) + timedelta(
        hours=int(clock.group(1)), minutes=int(clock.group(2))
    )
    return when.timestamp()


def parse_odds(raw: str) -> list[OddsQuote] | None:
    """Read the Home/Away odds tab. None when the tab is not there at all.

    An empty list is a real answer -- the page has the tab and no bookmaker is
    offering a price -- while None means the markup has moved and the parser is
    looking at the wrong thing, which the caller logs rather than mistaking for
    a market with no quotes.
    """
    tabs = list(_ODDS_TAB.finditer(raw))
    home = next((t for t in tabs if t.group(1) == "1"), None)
    if home is None:
        return None
    after = [t.start() for t in tabs if t.start() > home.start()]
    section = raw[home.end() : after[0] if after else len(raw)]

    quotes: list[OddsQuote] = []
    for row in _ODDS_ROW.split(section)[1:]:
        book = _ODDS_BOOK.search(row)
        if book is None:
            continue
        prices = {side: float(value) for side, value in _ODDS_PRICE.findall(row)}
        # A bookmaker with only one side quoted is not a two-way market; it is
        # skipped rather than stored half-known.
        if "k1" not in prices or "k2" not in prices:
            continue
        quotes.append(OddsQuote(book.group(1).strip(), prices["k1"], prices["k2"]))
    return quotes


class TeBoard:
    """A few days of matches, indexed for pairing with Polymarket markets.

    Pairing is by both players at once, as it is against Flashscore's board.
    The tournament is not part of the key here because the match's own page is
    reached by id and the two players meeting on one day's draw is unique in
    practice -- and because TennisExplorer names its tournaments differently
    from Polymarket ("Guadalajara 2 WTA" against "Guadalajara"), so requiring
    one to agree would reject more matches than it protects.
    """

    def __init__(self, matches: Iterable[TeMatch] = ()) -> None:
        self.matches: dict[str, TeMatch] = {}
        for match in matches:
            if match.id:
                self.matches.setdefault(match.id, match)
        self._by_tour: dict[str, list[TeMatch]] = {}
        for match in self.matches.values():
            self._by_tour.setdefault(match.tour, []).append(match)

    def __len__(self) -> int:
        return len(self.matches)

    def pair(
        self,
        tour: str,
        players: Sequence[str],
        start_time: float | None = None,
    ) -> TePaired | None:
        """Find the TennisExplorer match these two players are in, either way.

        ``players`` is in Polymarket's outcome order; ``flip`` says whether that
        is the reverse of the page's home/away. Ambiguity is refused, not
        guessed at, exactly as ``ScoreBoard.pair`` refuses it.
        """
        if len(players) != 2:
            return None
        first, second = (name_tokens(p) for p in players)

        scored: list[tuple[int, float, TeMatch, bool]] = []
        for candidate in self._by_tour.get(tour, ()):
            for flip in (False, True):
                home, away = candidate.home_tokens, candidate.away_tokens
                if flip:
                    home, away = away, home
                agree = _distinctive(first & home), _distinctive(second & away)
                if not agree[0] or not agree[1]:
                    # A two-letter surname -- Wu, Li, Ce, Te -- is genuinely
                    # distinctive of a player, but `_distinctive` drops it as if
                    # it were an initial, so a name like "Yibing Wu" leaves
                    # nothing to match. Fall back to the full token sets, still
                    # requiring *both* players to agree, which is what keeps it
                    # off a single shared surname. A loose match scores lower
                    # than a distinctive one, so it can never win over one.
                    loose = (first & home, second & away)
                    if not loose[0] or not loose[1]:
                        continue
                    agree = loose
                apart = (
                    abs(candidate.starts_at - start_time)
                    if start_time is not None and candidate.starts_at is not None
                    else float("inf")
                )
                scored.append((len(agree[0]) + len(agree[1]), -apart, candidate, flip))

        if not scored:
            return None
        scored.sort(key=lambda row: (-row[0], -row[1]))
        best = scored[0]
        tied = [row for row in scored[1:] if row[:2] == best[:2]]
        if any(row[2].id != best[2].id for row in tied):
            log.warning(
                "tennisexplorer: %s vs %s is ambiguous (%s), leaving it unpaired",
                players[0],
                players[1],
                ", ".join(m.label for _, _, m, _ in scored[:3]),
            )
            return None
        oriented = not any(row[3] != best[3] for row in tied)
        return TePaired(
            id=best[2].id,
            flip=best[3],
            label=best[2].label,
            oriented=oriented,
        )


class TennisExplorer:
    """The site reader. One HTTP client, reused, sequential."""

    def __init__(
        self,
        timeout: float = TENNISEXPLORER_TIMEOUT,
        days: Sequence[int] = TENNISEXPLORER_DAYS,
        tz: int = TENNISEXPLORER_TZ,
    ) -> None:
        self.days = tuple(days)
        self.tz = tz
        # One connection, and reads are sequential. This is a scrape of a site
        # that owes us nothing; the daily lists are a handful of requests per
        # refresh and the odds pages are one per match per tick. Keeping the
        # requests on one connection also keeps the arrival order the same as
        # the order they were asked for, which is what a log can be read against.
        self._client = httpx.Client(
            timeout=timeout,
            headers=HEADERS,
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
            transport=httpx.HTTPTransport(retries=2),
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "TennisExplorer":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def daily(self, tour: str, date: datetime) -> list[TeMatch]:
        """One specific day's list for one tour, however old.

        The daily-list URL is a calendar date, so the same request that serves
        today's card serves an arbitrary past one -- which is what makes a
        historical pairing possible. Raises on a transport failure rather than
        returning empty, so a caller backfilling many days can tell a day with
        no matches from a day that could not be read.
        """
        kind = _TOUR_TYPES.get(tour)
        if kind is None:
            return []
        response = self._client.get(
            f"{TENNISEXPLORER_HOST}/matches/",
            params={
                "type": kind,
                "year": f"{date.year:04d}",
                "month": f"{date.month:02d}",
                "day": f"{date.day:02d}",
            },
        )
        response.raise_for_status()
        return parse_day(response.text, tour, date, self.tz)

    def board(self, tours: Sequence[str], today: datetime | None = None) -> TeBoard:
        """The daily lists for `tours` across the configured days.

        More than one day for the same reason Flashscore's board reads three: a
        night session lands on either side of the local date boundary, and a
        match needs an id before it starts, not after. A day that fails is
        skipped rather than fatal; every day failing raises, because an empty
        board is a real answer and the caller must be able to tell them apart.
        """
        today = today or datetime.now(timezone.utc)
        matches: list[TeMatch] = []
        failure: Exception | None = None
        for offset in self.days:
            day = today + timedelta(days=offset)
            for tour in tours:
                kind = _TOUR_TYPES.get(tour)
                if kind is None:
                    continue
                try:
                    raw = self._client.get(
                        f"{TENNISEXPLORER_HOST}/matches/",
                        params={
                            "type": kind,
                            "year": f"{day.year:04d}",
                            "month": f"{day.month:02d}",
                            "day": f"{day.day:02d}",
                        },
                    )
                    raw.raise_for_status()
                    text = raw.text
                except (httpx.HTTPError, ValueError) as exc:
                    log.warning(
                        "tennisexplorer: %s %s unavailable (%s)", tour, day.date(), exc
                    )
                    failure = exc
                    continue
                matches += parse_day(text, tour, day, self.tz)
        if failure is not None and not matches:
            raise failure
        return TeBoard(matches)

    def odds(self, match_id: str) -> list[OddsQuote] | None:
        """Read one match's Home/Away odds page.

        None means the page could not be read or the odds tab is not there;
        an empty list is a page that has the tab with nothing in it.
        """
        try:
            response = self._client.get(
                f"{TENNISEXPLORER_HOST}/match-detail/", params={"id": match_id}
            )
            response.raise_for_status()
        except (httpx.HTTPError, ValueError) as exc:
            log.debug("tennisexplorer: odds page %s unavailable (%s)", match_id, exc)
            return None
        return parse_odds(response.text)
