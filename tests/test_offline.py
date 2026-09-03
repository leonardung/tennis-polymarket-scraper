"""Offline checks against fabricated payloads (no network required).

    uv run python tests/test_offline.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from polymarket.book import parse_book  # noqa: E402
from polymarket.config import BOOK_DEPTH, MATCH_SLUG, match_tournament  # noqa: E402
from polymarket.discovery import discover  # noqa: E402
from polymarket.store import Store  # noqa: E402

PASSED = 0


def check(name: str, condition: bool) -> None:
    global PASSED
    if not condition:
        raise AssertionError(f"FAILED: {name}")
    PASSED += 1
    print(f"  ok  {name}")


# --------------------------------------------------------------------------
# book parsing
# --------------------------------------------------------------------------

# Deliberately unsorted, and with a zero-size level that must be dropped.
BOOK = {
    "asset_id": "111",
    "hash": "abc",
    "timestamp": "1700000000000",
    "bids": [
        {"price": "0.40", "size": "500"},
        {"price": "0.55", "size": "100"},
        {"price": "0.50", "size": "250"},
        {"price": "0.45", "size": "0"},
        {"price": "0.44", "size": "900"},
    ],
    "asks": [
        {"price": "0.62", "size": "300"},
        {"price": "0.57", "size": "150"},
        {"price": "0.60", "size": "200"},
    ],
}


def test_book() -> None:
    print("\nbook parsing")
    snap = parse_book("111", BOOK)
    check("best bid is the highest bid", snap.best_bid == 0.55)
    check("best ask is the lowest ask", snap.best_ask == 0.57)
    check("every non-zero bid kept when the book is shallower than the cap", len(snap.bids) == 4)
    check("bids descending, best first", [p for p, _ in snap.bids] == [0.55, 0.50, 0.44, 0.40])
    check("asks ascending, best first", [p for p, _ in snap.asks] == [0.57, 0.60, 0.62])
    check("zero-size level dropped", 0.45 not in [p for p, _ in snap.bids])
    check("sizes preserved", snap.bids[0][1] == 100.0)
    check("mid", abs(snap.mid - 0.56) < 1e-9)
    check("spread", abs(snap.spread - 0.02) < 1e-9)

    # Reversed input must give the same answer -- API ordering is not trusted.
    flipped = {**BOOK, "bids": list(reversed(BOOK["bids"])), "asks": list(reversed(BOOK["asks"]))}
    check("order-insensitive", parse_book("111", flipped).bids == snap.bids)

    empty = parse_book("222", {"bids": [], "asks": []})
    check("empty book yields None quotes", empty.best_bid is None and empty.mid is None)

    one_sided = parse_book("333", {"bids": [{"price": "0.9", "size": "10"}], "asks": []})
    check("one-sided book keeps the bid", one_sided.best_bid == 0.9)
    check("one-sided book has no mid", one_sided.mid is None)

    # A live book runs to dozens of levels a side, so the cap -- not the book --
    # is what decides how much gets stored. Exercise it with more than we keep.
    deep = parse_book("444", {
        "bids": [{"price": f"{0.50 - i / 100:.2f}", "size": "10"} for i in range(BOOK_DEPTH + 5)],
        "asks": [{"price": f"{0.51 + i / 100:.2f}", "size": "10"} for i in range(BOOK_DEPTH + 5)],
    })
    check(f"depth capped at {BOOK_DEPTH} bids", len(deep.bids) == BOOK_DEPTH)
    check(f"depth capped at {BOOK_DEPTH} asks", len(deep.asks) == BOOK_DEPTH)
    check("the cap keeps the best levels", deep.bids[0][0] == 0.50 and deep.asks[0][0] == 0.51)


# --------------------------------------------------------------------------
# tournament + slug parsing
# --------------------------------------------------------------------------


def test_filters() -> None:
    print("\ntournament matching")
    check("slam", match_tournament("Wimbledon", "atp").name == "Wimbledon")
    check("masters full name", match_tournament("Cincinnati Open", "atp").name == "Cincinnati Open")
    check("alias", match_tournament("Western & Southern Open", "atp").name == "Cincinnati Open")
    check("hyphen alias", match_tournament("Monte-Carlo Masters", "atp").name == "Monte-Carlo")
    check("atp 250", match_tournament("Winston-Salem Open", "atp").name == "Winston-Salem")
    check("atp 250 by city", match_tournament("Geneva Open", "atp").name == "Geneva")
    check("challenger city not on tour", match_tournament("Sion", "atp") is None)
    check("challenger city 2", match_tournament("Kingston", "atp") is None)
    check("challenger city 3", match_tournament("Prague 2", "atp") is None)
    check("non-tennis rejected", match_tournament("Will the Fed cut rates?", "atp") is None)

    # The two calendars are looked up separately, because a combined event runs
    # both draws under one name and they are not the same tournament.
    check("wta 1000", match_tournament("Wuhan Open", "wta").name == "Wuhan Open")
    check("wta 500", match_tournament("Charleston Open", "wta").name == "Charleston")
    check("wta 250", match_tournament("Hobart International", "wta").name == "Hobart")
    check("wta slam", match_tournament("Roland Garros", "wta").name == "Roland Garros")
    check(
        "a combined event is on both calendars",
        match_tournament("Cincinnati Open", "wta").name == "Cincinnati Open",
    )
    check(
        "and reads as that tour's tier",
        (
            match_tournament("Cincinnati Open", "atp").tier,
            match_tournament("Cincinnati Open", "wta").tier,
        )
        == ("masters", "wta_1000"),
    )
    check(
        "a name only one tour plays is not on the other's calendar",
        match_tournament("Wuhan Open", "atp") is None
        and match_tournament("Shanghai Masters", "wta") is None,
    )
    check("an unknown tour matches nothing", match_tournament("Wimbledon", "itf") is None)

    print("\nslug parsing")
    m = MATCH_SLUG.match("atp-norrie-navone-2026-05-20")
    check("atp singles slug", m is not None and m.group("tour") == "atp")
    check("date captured", m.group("date") == "2026-05-20")
    check("not doubles", m.group("doubles") is None)
    d = MATCH_SLUG.match("atp-doubles-duncrib-bianshe-2026-05-19")
    check("doubles flagged", d is not None and d.group("doubles") == "doubles-")
    w = MATCH_SLUG.match("wta-bouzkov-stefani-2026-08-15")
    check("wta prefix", w is not None and w.group("tour") == "wta")
    i = MATCH_SLUG.match("itf-pohjola-siekano-2026-08-03")
    check("itf prefix", i is not None and i.group("tour") == "itf")
    check(
        "outright slug is not a match",
        MATCH_SLUG.match("cincinnati-open-winner-20260729185627610") is None,
    )
    check(
        "futures slug is not a match",
        MATCH_SLUG.match("2026-mens-us-open-winner-tennis") is None,
    )


# --------------------------------------------------------------------------
# discovery, against payloads shaped like the live API
# --------------------------------------------------------------------------


class FakeAPI:
    """Duck-types the methods discovery and the poller use."""

    def __init__(self, events: list[dict]) -> None:
        self._events = events

    def tag_id(self, slug: str) -> str | None:
        return "864"

    def events(self, **_params: object):
        return iter(self._events)


class FakeFlashscore:
    """Stands in for the score feed. `readings` is what the tick cadence hits."""

    def __init__(self, board=None, readings: dict | None = None) -> None:
        from polymarket.scores import ScoreBoard

        self._board = board if board is not None else ScoreBoard()
        self.readings_by_id = readings or {}
        self.calls: list[list[str]] = []

    def board(self):
        return self._board

    def readings(self, match_ids):
        wanted = list(match_ids)
        self.calls.append(wanted)
        return {i: self.readings_by_id[i] for i in wanted if i in self.readings_by_id}


def _board(*matches: tuple, tour: str = "atp") -> object:
    """Build a ScoreBoard the way parse_board would, from (players, status, sets).

    Each entry is ``(home, away, status_code, [(games, games), ...])`` with the
    status code Flashscore's `AC` key uses -- 17 for "set 1 in play", 3 for
    finished, and so on.
    """
    from polymarket.scores import BoardMatch, Reading, ScoreBoard, SetScore, name_tokens
    from polymarket.scores import _STATUS
    from polymarket.config import match_tournament

    built = []
    for index, (home, away, status, sets) in enumerate(matches):
        state, period = _STATUS[str(status)]
        built.append(
            BoardMatch(
                id=f"fs{index}" if tour == "atp" else f"{tour}{index}",
                tour=tour,
                tournament=match_tournament("Cincinnati Open", tour),
                home=home,
                away=away,
                home_tokens=name_tokens(home),
                away_tokens=name_tokens(away),
                starts_at=None,
                reading=Reading(state, period, tuple(SetScore(*s) for s in sets)),
            )
        )
    return ScoreBoard(built)


# The pair discovery's fixture event is about, as Flashscore would name them.
_ZANDSCHULP = ("Van de Zandschulp B.", "Griekspoor T.")


def _live_board(sets=((6, 3), (3, 1)), status: int = 18) -> object:
    return _board((*_ZANDSCHULP, status, sets))


# The WTA half of the same combined event, as the day card lists it.
_BOUZKOVA = ("Bouzkova M.", "Stefanini L.")


def _both_tours_board() -> object:
    """One board carrying both draws of the same tournament, as Cincinnati does."""
    from polymarket.scores import ScoreBoard

    atp, wta = _live_board(), _board((*_BOUZKOVA, 17, [(2, 1)]), tour="wta")
    return ScoreBoard(list(atp.matches.values()) + list(wta.matches.values()))


def _event(
    title: str,
    slug: str,
    markets: list[tuple[str, list[str], bool]],
    start_time: str = "2026-08-16T18:30:00Z",
) -> dict:
    """Build an event in the live shape: generic tags, several markets per match.

    Nothing here says whether the match is being played: that comes from the
    score board, which is why these carry no score fields at all.
    """
    return {
        "title": title,
        "slug": slug,
        "tags": [{"slug": t} for t in ("tennis", "sports", "games")],
        "startDate": "2026-08-16T10:00:00Z",
        "startTime": start_time,
        "markets": [
            {
                "conditionId": "0x%08x" % (abs(hash(question)) & 0xFFFFFFFF),
                "question": question,
                "slug": f"{slug}-m{n}",
                "outcomes": outcomes,
                "clobTokenIds": [f"{slug}-tok{n}a", f"{slug}-tok{n}b"],
                "closed": not tradeable,
                "active": True,
                "archived": False,
                "acceptingOrders": tradeable,
                "endDate": "2026-08-24T14:00:00Z",
            }
            for n, (question, outcomes, tradeable) in enumerate(markets)
        ],
    }


def _cincinnati_atp() -> dict:
    title = "Cincinnati Open: Botic van de Zandschulp vs Tallon Griekspoor"
    return _event(
        title,
        "atp-zandsch-grieksp-2026-08-13",
        [
            (title, ["Botic van de Zandschulp", "Tallon Griekspoor"], True),
            (f"Cincinnati Open: Completed Match: {title.split(': ')[1]}", ["Yes", "No"], True),
            (
                "Botic van de Zandschulp vs. Tallon Griekspoor: Total Sets O/U 2.5",
                ["Over 2.5", "Under 2.5"],
                True,
            ),
        ],
    )


def test_discovery() -> None:
    print("\ndiscovery")
    wta_title = "Cincinnati Open: Marie Bouzkova vs Lucrezia Stefanini"
    events = [
        _cincinnati_atp(),
        # same tournament, WTA draw -- only the slug prefix separates them
        _event(
            wta_title,
            "wta-bouzkov-stefani-2026-08-15",
            [(wta_title, ["Marie Bouzkova", "Lucrezia Stefanini"], True)],
        ),
        # wta slug prefix, but a 125 -> below WTA 250
        _event(
            "Contrexeville: Player C vs Player D",
            "wta-playerc-playerd-2026-08-16",
            [("Contrexeville: Player C vs Player D", ["Player C", "Player D"], True)],
        ),
        # atp slug prefix, but a Challenger -> below ATP 250
        _event(
            "Sion: Player A vs Player B",
            "atp-playera-playerb-2026-08-16",
            [("Sion: Player A vs Player B", ["Player A", "Player B"], True)],
        ),
        _event(
            "Todi (Doubles): Aboian/Torres vs Dellien/Mena",
            "atp-doubles-aboitor-dellmen-2026-08-11",
            [
                (
                    "Todi (Doubles): Aboian/Torres vs Dellien/Mena",
                    ["Aboian/Torres", "Dellien/Mena"],
                    True,
                )
            ],
        ),
        _event(
            "ITF M15 Targu Jiu Men: A vs B",
            "itf-coreisa-miletic-2026-08-12",
            [("ITF M15 Targu Jiu Men: A vs B", ["A", "B"], True)],
        ),
        # outright / futures, not a head-to-head
        _event(
            "Cincinnati Open: Winner",
            "cincinnati-open-winner-20260729185627610",
            [("Will Carlos Alcaraz Win the Cincinnati Open?", ["Yes", "No"], True)],
        ),
    ]

    kept, _ = discover(FakeAPI(events), _both_tours_board())
    check("both moneylines kept", len(kept) == 2)
    atp = [m for m in kept if m.tour == "atp"]
    wta = [m for m in kept if m.tour == "wta"]
    check("right match", atp[0].question.startswith("Cincinnati Open: Botic"))
    check("tournament", atp[0].tournament == "Cincinnati Open")
    check("tier", atp[0].tier == "masters")
    check("match date from slug", atp[0].match_date == "2026-08-13")
    check("typed as moneyline", atp[0].market_type == "moneyline")
    check("player-name outcomes", atp[0].outcomes[1] == "Tallon Griekspoor")

    # The same tournament name, the other draw: only the slug prefix says so,
    # and it is what decides which calendar the name is looked up on.
    check("the wta draw at the same event is kept too", len(wta) == 1)
    check("and is the wta match", wta[0].question.startswith("Cincinnati Open: Marie"))
    check("tiered on the wta calendar", wta[0].tier == "wta_1000")
    check("paired against the wta half of the board", wta[0].pairing is not None)
    check("with the wta score, not the atp one", wta[0].score == "2-1")
    check("challenger excluded", not any("Sion" in m.question for m in kept))
    check("wta 125 excluded", not any("Contrexeville" in m.question for m in kept))
    check("doubles excluded", not any("Doubles" in m.question for m in kept))
    check("itf excluded", not any("ITF" in m.question for m in kept))
    check("outright excluded", not any("Will Carlos" in m.question for m in kept))

    only_atp, _ = discover(FakeAPI(events), _both_tours_board(), tours=("atp",))
    check("one tour can be asked for on its own", len(only_atp) == 1)
    check("and it is that one", only_atp[0].tour == "atp")

    everything, _ = discover(FakeAPI(events), _both_tours_board(), all_markets=True)
    check("all-markets picks up derivatives", len(everything) == 4)
    check(
        "derivatives typed",
        sorted({m.market_type for m in everything}) == ["derivative", "moneyline"],
    )

    stale = _cincinnati_atp()
    stale["markets"][0]["acceptingOrders"] = False
    kept2, skipped2 = discover(FakeAPI([stale]))
    check("moneyline not accepting orders is dropped", kept2 == [])
    check("and reported", any(s.reason == "not accepting orders" for s in skipped2))

    qual = _event(
        "Cincinnati Open, Qualification: A vs B",
        "atp-aaa-bbb-2026-08-10",
        [("Cincinnati Open, Qualification: A vs B", ["A", "B"], True)],
    )
    check("qualifying excluded by default", discover(FakeAPI([qual]))[0] == [])
    check(
        "qualifying opt-in works",
        len(discover(FakeAPI([qual]), None, include_qualifying=True)[0]) == 1,
    )


def test_live_filter() -> None:
    print("\nlive-match filter")

    def match(a, b, slug, start="2026-08-16T20:15:00Z"):
        title = f"Cincinnati Open: {a} vs {b}"
        return _event(title, slug, [(title, [a, b], True)], start)

    events = [
        match("Playing Now", "Opponent One", "atp-aaa-bbb-2026-08-16"),
        match("Later Today", "Opponent Two", "atp-ccc-ddd-2026-08-16", "2099-01-01T00:00:00Z"),
        match("Already Done", "Opponent Three", "atp-eee-fff-2026-08-16"),
        match("Called Off", "Opponent Four", "atp-ggg-hhh-2026-08-14"),
    ]
    board = _board(
        ("Now P.", "One O.", 18, [(6, 3), (3, 1)]),
        ("Today L.", "Two O.", 1, []),
        ("Done A.", "Three O.", 3, [(4, 6), (2, 6)]),
        ("Off C.", "Four O.", 5, []),
    )

    everything, _ = discover(FakeAPI(events), board)
    check("without live_only, all four are returned", len(everything) == 4)
    check("every one of them was paired", all(m.pairing for m in everything))

    live, skipped = discover(FakeAPI(events), board, live_only=True)
    check("live_only keeps just the live match", len(live) == 1)
    check("and it is the right one", live[0].question.endswith("Opponent One"))
    check("state recorded", live[0].state == "live")
    check("period recorded", live[0].period == "S2")
    check("score recorded", live[0].score == "6-3, 3-1")
    reasons = sorted({s.reason for s in skipped})
    check("skips are labelled by state", reasons == ["ended", "upcoming"])
    check(
        "finished match is not polled",
        not any("Already Done" in m.question for m in live),
    )
    check(
        "cancelled match is not polled",
        not any("Called Off" in m.question for m in live),
    )
    check(
        "upcoming carries its start time for scheduling",
        any(s.reason == "upcoming" and s.start_time for s in skipped),
    )

    # A match the board has never heard of has to be guessed at, and the guess
    # is bounded by how long a tennis match can credibly last.
    from polymarket.discovery import unpaired_state

    check("before its slot, not started", unpaired_state(_iso(600)) == "upcoming")
    check("no start time at all, not started", unpaired_state(None) == "upcoming")
    check("just past its slot, assumed in play", unpaired_state(_iso(-1800)) == "live")
    check("still assumed in play hours later", unpaired_state(_iso(-5 * 3600)) == "live")
    check("but not a day later", unpaired_state(_iso(-24 * 3600)) == "ended")

    unknown = [match("Nobody Knows", "This Match", "atp-nnn-kkk-2026-08-16")]
    guessed, _ = discover(FakeAPI(unknown), board)
    check("an unpaired match still comes back", len(guessed) == 1)
    check("with no feed id", guessed[0].pairing is None)
    check("and no invented score", guessed[0].score is None and guessed[0].period is None)


# --------------------------------------------------------------------------
# storage round-trip
# --------------------------------------------------------------------------


def test_store() -> None:
    print("\nstorage")
    kept, _ = discover(FakeAPI([_cincinnati_atp()]), _live_board())
    market = kept[0]

    with tempfile.TemporaryDirectory() as tmp:
        with Store(Path(tmp) / "t.db") as store:
            check("markets upserted", store.upsert_markets(kept) == 1)
            store.upsert_markets(kept)  # idempotent
            ts = time.time()
            book = dict(BOOK, last_trade_price="0.56")
            snap_a = parse_book(market.tokens[0], book)
            snap_b = parse_book(market.tokens[1], book)
            written = store.insert_snapshots(
                ts,
                [
                    (snap_a, market.condition_id, 0, market.outcomes[0]),
                    (snap_b, market.condition_id, 1, market.outcomes[1]),
                ],
            )
            check("two snapshots written", written == 2)

            row = store.conn.execute(
                "SELECT best_bid, best_ask, bid_px_1, bid_sz_1, ask_px_3, ask_sz_3, outcome,"
                " market_last_trade FROM books WHERE token_id = ?",
                (market.tokens[0],),
            ).fetchone()
            check("best bid stored", row[0] == 0.55)
            check("best ask stored", row[1] == 0.57)
            check("depth level 1 stored", (row[2], row[3]) == (0.55, 100.0))
            check("depth level 3 stored", (row[4], row[5]) == (0.62, 300.0))
            check("outcome label stored", row[6] == "Botic van de Zandschulp")
            check("market last trade stored", row[7] == 0.56)
            # The API reports one last-trade price per market on BOTH tokens,
            # oriented to whichever side traded last -- so it must never be read
            # as "this outcome's" price. Verified against live Cincinnati books.
            both = store.conn.execute(
                "SELECT DISTINCT market_last_trade FROM books WHERE condition_id = ?",
                (market.condition_id,),
            ).fetchall()
            check("last trade is per-market, identical on both tokens", both == [(0.56,)])

            quote = store.conn.execute(
                "SELECT buy_price, sell_price, question, tournament, match_date"
                " FROM quotes LIMIT 1"
            ).fetchone()
            check("view: buy = best ask", quote[0] == 0.57)
            check("view: sell = best bid", quote[1] == 0.55)
            check("view joins market metadata", quote[3] == "Cincinnati Open")
            check("view exposes match date", quote[4] == "2026-08-13")

            store.insert_snapshots(ts, [(snap_a, market.condition_id, 0, market.outcomes[0])])
            count = store.conn.execute("SELECT COUNT(*) FROM books").fetchone()[0]
            check("replaying the same tick does not duplicate rows", count == 2)

            stats = store.stats()
            check("stats counts markets", stats["markets"] == 1)
            check("stats counts snapshots", stats["snapshots"] == 2)

        with Store(Path(tmp) / "t.db") as store:
            check("schema is idempotent", store.stats()["snapshots"] == 2)


def test_migration() -> None:
    """A database written by an older version must be upgraded, not rejected."""
    print("\nschema migration")
    import sqlite3

    from polymarket.store import BOOK_COLUMNS

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "old.db"
        # a v1 database: markets without match_date/market_type, books without
        # market_last_trade, and a stale column that no longer exists in the code
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE markets (condition_id TEXT PRIMARY KEY, question TEXT,
                tour_reason TEXT, first_seen REAL, last_seen REAL, raw TEXT);
            CREATE TABLE books (ts REAL, token_id TEXT, best_bid REAL,
                PRIMARY KEY (token_id, ts));
            CREATE VIEW quotes AS SELECT ts FROM books;
            INSERT INTO markets (condition_id, question) VALUES ('0xold', 'legacy row');
            """
        )
        conn.commit()
        conn.close()

        with Store(path) as store:
            cols = {r[1] for r in store.conn.execute("PRAGMA table_info(markets)")}
            check("missing market columns added", {"match_date", "market_type"} <= cols)
            check("obsolete column left alone", "tour_reason" in cols)
            book_cols = {r[1] for r in store.conn.execute("PRAGMA table_info(books)")}
            check("missing book columns added", set(BOOK_COLUMNS) <= book_cols)
            check("existing rows survive", store.conn.execute(
                "SELECT question FROM markets WHERE condition_id='0xold'").fetchone()[0] == "legacy row")

            # the stale view must have been rebuilt against the new columns
            check("view rebuilt", store.conn.execute(
                "SELECT COUNT(*) FROM pragma_table_info('quotes') WHERE name='buy_price'"
            ).fetchone()[0] == 1)

            # and a real insert must now work
            kept, _ = discover(FakeAPI([_cincinnati_atp()]), _live_board())
            check("insert works after migration", store.upsert_markets(kept) == 1)
            snap = parse_book(kept[0].tokens[0], BOOK)
            check("snapshot insert works after migration",
                  store.insert_snapshots(time.time(),
                                         [(snap, kept[0].condition_id, 0, kept[0].outcomes[0])]) == 1)


