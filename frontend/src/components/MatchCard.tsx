import { Sparkline } from "./Sparkline";
import {
  fmtClock,
  fmtDateTime,
  fmtEnding,
  fmtPrice,
  fmtRelative,
  fmtSigned,
  NO_QUOTE,
  shortName,
} from "../format";
import type { MatchSummary } from "../types";

/** A move smaller than this is the market standing still, not drifting. */
const MOVE_EPSILON = 0.0005;

export function MatchCard({ match, onOpen }: { match: MatchSummary; onOpen: () => void }) {
  const overdue =
    match.state === "upcoming" && match.start_epoch != null && match.start_epoch < Date.now() / 1000;

  const moved = match.move != null && Math.abs(match.move) >= MOVE_EPSILON;
  const moveClass = !moved ? "delta" : match.move! > 0 ? "delta up" : "delta down";

  const when =
    match.state === "live"
      ? fmtClock(match.last_ts)
      : match.state === "upcoming"
        ? fmtDateTime(match.start_epoch)
        : match.last_ts != null
          ? fmtDateTime(match.last_ts)
          : "—";

  return (
    <button
      type="button"
      className="card"
      onClick={onOpen}
      aria-label={`${match.question}. ${match.score ?? match.state}${
        match.state === "live" && match.game ? `, ${match.game}` : ""
      }`}
    >
      <div className="card-top">
        <span className="card-meta">{match.tournament ?? "—"}</span>
        <span className="card-when">{when}</span>
      </div>

      <div className="card-top">
        <Badge match={match} overdue={overdue} />
        {match.score && (
          <span className="card-score">
            {match.score}
            {/* The points inside the current game, which turn over several times
                a game -- only while one is being played, and only for a live
                match, where they are still the state of play rather than the
                last thing that happened. */}
            {match.state === "live" && match.game && (
              <span className="card-points">{match.game}</span>
            )}
          </span>
        )}
      </div>

      <div className="players">
        {match.players.map((name, index) => {
          const mid = match.prices[index]?.mid ?? null;
          return (
            <div className="player-row" key={index}>
              <span className={`swatch swatch-${index}`} />
              <span className="player-name">{name || `Outcome ${index}`}</span>
              <span className={mid == null ? "player-price is-null" : "player-price"}>
                {mid == null ? NO_QUOTE : fmtPrice(mid)}
              </span>
            </div>
          );
        })}
      </div>

      <div className="card-foot">
        <div>
          <Sparkline values={match.spark} />
          <span className={moveClass}>
            {moved ? `${fmtSigned(match.move)} ${shortName(match.players[0])}` : ""}
          </span>
        </div>
        <span className="card-stats">{match.snapshots.toLocaleString()} snaps</span>
      </div>
    </button>
  );
}

/** The badge earns its place by saying what the rest of the card does not. */
function Badge({ match, overdue }: { match: MatchSummary; overdue: boolean }) {
  if (match.state === "live") {
    return <span className="badge badge-live">{match.period ?? "Live"}</span>;
  }
  if (overdue) return <span className="badge badge-overdue">Overdue</span>;
  if (match.state === "upcoming") {
    return (
      <span className="badge">
        {match.start_epoch != null ? `starts ${fmtRelative(match.start_epoch)}` : "Scheduled"}
      </span>
    );
  }
  return <span className="badge">{fmtEnding(match.period)}</span>;
}
