"""Static configuration: ATP calendar whitelist and defaults."""

from __future__ import annotations

import re
from dataclasses import dataclass

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

TENNIS_TAG_ID = "864"  # Gamma tag slug "tennis"; resolved at runtime, this is the fallback

POLL_INTERVAL = 10.0  # seconds between order-book snapshots
REFRESH_INTERVAL = 300.0  # seconds between market-list refreshes
HEARTBEAT = 300.0  # force a snapshot this often even if the book has not moved
START_GRACE = 20.0  # wait this long after a scheduled start before re-checking
OVERDUE_RECHECK = 60.0  # re-check this often while a match is past its start time
OVERDUE_WINDOW = 6 * 3600.0  # stop expecting a match this long after its start time
BOOK_DEPTH = 3  # levels captured per side
BOOKS_CHUNK = 50  # token_ids per batched /books request

# The score feed is read on its own cadence, alongside the books rather than
# with the market-list refresh. A service game lasts a couple of minutes, so
# refresh-rate sampling (5 minutes) can miss whole games; this resolves them.
SCORE_INTERVAL = 10.0  # seconds between score-feed polls; 0 disables
SCORE_CHUNK = 20  # event slugs per batched /events request
# Each event comes back with its full market list attached and no way to ask for
# less, so the poll is aimed at the matches whose score can actually change:
# those in play, and those close enough to their slot to start at any moment.
SCORE_LEAD = 900.0  # also poll matches starting within this many seconds
MIN_REFRESH_GAP = 60.0  # floor between refreshes triggered by a state change


@dataclass(frozen=True)
class Tournament:
    name: str
    tier: str
    patterns: tuple[str, ...]


def _t(name: str, tier: str, *patterns: str) -> Tournament:
    return Tournament(name, tier, patterns or (name.lower(),))


