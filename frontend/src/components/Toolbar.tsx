import { useState } from "react";
import * as api from "../api.ts";
import { isSnoozed, until } from "../format.ts";
import type { Folder, MessageDetail } from "../types.ts";
import { ConfirmDialog } from "./ConfirmDialog.tsx";

/* Archive, trash, unread, snooze. Nothing that sends — there is no reply, forward or
 * compose anywhere in this product, and no disabled button pretending otherwise.
 *
 * Every call names the copy with `?mailbox_id=`. A message id does not identify a row
 * (HLD §10.3): one email can reach one reader twice, and deleting the wrong copy
 * destroys a team's only copy with no undo. The row we were opened from always knows
 * which copy it is, so the 409 branch should be unreachable from this UI.
 */

/** Local time from the picker -> an instant with an offset. `new Date("2026-08-11T09:00")`
 *  parses as local, and toISOString() always ends in "Z", so the server never has to
 *  guess a timezone — a naive string is a 422 by design. */
const toInstant = (localValue: string): string | null => {
  const d = new Date(localValue);
  return Number.isNaN(d.getTime()) ? null : d.toISOString();
};

function presets(): { label: string; at: Date }[] {
  const laterToday = new Date(Date.now() + 3 * 3600_000);
  const tomorrow = new Date();
  tomorrow.setDate(tomorrow.getDate() + 1);
  tomorrow.setHours(9, 0, 0, 0);
  const monday = new Date();
  monday.setDate(monday.getDate() + ((8 - monday.getDay()) % 7 || 7));
  monday.setHours(9, 0, 0, 0);
  return [
    { label: "Later today", at: laterToday },
    { label: "Tomorrow 09:00", at: tomorrow },
    { label: "Monday 09:00", at: monday },
  ];
}

export function Toolbar({
  message,
  mailboxId,
  onChanged,
  onDeleted,
}: {
  message: MessageDetail;
  mailboxId: string;
  onChanged: (patch: Partial<MessageDetail>) => void;
  onDeleted: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [snoozing, setSnoozing] = useState(false);
  const [custom, setCustom] = useState("");
  const snoozed = isSnoozed(message.snooze_until);

  async function patch(body: api.Patch) {
    setBusy(true);
    setError(null);
    try {
      await api.patchMessage(message.id, mailboxId, body);
      onChanged(body as Partial<MessageDetail>);
      setSnoozing(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : "That did not work");
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    setBusy(true);
    setError(null);
    try {
      await api.deleteMessage(message.id, mailboxId);
      setConfirming(false);
      onDeleted();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not delete");
      setConfirming(false);
    } finally {
      setBusy(false);
    }
  }

  const move = (folder: Folder) => patch({ folder });

  return (
    <>
      <div className="topbar">
        {message.folder !== "archive" && (
          <button className="btn" onClick={() => move("archive")} disabled={busy}>
            Archive
          </button>
        )}
        {message.folder !== "inbox" && (
          <button className="btn" onClick={() => move("inbox")} disabled={busy}>
            Move to inbox
          </button>
        )}
        <button className="btn" onClick={() => patch({ is_read: !message.is_read })} disabled={busy}>
          Mark {message.is_read ? "unread" : "read"}
        </button>

        {snoozed ? (
          <button className="btn" onClick={() => patch({ snooze_until: null })} disabled={busy}>
            Un-snooze
          </button>
        ) : (
          <button className="btn" onClick={() => setSnoozing(true)} disabled={busy}>
            Snooze
          </button>
        )}

        <button
          className="btn btn--danger"
          onClick={() => setConfirming(true)}
          disabled={busy}
          style={{ marginLeft: "auto" }}
        >
          Delete
        </button>
      </div>

      {snoozed && message.snooze_until && (
        <p className="hint" style={{ padding: "8px 12px" }}>
          Hidden from the inbox until {until(message.snooze_until)}. Nothing runs to wake
          it — it simply reappears once that time passes.
        </p>
      )}

      {error && (
        <p className="error" role="alert" style={{ padding: "8px 12px" }}>
          {error}
        </p>
      )}

      <ConfirmDialog open={confirming} title="Delete this copy?" onClose={() => setConfirming(false)}>
        <p>
          This removes one copy — the one in this mailbox — and it cannot be undone. If
          this is a shared mailbox, it disappears for every member.
        </p>
        <p className="hint" style={{ marginTop: 8 }}>
          The attachment's bytes survive as long as any other mailbox still points at
          them.
        </p>
        <div className="dialog__actions">
          <button className="btn btn--bordered" onClick={() => setConfirming(false)}>
            Keep it
          </button>
          <button className="btn btn--bordered btn--danger" onClick={remove} disabled={busy}>
            Delete
          </button>
        </div>
      </ConfirmDialog>

      <ConfirmDialog open={snoozing} title="Snooze until" onClose={() => setSnoozing(false)}>
        <div className="stack">
          {presets().map((p) => (
            <button
              key={p.label}
              className="dialog__choice"
              onClick={() => patch({ snooze_until: p.at.toISOString() })}
              disabled={busy}
            >
              {p.label}
              <span className="hint" style={{ display: "block" }}>
                {p.at.toLocaleString()}
              </span>
            </button>
          ))}

          <div className="field">
            <label htmlFor="snooze-custom">Or pick a time</label>
            {/* Native picker — no date library. The browser already localises it. */}
            <input
              id="snooze-custom"
              className="input"
              type="datetime-local"
              value={custom}
              onChange={(e) => setCustom(e.target.value)}
            />
          </div>
        </div>
        <div className="dialog__actions">
          <button className="btn btn--bordered" onClick={() => setSnoozing(false)}>
            Cancel
          </button>
          <button
            className="btn btn--primary"
            disabled={busy || !toInstant(custom)}
            onClick={() => {
              const at = toInstant(custom);
              if (at) patch({ snooze_until: at });
            }}
          >
            Snooze
          </button>
        </div>
      </ConfirmDialog>
    </>
  );
}
