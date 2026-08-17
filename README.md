# Polymarket ATP tennis capture

Records the Polymarket order book for every ATP tour-level (250 and above) tennis
match **while it is being played**, every 10 seconds, 3 levels deep on each side,
into SQLite.

## Commands

```bash
uv run polymarket discover     # list the matches that would be captured
uv run polymarket run          # start capturing (Ctrl-C to stop)
uv run polymarket dashboard    # browse it in a browser
uv run polymarket stats        # summarise the database
uv run polymarket sql          # latest quote for every match
uv run polymarket sql "SELECT ..."   # any query
```

`dashboard`, `stats` and `sql` are safe to run while `run` is recording — they open
the database read-only, so a query can neither block nor damage the capture. (The
dashboard opens it writable once at startup, to add any indexes an older database
is missing; that is the same idempotent migration `run` performs on every start,
and every query it then serves is read-only.)

Run `discover` first to see what's on, then leave `run` going. It captures only
matches actually in play, picks up each new match as it starts, and drops it when
it finishes — so it can stay running across a whole tournament unattended.

| Flag | Default | Effect |
|---|---|---|
| `--db PATH` | `data/tennis.db` | where to write |
| `--interval N` | `10` | seconds between snapshots |
| `--refresh N` | `300` | seconds between match-list refreshes |
| `--all-markets` | off | also capture set winner and games/sets over-under |
| `--include-qualifying` | off | also capture qualifying rounds |
| `--include-upcoming` | off | also poll matches that haven't started yet |
| `--every-tick` | off | write every tick, even when the book hasn't moved |
| `--heartbeat N` | `300` | write an unchanged book at least this often |
| `--score-interval N` | `10` | seconds between score-feed polls; `0` disables |
| `--dns MODE` | `auto` | see [DNS.md](DNS.md) |
| `-v` | off | verbose logging |

## Dashboard

```bash
uv run polymarket dashboard          # http://127.0.0.1:8787, opens a browser
uv run polymarket dashboard --port 9000 --no-open
```

Three tabs — **Live**, **Upcoming**, **Past** — over the same database `run` is
writing. It polls for new snapshots every 5 seconds, so a live match updates in
front of you; leave it open beside the capture.

| Flag | Default | Effect |
|---|---|---|
| `--port N` | `8787` | port to serve on |
| `--host H` | `127.0.0.1` | bind address; localhost only by default |
| `--no-open` | off | don't open a browser |

Each card shows both players' current prices, the score, and a sparkline. Opening
a match gives its full history:

- **Price** — both players' mid over time, with the last traded price overlaid.
- **Spread** — how far apart the two sides sit, and where quotes went missing.
- **Depth** — shares resting on each side, so you can see liquidity arrive or leave.
- **Order book** — the live 3-level ladder for both players.
- **Table view** — the same numbers as text, for reading exact values.

Set and game changes are drawn on every chart as vertical rules, and the hover
readout names the score at that moment — this comes from `score_events`, so it
only covers matches recorded after that table existed.

Which tab a match lands in comes from the score feed's `state`, plus how recently
the capture saw it: a match still marked `live` that nothing has touched for 15
minutes has finished, not stalled, so it moves to **Past**. A scheduled match
stays in **Upcoming** past its start time — tennis start times are "not before"
times — but not indefinitely.

The last-trade line is inferred, and the chart says so. Polymarket reports one
last-traded price per match oriented to whichever side traded last (see
`market_last_trade` below), and nothing in the record says which side that was.
The dashboard re-expresses it as the first player by which of the two mids it
sits nearer to. That is a good guess when the players are priced apart and a
coin-flip when they aren't; `last_trade_raw` in the API response is the
untouched value.

## Which matches are captured

ATP tour level only: Grand Slams, ATP Finals, Masters 1000, ATP 500, ATP 250.
Men's singles, main draw. Challengers, ITF, WTA, doubles and qualifying are
excluded.

`run` captures **only matches currently being played**. A finished match keeps
trading on Polymarket for hours or days until it's resolved, so "still tradeable"
is not the same as "still playing" — the match state comes from the live score
feed instead. Matches are picked up as they start (including late starts, which
are the norm) and dropped once they end. Pass `--include-upcoming` to also poll
matches that haven't started.

## The score feed

The score is read on the book cadence, not the market-list one — every
`--score-interval` seconds (10 by default), against only the matches whose score
can actually change: those in play, and those within 15 minutes of their slot.
Each change lands in `score_events`, so a price move can be read against the game
that caused it. At refresh-rate sampling a whole service game fits between two
readings; at 10 seconds none do.

Two things follow from it beyond the score itself:

- **A match starting or finishing is noticed within a tick**, not at the next
  refresh. `run` reacts by refreshing early, so a finished match stops being
  captured in seconds rather than minutes. Rate-limited to one triggered refresh
  a minute — the feed's `live` flag is known to flicker, and a refresh pages the
  whole tennis catalog.