def test_encoded_fields() -> None:
    print("\ngamma field decoding")
    from polymarket.api import _decode

    raw = {
        "question": "A vs B",
        "outcomes": json.dumps(["A", "B"]),
        "clobTokenIds": json.dumps(["1", "2"]),
        "outcomePrices": json.dumps(["0.6", "0.4"]),
    }
    decoded = _decode(dict(raw))
    check("outcomes decoded to list", decoded["outcomes"] == ["A", "B"])
    check("token ids decoded to list", decoded["clobTokenIds"] == ["1", "2"])
    check("prices decoded to list", decoded["outcomePrices"] == ["0.6", "0.4"])
    nested = _decode({"markets": [dict(raw)]})
    check("nested markets decoded", nested["markets"][0]["outcomes"] == ["A", "B"])
    check("malformed json left alone", _decode({"outcomes": "not json"})["outcomes"] == "not json")


# --------------------------------------------------------------------------
# poll loop
# --------------------------------------------------------------------------


def _overdue_reschedules() -> bool:
    """A match past its start time but not yet live must be re-checked soon.

    Timestamps are built relative to now, so this cannot rot with the calendar.
    """
    import tempfile as _tf
    from datetime import datetime, timedelta, timezone

    from polymarket.poller import Poller

    def ago(**kw):
        return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat().replace("+00:00", "Z")

    def skip(start_time):
        return type("S", (), {"reason": "upcoming", "start_time": start_time})()

    api = FakeAPI([])
    api.books = lambda token_ids: {}
    with _tf.TemporaryDirectory() as tmp:
        with Store(Path(tmp) / "late.db") as store:
            poller = Poller(api, store)

            # scheduled an hour ago and still not under way -> check again soon
            poller._schedule_next_start([skip(ago(hours=1))])
            soon = (
                poller._next_start is not None
                and poller._next_start - time.monotonic() <= 61
            )

            # abandoned long ago -> stop expecting it, fall back to the normal interval
            poller._schedule_next_start([skip(ago(days=10))])
            long_past_ignored = poller._next_start is None

            # a genuine future start wins over an overdue one
            future = (
                datetime.now(timezone.utc) + timedelta(minutes=30)
            ).isoformat().replace("+00:00", "Z")
            poller._schedule_next_start([skip(ago(hours=1)), skip(future)])
            prefers_future = (
                poller._next_start is not None
                and poller._next_start - time.monotonic() > 1000
            )
    return soon and long_past_ignored and prefers_future


