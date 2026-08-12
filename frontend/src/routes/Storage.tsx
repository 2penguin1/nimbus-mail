import * as api from "../api.ts";
import { bytes, exact, formatRatio, percent, savingRatio } from "../format.ts";
import { useAsync } from "../hooks.ts";
import { Bar } from "../components/Bar.tsx";

/* The savings dashboard. Three numbers from GET /v1/quota, and the gap between two of
 * them is the entire product.
 *
 * Forms, and why each one:
 *   - the saving is a HERO FIGURE, not a chart. One number a dashboard leads with.
 *   - logical vs physical is EMPHASIS (one hue plus context), not a pie and not two
 *     categorical colours: physical is a fraction of logical, not its rival.
 *   - quota is a METER — a single ratio against a limit — with the track a lighter
 *     step of the fill's own ramp so state reads across the whole bar.
 *   - the table is always visible, not behind a toggle. In a dense product the exact
 *     figures are part of the design, and it satisfies the accessibility rule that a
 *     chart always has a readable equivalent.
 *
 * No time series, because nothing stores one: there is no history of used_bytes and no
 * endpoint for it. A sparkline here would be invented data.
 */

export default function Storage() {
  const { data, error, loading, reload } = useAsync(() => api.getQuota(), []);

  if (loading) return <p className="empty">Loading…</p>;
  if (error)
    return (
      <div className="empty" role="alert">
        <p className="empty__title">Storage did not load.</p>
        <p>{error}</p>
        <button className="btn btn--bordered" onClick={reload}>
          Try again
        </button>
      </div>
    );
  if (!data) return null;

  const { logical_bytes, physical_bytes, quota_bytes } = data;
  const ratio = savingRatio(logical_bytes, physical_bytes);
  const used = percent(logical_bytes, quota_bytes);
  const tone = used >= 90 ? "crit" : used >= 75 ? "warn" : "accent";

  return (
    <div className="page">
      <div className="row-flex" style={{ justifyContent: "space-between" }}>
        <h1 className="page__title">Storage</h1>
        {/* Recomputed per call server-side — 56–70 ms on a 100k-message mailbox, with
            no cache. So: fetched once, refreshed on request, never polled. */}
        <button className="btn btn--bordered" onClick={reload}>
          Refresh
        </button>
      </div>

      <section className="card" style={{ marginTop: 24 }}>
        <p className="eyebrow">Deduplication</p>

        {ratio ? (
          <>
            <p className="hero" style={{ marginTop: 12 }}>
              {formatRatio(ratio)}
            </p>
            <p style={{ marginTop: 8, color: "var(--ink-2)" }}>
              less disk than a normal mail server would have used
            </p>
            {/* Attached to the hero, not left to the caveats below the table. The big
                number is the most-read thing on the page and it slightly overstates
                DEDUPLICATION, because part of the gap is base64 expansion in the raw
                message. Disclosing that three screens further down is technically
                honest and practically not: a skimmer banks the hero and never reaches
                the correction. One line here costs the number nothing it has earned. */}
            <p style={{ marginTop: 4, color: "var(--ink-3)", fontSize: "var(--fs-2)" }}>
              Part of this gap is email encoding overhead, not deduplication —{" "}
              <a href="#caveats">what this figure does and does not count</a>.
            </p>
          </>
        ) : (
          <p style={{ marginTop: 12, color: "var(--ink-2)" }}>
            Not enough mail yet to show a saving. The figure appears once this mailbox
            holds a stored attachment.
          </p>
        )}

        {/* One shared axis, logical at 100%. The two bars are 2px apart — the surface
            gap is what separates them, not a border. */}
        <div className="bar-pair" style={{ marginTop: 28 }}>
          <Bar label="Mail you received" value={bytes(logical_bytes)} percent={100} />
          <Bar
            label="Bytes we actually stored"
            value={bytes(physical_bytes)}
            percent={percent(physical_bytes, logical_bytes)}
          />
        </div>

        <p style={{ marginTop: 20, color: "var(--ink-2)" }}>
          Every attachment is stored once. Forty people receiving one 25&nbsp;MB deck
          costs us 25&nbsp;MB, not 1&nbsp;GB.
        </p>
      </section>

      <section className="card" style={{ marginTop: 16 }}>
        <p className="eyebrow">Your mailbox</p>
        <div style={{ marginTop: 20 }}>
          <Bar
            /* "<1%", never a rounded-down "0%" — a mailbox that holds mail has not
               used nothing, and showing zero for a real figure is the same class of
               mistake as an invented total. */
            label={`${used > 0 && used < 1 ? "<1" : Math.round(used)}% of your ${bytes(quota_bytes)}`}
            value={bytes(logical_bytes)}
            percent={used}
            tone={tone}
            note={
              used >= 90
                ? "Nearly full. Nothing is rejected when you pass the limit — the mail still arrives."
                : used >= 75
                  ? "Filling up. Nothing is rejected when you pass the limit."
                  : "We bill what you received, not what we stored."
            }
          />
        </div>
      </section>

      <table className="table" style={{ marginTop: 24 }}>
        <caption className="sr-only">Exact storage figures in bytes</caption>
        <thead>
          <tr>
            <th scope="col">Figure</th>
            <th scope="col" style={{ textAlign: "right" }}>
              Bytes
            </th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td>Mail you received</td>
            <td className="num">{exact(logical_bytes)}</td>
          </tr>
          <tr>
            <td>Bytes we stored</td>
            <td className="num">{exact(physical_bytes)}</td>
          </tr>
          <tr>
            <td>Your limit</td>
            <td className="num">{exact(quota_bytes)}</td>
          </tr>
        </tbody>
      </table>

      {/* Both paragraphs are required, not decorative. HLD §11.1 says the two ways this
          contrast flatters us must stay stated, and the second keeps a reader from
          adding these numbers up across mailboxes — which would be wrong. */}
      <div className="stack" id="caveats" style={{ marginTop: 24 }}>
        <p className="caveat">
          Two caveats, both real. The received figure counts the whole raw email
          including base64 encoding — about 1.37× a file's true size — so part of this
          gap is encoding, not deduplication. It also excludes the 7-day raw message
          archive, which is not deduplicated at all.
        </p>
        <p className="caveat">
          The stored figure is yours alone and cannot be added up across mailboxes.
          Three people sharing one 25&nbsp;MB file each see 25&nbsp;MB — and we stored
          25&nbsp;MB, not 75.
        </p>
      </div>
    </div>
  );
}
