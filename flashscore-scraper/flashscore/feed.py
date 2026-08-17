"""Fetch and parse the Flashscore tennis feed.

Flashscore's web page is rendered client-side from a feed served by
``local-global.flashscore.ninja``. The feed is not JSON: it is a flat stream of
``KEY÷VALUE`` pairs joined by ``¬``, split into blocks by ``~``. A block that
carries an ``AA`` key is a match; a block with ``ZA`` is the tournament header
that the matches after it belong to.

The relevant match keys, worked out by inspecting live and finished matches:

===== ==========================================================
key   meaning
===== ==========================================================
AA    match id (used in the /match/tennis/<id>/ URL)
AD    start time, unix seconds
AB    stage: 1 scheduled, 2 live, 3 finished
AC    detailed status (see ``STATUS``); 17-21 mean set 1-5 in play
AE/AF home / away name
CA/CB home / away ranking
FU/FV home / away country
WU/WV home / away slug
AG/AH home / away sets won
BA/BB games in set 1, BC/BD set 2, BE/BF set 3, BG/BH set 4, BI/BJ set 5
DA/DB tiebreak in set 1, DC/DD set 2, DE/DF set 3, DG/DH set 4, DI/DJ set 5
WA/WB points in the game being played ("0", "15", "30", "40", "A")
WC    who is serving: 1 home, 2 away
AS    winner: 1 home, 2 away
===== ==========================================================

The day feed sits behind an edge cache that answers with copies up to a few
minutes old (it says ``no-store`` and then serves an ``age: 169``), so on its
own it lags the website badly on matches in play. The per-match feeds are not
cached that way, so live matches are topped up from those:

``dc_2_<id>``
    Live state — sets won, games in the current set, the points in the game
    being played, and who is serving.
``df_sur_2_<id>``
    Set-by-set history with tiebreaks, keyed the same way as the day feed but
    with one block per set.

See ``refresh_live``.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import httpx

from .models import Match, Player, SetScore, to_utc

FEED_HOST = "https://local-global.flashscore.ninja"
# Static signature the site sends on every feed request.
FSIGN = "SW9D1eZo"
TENNIS = 2

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "x-fsign": FSIGN,
    "Referer": "https://www.flashscore.com/",
    "Accept": "*/*",
}

STATUS = {
    "1": "Scheduled",
    "2": "Live",
    "3": "Finished",
    "4": "Postponed",
    "5": "Cancelled",
    "8": "Retired",
    "9": "Walkover",
    "17": "Set 1",
    "18": "Set 2",
    "19": "Set 3",
    "20": "Set 4",
    "21": "Set 5",
    "36": "Interrupted",
    "47": "Set 1 TB",
    "48": "Set 2 TB",
    "49": "Set 3 TB",
    "50": "Set 4 TB",
    "51": "Set 5 TB",
}

# (games home, games away, tiebreak home, tiebreak away) per set.
SET_KEYS = [
    ("BA", "BB", "DA", "DB"),
    ("BC", "BD", "DC", "DD"),
    ("BE", "BF", "DE", "DF"),
    ("BG", "BH", "DG", "DH"),
    ("BI", "BJ", "DI", "DJ"),
]


def parse_blocks(raw: str) -> list[dict[str, str]]:
    """Split the feed into its ``KEY÷VALUE`` blocks."""
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
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _parse_match(block: dict[str, str], tournament: str) -> Match:
    stage = block.get("AB")
    detail = block.get("AC", "")
    is_live = stage == "2"

    sets = []
    for home_key, away_key, home_tb, away_tb in SET_KEYS:
        home = _int(block, home_key)
        away = _int(block, away_key)
        if home is None and away is None:
            break
        sets.append(
            SetScore(
                home=home or 0,
                away=away or 0,
                home_tiebreak=_int(block, home_tb),
                away_tiebreak=_int(block, away_tb),
            )
        )

    serving = block.get("WC")
    return Match(
        id=block.get("AA", ""),
        tournament=tournament,
        home=Player(
            name=block.get("AE", "?"),
            slug=block.get("WU"),
            country=block.get("FU"),
            rank=_int(block, "CA"),
            serving=serving == "1",
        ),
        away=Player(
            name=block.get("AF", "?"),
            slug=block.get("WV"),
            country=block.get("FV"),
            rank=_int(block, "CB"),
            serving=serving == "2",
        ),
        status=STATUS.get(detail, STATUS.get(stage or "", "Unknown")),
        starts_at=to_utc(int(block.get("AD", "0"))),
        home_sets=_int(block, "AG"),
        away_sets=_int(block, "AH"),
        sets=sets,
        home_point=block.get("WA"),
        away_point=block.get("WB"),
        winner=_int(block, "AS"),
        is_live=is_live,
    )


def parse_feed(raw: str) -> list[Match]:
    """Turn a raw feed response into matches, carrying the tournament header down."""
    matches = []
    tournament = ""
    for block in parse_blocks(raw):
        if "ZA" in block:
            tournament = block["ZA"]
        elif "AA" in block and "AE" in block:
            matches.append(_parse_match(block, tournament))
    return matches


def _get(client: httpx.Client, feed: str) -> str:
    response = client.get(f"{FEED_HOST}/{TENNIS}/x/feed/{feed}", headers=HEADERS)
    response.raise_for_status()
    return response.text


def fetch_tennis(
    day: int = 0,
    tz: int = 1,
    client: httpx.Client | None = None,
    timeout: float = 20.0,
) -> list[Match]:
    """Fetch every tennis match Flashscore lists for a day.

    ``day`` is relative to today (-1 yesterday, 0 today, 1 tomorrow). ``tz`` is
    Flashscore's timezone bucket, which only shifts where the day boundary
    falls; it does not change the timestamps, which are always UTC.

    Scores for matches in play can be a few minutes behind — see
    ``refresh_live``, or use ``live_tennis``, which applies it.
    """
    owned = client is None
    client = client or httpx.Client(timeout=timeout)
    try:
        return parse_feed(_get(client, f"f_{TENNIS}_{day}_{tz}_en_1"))
    finally:
        if owned:
            client.close()


def _parse_sets(raw: str) -> list[SetScore]:
    """Read the set-by-set blocks of a ``df_sur_`` response."""
    sets = []
    for block in parse_blocks(raw):
        for home_key, away_key, home_tb, away_tb in SET_KEYS:
            if home_key not in block and away_key not in block:
                continue
            sets.append(
                SetScore(
                    home=_int(block, home_key) or 0,
                    away=_int(block, away_key) or 0,
                    home_tiebreak=_int(block, home_tb),
                    away_tiebreak=_int(block, away_tb),
                )
            )
            break
    return sets


def _refresh_one(match: Match, client: httpx.Client) -> None:
    """Overwrite one live match's score with the uncached per-match feeds."""
    state = parse_blocks(_get(client, f"dc_{TENNIS}_{match.id}"))
    if state:
        block = state[0]
        stage = block.get("DA")
        match.is_live = stage == "2"
        match.status = STATUS.get(block.get("DB", ""), STATUS.get(stage or "", match.status))
        match.home_sets = _int(block, "DE")
        match.away_sets = _int(block, "DF")
        match.home_point = block.get("DP")
        match.away_point = block.get("DQ")
        serving = block.get("DR")
        match.home.serving = serving == "1"
        match.away.serving = serving == "2"

    sets = _parse_sets(_get(client, f"df_sur_{TENNIS}_{match.id}"))
    if sets:
        match.sets = sets


