"""Offline checks against fabricated payloads (no network required).

    uv run python tests/test_offline.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from polymarket.book import parse_book  # noqa: E402
from polymarket.config import MATCH_SLUG, match_tournament  # noqa: E402
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
    check("depth capped at 3 bids", len(snap.bids) == 3)
    check("bids descending, best first", [p for p, _ in snap.bids] == [0.55, 0.50, 0.44])
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


# --------------------------------------------------------------------------
# tournament + slug parsing
# --------------------------------------------------------------------------


def test_filters() -> None:
    print("\ntournament matching")
    check("slam", match_tournament("Wimbledon").name == "Wimbledon")
    check("masters full name", match_tournament("Cincinnati Open").name == "Cincinnati Open")
    check("alias", match_tournament("Western & Southern Open").name == "Cincinnati Open")
    check("hyphen alias", match_tournament("Monte-Carlo Masters").name == "Monte-Carlo")
    check("atp 250", match_tournament("Winston-Salem Open").name == "Winston-Salem")
    check("atp 250 by city", match_tournament("Geneva Open").name == "Geneva")
    check("challenger city not on tour", match_tournament("Sion") is None)
    check("challenger city 2", match_tournament("Kingston") is None)
    check("challenger city 3", match_tournament("Prague 2") is None)
    check("non-tennis rejected", match_tournament("Will the Fed cut rates?") is None)

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
        self.slug_calls: list[list[str]] = []

    def tag_id(self, slug: str) -> str | None:
        return "864"

    def events(self, **_params: object):
        return iter(self._events)

    def events_by_slug(self, slugs):
        """Targeted fetch, as Gamma's repeated ?slug= gives us."""
        wanted = list(slugs)
        self.slug_calls.append(wanted)
        return [e for e in self._events if e.get("slug") in set(wanted)]


def _event(
    title: str,
    slug: str,
    markets: list[tuple[str, list[str], bool]],
    state: str = "live",
    start_time: str = "2026-08-16T18:30:00Z",
) -> dict:
    """Build an event in the live shape: generic tags, several markets per match."""
    feed = {
        "live": {"live": True, "ended": False, "period": "S2", "score": "6-3, 3-1"},
        "upcoming": {"live": None, "ended": None, "period": None, "score": None},
        "ended": {"live": False, "ended": True, "period": "FT", "score": "4-6, 2-6"},
        "cancelled": {"live": False, "ended": True, "period": "CAN", "score": "0-0"},
    }[state]
    return {
        "title": title,
        "slug": slug,
        "tags": [{"slug": t} for t in ("tennis", "sports", "games")],
        "startDate": "2026-08-16T10:00:00Z",
        "startTime": start_time,
        **feed,
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

    kept, _ = discover(FakeAPI(events))
    check("only the ATP moneyline kept", len(kept) == 1)
    check("right match", kept[0].question.startswith("Cincinnati Open: Botic"))
    check("tournament", kept[0].tournament == "Cincinnati Open")
    check("tier", kept[0].tier == "masters")
    check("match date from slug", kept[0].match_date == "2026-08-13")
    check("typed as moneyline", kept[0].market_type == "moneyline")
    check("player-name outcomes", kept[0].outcomes[1] == "Tallon Griekspoor")
    check("wta at same tournament excluded", not any("Bouzkova" in m.question for m in kept))
    check("challenger excluded", not any("Sion" in m.question for m in kept))
    check("doubles excluded", not any("Doubles" in m.question for m in kept))
    check("itf excluded", not any("ITF" in m.question for m in kept))
    check("outright excluded", not any("Will Carlos" in m.question for m in kept))

    everything, _ = discover(FakeAPI(events), all_markets=True)
    check("all-markets picks up derivatives", len(everything) == 3)
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
        len(discover(FakeAPI([qual]), include_qualifying=True)[0]) == 1,
    )


