# Project context

What this project is for, how it is worked on, and the things that are true but
not visible in the code. Companion: [architecture.md](architecture.md) (module
map, data flow, invariants).

## What it is

A research capture tool. It records the Polymarket order book for every
tour-level (250 and above) ATP and WTA singles match **while it is being
played** — every 5 seconds, 10 levels deep on each side — into SQLite, with the
live score and the match statistics read on the same tick. Their individual
timestamps say when each sequential read happened; their shared `tick_id` is
the exact join key. A match that has not started yet is read once a minute instead,
and one that has finished is not read again. A read-only web dashboard browses
what has been recorded.

Both circuits are captured by default; `--tour atp` / `--tour wta` narrows it to
one. A combined event runs both draws under one tournament name, so the event
slug's tour prefix is what tells them apart, and each draw is looked up on its
own calendar.

The point of the dataset is reading price against play: what does the book do
when a break point is saved, when a set turns, when a match is stopped for rain.
That is why the score is timestamped in its own table rather than only kept as a
current value, and why the book, the score and the statistics of one match are
always read in the same pass, on that match's own cadence.

The statistics are there because the score alone does not say *how* a game was
won. 30-40 is a break point either way; whether it was saved on an ace or handed
over on a double fault is in `stat_events`, on the same clock as the price. They
are recorded like a book snapshot -- every tick, changes only -- and then taken
again once, an hour after the match, broken down by set, because Flashscore goes
on revising a finished match and that late reading is the one to check the live
capture against.

**It only reads.** There is no trading, no wallet, no authentication, no order
placement anywhere in the codebase, and none of the APIs it touches require auth.

## The governing constraint

**A live order book cannot be recovered later.** Polymarket keeps no history you
can go back for, and a match that was not captured while it was being played is
simply gone. Every design decision here bends that way:

- an unpaired match is *assumed live* for six hours rather than dropped;
- `INT` (rain delay) counts as live, because the book keeps trading;
- the capture and the dashboard are separate containers so restarting the UI
  cannot interrupt a recording;
- every tick and score poll is wrapped so no exception kills the loop;
- the capture is safe to stop and restart — it reopens the same file and carries
  on, and re-running a tick never duplicates rows.

When a change trades disk or tidiness against the risk of missing a live book,
choose the book.

## Scale and cost

A Masters week has a handful of matches in play at once, and a combined week
roughly twice that, since both draws are on court together. Expect tens of thousands of
rows a day; a full season fits comfortably inside a gigabyte. The current
`data/tennis.db` is ~25 MB. A score read is ~200 bytes per live match per tick
and a statistics read about a kilobyte on the wire and 50 ms; the day card is
~750 KB every 5 minutes. Points turn over about every 26 seconds, so the
5-second tick sees every point with room to spare. Only matches in play run at
that cadence -- a full day card of matches yet to start costs one poll a minute
each.

The statistics are the cheapest table by row count and the widest by column
count: about one row every ten seconds per live match, so a thousand or so over
a three-set match against the tens of thousands its books produce, at 75
columns each -- most of them NULL at a tournament without ball tracking. Six
live matches take the tick from ~0.05 s to ~0.35 s; the budget is five seconds.

## How to run it

```bash
uv run polymarket discover              # what would be captured, with state
uv run polymarket discover --live-only  # exactly what `run` would capture
uv run polymarket run                   # start capturing; Ctrl-C to stop
uv run polymarket dashboard             # http://127.0.0.1:8787
uv run polymarket stats                 # summarise the database
uv run polymarket sql "SELECT ..."      # any query, read-only
uv run polymarket clean-scores          # repair pre-ratchet score history
uv run polymarket backfill-book-ts      # reconstruct book_ts for older rows
```

`dashboard`, `stats` and `sql` open the file read-only and are safe to run beside
a live capture. Docker: `docker compose up -d`, logs with
`docker compose logs -f capture`, one-offs with `docker compose run --rm capture
discover`.

## How to verify a change

```bash
uv run python tests/test_offline.py     # ~455 checks, no network
```

