# flashscore-scraper

Live tennis scores from [flashscore.com](https://www.flashscore.com/tennis/).

The web page renders client-side, so scraping the HTML gets you nothing. It
pulls its data from feeds on `local-global.flashscore.ninja`, and that is what
this reads. They are flat `KEY÷VALUE` streams rather than JSON;
`flashscore/feed.py` documents the keys and parses them into dataclasses.

## Staying live

The day feed listing every match sits behind an edge cache that hands out
copies up to a few minutes old — it sends `no-store` and then answers with
`age: 169` anyway. Read on its own it will show you a game or two behind what
the browser shows, which is no good for anything in play.

So matches in play are topped up from the per-match feeds, `dc_2_<id>` and
`df_sur_2_<id>`, which come back uncached. That is two small requests per live
match, run concurrently — with 14 matches on court the whole refresh adds about
0.3s. `--no-refresh` turns it off if you only want the one request and don't
care about lag.

## Use

```bash
python -m flashscore.cli              # live matches, printed once
python -m flashscore.cli --watch 15   # refresh in place every 15 seconds
python -m flashscore.cli --all        # every match today, not just live
python -m flashscore.cli --day 1 --all  # tomorrow's schedule (nothing is live yet)
python -m flashscore.cli --json       # machine-readable
```

```python
from flashscore import live_tennis

for match in live_tennis():
    print(match.tournament, match)
    print(match.url)
```

Output looks like:

```
WTA - SINGLES: Cincinnati (USA), hard
  Set 2      Frech M.*  0-1  Rybakina E.   4-6 1-2   0-0
  Set 1     Cirstea S.  0-0  Kalinskaya A.*  4-3     0-0

ATP - DOUBLES: Cincinnati (USA), hard
  Set 2  Bolelli S./Vavassori A.*  1-0  Rinderknech A./Vacherot V.  7-6(10) 0-3  40-0
```

`*` marks the player serving. `7-6(10)` is a set won on a tiebreak, with the
loser's tiebreak points in brackets. The last column is the game in progress —
during a tiebreak (status `Set 2 TB`) it holds the running tiebreak points.

## What you get

`Match` carries the tournament, both players (name, slug, country, ranking,
who's serving), status, UTC start time, sets won, the games and tiebreak in
every set, the points in the current game, and the winner once it's over.

Only `httpx` is needed, which the parent project already depends on.

## Caveats

This is an undocumented feed. The `x-fsign` header value is a constant lifted
from the site and the field names are single letters with no guarantee they
stay put — if scores start coming back empty, re-check the key mapping in
`feed.py` first.

The whole day's card comes back in one ~300 KB response and live matches are
filtered client-side, so poll at a sane interval rather than every second. The
per-match refresh means a poll costs one big request plus two small ones per
live match.

Field mapping was verified against a full day of results: for all 65 finished
matches, the recorded winner agreed with the sets won, and the per-set games
agreed with the set totals.
