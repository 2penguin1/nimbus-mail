import { Link } from "react-router";
import type { MessageRow as Row } from "../types.ts";
import { bytes, isSnoozed, sender, until, when } from "../format.ts";

/* One list row, shared by the inbox and by search — both endpoints return the same
 * shape, so two components would be two places to fix a rendering bug.
 *
 * It is a real <a>, not a div with onClick. Tab and Enter work with no roving-tabindex
 * code, the row is middle-clickable and deep-linkable, and a screen reader announces it
 * as a link. A custom listbox would be more code and less accessible.
 */

export function MessageRow({
  row,
  to,
  selected,
  ownMailboxId,
  showFolder,
}: {
  row: Row;
  to: string;
  selected?: boolean;
  /** This session's own mailbox id. Anything else is a shared mailbox's copy. */
  ownMailboxId: string | null;
  /** Search spans every folder, so a hit in trash must be labelled, not hidden. */
  showFolder?: boolean;
}) {
  const unread = !row.is_read;
  const shared = ownMailboxId !== null && row.mailbox_id !== ownMailboxId;
  const snoozed = isSnoozed(row.snooze_until);

  return (
    <li>
      <Link
        to={to}
        className={`row${unread ? " row--unread" : ""}`}
        aria-current={selected ? "true" : undefined}
      >
        <span className="row__dot" style={{ visibility: unread ? "visible" : "hidden" }}>
          <span className="sr-only">Unread</span>
        </span>

        {/* The display name only. `title` keeps the full header one hover away — see
         * format.sender for why the address is demoted rather than shown. */}
        <span className="row__from" title={row.from ?? undefined}>
          {sender(row.from)}
        </span>

        <span className="row__main">
          <span className="row__subject">{row.subject || "(no subject)"}</span>
          {shared && (
            /* We can flag that this copy lives in another mailbox, but we cannot name
             * it: no endpoint maps a mailbox id to an address. Showing six characters
             * of the id is the honest version of "via support@" — a fabricated name
             * would be worse than an ugly one. */
            <span className="chip mono" title={`Shared mailbox ${row.mailbox_id}`}>
              shared · {row.mailbox_id.slice(0, 6)}
            </span>
          )}
          {snoozed && row.snooze_until && (
            <span className="chip chip--accent">Until {until(row.snooze_until)}</span>
          )}
          {showFolder && row.folder !== "inbox" && <span className="chip">{row.folder}</span>}
        </span>

        <span className="row__meta">
          {row.has_attachments && (
            <span title="Has attachments" aria-label="Has attachments">
              ◫
            </span>
          )}
          <span>{bytes(row.size_bytes)}</span>
          <time dateTime={row.received_at}>{when(row.received_at)}</time>
        </span>
      </Link>
    </li>
  );
}
