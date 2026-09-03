import { useMemo, useState } from "react";
import { fmtRelative } from "../format";
import type { MatchStats as Stats, StatEntry } from "../types";
import { Panel, Segmented } from "./Chrome";

/** Which reading is on screen: the tick capture, or one settled period. */
type PeriodKey = string;

const LIVE = "—live—"; // not a period name the feed can produce

/**
 * The match statistics as Flashscore lays them out: one row per statistic, the
 * two players either side of a bar that shows who is ahead on it.
 *
 * Two readings can be on offer and they are deliberately not merged. `live` is
 * the last row the capture wrote on the tick, current to within a poll. `sets`
 * is the reading taken an hour after the match, once the site had stopped
 * reclassifying winners and revising serve speeds, so its "Match" row can
 * legitimately disagree with the last live one -- which is the whole reason it
 * is collected. Showing them as one number would hide exactly the difference
 * the table exists to expose.
 */
export function MatchStats({
  stats,
  players,
  live,
}: {
  stats: Stats;
  players: [string, string];
  live: boolean;
}) {
  const periods = stats.sets.map((s) => s.period);
  const hasLive = stats.live.length > 0;
  // A match still being played opens on the tick reading; a finished one on the
  // settled match totals, which are the better number once they exist.
  const [chosen, setChosen] = useState<PeriodKey | null>(null);
  const fallback = live && hasLive ? LIVE : (periods[0] ?? LIVE);
  const selected = chosen ?? fallback;

  const rows = useMemo<StatEntry[]>(
    () =>
      selected === LIVE
        ? stats.live
        : (stats.sets.find((s) => s.period === selected)?.stats ?? []),
    [stats, selected],
  );

  if (!hasLive && periods.length === 0) return null;

  const options = [
    ...(hasLive ? [{ value: LIVE, label: "Live" }] : []),
    ...periods.map((period) => ({ value: period, label: period === "Match" ? "Final" : period })),
  ];

  const groups: { name: string; rows: StatEntry[] }[] = [];
  for (const row of rows) {
    const last = groups[groups.length - 1];
    if (last && last.name === row.group) last.rows.push(row);
    else groups.push({ name: row.group, rows: [row] });
  }

  return (
    <Panel
      title="Match statistics"
      action={
        options.length > 1 ? (
          <Segmented label="Statistics period" value={selected} onChange={setChosen} options={options} />
        ) : undefined
      }
      note={note(stats, selected, rows.length)}
    >
      <div className="stat-players">
        <span>
          <span className="swatch swatch-0" />
          {players[0]}
        </span>
        <span>
          {players[1]}
          <span className="swatch swatch-1" />
        </span>
      </div>
      {rows.length === 0 ? (
        <p className="chart-empty">Nothing recorded for this period.</p>
      ) : (
        groups.map((group) => (
          <div className="stat-group" key={group.name}>
            <h4>{group.name}</h4>
            {group.rows.map((row) => (
              <StatRow key={row.key} row={row} />
            ))}
          </div>
        ))
      )}
    </Panel>
  );
}

function StatRow({ row }: { row: StatEntry }) {
  const [a, b] = row.values;
  const share = leadShare(row);
  return (
    <div className="stat-row">
      <span className="stat-value">{format(a, row.unit)}</span>
      <span className="stat-mid">
        <span className="stat-label">{row.label}</span>
        <span className="stat-bar" aria-hidden="true">
          <span className="stat-bar-a" style={{ width: `${share * 100}%` }} />
          <span className="stat-bar-b" style={{ width: `${(1 - share) * 100}%` }} />
        </span>
      </span>
      <span className="stat-value">{format(b, row.unit)}</span>
    </div>
  );
}

/**
 * How much of the bar belongs to the first player.
 *
 * Compared on the rate where the feed gives one, not on the raw count: 49 of 67
 * first serves against 46 of 54 is the *second* player ahead, and a bar drawn
 * from 49 against 46 would say the opposite. Where there is nothing to be out
 * of, the counts are the comparison. Nothing on either side is an even split
 * rather than a divide by zero.
 */
function leadShare(row: StatEntry): number {
  const rates = row.values.map(([value, of]) => {
    if (value == null) return null;
    if (of == null) return value;
    return of > 0 ? value / of : 0;
  });
  const [a, b] = rates;
  if (a == null || b == null) return 0.5;
  const total = a + b;
  if (total <= 0) return 0.5;
  return a / total;
}

function format(pair: [number | null, number | null], unit: string | null): string {
  const [value, of] = pair;
  if (value == null) return "—";
  // The percentage is the quotient of the pair and is not stored, so it is
  // computed here rather than read back -- and shown beside the pair, never
  // instead of it: 0/0 and 0/8 are both "0%" and are not the same thing.
  if (of != null) {
    const pct = of > 0 ? ` (${Math.round((100 * value) / of)}%)` : "";
    return `${trim(value)}/${trim(of)}${pct}`;
  }
  if (unit === "%") return `${trim(value)}%`;
  if (unit) return `${trim(value).toLocaleString()} ${unit}`;
  return trim(value).toLocaleString();
}

function trim(value: number): number {
  return Number.isInteger(value) ? value : Math.round(value * 10) / 10;
}

function note(stats: Stats, selected: PeriodKey, rows: number): string {
  if (rows === 0) return "";
  if (selected === LIVE) {
    const age = stats.ts == null ? "" : ` Read ${fmtRelative(stats.ts)}.`;
    return (
      "Flashscore's running totals for the match, recorded on the same tick as the book above, so a" +
      " statistic and the price beside it describe one moment." +
      age +
      (stats.sets.length
        ? " The settled per-set version, collected an hour after the match, is under the other tabs — where it disagrees, that one is right."
        : "")
    );
  }
  const when = stats.final_ts == null ? "" : ` Collected ${fmtRelative(stats.final_ts)}.`;
  return (
    "The settled reading, taken an hour after the match ended — Flashscore goes on reclassifying" +
    " winners and correcting serve speeds after the last point, and this is the version that" +
    ` stopped moving. The per-set rows sum to Final.${when}`
  );
}
