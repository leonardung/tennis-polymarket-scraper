import type { MatchDetail, MatchSeries, Overview, Pulse } from "./types";

export class ApiError extends Error {}

async function getJSON<T>(url: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(url, { cache: "no-store", signal });
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try {
      const body = (await response.json()) as { error?: string; detail?: string };
      detail = body.error ?? body.detail ?? detail;
    } catch {
      /* keep the status */
    }
    throw new ApiError(detail);
  }
  return (await response.json()) as T;
}

export const fetchOverview = (signal?: AbortSignal) => getJSON<Overview>("/api/overview", signal);

export const fetchPulse = (signal?: AbortSignal) => getJSON<Pulse>("/api/pulse", signal);

export const fetchMatch = (conditionId: string, signal?: AbortSignal) =>
  getJSON<MatchDetail>(`/api/match/${conditionId}`, signal);

export function fetchSeries(
  conditionId: string,
  options: { points?: number; since?: number | null } = {},
  signal?: AbortSignal,
) {
  const params = new URLSearchParams({ points: String(options.points ?? 900) });
  if (options.since != null) params.set("since", String(options.since));
  return getJSON<MatchSeries>(`/api/match/${conditionId}/series?${params}`, signal);
}
