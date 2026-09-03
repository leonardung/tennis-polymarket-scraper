"""Static configuration: the ATP and WTA calendar whitelists, and defaults."""

from __future__ import annotations

import re
from dataclasses import dataclass

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

TENNIS_TAG_ID = "864"  # Gamma tag slug "tennis"; resolved at runtime, this is the fallback

# Two cadences, chosen per match rather than per feed: a match in play is read
# every POLL_INTERVAL, everything else every IDLE_INTERVAL, and a match that has
# finished is not read at all. The books and the score of one match are still
# read in the same pass on the ticks it is read, so a price and the point it
# moved on keep sharing a timestamp.
#
# The split exists because a point turns over about every 26 seconds: that is
# what the five-second tick is for, and a match that has not started has nothing
# to say that often. Polling a whole day's card at five seconds is most of the
# request volume for none of the data.
POLL_INTERVAL = 5.0  # seconds between polls of a match in play
IDLE_INTERVAL = 60.0  # seconds between polls of a match that is not in play
REFRESH_INTERVAL = 300.0  # seconds between market-list refreshes
HEARTBEAT = 300.0  # force a snapshot this often even if the book has not moved

# Polymarket's CLOB serves a stale book during an outage rather than failing:
# every request still returns 200 in milliseconds, the book behind it just stops
# moving. Row counts cannot catch that -- "0 rows, all unchanged" is also what a
# calm market looks like -- so the book's own timestamp is what gets watched.
# One quiet market means nothing; every book stale at once is the outage.
STALE_AFTER = 120.0  # warn once even the freshest book is this far behind
STALE_WARN_EVERY = 60.0  # seconds between repeats while it stays that way
START_GRACE = 20.0  # wait this long after a scheduled start before re-checking
OVERDUE_RECHECK = 60.0  # re-check this often while a match is past its start time
OVERDUE_WINDOW = 6 * 3600.0  # stop expecting a match this long after its start time
BOOK_DEPTH = 10  # levels captured per side
BOOKS_CHUNK = 50  # token_ids per batched /books request

# Scores come from Flashscore, not from Polymarket -- see scores.py for why.
# They are read with the books, on the same cadence as the match they belong to,
# rather than with the market-list refresh: a service game lasts a couple of
# minutes, so refresh-rate sampling (5 minutes) misses whole games.
SCORE_LEAD = 900.0  # also poll matches starting within this many seconds
SCORE_TIMEOUT = 6.0  # per-match read; short enough that a stall cannot eat a tick
# Flashscore answers from whichever edge cache takes the request and they do not
# all hold the same copy, so a score can appear to go backwards. Readings that
# regress are dropped -- but not forever, or a genuine correction by the scorer
# could never land. This many consecutive reads of the lower value takes it.
SCORE_PATIENCE = 5
MIN_REFRESH_GAP = 60.0  # floor between refreshes triggered by a state change

FLASHSCORE_HOST = "https://local-global.flashscore.ninja"
FLASHSCORE_SIGN = "SW9D1eZo"  # static signature the site sends on every feed request
FLASHSCORE_TZ = 1  # only shifts where the day boundary falls; times are always UTC
FLASHSCORE_DAYS = (-1, 0, 1)  # day cards to read, relative to today


# Match statistics come off a third Flashscore feed, `df_st_2_<id>`, and are read
# on the tick with the book and the score. One read carries every period at once
# -- the running match totals and each set so far -- and the counters move on
# almost every point, so the overall block is the one recorded live and only
# where it changed, exactly as a book snapshot is.
STATS_TIMEOUT = 6.0  # per-match read; same reasoning as SCORE_TIMEOUT
# A floor between statistics reads of one match, on top of the tick cadence.
# The score is read every tick because a point can be scored at any moment and
# the book moves with it; the statistics describe the point that was just
# played, and a point takes about 26 seconds. At --interval=2 an unfloored read
# asks thirteen times per point, and those requests are sequential on one
# connection with the score reads -- at a slam, with sixteen matches on court,
# that is what pushes a tick past its own budget and starts costing book
# snapshots. Five seconds still sees every point five times over. Raising
# --interval above this makes it a no-op.
STATS_INTERVAL = 5.0
# Flashscore keeps correcting a finished match's statistics for a while after
# the last point -- an unforced error becomes a winner, the speed radar's
# numbers are revised -- so the per-set breakdown is taken once, late, rather
# than on the tick. It is the record to check the live capture against.
FINAL_STATS_DELAY = 3600.0  # wait this long after a match ends before taking them
FINAL_STATS_WINDOW = 24 * 3600.0  # and stop trying this long after it ended
FINAL_STATS_CHECK = 60.0  # seconds between looks for a match that has come due
FINAL_STATS_BATCH = 4  # matches read per look, so a backlog cannot eat a tick


