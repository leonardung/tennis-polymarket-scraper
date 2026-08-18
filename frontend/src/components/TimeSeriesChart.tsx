import {
  createChart,
  createSeriesMarkers,
  CrosshairMode,
  LineSeries,
  LineStyle,
  type IChartApi,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type LineData,
  type LogicalRange,
  type MouseEventParams,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
  type WhitespaceData,
} from "lightweight-charts";
import { useEffect, useMemo, useRef, useState } from "react";
import type { ChartPalette } from "../theme";

export interface ChartSeries {
  key: string;
  name: string;
  color: string;
  values: (number | null)[];
  dashed?: boolean;
  width?: number;
}

export interface ChartMarker {
  ts: number;
  label: string;
  major: boolean;
}

export interface TimeSeriesChartProps {
  ts: number[];
  series: ChartSeries[];
  palette: ChartPalette;
  height?: number;
  /** Decimal places on the price axis and in the tooltip. */
  precision?: number;
  minMove?: number;
  format?: (value: number) => string;
  markers?: ChartMarker[];
  /** Text for the tooltip's extra line, e.g. the score at that moment. */
  annotate?: (ts: number) => string | null;
  refLine?: { price: number; label: string };
  scale?: ScaleHint;
  /** Refit the viewport whenever this changes, and go back to following new
   *  data. While it does not change, the reader owns the viewport: see the data
   *  effect. Pass something identifying the window being shown, not the window
   *  that was last requested. */
  fitKey?: string;
  emptyText?: string;
  ariaLabel?: string;
}

interface TooltipRow {
  name: string;
  color: string;
  text: string;
}

interface TooltipState {
  x: number;
  y: number;
  time: number;
  rows: TooltipRow[];
  note: string | null;
}

/** Lightweight Charts expects whole, strictly ascending seconds. */
function toTime(ts: number): UTCTimestamp {
  return Math.round(ts) as UTCTimestamp;
}

/** Most points a resampled grid may hold. */
const MAX_SLOTS = 2000;

export interface Uniform {
  grid: number[];
  values: (number | null)[][];
}

/** Resample onto an evenly spaced time grid, carrying the last reading forward.
 *
 * Lightweight Charts spaces points by index, not by time -- that is right for
 * daily bars and wrong here, where a match is sampled every 10 seconds while it
 * is being played and every 5 minutes while it is not. Fed the raw rows, nine
 * quiet hours take as much width as ten live minutes and the shape of the match
 * disappears.
 *
 * Forward filling is not a smoothing choice: the capture only writes when the
 * book moves, so a slot with no row means the previous book still stood. A
 * stored null -- a side with no offers at all -- carries forward as a null, and
 * becomes whitespace, because that is an absence of quotes rather than an
 * absence of data.
 */
export function toUniformGrid(ts: number[], columns: (number | null)[][]): Uniform {
  // Plain numbers inside; the UTCTimestamp brand is applied at the chart edge.
  if (ts.length < 2) return { grid: ts.map((t) => Math.round(t)), values: columns };

  const first = Math.round(ts[0]!);
  const last = Math.round(ts[ts.length - 1]!);
  const span = Math.max(1, last - first);
  const target = Math.min(MAX_SLOTS, Math.max(200, ts.length * 2));
  const step = Math.max(1, Math.round(span / target));

  const grid: number[] = [];
  for (let time = first; time < last; time += step) grid.push(time);
  grid.push(last);

  const values = columns.map(() => new Array<number | null>(grid.length).fill(null));
  let cursor = 0;
  for (let slot = 0; slot < grid.length; slot++) {
    while (cursor + 1 < ts.length && toTime(ts[cursor + 1]!) <= grid[slot]!) cursor++;
    const reached = toTime(ts[cursor]!) <= grid[slot]!;
    for (let c = 0; c < columns.length; c++) {
      values[c]![slot] = reached ? (columns[c]![cursor] ?? null) : null;
    }
  }
  return { grid, values };
}

/** A null is a gap -- no quote, or nothing captured yet -- so it becomes
 *  whitespace rather than being interpolated across or dropped to zero. */