def test_poller() -> None:
    print("\npoller")
    from polymarket.poller import Poller

    api = FakeAPI([_cincinnati_atp()])
    feed = FakeFlashscore(_live_board())
    book = dict(BOOK)
    api.books = lambda token_ids: {tid: dict(book, asset_id=tid) for tid in token_ids}

    with tempfile.TemporaryDirectory() as tmp:
        with Store(Path(tmp) / "p.db") as store:
            poller = Poller(api, store, scores=feed, interval=10.0)
            check("deduplication is on by default", poller.only_changes is True)
            check("live-only is on by default", poller.live_only is True)

            poller.refresh()
            check("refresh tracks both sides of the match", len(poller.tracked) == 2)
            check("first tick writes both sides", poller.tick() == 2)
            check("unchanged book writes nothing", poller.tick() == 0)
            check("still nothing on a third identical tick", poller.tick() == 0)

            # a real move must come through immediately
            book["bids"] = [{"price": "0.56", "size": "10"}] + BOOK["bids"]
            check("a changed book is written", poller.tick() == 2)
            check("and then goes quiet again", poller.tick() == 0)

            # a trade with no book change still counts as new information
            book["last_trade_price"] = "0.57"
            check("a new last-trade price is written", poller.tick() == 2)

            rows = store.conn.execute("SELECT COUNT(DISTINCT ts) FROM books").fetchone()[0]
            check("three ticks produced rows", rows == 3)

    # heartbeat: an unchanged book is still written periodically
    with tempfile.TemporaryDirectory() as tmp:
        with Store(Path(tmp) / "h.db") as store:
            steady = dict(BOOK)
            api.books = lambda token_ids: {tid: dict(steady, asset_id=tid) for tid in token_ids}
            poller = Poller(api, store, scores=feed, heartbeat=0.0)
            poller.refresh()
            check("heartbeat 0 writes every tick", poller.tick() == 2 and poller.tick() == 2)

            poller.heartbeat = 3600.0
            check("long heartbeat suppresses again", poller.tick() == 0)
            # pretend the last write was long ago
            poller._last_write = {t: 0.0 for t in poller.tracked}
            check("stale token is rewritten even if unchanged", poller.tick() == 2)

    # --every-tick disables deduplication entirely
    with tempfile.TemporaryDirectory() as tmp:
        with Store(Path(tmp) / "e.db") as store:
            poller = Poller(api, store, scores=feed, only_changes=False)
            poller.refresh()
            check("every-tick writes regardless", poller.tick() == 2 and poller.tick() == 2)

    # live filtering drives what the loop tracks
    with tempfile.TemporaryDirectory() as tmp:
        with Store(Path(tmp) / "l.db") as store:
            upcoming = _event(
                "Cincinnati Open: Not Started vs Yet",
                "atp-nnn-yyy-2026-08-16",
                [("Cincinnati Open: Not Started vs Yet", ["Not Started", "Yet Waiting"], True)],
                "2099-01-01T00:00:00Z",
            )
            quiet = FakeAPI([upcoming])
            quiet.books = lambda token_ids: {}
            waiting = FakeFlashscore(_board(("Started N.", "Waiting Y.", 1, [])))
            poller = Poller(quiet, store, scores=waiting)
            poller.refresh()
            check("upcoming match is not tracked", poller.tracked == {})
            check("tick with nothing tracked is a no-op", poller.tick() == 0)
            check("next refresh is scheduled for the start time", poller._next_start is not None)

            quiet._events = [_cincinnati_atp()]
            poller.scores = feed
            poller.refresh()
            check("match is picked up once it goes live", len(poller.tracked) == 2)

            quiet._events = []
            poller.refresh()
            check("finished match stops being tracked", poller.tracked == {})

    print("\nstart-time scheduling")
    from polymarket.discovery import parse_iso, seconds_from_now

    check("past times parse as past", seconds_from_now("2020-01-01T00:00:00Z") < 0)
    check("overdue match reschedules a quick re-check", _overdue_reschedules())
    check("garbage is ignored", seconds_from_now("not a time") is None)
    check("missing is ignored", seconds_from_now(None) is None)
    check("future times parse", seconds_from_now("2099-01-01T00:00:00Z") > 0)
    check("naive timestamps are read as UTC", parse_iso("2026-08-16T18:30:00") == parse_iso("2026-08-16T18:30:00Z"))


