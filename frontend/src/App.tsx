import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { fetchCounts, fetchOverview, fetchPast } from "./api";
import { Filters, type FilterState, Pager, Tabs, TopBar } from "./components/Chrome";
import { MatchCard } from "./components/MatchCard";
import { MatchDetail } from "./components/MatchDetail";
import { useAsync, useDataVersion, useRoute, useTicker } from "./hooks";
import { useTheme } from "./theme";
import type { MatchState, MatchSummary } from "./types";

const EMPTY: Record<MatchState, string> = {
  live: "No match is being played right now. Upcoming matches are picked up as they start.",
  upcoming:
    "No scheduled matches recorded yet — run the capture with --include-upcoming to poll them before they start.",
  past: "No finished matches recorded yet.",
};

// Finished matches are served a page at a time; a multiple of 2, 3 and 4 so the
// card grid ends on a full row at the common widths.
const PAST_PAGE = 48;

const SORTS = ["natural", "move", "span", "spread"];
const NO_FILTERS: FilterState = { tour: "", tournament: "", search: "", sort: "natural" };

/** The list's filters and page, out of a list route's query string. */
function readQuery(query: string): { filters: FilterState; page: number } {
  const params = new URLSearchParams(query);
  const sort = params.get("sort") ?? "";
  const page = Number.parseInt(params.get("page") ?? "", 10);
  return {
    filters: {
      tour: params.get("tour") ?? "",
      tournament: params.get("tournament") ?? "",
      search: params.get("q") ?? "",
      sort: SORTS.includes(sort) ? sort : "natural",
    },
    page: Number.isFinite(page) && page > 1 ? page - 1 : 0,
  };
}

/** The inverse of readQuery; defaults are left out so a plain tab stays `#tab/live`. */
function writeQuery(filters: FilterState, page: number): string {
  const params = new URLSearchParams();
  if (filters.tour) params.set("tour", filters.tour);
  if (filters.tournament) params.set("tournament", filters.tournament);
  if (filters.search) params.set("q", filters.search);
  if (filters.sort !== "natural") params.set("sort", filters.sort);
  if (page > 0) params.set("page", String(page + 1));
  return params.toString();
}