- **It costs bandwidth.** Gamma has no endpoint that returns a score without the
  event's entire market list, and no parameter trims it, so each match costs about
  60 KB per poll — roughly 25 KB/s with four matches in play, 50 KB/s with eight.
  Raise `--score-interval` to trade resolution for traffic (30s still resolves
  every game), or pass `0` to switch it off and fall back to whatever the
  market-list refresh happens to catch.

`discover` shows everything with its state; `discover --live-only` shows exactly
what `run` would capture:

```
[LIVE S2 6-3, 4-2]  Cincinnati Open: Taylor Fritz vs Alex Michelsen
```

Each match has several markets on Polymarket — the match winner, plus set winner
and games/sets over-under. Only the **match winner** is captured unless you pass
`--all-markets`.

The tournament list lives in `TOURNAMENTS` in `src/polymarket/config.py`. The ATP
calendar shifts a little each year, so if a tournament isn't listed its matches
won't appear. `discover` is the way to check when a new event starts.

## What gets stored

Three tables and a view, in one SQLite file.

**`markets`** — one row per match:

`condition_id`, `question`, `tournament`, `tier`, `match_date`, `market_type`,
both player names (`outcome_0`, `outcome_1`) and their token ids, start/end dates,
and the raw API response.

Also `state` (live / upcoming / ended), `start_time`, and `period` + `score` from
the score feed (`S2`, `6-3, 4-2`). These are refreshed as the match progresses, so
they hold the latest known state rather than a per-tick history.

**`books`** — one row per player per 10-second tick:

| Column | Meaning |
|---|---|
| `ts` | unix timestamp of the tick |
| `outcome` | which player this row is for |
| `best_bid`, `best_ask` | top of book |
| `mid`, `spread` | derived from the two above |
| `bid_px_1..3`, `bid_sz_1..3` | 3 best bids, price and size, best first |
| `ask_px_1..3`, `ask_sz_1..3` | 3 best asks, price and size, best first |
| `market_last_trade` | last traded price for the **match**, not this player (see below) |
| `book_hash` | changes when the book changes |

Each match writes 2 rows per tick, one per player. Prices are probabilities
between 0 and 1; the two players' prices sum to roughly 1.

A snapshot is only written when something moved — price, depth, or last traded
price. During play the book moves almost every tick, so this changes little there;
it mostly stops quiet markets filling the database. An unchanged book is still
written every `--heartbeat` seconds (5 minutes by default), so a quiet market stays
distinguishable from a collector that stopped. `--every-tick` disables it.

A missing quote is stored as NULL rather than a made-up number — normal on a match
that's effectively decided, where one side has no offers left.

`market_last_trade` is the one column that is **not** about the player named in
`outcome`. The API reports a single last-traded price per match on both tokens,
oriented to whichever side traded last, so on one of the two rows it is the
opponent's price — Tommy Paul's row can read `0.19` while his own mid is `0.81`.
Use it per match, and don't compare it to that row's `mid`. Everything else in the
table is genuinely per-player.

**`score_events`** — one row each time a match's `state`, `period` or `score`
changes: `ts`, `condition_id`, `state`, `period`, `score`.

`markets` holds only the latest score, which says where a match stands but not
when it got there — so a price move can't be read against the point that caused
it. This table timestamps every change. Only changes are written, and the feed is
re-read every `--score-interval` seconds, so a row lands within about 10 seconds
of the game that produced it.

```sql
-- what the score was doing while the price moved
SELECT datetime(ts,'unixepoch') AS t, period, score
FROM score_events WHERE condition_id = '0x...' ORDER BY ts;
```

**`quotes`** — a view that spells out the direction, since bid/ask is easy to
invert:

```sql
buy_price  = best_ask   -- what you pay to buy this player
sell_price = best_bid   -- what you receive to sell
```

## Example queries

```sql
-- current prices for everything being tracked
SELECT utc_time, question, outcome, buy_price, sell_price, spread
FROM quotes WHERE ts = (SELECT MAX(ts) FROM books) ORDER BY question;

-- one match over time
SELECT datetime(ts,'unixepoch') AS t, outcome, mid, bid_sz_1, ask_sz_1
FROM books WHERE condition_id = '0x...' ORDER BY ts;

-- how much has been captured per match
SELECT m.question, COUNT(*) AS snapshots, MIN(b.ts), MAX(b.ts)
FROM books b JOIN markets m USING (condition_id)
GROUP BY m.condition_id ORDER BY snapshots DESC;
```

## Notes

- A Masters typically has a handful of matches in play at once — expect tens of
  thousands of rows a day, and a full season well under a gigabyte.
- Safe to stop and restart: it reopens the same database and carries on, and
  re-running a tick never duplicates rows.
- If the API can't be reached, see [DNS.md](DNS.md).

```bash
uv run python tests/test_offline.py   # 201 checks, no network needed
```
