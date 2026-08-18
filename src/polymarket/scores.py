"""Live tennis scores from Flashscore.

Polymarket's own event payload carries ``live``, ``period`` and ``score``, and
that is where these used to come from. Gamma has no endpoint that returns a
score without the event's entire market list attached, and no parameter trims
it, so re-reading one score costs about 60 KB -- at ten-second resolution with
eight matches on court that is 50 KB/s spent on a handful of strings.

Flashscore's site is rendered client-side from feeds on
``local-global.flashscore.ninja``, and that is what this reads instead. They are
not JSON: a feed is a flat stream of ``KEY÷VALUE`` pairs joined by ``¬``, split
into blocks by ``~``. Two of them are used:

``f_2_<day>_<tz>_en_1``
    Every tennis match listed for one day -- ids, players, start times, status,
    set scores. One request covers the whole card, but it sits behind an edge
    cache that hands out copies a few minutes old (it sends ``no-store`` and
    then answers with ``age: 169``), so it cannot be the live source.

``df_sur_2_<id>``
    One match's status and set-by-set score, in about 200 bytes. Also edge
    cached, but briefly -- watched against matches in play, ``Age`` climbs to
    roughly two minutes and resets, and a set change was seen arriving 15
    seconds after it happened.

So the day feed is read on the market-list cadence to learn what exists, and
matches in play are topped up from the per-match feed on the book cadence. A
ten-second poll costs roughly 200 bytes per live match rather than 60 KB.

The caching is not just a delay, and this is the thing to know before changing
anything here. Requests are answered by a pool of edge caches holding copies of
different ages, so consecutive reads can return a score and then the score
before it. Taken at face value that writes one game down as three changes, two
of them going backwards -- which is what it did until ``Ratchet``. Two things
hold it off: every read goes over a single connection, which keeps them on one
cache, and every reading is put through the ratchet before it is written.

The feed is undocumented. ``FSIGN`` is a constant lifted from the site and the
keys are single letters with no promise they stay put -- if scores start coming
back empty, check ``_SET_KEYS`` and ``_STATUS`` here first.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, replace
from typing import Iterable, Sequence

import httpx

from .config import (
    FLASHSCORE_DAYS,
    FLASHSCORE_HOST,
    FLASHSCORE_SIGN,
    FLASHSCORE_TZ,
    SCORE_PATIENCE,
    SCORE_TIMEOUT,
    Tournament,
    match_tournament,
)

log = logging.getLogger(__name__)

SPORT = 2  # Flashscore's id for tennis

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "x-fsign": FLASHSCORE_SIGN,
    "Referer": "https://www.flashscore.com/",
    "Accept": "*/*",
}

# Flashscore heads each tournament's matches with a name. Tour-level men's
# singles is exactly this prefix: Challengers come through as "CHALLENGER MEN -
# SINGLES", which is the same distinction discovery makes on the Polymarket side
# and the reason a Challenger with a tour player in it cannot be mistaken for
# the real thing.
ATP_SINGLES = "ATP - SINGLES"

# Detailed status (`AC`) -> (state, period). The period vocabulary is the one
# the database already stores -- S1..S5 while a set is being played, FT/RET/WO
# and friends once it is over -- so the column keeps its meaning across the
# change of source. "INT" is new, and only Flashscore reports it.
_STATUS: dict[str, tuple[str, str | None]] = {
    "1": ("upcoming", None),
    "2": ("live", "LIVE"),  # in play, no set marker yet
    "3": ("ended", "FT"),
    "4": ("upcoming", "POST"),  # postponed: it will be played, just not now
    "5": ("ended", "CAN"),
    "8": ("ended", "RET"),
    "9": ("ended", "WO"),
    "17": ("live", "S1"),
    "18": ("live", "S2"),
    "19": ("live", "S3"),
    "20": ("live", "S4"),
    "21": ("live", "S5"),
    # Rain, bad light, a medical timeout that ran long. The match has not ended
    # and the book keeps trading -- often hard, since a delay is news -- so this
    # counts as live and stays captured.
    "36": ("live", "INT"),
    # A tiebreak is still that set being played; the 6-6 in the score line is
    # what says so, and the period stays the set marker.
    "47": ("live", "S1"),
    "48": ("live", "S2"),
    "49": ("live", "S3"),
    "50": ("live", "S4"),
    "51": ("live", "S5"),
}

# Coarse stage (`AB`), used only when the detailed status is one we don't know.
_STAGE = {"1": "upcoming", "2": "live", "3": "ended"}

# Games and tiebreak for each set: (home games, away games, home tb, away tb).
_SET_KEYS = (
    ("BA", "BB", "DA", "DB"),
    ("BC", "BD", "DC", "DD"),
    ("BE", "BF", "DE", "DF"),
    ("BG", "BH", "DG", "DH"),
    ("BI", "BJ", "DI", "DJ"),
)

# The live feed's keys, which are its own: `dc_` names things differently from
# the day card. Only the points are taken from it -- the status and the set
# scores come from `df_sur_`, which is read anyway.
_POINTS_HOME, _POINTS_AWAY = "DP", "DQ"

# What a point can read as inside a game. A tiebreak counts in plain numbers
# instead, so those are taken as they come.
_GAME_POINTS = frozenset({"0", "15", "30", "40", "A", "AD"})


def in_a_game(period: str | None) -> bool:
    """True while a set is being played, which is when points mean points."""
    return bool(period and len(period) == 2 and period[0] == "S" and period[1].isdigit())


def read_points(block: dict[str, str], period: str | None) -> tuple[str, str] | None:
    """The points in the game being played, if one is.

    Only while a set is in progress. Outside that these keys hold something
    else entirely -- a finished match reports 12 and 7, which are its total
    games -- and the period is what says which it is.
    """
    if not in_a_game(period):
        return None
    home, away = block.get(_POINTS_HOME, ""), block.get(_POINTS_AWAY, "")
    if home in _GAME_POINTS and away in _GAME_POINTS:
        return (home, away)
    if home.isdigit() and away.isdigit():
        return (home, away)  # tiebreak, counted in points rather than 15s
    return None


# Name fragments that carry no identity of their own. "de Minaur" and "Auger-
# Aliassime" still match on their distinctive parts, and dropping these stops a
# bare "de" or "van" from being read as agreement between two different players.
_PARTICLES = frozenset(
    "de del della van von der den da dos du la le el al bin ben mc mac st".split()
)


def parse_blocks(raw: str) -> list[dict[str, str]]:
    """Split a feed response into its ``KEY÷VALUE`` blocks."""
    blocks = []
    for chunk in raw.split("~"):
        block: dict[str, str] = {}
        for pair in chunk.split("¬"):
            key, sep, value = pair.partition("÷")
            if sep:
                block[key] = value
        if block:
            blocks.append(block)
    return blocks


def _int(block: dict[str, str], key: str) -> int | None:
    value = block.get(key)
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def name_tokens(text: str) -> frozenset[str]:
    """Break a player name into comparable pieces.

    Accents are folded and punctuation dropped, so "Auger-Aliassime",
    "auger-aliassime" and "Auger Aliassime" all come out the same. Single
    letters go too: Flashscore abbreviates first names to an initial, which
    would otherwise agree with every player sharing it.
    """
    folded = unicodedata.normalize("NFKD", text or "")
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    return frozenset(p for p in re.split(r"[^a-z0-9]+", folded.lower()) if len(p) > 1)


def _distinctive(tokens: Iterable[str]) -> set[str]:
    return {t for t in tokens if len(t) > 2 and t not in _PARTICLES}


@dataclass(frozen=True)
class SetScore:
    """Games won by each side in one set, and the tiebreak if there was one."""

    home: int
    away: int
    home_tiebreak: int | None = None
    away_tiebreak: int | None = None

    def render(self, flip: bool = False) -> str:
        home, away = (self.away, self.home) if flip else (self.home, self.away)
        tb = (
            (self.away_tiebreak, self.home_tiebreak)
            if flip
            else (self.home_tiebreak, self.away_tiebreak)
        )
        text = f"{home}-{away}"
        # Only annotate a set that has been decided. At 6-6 the tiebreak is
        # still being played, and its running score is not the set's result.
        if tb[0] is not None and tb[1] is not None and home != away:
            text += f"({min(tb[0], tb[1])})"
        return text


@dataclass(frozen=True)
class Reading:
    """What a feed says about one match right now."""

    state: str  # "live", "upcoming" or "ended"
    period: str | None  # "S2", "FT", "INT", ...
    sets: tuple[SetScore, ...] = ()
    # Points in the game being played, home then away: ("30", "40"). Raw counts
    # during a tiebreak. None whenever no game is in progress.
    points: tuple[str, str] | None = None

    def line(self, flip: bool = False) -> str | None:
        """The set scores as one string, e.g. ``"6-4, 6-7(3), 2-1"``.

        ``flip`` swaps the sides, which is how a score read home-away off
        Flashscore is re-expressed in the order Polymarket lists the players.
        """
        if not self.sets:
            return None
        return ", ".join(s.render(flip) for s in self.sets)

    def game(self, flip: bool = False) -> str | None:
        """The game in progress, e.g. ``"30-40"``, in the same order as `line`."""
        if self.points is None:
            return None
        home, away = self.points
        return f"{away}-{home}" if flip else f"{home}-{away}"

    @property
    def without_points(self) -> tuple[str, str | None, tuple[SetScore, ...]]:
        """Everything but the points, which move differently -- see Ratchet."""
        return (self.state, self.period, self.sets)


@dataclass(frozen=True)
class BoardMatch:
    """One match on the day card, ready to be paired with a Polymarket market."""

    id: str
    tournament: Tournament | None
    home: str
    away: str
    home_tokens: frozenset[str]
    away_tokens: frozenset[str]
    starts_at: float | None
    reading: Reading

    @property
    def label(self) -> str:
        return f"{self.home} vs {self.away}"


@dataclass(frozen=True)
class Paired:
    """A Polymarket market matched to a Flashscore one."""

    id: str
    # True when Flashscore's home player is Polymarket's second outcome, so
    # every score read for this match has to be swapped before it is stored.
    flip: bool
    reading: Reading
    label: str
    # False when the two players' names fit each other's side just as well, so
    # which way round the score goes cannot be told from the names alone.
    oriented: bool = True

    def render(self, reading: Reading) -> str | None:
        """A reading of this match as a score line, in the market's player order.

        Every score for this match goes through here, whether it came from the
        day card or from a later per-match read, so the one decision about which
        way round the players go is made once.
        """
        return reading.line(self.flip) if self.oriented else None

    def render_game(self, reading: Reading) -> str | None:
        """The points in the game being played, in the market's player order."""
        return reading.game(self.flip) if self.oriented else None

    @property
    def score(self) -> str | None:
        return self.render(self.reading)

    @property
    def game(self) -> str | None:
        return self.render_game(self.reading)


