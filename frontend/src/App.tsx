import { useEffect, useMemo, useState } from "react";
import { fetchOverview } from "./api";
import { Filters, type FilterState, Tabs, TopBar } from "./components/Chrome";
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

export function App() {
  const { palette, isDark, toggle } = useTheme();
  const version = useDataVersion();
  const [route, navigate] = useRoute();
  const overview = useAsync((signal) => fetchOverview(signal), [version]);
  useTicker();

  const [filters, setFilters] = useState<FilterState>({
    tournament: "",
    search: "",
    sort: "natural",
  });
  // Chosen once, when the first payload says which tabs have anything in them;
  // re-picking on every poll would move the ground under the reader.
  const [chosenTab, setChosenTab] = useState<MatchState | null>(null);

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

  const inTab = useMemo(
    () => (overview.data?.matches ?? []).filter((m) => m.state === tab),
    [overview.data, tab],
  );
  const visible = useMemo(() => sortMatches(filterMatches(inTab, filters), filters.sort, tab), [
    inTab,
    filters,
    tab,
  ]);

  const summary =
    inTab.length === 0
      ? ""
      : visible.length === inTab.length
        ? `${inTab.length} match${inTab.length === 1 ? "" : "es"}`
        : `${visible.length} of ${inTab.length}`;

  return (
    <>
      <TopBar
        overview={overview.data}
        error={overview.error}
        isDark={isDark}
        onToggleTheme={toggle}
      />
      <Filters
        tournaments={overview.data?.tournaments ?? []}
        value={filters}
        onChange={setFilters}
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
            <div className={overview.refetching ? "cards is-refetching" : "cards"}>
              {visible.map((match) => (
                <MatchCard
                  key={match.condition_id}
                  match={match}
                  onOpen={() => navigate({ kind: "match", id: match.condition_id })}
                />
              ))}
            </div>
            {visible.length === 0 && (
              <p className="empty">
                {overview.error
                  ? `Cannot read the capture database — ${overview.error}`
                  : !overview.data
                    ? "Loading…"
                    : inTab.length
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
    if (filters.tournament && match.tournament !== filters.tournament) return false;
    if (!needle) return true;
    return [match.question, ...match.players].join(" ").toLowerCase().includes(needle);
  });
}

function widestSpread(match: MatchSummary): number {
  const spreads = match.prices.map((p) => p?.spread).filter((s): s is number => s != null);
  return spreads.length ? Math.max(...spreads) : -1;
}

function sortMatches(matches: MatchSummary[], sort: string, tab: MatchState): MatchSummary[] {
  const sorted = [...matches];
  switch (sort) {
    case "move":
      return sorted.sort((a, b) => Math.abs(b.move ?? 0) - Math.abs(a.move ?? 0));
    case "snapshots":
      return sorted.sort((a, b) => b.snapshots - a.snapshots);
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
