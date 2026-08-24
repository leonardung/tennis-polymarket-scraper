import { useMemo, useState } from "react";
import { fetchMatch, fetchSeries } from "../api";
import {
  fmtDateTime,
  fmtDuration,
  fmtPrice,
  fmtRelative,
  fmtSize,
  NO_QUOTE,
  scoreLabel,
  shortName,
} from "../format";
import { useAsync } from "../hooks";
import type { ChartPalette } from "../theme";
import type { MatchDetail as Detail, ScoreEvent } from "../types";
import { Legend, Panel, Segmented, StatTile } from "./Chrome";
import { OrderBook } from "./OrderBook";
import { TableView } from "./TableView";
import { TimeSeriesChart, type ChartMarker } from "./TimeSeriesChart";

const RANGES = [
  { value: "15m", label: "15m", seconds: 900 },
  { value: "1h", label: "1h", seconds: 3600 },
  { value: "3h", label: "3h", seconds: 10800 },
  { value: "all", label: "All", seconds: null },
] as const;

type RangeKey = (typeof RANGES)[number]["value"];

export function MatchDetail({
  conditionId,
  palette,
  version,
  onBack,
}: {
  conditionId: string;
  palette: ChartPalette;
  version: number;
  onBack: () => void;
}) {
  // Null until the reader picks one, so the default can follow the match.
  const [chosenRange, setChosenRange] = useState<RangeKey | null>(null);
  const [depthOutcome, setDepthOutcome] = useState(0);
  const [showTable, setShowTable] = useState(false);
  // Bumped on every press of the range control, including a press of the range
  // already selected: that fetches nothing, so without it there would be no
  // change for the charts to notice and the press would appear to do nothing.
  const [resetNonce, setResetNonce] = useState(0);

  const pickRange = (next: RangeKey) => {
    setChosenRange(next);
    setResetNonce((n) => n + 1);
  };

  const detail = useAsync((signal) => fetchMatch(conditionId, signal), [conditionId, version]);

  // A match is usually polled for hours before it starts, and that flat
  // pre-match stretch dwarfs the play. Open a live match on the last hour and a
  // finished one on the whole thing; either way the range control overrides it.
  const range: RangeKey = chosenRange ?? (detail.data?.state === "live" ? "1h" : "all");

  // The window is measured from the match's own last snapshot, not from now:
  // "the last hour" of a match that finished yesterday is otherwise empty.
  const window = RANGES.find((r) => r.value === range)?.seconds ?? null;
  const lastTs = detail.data?.last_ts ?? null;
  const since = window != null && lastTs != null ? lastTs - window : null;

  const series = useAsync(
    async (signal) => {
      if (!detail.data) return null;
      const payload = await fetchSeries(conditionId, { points: 900, since }, signal);
      // The range is recorded as it was when the request went out, so the charts
      // can tell whether what they are showing is the window now selected. Fit
      // on the arriving data, not on the click: at click time the previous
      // window is still on screen.
      return { payload, range };
    },
    [conditionId, version, since, range, detail.data != null],
  );

  const colors = [palette["--series-1"], palette["--series-2"]] as const;
  const markers = useMemo(() => scoreMarkers(detail.data?.score_events ?? []), [detail.data]);
  const annotate = useMemo(
    () => scoreLookup(detail.data?.score_events ?? [], detail.data?.players ?? ["", ""]),
    [detail.data],
  );

  if (!detail.data) {
    return (
      <section>
        <button type="button" className="back-btn" onClick={onBack}>
          ← All matches
        </button>
        <p className="empty">
          {detail.error ? `Could not load this match — ${detail.error}` : "Loading…"}
        </p>
      </section>
    );
  }

  const match = detail.data;
  const data = series.data?.payload ?? null;
  // Identifies the window on screen. Changes when the reader picks a different
  // range or opens a different match -- both of which should reset the
  // viewport -- and not when a poll appends to the window already shown.
  const fitKey = data ? `${data.condition_id}:${series.data?.range}:${resetNonce}` : "";
  const widest = Math.max(...match.books.map((book) => book?.spread ?? 0));

  return (
    <section className={series.refetching ? "is-refetching" : undefined}>
      <div className="detail-head">
        <button type="button" className="back-btn" onClick={onBack}>
          ← All matches
        </button>
        <div className="detail-title">
          <h2>{match.players.join("  vs  ")}</h2>
          <div className="sub">{subtitle(match)}</div>
        </div>
      </div>

      <div className="tiles">
        {match.players.map((player, index) => {
          const book = match.books[index];
          const mid = book?.mid ?? null;
          return (
            <StatTile
              key={player}
              label={player}
              swatch={index}
              value={mid == null ? NO_QUOTE : fmtPrice(mid)}
              small={mid == null}
              sub={book ? `buy ${fmtPrice(book.ask)} · sell ${fmtPrice(book.bid)}` : "—"}
            />
          );
        })}
        <StatTile
          label="Widest spread"
          value={widest ? fmtPrice(widest) : "—"}
          sub="cost of crossing the book"
        />
        <StatTile
          label="Captured"
          value={match.snapshots.toLocaleString()}
          sub={
            match.first_ts != null && match.last_ts != null
              ? `over ${fmtDuration(match.last_ts - match.first_ts)} · updated ${fmtRelative(match.last_ts)}`
              : "nothing yet"
          }
        />
      </div>

      {!data || data.ts.length === 0 ? (
        <Panel title="History">
          <p className="chart-empty">No order-book snapshots recorded for this match yet.</p>
        </Panel>
      ) : (
        <>
          <Panel
            title="History"
            action={
              <Segmented
                label="Time range"
                value={range}
                onChange={pickRange}
                options={RANGES.map((r) => ({ value: r.value, label: r.label }))}
              />
            }
            note={priceNote(match, data.decimated, data.points, data.total_points, markers.length)}
          >
            <Legend
              items={[
                ...data.outcomes.map((outcome, i) => ({
                  name: outcome.name,
                  color: colors[i]!,
                })),
                ...(data.last_trade.some((v) => v != null)
                  ? [
                      {
                        name: `Last trade (as ${match.players[0]})`,
                        color: palette["--series-3"],
                        dashed: true,
                      },
                    ]
                  : []),
              ]}
            />
            <TimeSeriesChart
              ts={data.ts}
              palette={palette}
              height={280}
              precision={3}
              minMove={0.001}
              markers={markers}
              annotate={annotate}
              refLine={{ price: 0.5, label: "even" }}
              scale={{ minSpan: 0.08, clamp: [0, 1] }}
              ariaLabel={`Price history for ${match.question}`}
              fitKey={fitKey}
              series={[
                ...data.outcomes.map((outcome, i) => ({
                  key: `mid-${outcome.index}`,
                  name: outcome.name,
                  color: colors[i]!,
                  values: outcome.mid,
                })),
                ...(data.last_trade.some((v) => v != null)
                  ? [
                      {
                        key: "last-trade",
                        name: `Last trade (as ${match.players[0]})`,
                        color: palette["--series-3"],
                        values: data.last_trade,
                        dashed: true,
                        width: 1,
                      },
                    ]
                  : []),
              ]}
            />
          </Panel>

          <Panel
            title="Spread"
            hint="how far apart the best bid and best ask sit"
            note={spreadNote(data.outcomes, match.players, data.points)}
          >
            <Legend
              items={data.outcomes.map((outcome, i) => ({ name: outcome.name, color: colors[i]! }))}
            />
            <TimeSeriesChart
              ts={data.ts}
              palette={palette}
              height={190}
              precision={3}
              minMove={0.001}
              markers={markers}
              annotate={annotate}
              ariaLabel={`Spread over time for ${match.question}`}
              fitKey={fitKey}
              scale={{ zeroBased: true, minSpan: 0.02 }}
              series={data.outcomes.map((outcome, i) => ({
                key: `spread-${outcome.index}`,
                name: outcome.name,
                color: colors[i]!,
                values: outcome.spread,
              }))}
            />
          </Panel>

          <Panel
            title="Order-book depth"
            action={
              <Segmented
                label="Player"
                value={depthOutcome}
                onChange={setDepthOutcome}
                options={match.players.map((player, i) => ({ value: i, label: player }))}
              />
            }
            note={`Shares resting within the top ${match.depth} levels on each side of ${match.players[depthOutcome]}'s book — every level the capture stores. Falling depth means liquidity is leaving.`}
          >
            <Legend
              items={[
                { name: "Bid depth", color: palette["--bid"] },
                { name: "Ask depth", color: palette["--ask"] },
              ]}
            />
            <TimeSeriesChart
              // Remounted per player: a different book is different data, not an
              // update to the same series.
              key={`depth-${depthOutcome}`}
              ts={data.ts}
              palette={palette}
              height={190}
              precision={0}
              minMove={1}
              format={fmtSize}
              markers={markers}
              annotate={annotate}
              ariaLabel={`Order book depth over time for ${match.players[depthOutcome]}`}
              fitKey={fitKey}
              scale={{ zeroBased: true }}
              series={[
                {
                  key: "bid-depth",
                  name: "Bid depth",
                  color: palette["--bid"],
                  values: data.outcomes[depthOutcome]?.bid_depth ?? [],
                },
                {
                  key: "ask-depth",
                  name: "Ask depth",
                  color: palette["--ask"],
                  values: data.outcomes[depthOutcome]?.ask_depth ?? [],
                },
              ]}
            />
          </Panel>
        </>
      )}

      <Panel
        title="Order book"
        hint={match.last_ts != null ? `as of ${new Date(match.last_ts * 1000).toLocaleTimeString()}` : ""}
      >
        <div className="ladders">
          {match.books.map((book, index) => (
            <OrderBook key={index} book={book} player={match.players[index]!} index={index} />
          ))}
        </div>
      </Panel>

      <Panel
        title="Table view"
        action={
          <button type="button" className="ghost-btn" onClick={() => setShowTable((v) => !v)}>
            {showTable ? "Hide" : "Show"}
          </button>
        }
      >
        {showTable && data ? (
          <TableView series={data} detail={match} />
        ) : (
          <p className="note">The same numbers the charts plot, as text.</p>
        )}
      </Panel>
    </section>
  );
}

