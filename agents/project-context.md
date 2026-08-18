# Project context

What this project is for, how it is worked on, and the things that are true but
not visible in the code. Companion: [architecture.md](architecture.md) (module
map, data flow, invariants).

## What it is

A research capture tool. It records the Polymarket order book for every ATP
tour-level (250 and above) men's singles match **while it is being played** —
every 5 seconds, 3 levels deep on each side — into SQLite, with the live score
read on the same tick so a price and the point it moved on carry the same
timestamp. A read-only web dashboard browses what has been recorded.

The point of the dataset is reading price against play: what does the book do
when a break point is saved, when a set turns, when a match is stopped for rain.
That is why the score is timestamped in its own table rather than only kept as a
current value, and why the book and the score share one poll cadence.

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

A Masters has a handful of matches in play at once. Expect tens of thousands of
rows a day; a full season fits comfortably inside a gigabyte. The current
`data/tennis.db` is ~25 MB. A score read is ~200 bytes per live match per tick;
the day card is ~750 KB every 5 minutes. Points turn over about every 26 seconds,
so the 5-second tick sees every point with room to spare.

## How to run it

```bash
uv run polymarket discover              # what would be captured, with state
uv run polymarket discover --live-only  # exactly what `run` would capture
uv run polymarket run                   # start capturing; Ctrl-C to stop
uv run polymarket dashboard             # http://127.0.0.1:8787
uv run polymarket stats                 # summarise the database
uv run polymarket sql "SELECT ..."      # any query, read-only
uv run polymarket clean-scores          # repair pre-ratchet score history
```

`dashboard`, `stats` and `sql` open the file read-only and are safe to run beside
a live capture. Docker: `docker compose up -d`, logs with
`docker compose logs -f capture`, one-offs with `docker compose run --rm capture
discover`.

## How to verify a change

```bash
uv run python tests/test_offline.py     # ~319 checks, no network
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
  score changes, all spurious.
- **`market_last_trade` is per match, not per player.** Tommy Paul's row can read
  0.19 while his own mid is 0.81. Use it per match; the dashboard's orientation
  of it is an inference and is labelled as one.
- **NULL means "no quote"**, which is real information about a decided market.
  Never fill it with a number at any layer.
- **The Flashscore feed is undocumented.** `FLASHSCORE_SIGN` (`x-fsign`) is a
  constant lifted from the site and the keys are single letters. If scores start
  coming back empty, check `_STATUS` and `_SET_KEYS` in `scores.py` first — that
  is the expected failure mode, not a bug in the pairing.
- **The ATP calendar shifts each year.** A tournament missing from
  `config.TOURNAMENTS` means its matches silently never appear. `discover` is how
  you check.
- **The committed frontend bundle can drift from `frontend/src`.** Nothing
  enforces it.
- **`.env` holds the Cloudflare tunnel token and is gitignored.** That token *is*
  the tunnel — anyone holding it can serve traffic as that hostname. It goes in
  the environment, never on a command line. `.env.example` documents the shape.
- **The tunnel origin is `dashboard:8787`, not `localhost:8787`**, and
  `network_mode: "service:dashboard"` is a trap that 502s after every rebuild.
- **DNS**: several ISP resolvers NXDOMAIN `polymarket.com`. Handled automatically
  by `resolver.py`; `DNS.md` is the user-facing writeup. A `cannot reach the
  Polymarket API` error is almost always this.

## Out of scope / deliberately absent

- **Trading, orders, wallets, auth** — never add these without being asked.
- **WTA, doubles, Challengers, ITF, juniors, qualifying** (qualifying is behind an
  opt-in flag). The three gates in `discovery.py` enforce this.
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