def _read_status(block: dict[str, str]) -> tuple[str, str | None] | None:
    """Turn a block's status keys into (state, period), or None if it has none."""
    detail = block.get("AC")
    if detail in _STATUS:
        return _STATUS[detail]
    stage = block.get("AB")
    if detail or stage:
        log.debug("unmapped flashscore status AC=%r AB=%r", detail, stage)
    if stage in _STAGE:
        return _STAGE[stage], None
    return None


def _read_sets(blocks: Sequence[dict[str, str]]) -> tuple[SetScore, ...]:
    """Collect the set scores out of one or more blocks.

    The day feed puts every set in the match's single block; the per-match feed
    puts one set per block. Indexing by set rather than by block order reads
    both, and keeps the sets in order whichever way they arrive.
    """
    by_index: dict[int, SetScore] = {}
    for block in blocks:
        for index, (home_key, away_key, home_tb, away_tb) in enumerate(_SET_KEYS):
            home, away = _int(block, home_key), _int(block, away_key)
            if home is None and away is None:
                continue
            by_index[index] = SetScore(
                home=home or 0,
                away=away or 0,
                home_tiebreak=_int(block, home_tb),
                away_tiebreak=_int(block, away_tb),
            )
    return tuple(by_index[i] for i in sorted(by_index))