def refresh_live(
    matches: list[Match],
    client: httpx.Client | None = None,
    timeout: float = 20.0,
    workers: int = 8,
) -> list[Match]:
    """Replace the scores of in-play matches with live ones, in place.

    Two small requests per live match, run concurrently. A match whose feeds
    fail keeps its day-feed score rather than dropping out.
    """
    live = [m for m in matches if m.is_live]
    if not live:
        return matches

    owned = client is None
    client = client or httpx.Client(timeout=timeout)

    def attempt(match: Match) -> None:
        try:
            _refresh_one(match, client)
        except (httpx.HTTPError, ValueError):
            pass

    try:
        with ThreadPoolExecutor(max_workers=min(workers, len(live))) as pool:
            list(pool.map(attempt, live))
    finally:
        if owned:
            client.close()
    return matches


def live_tennis(refresh: bool = True, **kwargs) -> list[Match]:
    """The matches currently in play, with live scores.

    Set ``refresh=False`` to skip the per-match feeds and take the day feed's
    scores as they come, which is one request but minutes behind.
    """
    client = kwargs.pop("client", None)
    owned = client is None
    client = client or httpx.Client(timeout=kwargs.get("timeout", 20.0))
    try:
        matches = [m for m in fetch_tennis(client=client, **kwargs) if m.is_live]
        if refresh:
            refresh_live(matches, client=client)
        return matches
    finally:
        if owned:
            client.close()