@dataclass(frozen=True)
class Statistic:
    """One row of the statistics feed, and the shape of the value it carries.

    `label` is Flashscore's own wording, which is what the feed is keyed by;
    `key` is the column prefix, and it is what the database is keyed by, so the
    two can drift apart without a migration if the site renames something.

    `of` says the value arrives as a made-of-attempted pair -- "75% (48/64)" or
    "1/3" -- and gets a second column holding the denominator. The percentage
    is not stored: it is the quotient of two numbers that are, and keeping it
    as well would let a row disagree with itself.
    """

    key: str
    label: str
    group: str  # "Serve", "Return", "Points", "Games" -- the feed's own sections
    of: bool = False
    unit: str | None = None  # informational; the value is stored as the number


def _stat(
    key: str, label: str, group: str, of: bool = False, unit: str | None = None
) -> Statistic:
    return Statistic(key, label, group, of, unit)


# Every statistic the feed has been seen to carry, in the order it lists them,
# under the section heading (`SF`) the feed files it under -- which the dashboard
# reads back to lay the table out the way the site does.
# A tournament without ball tracking simply omits some -- serve speed, distance
# covered and "last 10 balls" are the ones that come and go -- and an absent
# statistic is stored as NULL rather than zero: not measured is not none.
# A label that is not on this list is dropped and logged once. The feed is
# undocumented and the site does add rows, so that log is the warning.
STATISTICS: tuple[Statistic, ...] = (
    _stat("aces", "Aces", "Serve"),
    _stat("double_faults", "Double Faults", "Serve"),
    _stat("first_serve_pct", "1st serve percentage", "Serve", unit="%"),
    _stat("first_serve_won", "1st serve points won", "Serve", of=True),
    _stat("second_serve_won", "2nd serve points won", "Serve", of=True),
    _stat("break_points_saved", "Break Points Saved", "Serve", of=True),
    _stat("first_serve_speed", "Average 1st serve speed", "Serve", unit="km/h"),
    _stat("second_serve_speed", "Average 2nd serve speed", "Serve", unit="km/h"),
    _stat("first_return_won", "1st return points won", "Return", of=True),
    _stat("second_return_won", "2nd return points won", "Return", of=True),
    _stat("break_points_converted", "Break Points Converted", "Return", of=True),
    _stat("winners", "Winners", "Points"),
    _stat("unforced_errors", "Unforced errors", "Points"),
    _stat("net_points_won", "Net points won", "Points", of=True),
    _stat("service_points_won", "Service Points Won", "Points", of=True),
    _stat("return_points_won", "Return Points Won", "Points", of=True),
    _stat("total_points_won", "Total Points Won", "Points", of=True),
    _stat("last_10_balls", "Last 10 balls", "Points"),
    _stat("match_points_saved", "Match points saved", "Points"),
    _stat("service_games_won", "Service games won", "Games", of=True),
    _stat("return_games_won", "Return games won", "Games", of=True),
    _stat("total_games_won", "Total games won", "Games", of=True),
    _stat("distance_covered", "Distance covered (metres)", "Games", unit="m"),
)

# Keyed by the feed's own wording, folded, since that is what a block carries.
STATISTICS_BY_LABEL: dict[str, Statistic] = {s.label.lower(): s for s in STATISTICS}

# The feed names the running totals "Match" and each set "Set 1", "Set 2"...
# The first is what the live capture records; the rest are the per-set table.
STATS_OVERALL = "Match"


# Which tours are captured. Both draws of a combined event are recorded, and
# each is kept on its own calendar below -- the same week at Cincinnati is a
# Masters 1000 for one tour and a WTA 1000 for the other.
TOURS: tuple[str, ...] = ("atp", "wta")


@dataclass(frozen=True)
class Tournament:
    name: str
    tier: str
    tour: str
    patterns: tuple[str, ...]


def _atp(name: str, tier: str, *patterns: str) -> Tournament:
    return Tournament(name, tier, "atp", patterns or (name.lower(),))