# --------------------------------------------------------------------------
# the Flashscore feed: parsing and pairing
# --------------------------------------------------------------------------

# Captured from the live feeds. The day card is one block per match with a
# tournament header before it; the per-match feed is one block per set.
DAY_FEED = (
    "ZA÷ATP - SINGLES: Cincinnati (USA), hard¬~"
    "AA÷Qi0f7iu1¬AD÷1786969800¬AB÷3¬AC÷3¬"
    "AE÷Lehecka J.¬WU÷lehecka-jiri¬AF÷Berrettini M.¬WV÷berrettini-matteo¬"
    "BA÷6¬BB÷4¬BC÷6¬DC÷3¬BD÷7¬DD÷7¬BE÷6¬BF÷3¬~"
    "AA÷Iy42aBeE¬AD÷1786989600¬AB÷3¬AC÷36¬"
    "AE÷Fery A.¬WU÷fery-arthur¬AF÷De Minaur A.¬WV÷de-minaur-alex¬"
    "BA÷5¬BB÷7¬BC÷0¬BD÷0¬~"
    "ZA÷CHALLENGER MEN - SINGLES: Sion (Sui), hard¬~"
    "AA÷chal1234¬AD÷1786989600¬AB÷2¬AC÷17¬"
    "AE÷Lehecka J.¬WU÷lehecka-jiri¬AF÷Berrettini M.¬WV÷berrettini-matteo¬BA÷1¬BB÷0¬~"
    "ZA÷WTA - SINGLES: Cincinnati (USA), hard¬~"
    "AA÷wta12345¬AD÷1786989600¬AB÷2¬AC÷17¬"
    "AE÷Bouzkova M.¬WU÷bouzkova-marie¬AF÷Stefanini L.¬WV÷stefanini-lucrezia¬BA÷2¬BB÷1¬~"
)

MATCH_FEED = "AC÷3¬BA÷6¬BB÷4¬RC÷0:38¬~BC÷6¬DC÷3¬BD÷7¬DD÷7¬RD÷0:58¬~BE÷6¬BF÷3¬RE÷0:48¬~RB÷2:24¬~"

# What a match that has not been played answers with: TV listings, no status.
EMPTY_FEED = "PSPH÷12¬PSPA÷22¬TA÷Arena Premium 5 (Srb)¬A1÷¬~"

# The live feed, which is the only one carrying the points in the current game.
# DP/DQ are those points, DR is who is serving, DN/DO the games in the set.
LIVE_FEED = "DA÷2¬DB÷17¬DE÷0¬DF÷0¬DN÷1¬DO÷2¬DP÷15¬DQ÷40¬DR÷1¬~"
# The same keys on a match that is over hold something else entirely.
DONE_FEED = "DA÷3¬DB÷3¬DE÷2¬DF÷0¬DP÷12¬DQ÷7¬DR÷0¬~"


def test_poll_cadence() -> None:
    """Who is read on this tick: the matches in play every time, the ones that
    have not started every idle interval, the finished ones never again."""
    print("\npoll cadence")
    from polymarket.poller import Poller, Tracked

    with tempfile.TemporaryDirectory() as tmp:
        with Store(Path(tmp) / "cad.db") as store:
            asked: list[list[str]] = []
            api = FakeAPI([])
            api.books = lambda token_ids: asked.append(list(token_ids)) or {}
            poller = Poller(api, store, scores=FakeFlashscore())
            check("the idle cadence defaults to a minute", poller.idle_interval == 60.0)

            def track(cid: str, state: str) -> None:
                for index in (0, 1):
                    poller.tracked[f"{cid}-{index}"] = Tracked(cid, index, f"P{index}", f"Match {cid}")
                poller._state[cid] = state

            track("live", "live")
            track("soon", "upcoming")
            track("done", "ended")

            first = poller._due_matches()
            check("a match in play is read", "live" in first)
            check("one that has not started is read on the first tick", "soon" in first)
            check("a finished match is not read at all", "done" not in first)

            check("the next tick reads only the match in play", poller._due_matches() == {"live"})
            check("and the one after that", poller._due_matches() == {"live"})

            # A minute later the upcoming match comes back round.
            poller._last_poll["soon"] -= 60.0
            check("an idle match is read again after the idle interval", "soon" in poller._due_matches())

            # A match that ends between refreshes stops being read at once,
            # rather than waiting for the market list to drop it.
            poller._state["live"] = "ended"
            check("a match that has just ended drops out", poller._due_matches() == set())

            # ... and the books request is asked for exactly those tokens.
            poller._state["live"] = "live"
            poller._last_poll.clear()
            poller.tick()
            check(
                "the first tick asks for the live and the upcoming books",
                set(asked[-1]) == {"live-0", "live-1", "soon-0", "soon-1"},
            )
            poller.tick()
            check("the next asks only for the match in play", set(asked[-1]) == {"live-0", "live-1"})

            # The score of a match is read in the same pass as its book, so the
            # two cannot end up on different clocks.
            from polymarket.poller import Watched
            from polymarket.scores import Paired, Reading

            for cid in ("live", "soon"):
                poller.watched[cid] = Watched(
                    cid, Paired(f"fs-{cid}", False, Reading("live", None), "x"), _iso(300), f"Match {cid}"
                )
            due = poller._due_matches()
            ids = {w.pairing.id for w in poller._score_targets(due)}
            check("the live match's score is read with its book", ids == {"fs-live"})
            check(
                "the idle match's score waits for its own turn",
                {w.pairing.id for w in poller._score_targets({"soon"})} == {"fs-soon"},
            )