# ATP tour-level calendar. Every entry is ATP 250 or above; tier is informational
# only, since the filter is really "is this an ATP tour event". Combined events
# (Indian Wells, Rome, the slams...) also host WTA draws, so matching a tournament
# here does NOT by itself mean the match is ATP -- see discovery.classify_tour.
TOURNAMENTS: tuple[Tournament, ...] = (
    # --- Grand Slams ---
    _t("Australian Open", "grand_slam", "australian open", "aus open", r"\bao\b"),
    _t("Roland Garros", "grand_slam", "roland garros", "french open"),
    _t("Wimbledon", "grand_slam", "wimbledon"),
    _t("US Open", "grand_slam", r"\bus open\b", "usopen"),
    # --- Season finale ---
    _t("ATP Finals", "finals", "atp finals", "nitto atp finals", "tour finals"),
    # --- Masters 1000 ---
    _t("Indian Wells", "masters", "indian wells", "bnp paribas open"),
    _t("Miami Open", "masters", "miami open", "miami masters"),
    _t("Monte-Carlo", "masters", "monte.?carlo", "monaco masters"),
    _t("Madrid Open", "masters", "madrid"),
    _t("Italian Open", "masters", "italian open", "rome masters", r"\brome\b", "internazionali"),
    _t("Canadian Open", "masters", "canadian open", "national bank open", "toronto", "montreal"),
    _t("Cincinnati Open", "masters", "cincinnati", "western.{0,3}southern"),
    _t("Shanghai Masters", "masters", "shanghai"),
    _t("Paris Masters", "masters", "paris masters", "rolex paris", "paris bercy"),
    # --- ATP 500 ---
    _t("Rotterdam", "atp_500", "rotterdam", "abn amro"),
    _t("Qatar Open", "atp_500", "qatar open", "doha"),
    _t("Dubai", "atp_500", "dubai"),
    _t("Mexican Open", "atp_500", "mexican open", "acapulco"),
    _t("Rio Open", "atp_500", "rio open", "rio de janeiro"),
    _t("Barcelona Open", "atp_500", "barcelona", "godo"),
    _t("BMW Open", "atp_500", "bmw open", "munich"),
    _t("Hamburg Open", "atp_500", "hamburg"),
    _t("Halle", "atp_500", "halle", "terra wortmann"),
    _t("Queen's Club", "atp_500", "queen.?s club", r"\bqueens\b", "cinch championships"),
    _t("Washington", "atp_500", "washington", "citi open", "dc open"),
    _t("China Open", "atp_500", "china open", "beijing"),
    _t("Japan Open", "atp_500", "japan open", "tokyo"),
    _t("Vienna", "atp_500", "vienna", "erste bank"),
    _t("Basel", "atp_500", "basel", "swiss indoors"),
    # --- ATP 250 ---
    _t("Brisbane", "atp_250", "brisbane"),
    _t("Adelaide", "atp_250", "adelaide"),
    _t("Auckland", "atp_250", "auckland", "asb classic"),
    _t("Hong Kong", "atp_250", "hong kong"),
    _t("Montpellier", "atp_250", "montpellier"),
    _t("Dallas", "atp_250", "dallas"),
    _t("Delray Beach", "atp_250", "delray"),
    _t("Marseille", "atp_250", "marseille", "open 13"),
    _t("Argentina Open", "atp_250", "argentina open", "buenos aires"),
    _t("Chile Open", "atp_250", "chile open", "santiago"),
    _t("Cordoba", "atp_250", "cordoba", "córdoba"),
    _t("Houston", "atp_250", "houston"),
    _t("Marrakech", "atp_250", "marrakech", "hassan ii"),
    _t("Bucharest", "atp_250", "bucharest"),
    _t("Estoril", "atp_250", "estoril"),
    _t("Geneva", "atp_250", "geneva"),
    _t("Lyon", "atp_250", "lyon"),
    _t("Stuttgart", "atp_250", "stuttgart", "boss open"),
    _t("s-Hertogenbosch", "atp_250", "hertogenbosch", "libema", "rosmalen"),
    _t("Mallorca", "atp_250", "mallorca"),
    _t("Eastbourne", "atp_250", "eastbourne"),
    _t("Newport", "atp_250", "newport", "hall of fame"),
    _t("Bastad", "atp_250", "bastad", "båstad", "nordea"),
    _t("Gstaad", "atp_250", "gstaad", "swiss open"),
    _t("Umag", "atp_250", "umag", "croatia open"),
    _t("Kitzbuhel", "atp_250", "kitzbuhel", "kitzbühel"),
    _t("Atlanta", "atp_250", "atlanta"),
    _t("Los Cabos", "atp_250", "los cabos", "mifel"),
    _t("Winston-Salem", "atp_250", "winston.?salem"),
    _t("Chengdu", "atp_250", "chengdu"),
    _t("Hangzhou", "atp_250", "hangzhou"),
    _t("Almaty", "atp_250", "almaty"),
    _t("Stockholm", "atp_250", "stockholm"),
    _t("Antwerp", "atp_250", "antwerp", "european open"),
    _t("Brussels", "atp_250", "brussels"),
    _t("Metz", "atp_250", "metz", "moselle"),
    _t("Belgrade", "atp_250", "belgrade"),
    _t("Sofia", "atp_250", "sofia"),
)

_COMPILED = tuple(
    (t, tuple(re.compile(p, re.I) for p in t.patterns)) for t in TOURNAMENTS
)

# Polymarket slugs every head-to-head as "<tour>-[doubles-]<p1>-<p2>-<YYYY-MM-DD>",
# e.g. "atp-norrie-navone-2026-05-20" or "wta-doubles-alexgib-chanjoi-2026-07-28".
# The tour prefix is authoritative and is the ONLY reliable ATP/WTA signal: match
# events carry generic tags (tennis/sports/games), never atp/wta ones.
MATCH_SLUG = re.compile(
    r"^(?P<tour>atp|wta|itf)-(?P<doubles>doubles-)?.+-(?P<date>\d{4}-\d{2}-\d{2})$"
)

# The "atp" prefix means men's professional, NOT tour level -- Challengers use it
# too (Sion, Kingston, Todi...). TOURNAMENTS above is what enforces "250 or above".
QUALIFYING = re.compile(r"\bqualif\w*\b", re.I)

# Non-tour formats that can still appear under an ATP-level tournament name.
EXCLUDE = re.compile(
    r"\b(juniors?|exhibition|utr|uts|laver cup|next ?gen|legends|wheelchair)\b", re.I
)


def match_tournament(text: str) -> Tournament | None:
    """Return the ATP tour event whose name appears in `text`, if any."""
    if not text:
        return None
    for tournament, patterns in _COMPILED:
        if any(p.search(text) for p in patterns):
            return tournament
    return None
