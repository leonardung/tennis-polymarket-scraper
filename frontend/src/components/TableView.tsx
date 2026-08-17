import { fmtClock, fmtPrice } from "../format";
import type { MatchDetail, MatchSeries } from "../types";

/** Every charted value as text.
 *
 * Not optional polish: the "last trade" series uses the light aqua slot, which
 * sits below 3:1 against the light surface, and the palette rules allow that
 * only where the values are also readable without relying on the colour.
 */
export function TableView({ series, detail }: { series: MatchSeries; detail: MatchDetail }) {
  const rows = series.ts.map((_, i) => i).reverse();

  return (
    <div className="table-scroll">
      <table className="data-table">
        <thead>
          <tr>
            <th>Time</th>
            {detail.players.map((player) => (
              <th key={`${player}-mid`}>{player} mid</th>
            ))}
            {detail.players.map((player) => (
              <th key={`${player}-spread`}>{player} spread</th>
            ))}
            <th>Last trade</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((i) => (
            <tr key={series.ts[i]}>
              <td>{fmtClock(series.ts[i], true)}</td>
              {series.outcomes.map((outcome) => (
                <td key={`${outcome.index}-m`}>{fmtPrice(outcome.mid[i])}</td>
              ))}
              {series.outcomes.map((outcome) => (
                <td key={`${outcome.index}-s`}>{fmtPrice(outcome.spread[i])}</td>
              ))}
              <td>{fmtPrice(series.last_trade[i])}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