def test_score_feed() -> None:
    print("\nscore feed parsing")
    from polymarket.scores import Reading, SetScore, parse_board, parse_reading

    board = parse_board(DAY_FEED)
    check("only tour-level singles is read", len(board) == 3)
    check("a challenger with the same players is skipped", all(m.id != "chal1234" for m in board))

    finished, interrupted, women = board
    check("id read", finished.id == "Qi0f7iu1")
    check("players read", (finished.home, finished.away) == ("Lehecka J.", "Berrettini M."))
    check("tournament resolved to the ATP calendar", finished.tournament.name == "Cincinnati Open")
    check("and the heading says which tour it is", finished.tour == "atp")

    # The other draw at the same event, under its own heading. Same tournament
    # name, other calendar, other tier.
    check("the wta draw is read as well", women.id == "wta12345")
    check("tagged as wta", women.tour == "wta")
    check("on the wta calendar", women.tournament.tier == "wta_1000")
    check("start time read", finished.starts_at == 1786969800.0)
    check("finished match is ended", finished.reading.state == "ended")
    check("with full time as its period", finished.reading.period == "FT")
    check("sets in order", finished.reading.line() == "6-4, 6-7(3), 6-3")

    # The stage says 3, which on its own reads as finished. A match stopped for
    # rain has not finished, and its book is still trading.
    check("an interruption is still live", interrupted.reading.state == "live")
    check("and says so", interrupted.reading.period == "INT")
    check("score so far", interrupted.reading.line() == "5-7, 0-0")

    reading = parse_reading(MATCH_FEED)
    check("per-match feed reads one set per block", reading.line() == "6-4, 6-7(3), 6-3")
    check("and its status", (reading.state, reading.period) == ("ended", "FT"))
    check("a match with nothing to say reads as nothing", parse_reading(EMPTY_FEED) is None)
    check("an empty response too", parse_reading("") is None)

    # The score is stored in the order Polymarket lists the players, which is
    # not always the order Flashscore does.
    check("flipped score is mirrored", reading.line(flip=True) == "4-6, 7-6(3), 3-6")
    check("a set with no tiebreak is untouched", SetScore(6, 4).render() == "6-4")
    check("the loser's tiebreak points are shown", SetScore(7, 6, 7, 4).render() == "7-6(4)")
    check("a tiebreak in progress is not annotated", SetScore(6, 6, 5, 4).render() == "6-6")
    check("no sets means no score line", Reading("upcoming", None).line() is None)

    # Every status has to land somewhere: an unknown one falls back to the
    # coarse stage rather than being dropped.
    from polymarket.scores import _read_status

    check("unknown detail falls back to the stage", _read_status({"AC": "999", "AB": "2"}) == ("live", None))
    check("no status at all is no reading", _read_status({}) is None)
    check("a tiebreak is still that set", _read_status({"AC": "48"}) == ("live", "S2"))
    check("retired is over", _read_status({"AC": "8"}) == ("ended", "RET"))
    check("postponed is still to come", _read_status({"AC": "4"}) == ("upcoming", "POST"))

    # Points come off the other per-match feed, and only mean points while a
    # set is being played -- a finished match reports its total games in the
    # same keys, which is why the period has to gate them.
    from polymarket.scores import parse_blocks, read_points

    live = parse_blocks(LIVE_FEED)[0]
    check("points read from the live feed", read_points(live, "S1") == ("15", "40"))
    check("a tiebreak counts in whole points", read_points({"DP": "5", "DQ": "7"}, "S3") == ("5", "7"))
    check("no points once the match is over", read_points(parse_blocks(DONE_FEED)[0], "FT") is None)
    check("nor while it is interrupted", read_points(live, "INT") is None)
    check("nor before it starts", read_points(live, None) is None)
    check("nonsense is not a score", read_points({"DP": "x", "DQ": "y"}, "S1") is None)

    scored = Reading("live", "S1", (SetScore(1, 2),), ("15", "40"), serving=1)
    check("the game reads in feed order", scored.game() == "15-40")
    check("and flips with the score", scored.game(flip=True) == "40-15")
    check("no points, no game", Reading("live", "S1", (SetScore(1, 2),)).game() is None)

    from polymarket.scores import read_serving

    check("the server is read", read_serving(parse_blocks(LIVE_FEED)[0]) == 1)
    check("nobody serving reads as nobody", read_serving({"DR": "0"}) is None)
    check("and a missing key too", read_serving({}) is None)
    check("home serving is the first outcome", scored.server() == 0)
    check("unless the sides are the other way round", scored.server(flip=True) == 1)
    check("no server, no index", Reading("live", "S1").server() is None)

    # A score line read back out of the database, which is how the cleanup
    # replays what was already recorded.
    from polymarket.scores import parse_line

    check("a plain line round-trips", Reading("live", "S1", parse_line("6-4, 3-2")).line() == "6-4, 3-2")
    check("a tiebreak round-trips", Reading("live", "S1", parse_line("6-7(3)")).line() == "6-7(3)")
    check("nothing parses to nothing", parse_line(None) == () and parse_line("") == ())
    check("junk is skipped", parse_line("not a score") == ())

    # A day with no tour matches on it and a feed that cannot be reached must
    # not look the same: one is an answer, the other is the absence of one.
    import httpx

    from polymarket.scores import Flashscore

    quiet = Flashscore(days=(0,))
    quiet._get = lambda feed: "ZA÷WTA - SINGLES: Monterrey (Mex), hard¬~"
    check("a card with no tour matches is an empty board", len(quiet.board()) == 0)

    down = Flashscore(days=(0, 1))

    def unreachable(_feed):
        raise httpx.ConnectError("no route")

    down._get = unreachable
    try:
        down.board()
        raised = False
    except httpx.HTTPError:
        raised = True
    check("losing every day card raises instead", raised)

    half = Flashscore(days=(0, 1))
    seen = []

    def flaky(feed):
        seen.append(feed)
        if len(seen) == 1:
            raise httpx.ConnectError("no route")
        return DAY_FEED

    half._get = flaky
    check("one bad day still yields the others", len(half.board()) == 3)
    quiet.close(), down.close(), half.close()


def test_score_pairing() -> None:
    """Matching a Polymarket market to a Flashscore one, by tournament and both players."""
    print("\nscore pairing")
    from polymarket.scores import parse_board, ScoreBoard

    board = ScoreBoard(parse_board(DAY_FEED))

    # Polymarket spells names out in full; Flashscore abbreviates the first name
    # in the display name and reverses it in the slug.
    hit = board.pair("atp", "Cincinnati Open", ["Arthur Fery", "Alex de Minaur"])
    check("full names match abbreviated ones", hit is not None and hit.id == "Iy42aBeE")
    check("same order needs no flip", hit.flip is False)
    check("score comes out in that order", hit.score == "5-7, 0-0")

    flipped = board.pair("atp", "Cincinnati Open", ["Alex de Minaur", "Arthur Fery"])
    check("the reverse order pairs too", flipped is not None and flipped.id == "Iy42aBeE")
    check("and is flagged as flipped", flipped.flip is True)
    check("so the score is mirrored", flipped.score == "7-5, 0-0")

    check(
        "one player agreeing is not enough",
        board.pair("atp", "Cincinnati Open", ["Arthur Fery", "Somebody Else"]) is None,
    )
    check(
        "the right players at the wrong tournament do not pair",
        board.pair("atp", "Wimbledon", ["Arthur Fery", "Alex de Minaur"]) is None,
    )
    check(
        "a match the board has never seen does not pair",
        board.pair("atp", "Cincinnati Open", ["Nobody Here", "Nor Here"]) is None,
    )
    check("an empty board pairs nothing", ScoreBoard().pair("atp", "Cincinnati Open", ["A B", "C D"]) is None)

    # Names that are only particles must not be read as agreement: every Dutch
    # player shares "van", and "de Minaur" and "de Jong" are different people.
    from polymarket.scores import name_tokens

    check("initials are dropped", "a" not in name_tokens("Fery A."))
    check("accents are folded", name_tokens("Cerúndolo") == name_tokens("Cerundolo"))
    check("hyphens split", name_tokens("Auger-Aliassime") == {"auger", "aliassime"})
    check("apostrophes are dropped", name_tokens("O'Connell") == {"connell"})

    from polymarket.scores import BoardMatch, Reading
    from polymarket.config import match_tournament

    def entry(mid, home, away):
        return BoardMatch(
            id=mid,
            tour="atp",
            tournament=match_tournament("Cincinnati Open", "atp"),
            home=home,
            away=away,
            home_tokens=name_tokens(home),
            away_tokens=name_tokens(away),
            starts_at=None,
            reading=Reading("live", "S1", ()),
        )

    particles = ScoreBoard([entry("a", "De Jong J.", "Van Rijthoven T.")])
    check(
        "sharing only a particle is not a pairing",
        particles.pair("atp", "Cincinnati Open", ["Alex de Minaur", "Botic van de Zandschulp"]) is None,
    )

    # Two matches that fit equally well is not a coin toss to be won; refusing
    # leaves the market on the fallback, which is at least honest.
    twins = ScoreBoard([entry("x", "Smith J.", "Jones A."), entry("y", "Smith J.", "Jones A.")])
    check("a genuine tie is refused", twins.pair("atp", "Cincinnati Open", ["John Smith", "Alan Jones"]) is None)

    # The same fixture on two adjacent day cards is one match, not two.
    same = ScoreBoard([entry("x", "Smith J.", "Jones A."), entry("x", "Smith J.", "Jones A.")])
    check("the day cards overlap harmlessly", same.pair("atp", "Cincinnati Open", ["John Smith", "Alan Jones"]).id == "x")

    # Two brothers across the net: the surname fits either side, so which way
    # round the score goes cannot be read off the names.
    from polymarket.scores import Reading as R, SetScore as S

    brothers = ScoreBoard(
        [
            BoardMatch(
                id="sib",
                tour="atp",
                tournament=match_tournament("Cincinnati Open", "atp"),
                home="Cerundolo F.",
                away="Cerundolo J. M.",
                home_tokens=name_tokens("Cerundolo F."),
                away_tokens=name_tokens("Cerundolo J. M."),
                starts_at=None,
                reading=R("live", "S1", (S(4, 2),)),
            )
        ]
    )
    sib = brothers.pair("atp", "Cincinnati Open", ["Francisco Cerundolo", "Juan Manuel Cerundolo"])
    check("the match is still identified", sib is not None and sib.id == "sib")
    check("but flagged as un-orientable", sib.oriented is False)
    check("so no score is claimed", sib.score is None)
    check("while the state still comes through", sib.reading.state == "live")
    check("and a later reading is dropped too", sib.render(R("live", "S1", (S(5, 2),))) is None)

    # First names on the feed side are what resolve it, and Flashscore's slugs
    # carry them even where the display name is an initial.
    told_apart = ScoreBoard(
        [
            BoardMatch(
                id="sib",
                tour="atp",
                tournament=match_tournament("Cincinnati Open", "atp"),
                home="Cerundolo F.",
                away="Cerundolo J. M.",
                home_tokens=name_tokens("Cerundolo F. cerundolo-francisco"),
                away_tokens=name_tokens("Cerundolo J. M. cerundolo-juan-manuel"),
                starts_at=None,
                reading=R("live", "S1", (S(4, 2),)),
            )
        ]
    )
    solved = told_apart.pair("atp", "Cincinnati Open", ["Juan Manuel Cerundolo", "Francisco Cerundolo"])
    check("first names settle it", solved.oriented is True)
    check("and the score comes out the market's way round", solved.score == "2-4")