function toLineData(grid: number[], values: (number | null)[]): (LineData<Time> | WhitespaceData<Time>)[] {
  const out: (LineData<Time> | WhitespaceData<Time>)[] = [];
  let previous: number | null = null;
  for (let i = 0; i < grid.length; i++) {
    const time = grid[i]! as UTCTimestamp;
    if (previous !== null && time <= previous) continue; // strictly ascending
    previous = time;
    const value = values[i];
    out.push(value == null || !Number.isFinite(value) ? { time } : { time, value });
  }
  return out;
}

export interface Viewport {
  /** The stretch of time on screen, in seconds. */
  from: number;
  to: number;
  /** True when that is the whole of the data: nobody has zoomed or panned. */
  whole: boolean;
}

/** Where the chart is looking, read out of grid slots and into seconds.
 *
 * Lightweight Charts holds the viewport by index. That is stable while the data
 * is, and a poll is exactly when it is not: the window slides, `toUniformGrid`
 * picks a new step, and the same indices become a different stretch of the
 * match. Seconds survive that; slots do not.
 */
export function viewport(chart: IChartApi, grid: number[]): Viewport | null {
  if (grid.length < 2) return null;
  const scale = chart.timeScale();
  const logical = scale.getVisibleLogicalRange();
  if (!logical) return null;
  const first = grid[0]!;
  const last = grid[grid.length - 1]!;
  const step = (last - first) / (grid.length - 1);

  // "The whole window" is measured against getVisibleRange, which is clamped to
  // the data: a fit leaves empty slots at each edge and the logical range counts
  // them, so comparing logical ranges would read a fitted chart as zoomed out.
  const shown = scale.getVisibleRange();
  const whole =
    !shown || ((shown.from as number) <= first + step && (shown.to as number) >= last - step);
  return { from: first + logical.from * step, to: first + logical.to * step, whole };
}

/** Put a viewport back after the grid underneath it has been rebuilt. */
export function restoreViewport(chart: IChartApi, grid: number[], view: Viewport): void {
  if (grid.length < 2) return;
  const first = grid[0]!;
  const step = (grid[grid.length - 1]! - first) / (grid.length - 1);
  if (!(step > 0)) return;
  const from = (view.from - first) / step;
  const to = (view.to - first) / step;
  if (!Number.isFinite(from) || !Number.isFinite(to) || to <= from) return;
  chart.timeScale().setVisibleLogicalRange({ from, to });
}

export interface ScaleHint {
  /** Anchor the axis at zero: spread and depth cannot go below it. */
  zeroBased?: boolean;
  /** Smallest range the axis may show, so noise does not fill the plot. */
  minSpan?: number;
  clamp?: [number, number];
}

function computeScale(
  columns: (number | null)[][],
  hint: ScaleHint,
): { minValue: number; maxValue: number } | null {
  let lo = Infinity;
  let hi = -Infinity;
  for (const column of columns) {
    for (const value of column) {
      if (value == null || !Number.isFinite(value)) continue;
      if (value < lo) lo = value;
      if (value > hi) hi = value;
    }
  }
  if (lo > hi) return null;

  const dataLo = lo;
  if (hint.zeroBased) lo = Math.min(0, lo);
  if (hint.minSpan && hi - lo < hint.minSpan) {
    const mid = (hi + lo) / 2;
    lo = mid - hint.minSpan / 2;
    hi = mid + hint.minSpan / 2;
  }
  if (hi === lo) hi = lo + (hint.minSpan || 1);

  const pad = (hi - lo) * 0.08;
  lo -= pad;
  hi += pad;
  if (hint.clamp) {
    lo = Math.max(hint.clamp[0], lo);
    hi = Math.min(hint.clamp[1], hi);
  }
  // Padding must not invent a negative axis under a quantity that has none.
  if (hint.zeroBased) lo = Math.min(0, dataLo);

  return { minValue: lo, maxValue: hi };
}