def test_live_filter() -> None:
    print("\nlive-match filter")
    from polymarket.discovery import match_state

    check("live flag", match_state({"live": True}) == "live")
    check("ended flag beats live", match_state({"live": True, "ended": True}) == "ended")
    check("full time", match_state({"period": "FT"}) == "ended")
    check("cancelled", match_state({"period": "CAN"}) == "ended")
    check("retired", match_state({"period": "RET"}) == "ended")
    check("set in progress", match_state({"period": "S3"}) == "live")
    check("set marker survives a live flicker", match_state({"live": False, "period": "S1"}) == "live")
    check("nothing yet", match_state({}) == "upcoming")
    check("nulls mean upcoming", match_state({"live": None, "ended": None}) == "upcoming")

    def match(title, slug, state, start="2026-08-16T20:15:00Z"):
        return _event(title, slug, [(title, ["A", "B"], True)], state, start)

    events = [
        match("Cincinnati Open: Playing Now", "atp-aaa-bbb-2026-08-16", "live"),
        match("Cincinnati Open: Later Today", "atp-ccc-ddd-2026-08-16", "upcoming"),
        match("Cincinnati Open: Already Done", "atp-eee-fff-2026-08-16", "ended"),
        match("Cincinnati Open: Called Off", "atp-ggg-hhh-2026-08-14", "cancelled"),
    ]

    everything, _ = discover(FakeAPI(events))
    check("without live_only, all four are returned", len(everything) == 4)

    live, skipped = discover(FakeAPI(events), live_only=True)
    check("live_only keeps just the live match", len(live) == 1)
    check("and it is the right one", live[0].question.endswith("Playing Now"))
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


# --------------------------------------------------------------------------
# storage round-trip
# --------------------------------------------------------------------------


def test_store() -> None:
    print("\nstorage")
    kept, _ = discover(FakeAPI([_cincinnati_atp()]))
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

    from polymarket.store import BOOK_COLUMNS, MARKET_COLUMNS

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
            kept, _ = discover(FakeAPI([_cincinnati_atp()]))
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
    from polymarket.poller import Poller, _seconds_until

    api = FakeAPI([_cincinnati_atp()])
    book = dict(BOOK)
    api.books = lambda token_ids: {tid: dict(book, asset_id=tid) for tid in token_ids}

    with tempfile.TemporaryDirectory() as tmp:
        with Store(Path(tmp) / "p.db") as store:
            poller = Poller(api, store, interval=10.0)
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
            poller = Poller(api, store, heartbeat=0.0)
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
            poller = Poller(api, store, only_changes=False)
            poller.refresh()
            check("every-tick writes regardless", poller.tick() == 2 and poller.tick() == 2)

    # live filtering drives what the loop tracks
    with tempfile.TemporaryDirectory() as tmp:
        with Store(Path(tmp) / "l.db") as store:
            upcoming = _event(
                "Cincinnati Open: Not Started vs Yet",
                "atp-nnn-yyy-2026-08-16",
                [("Cincinnati Open: Not Started vs Yet", ["N", "Y"], True)],
                "upcoming",
                "2099-01-01T00:00:00Z",
            )
            quiet = FakeAPI([upcoming])
            quiet.books = lambda token_ids: {}
            poller = Poller(quiet, store)
            poller.refresh()
            check("upcoming match is not tracked", poller.tracked == {})
            check("tick with nothing tracked is a no-op", poller.tick() == 0)
            check("next refresh is scheduled for the start time", poller._next_start is not None)

            quiet._events = [_cincinnati_atp()]
            poller.refresh()
            check("match is picked up once it goes live", len(poller.tracked) == 2)

            quiet._events = []
            poller.refresh()
            check("finished match stops being tracked", poller.tracked == {})

    print("\nstart-time scheduling")
    check("past times are ignored", _seconds_until("2020-01-01T00:00:00Z") is None)
    check("overdue match reschedules a quick re-check", _overdue_reschedules())
    check("garbage is ignored", _seconds_until("not a time") is None)
    check("missing is ignored", _seconds_until(None) is None)
    future = _seconds_until("2099-01-01T00:00:00Z")
    check("future times parse", future is not None and future > 0)


# --------------------------------------------------------------------------
# score polling on the tick cadence
# --------------------------------------------------------------------------