def test_score_ratchet() -> None:
    """Stale reads from Flashscore's edge caches must not rewind a score."""
    print("\nscore ratchet")
    from polymarket.scores import Ratchet, Reading, SetScore, progress

    def r(state, period, *sets):
        return Reading(state, period, tuple(SetScore(*x) for x in sets))

    check("more games is further on", progress(r("live", "S1", (3, 5))) > progress(r("live", "S1", (3, 4))))
    check("a new set is further on", progress(r("live", "S2", (3, 6), (0, 0))) > progress(r("live", "S1", (3, 5))))
    check("finishing is further on", progress(r("ended", "FT", (3, 6))) > progress(r("live", "S1", (3, 6))))
    check("tiebreak points count", progress(r("live", "S1", (6, 6, 5, 4))) > progress(r("live", "S1", (6, 6, 4, 4))))

    # The exact sequence the live capture recorded: every real game followed by
    # one stale read that put it back, then the real value again.
    ratchet = Ratchet()
    seen = []
    for reading in [
        r("live", "S1", (1, 2)),
        r("live", "S1", (1, 1)),   # stale
        r("live", "S1", (1, 2)),
        r("live", "S1", (2, 2)),
        r("live", "S1", (1, 2)),   # stale
        r("live", "S1", (2, 2)),
        r("live", "S2", (3, 6), (0, 0)),
        r("live", "S1", (3, 5)),   # stale
        r("live", "S2", (3, 6), (0, 0)),
    ]:
        if ratchet.accept("m", reading):
            seen.append(reading.line())
    check("only forward readings are taken", seen == ["1-2", "1-2", "2-2", "2-2", "3-6, 0-0", "3-6, 0-0"])
    check("no reading ever goes backwards", seen == sorted(seen, key=lambda x: (len(x), x)))
    # Repeats are harmless -- record_score_events drops them -- but a rewind is
    # not, and none of the three stale reads got through.
    check("the score ends where the feed did", ratchet.latest("m").line() == "3-6, 0-0")

    # A rain delay flaps the period with the score standing still. Same games,
    # same state, so it is not progress and must not be written down.
    flapping = Ratchet()
    flapping.accept("m", r("live", "S2", (5, 7), (0, 0)))
    check(
        "a period-only flap is ignored",
        flapping.accept("m", r("live", "INT", (5, 7), (0, 0))) is False,
    )

    # A retirement ends a match without another game being played, so the state
    # moving on is progress even when nothing else has.
    quit = Ratchet()
    quit.accept("m", r("live", "S2", (7, 5), (1, 2)))
    check("a retirement gets through", quit.accept("m", r("ended", "RET", (7, 5), (1, 2))) is True)

    # A scorer correcting a mistake also reads as going backwards. It is told
    # from a cache by sticking: an old copy alternates with the fresh one, a
    # correction comes back every read.
    fixed = Ratchet(patience=3)
    fixed.accept("m", r("live", "S1", (4, 2)))
    wrong = r("live", "S1", (3, 2))
    check("first rewind is refused", fixed.accept("m", wrong) is False)
    check("so is the second", fixed.accept("m", wrong) is False)
    check("the third is taken as a correction", fixed.accept("m", wrong) is True)
    check("and becomes the new floor", fixed.latest("m").line() == "3-2")

    # ...whereas an alternating cache never gets there, however long it runs.
    patient = Ratchet(patience=3)
    good, stale = r("live", "S1", (4, 2)), r("live", "S1", (3, 2))
    patient.accept("m", good)
    took = [patient.accept("m", x) for _ in range(6) for x in (stale, good)]
    check("flapping never wears the ratchet down", took.count(True) == 6)
    check("and it stays on the newer score", patient.latest("m").line() == "4-2")

    # Points are the one part that legitimately goes backwards -- deuce comes
    # round again and again -- so they are exempt from the ratchet entirely.
    game = Ratchet()
    game.accept("m", Reading("live", "S1", (SetScore(3, 2),), ("0", "0")))
    for a, b in [("15", "0"), ("15", "15"), ("40", "40"), ("A", "40"), ("40", "40")]:
        got = game.accept("m", Reading("live", "S1", (SetScore(3, 2),), (a, b)))
        check(f"points {a}-{b} get through", got is True)
    check("and the score underneath is untouched", game.latest("m").line() == "3-2")
    check(
        "the serve changing hands is not a rewind",
        game.accept("m", Reading("live", "S1", (SetScore(3, 2),), ("0", "0"), serving=2)) is True,
    )
    check(
        "a rewind is still caught even with points on it",
        game.accept("m", Reading("live", "S1", (SetScore(2, 2),), ("0", "0"))) is False,
    )

    # Matches that are over stop being tracked, so their history is dropped.
    ratchet.forget(["other"])
    check("finished matches are forgotten", ratchet.latest("m") is None)


# --------------------------------------------------------------------------
# score polling on the tick cadence
# --------------------------------------------------------------------------


def _iso(offset_seconds: float) -> str:
    from datetime import datetime, timedelta, timezone

    when = datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_score_poll_targeting() -> None:
    """Which matches the poll asks about, before the tick cadence narrows it
    further -- one small request each."""
    print("\nscore poll targeting")
    from polymarket.poller import Poller, Watched
    from polymarket.scores import Paired, Reading

    with tempfile.TemporaryDirectory() as tmp:
        with Store(Path(tmp) / "t.db") as store:
            poller = Poller(FakeAPI([]), store, scores=FakeFlashscore())

            def watch(cid: str, state: str, start: str | None) -> None:
                poller.watched[cid] = Watched(cid, Paired(f"fs-{cid}", False, Reading("live", None), "x"), start, f"Match {cid}")
                poller._state[cid] = state

            watch("live", "live", _iso(-3600))
            watch("ended", "ended", _iso(-7200))
            watch("soon", "upcoming", _iso(300))          # starts in 5 min
            watch("later", "upcoming", _iso(6 * 3600))    # starts in 6 hours
            watch("overdue", "upcoming", _iso(-1800))     # 30 min past its slot
            watch("abandoned", "upcoming", _iso(-9 * 3600))  # long past
            watch("undated", "upcoming", None)

            ids = {w.pairing.id for w in poller._score_targets()}
            check("a match in play is polled", "fs-live" in ids)
            check("a finished match is not", "fs-ended" not in ids)
            check("one about to start is polled", "fs-soon" in ids)
            check("one hours away is not", "fs-later" not in ids)
            check("an overdue match is still polled", "fs-overdue" in ids)
            check("one long past its slot is dropped", "fs-abandoned" not in ids)
            check("an undated match is polled", "fs-undated" in ids)


def test_score_poll() -> None:
    print("\nscore poll")
    from polymarket.poller import Poller
    from polymarket.scores import Reading, SetScore

    api = FakeAPI([_cincinnati_atp()])
    api.books = lambda token_ids: {}
    feed = FakeFlashscore(_live_board())

    def says(status: int, *sets: tuple) -> None:
        from polymarket.scores import _STATUS

        state, period = _STATUS[str(status)]
        feed.readings_by_id["fs0"] = Reading(state, period, tuple(SetScore(*s) for s in sets))

    with tempfile.TemporaryDirectory() as tmp:
        with Store(Path(tmp) / "s.db") as store:
            poller = Poller(api, store, scores=feed)
            poller.refresh()
            check("a live match is watched", len(poller.watched) == 1)
            check("refresh seeds the known state", set(poller._state.values()) == {"live"})

            # refresh already recorded the opening reading, so a poll that sees
            # the same score must not write it again.
            says(18, (6, 3), (3, 1))
            check("an unchanged score writes nothing", poller.poll_scores() == 0)
            check("the poll asked only about the live match", feed.calls[-1] == ["fs0"])

            says(18, (6, 3), (4, 1))
            check("a game going by is recorded", poller.poll_scores() == 1)
            check("and only once", poller.poll_scores() == 0)

            says(19, (6, 3), (6, 4), (1, 0))
            check("a set change is recorded", poller.poll_scores() == 1)

            row = store.conn.execute(
                "SELECT period, score FROM markets WHERE condition_id = ?",
                (next(iter(poller.watched)),),
            ).fetchone()
            check("markets carries the latest score", tuple(row) == ("S3", "6-3, 6-4, 1-0"))

            history = store.conn.execute(
                "SELECT period, score FROM score_events ORDER BY ts"
            ).fetchall()
            check("history is the sequence of changes", len(history) == 3)
            check("first entry is where refresh found it", history[0][1] == "6-3, 3-1")
            check("last entry is the newest", history[-1][0] == "S3")

            # A stale copy from one of Flashscore's other edge caches, which is
            # what turned every game into three score changes.
            before = store.conn.execute("SELECT COUNT(*) FROM score_events").fetchone()[0]
            says(18, (6, 3), (3, 1))          # one game behind what we have
            check("a rewound score writes nothing", poller.poll_scores() == 0)
            says(19, (6, 3), (6, 4), (1, 0))  # the real value again
            check("and the real one is not re-written either", poller.poll_scores() == 0)
            after = store.conn.execute("SELECT COUNT(*) FROM score_events").fetchone()[0]
            check("so the history did not grow", after == before)
            kept_row = store.conn.execute(
                "SELECT period, score FROM markets WHERE condition_id = ?",
                (next(iter(poller.watched)),),
            ).fetchone()
            check("and markets still holds the newer score", tuple(kept_row) == ("S3", "6-3, 6-4, 1-0"))

            # A refresh re-reads the day card, which is staler still; it must not
            # rewind the score either.
            feed._board = _live_board(sets=((6, 3), (3, 1)))
            poller.refresh()
            row_after_refresh = store.conn.execute(
                "SELECT period, score FROM markets WHERE condition_id = ?",
                (next(iter(poller.watched)),),
            ).fetchone()
            check(
                "a stale day card does not rewind it",
                tuple(row_after_refresh) == ("S3", "6-3, 6-4, 1-0"),
            )

            # A feed that answers with nothing is not a score of nothing: a
            # walkover reports no status at all, and the last one must stand.
            del feed.readings_by_id["fs0"]
            check("a silent feed writes nothing", poller.poll_scores() == 0)
            still = store.conn.execute(
                "SELECT period FROM markets WHERE condition_id = ?",
                (next(iter(poller.watched)),),
            ).fetchone()
            check("and leaves the last reading in place", still[0] == "S3")

            # The match finishing is what should cut the capture short.
            check("no refresh pending yet", poller._state_changed is False)
            says(3, (6, 3), (6, 4), (6, 2))
            poller.poll_scores()
            check("the end of a match is noticed", poller._state_changed is True)
            check("but not acted on instantly", poller._refresh_due(time.monotonic() - 5) is False)
            check("acted on once the gap has passed", poller._refresh_due(time.monotonic() - 90) is True)
            check("a normal refresh still wins", poller._refresh_due(time.monotonic() - 9999) is True)

            poller.refresh()
            check("refreshing clears the pending flag", poller._state_changed is False)


