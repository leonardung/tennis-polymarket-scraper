import type { ReactNode } from "react";
import { fmtRelative } from "../format";
import type { MatchState, Overview } from "../types";

export function TopBar({
  overview,
  error,
  isDark,
  onToggleTheme,
}: {
  overview: Overview | null;
  error: string | null;
  isDark: boolean;
  onToggleTheme: () => void;
}) {
  let className = "status";
  let text = "connecting…";

  if (error) {
    className = "status is-error";
    text = error;
  } else if (overview?.capturing) {
    className = "status is-live";
    text = `capturing · last tick ${fmtRelative(overview.last_tick)}`;
  } else if (overview) {
    className = "status is-stale";
    text = overview.last_tick != null ? `idle · last tick ${fmtRelative(overview.last_tick)}` : "no snapshots yet";
  }

  return (
    <header className="topbar">
      <div className="brand">
        <span className="brand-mark" aria-hidden="true" />
        <h1>Tennis order books</h1>
        <span className="brand-sub">Polymarket capture</span>
      </div>
      <div className="topbar-right">
        <div className={className} role="status" aria-live="polite">
          <span className="status-dot" aria-hidden="true" />
          <span className="status-text">{text}</span>
        </div>
        <button
          type="button"
          className="ghost-btn"
          onClick={onToggleTheme}
          aria-label={`Switch to ${isDark ? "light" : "dark"} theme`}
        >
          <span aria-hidden="true">{isDark ? "☀" : "☾"}</span>
        </button>
      </div>
    </header>
  );
}

export interface FilterState {
  tour: string;
  tournament: string;
  search: string;
  sort: string;
}

export function Filters({
  tours,
  tournaments,
  value,
  onChange,
  summary,
}: {
  tours: string[];
  tournaments: string[];
  value: FilterState;
  onChange: (next: FilterState) => void;
  summary: string;
}) {
  return (
    <div className="filters" role="search">
      {/* Only worth a control once both draws are in the capture; with one tour
          it would be a select with a single choice in it. */}
      {tours.length > 1 && (
        <label className="field">
          <span className="field-label">Tour</span>
          <select
            value={value.tour}
            onChange={(e) => onChange({ ...value, tour: e.target.value })}
          >
            <option value="">All</option>
            {tours.map((tour) => (
              <option key={tour} value={tour}>
                {tour.toUpperCase()}
              </option>
            ))}
          </select>
        </label>
      )}

      <label className="field">
        <span className="field-label">Tournament</span>
        <select
          value={value.tournament}
          onChange={(e) => onChange({ ...value, tournament: e.target.value })}
        >
          <option value="">All</option>
          {tournaments.map((name) => (
            <option key={name} value={name}>
              {name}
            </option>
          ))}
        </select>
      </label>

      <label className="field field-grow">
        <span className="field-label">Player or match</span>
        <input
          type="search"
          placeholder="e.g. Zverev"
          autoComplete="off"
          value={value.search}
          onChange={(e) => onChange({ ...value, search: e.target.value })}
        />
      </label>

      <label className="field">
        <span className="field-label">Sort</span>
        <select value={value.sort} onChange={(e) => onChange({ ...value, sort: e.target.value })}>
          <option value="natural">Start time</option>
          <option value="move">Biggest move</option>
          <option value="snapshots">Most data</option>
          <option value="spread">Widest spread</option>
        </select>
      </label>

      <div className="filter-note">{summary}</div>
    </div>
  );
}

const TABS: { key: MatchState; label: string }[] = [
  { key: "live", label: "Live" },
  { key: "upcoming", label: "Upcoming" },
  { key: "past", label: "Past" },
];

export function Tabs({
  active,
  counts,
  onSelect,
}: {
  active: MatchState;
  counts: Record<MatchState, number>;
  onSelect: (tab: MatchState) => void;
}) {
  return (
    <nav className="tabs" role="tablist" aria-label="Match state">
      {TABS.map(({ key, label }) => (
        <button
          key={key}
          type="button"
          role="tab"
          className="tab"
          aria-selected={active === key}
          onClick={() => onSelect(key)}
        >
          <span className="tab-name">{label}</span>
          <span className="tab-count">{counts[key] ?? 0}</span>
        </button>
      ))}
    </nav>
  );
}

export function Panel({
  title,
  hint,
  action,
  children,
  note,
}: {
  title: string;
  hint?: ReactNode;
  action?: ReactNode;
  children: ReactNode;
  note?: ReactNode;
}) {
  return (
    <section className="panel">
      <div className="panel-head">
        <h3>{title}</h3>
        {action ?? (hint ? <span className="hint">{hint}</span> : null)}
      </div>
      {children}
      {note && <p className="note">{note}</p>}
    </section>
  );
}

export function Segmented<T extends string | number>({
  label,
  options,
  value,
  onChange,
}: {
  label: string;
  options: { value: T; label: string }[];
  value: T;
  onChange: (value: T) => void;
}) {
  return (
    <div className="seg" role="group" aria-label={label}>
      {options.map((option) => (
        <button
          key={String(option.value)}
          type="button"
          aria-pressed={value === option.value}
          onClick={() => onChange(option.value)}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}

export function StatTile({
  label,
  value,
  sub,
  swatch,
  small,
}: {
  label: string;
  value: string;
  sub?: string;
  swatch?: number;
  small?: boolean;
}) {
  return (
    <div className="tile">
      <div className="tile-label">
        {swatch !== undefined && <span className={`swatch swatch-${swatch}`} />}
        <span>{label}</span>
      </div>
      <div className={small ? "tile-value small" : "tile-value"}>{value}</div>
      {sub && <div className="tile-sub">{sub}</div>}
    </div>
  );
}

export function Legend({ items }: { items: { name: string; color: string; dashed?: boolean }[] }) {
  return (
    <div className="legend">
      {items.map((item) => (
        <span className="legend-item" key={item.name}>
          <span
            className="legend-key"
            style={
              item.dashed
                ? { background: "none", borderTop: `2px dotted ${item.color}`, height: 0 }
                : { background: item.color }
            }
          />
          <span>{item.name}</span>
        </span>
      ))}
    </div>
  );
}
