import { fmtPrice, fmtSize } from "../format";
import type { BookLevel, Ladder } from "../types";

/** The stored book as a ladder: asks descending above, bids descending below. */
export function OrderBook({
  book,
  player,
  index,
}: {
  book: Ladder | null;
  player: string;
  index: number;
}) {
  return (
    <div>
      <div className="ladder-title">
        <span className={`swatch swatch-${index}`} />
        <span>{player}</span>
      </div>
      {!book || (book.bids.length === 0 && book.asks.length === 0) ? (
        <p className="ladder-empty">No resting orders recorded.</p>
      ) : (
        <Ladders book={book} />
      )}
    </div>
  );
}

function Ladders({ book }: { book: Ladder }) {
  const sizes = [...book.bids, ...book.asks].map((level) => level.size ?? 0);
  const largest = Math.max(...sizes, 1);

  return (
    <>
      <table className="ladder">
        <thead>
          <tr>
            <th>Price</th>
            <th>Size</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {/* Worst ask at the top, best ask nearest the mid. */}
          {[...book.asks].reverse().map((level, i) => (
            <Row key={`a${i}`} level={level} side="ask" largest={largest} />
          ))}
          <tr className="ladder-mid">
            <td>mid {fmtPrice(book.mid)}</td>
            <td className="sz">spread</td>
            <td className="sz">{fmtPrice(book.spread)}</td>
          </tr>
          {book.bids.map((level, i) => (
            <Row key={`b${i}`} level={level} side="bid" largest={largest} />
          ))}
        </tbody>
      </table>
      <p className="note">
        Asks above, bids below — buying costs the lowest ask, selling earns the highest bid.
      </p>
    </>
  );
}

function Row({
  level,
  side,
  largest,
}: {
  level: BookLevel;
  side: "bid" | "ask";
  largest: number;
}) {
  const width = Math.max(2, ((level.size ?? 0) / largest) * 100);
  return (
    <tr className={side === "bid" ? "bid-row" : "ask-row"}>
      <td className="px">{fmtPrice(level.price)}</td>
      <td className="sz">{fmtSize(level.size)}</td>
      <td className="bar-cell">
        <div className={`depth-bar ${side}`} style={{ width: `${width}%` }} />
      </td>
    </tr>
  );
}