`tests/test_offline.py` is a **plain script, not pytest** — a flat list of
`test_*()` functions called from `__main__`, each assertion through
`check(name, condition)` which raises on the first failure. Fakes (`FakeAPI`,
`FakeFlashscore`, `_board`, `_event`) build payloads by hand; nothing touches the
network and the store tests use `tempfile`. Add checks to the matching function
and call any new function from the `__main__` block.

Front end: `cd frontend && npm run build` typechecks (`tsc --noEmit`) and then
builds. TypeScript is strict, with `noUncheckedIndexedAccess` and
`noUnusedLocals` on — indexing an array yields `T | undefined` and you must
handle it.

There is **no CI, no linter config, no formatter config** in the repo (a
`.ruff_cache/` exists from ad-hoc runs; ruff is not a declared dependency). The
offline suite and the typecheck are the whole gate.

## Working on the front end

```bash
cd frontend
npm install                                  # once
npm run dev                                  # :5173, proxies /api to :8787
uv run polymarket dashboard --no-open        # in another terminal, serving real data
npm run build                                # writes into the Python package
```

The build output under `src/polymarket/dashboard/static/` **is committed**.
A front-end change is not delivered until you have run `npm run build` and
committed the result; for Docker, also `docker compose build`. The image contains
no Node.

## Conventions

**Comments explain *why*, at length, and they are the house style.** This
codebase carries unusually dense prose comments and module docstrings, and they
are load-bearing documentation of decisions that were expensive to learn — the
edge-cache behaviour, the `CROSS JOIN`, the network-namespace trap, the
index-vs-time spacing in Lightweight Charts. Match that register. Do not strip
them, and when you change the code under one, change the comment with it. Never
leave a comment describing behaviour that no longer exists.

Prose style, in comments and in the README alike: plain declarative sentences,
`--` for an em dash in Python, no exclamation, no hedging, no "note that". State
the constraint, then the consequence.

Other conventions:

- Python: `from __future__ import annotations` everywhere; modern generics
  (`list[str]`, `X | None`); frozen dataclasses for value types; `logging` with a
  module-level `log`, never `print` outside `cli.py`.
- Constants live in `config.py`, not inline. Flags in `cli.py` default to them.
- Derived schema: column lists are generated from `BOOK_DEPTH` — don't hand-write
  a depth column anywhere.
- Failures degrade, they don't raise: a day of the score card failing is a
  warning and a partial board; *all* days failing raises, because an empty board
  is a real answer and the caller must be able to tell the two apart.
- Commit messages are a subject line in the imperative plus a paragraph of *why*,
  in the same register as the comments. Recent examples: "Serve the dashboard
  over a Cloudflare tunnel", "One poll interval for books and scores, defaulting
  to 5s". End with the `Co-Authored-By` trailer.

## Things that will bite you

- **Do not parallelise the Flashscore reads.** One connection, sequential, on
  purpose. Concurrency lands reads on different edge caches (the score appears to
  rewind) and got a burst reset from the host. See `Flashscore.__init__`.
- **Never write a score without the `Ratchet`.** It removes ~47% of raw recorded
  score changes, all spurious. The statistics feed comes off the same caches and
  needs `StatRatchet` for the same reason; it measures points played, the one
  number in that feed that cannot fall, and rejects every lower reading rather
  than treating persistence as a correction. Its floor is restored from the
  latest stored row when a process starts or refreshes.
- **The statistics feed's own arithmetic can be wrong mid-match.** `800% (8/1)`
  for second-serve points won, observed live, with every other row on the same
  read sound. The parser is faithful and stores it as 8 out of 1: a capture that
  silently corrects its source cannot be checked against it, and `set_stats` an
  hour later is the correction. Do not add a sanity clamp.
- **The statistics feed answers with a full set of zeros before a match starts.**
  That is a shape, not an absence, so `_stat_targets` reads matches in play only.
  Deduplicating it would not help -- the zeros are a genuine "change" from
  nothing, and every match would get a row of noughts in front of it.
- **A statistic is only ever stored as its numbers, never its percentage.**
  `75% (48/64)` is `48` and `64`; the percentage is their quotient. NULL means
  the tournament does not measure it (serve speed and distance covered need ball
  tracking) and is not a zero.
- **`market_last_trade` is per match, not per player.** Tommy Paul's row can read
  0.19 while his own mid is 0.81. Use it per match; the dashboard's orientation
  of it is an inference and is labelled as one.