def _iso(offset_seconds: float) -> str:
    from datetime import datetime, timedelta, timezone

    when = datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_score_poll_targeting() -> None:
    """Which matches the poll asks about -- each event costs ~60 KB to re-read."""
    print("\nscore poll targeting")
    from polymarket.poller import Poller, Watched

    with tempfile.TemporaryDirectory() as tmp:
        with Store(Path(tmp) / "t.db") as store:
            poller = Poller(FakeAPI([]), store)

            def watch(cid: str, state: str, start: str | None) -> None:
                poller.watched[cid] = Watched(cid, f"slug-{cid}", start, f"Match {cid}")
                poller._state[cid] = state

            watch("live", "live", _iso(-3600))
            watch("ended", "ended", _iso(-7200))
            watch("soon", "upcoming", _iso(300))          # starts in 5 min
            watch("later", "upcoming", _iso(6 * 3600))    # starts in 6 hours
            watch("overdue", "upcoming", _iso(-1800))     # 30 min past its slot
            watch("abandoned", "upcoming", _iso(-9 * 3600))  # long past
            watch("undated", "upcoming", None)

            slugs = set(poller._score_slugs())
            check("a match in play is polled", "slug-live" in slugs)
            check("a finished match is not", "slug-ended" not in slugs)
            check("one about to start is polled", "slug-soon" in slugs)
            check("one hours away is not", "slug-later" not in slugs)
            check("an overdue match is still polled", "slug-overdue" in slugs)
            check("one long past its slot is dropped", "slug-abandoned" not in slugs)
            check("an undated match is polled", "slug-undated" in slugs)


def test_score_poll() -> None:
    print("\nscore poll")
    from polymarket.poller import Poller

    live = _cincinnati_atp()
    api = FakeAPI([live])
    api.books = lambda token_ids: {}

    with tempfile.TemporaryDirectory() as tmp:
        with Store(Path(tmp) / "s.db") as store:
            poller = Poller(api, store, score_interval=10.0)
            poller.refresh()
            check("a live match is watched", len(poller.watched) == 1)
            check("refresh seeds the known state", set(poller._state.values()) == {"live"})

            # refresh already recorded the opening reading, so a poll that sees
            # the same score must not write it again.
            check("an unchanged score writes nothing", poller.poll_scores() == 0)
            check("the poll asked only about the live match", api.slug_calls[-1] == [live["slug"]])

            live["score"] = "6-3, 4-1"
            check("a game going by is recorded", poller.poll_scores() == 1)
            check("and only once", poller.poll_scores() == 0)

            live["period"] = "S3"
            live["score"] = "6-3, 6-4, 1-0"
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

            # The match finishing is what should cut the capture short.
            check("no refresh pending yet", poller._state_changed is False)
            live["live"], live["ended"], live["period"] = False, True, "FT"
            poller.poll_scores()
            check("the end of a match is noticed", poller._state_changed is True)
            check("but not acted on instantly", poller._refresh_due(time.monotonic() - 5) is False)
            check("acted on once the gap has passed", poller._refresh_due(time.monotonic() - 90) is True)
            check("a normal refresh still wins", poller._refresh_due(time.monotonic() - 9999) is True)

            poller.refresh()
            check("refreshing clears the pending flag", poller._state_changed is False)


def test_score_cadence() -> None:
    """The poll only gets to run between ticks, so its deadline must allow for that."""
    print("\nscore cadence")
    from polymarket.poller import Poller

    with tempfile.TemporaryDirectory() as tmp:
        with Store(Path(tmp) / "c.db") as store:
            p = Poller(FakeAPI([]), store, interval=10.0, score_interval=10.0)
            check("polls on the very first tick", p._score_due() is True)

            now = time.monotonic()
            p._last_score = now
            check("not due again immediately", p._score_due() is False)
            # Equal intervals: the deadline lands within the same tick that should
            # serve it. Without slack this waits a whole extra tick and samples
            # at half the requested rate.
            p._last_score = now - 9.9
            check("the tick at the deadline serves it", p._score_due() is True)
            p._last_score = now - 4.0
            check("but not one arriving far too early", p._score_due() is False)

            slow = Poller(FakeAPI([]), store, interval=10.0, score_interval=30.0)
            slow._last_score = time.monotonic() - 10.0
            check("a longer interval makes ticks wait", slow._score_due() is False)
            slow._last_score = time.monotonic() - 29.0
            check("and fires on the tick nearest it", slow._score_due() is True)