def test_score_cadence() -> None:
    """Books and scores share one cadence, and the loop reads them together."""
    print("\nscore cadence")
    import inspect

    from polymarket.poller import Poller
    from polymarket.scores import Reading, SetScore

    check(
        "there is no separate score interval to get out of step",
        "score_interval" not in inspect.signature(Poller.__init__).parameters,
    )

    api = FakeAPI([_cincinnati_atp()])
    api.books = lambda token_ids: {tid: dict(BOOK, asset_id=tid) for tid in token_ids}
    feed = FakeFlashscore(_live_board())
    feed.readings_by_id["fs0"] = Reading("live", "S1", (SetScore(1, 0),))

    with tempfile.TemporaryDirectory() as tmp:
        with Store(Path(tmp) / "c.db") as store:
            poller = Poller(api, store, scores=feed, interval=5.0)
            poller.refresh()
            check("the default cadence is five seconds", Poller(api, store, scores=feed).interval == 5.0)

            # One turn of the loop's body, three times over: the score feed is
            # asked every time, not every other time.
            before = len(feed.calls)
            for _ in range(3):
                poller.tick()
                poller.poll_scores()
            check("every tick reads the score", len(feed.calls) - before == 3)


def test_score_poll_isolation() -> None:
    """The score poll must never be able to take the capture down with it."""
    print("\nscore poll isolation")
    from polymarket.poller import Poller

    api = FakeAPI([_cincinnati_atp()])
    book = dict(BOOK)
    api.books = lambda token_ids: {tid: dict(book, asset_id=tid) for tid in token_ids}
    feed = FakeFlashscore(_live_board())

    with tempfile.TemporaryDirectory() as tmp:
        with Store(Path(tmp) / "i.db") as store:
            poller = Poller(api, store, scores=feed)
            poller.refresh()

            def explode(_ids):
                raise RuntimeError("score feed is down")

            feed.readings = explode
            try:
                poller.poll_scores()
                raised = False
            except RuntimeError:
                raised = True
            check("poll_scores itself propagates", raised)
            # ...and the run loop is what swallows it, so books keep being written.
            check("books are unaffected", poller.tick() == 2)

            # A board that will not load must not stop the capture either: the
            # ids it already handed out are what the score poll actually uses.
            def no_board():
                import httpx

                raise httpx.ConnectError("flashscore is down")

            before = len(poller.board)
            feed.board = no_board
            poller.refresh()
            check("a dead board keeps the previous one", len(poller.board) == before)
            check("and the match stays tracked", len(poller.tracked) == 2)

            # A feed that cannot be reached at all is survivable on its own:
            # readings() drops what it cannot fetch, and the books carry on.
            feed.readings = lambda ids: {}
            check("an unreachable feed writes nothing", poller.poll_scores() == 0)
            book["last_trade_price"] = "0.61"
            check("and the books still go in", poller.tick() == 2)


def test_update_scores() -> None:
    print("\nscore updates")
    from polymarket.store import ScoreRow

    kept, _ = discover(FakeAPI([_cincinnati_atp()]), _live_board())
    market = kept[0]
    reading = [ScoreRow.of(market)]

    with tempfile.TemporaryDirectory() as tmp:
        with Store(Path(tmp) / "u.db") as store:
            # A match the poll sees before discovery inserted it is a no-op, not
            # a half-built row.
            store.update_scores(reading)
            check("no row is invented", store.conn.execute("SELECT COUNT(*) FROM markets").fetchone()[0] == 0)

            store.upsert_markets(kept)
            before = store.conn.execute(
                "SELECT raw, question FROM markets WHERE condition_id = ?", (market.condition_id,)
            ).fetchone()

            store.update_scores([ScoreRow(market.condition_id, "live", "S5", "7-6")])
            after = store.conn.execute(
                "SELECT period, score, state, raw, question FROM markets WHERE condition_id = ?",
                (market.condition_id,),
            ).fetchone()
            check("score fields updated", tuple(after[:3]) == ("S5", "7-6", "live"))
            check("the raw payload is left alone", after[3] == before[0])
            check("identity is left alone", after[4] == before[1])


def test_prune_score_events() -> None:
    """Repairing a history recorded before the ratchet existed."""
    print("\nscore cleanup")
    from polymarket.store import ScoreRow

    kept, _ = discover(FakeAPI([_cincinnati_atp()]), _live_board())
    cid = kept[0].condition_id

    # The exact shape the capture left behind: each real game written down, then
    # rewound by a stale read, then written again.
    recorded = [
        ("live", "S1", "0-0"),
        ("live", "S1", "1-0"),
        ("live", "S1", "0-0"),   # stale
        ("live", "S1", "1-0"),   # only a change because of the rewind
        ("live", "S1", "1-1"),
        ("live", "S1", "1-0"),   # stale
        ("live", "S1", "1-1"),
        ("live", "S2", "6-1, 0-0"),
        ("live", "S1", "5-1"),   # stale, a whole set behind
        ("live", "S2", "6-1, 0-0"),
        ("ended", "FT", "6-1, 6-2"),
    ]

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "dirty.db"
        with Store(path) as store:
            store.upsert_markets(kept)
            for index, (state, period, score) in enumerate(recorded):
                store.conn.execute(
                    "INSERT INTO score_events (ts, condition_id, state, period, score) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (1_800_000_000.0 + index * 10, cid, state, period, score),
                )
            store.conn.execute(
                "UPDATE markets SET state = ?, period = ?, score = ? WHERE condition_id = ?",
                ("live", "S1", "5-1", cid),  # what the last stale write left
            )

            dry = store.prune_score_events()
            check("a dry run reports what it would do", dry["removing"] == 6)
            check("and changes nothing", dry["applied"] is False)
            check(
                "the rows are all still there",
                store.conn.execute("SELECT COUNT(*) FROM score_events").fetchone()[0] == 11,
            )

            done = store.prune_score_events(apply=True)
            check("applying removes them", done["removing"] == 6)
            survivors = [
                r[0]
                for r in store.conn.execute(
                    "SELECT score FROM score_events WHERE condition_id = ? ORDER BY ts", (cid,)
                )
            ]
            check(
                "what is left only ever moves forward",
                survivors == ["0-0", "1-0", "1-1", "6-1, 0-0", "6-1, 6-2"],
            )
            check(
                "markets is brought back in step",
                tuple(
                    store.conn.execute(
                        "SELECT state, period, score FROM markets WHERE condition_id = ?", (cid,)
                    ).fetchone()
                )
                == ("ended", "FT", "6-1, 6-2"),
            )
            check("running it again finds nothing left", store.prune_score_events()["removing"] == 0)

    # Points move within a game and must survive a cleanup, since they are not
    # a rewind of anything.
    with tempfile.TemporaryDirectory() as tmp:
        with Store(Path(tmp) / "points.db") as store:
            store.upsert_markets(kept)
            for index, game in enumerate(["0-0", "15-0", "15-15", "15-30", "30-30"]):
                store.record_score_events([ScoreRow(cid, "live", "S1", "3-2", game)])
            check(
                "every point is kept",
                store.prune_score_events()["removing"] == 0,
            )


# --------------------------------------------------------------------------
# dashboard: state classification, last-trade orientation, series reconstruction
# --------------------------------------------------------------------------