def parse_board(raw: str) -> list[BoardMatch]:
    """Read a day feed into the tour-level men's singles matches it lists.

    A ``ZA`` block is a tournament header and applies to the matches after it;
    an ``AA`` block is a match.
    """
    matches: list[BoardMatch] = []
    heading = ""
    for block in parse_blocks(raw):
        if "ZA" in block:
            heading = block["ZA"]
            continue
        if "AA" not in block or "AE" not in block:
            continue
        if not heading.startswith(ATP_SINGLES):
            continue
        status = _read_status(block) or ("upcoming", None)
        matches.append(
            BoardMatch(
                id=block.get("AA", ""),
                tournament=match_tournament(heading),
                home=block.get("AE", "?"),
                away=block.get("AF", "?"),
                # The slug carries the full first name where the display name
                # has only an initial, so both feed the comparison.
                home_tokens=name_tokens(block.get("AE", "")) | name_tokens(block.get("WU", "")),
                away_tokens=name_tokens(block.get("AF", "")) | name_tokens(block.get("WV", "")),
                starts_at=float(_int(block, "AD") or 0) or None,
                reading=Reading(status[0], status[1], _read_sets([block])),
            )
        )
    return matches


def parse_reading(raw: str) -> Reading | None:
    """Read a ``df_sur_`` response, or None if it says nothing about the score.

    A match that has not started -- and one that never will, walkovers and
    cancellations included -- comes back with no status key at all. That is an
    absence of news rather than news, so the caller keeps what the day feed
    said instead of overwriting it with a guess.
    """
    blocks = parse_blocks(raw)
    if not blocks:
        return None
    status = _read_status(blocks[0])
    if status is None:
        return None
    return Reading(status[0], status[1], _read_sets(blocks))


