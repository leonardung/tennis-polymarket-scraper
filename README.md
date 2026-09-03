# Polymarket ATP and WTA tennis capture

Records the Polymarket order book for every tour-level (250 and above) ATP and
WTA singles match **while it is being played**, every 5 seconds, 10 levels deep on
each side, into SQLite — with the live score and the match statistics read on the
same tick, so a price, the point it moved on, and the aces count behind it all
share a timestamp.

## Commands

```bash
uv run polymarket discover     # list the matches that would be captured
uv run polymarket run          # start capturing (Ctrl-C to stop)
uv run polymarket dashboard    # browse it in a browser
uv run polymarket stats        # summarise the database
uv run polymarket sql          # latest quote for every match
uv run polymarket sql "SELECT ..."   # any query
uv run polymarket clean-scores # repair a score history recorded before the ratchet
uv run polymarket backfill-book-ts   # reconstruct book_ts for older rows
```

Or run the capture and the dashboard as two containers — see [Docker](#docker).

`dashboard`, `stats` and `sql` are safe to run while `run` is recording — they open
the database read-only, so a query can neither block nor damage the capture. (The
dashboard opens it writable once at startup, to add any indexes an older database
is missing; that is the same idempotent migration `run` performs on every start,
and every query it then serves is read-only.)

Run `discover` first to see what's on, then leave `run` going. It captures only
matches actually in play, picks up each new match as it starts, and drops it when
it finishes — so it can stay running across a whole tournament unattended.

Each match is read at its own cadence. A match **in play** is read every
`--interval` seconds, books and score together. A match that **hasn't started**
— only tracked at all with `--include-upcoming` — is read every
`--idle-interval` seconds instead: its book drifts, and its score has nothing to
say until it starts. A match that has **finished** is not read again; going live
puts a match on the fast cadence within one idle poll, and the state change also
triggers an early refresh.

| Flag | Default | Effect |
|---|---|---|
| `--db PATH` | `data/tennis.db` | where to write |
| `--interval N` | `5` | seconds between polls of a match in play — **books and scores both** |
| `--idle-interval N` | `60` | seconds between polls of a match that hasn't started; a finished one isn't polled at all |
| `--refresh N` | `300` | seconds between match-list refreshes |
| `--all-markets` | off | also capture set winner and games/sets over-under |
| `--include-qualifying` | off | also capture qualifying rounds |
| `--tour TOUR` | `both` | capture one circuit only: `atp`, `wta` or `both` |
| `--include-upcoming` | off | also poll matches that haven't started yet |
| `--every-tick` | off | write every tick, even when the book hasn't moved |
| `--no-stats` | off | don't record match statistics (see [Match statistics](#match-statistics)) |
| `--stats-interval N` | `5` | floor between statistics reads of one match, on top of `--interval` |
| `--heartbeat N` | `300` | write an unchanged book at least this often |
| `--stale-after N` | `120` | warn when every in-play book is this far behind its own upstream timestamp |
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

Each card shows both players' current prices, the score, and a sparkline. A live
match also carries the points in the game being played — `6-3, 4-2` `30-40` —
which turn over several times a game. Opening a match gives its full history:

- **Price** — both players' mid over time, with the last traded price overlaid.
- **Spread** — how far apart the two sides sit, and where quotes went missing.
- **Depth** — shares resting on each side, so you can see liquidity arrive or leave.
- **Match statistics** — aces, winners, points won and the rest, laid out the way
  Flashscore lays them out, with a bar for who is ahead on each. `Live` is the
  running total the capture read on the same tick as the book above; once the
  settled per-set reading has been collected, `Final` and one tab per set appear
  beside it. The two are kept apart on purpose — where they disagree, `Final` is
  right, and that disagreement is what the per-set table exists to show.
- **Order book** — the live 10-level ladder for both players.
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

**A zoom survives the poll.** While you are looking at the whole window — where
the 15m/1h/3h/All control leaves you — new snapshots are followed as they
arrive. Once you zoom or pan into part of it, that stretch of time is held
exactly as it was while the data underneath is replaced, and the new samples
accumulate off to the right until you scroll to them. Pressing a range button
again, including the one already selected, goes back to the full view and to
following.

One thing worth knowing about them: Lightweight Charts spaces points by **index**,
not by time. That is right for daily bars and wrong for this data, which is
sampled every 5 seconds while a match is being played and every 5 minutes while
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

## Docker

```bash
docker compose up -d          # capture + dashboard
docker compose logs -f capture
docker compose down
```

The dashboard is then on <http://127.0.0.1:8787> and the capture is writing
`./data/tennis.db` on the host, through a bind mount — so `sqlite3 data/tennis.db`,
`uv run polymarket stats` and the rest still work against it from outside the
containers, and nothing is trapped in a Docker volume.

Two services rather than one, off a single image: they fail and restart
independently, and rebuilding or restarting the dashboard must never interrupt a
recording, which is the one thing here that cannot be redone later. The dashboard
waits on a health check that the database file exists, because it exits rather
than serve a database that isn't there and would otherwise lose the race with
the capture's first tick by a fraction of a second.

| | |
|---|---|
| capture | `polymarket run --db=/data/tennis.db --interval=5 --include-upcoming` |
| dashboard | `polymarket dashboard --host=0.0.0.0`, published to `127.0.0.1:8787` only |
| cloudflared | optional public hostname for the dashboard, see below |
| data | `./data` on the host, mounted at `/data` |
| user | uid 1000, non-root; set `PUID`/`PGID` in a `.env` if yours differs |
| timestamps | UTC unless you set `TZ` in a `.env` |

Both containers run as uid 1000 so the bind-mounted `./data` needs no `chown`.
`--host=0.0.0.0` only makes the dashboard reachable inside its container; what
decides who can actually reach it is the port publish, which is bound to
localhost. Change it to `8787:8787` to expose it on the network.

Unlike a bare `polymarket run`, the compose file passes `--include-upcoming`:
the hours before a match are when its price moves on news, and a container left
running unattended may as well record them. Drop it from `command:` if you only
want matches in play — that is roughly the difference between polling every
listed match and polling the two or three being played.

Change a flag by editing `command:` in `docker-compose.yml`, or run any
subcommand one-off against the same database:

```bash
docker compose run --rm capture discover
docker compose run --rm capture stats
```

### Reaching it from outside

The stack includes a [Cloudflare tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/),
so the dashboard can be read from anywhere without forwarding a port or opening
anything on the host — cloudflared dials out to Cloudflare and traffic comes
back down that connection.

Put the tunnel's token in `.env` (see `.env.example`); it is gitignored, because
that token *is* the tunnel — anyone holding it can serve traffic as your
hostname. It reaches the container as an environment variable rather than as
`--token` on the command line, where `docker compose ps` and the host's process
list would both show it.

On the Cloudflare side, the public hostname's **Service** must be:

```
HTTP  ->  dashboard:8787
```

`dashboard` — not `localhost`. cloudflared runs in its own container, where
localhost is cloudflared; `dashboard` is the service name, which Docker also
makes its hostname on the compose network. Getting this wrong is a 502 with
`dial tcp [::1]:8787: connect: connection refused` in `docker compose logs
cloudflared`.

Sharing the dashboard's network namespace (`network_mode: "service:dashboard"`)
would make `localhost:8787` correct and save that step, and it is a trap:
recreating the dashboard leaves cloudflared holding a dead namespace, still
reporting `Up` while every request 502s — and `docker compose up -d` after a
rebuild does exactly that.

Anyone with the URL can read the whole capture; there is no login. Put
[Cloudflare Access](https://developers.cloudflare.com/cloudflare-one/policies/access/)
in front of the hostname if that matters — the dashboard is read-only, so the
exposure is what has been recorded, not the recording itself.

The image has no Node in it: it serves the frontend bundle committed under
`src/polymarket/dashboard/static/`, by the same rule that installing the package
doesn't need a Node toolchain. After changing anything in `frontend/src`, run
`npm run build` and then `docker compose build`.

## Which matches are captured

Tour level only, both circuits: Grand Slams, the two Finals, Masters 1000 and WTA
1000, ATP/WTA 500 and 250. Singles, main draw. Challengers, WTA 125s, ITF,
doubles and qualifying are excluded. `--tour atp` or `--tour wta` narrows the
capture to one circuit.

A combined event — Cincinnati, Indian Wells, the slams — runs both draws under
one tournament name, and nothing in the Polymarket payload distinguishes them
except the event slug's `atp-` / `wta-` prefix. That prefix is what decides which
calendar the tournament name is looked up on, so the same week is a Masters 1000
on one side and a WTA 1000 on the other. Every match is stored with its `tour`,
and the dashboard filters on it.

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
cadence, not the market-list one — every `--interval` seconds for a match in
play, every `--idle-interval` for one waiting to start, and against only the
matches whose score can actually change: those in play, and those within 15
minutes of their slot. Each change lands in
`score_events`, so a price move can be read against the game that caused it. At
refresh-rate sampling a whole service game fits between two readings; at 10
seconds none do.

Three feeds are involved, and the split matters:

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
- **The statistics feed** (`df_st_2_<id>`) carries the aces, winners, points won
  and twenty more, for the match as a whole *and* for each set so far, all in one
  response of about 4 KB (a kilobyte on the wire). It is read on the tick beside
  the other two. See [Match statistics](#match-statistics).

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

**One cadence per match, not one per feed.** A match's books and its score are
read in the same pass, so there is nothing to keep in step and no way for the two
to drift: a price and the point it moved on carry the same timestamp because they
were fetched together. What varies is which matches are on a given tick —
`--interval` for the ones in play, `--idle-interval` for the rest — never
whether the two feeds agree. There was a separate `--score-interval`, from when a
score cost 60 KB to read off Polymarket and slowing it down saved real bandwidth.
At 400 bytes it saved nothing, could not go faster than the tick it rode on
anyway, and silently ignored half its own range. It is gone.

For scale: points turn over about every 26 seconds, and the feed serves the new
value the moment it changes — `Age: 1s` at every change across a 2½-minute
watch. So the 5-second default sees every point with room to spare, and the
binding constraint on reading a price against a point is the book grid, which
is the same number.

**A match starting or finishing is noticed within a tick**, not at the next
refresh. `run` reacts by refreshing early, so a finished match stops being
captured in seconds rather than minutes. Rate-limited to one triggered refresh a
minute, since a refresh pages the whole tennis catalog.

Pairing a market to a Flashscore match is by tour and tournament and by **both**
players at once. The day card heads each block with the circuit — `ATP -
SINGLES`, `WTA - SINGLES` — which is both what keeps a Challenger or a WTA 125
out and what tells the two draws of a combined event apart. Polymarket writes "Alex de Minaur" where Flashscore writes "De Minaur
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

The tournament lists live in `ATP_TOURNAMENTS` and `WTA_TOURNAMENTS` in
`src/polymarket/config.py`, one per circuit. Both calendars shift a little each
year, so if a tournament isn't listed its matches won't appear. `discover` is the
way to check when a new event starts.

## Match statistics

Flashscore publishes 23 statistics per match — aces, double faults, first serve
percentage, points won on each serve, break points saved and converted, winners,
unforced errors, net points, games won, average serve speed, distance covered —
and it publishes them **twice over**: once as running match totals, and once
broken down by set. One request, `df_st_2_<id>`, returns all of it.

The counters move on nearly every point, so the running totals are read on the
tick with the book and the score, and, like a book snapshot, **only what changed
is written**. There is a floor of `--stats-interval` (5s) between reads of one
match, and it is there for the tick rather than for the data: these reads are
sequential on one connection with the score reads, a point takes about 26
seconds, and at `--interval=2` an unfloored read asks thirteen times per point —
which at a slam, with sixteen matches on court, is what pushes a tick past its
own budget and starts costing book snapshots. A tick that does read them still
reads them beside that tick's book and score. Nothing gets a heartbeat row here: a book that stops moving is
ambiguous, but a statistic that stops moving is not, because `books` and
`score_events` are writing on the same tick and already say whether anything was
running. In practice a match in play produces a row every ten seconds or so —
comfortably finer than the ~26 seconds a point takes, which is what "the
statistics at every point" needs.

Two things are true of this feed that are true of the score feed for the same
reason, and are handled the same way:

- It comes off the same edge caches, so it goes **backwards**. The ratchet here
  measures progress by *points played* — both players' "Total Points Won" are
  reported out of it, and it is the one number that cannot fall — and a reading
  covering fewer points than one already taken is dropped, unless it keeps
  coming back for `SCORE_PATIENCE` reads, which is a scorer's correction rather
  than a cache. A reading level with the last one is passed through: a statistic
  can genuinely change without a point being played, when an unforced error is
  reclassified as a winner or a serve speed lands a beat late.
- The statistics are **per player**, so they go through the same flip the score
  does and are stored in the order the market lists its players. A match whose
  two names fit each other's side equally well is skipped entirely — a mirrored
  row is worse than no row.

A value arrives in one of four shapes, and what is stored is always the numbers,
never the percentage:

| Feed | Stored |
|---|---|
| `18` | `aces_0 = 18` |
| `63%` | `first_serve_pct_0 = 63` |
| `194 km/h` | `first_serve_speed_0 = 194` |
| `75% (48/64)` | `first_serve_won_0 = 48`, `first_serve_won_0_of = 64` |
| `1/3` | `break_points_saved_0 = 1`, `break_points_saved_0_of = 3` |

The percentage is the quotient of two numbers that are already stored, and
keeping it as well would let a row disagree with itself. A statistic the
tournament does not measure — serve speed and distance covered need ball
tracking, and only the big events have it — is **NULL, not zero**: not measured
is not none. The catalogue is `STATISTICS` in `src/polymarket/config.py`, and it
generates the columns; a label the feed carries that is not on that list is
dropped and logged once, which is the notice that the site has added a row.

### The settled per-set version

Flashscore keeps revising a finished match for a while after the last point — an
unforced error becomes a winner, the radar's serve speeds are corrected — so the
per-set breakdown is **collected once, an hour after the match ends**, into
`set_stats`. The per-set rows sum to the `Match` row, and that is the point of
the table: it is the record to check the live capture against.

That queue is a query, not a timer — a match that has ended, is past the hour,
and has no `set_stats` rows yet — so a capture that was not running when the hour
came round picks it up on the next look rather than losing it, and running it
twice leaves one row per period rather than two. It gives up on a match 24 hours
after it ended, and after three empty answers (a walkover has no statistics and
never will).

`--no-stats` turns all of this off. The cost it saves is one extra Flashscore
read per live match per tick — about 50 ms and a kilobyte each.

## What gets stored

Five tables and a view, in one SQLite file.

**`markets`** — one row per match:

`condition_id`, `question`, `tour`, `tournament`, `tier`, `match_date`, `market_type`,
both player names (`outcome_0`, `outcome_1`) and their token ids, start/end dates,
and the raw API response.

Also `state` (live / upcoming / ended), `start_time`, and `period` + `score` from
the score feed (`S2`, `6-3, 4-2`). These are refreshed as the match progresses, so
they hold the latest known state rather than a per-tick history. `score` reads in
the same order as `outcome_0` and `outcome_1`, and a set won on a tiebreak carries
the loser's points — `6-7(3)`.

And `flashscore_id` + `flashscore_flip`: which Flashscore match this is, and
whether that feed's home player is this market's *second* outcome. Stored rather
than re-derived because the per-set statistics are collected an hour after the
match ends, by which time the market has usually been resolved and dropped from
the API — there is nothing left to pair against, only what was written down. A
NULL `flashscore_flip` means the two names fitted each other's side equally well,
so nothing that reads home from away may be attributed to a player at all.

**`books`** — one row per player per tick:

| Column | Meaning |
|---|---|
| `ts` | unix timestamp of the tick |
| `outcome` | which player this row is for |
| `best_bid`, `best_ask` | top of book |
| `mid`, `spread` | derived from the two above |
| `bid_px_1..10`, `bid_sz_1..10` | 10 best bids, price and size, best first |
| `ask_px_1..10`, `ask_sz_1..10` | 10 best asks, price and size, best first |
| `market_last_trade` | last traded price for the **match**, not this player (see below) |
| `book_hash` | changes when the book changes |
| `book_ts` | when the book last changed **upstream** (see below) |
| `book_ts_derived` | `0` if `book_ts` came from the API, `1` if reconstructed |

Each match writes 2 rows per tick, one per player. Prices are probabilities
between 0 and 1; the two players' prices sum to roughly 1.

A snapshot is only written when something moved — price, depth, or last traded
price. During play the book moves almost every tick, so this changes little there;
it mostly stops quiet markets filling the database. An unchanged book is still
written every `--heartbeat` seconds (5 minutes by default), so a quiet market stays
distinguishable from a collector that stopped. `--every-tick` disables it.

A missing quote is stored as NULL rather than a made-up number — normal on a match
that's effectively decided, where one side has no offers left.

`ts` is when the book was read; `book_ts` is when it last actually changed, as
reported by the API. The difference is the age of the quote:

```sql
SELECT ts - book_ts AS stale_seconds FROM books WHERE token_id = ?;
```

That matters because Polymarket's CLOB serves a **stale** book during an outage
rather than failing — every request still returns 200 in milliseconds, the book
behind it just stops moving. Nothing else in the record can tell that apart from
a market nobody is trading, so a price whose `stale_seconds` is large was real
once but was not the price at `ts`. Filter on it before reading price against
play. The capture warns in its log while it is happening — see `--stale-after`.

`book_ts_derived = 1` marks a row whose `book_ts` was reconstructed after the
fact by `backfill-book-ts` rather than read from the API, which is accurate only
to one polling interval and understates staleness across a capture outage. Rows
recorded before this column existed and never backfilled have `book_ts` NULL.

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

```bash
uv run polymarket backfill-book-ts           # report how many rows lack book_ts
uv run polymarket backfill-book-ts --apply   # reconstruct it
```

Rows recorded before `book_ts` was stored can have it inferred from what is
already there. Snapshots are only written when the book changed, so a run of
consecutive rows sharing a `book_hash` is one unchanged book seen repeatedly —
the heartbeat writing it out — and the book last moved at the first row of that
run. Every row in the run gets that timestamp and is marked
`book_ts_derived = 1`, because the value is the first time the hash was *seen*:
late by up to one polling interval, and blind to a change that happened while
nothing was recording. It only fills rows where `book_ts` is NULL, so it is safe
to re-run and never overwrites what the API reported. Like `clean-scores`, it
refuses to run while a capture is writing and does nothing without `--apply`.

`markets` holds only the latest score, which says where a match stands but not
when it got there — so a price move can't be read against the point that caused
it. This table timestamps every change. Only changes are written, and the feed is
re-read every `--interval` seconds while the match is in play, so a row lands
within a few seconds of the game that produced it.

```sql
-- what the score was doing while the price moved
SELECT datetime(ts,'unixepoch') AS t, period, score
FROM score_events WHERE condition_id = '0x...' ORDER BY ts;
```

**`stat_events`** — one row each time the match's running statistics moved:

`ts`, `condition_id`, `digest` (the feed's own hash of the response the row came
from), and two columns per statistic per player — `aces_0`, `aces_1`,
`winners_0`, `first_serve_won_0` with `first_serve_won_0_of` beside it, and so
on for the whole of `STATISTICS`. The `_0` / `_1` suffix is the outcome index,
the same order `books.outcome_index` and `markets.outcome_0` use.

Only the feed's **overall** block — the running match totals as they stood at
`ts` — is recorded here. Changes only, no heartbeat, and every reading through
the ratchet. See [Match statistics](#match-statistics).

```sql
-- aces against the price, on one clock
SELECT datetime(s.ts,'unixepoch') AS t, s.aces_0, s.aces_1,
       s.total_points_won_0_of AS points_played
FROM stat_events s WHERE s.condition_id = '0x...' ORDER BY s.ts;
```

**`set_stats`** — the settled statistics, one row per period, taken once an hour
after the match ended:

`condition_id`, `period` (`Match`, `Set 1`, `Set 2`, …), `ts` (when it was
*collected*, not when the set was played), `digest`, and the same statistic
columns as `stat_events`. The per-set rows sum to the `Match` row, which is what
makes this the record to check the live capture against:

```sql
-- do the sets add up to the match?
SELECT period, aces_0, aces_1, total_points_won_0_of AS points
FROM set_stats WHERE condition_id = '0x...' ORDER BY period;
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

-- what the book did on the points around a break point being saved
SELECT datetime(s.ts,'unixepoch') AS t,
       s.break_points_saved_0 || '/' || s.break_points_saved_0_of AS saved,
       (SELECT b.mid FROM books b
        WHERE b.condition_id = s.condition_id AND b.outcome_index = 0
          AND b.ts <= s.ts ORDER BY b.ts DESC LIMIT 1) AS mid
FROM stat_events s WHERE s.condition_id = '0x...' ORDER BY s.ts;

-- live capture against the settled per-set version, for one match
SELECT (SELECT aces_0 FROM stat_events WHERE condition_id = f.condition_id
        ORDER BY ts DESC LIMIT 1)              AS captured_live,
       f.aces_0                                AS settled
FROM set_stats f WHERE f.condition_id = '0x...' AND f.period = 'Match';
```

## Notes

- A Masters typically has a handful of matches in play at once — expect tens of
  thousands of rows a day, and a full season comfortably inside a gigabyte. The
  5-second tick roughly doubles that against the 10 seconds this used to run
  at; raise `--interval` if you would rather have the disk. Only the matches in
  play run at that cadence, so a day card full of matches that haven't started
  costs a poll a minute each, not one every five seconds.
- Match statistics add roughly a row every ten seconds per match in play — about
  a thousand rows over a three-set match, against the tens of thousands its books
  produce — plus one `set_stats` row per period, once. On the wire they cost a
  kilobyte and about 50 ms per live match per tick. `--no-stats` turns them off.
- Safe to stop and restart: it reopens the same database and carries on, and
  re-running a tick never duplicates rows.
- If the API can't be reached, see [DNS.md](DNS.md).

```bash
uv run python tests/test_offline.py   # 453 checks, no network needed
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

The Docker image serves that committed bundle rather than building one, so a
frontend change reaches a container only after `npm run build` **and**
`docker compose build`.

`npm run dev` expects a dashboard already serving the real database alongside it
(`uv run polymarket dashboard --no-open`), so the front end reloads on save while
reading live capture data.

The Python side owns the data: everything the browser shows comes from the four
endpoints in `src/polymarket/dashboard/app.py`, and the reconstructions that
depend on how the capture writes — forward fill, last-trade orientation — happen
in `queries.py` rather than in the browser.