def _wta(name: str, tier: str, *patterns: str) -> Tournament:
    return Tournament(name, tier, "wta", patterns or (name.lower(),))


# ATP tour-level calendar. Every entry is ATP 250 or above; tier is informational
# only, since the filter is really "is this an ATP tour event". Combined events
# (Indian Wells, Rome, the slams...) also host WTA draws, so matching a tournament
# here does NOT by itself mean the match is ATP -- the slug's tour prefix decides
# that, and it is what picks which of the two calendars is searched.
ATP_TOURNAMENTS: tuple[Tournament, ...] = (
    # --- Grand Slams ---
    _atp("Australian Open", "grand_slam", "australian open", "aus open", r"\bao\b"),
    _atp("Roland Garros", "grand_slam", "roland garros", "french open"),
    _atp("Wimbledon", "grand_slam", "wimbledon"),
    _atp("US Open", "grand_slam", r"\bus open\b", "usopen"),
    # --- Season finale ---
    _atp("ATP Finals", "finals", "atp finals", "nitto atp finals", "tour finals"),
    # --- Masters 1000 ---
    _atp("Indian Wells", "masters", "indian wells", "bnp paribas open"),
    _atp("Miami Open", "masters", "miami open", "miami masters"),
    _atp("Monte-Carlo", "masters", "monte.?carlo", "monaco masters"),
    _atp("Madrid Open", "masters", "madrid"),
    _atp("Italian Open", "masters", "italian open", "rome masters", r"\brome\b", "internazionali"),
    _atp("Canadian Open", "masters", "canadian open", "national bank open", "toronto", "montreal"),
    _atp("Cincinnati Open", "masters", "cincinnati", "western.{0,3}southern"),
    _atp("Shanghai Masters", "masters", "shanghai"),
    _atp("Paris Masters", "masters", "paris masters", "rolex paris", "paris bercy"),
    # --- ATP 500 ---
    _atp("Rotterdam", "atp_500", "rotterdam", "abn amro"),
    _atp("Qatar Open", "atp_500", "qatar open", "doha"),
    _atp("Dubai", "atp_500", "dubai"),
    _atp("Mexican Open", "atp_500", "mexican open", "acapulco"),
    _atp("Rio Open", "atp_500", "rio open", "rio de janeiro"),
    _atp("Barcelona Open", "atp_500", "barcelona", "godo"),
    _atp("BMW Open", "atp_500", "bmw open", "munich"),
    _atp("Hamburg Open", "atp_500", "hamburg"),
    _atp("Halle", "atp_500", "halle", "terra wortmann"),
    _atp("Queen's Club", "atp_500", "queen.?s club", r"\bqueens\b", "cinch championships"),
    _atp("Washington", "atp_500", "washington", "citi open", "dc open"),
    _atp("China Open", "atp_500", "china open", "beijing"),
    _atp("Japan Open", "atp_500", "japan open", "tokyo"),
    _atp("Vienna", "atp_500", "vienna", "erste bank"),
    _atp("Basel", "atp_500", "basel", "swiss indoors"),
    # --- ATP 250 ---
    _atp("Brisbane", "atp_250", "brisbane"),
    _atp("Adelaide", "atp_250", "adelaide"),
    _atp("Auckland", "atp_250", "auckland", "asb classic"),
    _atp("Hong Kong", "atp_250", "hong kong"),
    _atp("Montpellier", "atp_250", "montpellier"),
    _atp("Dallas", "atp_250", "dallas"),
    _atp("Delray Beach", "atp_250", "delray"),
    _atp("Marseille", "atp_250", "marseille", "open 13"),
    _atp("Argentina Open", "atp_250", "argentina open", "buenos aires"),
    _atp("Chile Open", "atp_250", "chile open", "santiago"),
    _atp("Cordoba", "atp_250", "cordoba", "córdoba"),
    _atp("Houston", "atp_250", "houston"),
    _atp("Marrakech", "atp_250", "marrakech", "hassan ii"),
    _atp("Bucharest", "atp_250", "bucharest"),
    _atp("Estoril", "atp_250", "estoril"),
    _atp("Geneva", "atp_250", "geneva"),
    _atp("Lyon", "atp_250", "lyon"),
    _atp("Stuttgart", "atp_250", "stuttgart", "boss open"),
    _atp("s-Hertogenbosch", "atp_250", "hertogenbosch", "libema", "rosmalen"),
    _atp("Mallorca", "atp_250", "mallorca"),
    _atp("Eastbourne", "atp_250", "eastbourne"),
    _atp("Newport", "atp_250", "newport", "hall of fame"),
    _atp("Bastad", "atp_250", "bastad", "båstad", "nordea"),
    _atp("Gstaad", "atp_250", "gstaad", "swiss open"),
    _atp("Umag", "atp_250", "umag", "croatia open"),
    _atp("Kitzbuhel", "atp_250", "kitzbuhel", "kitzbühel"),
    _atp("Atlanta", "atp_250", "atlanta"),
    _atp("Los Cabos", "atp_250", "los cabos", "mifel"),
    _atp("Winston-Salem", "atp_250", "winston.?salem"),
    _atp("Chengdu", "atp_250", "chengdu"),
    _atp("Hangzhou", "atp_250", "hangzhou"),
    _atp("Almaty", "atp_250", "almaty"),
    _atp("Stockholm", "atp_250", "stockholm"),
    _atp("Antwerp", "atp_250", "antwerp", "european open"),
    _atp("Brussels", "atp_250", "brussels"),
    _atp("Metz", "atp_250", "metz", "moselle"),
    _atp("Belgrade", "atp_250", "belgrade"),
    _atp("Sofia", "atp_250", "sofia"),
)

