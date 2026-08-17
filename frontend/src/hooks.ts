import { useCallback, useEffect, useRef, useState } from "react";
import { fetchPulse } from "./api";

const POLL_MS = 5000;

/** A counter that ticks whenever the capture writes something new.
 *
 * /api/pulse is a two-column read; the heavy endpoints are only refetched when
 * it moves. That keeps an idle dashboard from re-rendering every five seconds,
 * which matters here because a chart re-render throws away the user's zoom.
 */
export function useDataVersion(): number {
  const [version, setVersion] = useState(0);
  const previous = useRef<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;

    const poll = async () => {
      try {
        const pulse = await fetchPulse();
        const key = `${pulse.last_ts}:${pulse.rows}`;
        if (!cancelled && previous.current !== key) {
          previous.current = key;
          setVersion((n) => n + 1);
        }
      } catch {
        /* the visible request will surface the error */
      }
      if (!cancelled) timer = window.setTimeout(poll, POLL_MS);
    };

    void poll();
    return () => {
      cancelled = true;
      if (timer) window.clearTimeout(timer);
    };
  }, []);

  return version;
}

export interface Async<T> {
  data: T | null;
  error: string | null;
  /** True while refetching over data already on screen -- hold the frame. */
  refetching: boolean;
}

/** Fetch that keeps the previous value visible while the next one loads. */
export function useAsync<T>(
  load: (signal: AbortSignal) => Promise<T>,
  deps: readonly unknown[],
): Async<T> {
  const [state, setState] = useState<Async<T>>({ data: null, error: null, refetching: false });
  // Kept in a ref so the effect does not re-run when the data it fetched arrives.
  const hasData = useRef(false);

  useEffect(() => {
    const controller = new AbortController();
    setState((s) => ({ ...s, refetching: hasData.current }));

    load(controller.signal)
      .then((data) => {
        if (controller.signal.aborted) return;
        hasData.current = true;
        setState({ data, error: null, refetching: false });
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted || (error as Error).name === "AbortError") return;
        setState((s) => ({ ...s, error: (error as Error).message, refetching: false }));
      });

    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  return state;
}

export type Route = { kind: "list"; tab: string | null } | { kind: "match"; id: string };

function parseHash(): Route {
  const hash = window.location.hash.replace(/^#/, "");
  if (hash.startsWith("match/")) return { kind: "match", id: hash.slice("match/".length) };
  if (hash.startsWith("tab/")) return { kind: "list", tab: hash.slice("tab/".length) };
  return { kind: "list", tab: null };
}

/** Hash routing, so reload and the browser's back button both behave. */
export function useRoute(): [Route, (route: Route) => void] {
  const [route, setRoute] = useState<Route>(parseHash);

  useEffect(() => {
    const onChange = () => setRoute(parseHash());
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);

  const navigate = useCallback((next: Route) => {
    const hash = next.kind === "match" ? `match/${next.id}` : `tab/${next.tab ?? "live"}`;
    if (window.location.hash.replace(/^#/, "") === hash) setRoute(next);
    else window.location.hash = hash;
  }, []);

  return [route, navigate];
}

/** Re-render on a timer, so relative timestamps ("4m ago") do not go stale. */
export function useTicker(ms = 15000): void {
  const [, force] = useState(0);
  useEffect(() => {
    const timer = window.setInterval(() => force((n) => n + 1), ms);
    return () => window.clearInterval(timer);
  }, [ms]);
}