- **NULL means "no quote"**, which is real information about a decided market.
  Never fill it with a number at any layer.
- **The Flashscore feed is undocumented.** `FLASHSCORE_SIGN` (`x-fsign`) is a
  constant lifted from the site and the keys are single letters. If scores start
  coming back empty, check `_STATUS` and `_SET_KEYS` in `scores.py` first — that
  is the expected failure mode, not a bug in the pairing.
- **Both calendars shift each year.** A tournament missing from
  `config.ATP_TOURNAMENTS` / `config.WTA_TOURNAMENTS` means its matches silently
  never appear. `discover` is how you check. The two lists are separate on
  purpose: a combined event's name is on both, at a different tier each side, and
  `match_tournament(name, tour)` will not look one up without being told which.
- **The committed frontend bundle can drift from `frontend/src`.** Nothing
  enforces it.
- **`.env` holds the Cloudflare tunnel token and is gitignored.** That token *is*
  the tunnel — anyone holding it can serve traffic as that hostname. It goes in
  the environment, never on a command line. `.env.example` documents the shape.
- **The tunnel origin is `dashboard:8787`, not `localhost:8787`**, and
  `network_mode: "service:dashboard"` is a trap that 502s after every rebuild.
- **Polymarket PAUSES TRADING during a CLOB incident, and the API keeps serving
  the last book.** Every request still returns 200 in milliseconds; the book
  behind it simply stops changing, because the market it describes is halted.
  That is also why `book_ts` freezes at a different moment for each token -- it
  is each market's last trade before the halt, not the halt itself. Their stated
  root cause is database replica lag (an internal DB issue they took to AWS). The tick log cannot show it -- "0 rows, all unchanged" is equally what
  a calm market looks like -- and their status page has run hours behind, and
  uptime monitors call the CLOB healthy throughout because the endpoint answers.
  `book_ts` (the book's own timestamp) is the only local signal, and the capture
  warns on it; see `Poller._check_stale`. This has cost real data: clean through
  2026-08-25, then from 2026-08-30 onwards a recurring daily failure that froze
  live books for 25, 40, 95, 155 and 72 minutes at a stretch on 08-30 to 09-03,
  roughly 58-61% of live capture time on the worst days. Polymarket published no
  root cause and is rebuilding the CLOB in Rust, so expect it to recur. When
  reading price against play, filter on `ts - book_ts` first.
- **DNS**: several ISP resolvers NXDOMAIN `polymarket.com`. Handled automatically
  by `resolver.py`; `DNS.md` is the user-facing writeup. A `cannot reach the
  Polymarket API` error is almost always this.

## Out of scope / deliberately absent

- **Trading, orders, wallets, auth** — never add these without being asked.
- **Doubles, Challengers, WTA 125s, ITF, juniors, qualifying** (qualifying is
  behind an opt-in flag). The three gates in `discovery.py` enforce this. ATP and
  WTA tour level are both **in** scope, since 2026-08-24.
- **`flashscore-scraper/`** is a standalone package with its own README and CLI.
  Nothing in `src/polymarket/` imports it; `scores.py` is the trimmed, capture-
  oriented reimplementation. Changing one does **not** change the other — decide
  deliberately which you mean, and prefer `scores.py` for anything the capture
  uses.
- **`.claude/worktrees/react-lightweight-charts/`** is a leftover git worktree
  from the dashboard rewrite. Ignore it; it is not part of the build.
- **No database migrations framework, no ORM, no test framework, no CI.** That is
  the intended weight of the project. Adding one is a decision, not a cleanup.

## Documentation map

| File | Audience |
|---|---|
| `README.md` | the user: commands, flags, schema, example queries, Docker, the score-feed writeup. **Very thorough — it is the primary user doc and a change that alters behaviour should update it.** |
| `DNS.md` | the user, when the API cannot be reached |
| `flashscore-scraper/README.md` | that standalone package only |
| `agents/architecture.md` | an agent about to modify the code |
| `agents/project-context.md` | this file |

When you change behaviour, the README is usually the doc that must move with it;
these two `agents/` files should be updated when the *structure* or the
*constraints* change, not for every feature.