export function App() {
  const { palette, isDark, toggle } = useTheme();
  const version = useDataVersion();
  const [route, navigate] = useRoute();
  const overview = useAsync((signal) => fetchOverview(signal), [version]);
  useTicker();

  // Chosen once, when the first payload says which tabs have anything in them;
  // re-picking on every poll would move the ground under the reader.
  const [chosenTab, setChosenTab] = useState<MatchState | null>(null);

  const allCounts = overview.data?.counts ?? { live: 0, upcoming: 0, past: 0 };

  useEffect(() => {
    if (chosenTab || !overview.data) return;
    setChosenTab(allCounts.live ? "live" : allCounts.upcoming ? "upcoming" : "past");
  }, [overview.data, chosenTab, allCounts.live, allCounts.upcoming]);

  // The URL is the list's state: tab, filters and page all live in the hash.
  // A match route has none of its own, so the last list route is remembered
  // and the match page's Back returns to exactly that list.
  const lastList = useRef<{ tab: MatchState | null; query: string }>({ tab: null, query: "" });
  const routeTab = route.kind === "list" ? route.tab : lastList.current.tab;
  const tab: MatchState =
    routeTab === "live" || routeTab === "upcoming" || routeTab === "past"
      ? routeTab
      : (chosenTab ?? "live");
  const query = route.kind === "list" ? route.query : lastList.current.query;
  useEffect(() => {
    if (route.kind === "list") lastList.current = { tab, query };
  }, [route.kind, tab, query]);

  const { filters, page } = useMemo(() => readQuery(query), [query]);
  const pastOffset = page * PAST_PAGE;
  const goList = useCallback(
    (next: { tab?: MatchState; filters?: FilterState; page?: number }, replace = false) =>
      navigate(
        {
          kind: "list",
          tab: next.tab ?? tab,
          query: writeQuery(next.filters ?? filters, next.page ?? page),
        },
        { replace },
      ),
    [navigate, tab, filters, page],
  );

  // The tournament list follows the tour: with ATP picked, only ATP events.
  const tournaments = useMemo(() => {
    if (!overview.data) return [];
    return filters.tour
      ? (overview.data.tournaments_by_tour[filters.tour] ?? [])
      : overview.data.tournaments;
  }, [overview.data, filters.tour]);

  // The badges count under the filters, from `markets` alone. Only the fields
  // that decide membership are keys -- a sort change moves nothing between tabs.
  const filtered = useAsync(
    (signal) => fetchCounts(filters, signal),
    [version, filters.tour, filters.tournament, filters.search],
  );
  const counts = filtered.data ?? allCounts;

  // The overview carries live and upcoming cards only. Finished matches -- the
  // bulk of a season, and growing -- are filtered, sorted and paged by the
  // server, and only fetched while their tab is on screen.
  const isPast = tab === "past";
  const past = useAsync(
    (signal) => fetchPast({ ...filters, offset: pastOffset, limit: PAST_PAGE }, signal),
    [version, filters, pastOffset],
    isPast && route.kind === "list",
  );

  // A page can empty out under the reader (a narrower filter, a match leaving
  // the tab, a stale link); step back to the last page that still has something.
  const pastTotal = past.data?.total ?? 0;
  useEffect(() => {
    if (route.kind === "list" && isPast && page > 0 && pastOffset >= pastTotal && past.data) {
      goList({ page: Math.max(0, Math.ceil(pastTotal / PAST_PAGE) - 1) }, true);
    }
  }, [route.kind, isPast, page, pastOffset, pastTotal, past.data, goList]);

  const inTab = useMemo(
    () => (overview.data?.matches ?? []).filter((m) => m.state === tab),
    [overview.data, tab],
  );
  const visible = useMemo(
    () => (isPast ? (past.data?.matches ?? []) : sortMatches(filterMatches(inTab, filters), filters.sort)),
    [isPast, past.data, inTab, filters],
  );

  const tabTotal = allCounts[tab];
  const shown = isPast ? pastTotal : visible.length;
  const summary =
    tabTotal === 0 || (isPast && !past.data)
      ? ""
      : shown === tabTotal
        ? `${tabTotal} match${tabTotal === 1 ? "" : "es"}`
        : `${shown} of ${tabTotal}`;
  const current = isPast ? past : overview;

  const changeFilters = (next: FilterState) => {
    // A tournament the newly picked tour does not hold would filter to nothing.
    const held = next.tour ? (overview.data?.tournaments_by_tour[next.tour] ?? []) : null;
    if (next.tournament && held && !held.some((t) => t.name === next.tournament)) {
      next = { ...next, tournament: "" };
    }
    goList({ filters: next, page: 0 }, true);
  };

  return (
    <>
      <TopBar
        overview={overview.data}
        error={overview.error}
        isDark={isDark}
        onToggleTheme={toggle}
      />
      <Filters
        tours={overview.data?.tours ?? []}
        tournaments={tournaments}
        value={filters}
        onChange={changeFilters}
        onReset={() => goList({ filters: NO_FILTERS, page: 0 }, true)}
        naturalLabel={isPast ? "Last captured" : "Start time"}
        summary={summary}
      />
      <Tabs active={tab} counts={counts} onSelect={(next) => goList({ tab: next, page: 0 })} />

      <main>
        {route.kind === "match" ? (
          <MatchDetail
            conditionId={route.id}
            palette={palette}
            version={version}
            onBack={() => goList({})}
          />
        ) : (
          <section>
            <div className={current.refetching ? "cards is-refetching" : "cards"}>
              {visible.map((match) => (
                <MatchCard
                  key={match.condition_id}
                  match={match}
                  onOpen={() => navigate({ kind: "match", id: match.condition_id })}
                />
              ))}
            </div>
            {isPast && past.data && (
              <Pager
                offset={past.data.offset}
                limit={past.data.limit}
                total={past.data.total}
                onPage={(offset) => {
                  goList({ page: Math.floor(offset / PAST_PAGE) });
                  window.scrollTo({ top: 0 });
                }}
              />
            )}
            {visible.length === 0 && (
              <p className="empty">
                {current.error
                  ? `Cannot read the capture database — ${current.error}`
                  : !current.data
                    ? "Loading…"
                    : tabTotal
                      ? "Nothing matches those filters."
                      : EMPTY[tab]}
              </p>
            )}
          </section>
        )}
      </main>
    </>
  );
}

function filterMatches(matches: MatchSummary[], filters: FilterState): MatchSummary[] {
  const needle = filters.search.trim().toLowerCase();
  return matches.filter((match) => {
    if (filters.tour && match.tour !== filters.tour) return false;
    if (filters.tournament && match.tournament !== filters.tournament) return false;
    if (!needle) return true;
    return [match.question, ...match.players].join(" ").toLowerCase().includes(needle);
  });
}

function widestSpread(match: MatchSummary): number {
  const spreads = match.prices.map((p) => p?.spread).filter((s): s is number => s != null);
  return spreads.length ? Math.max(...spreads) : -1;
}

/** How long the book has been recorded -- the overview has no row count, which costs a full index scan. */
function captured(match: MatchSummary): number {
  return match.first_ts != null && match.last_ts != null ? match.last_ts - match.first_ts : -1;
}

/** Live and upcoming only; Past is sorted by the server (`_sort_key` in queries.py). */
function sortMatches(matches: MatchSummary[], sort: string): MatchSummary[] {
  const sorted = [...matches];
  switch (sort) {
    case "move":
      return sorted.sort((a, b) => Math.abs(b.move ?? 0) - Math.abs(a.move ?? 0));
    case "span":
      return sorted.sort((a, b) => captured(b) - captured(a));
    case "spread":
      return sorted.sort((a, b) => widestSpread(b) - widestSpread(a));
    default:
      // Soonest first.
      return sorted.sort((a, b) => (a.start_epoch ?? 0) - (b.start_epoch ?? 0));
  }
}
