import { fmtPrice, fmtRelative, fmtSigned } from "../format";
import type { MatchOdds as Odds } from "../types";
import { Panel } from "./Chrome";

/**
 * The bookmakers' consensus for one match, beside Polymarket's own price.
 *
 * Recorded from TennisExplorer and **pre-match only**: bookmakers close at the
 * first ball, so whatever line is shown is the closing one, sampled up to the
 * start and once more on the tick the match went live. The stored price is a
 * decimal odd; the probability beside it is `1/odd`, which includes the book's
 * margin. The consensus row averages those probabilities and normalizes them so
 * the two sum to one -- the book's margin removed -- which is the number
 * comparable to a Polymarket mid.
 *
 * It renders nothing at all when the capture has no odds for the match: a
 * match first seen live never had its pre-match market recorded, and an empty
 * panel would read as a failure rather than an absence.
 */
export function MatchOdds({
  odds,
  players,
  mids,
}: {
  odds: Odds;
  players: [string, string];
  mids: [number | null, number | null];
}) {
  // A decimal odd of 1 or less is not a two-way price; the capture stores only
  // two-sided lines, but a malformed row should not become a division by zero.
  const quotes = odds.bookmakers.filter((b) => b.price_0 > 1 && b.price_1 > 1);
  if (quotes.length === 0) return null;

  const best0 = Math.max(...quotes.map((q) => q.price_0));
  const best1 = Math.max(...quotes.map((q) => q.price_1));

  // Mean implied probability per side, then normalize: the division is what
  // takes the overround out, so the two consensus numbers sum to 1 exactly.
  const mean0 = quotes.reduce((sum, q) => sum + 1 / q.price_0, 0) / quotes.length;
  const mean1 = quotes.reduce((sum, q) => sum + 1 / q.price_1, 0) / quotes.length;
  const total = mean0 + mean1;
  const fair0 = total > 0 ? mean0 / total : null;
  const fair1 = total > 0 ? mean1 / total : null;
  const margin = total > 0 ? total - 1 : null;

  return (
    <Panel
      title="Bookmaker odds"
      hint={odds.ts == null ? undefined : `read ${fmtRelative(odds.ts)}`}
      note={note(quotes.length, margin, odds.ts)}
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
      <div className="table-scroll">
        <table className="data-table">
          <thead>
            <tr>
              <th>Bookmaker</th>
              <th>{players[0]}</th>
              <th>{players[1]}</th>
            </tr>
          </thead>
          <tbody>
            {quotes.map((quote) => (
              <tr key={quote.bookmaker}>
                <td>{quote.bookmaker}</td>
                <td className={quote.price_0 === best0 ? "odds-best" : undefined}>
                  {oddsCell(quote.price_0)}
                </td>
                <td className={quote.price_1 === best1 ? "odds-best" : undefined}>
                  {oddsCell(quote.price_1)}
                </td>
              </tr>
            ))}
            <tr className="odds-consensus">
              <td>Consensus</td>
              <td>
                {fmtPrice(fair0)}
                <span className="odds-delta">{delta(fair0, mids[0])}</span>
              </td>
              <td>
                {fmtPrice(fair1)}
                <span className="odds-delta">{delta(fair1, mids[1])}</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </Panel>
  );
}

function oddsCell(price: number): string {
  return `${price.toFixed(2)}  ${(100 / price).toFixed(1)}%`;
}

/** The consensus probability minus Polymarket's mid, as a signed number. */
function delta(consensus: number | null, mid: number | null): string {
  if (consensus == null || mid == null) return "";
  return ` ${fmtSigned(consensus - mid)}`;
}

function note(books: number, margin: number | null, ts: number | null): string {
  const read = ts == null ? "" : ` Last read ${fmtRelative(ts)}, before the match started.`;
  const vig =
    margin == null
      ? ""
      : ` The bookmakers' margin is ${(margin * 100).toFixed(1)}%; the consensus removes it.`;
  return (
    `${books} bookmakers, from TennisExplorer. These markets are pre-match only — they` +
    ` close at the first ball, so the line shown is the closing one. The number beside each` +
    ` decimal odd is its implied probability (1/odd); the best price on each side is shown` +
    ` in bold.${vig} The consensus is the mean implied probability, normalized to sum to 1,` +
    ` and the signed number beside it is its difference from Polymarket's mid.${read}`
  );
}