# WTA tour-level calendar, on the same terms: WTA 250 or above, which is what
# drops the 125s and the ITF circuit. A separate list rather than a tier on the
# entries above, because the two tours share a great many names and agree on
# nothing else -- Stuttgart is an ATP 250 and a WTA 500, Tokyo is two different
# tournaments in two different weeks.
WTA_TOURNAMENTS: tuple[Tournament, ...] = (
    # --- Grand Slams ---
    _wta("Australian Open", "grand_slam", "australian open", "aus open", r"\bao\b"),
    _wta("Roland Garros", "grand_slam", "roland garros", "french open"),
    _wta("Wimbledon", "grand_slam", "wimbledon"),
    _wta("US Open", "grand_slam", r"\bus open\b", "usopen"),
    # --- Season finale ---
    _wta("WTA Finals", "finals", "wta finals", "tour finals"),
    # --- WTA 1000 ---
    _wta("Qatar Open", "wta_1000", "qatar open", "doha"),
    _wta("Dubai", "wta_1000", "dubai"),
    _wta("Indian Wells", "wta_1000", "indian wells", "bnp paribas open"),
    _wta("Miami Open", "wta_1000", "miami open", "miami masters"),
    _wta("Madrid Open", "wta_1000", "madrid"),
    _wta("Italian Open", "wta_1000", "italian open", r"\brome\b", "internazionali"),
    _wta("Canadian Open", "wta_1000", "canadian open", "national bank open", "toronto", "montreal"),
    _wta("Cincinnati Open", "wta_1000", "cincinnati", "western.{0,3}southern"),
    _wta("China Open", "wta_1000", "china open", "beijing"),
    _wta("Wuhan Open", "wta_1000", "wuhan"),
    # --- WTA 500 ---
    _wta("Brisbane", "wta_500", "brisbane"),
    _wta("Adelaide", "wta_500", "adelaide"),
    _wta("Linz", "wta_500", "linz", "upper austria"),
    _wta("Abu Dhabi", "wta_500", "abu dhabi", "mubadala"),
    _wta("Monterrey", "wta_500", "monterrey", "mexican open"),
    _wta("Merida", "wta_500", "merida", "mérida"),
    _wta("Charleston", "wta_500", "charleston", "credit one"),
    _wta("Stuttgart", "wta_500", "stuttgart", "porsche"),
    _wta("Strasbourg", "wta_500", "strasbourg"),
    _wta("Berlin", "wta_500", "berlin", "ecotrans"),
    _wta("Bad Homburg", "wta_500", "bad homburg"),
    _wta("Eastbourne", "wta_500", "eastbourne"),
    _wta("Washington", "wta_500", "washington", "citi open", "dc open"),
    _wta("Guadalajara", "wta_500", "guadalajara", "akron"),
    _wta("Seoul", "wta_500", "seoul", "korea open"),
    _wta("Tokyo", "wta_500", "toray", "pan pacific", "tokyo"),
    _wta("Ningbo", "wta_500", "ningbo"),
    _wta("Zhengzhou", "wta_500", "zhengzhou"),
    _wta("San Diego", "wta_500", "san diego"),
    # --- WTA 250 ---
    _wta("Auckland", "wta_250", "auckland", "asb classic"),
    _wta("Hobart", "wta_250", "hobart"),
    _wta("Cluj-Napoca", "wta_250", "cluj"),
    _wta("Singapore", "wta_250", "singapore"),
    _wta("Austin", "wta_250", "austin", r"\batx\b"),
    _wta("Bogota", "wta_250", "bogota", "bogotá"),
    _wta("Sao Paulo", "wta_250", "sao paulo", "são paulo", "sp open"),
    _wta("Rouen", "wta_250", "rouen"),
    _wta("Rabat", "wta_250", "rabat", "morocco"),
    _wta("Nottingham", "wta_250", "nottingham"),
    _wta("Birmingham", "wta_250", "birmingham"),
    _wta("s-Hertogenbosch", "wta_250", "hertogenbosch", "libema", "rosmalen"),
    _wta("Lausanne", "wta_250", "lausanne"),
    _wta("Hamburg", "wta_250", "hamburg"),
    _wta("Budapest", "wta_250", "budapest"),
    _wta("Iasi", "wta_250", "iasi", "iaşi", "iași"),
    _wta("Prague", "wta_250", "prague"),
    _wta("Warsaw", "wta_250", "warsaw"),
    _wta("Palermo", "wta_250", "palermo"),
    _wta("Cleveland", "wta_250", "cleveland", "tennis in the land"),
    _wta("Monastir", "wta_250", "monastir", "jasmin open"),
    _wta("Guangzhou", "wta_250", "guangzhou"),
    _wta("Hua Hin", "wta_250", "hua hin", "thailand open"),
    _wta("Jiangxi", "wta_250", "jiangxi", "nanchang"),
    _wta("Osaka", "wta_250", "osaka"),
    _wta("Hong Kong", "wta_250", "hong kong"),
    _wta("Chennai", "wta_250", "chennai"),
)

