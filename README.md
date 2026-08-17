# Polymarket ATP tennis capture

Records the Polymarket order book for every ATP tour-level (250 and above) tennis
match **while it is being played**, every 10 seconds, 3 levels deep on each side,
into SQLite.

## Commands

```bash
uv run polymarket discover     # list the matches that would be captured
uv run polymarket run          # start capturing (Ctrl-C to stop)
uv run polymarket stats        # summarise the database
uv run polymarket sql          # latest quote for every match
uv run polymarket sql "SELECT ..."   # any query
```

`stats` and `sql` are safe to run while `run` is recording — they open the database
read-only, so a query can neither block nor damage the capture.

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
| `--dns MODE` | `auto` | see [DNS.md](DNS.md) |
| `-v` | off | verbose logging |

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

Two tables and a view, in one SQLite file.

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
uv run python tests/test_offline.py   # 120 checks, no network needed
```
