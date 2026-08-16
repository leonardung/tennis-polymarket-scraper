# Polymarket ATP tennis capture

Records the Polymarket order book for every open ATP tour-level (250 and above)
tennis match, every 10 seconds, 3 levels deep on each side, into SQLite.

## Commands

```bash
uv run polymarket discover     # list the matches that would be captured
uv run polymarket run          # start capturing (Ctrl-C to stop)
uv run polymarket stats        # summarise the database
```

Run `discover` first to see what's live, then leave `run` going. It picks up new
matches and drops finished ones on its own, so it can stay running across a whole
tournament.

| Flag | Default | Effect |
|---|---|---|
| `--db PATH` | `data/tennis.db` | where to write |
| `--interval N` | `10` | seconds between snapshots |
| `--refresh N` | `300` | seconds between match-list refreshes |
| `--all-markets` | off | also capture set winner and games/sets over-under |
| `--include-qualifying` | off | also capture qualifying rounds |
| `--only-changes` | off | don't write a snapshot if the book hasn't moved |
| `--dns MODE` | `auto` | see [DNS.md](DNS.md) |
| `-v` | off | verbose logging |

## Which matches are captured

ATP tour level only: Grand Slams, ATP Finals, Masters 1000, ATP 500, ATP 250.
Men's singles, main draw. Challengers, ITF, WTA, doubles and qualifying are
excluded.

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

**`books`** — one row per player per 10-second tick:

| Column | Meaning |
|---|---|
| `ts` | unix timestamp of the tick |
| `outcome` | which player this row is for |
| `best_bid`, `best_ask` | top of book |
| `mid`, `spread` | derived from the two above |
| `bid_px_1..3`, `bid_sz_1..3` | 3 best bids, price and size, best first |
| `ask_px_1..3`, `ask_sz_1..3` | 3 best asks, price and size, best first |
| `last_trade_price` | last traded price |
| `book_hash` | changes when the book changes |

Each match writes 2 rows per tick, one per player. Prices are probabilities
between 0 and 1; the two players' prices sum to roughly 1.

A missing quote is stored as NULL rather than a made-up number — normal on a match
that's effectively decided, where one side has no offers left.

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

- A Masters day is roughly 20 matches, so about 350k rows a day. A full season
  stays well under a gigabyte.
- Safe to stop and restart: it reopens the same database and carries on, and
  re-running a tick never duplicates rows.
- If the API can't be reached, see [DNS.md](DNS.md).

```bash
uv run python tests/test_offline.py   # 86 checks, no network needed
```
