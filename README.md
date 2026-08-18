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
uv run polymarket clean-scores # repair a score history recorded before the ratchet
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
| `--score-interval N` | `10` | seconds between per-match score reads; cannot beat `--interval`, `0` disables |
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
readout names the score at that moment, down to the point and who is serving it
— `S2 6-3, 4-2 · 30-40 · Griekspoor serving`. This comes from `score_events`,
so it only covers matches recorded after that table existed, and the points and
server only those recorded after `game` and `serving` were added to it.

Which tab a match lands in comes from the score feed's `state`, plus how recently
the capture saw it: a match still marked `live` that nothing has touched for 15
minutes has finished, not stalled, so it moves to **Past**. A scheduled match
stays in **Upcoming** past its start time — tennis start times are "not before"
times — but not indefinitely.

The charts are [TradingView Lightweight Charts](https://www.tradingview.com/lightweight-charts/),
so they pan and zoom; the crosshair reads every series at once, with the score at
that moment underneath.

One thing worth knowing about them: Lightweight Charts spaces points by **index**,
not by time. That is right for daily bars and wrong for this data, which is
sampled every 10 seconds while a match is being played and every 5 minutes while
it is not — fed the rows as stored, nine quiet hours take as much width as ten
live minutes. The dashboard therefore resamples onto an evenly spaced time grid
before charting, carrying the last reading forward. That is not smoothing: the
capture only writes when the book moves, so a slot with no row means the previous
book still stood. A stored NULL carries forward as a gap, because that is an
absence of quotes rather than an absence of data.

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
feed instead (see below — it is Flashscore's, not Polymarket's). Matches are
picked up as they start (including late starts, which are the norm) and dropped
once they end. Pass `--include-upcoming` to also poll matches that haven't
started.

## The score feed

Scores come from **Flashscore**, not from Polymarket. Polymarket's own event
payload carries `live`, `period` and `score`, but Gamma has no endpoint that
returns them without the event's entire market list attached and no parameter
trims it — about 60 KB to re-read one score, or 50 KB/s at ten-second resolution
with eight matches on court. Flashscore's per-match feed answers the same
question in about 200 bytes.

Which is what makes the cadence affordable. The score is read on the book
cadence, not the market-list one — every `--score-interval` seconds (10 by
default), against only the matches whose score can actually change: those in
play, and those within 15 minutes of their slot. Each change lands in
`score_events`, so a price move can be read against the game that caused it. At
refresh-rate sampling a whole service game fits between two readings; at 10
seconds none do.

Two feeds are involved, and the split matters:

- **The day card** (`f_2_<day>_<tz>_en_1`) lists every match Flashscore has for a
  day. It is read on the market-list refresh, and it is what turns a Polymarket
  market into a Flashscore id. Three days are read — a night session lands on
  either side of the local date boundary — for about 750 KB every 5 minutes.
  It sits behind an edge cache that says `no-store` and then answers with an
  `Age` of one to four minutes, so it can identify matches but cannot follow one.
- **The per-match feeds** (`df_sur_2_<id>` and, for a match in play,
  `dc_2_<id>`) carry one match's status and set-by-set score, and the points in
  the game being played with who is serving them. About 200 bytes each. This is what the tick cadence
  reads. `dc_` is the optional one: a score without its points is still a
  score, so failing to read it does not lose the reading. It is also edge
  cached, but briefly: measured against matches in play, `Age` climbs to roughly
  two minutes and resets, and a set change was observed arriving 15 seconds after
  it happened.

That caching is not a simple delay, and it is worth knowing about before
changing anything here. The requests are answered by a pool of caches holding
copies of different ages, so two reads seconds apart can return a score and then
the score before it. Written down as they arrive, one game becomes three score
changes, two of them backwards — which is exactly what the first version did.
Two things stop it. Every read goes over a **single connection**, which keeps
them on one cache instead of scattering them (that is also what the host wants:
eight at once got the burst reset). And every reading, from the day card or the
per-match feed, passes through a **ratchet** before it is written: a tennis
score only advances, so one that has gone backwards is a stale copy and is
dropped. Not forever — a scorer correcting a mistake also reads as going
backwards — so a value that comes back on `SCORE_PATIENCE` reads in a row is
taken. A cache alternates with the fresh copy and never gets there; a
correction does. Replayed over a day of real capture, this removes 47% of the
recorded score changes, all of them spurious.

`--score-interval` was the lever for trading resolution against traffic when a
score cost 60 KB to read. At 400 bytes there is not much left to trade, and the
useful setting now is `0`, which switches the per-match reads off and takes
whatever the 5-minute day card happens to catch — worth reaching for if the
feed starts refusing requests.

The poll rides on the book tick and is only offered a turn between ticks, so it
can never run faster than `--interval`, and it lands on the nearest tick rather
than the one after. On the default 10-second tick that makes 5, 10 and 15 all
mean 10; the first value that actually slows it down is 20. `run` says so at
startup rather than appearing to accept a number and using another. To sample
scores faster than 10 seconds, lower `--interval` — which is the honest thing
to do anyway, since the books would otherwise still be on a 10-second grid and
there would be nothing finer to line the score up against.

Measured against a match in play, that grid is already fine enough: points turn
over about every 26 seconds, and the feed serves the new value the moment it
changes (`Age: 1s` at every change over a 2½-minute watch). A 10-second poll
sees each of them.

**A match starting or finishing is noticed within a tick**, not at the next
refresh. `run` reacts by refreshing early, so a finished match stops being
captured in seconds rather than minutes. Rate-limited to one triggered refresh a
minute, since a refresh pages the whole tennis catalog.

Pairing a market to a Flashscore match is by tournament and by **both** players
at once. Polymarket writes "Alex de Minaur" where Flashscore writes "De Minaur
A." and `de-minaur-alex`, so names are compared as folded token sets, ignoring
initials and bare particles — every Dutch player shares a "van". Requiring both
sides to agree is what makes a wrong pairing cost two coincidences rather than
one; two candidates that fit equally well are refused rather than guessed
between. Since Flashscore also reports which player is at home and Polymarket
does not, the score is stored in the order the market lists the players.

A match that cannot be paired is logged and falls back to a guess: not started
before its slot, assumed in play for six hours after it, finished past that.
Capturing a finished match for a few hours costs disk; calling a live one
finished loses the only copy of its book that will ever exist.

One status is worth knowing about: `INT`, a match stopped for rain or bad light.
Flashscore reports its coarse stage as "finished" while that lasts, but the match
has not ended and the book keeps trading — often hard, since a delay is news — so
it counts as live and stays captured.

The feed is undocumented. The `x-fsign` header is a constant lifted from the site
and the keys are single letters, so if scores start coming back empty, check the
key tables at the top of `src/polymarket/scores.py` first.

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
they hold the latest known state rather than a per-tick history. `score` reads in
the same order as `outcome_0` and `outcome_1`, and a set won on a tiebreak carries
the loser's points — `6-7(3)`.

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

**`score_events`** — one row each time a match's `state`, `period`, `score`,
`game` or `serving` changes: `ts`, `condition_id`, `state`, `period`, `score`,
`game`, `serving`.

`game` is the points inside the game being played — `30-40`, or plain counts
during a tiebreak — in the same player order as `score`, and `serving` is which
of the two is serving them, as an index into `outcome_0`/`outcome_1`. Together
they are what separates 30-40 on serve from 30-40 against it. They turn over
several times a game, so this table holds many more rows than the chart draws
marks: the marks are the games, these are where you were within one. Both are
only read while a set is actually in progress; the same feed keys on a finished
match hold its total games instead, which is why the period gates them.

```bash
uv run polymarket clean-scores           # report what it would remove
uv run polymarket clean-scores --apply   # remove it
```

A history recorded before the ratchet existed has the rewinds in it, roughly
half the rows. `clean-scores` replays what is stored through the same ratchet
the capture now uses, drops what it would not have accepted along with the
repeats those left stranded, and brings `markets` back in step with what
survives. It refuses to run while a capture is writing, and does nothing at all
without `--apply`.

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
uv run python tests/test_offline.py   # 333 checks, no network needed
```

## Working on the dashboard front end

The dashboard is React + TypeScript, built with Vite, in `frontend/`. The build
output lands in `src/polymarket/dashboard/static/` and **is committed**, so
`uv run polymarket dashboard` works without a Node toolchain. Rebuild it after
changing anything under `frontend/src`:

```bash
cd frontend
npm install          # once
npm run build        # typechecks, then writes into the Python package
npm run dev          # hot reload on :5173, proxying /api to :8787
```

`npm run dev` expects a dashboard already serving the real database alongside it
(`uv run polymarket dashboard --no-open`), so the front end reloads on save while
reading live capture data.

The Python side owns the data: everything the browser shows comes from the four
endpoints in `src/polymarket/dashboard/app.py`, and the reconstructions that
depend on how the capture writes — forward fill, last-trade orientation — happen
in `queries.py` rather than in the browser.