export function TimeSeriesChart({
  ts,
  series,
  palette,
  height = 260,
  precision = 3,
  minMove = 0.001,
  format,
  markers,
  annotate,
  refLine,
  scale,
  fitKey = "",
  emptyText = "No snapshots in this window yet.",
  ariaLabel,
}: TimeSeriesChartProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef(new Map<string, ISeriesApi<"Line">>());
  const markersRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null);
  const [tooltip, setTooltip] = useState<TooltipState | null>(null);

  const usable = ts.length >= 2 && series.some((s) => s.values.some((v) => v != null));
  const formatValue = useMemo(
    () => format ?? ((value: number) => value.toFixed(precision)),
    [format, precision],
  );

  const uniform = useMemo(
    () => toUniformGrid(ts, series.map((s) => s.values)),
    [ts, series],
  );

  // Seconds covered by what is actually on screen, read by the axis formatter,
  // which Lightweight Charts holds from the moment the chart is created. The
  // labels are drawn for the visible range, so it is that range -- not the
  // whole window -- which decides whether minutes tell them apart: zoomed into
  // one minute of an hour-long match every tick otherwise reads "10:50 PM".
  // Written straight from the pan handler rather than through React state, so
  // the formatter cannot lag a frame behind the axis it is labelling.
  const dataSpanRef = useRef(0);
  dataSpanRef.current =
    uniform.grid.length > 1 ? uniform.grid[uniform.grid.length - 1]! - uniform.grid[0]! : 0;
  const visibleSpanRef = useRef<number | null>(null);
  // The grid the pan handler measures against, which outlives any one render.
  const gridRef = useRef<number[]>([]);
  gridRef.current = uniform.grid;
  const shownSpan = () => visibleSpanRef.current ?? dataSpanRef.current;

  // Read through a ref by the autoscale callback, which Lightweight Charts holds
  // from the moment the series is created.
  const scaleRef = useRef<{ minValue: number; maxValue: number } | null>(null);
  scaleRef.current = useMemo(
    () => (scale ? computeScale(uniform.values, scale) : null),
    [uniform, scale],
  );

  // Kept in refs so the crosshair handler never needs re-subscribing (which
  // would mean tearing the chart down and losing the reader's zoom).
  const lookup = useRef({ series, annotate, formatValue });
  lookup.current = { series, annotate, formatValue };

  /* ---- create once ---- */
  useEffect(() => {
    const container = containerRef.current;
    if (!container || !usable) return;

    const chart = createChart(container, {
      autoSize: true,
      layout: {
        background: { color: "transparent" },
        textColor: palette["--text-muted"],
        fontFamily: 'system-ui, -apple-system, "Segoe UI", sans-serif',
        fontSize: 11,
        attributionLogo: false,
      },
      grid: {
        vertLines: { color: palette["--gridline"] },
        horzLines: { color: palette["--gridline"] },
      },
      rightPriceScale: {
        borderColor: palette["--baseline"],
        // With an explicit range the padding is already in it; leaving the
        // margins on top of that pushes the axis below zero on a quantity that
        // has no negative values, and labels a phantom "-0.000".
        scaleMargins: scale ? { top: 0, bottom: 0 } : { top: 0.12, bottom: 0.12 },
      },
      timeScale: {
        borderColor: palette["--baseline"],
        timeVisible: true,
        secondsVisible: false,
        // Axis labels in the reader's own timezone, matching every other time
        // on the page -- Lightweight Charts renders UTC unless told otherwise --
        // and to whatever precision the window actually needs. A match that has
        // just started spans a few minutes, where minute-resolution labels
        // repeat the same value across every tick.
        tickMarkFormatter: (time: Time) =>
          new Date((time as number) * 1000).toLocaleTimeString([], {
            hour: "2-digit",
            minute: "2-digit",
            ...(shownSpan() < 1800 ? { second: "2-digit" } : {}),
          }),
      },
      crosshair: {
        // Normal, not Magnet: with gaps in the data, magnet snaps the crosshair
        // onto a neighbouring point and misreports where the reader is pointing.
        mode: CrosshairMode.Normal,
        vertLine: { color: palette["--text-muted"], width: 1, style: LineStyle.Solid, labelVisible: true },
        horzLine: { color: palette["--text-muted"], width: 1, style: LineStyle.Solid, labelVisible: true },
      },
      handleScale: { axisPressedMouseMove: { time: true, price: false } },
      localization: {
        // The crosshair's own time badge, which defaults to UTC and would
        // disagree with the tooltip sitting a few pixels away from it.
        timeFormatter: (time: Time) =>
          new Date((time as number) * 1000).toLocaleString([], {
            month: "short",
            day: "numeric",
            hour: "2-digit",
            minute: "2-digit",
            second: "2-digit",
          }),
      },
    });
    chartRef.current = chart;
    fittedKey.current = null; // a new chart starts unfitted, whatever came before

    // Keep the axis formatter told what is on screen. The range comes back in
    // grid slots, so it is turned back into seconds through the grid itself,
    // which is the only thing that knows how wide a slot is.
    const onRange = (range: LogicalRange | null) => {
      const grid = gridRef.current;
      if (!range || grid.length < 2) {
        visibleSpanRef.current = null;
        return;
      }
      const step = (grid[grid.length - 1]! - grid[0]!) / (grid.length - 1);
      visibleSpanRef.current = Math.max(0, (range.to - range.from) * step);
    };
    chart.timeScale().subscribeVisibleLogicalRangeChange(onRange);

    const onMove = (param: MouseEventParams<Time>) => {
      if (!param.point || param.time == null) {
        setTooltip(null);
        return;
      }
      const { series: current, annotate: note, formatValue: fmt } = lookup.current;
      const rows: TooltipRow[] = current.map((definition) => {
        const api = seriesRef.current.get(definition.key);
        const point = api ? param.seriesData.get(api) : undefined;
        const value = point && "value" in point ? (point.value as number | undefined) : undefined;
        return {
          name: definition.name,
          color: definition.color,
          text: value == null ? "no quote" : fmt(value),
        };
      });
      const seconds = param.time as number;
      setTooltip({
        x: param.point.x,
        y: param.point.y,
        time: seconds,
        rows,
        note: note?.(seconds) ?? null,
      });
    };

    chart.subscribeCrosshairMove(onMove);
    return () => {
      chart.unsubscribeCrosshairMove(onMove);
      chart.remove();
      chartRef.current = null;
      seriesRef.current.clear();
      markersRef.current = null;
    };
    // Palette is applied by its own effect below; recreating on a theme switch
    // would throw away the zoom.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [usable]);

  /* ---- series set ---- */
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;

    const wanted = new Set(series.map((s) => s.key));
    for (const [key, api] of seriesRef.current) {
      if (!wanted.has(key)) {
        chart.removeSeries(api);
        seriesRef.current.delete(key);
      }
    }
    for (const definition of series) {
      if (seriesRef.current.has(definition.key)) continue;
      const api = chart.addSeries(LineSeries, {
        color: definition.color,
        lineWidth: (definition.width ?? 2) as 1 | 2 | 3 | 4,
        lineStyle: definition.dashed ? LineStyle.Dotted : LineStyle.Solid,
        priceLineVisible: false,
        lastValueVisible: true,
        crosshairMarkerVisible: true,
        crosshairMarkerRadius: 4,
        // A custom formatter also drives the axis labels, so share depths read
        // "120.0K" on the scale rather than "120000".
        priceFormat: format
          ? { type: "custom", formatter: format, minMove }
          : { type: "price", precision, minMove },
        autoscaleInfoProvider: () =>
          scaleRef.current ? { priceRange: scaleRef.current } : null,
      });
      seriesRef.current.set(definition.key, api);
    }
  }, [series, precision, minMove, format]);

  /* ---- data ---- */
  const fittedKey = useRef<string | null>(null);
  // The grid the chart is currently drawing. It is what the viewport on screen
  // is expressed in, so it cannot be read off `uniform`: by the time this effect
  // runs that is already the incoming grid.
  const drawnGrid = useRef<number[]>([]);
  useEffect(() => {
    const chart = chartRef.current;
    // Read where the reader is looking before the new data moves the ground
    // under them.
    const before = chart ? viewport(chart, drawnGrid.current) : null;

    series.forEach((definition, index) => {
      const column = uniform.values[index];
      if (column) seriesRef.current.get(definition.key)?.setData(toLineData(uniform.grid, column));
    });
    drawnGrid.current = uniform.grid;
    if (!chart) return;

    // Fit on the first data, and again whenever the window itself changes --
    // picking a new range should put the reader back at a full view even if
    // they had panned away. A poll that appends to the same window leaves
    // fitKey alone.
    if (fittedKey.current !== fitKey) {
      visibleSpanRef.current = null; // refitting puts the whole window back on screen
      chart.timeScale().fitContent();
      fittedKey.current = fitKey;
      return;
    }

    // A poll, then. Following the new data is only what the reader wants while
    // they are still looking at the whole window -- which is where picking a
    // range leaves them, and is the only view a refit could be said to
    // preserve. Once they have zoomed or panned into part of it, that part is
    // what they asked to see: hold it exactly, in seconds, and let the new
    // samples arrive off to the right until they scroll to them.
    if (!before || before.whole) {
      visibleSpanRef.current = null;
      chart.timeScale().fitContent();
    } else {
      restoreViewport(chart, uniform.grid, before);
    }
  }, [uniform, series, fitKey]);

  /* ---- score markers ---- */
  useEffect(() => {
    const first = series[0] && seriesRef.current.get(series[0].key);
    if (!first) return;
    const points: SeriesMarker<Time>[] = (markers ?? []).map((marker) => ({
      time: toTime(marker.ts),
      position: "aboveBar",
      shape: "arrowDown",
      color: marker.major ? palette["--text-secondary"] : palette["--baseline"],
      size: marker.major ? 1 : 0.6,
      text: marker.major ? marker.label : "",
    }));
    if (!markersRef.current) markersRef.current = createSeriesMarkers(first, points);
    else markersRef.current.setMarkers(points);
  }, [markers, series, palette]);

  /* ---- reference line ---- */
  useEffect(() => {
    const first = series[0] && seriesRef.current.get(series[0].key);
    if (!first || !refLine) return;
    const line = first.createPriceLine({
      price: refLine.price,
      color: palette["--baseline"],
      lineWidth: 1,
      lineStyle: LineStyle.Solid,
      axisLabelVisible: false,
      title: refLine.label,
    });
    return () => {
      try {
        first.removePriceLine(line);
      } catch {
        /* the series is already gone with the chart */
      }
    };
  }, [refLine, series, palette]);

  /* ---- theme ---- */
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    chart.applyOptions({
      layout: { textColor: palette["--text-muted"] },
      grid: {
        vertLines: { color: palette["--gridline"] },
        horzLines: { color: palette["--gridline"] },
      },
      rightPriceScale: { borderColor: palette["--baseline"] },
      timeScale: { borderColor: palette["--baseline"] },
      crosshair: {
        vertLine: { color: palette["--text-muted"] },
        horzLine: { color: palette["--text-muted"] },
      },
    });
    for (const definition of series) {
      seriesRef.current.get(definition.key)?.applyOptions({ color: definition.color });
    }
  }, [palette, series]);

  if (!usable) return <p className="chart-empty">{emptyText}</p>;

  return (
    <div className="chart-wrap" style={{ height }}>
      <div
        ref={containerRef}
        className="chart-canvas"
        role="img"
        aria-label={ariaLabel ?? "time series chart"}
      />
      {tooltip && <ChartTooltip state={tooltip} height={height} />}
    </div>
  );
}

function ChartTooltip({ state, height }: { state: TooltipState; height: number }) {
  // Flipped to whichever side of the crosshair has room, so it never covers the
  // point being read.
  const flip = state.x > 320;
  return (
    <div
      className="chart-tooltip"
      style={{
        left: flip ? undefined : state.x + 16,
        right: flip ? `calc(100% - ${state.x - 16}px)` : undefined,
        top: Math.max(4, Math.min(height - 110, state.y - 12)),
      }}
    >
      <div className="tt-when">
        {new Date(state.time * 1000).toLocaleTimeString([], {
          hour: "2-digit",
          minute: "2-digit",
          second: "2-digit",
        })}
      </div>
      {state.rows.map((row) => (
        <div className="tt-row" key={row.name}>
          <span className="tt-key" style={{ background: row.color }} />
          <span className="tt-name">{row.name}</span>
          <span className="tt-value">{row.text}</span>
        </div>
      ))}
      {state.note && <div className="tt-note">{state.note}</div>}
    </div>
  );
}
