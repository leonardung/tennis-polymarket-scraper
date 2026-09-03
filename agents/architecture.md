# Architecture

How this repo is put together, module by module, and which invariants a change
must not break. Written so an agent can add or modify a feature without reading
every file first. Companion: [project-context.md](project-context.md) (what the
project is for, conventions, workflows).

## One paragraph

A Python CLI (`src/polymarket/`) polls the Polymarket CLOB for ATP and WTA tennis
order books every 5 seconds while a match is being played -- every 60 seconds
before it starts, never once it is over -- and, on the same tick, reads the live
score and the match statistics from Flashscore. All three land in one SQLite
file, plus a fourth thing that does not run on the tick at all: a finished
match's per-set statistics, collected an hour after it ended. A FastAPI dashboard
(`src/polymarket/dashboard/`) serves that file read-only to a React + Lightweight
Charts front end (`frontend/`, built into the Python package). `docker compose`
runs the capture and the dashboard as two containers off one image, plus an
optional Cloudflare tunnel. `flashscore-scraper/` is a **separate, standalone**
package — a fuller Flashscore client that is not imported by anything in `src/`.

## Repository map

```
src/polymarket/            the package; `polymarket` console script -> cli.main
  cli.py                   argparse; subcommands discover/run/dashboard/stats/sql/clean-scores
  config.py                all tunables + the two tournament whitelists + regexes
  api.py                   Polymarket: Gamma (catalog) and CLOB (books) HTTP client
  book.py                  Snapshot dataclass; parse_book() normalises a raw book
  discovery.py             TennisMarket; the three gates that pick tour-level singles
  scores.py                Flashscore feeds, pairing, Ratchet/StatRatchet. The subtlest file here.
  poller.py                Poller: the capture loop (tick, score poll, stats poll, refresh)
  store.py                 Store: SQLite schema, migration, writes, prune_score_events
  resolver.py              DNS-over-HTTPS shim for ISPs that NXDOMAIN polymarket.com
  dashboard/
    app.py                 FastAPI app; 4 JSON endpoints + static mount; serve()
    queries.py             every read query; forward fill, decimation, last-trade orientation
    static/                COMMITTED Vite build output (index.html + assets/)
frontend/                  React 19 + TS + Vite sources for that bundle
  src/App.tsx              tabs, filters, routing; owns the match list
  src/hooks.ts             useDataVersion (pulse poll), useAsync, useRoute, useTicker
  src/api.ts, types.ts     fetch wrappers; TS mirrors of queries.py payloads
  src/theme.ts             light/dark; reads CSS tokens back out for the canvas charts
  src/components/          Chrome, MatchCard, MatchDetail, MatchStats, TimeSeriesChart,
                           OrderBook, Sparkline, TableView
tests/test_offline.py      ~455 assertions, no network, plain `python` script
flashscore-scraper/        standalone Flashscore client (NOT imported by src/)
Dockerfile,                two-stage image; capture + dashboard + cloudflared
docker-compose.yml
data/tennis.db             the capture (gitignored)
```

## Data flow

```
Gamma /events?tag_id=tennis ─┐
                             ├─> discovery.discover() ─> [TennisMarket]
Flashscore day card f_2_… ───┘         (3 gates + ScoreBoard.pair)
                                                │
                            Poller.refresh()  ──┤  every --refresh (300s),
                                                │  or early on a state change
                                                ▼
                                 store.upsert_markets + record_score_events
                                                │
  every --interval (5s), for the                ▼
  matches _due_matches() returns:
    CLOB POST /books (batched 50) ──> parse_book ──> Poller._changed ──> books
    Flashscore df_sur_2_<id> (+ dc_2_<id>) ──> Ratchet ──> markets, score_events
    Flashscore df_st_2_<id> ──> parse_stats ──> StatRatchet ──> stat_events
                                                │
  once, 1h after a match ends:                  │
    Flashscore df_st_2_<id> ──> every period ──> set_stats
                                                │
                                          data/tennis.db
                                                │  (mode=ro)
                             dashboard/queries.py ──> /api/* ──> React
```