def test_score_poll_isolation() -> None:
    """The score poll must never be able to take the capture down with it."""
    print("\nscore poll isolation")
    from polymarket.poller import Poller

    live = _cincinnati_atp()
    api = FakeAPI([live])
    api.books = lambda token_ids: {tid: dict(BOOK, asset_id=tid) for tid in token_ids}

    with tempfile.TemporaryDirectory() as tmp:
        with Store(Path(tmp) / "i.db") as store:
            poller = Poller(api, store)
            poller.refresh()

            def explode(_slugs):
                raise RuntimeError("score feed is down")

            api.events_by_slug = explode
            try:
                poller.poll_scores()
                raised = False
            except RuntimeError:
                raised = True
            check("poll_scores itself propagates", raised)
            # ...and the run loop is what swallows it, so books keep being written.
            check("books are unaffected", poller.tick() == 2)

            check("scores can be turned off entirely", Poller(api, store, score_interval=0)._score_due() is False)


def test_update_scores() -> None:
    print("\nscore updates")
    kept, _ = discover(FakeAPI([_cincinnati_atp()]))
    market = kept[0]

    with tempfile.TemporaryDirectory() as tmp:
        with Store(Path(tmp) / "u.db") as store:
            # A match the poll sees before discovery inserted it is a no-op, not
            # a half-built row.
            store.update_scores(kept)
            check("no row is invented", store.conn.execute("SELECT COUNT(*) FROM markets").fetchone()[0] == 0)

            store.upsert_markets(kept)
            before = store.conn.execute(
                "SELECT raw, question FROM markets WHERE condition_id = ?", (market.condition_id,)
            ).fetchone()

            market.period, market.score, market.state = "S5", "7-6", "live"
            store.update_scores(kept)
            after = store.conn.execute(
                "SELECT period, score, state, raw, question FROM markets WHERE condition_id = ?",
                (market.condition_id,),
            ).fetchone()
            check("score fields updated", tuple(after[:3]) == ("S5", "7-6", "live"))
            check("the raw payload is left alone", after[3] == before[0])
            check("identity is left alone", after[4] == before[1])


# --------------------------------------------------------------------------
# dashboard: state classification, last-trade orientation, series reconstruction
# --------------------------------------------------------------------------


def test_score_events() -> None:
    print("\nscore history")
    kept, _ = discover(FakeAPI([_cincinnati_atp()]))
    market = kept[0]

    with tempfile.TemporaryDirectory() as tmp:
        with Store(Path(tmp) / "t.db") as store:
            check("first reading recorded", store.record_score_events(kept) == 1)
            check("unchanged reading not repeated", store.record_score_events(kept) == 0)

            market.score = "6-3, 4-1"
            check("changed score recorded", store.record_score_events(kept) == 1)
            market.period = "FT"
            market.state = "ended"
            check("changed period recorded", store.record_score_events(kept) == 1)

            rows = store.conn.execute(
                "SELECT period, score FROM score_events WHERE condition_id = ? ORDER BY ts",
                (market.condition_id,),
            ).fetchall()
            check("history kept in order", [r[1] for r in rows] == ["6-3, 3-1", "6-3, 4-1", "6-3, 4-1"])
            check("final state recorded", rows[-1][0] == "FT")


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

    kept, _ = discover(FakeAPI([_cincinnati_atp()]))
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
        check("depth summed across levels", first["bid_depth"][0] == 100.0 + 250.0 + 900.0)
        check("last trade oriented to outcome 0", abs(series["last_trade"][0] - 0.56) < 1e-9)
        check("raw last trade preserved", series["last_trade_raw"][0] == 0.56)

        detail = queries.match_detail(conn, market.condition_id)
        check("detail found", detail is not None)
        check("detail reports capture depth", detail["depth"] == 3)
        # The latest outcome-0 book has one bid; the other two slots are stored
        # NULL padding and must not surface as empty ladder rows.
        check("ladder drops NULL padding", len(detail["books"][0]["bids"]) == 1)
        check("ladder keeps every real ask", len(detail["books"][0]["asks"]) == 3)
        check("emptied book has no levels", detail["books"][1]["bids"] == [])
        check("missing match returns nothing", queries.match_detail(conn, "0xdead") is None)

        view = queries.overview(conn)
        check("overview sees the match", len(view["matches"]) == 1)
        check("overview counts by tab", sum(view["counts"].values()) == 1)
        check("sparkline carries the mid series", len(view["matches"][0]["spark"]) == 2)
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
    test_score_events()
    test_score_poll_targeting()
    test_score_poll()
    test_score_cadence()
    test_score_poll_isolation()
    test_update_scores()
    test_dashboard_state()
    test_last_trade_orientation()
    test_dashboard_series()
    test_decimation()
    print(f"\n{PASSED} checks passed\n")
