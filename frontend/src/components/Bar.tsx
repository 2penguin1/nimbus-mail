/* One horizontal bar: a track, a fill, a label at each end.
 *
 * Both dashboard charts are this component. The mark spec: square at the baseline,
 * 4px rounded at the data end, and the value never sits inside the fill — the physical
 * bar is a sliver by design, and text clipped by its own mark is worse than no label.
 */

export function Bar({
  label,
  value,
  percent,
  tone = "accent",
  note,
}: {
  label: string;
  /** Already formatted. The bar does not know what a byte is. */
  value: string;
  percent: number;
  tone?: "accent" | "warn" | "crit";
  note?: string;
}) {
  return (
    <div className="bar">
      <div className="bar__head">
        <span>{label}</span>
        <span className="bar__value">{value}</span>
      </div>
      <div
        className="bar__track"
        role="img"
        aria-label={`${label}: ${value}, ${Math.round(percent)} percent of the scale`}
      >
        <div
          className={`bar__fill${tone === "warn" ? " bar__fill--warn" : tone === "crit" ? " bar__fill--crit" : ""}`}
          style={{ width: `${percent}%` }}
        />
      </div>
      {note && <p className="hint">{note}</p>}
    </div>
  );
}
