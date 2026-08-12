import type { ReactNode } from "react";
import type { Paged } from "../hooks.ts";
import type { MessageRow as Row } from "../types.ts";
import { MessageRow } from "./MessageRow.tsx";

/* The list plus its four states. "Load older" is a button, not infinite scroll:
 * the API returns `next_cursor` and nothing else, so there is no total to show
 * progress against, and auto-loading on scroll breaks the back button and strands
 * keyboard users at a footer that keeps moving.
 */

export function MessageList({
  paged,
  href,
  selectedId,
  selectedCopy,
  ownMailboxId,
  showFolder,
  empty,
}: {
  paged: Paged<Row>;
  href: (row: Row) => string;
  selectedId?: string;
  /** Which COPY is open. A message id does not identify a row (HLD §10.3), so without
   *  this both copies of a dual-delivered message light up as current. */
  selectedCopy?: string | null;
  ownMailboxId: string | null;
  showFolder?: boolean;
  empty: ReactNode;
}) {
  if (paged.loading) {
    return (
      <p className="empty" role="status">
        Loading…
      </p>
    );
  }

  // Only take over the screen when there is nothing else to show. A failed "Load older"
  // used to replace fifty successfully loaded rows with this panel, and "Try again"
  // then refetched from page one — the user lost their place to a transient error on a
  // page they had not read yet.
  if (paged.error && paged.items.length === 0) {
    return (
      <div className="empty" role="alert">
        <p className="empty__title">That did not load.</p>
        <p>{paged.error}</p>
        <button className="btn btn--bordered" onClick={paged.reload}>
          Try again
        </button>
      </div>
    );
  }

  if (paged.items.length === 0) return <>{empty}</>;

  return (
    <>
      {/* j/k moves focus between rows. Ten lines on top of links that already work,
          rather than a listbox that would have to re-implement Tab and Enter. */}
      <ul
        className="list"
        onKeyDown={(e) => {
          if (e.key !== "j" && e.key !== "k") return;
          const links = Array.from(e.currentTarget.querySelectorAll("a.row"));
          const i = links.indexOf(document.activeElement as Element);
          const next = links[i + (e.key === "j" ? 1 : -1)];
          if (next instanceof HTMLElement) {
            e.preventDefault();
            next.focus();
          }
        }}
      >
        {paged.items.map((row) => (
          <MessageRow
            key={`${row.id}:${row.mailbox_id}`}
            row={row}
            to={href(row)}
            selected={
              row.id === selectedId && (!selectedCopy || row.mailbox_id === selectedCopy)
            }
            ownMailboxId={ownMailboxId}
            showFolder={showFolder}
          />
        ))}
      </ul>

      {paged.error && (
        <p className="error" role="alert" style={{ padding: "8px 12px" }}>
          {paged.error}
        </p>
      )}

      {paged.cursor && (
        <div className="list__more">
          <button
            className="btn btn--bordered"
            onClick={paged.loadMore}
            disabled={paged.loadingMore}
          >
            {paged.loadingMore ? "Loading…" : "Load older"}
          </button>
        </div>
      )}
    </>
  );
}