class ScoreBoard:
    """A day's tour-level matches, indexed for pairing with Polymarket markets.

    Pairing is by tournament and by both players at once. Neither alone is
    enough -- surnames repeat across the draw and two players meet more than
    once a season -- but a tournament plus two names is unique in practice, and
    requiring both sides to agree is what makes a wrong pairing cost two
    independent coincidences rather than one.
    """

    def __init__(self, matches: Iterable[BoardMatch] = ()) -> None:
        self.matches: dict[str, BoardMatch] = {}
        for match in matches:
            # Adjacent day feeds overlap around the date boundary and list the
            # same fixture twice; first wins, they are identical.
            if match.id:
                self.matches.setdefault(match.id, match)
        self._by_tournament: dict[str, list[BoardMatch]] = {}
        for match in self.matches.values():
            if match.tournament is not None:
                self._by_tournament.setdefault(match.tournament.name, []).append(match)

    def __len__(self) -> int:
        return len(self.matches)

    def pair(
        self, tournament: str, players: Sequence[str], start_time: float | None = None
    ) -> Paired | None:
        """Find the match these two players are playing, in either order.

        ``players`` is in Polymarket's outcome order; the returned ``flip`` says
        whether that is the reverse of Flashscore's home/away, so the score can
        be stored the way the market lists it.
        """
        if len(players) != 2:
            return None
        first, second = (name_tokens(p) for p in players)

        scored: list[tuple[int, float, BoardMatch, bool]] = []
        for candidate in self._by_tournament.get(tournament, ()):
            for flip in (False, True):
                home, away = candidate.home_tokens, candidate.away_tokens
                if flip:
                    home, away = away, home
                agree = _distinctive(first & home), _distinctive(second & away)
                if not agree[0] or not agree[1]:
                    continue
                # More shared name parts is a better read; among equals, the
                # match nearest the market's start time wins, which separates
                # two meetings of the same pair inside the board's few days.
                apart = (
                    abs(candidate.starts_at - start_time)
                    if start_time and candidate.starts_at
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
                "score feed: %s vs %s is ambiguous at %s (%s), leaving it unpaired",
                players[0],
                players[1],
                tournament,
                ", ".join(m.label for _, _, m, _ in scored[:3]),
            )
            return None

        # The same match fitting both ways round means the names cannot say who
        # is who -- two brothers across the net, most of the time. Which match
        # it is remains certain, so the state is still worth having; the score
        # is dropped rather than published with a one-in-two chance of being
        # mirrored.
        oriented = not any(row[3] != best[3] for row in tied)
        if not oriented:
            log.warning(
                "score feed: cannot tell %s from %s in %s, keeping the state without a score",
                players[0],
                players[1],
                best[2].label,
            )

        return Paired(
            id=best[2].id,
            flip=best[3],
            reading=best[2].reading,
            label=best[2].label,
            oriented=oriented,
        )