## The capture loop (`poller.py`)

`Poller.run()` is a fixed-grid loop, not `sleep(interval)` — the target time is
`grid_start + tick_index * interval`, so latency never accumulates; if it falls
behind it skips whole slots and logs it.

Each iteration, in order:

0. **`_due_matches()`** — which matches this tick reads, by condition id, and it
   stamps them as read, so it is called **once** per iteration and the set is
   passed to both `tick()` and `poll_scores()`. A match whose `_state` is `live`
   is due every tick; anything else is due every `--idle-interval` (60s); a
   match that has ended is never due again and is logged once as retired. This
   is the only thing that varies -- the loop grid stays at `--interval`, and a
   match's book and score are still read in the same pass, so they share a
   timestamp.
1. **`tick(due)`** — one batched `POST /books` for the due token ids.
   `parse_book` normalises each; `_changed()` decides whether to write.
   Deduplication compares `_fingerprint()` (best bid/ask, both ladders,
   last trade) rather than the API's `hash`, which also moves for levels deeper
   than the 3 captured. An unchanged book is still written every `--heartbeat`
   (300s) so a quiet market stays distinguishable from a stopped collector.
2. **`poll_scores(due)`** — `_score_targets(due)` picks the due matches whose score can
   move (state `live`, or within `SCORE_LEAD`=900s of the slot, or with no slot
   at all). Each is re-read from its own Flashscore feed, run through the
   `Ratchet`, and written to `markets` (`update_scores`) plus `score_events`
   (`record_score_events`, changes only).
3. **`poll_stats(due)`** — `_stat_targets(due)` narrows further than the score
   does: matches in play only, only those whose pairing is `oriented`, and only
   those not read within `STATS_INTERVAL`=5s. One `df_st_2_<id>` per match,
   through `StatRatchet`, written to `stat_events` (`record_stat_events`,
   changes only). Before a match starts this feed answers with a full set of
   zeros — a shape, not an absence — which is why upcoming matches are excluded
   rather than merely deduplicated. Like `_due_matches`, it **stamps what it
   returns**, so call it once a tick. The 5s floor is about the tick budget, not
   the data: these reads are sequential on one connection with the score reads,
   and at `--interval=2` (which is what compose runs) sixteen live matches would
   otherwise spend most of a tick to learn nothing — and that tick is what the
   books are written on.
4. **`collect_final_stats()`** — once a minute (`FINAL_STATS_CHECK`), not once a
   tick. `store.matches_awaiting_set_stats` is the queue: matches that ended
   more than `FINAL_STATS_DELAY`=1h ago, less than `FINAL_STATS_WINDOW`=24h ago,
   with a stored `flashscore_id`, an oriented `flashscore_flip`, and no
   `set_stats` rows yet. `FINAL_STATS_BATCH`=4 at a time. The queue is a query
   over the database rather than a timer in memory, so a restart does not lose
   it and re-running writes one row per period rather than two.
5. **`_refresh_due()`** — refresh when the interval elapses, **or** when a match
   changed state (rate-limited to `MIN_REFRESH_GAP`=60s, since a refresh pages
   the whole tennis catalog), **or** at `_next_start`, the scheduled start of the
   next upcoming match plus `START_GRACE`.

`tick()`, `poll_scores()`, `poll_stats()` and `collect_final_stats()` are each
wrapped so no exception can kill the capture; the books matter more than the
score, and the score more than the statistics.

`refresh()` reloads the day card, re-runs `discover`, rebuilds `self.tracked`
(token id -> `Tracked`) and `self.watched` (condition id -> `Watched`, only for
matches Flashscore could pair). It then passes the day card's reading through the
ratchet too — the card is minutes old, so where it is behind, the already-known
score wins rather than the refresh rewinding it every 5 minutes.

## Market selection (`discovery.py`, `config.py`)

Three independent gates, in order:

1. `config.MATCH_SLUG` must match the **event slug** with its `tour` in the
   captured set (`config.TOURS`, both by default) and no `doubles-` segment. The
   slug prefix is the only reliable ATP/WTA signal — match events carry generic
   tags. This is what separates the two draws at combined events like Cincinnati,
   and it decides which calendar gate 2 searches.
2. The tournament name (event title up to the first `:`) must hit that tour's
   calendar — `config.ATP_TOURNAMENTS` or `WTA_TOURNAMENTS` — via
   `match_tournament(name, tour)`. This is what drops Challengers and WTA 125s,
   which use the same slug prefixes. `EXCLUDE` and `QUALIFYING` filter further.
3. The market must be tradeable (`acceptingOrders and active and not closed and
   not archived`).

Only the **moneyline** (question == event title) is kept unless `--all-markets`.
Nothing in the Polymarket payload is trusted for `state`/`period`/`score`;
`apply_score()` fills those from the `ScoreBoard`, and `unpaired_state()` is the
fallback guess for a match Flashscore does not have (upcoming before its slot,
assumed live for `OVERDUE_WINDOW`=6h after, ended past that — biased toward
over-capturing, because a live match's book cannot be recovered later).

## Scores (`scores.py`) — read this before touching anything score-related

Scores come from Flashscore, not Polymarket: Gamma has no endpoint that returns a
score without the event's whole market list (~60 KB per read); a Flashscore
per-match feed is ~200 bytes.

Feeds are `KEY÷VALUE` pairs joined by `¬`, blocks split by `~` (`parse_blocks`).

| Feed | Read on | Carries |
|---|---|---|
| `f_2_<day>_<tz>_en_1` | market-list refresh, 3 days (`FLASHSCORE_DAYS`) | the whole day card: ids, names, slugs, start times, status, set scores. A `ZA` heading names the circuit (`scores.TOUR_HEADINGS`); anything not under `ATP - SINGLES` or `WTA - SINGLES` is skipped |
| `df_sur_2_<id>` | every tick, per live match | one match's status + set-by-set score |
| `dc_2_<id>` | every tick, only while a set is in play | points in the game and who is serving; **optional** — a failure must not lose the reading |
| `df_st_2_<id>` | every tick per live match, and once 1h after it ends | 23 statistics per player, for the match as a whole **and** each set, in one ~4 KB response. Keys are `SE` (period), `SF` (group heading), `SG`/`SH`/`SI` (name, home, away), `A1` (the feed's own digest) |

Three mechanisms, each load-bearing:

- **Single connection.** `Flashscore.__init__` sets
  `httpx.Limits(max_connections=1, max_keepalive_connections=1)` and
  `readings()` is sequential. Flashscore answers from a pool of edge caches
  holding copies of different ages; spreading reads across connections makes the
  score appear to jump back and forth. Concurrency here also triggered a burst
  reset from the host. **Do not parallelise the score reads.**
- **`Ratchet`.** Every reading, from either feed, passes through
  `Ratchet.accept()` before it is written. `progress()` ranks a reading by
  (state, sets, total games, tiebreak points) — all monotone — so a reading that
  went backwards is a stale cache copy and is dropped. Not forever: a reading
  that persists for `SCORE_PATIENCE`=5 consecutive reads is taken, which is how a
  scorer's genuine correction lands. `Reading.settled` deliberately excludes
  `points` and `serving`, which legitimately go backwards (deuce, serve
  alternating). Replayed over a day of real capture this removed 47% of recorded
  score changes, all spurious.
- **`StatRatchet`.** The same mechanism for the statistics feed, measuring
  progress by *points played* — both players' "Total Points Won" are reported out
  of it, and it is the only number there that cannot fall. A reading level with
  the last one is **passed through**, not rejected: a statistic can change
  without a point being played (an unforced error reclassified as a winner, a
  serve speed landing late), and whether that is worth a row is
  `record_stat_events`'s question, not the ratchet's.
- **Pairing.** `ScoreBoard.pair(tour, tournament, players, start_time)` matches by
  tour and tournament **and both players at once**, comparing folded token sets
  (`name_tokens`: accent-folded, punctuation-split, single letters dropped since
  Flashscore abbreviates first names; `_PARTICLES` drops bare "de"/"van"). Ties
  are broken by more shared tokens, then by proximity to the market's start time.
  Two candidates that fit equally well are **refused, not guessed**. If the same
  match fits both ways round, `Paired.oriented=False`: the state is kept, the
  score dropped rather than published possibly mirrored.
  `Paired.flip` re-expresses every score in the order Polymarket lists the
  players — all rendering goes through `Paired.render/render_game/render_server`
  so that decision is made once.

Status codes live in `_STATUS` (detailed `AC`) with `_STAGE` (coarse `AB`) as
fallback. `36` = `INT` (rain / bad light) counts as **live**: the match has not
ended and the book keeps trading hard. Set keys are `_SET_KEYS`; points/serving
keys are `_POINTS_HOME/_POINTS_AWAY/_SERVING` (`DP`/`DQ`/`DR`) and are only read
while `in_a_game(period)` — on a finished match the same keys hold total games.

The feed is undocumented and `FLASHSCORE_SIGN` is a constant lifted from the
site. **If scores come back empty, check `_STATUS` and `_SET_KEYS` first.**

## Storage (`store.py`)

One SQLite file, WAL, `synchronous=NORMAL`, `isolation_level=None`.

| Object | Grain | Notes |
|---|---|---|
| `markets` | one row per condition_id | metadata (`tour` included) + **latest** state/period/score; `raw` is the JSON payload |
| `books` | one row per token per written tick | `PRIMARY KEY (token_id, ts) WITHOUT ROWID`; `INSERT OR REPLACE`, so replaying a tick never duplicates |
| `score_events` | one row per *change* of (state, period, score, game, serving) | `PRIMARY KEY (condition_id, ts)`; this is the timestamped history `markets` lacks |
| `stat_events` | one row per *change* of the running match statistics | `PRIMARY KEY (condition_id, ts)`; the feed's **overall** block only. No heartbeat -- `books` and `score_events` are writing on the same tick and already say whether anything was running |
| `set_stats` | one row per (match, period), written once an hour after the match | `PRIMARY KEY (condition_id, period)`; `INSERT OR REPLACE`, so collecting twice leaves one row. The per-set rows sum to the `Match` row, which is what makes it a check on `stat_events` |
| `quotes` | view | spells out direction: `buy_price = best_ask`, `sell_price = best_bid` |

Column lists are **generated**: `BOOK_COLUMNS` from `BOOK_DEPTH` via
`_depth_columns()`, so raising depth changes the schema in one place, and
`STAT_COLUMNS` from `config.STATISTICS` via `_stat_columns()` -- two columns per
statistic per player (`aces_0`, `aces_1`), plus a `_of` column for the ones the
feed reports as a made-of-attempted pair (`first_serve_won_0_of`). Both
`stat_events` and `set_stats` use that same list. `MARKET_COLUMNS` and
`SCORE_EVENT_COLUMNS` are explicit lists.

**Migration.** `Store._migrate()` runs on every open and `ALTER TABLE ... ADD
COLUMN`s anything missing. `CREATE TABLE IF NOT EXISTS` is a no-op on an existing
table, so without this an older database would fail every insert on column count.
The schema only ever grows; SQLite backfills NULL. Indexes and the view are in
`VIEWS`, applied *after* the migration because they reference new columns; the
view is dropped and recreated every open (free, keeps it in step with the code).

Three column semantics that are easy to get wrong:

- **`market_last_trade` is per MATCH, not per player.** The API reports one
  last-traded price on both tokens, oriented to whichever side traded last, so on
  one of the two rows it is the opponent's price. Never compare it to that row's
  `mid`.
- **NULL means no quote**, not zero and not missing data. One side of a decided
  match genuinely has no offers.
- **A statistic's percentage is never stored, only its numbers.** `75% (48/64)`
  becomes `48` and `64`; the percentage is their quotient and storing it too
  would let a row disagree with itself. A statistic the tournament does not
  measure (serve speed, distance covered) is NULL -- not measured is not none.
- **`markets.flashscore_id` / `flashscore_flip` are stored, not re-derived.**
  The per-set statistics are collected an hour after the match, by which time
  the market is usually resolved and gone from the API, so there is nothing left
  to pair against. A NULL `flashscore_flip` means the pairing was not oriented
  and nothing per-player may be attributed at all.
- **`ts` is when we read the book, `book_ts` is when it last changed upstream.**
  `ts - book_ts` is the age of the quote, and it is the only thing in the record
  that separates a market nobody is trading from a CLOB that has stopped serving
  fresh books. `book_ts_derived = 1` marks a value reconstructed by
  `backfill-book-ts` rather than read from the API — good to one polling
  interval, and it understates staleness across a capture outage.

`backfill_book_ts(apply=False)` fills `book_ts` for rows written before it was
recorded. Snapshots are only written when the book changed, so a run of
consecutive rows sharing a `book_hash` is one unchanged book seen repeatedly (the
heartbeat writing it out) and the book last moved at the first row of the run.
Batched with a commit between each so a concurrent capture is never blocked long;
only fills NULLs, so it is safe to re-run. Reachable as
`polymarket backfill-book-ts`, same idle guard as below.

`prune_score_events(apply=False)` replays stored history through the same
`Ratchet` and deletes what would not have been accepted, plus the repeats those
deletions strand, then brings `markets` back in step. Reachable as
`polymarket clean-scores`; refuses to run while a capture is writing
(`cli._capture_is_idle`, a 60s freshness check on `MAX(ts)`, not a lock).

## Dashboard back end (`dashboard/app.py`, `queries.py`)

`build_app(db)` returns a FastAPI app with one sqlite connection **per thread**
(`threading.local`) opened `file:…?mode=ro`. Four endpoints:

| Endpoint | Returns |
|---|---|
| `GET /api/overview` | every match with latest prices, sparkline, coverage, tab classification |
| `GET /api/pulse` | `{last_ts, rows}` — the cheap poll target |
| `GET /api/match/{cid}` | metadata, both ladders, oriented last trade, full `score_events`, and `stats` (`match_stats`: the latest `stat_events` row plus every `set_stats` period, kept apart rather than merged) |
| `GET /api/match/{cid}/series?points=&since=` | both players on one shared forward-filled grid |

`serve()` calls `_ensure_schema()` first, which opens the DB **writable once** to
run `Store`'s idempotent migration (a read-only connection cannot create the
index the per-match lookups seek on). Everything after that is read-only. `/`
returns `static/index.html`; `/static` is mounted at the built bundle.

Three reconstructions live in `queries.py` (server side, because they depend on
how the capture writes, not on how a chart draws):

- **`classify()`** decides the live/upcoming/past tab. `markets.state` is only as
  fresh as the last refresh that saw it, so a `live` row nothing has touched for
  `STALE_AFTER`=900s reads as `past`. An `upcoming` match past its slot stays
  upcoming for `OVERDUE_LIMIT`=12h (tennis start times are "not before" times).
- **Forward fill** in `match_series()`: rows are only written when the book
  moved, so the two players have different timestamps; the last row is carried
  across a shared grid. A stored NULL carries forward as NULL — absence of quotes,
  not absence of data.
- **`_orient()`**: re-expresses the per-match `market_last_trade` as outcome 0's
  price by whichever of `mid0` / `1 - mid0` it sits nearer to. An **inference**,
  unreliable exactly when the mids are close; `last_trade_raw` ships alongside it
  and the UI says so.

`_LATEST_BOOKS` is written as a `CROSS JOIN` seek driven from `markets`, not a
`ROW_NUMBER()` window — the `CROSS JOIN` is load-bearing (it pins the join order;
left free, SQLite scans all of `books`). `_decimate()` thins an over-long grid by
taking the **last** sample in each bucket, never an average (an averaged book is a
book that never existed).

## Dashboard front end (`frontend/`)

React 19 + TypeScript + Vite, no router, no state library. Built with
`npm run build` into `src/polymarket/dashboard/static/`, and **that output is
committed** — installing the package must not need a Node toolchain, and the
Docker image has no Node in it.

- **Polling**: `useDataVersion()` polls `/api/pulse` every 5s and bumps a counter
  only when `(last_ts, rows)` moves; the heavy endpoints are keyed on that
  counter. This exists because a chart re-render throws away the reader's zoom.
- **`useAsync`** keeps the previous value on screen while refetching
  (`refetching` flag, not a spinner swap).
- **Routing** is `window.location.hash`: `#tab/live`, `#match/<condition_id>`.
- **`TimeSeriesChart`** wraps Lightweight Charts. Its central problem:
  **Lightweight Charts spaces points by index, not by time**, which is wrong for
  data sampled every 5s in play and every 5 min otherwise. `toUniformGrid()`
  resamples onto an evenly spaced grid carrying the last reading forward; nulls
  become whitespace rather than being interpolated. The chart is created **once**
  (`[usable]` dep) and mutated afterwards — series set, data, markers, theme each
  have their own effect — because recreating it loses the zoom. `fitKey` refits
  the viewport, and it identifies the window *being shown*, not the one last
  requested. On a poll the data effect decides between following and holding:
  `viewport()` reads the visible range **as seconds** off the grid being drawn
  (`drawnGrid`, not `uniform` — that is already the new one) before `setData`,
  and `restoreViewport()` puts those same seconds back on the rebuilt grid. A
  view covering the whole window is refit instead, so a chart nobody has touched
  follows the capture. Holding by index would not hold anything: a poll slides
  the window and changes `toUniformGrid`'s step, so the same indices are a
  different stretch of the match.
- **`theme.ts`** owns light/dark. CSS custom properties are the single source of
  truth; canvas cannot read them, so `readPalette()` pulls the `CHART_TOKENS` out
  of computed style after a frame and the charts are restyled from that.
- **`MatchStats`** is plain DOM, no chart: one row per statistic, the two
  players either side of a two-segment bar. The bar's split is computed on the
  *rate* where the feed reports one — 49 of 67 first serves against 46 of 54 is
  the second player ahead, and a bar drawn from 49 against 46 says the opposite
  — and the percentage shown beside a pair is computed there too, never read
  back, since it is not stored. `Live` and the settled per-set tabs are separate
  selections, never merged: their disagreement is the point.
- **`Sparkline`** is deliberately inline SVG, not a chart instance: a tab can hold
  twenty cards, each Lightweight Chart owns a canvas and a resize observer.

`types.ts` mirrors the `queries.py` payloads by hand — **change both together**.
Every price field is nullable there on purpose.

## Deployment

`Dockerfile` — two stages, both on `python:3.12-slim-bookworm` (a venv records
its interpreter's absolute path, so the base must be identical in both). `uv`
arrives as a copied binary. Dependencies install from the lockfile alone in their
own layer, then the source, both with `--no-editable`. Final stage runs as uid
1000, `WORKDIR /data`, `ENTRYPOINT ["polymarket"]`.

`docker-compose.yml` — one image, three services:

| Service | Command / role |
|---|---|
| `capture` | `run --db=/data/tennis.db --interval=5 --include-upcoming`; healthcheck is just "the file exists" |
| `dashboard` | `dashboard --host=0.0.0.0 --no-open`, published to `127.0.0.1:8787` only; `depends_on: capture healthy` |
| `cloudflared` | outbound tunnel; token via `CLOUDFLARED_TOKEN` env, never a command line |

Two services rather than one because restarting the dashboard must never
interrupt a recording. The tunnel's origin must be `http://dashboard:8787` — the
compose service name; `network_mode: "service:dashboard"` is a documented trap
(recreating the dashboard leaves cloudflared on a dead namespace, `Up` but 502).

`resolver.py` handles ISPs that NXDOMAIN `polymarket.com`: it resolves the API
hostnames over DoH against `1.1.1.1`/`8.8.8.8` **by IP literal** (their certs
carry those IPs in SANs, so bootstrapping needs no DNS and verification stays on)
and patches `socket.getaddrinfo` for those hostnames only. `--dns auto` (default)
only activates when the system resolver fails. See `DNS.md`.

## Invariants a change must not break

1. **A tick's price, score and statistics share a timestamp.** A match's book,
   score and statistics are read in the same pass, off one `due` set. There is no
   separate score interval (there was; it was removed) -- what varies is which
   matches a tick reads, not which feed. `STATS_INTERVAL` is the one
   qualification, and it does not weaken this: it drops whole statistics reads,
   so a statistics row is still written on a tick that read that match's book
   and score. It never lets the three describe different moments.
2. **Score and statistics reads stay on one connection, sequential.** See
   `Flashscore.__init__`; `stat_readings` is a loop for the same reason
   `readings` is.
3. **Every score written passes through the `Ratchet`** — day card and per-match
   read alike — **and every statistics reading through the `StatRatchet`.**
4. **A missing quote is NULL**, never a substituted number, at every layer.
5. **`market_last_trade` is per match**; orientation is an inference and must
   ship with `last_trade_raw` and a caveat.
6. **The store's schema only grows**; `_migrate()` must keep opening old files.
7. **`books` writes are `INSERT OR REPLACE` on (token_id, ts)** — replaying a tick
   is idempotent.
8. **The dashboard is read-only** apart from the one startup migration.
9. **The committed bundle under `dashboard/static/` must match `frontend/src`** —
   rebuild and commit it, or the served UI silently lags the source.
10. **Staleness is judged on in-play books only.** Both cadences share one loop,
    so an idle tick sweeps in a whole day card of not-yet-started markets. Count
    those in `_check_stale` and one fresh upcoming book clears the warning every
    time that tick comes round — which is exactly what happened before it was
    restricted to `_state == "live"`.
11. **Bias toward over-capturing.** A finished match captured for a few hours
    costs disk; a live match called finished loses a book that cannot be recovered.
12. **Per-player data is stored in the market's outcome order**, through
    `Paired.flip` — the score via `Paired.render`, the statistics via
    `StatPeriod.sides`. A pairing that could not be oriented publishes neither.

## Where to make a change

| Goal | Touch |
|---|---|
| Add / fix a tournament | `config.ATP_TOURNAMENTS` or `config.WTA_TOURNAMENTS`, whichever tour plays it (and check with `polymarket discover`) |
| Capture another circuit | `config.TOURS` + a calendar + a `scores.TOUR_HEADINGS` entry; `discovery` and the poller take `tours` as an argument |
| Change a cadence or window | `config.py` constants; flags in `cli.py` |
| Change who is polled how often | `poller._due_matches` (`POLL_INTERVAL` / `IDLE_INTERVAL`) |
| Capture more book depth | `config.BOOK_DEPTH` — `store` columns and `queries._LEVELS` follow automatically; the DB migrates itself, old rows stay NULL |
| Capture a new field per tick | `book.Snapshot` + `parse_book` -> `store.BOOK_COLUMNS` + `insert_snapshots` -> `queries._BOOK_FIELDS` -> `types.ts` |
| Handle a new Flashscore status | `scores._STATUS` / `_STAGE` |
| Record a statistic the feed has started carrying | add a `Statistic` to `config.STATISTICS` — `store.STAT_COLUMNS` and both tables follow, and the DB migrates itself; the log line "no column for ..." is what tells you one is missing |
| Change when the per-set statistics are collected | `config.FINAL_STATS_DELAY` / `_WINDOW` / `_CHECK` / `_BATCH` |
| New API endpoint | `dashboard/app.py` + a function in `queries.py` + `types.ts` + `api.ts` |
| New chart or panel | `frontend/src/components/`, then `npm run build` and commit `static/` |
| New CLI subcommand | `cli.py`: a `cmd_*` function plus a `sub.add_parser(..., parents=[common])` with `set_defaults(func=..., needs_network=...)` |
