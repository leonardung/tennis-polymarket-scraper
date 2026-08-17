/** Shared formatting. Prices are probabilities in [0, 1]; sizes are share counts. */

export const NO_QUOTE = "no quote";

export function fmtPrice(value: number | null | undefined, digits = 3): string {
  return value == null ? "—" : value.toFixed(digits);
}

export function fmtSize(value: number | null | undefined): string {
  if (value == null) return "—";
  if (Math.abs(value) >= 1e6) return `${(value / 1e6).toFixed(1)}M`;
  if (Math.abs(value) >= 1e3) return `${(value / 1e3).toFixed(1)}K`;
  if (Number.isInteger(value)) return String(value);
  return Math.abs(value) >= 100 ? value.toFixed(0) : value.toFixed(1);
}

export function fmtSigned(value: number | null | undefined, digits = 3): string {
  if (value == null) return "—";
  const sign = value > 0 ? "+" : value < 0 ? "−" : "";
  return sign + Math.abs(value).toFixed(digits);
}

export function fmtClock(ts: number | null | undefined, withSeconds = false): string {
  if (ts == null) return "—";
  const options: Intl.DateTimeFormatOptions = { hour: "2-digit", minute: "2-digit" };
  if (withSeconds) options.second = "2-digit";
  return new Date(ts * 1000).toLocaleTimeString([], options);
}

export function fmtDateTime(ts: number | null | undefined): string {
  if (ts == null) return "—";
  const date = new Date(ts * 1000);
  const time = date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  if (date.toDateString() === new Date().toDateString()) return time;
  return `${date.toLocaleDateString([], { month: "short", day: "numeric" })} ${time}`;
}

export function fmtDuration(seconds: number | null | undefined): string {
  if (seconds == null || seconds < 0) return "—";
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.round((seconds % 3600) / 60);
  if (hours >= 1) return `${hours}h ${String(minutes).padStart(2, "0")}m`;
  if (seconds >= 60) return `${Math.round(seconds / 60)}m`;
  return `${Math.round(seconds)}s`;
}

export function fmtRelative(ts: number | null | undefined): string {
  if (ts == null) return "—";
  const delta = ts - Date.now() / 1000;
  const magnitude = fmtDuration(Math.abs(delta));
  return delta > 0 ? `in ${magnitude}` : `${magnitude} ago`;
}

/** How the score feed says a match stopped. */
const ENDINGS: Record<string, string> = {
  FT: "Finished",
  RET: "Retired",
  WO: "Walkover",
  CAN: "Cancelled",
  ABD: "Abandoned",
  POST: "Postponed",
};

export const fmtEnding = (period: string | null): string =>
  (period && ENDINGS[period]) || "Ended";

export const scoreLabel = (period: string | null, score: string | null): string =>
  [period, score].filter(Boolean).join(" ");

/** Surname only, for places where the full name will not fit. */
export const shortName = (name: string): string => name.split(" ").pop() ?? name;