class Flashscore:
    """Reads the two feeds. One HTTP client, reused."""

    def __init__(
        self,
        timeout: float = 20.0,
        days: Sequence[int] = FLASHSCORE_DAYS,
        tz: int = FLASHSCORE_TZ,
    ) -> None:
        self.days = tuple(days)
        self.tz = tz
        # One connection, deliberately. Flashscore answers from a pool of edge
        # caches that do not hold the same copy, and spreading requests over
        # several connections lands them on different ones -- which reads back
        # as the score jumping between its current and its previous value. A
        # single kept-alive connection stays on one cache and sees it advance.
        # It is also what the host wants: eight at once got the burst reset.
        self._client = httpx.Client(
            timeout=timeout,
            headers=HEADERS,
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
            transport=httpx.HTTPTransport(retries=2),
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "Flashscore":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _get(self, feed: str, timeout: float | None = None) -> str:
        response = self._client.get(
            f"{FLASHSCORE_HOST}/{SPORT}/x/feed/{feed}",
            **({"timeout": timeout} if timeout is not None else {}),
        )
        response.raise_for_status()
        return response.text

    def board(self) -> ScoreBoard:
        """The tour-level singles card across the configured days.

        Three days rather than one: the feed buckets matches by local date, so a
        night session lands on either side of the boundary depending on where it
        is played, and a match listed for tomorrow needs an id before it starts,
        not after. A day that fails to load is skipped rather than fatal -- a
        partial board still pairs most matches, and the next refresh retries.

        Losing every day is different, and raises. An empty board is a real
        answer -- a Monday between tournaments has no tour matches on it -- and
        the caller has to be able to tell that from the feed being unreachable,
        because one should replace what it knows and the other should not.
        """
        matches: list[BoardMatch] = []
        failure: Exception | None = None
        for day in self.days:
            try:
                matches += parse_board(self._get(f"f_{SPORT}_{day}_{self.tz}_en_1"))
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("score feed: day %+d unavailable (%s)", day, exc)
                failure = exc
        if failure is not None and not matches:
            raise failure
        return ScoreBoard(matches)

    def reading(self, match_id: str) -> Reading | None:
        """Re-read one match's score, and the game it is in, from its own feeds.

        Two small requests: ``df_sur_`` for the status and the set-by-set score,
        ``dc_`` for the points. The second is the optional one -- a score
        without its points is still a score, so a failure there is not allowed
        to lose the reading.
        """
        base = parse_reading(self._get(f"df_sur_{SPORT}_{match_id}", timeout=SCORE_TIMEOUT))
        # Nobody is serving between sets, before the start or during a rain
        # delay, so there is nothing to ask the second feed for.
        if base is None or not in_a_game(base.period):
            return base
        try:
            blocks = parse_blocks(self._get(f"dc_{SPORT}_{match_id}", timeout=SCORE_TIMEOUT))
        except (httpx.HTTPError, ValueError) as exc:
            log.debug("score feed: no points for %s (%s)", match_id, exc)
            return base
        if not blocks:
            return base
        return replace(base, points=read_points(blocks[0], base.period))

    def readings(self, match_ids: Sequence[str]) -> dict[str, Reading]:
        """Re-read several matches, one after another. Failures are simply absent.

        Sequential rather than concurrent: see the connection limit in
        ``__init__``. Each read is a couple of hundred bytes over a connection
        that is already open, so a court's worth of matches costs a fraction of
        the tick they are read on.
        """
        out: dict[str, Reading] = {}
        for match_id in match_ids:
            try:
                reading = self.reading(match_id)
            except (httpx.HTTPError, ValueError) as exc:
                log.debug("score feed: %s unavailable (%s)", match_id, exc)
                continue
            if reading is not None:
                out[match_id] = reading
        return out


# How far along a match is, ordered so that it can only ever increase. State
# comes first: a match that has ended is past one still being played, however
# the games read.
_STATE_RANK = {"upcoming": 0, "live": 1, "ended": 2}


def progress(reading: Reading) -> tuple[int, int, int, int]:
    """A reading's place in the match, as something that only moves forwards.

    Sets, games and tiebreak points accumulate and never come back, so
    comparing this is how a stale copy is told from a newer one.
    """
    return (
        _STATE_RANK.get(reading.state, 0),
        len(reading.sets),
        sum(s.home + s.away for s in reading.sets),
        sum((s.home_tiebreak or 0) + (s.away_tiebreak or 0) for s in reading.sets),
    )


class Ratchet:
    """Keeps each match's score moving forwards.

    Flashscore answers from whichever of its edge caches takes the request, and
    they do not all hold the same copy. Two reads seconds apart can return a
    score and then the score before it, over and over -- so one game played
    gets written down as three score changes, two of them backwards. The day
    card is staler still, and fights the per-match reads every refresh.

    A tennis score only advances, so a reading that has gone backwards is a
    stale copy and is dropped. Not forever, though: a scorer correcting a
    mistake also reads as going backwards, and would otherwise never land. The
    two are told apart by persistence -- a cache serving an old copy alternates
    with the fresh one, which resets the count, while a correction comes back
    every single read until it is taken.

    A reading that is level with the last one but labelled differently -- the
    same games, "INT" instead of "S2" while play is stopped for rain -- is not
    progress either, and is dropped for the same reason: it flaps.
    """

    def __init__(self, patience: int = SCORE_PATIENCE) -> None:
        self.patience = patience
        self._best: dict[str, Reading] = {}
        self._rejected: dict[str, int] = {}

    def latest(self, key: str) -> Reading | None:
        """The furthest-along reading accepted for this match so far."""
        return self._best.get(key)

    def accept(self, key: str, reading: Reading) -> bool:
        """True if this reading should be written down, and remember it if so.

        Points are exempt. They are the one part of a reading that legitimately
        goes backwards -- deuce comes round again and again -- so a reading that
        only differs there is passed through rather than measured.
        """
        best = self._best.get(key)
        unmoved = best is not None and reading.without_points == best.without_points
        if best is None or unmoved or progress(reading) > progress(best):
            self._best[key] = reading
            self._rejected[key] = 0
            return True

        rejected = self._rejected.get(key, 0) + 1
        if rejected < self.patience:
            self._rejected[key] = rejected
            log.debug(
                "score feed: ignoring a reading that went backwards (%s %s, had %s %s)",
                reading.period,
                reading.line(),
                best.period,
                best.line(),
            )
            return False

        # It has come back every read since; that is a correction, not a cache.
        log.info(
            "score feed: %s %s has stood for %d reads, taking it over %s %s",
            reading.period,
            reading.line(),
            rejected,
            best.period,
            best.line(),
        )
        self._best[key] = reading
        self._rejected[key] = 0
        return True

    def forget(self, keys: Iterable[str]) -> None:
        """Drop every match except these, which are the ones still followed."""
        keeping = set(keys)
        for key in list(self._best):
            if key not in keeping:
                self._best.pop(key, None)
                self._rejected.pop(key, None)