# Every tour-level event on either calendar. Nothing filters on this -- it is
# the whole whitelist, for anything that wants to report on it.
TOURNAMENTS: tuple[Tournament, ...] = ATP_TOURNAMENTS + WTA_TOURNAMENTS

# Compiled once, per tour: a name is only ever looked up on the calendar of the
# tour that asked about it.
_COMPILED: dict[str, tuple[tuple[Tournament, tuple[re.Pattern[str], ...]], ...]] = {
    tour: tuple(
        (t, tuple(re.compile(p, re.I) for p in t.patterns))
        for t in TOURNAMENTS
        if t.tour == tour
    )
    for tour in TOURS
}

# Polymarket slugs every head-to-head as "<tour>-[doubles-]<p1>-<p2>-<YYYY-MM-DD>",
# e.g. "atp-norrie-navone-2026-05-20" or "wta-doubles-alexgib-chanjoi-2026-07-28".
# The tour prefix is authoritative and is the ONLY reliable ATP/WTA signal: match
# events carry generic tags (tennis/sports/games), never atp/wta ones.
MATCH_SLUG = re.compile(
    r"^(?P<tour>atp|wta|itf)-(?P<doubles>doubles-)?.+-(?P<date>\d{4}-\d{2}-\d{2})$"
)

# A tour prefix names the circuit, NOT its level -- Challengers are slugged "atp"
# (Sion, Kingston, Todi...) and the 125s "wta". The calendars above are what
# enforce "250 or above" on each side.
QUALIFYING = re.compile(r"\bqualif\w*\b", re.I)

# Non-tour formats that can still appear under a tour-level tournament name.
EXCLUDE = re.compile(
    r"\b(juniors?|exhibition|utr|uts|laver cup|next ?gen|legends|wheelchair)\b", re.I
)


def match_tournament(text: str, tour: str) -> Tournament | None:
    """Return the tour-level event whose name appears in `text`, if any.

    The tour has to be given rather than inferred from the name: a combined
    event runs both draws under one title, so "Cincinnati Open" is a Masters
    1000 or a WTA 1000 depending only on which draw is asking.
    """
    if not text:
        return None
    for tournament, patterns in _COMPILED.get(tour, ()):
        if any(p.search(text) for p in patterns):
            return tournament
    return None
