/** Shapes returned by the FastAPI layer in src/polymarket/dashboard/queries.py.
 *
 * A missing quote is `null`, never a stand-in number: the capture stores NULL
 * when a side of the book has no offers, which is real information about a
 * market rather than an absent reading. Every price field is therefore nullable
 * and the UI has to say "no quote" rather than draw a zero.
 */

export type MatchState = "live" | "upcoming" | "past";

export interface PriceSummary {
  ts: number;
  bid: number | null;
  ask: number | null;
  mid: number | null;
  spread: number | null;
}

/** The per-match last traded price, re-expressed as outcome 0. See `_orient`. */
export interface LastTrade {
  raw: number | null;
  p0: number | null;
  /** False when there was no mid to infer the orientation from. */
  certain: boolean;
}

export interface MatchSummary {
  condition_id: string;
  question: string;
  /** "atp" or "wta" -- the draw, which a combined event's name cannot say. */
  tour: string | null;
  tournament: string | null;
  tier: string | null;
  market_type: string | null;
  players: [string, string];
  feed_state: string | null;
  state: MatchState;
  period: string | null;
  score: string | null;
  /** Points in the game being played, e.g. "40-30", in `players` order. */
  game: string | null;
  start_time: string | null;
  start_epoch: number | null;
  last_seen: number | null;
  prices: [PriceSummary | null, PriceSummary | null];
  last_trade: LastTrade | null;
  snapshots: number;
  first_ts: number | null;
  last_ts: number | null;
  spark: (number | null)[];
  move: number | null;
}

export interface Overview {
  generated_at: number;
  last_tick: number | null;
  capturing: boolean;
  counts: Record<MatchState, number>;
  tours: string[];
  tournaments: string[];
  matches: MatchSummary[];
}

export interface BookLevel {
  price: number | null;
  size: number | null;
}

export interface Ladder {
  ts: number;
  outcome: string;
  bid: number | null;
  ask: number | null;
  mid: number | null;
  spread: number | null;
  bids: BookLevel[];
  asks: BookLevel[];
  bid_depth: number | null;
  ask_depth: number | null;
}

export interface ScoreEvent {
  ts: number;
  state: string | null;
  period: string | null;
  score: string | null;
  /** Points in the game being played at that moment, e.g. "30-40". */
  game: string | null;
  /** Which of the two players was serving, by index into `players`. */
  serving: number | null;
}

export interface MatchDetail {
  condition_id: string;
  question: string;
  tour: string | null;
  tournament: string | null;
  tier: string | null;
  market_type: string | null;
  slug: string | null;
  event_slug: string | null;
  players: [string, string];
  feed_state: string | null;
  state: MatchState;
  period: string | null;
  score: string | null;
  start_time: string | null;
  start_epoch: number | null;
  last_seen: number | null;
  snapshots: number;
  first_ts: number | null;
  last_ts: number | null;
  depth: number;
  books: [Ladder | null, Ladder | null];
  last_trade: LastTrade | null;
  score_events: ScoreEvent[];
  stats: MatchStats;
}

/** One statistic, both players. Mirrors queries._stat_period. */
export interface StatEntry {
  key: string;
  label: string;
  group: string;
  /** "%", "km/h", "m", or null for a plain count. */
  unit: string | null;
  /**
   * [value, out of] per player, in outcome order. `of` is null for a count or a
   * measurement; where it is set, the percentage is the quotient of the two and
   * is deliberately neither stored nor sent.
   */
  values: [[number | null, number | null], [number | null, number | null]];
}

export interface MatchStats {
  /** When the live totals below were read, or null if none were. */
  ts: number | null;
  live: StatEntry[];
  /** When the settled per-set reading was collected; null until an hour after. */
  final_ts: number | null;
  sets: { period: string; stats: StatEntry[] }[];
}

export interface OutcomeSeries {
  index: number;
  name: string;
  mid: (number | null)[];
  bid: (number | null)[];
  ask: (number | null)[];
  spread: (number | null)[];
  bid_depth: (number | null)[];
  ask_depth: (number | null)[];
}

export interface MatchSeries {
  condition_id: string;
  ts: number[];
  outcomes: [OutcomeSeries, OutcomeSeries];
  last_trade: (number | null)[];
  last_trade_raw: (number | null)[];
  score_events: ScoreEvent[];
  points: number;
  total_points: number;
  decimated: boolean;
}

export interface Pulse {
  last_ts: number | null;
  rows: number;
}