function subtitle(match: Detail): string {
  const state =
    match.state === "live"
      ? `live · ${scoreLabel(match.period, match.score)}`
      : match.state === "upcoming"
        ? `starts ${fmtDateTime(match.start_epoch)} (${fmtRelative(match.start_epoch)})`
        : `ended · ${scoreLabel(match.period, match.score) || "no final score recorded"}`;
  const kind = match.market_type === "moneyline" ? "match winner" : match.market_type;
  const event = [match.tour?.toUpperCase(), match.tournament].filter(Boolean).join(" ");
  return [event, state, kind].filter(Boolean).join(" · ");
}

function priceNote(
  match: Detail,
  decimated: boolean,
  points: number,
  total: number,
  markerCount: number,
): string {
  let note =
    "Prices are probabilities: what one share of that player winning costs. The two sides sum to about 1.";
  if (markerCount) {
    note += `  The ${markerCount} marks are score changes, set changes labelled; hover to read the score at that moment.`;
  }
  if (match.last_trade) {
    note += `  Polymarket reports one last-traded price per match, oriented to whichever side traded last, and the record does not say which side that was — the dotted line is that price re-expressed as ${match.players[0]}, inferred by which of the two mids it sits nearer to. It is least reliable when the mids are close together.`;
  }
  if (decimated) {
    note += `  Chart shows ${points.toLocaleString()} of ${total.toLocaleString()} snapshots; the table below and the database hold every one.`;
  }
  return note;
}

