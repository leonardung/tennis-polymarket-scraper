/** A card sparkline: one series, no axes, no interaction.
 *
 * Deliberately not a Lightweight Charts instance. Every chart there owns a
 * canvas and a resize observer, and a tab can hold twenty cards; inline SVG for
 * a 92x24 decoration costs nothing and keeps the card a pure render. The real
 * charts -- price, spread, depth -- are Lightweight Charts.
 */
export function Sparkline({
  values,
  width = 92,
  height = 24,
}: {
  values: (number | null)[];
  width?: number;
  height?: number;
}) {
  const known: [number, number][] = [];
  values.forEach((value, index) => {
    if (value != null && Number.isFinite(value)) known.push([index, value]);
  });

  if (known.length < 2) {
    return <svg className="spark" width={width} height={height} aria-hidden="true" />;
  }

  const numbers = known.map(([, value]) => value);
  let lo = Math.min(...numbers);
  let hi = Math.max(...numbers);
  if (hi - lo < 0.02) {
    const mid = (hi + lo) / 2;
    lo = mid - 0.01;
    hi = mid + 0.01;
  }

  const span = Math.max(1, values.length - 1);
  const px = (index: number) => (index / span) * (width - 4) + 2;
  const py = (value: number) => height - 3 - ((value - lo) / (hi - lo)) * (height - 6);
  const path = `M${known.map(([i, v]) => `${px(i).toFixed(1)},${py(v).toFixed(1)}`).join("L")}`;
  const last = known[known.length - 1]!;

  return (
    <svg
      className="spark"
      viewBox={`0 0 ${width} ${height}`}
      width={width}
      height={height}
      aria-hidden="true"
      focusable="false"
    >
      <path
        d={path}
        fill="none"
        stroke="var(--series-1)"
        strokeWidth={1.5}
        strokeLinejoin="round"
        strokeLinecap="round"
      />
      <circle
        cx={px(last[0])}
        cy={py(last[1])}
        r={2.4}
        fill="var(--series-1)"
        stroke="var(--surface-1)"
        strokeWidth={1.5}
      />
    </svg>
  );
}
