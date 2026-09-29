import { useEffect, useMemo, useState } from "react";
import { fetchOverview, fetchPast } from "./api";
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

export function App() {
  const { palette, isDark, toggle } = useTheme();
  const version = useDataVersion();
  const [route, navigate] = useRoute();
  const overview = useAsync((signal) => fetchOverview(signal), [version]);
  useTicker();

  const [filters, setFilters] = useState<FilterState>({
    tour: "",
    tournament: "",
    search: "",
    sort: "natural",
  });
  // Chosen once, when the first payload says which tabs have anything in them;
  // re-picking on every poll would move the ground under the reader.
  const [chosenTab, setChosenTab] = useState<MatchState | null>(null);
  const [pastOffset, setPastOffset] = useState(0);

  const counts = overview.data?.counts ?? { live: 0, upcoming: 0, past: 0 };

  useEffect(() => {
    if (chosenTab || !overview.data) return;
    setChosenTab(counts.live ? "live" : counts.upcoming ? "upcoming" : "past");
  }, [overview.data, chosenTab, counts.live, counts.upcoming]);

  const routeTab = route.kind === "list" ? route.tab : null;
  const tab: MatchState =
    routeTab === "live" || routeTab === "upcoming" || routeTab === "past"
      ? routeTab
      : (chosenTab ?? "live");

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
  // the tab); step back to the last page that still has something on it.
  const pastTotal = past.data?.total ?? 0;
  useEffect(() => {
    if (pastOffset > 0 && pastOffset >= pastTotal && past.data) {
      setPastOffset(Math.max(0, Math.ceil(pastTotal / PAST_PAGE) - 1) * PAST_PAGE);
    }
  }, [pastOffset, pastTotal, past.data]);

  const inTab = useMemo(
    () => (overview.data?.matches ?? []).filter((m) => m.state === tab),
    [overview.data, tab],
  );
  const visible = useMemo(
    () => (isPast ? (past.data?.matches ?? []) : sortMatches(filterMatches(inTab, filters), filters.sort, tab)),
    [isPast, past.data, inTab, filters, tab],
  );

  const tabTotal = isPast ? counts.past : inTab.length;
  const shown = isPast ? pastTotal : visible.length;
  const summary =
    tabTotal === 0 || (isPast && !past.data)
      ? ""
      : shown === tabTotal
        ? `${tabTotal} match${tabTotal === 1 ? "" : "es"}`
        : `${shown} of ${tabTotal}`;
  const current = isPast ? past : overview;

  const changeFilters = (next: FilterState) => {
    setFilters(next);
    setPastOffset(0);
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
        tournaments={overview.data?.tournaments ?? []}
        value={filters}
        onChange={changeFilters}
        summary={summary}
      />
      <Tabs
        active={tab}
        counts={counts}
        onSelect={(next) => navigate({ kind: "list", tab: next })}
      />

      <main>
        {route.kind === "match" ? (
          <MatchDetail
            conditionId={route.id}
            palette={palette}
            version={version}
            onBack={() => navigate({ kind: "list", tab })}
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
                  setPastOffset(offset);
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

function sortMatches(matches: MatchSummary[], sort: string, tab: MatchState): MatchSummary[] {
  const sorted = [...matches];
  switch (sort) {
    case "move":
      return sorted.sort((a, b) => Math.abs(b.move ?? 0) - Math.abs(a.move ?? 0));
    case "span":
      return sorted.sort((a, b) => captured(b) - captured(a));
    case "spread":
      return sorted.sort((a, b) => widestSpread(b) - widestSpread(a));
    default:
      // Finished matches read best newest-first; everything else soonest-first.
      return sorted.sort((a, b) =>
        tab === "past"
          ? (b.start_epoch ?? 0) - (a.start_epoch ?? 0)
          : (a.start_epoch ?? 0) - (b.start_epoch ?? 0),
      );
  }
}