function spreadNote(
  outcomes: Detail extends never ? never : { name: string; mid: (number | null)[] }[],
  players: [string, string],
  points: number,
): string {
  const gaps = outcomes.map((outcome, i) => ({
    name: players[i] ?? outcome.name,
    count: outcome.mid.filter((v) => v == null).length,
  }));
  const missing = gaps.filter((gap) => gap.count > 0);
  if (missing.length === 0) return "Both sides were quoted on every tick in this window.";
  return `Gaps in a line are ticks with no quote on one side — normal once a match is effectively decided. ${missing
    .map((gap) => `${gap.name}: ${gap.count} of ${points}`)
    .join(", ")}.`;
}

/** One mark per score change; set changes are the ones worth labelling. */
function scoreMarkers(events: ScoreEvent[]): ChartMarker[] {
  const marks: ChartMarker[] = [];
  let previous: ScoreEvent | null = null;
  for (const event of events) {
    if (!previous || event.period !== previous.period || event.score !== previous.score) {
      marks.push({
        ts: event.ts,
        major: !previous || event.period !== previous.period,
        label: scoreLabel(event.period, event.score),
      });
    }
    previous = event;
  }
  return marks;
}

function scoreLookup(events: ScoreEvent[], players: [string, string]): (ts: number) => string | null {
  const sorted = [...events].sort((a, b) => a.ts - b.ts);
  return (ts: number) => {
    let found: ScoreEvent | null = null;
    for (const event of sorted) {
      if (event.ts <= ts) found = event;
      else break;
    }
    if (!found) return null;
    const label = scoreLabel(found.period, found.score);
    if (!label) return null;
    // The points inside the game, and who was serving them. They turn over
    // several times a game, which is why score_events carries many more rows
    // than the chart has markers: the marks are the games, this is where you
    // were within one -- and 30-40 on serve is not the same news as 30-40
    // against it.
    const server = found.serving == null ? null : players[found.serving];
    const parts = [label, found.game, server && `${shortName(server)} serving`];
    return `Score: ${parts.filter(Boolean).join(" · ")}`;
  };
}