def test_score_events() -> None:
    print("\nscore history")
    from polymarket.store import ScoreRow

    kept, _ = discover(FakeAPI([_cincinnati_atp()]), _live_board())
    market = kept[0]
    cid = market.condition_id

    with tempfile.TemporaryDirectory() as tmp:
        with Store(Path(tmp) / "t.db") as store:
            opening = [ScoreRow.of(market)]
            check("first reading recorded", store.record_score_events(opening) == 1)
            check("unchanged reading not repeated", store.record_score_events(opening) == 0)

            moved = [ScoreRow(cid, "live", "S2", "6-3, 4-1")]
            check("changed score recorded", store.record_score_events(moved) == 1)
            check("changed period recorded", store.record_score_events([ScoreRow(cid, "ended", "FT", "6-3, 4-1")]) == 1)

            rows = store.conn.execute(
                "SELECT period, score FROM score_events WHERE condition_id = ? ORDER BY ts",
                (market.condition_id,),
            ).fetchall()
            check("history kept in order", [r[1] for r in rows] == ["6-3, 3-1", "6-3, 4-1", "6-3, 4-1"])
            check("final state recorded", rows[-1][0] == "FT")

            # Points move within a game, so they are a change worth a row even
            # when the score has not moved -- that is what the chart reads out
            # on hover. The marks stay on the games; see scoreMarkers.
            live = ScoreRow(cid, "live", "S2", "6-3, 4-1", "30-40")
            check("points are recorded", store.record_score_events([live]) == 1)
            check("the same points are not", store.record_score_events([live]) == 0)
            check(
                "the next point is",
                store.record_score_events([ScoreRow(cid, "live", "S2", "6-3, 4-1", "40-40")]) == 1,
            )
            games = store.conn.execute(
                "SELECT score, game FROM score_events WHERE condition_id = ? ORDER BY ts",
                (cid,),
            ).fetchall()
            check("the score underneath is unchanged", games[-1][0] == games[-2][0] == "6-3, 4-1")
            check("and the points differ", (games[-2][1], games[-1][1]) == ("30-40", "40-40"))


def test_dashboard_state() -> None:
    print("\ndashboard state")
    from polymarket.dashboard.queries import classify

    now = 1_800_000_000.0
    fresh, stale = now - 10, now - 4000
    soon = "2099-01-01T00:00:00Z"

    check("ended is past", classify("ended", soon, fresh, now) == "past")
    check("live and fresh is live", classify("live", None, fresh, now) == "live")
    # A finished capture leaves its last match sitting at "live" forever.
    check("live but stale is past", classify("live", None, stale, now) == "past")
    check("upcoming with a future start", classify("upcoming", soon, fresh, now) == "upcoming")

    # Tennis start times are "not before" times: overdue is normal for a while.
    from datetime import datetime, timezone

    def iso(offset: float) -> str:
        return datetime.fromtimestamp(now + offset, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    check("recently overdue stays upcoming", classify("upcoming", iso(-1800), fresh, now) == "upcoming")
    check("long overdue is past", classify("upcoming", iso(-13 * 3600), fresh, now) == "past")
    check("overdue with no capture is past", classify("upcoming", iso(-1800), stale, now) == "past")
    check("unknown state, no start time, stale", classify(None, None, stale, now) == "past")


def test_last_trade_orientation() -> None:
    print("\nlast-trade orientation")
    from polymarket.dashboard.queries import _orient

    # The stored number is the same on both rows; only its distance to a known
    # mid says which player it priced.
    check("already this side", _orient(0.80, 0.81) == 0.80)
    check("opponent's price is flipped back", abs(_orient(0.19, 0.81) - 0.81) < 1e-9)
    check("underdog side keeps its own price", abs(_orient(0.19, 0.19) - 0.19) < 1e-9)
    check("missing trade stays missing", _orient(None, 0.5) is None)
    check("no mid means no inference", _orient(0.19, None) == 0.19)
    # A coin-flip market is exactly the case the inference cannot resolve; it
    # must still return a value on the correct scale rather than blowing up.
    check("even market resolves to something sane", 0.0 <= _orient(0.5, 0.5) <= 1.0)


def test_dashboard_series() -> None:
    print("\ndashboard series")
    from polymarket.dashboard import queries

    kept, _ = discover(FakeAPI([_cincinnati_atp()]), _live_board())
    market = kept[0]

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "t.db"
        with Store(path) as store:
            store.upsert_markets(kept)
            base = 1_800_000_000.0
            book = dict(BOOK, last_trade_price="0.56")
            # t0: both sides written. t1: only outcome 0 moved, so outcome 1 has
            # no row -- exactly what "write only on change" produces.
            store.insert_snapshots(base, [
                (parse_book(market.tokens[0], book), market.condition_id, 0, market.outcomes[0]),
                (parse_book(market.tokens[1], book), market.condition_id, 1, market.outcomes[1]),
            ])
            moved = dict(book, bids=[{"price": "0.58", "size": "100"}])
            store.insert_snapshots(base + 10, [
                (parse_book(market.tokens[0], moved), market.condition_id, 0, market.outcomes[0]),
            ])
            # A side with no offers at all is real data, not a missing row.
            empty = {"bids": [], "asks": [], "last_trade_price": "0.56"}
            store.insert_snapshots(base + 20, [
                (parse_book(market.tokens[1], empty), market.condition_id, 1, market.outcomes[1]),
            ])

        conn = queries.connect(path)
        series = queries.match_series(conn, market.condition_id)

        check("one row per distinct timestamp", series["ts"] == [base, base + 10, base + 20])
        check("both outcomes on the shared grid", len(series["outcomes"]) == 2)

        first, second = series["outcomes"][0], series["outcomes"][1]
        check("outcome 0 moved at t1", first["bid"] == [0.55, 0.58, 0.58])
        # The gap at t1 means "unchanged", so the previous book carries forward.
        check("unchanged outcome carried forward", second["bid"][1] == 0.55)
        # An emptied book is a stored NULL and must stay NULL, not carry forward.
        check("emptied book reads as no quote", second["bid"][2] is None)
        check("no mid without both sides", second["mid"][2] is None)
        check("depth summed across levels", first["bid_depth"][0] == 100.0 + 250.0 + 900.0 + 500.0)
        check("last trade oriented to outcome 0", abs(series["last_trade"][0] - 0.56) < 1e-9)
        check("raw last trade preserved", series["last_trade_raw"][0] == 0.56)

        detail = queries.match_detail(conn, market.condition_id)
        check("detail found", detail is not None)
        check("detail reports capture depth", detail["depth"] == BOOK_DEPTH)
        # The latest outcome-0 book has one bid; the other two slots are stored
        # NULL padding and must not surface as empty ladder rows.
        check("ladder drops NULL padding", len(detail["books"][0]["bids"]) == 1)
        check("ladder keeps every real ask", len(detail["books"][0]["asks"]) == 3)
        check("emptied book has no levels", detail["books"][1]["bids"] == [])
        check("missing match returns nothing", queries.match_detail(conn, "0xdead") is None)

        view = queries.overview(conn)
        check("overview sees the match", len(view["matches"]) == 1)
        check("overview counts by tab", sum(view["counts"].values()) == 1)
        # The tour rides through to the UI: at a combined event the tournament
        # name alone cannot say which draw a card belongs to.
        check("overview carries the tour", view["matches"][0]["tour"] == "atp")
        check("and offers it as a filter", view["tours"] == ["atp"])
        check("detail carries it too", detail["tour"] == "atp")
        check("sparkline carries the mid series", len(view["matches"][0]["spark"]) == 2)
        # No score recorded yet: the card has no points to show rather than a
        # stale or invented pair.
        check("no points without a score event", view["matches"][0]["game"] is None)
        conn.close()

        # `markets` keeps only the set score, so the points on a card come from
        # the newest score_events row -- and it has to be the newest, since they
        # turn over several times a game.
        from polymarket.store import ScoreRow

        cid = market.condition_id
        with Store(path) as store:
            store.record_score_events([ScoreRow(cid, "live", "S2", "6-3, 4-1", "30-40")])
            store.record_score_events([ScoreRow(cid, "live", "S2", "6-3, 4-1", "40-40")])

        conn = queries.connect(path)
        check("overview carries the latest points", queries.overview(conn)["matches"][0]["game"] == "40-40")
        conn.close()

        # A capture written before score_events existed shows cards with no
        # points rather than failing to load.
        with sqlite3.connect(path) as raw:
            raw.execute("DROP TABLE score_events")
        conn = queries.connect(path)
        check("a database without the table still loads", queries.overview(conn)["matches"][0]["game"] is None)
        conn.close()


def test_decimation() -> None:
    print("\ndecimation")
    from polymarket.dashboard.queries import _decimate

    grid = [float(i) for i in range(1000)]
    kept = _decimate(grid, 100)
    check("thinned to the cap", len(kept) <= 102)
    check("first point kept", grid[0] in kept)
    check("last point kept", grid[-1] in kept)
    check("short grid untouched", _decimate(grid[:50], 100) == set(grid[:50]))
    check("every kept point is real", kept <= set(grid))


if __name__ == "__main__":
    test_book()
    test_filters()
    test_discovery()
    test_live_filter()
    test_store()
    test_migration()
    test_encoded_fields()
    test_poller()
    test_poll_cadence()
    test_score_feed()
    test_score_pairing()
    test_score_ratchet()
    test_score_events()
    test_score_poll_targeting()
    test_score_poll()
    test_score_cadence()
    test_score_poll_isolation()
    test_update_scores()
    test_prune_score_events()
    test_dashboard_state()
    test_last_trade_orientation()
    test_dashboard_series()
    test_decimation()
    print(f"\n{PASSED} checks passed\n")
