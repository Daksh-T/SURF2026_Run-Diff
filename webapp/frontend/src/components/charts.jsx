// Hand-rolled chart primitives for Insights. Flat, dense, no chart libs.
// Pure presentational — props in, markup out, no fetching.

/** Clamp a ratio to [0, 1], treating null/NaN/Infinity as 0. */
function clampRatio(x) {
  if (x == null || !Number.isFinite(x)) return 0;
  if (x < 0) return 0;
  if (x > 1) return 1;
  return x;
}

/**
 * Horizontal bar row: label, a bar whose length is `ratio` (0..1) of the
 * track width, and a value label rendered past the bar end.
 *
 * - `danger` paints the bar in the red/danger token (used for the worst row).
 * - `onClick` makes the whole row a button (for drill-down navigation).
 */
export function HBar({ label, ratio, valueLabel, danger = false, onClick, title }) {
  const pct = clampRatio(ratio) * 100;
  const Tag = onClick ? "button" : "div";
  return (
    <Tag
      className={"hbar-row" + (onClick ? " hbar-row-btn" : "")}
      onClick={onClick}
      data-tip={title}
      type={onClick ? "button" : undefined}
    >
      <div className="hbar-label">{label}</div>
      <div className="hbar-track">
        <div
          className={"hbar-fill" + (danger ? " hbar-fill-danger" : "")}
          style={{ width: `${pct}%` }}
        />
      </div>
      <div className="hbar-value">{valueLabel}</div>
    </Tag>
  );
}

/**
 * Stacked horizontal bar: one segment per entry in `segments`
 * ([{ value, className, label }, ...]). Segment widths are proportional to
 * `value` within the row's own total (not a shared scale) — this chart is
 * about the L1/L2/L3 split per problem, not cross-row comparison of length.
 * `total` is rendered as the trailing value label.
 */
export function StackedHBar({ label, segments, total, onClick, title }) {
  const sum = segments.reduce((acc, s) => acc + (s.value || 0), 0);
  const Tag = onClick ? "button" : "div";
  return (
    <Tag
      className={"hbar-row" + (onClick ? " hbar-row-btn" : "")}
      onClick={onClick}
      data-tip={title}
      type={onClick ? "button" : undefined}
    >
      <div className="hbar-label">{label}</div>
      <div className="hbar-track stacked-track">
        {sum > 0 ? (
          segments.map((s, i) => {
            const w = clampRatio(s.value / sum) * 100;
            if (w <= 0) return null;
            return (
              <div
                key={i}
                className={"stacked-seg " + (s.className || "")}
                style={{ width: `${w}%` }}
                data-tip={`${s.label}: ${s.value}`}
              />
            );
          })
        ) : (
          <div className="stacked-seg stacked-seg-empty" style={{ width: "100%" }} />
        )}
      </div>
      <div className="hbar-value">{total}</div>
    </Tag>
  );
}

/** Small legend row for the stacked hint-level bars. */
export function StackedLegend({ items }) {
  return (
    <div className="stacked-legend">
      {items.map((it, i) => (
        <span className="stacked-legend-item" key={i}>
          <span className={"stacked-legend-swatch " + (it.className || "")} />
          {it.label}
        </span>
      ))}
    </div>
  );
}

/**
 * Compact vertical mini bar chart — used for "hint levels used" on a single
 * problem. `bars` is [{ label, value, className }]. Heights are scaled to the
 * max value among the bars (min 1 to avoid divide-by-zero).
 */
export function MiniBars({ bars, height = 90 }) {
  const max = Math.max(1, ...bars.map((b) => b.value || 0));
  return (
    <div className="minibars" style={{ height }}>
      {bars.map((b, i) => {
        const h = clampRatio((b.value || 0) / max) * 100;
        return (
          <div className="minibar-col" key={i} data-tip={b.title || undefined}>
            <div className="minibar-value">{b.value || 0}</div>
            <div className="minibar-track">
              <div className={"minibar-fill " + (b.className || "")} style={{ height: `${h}%` }} />
            </div>
            <div className="minibar-label">{b.label}</div>
          </div>
        );
      })}
    </div>
  );
}
