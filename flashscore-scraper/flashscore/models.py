"""Data model for a Flashscore tennis match."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class Player:
    """One side of a match. For doubles the name holds both players."""

    name: str
    slug: str | None = None
    country: str | None = None
    rank: int | None = None
    serving: bool = False


@dataclass
class SetScore:
    """Games won by each side in one set, plus the tiebreak if there was one."""

    home: int
    away: int
    home_tiebreak: int | None = None
    away_tiebreak: int | None = None

    def __str__(self) -> str:
        s = f"{self.home}-{self.away}"
        # Only annotate a decided set: at 6-6 the tiebreak is still being played
        # and its running score belongs in the points column, not here.
        if (
            self.home_tiebreak is not None
            and self.away_tiebreak is not None
            and self.home != self.away
        ):
            s += f"({min(self.home_tiebreak, self.away_tiebreak)})"
        return s


@dataclass
class Match:
    id: str
    tournament: str
    home: Player
    away: Player
    status: str
    starts_at: datetime
    home_sets: int | None = None
    away_sets: int | None = None
    sets: list[SetScore] = field(default_factory=list)
    # Points in the game currently being played: "0", "15", "30", "40", "A".
    home_point: str | None = None
    away_point: str | None = None
    winner: int | None = None  # 1 = home, 2 = away
    is_live: bool = False

    @property
    def url(self) -> str:
        return f"https://www.flashscore.com/match/tennis/{self.id}/"

    def score_line(self) -> str:
        """Set scores as a single string, e.g. '6-4 3-6 2-1'."""
        return " ".join(str(s) for s in self.sets)

    def point_line(self) -> str:
        """Current game score, e.g. '40-30'."""
        if self.home_point is None and self.away_point is None:
            return ""
        return f"{self.home_point or '0'}-{self.away_point or '0'}"

    def __str__(self) -> str:
        marks = ("*" if self.home.serving else " ", "*" if self.away.serving else " ")
        head = f"{self.home.name}{marks[0]} vs {self.away.name}{marks[1]}"
        sets = f"{self.home_sets}-{self.away_sets}" if self.home_sets is not None else ""
        parts = [head, self.status, sets, self.score_line(), self.point_line()]
        return "  ".join(p for p in parts if p)


def to_utc(epoch: int) -> datetime:
    return datetime.fromtimestamp(epoch, tz=timezone.utc)
