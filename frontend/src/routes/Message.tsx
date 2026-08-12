import { useEffect, useRef, useState } from "react";
import { Link } from "react-router";
import * as api from "../api.ts";
import { bytes, fullWhen } from "../format.ts";
import { useAsync } from "../hooks.ts";
import type { MessageDetail } from "../types.ts";
import { AttachmentChip } from "../components/AttachmentChip.tsx";
import { BodyView } from "../components/BodyView.tsx";
import { Toolbar } from "../components/Toolbar.tsx";

/* The reading pane. Rendered by Mail, but a screen in its own right.
 *
 * The detail response is authoritative for `is_read` and `folder` now that
 * `GET /v1/messages/{id}` takes `?mailbox_id=` and resolves the copy exactly like
 * PATCH and DELETE. Before that it was a bare `.first()` with no ORDER BY, and a
 * reader holding two copies got whichever row Postgres felt like returning.
 */

export function Message({
  messageId,
  mailboxId,
  backHref,
  backLabel,
  onChanged,
  onDeleted,
}: {
  messageId: string;
  /** From the list row. Null only on a deep link that did not carry one. */
  mailboxId: string | null;
  /** Where the phone-only back link goes. Passed in because Mail owns the folder
   *  and the snoozed flag; rebuilding that here would be a second place to get it
   *  wrong. */
  backHref: string;
  backLabel: string;
  onChanged: (id: string, mailboxId: string, patch: Partial<MessageDetail>) => void;
  onDeleted: (id: string, mailboxId: string) => void;
}) {
  const { data, error, loading } = useAsync(
    () => api.getMessage(messageId, mailboxId),
    [messageId, mailboxId],
  );
  const [local, setLocal] = useState<MessageDetail | null>(null);
  const markedRead = useRef<string | null>(null);

  useEffect(() => setLocal(data), [data]);

  const msg = local;
  // The copy the server resolved, preferred over the one in the URL. A message opened
  // from a thread arrives with no ?copy= at all, and the toolbar used to vanish and the
  // message stay unread for ever — the comment here described a fallback that did not
  // exist. GET /v1/messages/{id} now returns the row it resolved, so this is real.
  const copy = mailboxId ?? msg?.mailbox_id ?? null;

  // Opening a message marks it read — once per message, guarded by a ref because
  // StrictMode runs effects twice in development and this is a write.
  useEffect(() => {
    if (!msg || msg.is_read || !copy) return;
    const key = `${msg.id}:${copy}`;
    if (markedRead.current === key) return;
    markedRead.current = key;
    api
      .patchMessage(msg.id, copy, { is_read: true })
      .then(() => {
        setLocal((m) => (m ? { ...m, is_read: true } : m));
        onChanged(msg.id, copy, { is_read: true });
      })
      .catch(() => {
        /* A failed read-receipt must not interrupt reading the mail. */
      });
  }, [msg, copy, onChanged]);

  if (loading) return <p className="empty">Loading…</p>;

  if (error) {
    return (
      <div className="empty" role="alert">
        <p className="empty__title">That message did not open.</p>
        <p>{error}</p>
      </div>
    );
  }

  if (!msg) return null;

  return (
    <article>
      {/* Phone-only, because only a phone hides the list behind this pane
          (base.css: .split--detail-open .split__list). Without it the sole way back to
          the inbox is the browser's own back button — which is not a control the app
          gets to rely on, and leaves a reader feeling stuck in a message. A real <a>,
          so it behaves like every other navigation in the app. */}
      <Link className="msg__back" to={backHref}>
        &larr; Back to {backLabel}
      </Link>
      {copy && (
        <Toolbar
          message={msg}
          mailboxId={copy}
          onChanged={(patch) => {
            setLocal((m) => (m ? { ...m, ...patch } : m));
            onChanged(msg.id, copy, patch);
          }}
          onDeleted={() => onDeleted(msg.id, copy)}
        />
      )}

      <div className="msg">
        <h1 className="msg__subject">{msg.subject || "(no subject)"}</h1>

        {/* The machine column: everything the system recorded, in mono, aligned. */}
        <div className="msg__facts">
          <span className="msg__key">From</span>
          <span className="msg__val">{msg.from ?? "(no sender)"}</span>

          <span className="msg__key">Received</span>
          <span className="msg__val">{fullWhen(msg.received_at)}</span>

          {msg.sent_at && (
            <>
              <span className="msg__key">Sent</span>
              <span className="msg__val">{fullWhen(msg.sent_at)}</span>
            </>
          )}

          <span className="msg__key">Size</span>
          <span className="msg__val">{bytes(msg.size_bytes)}</span>

          <span className="msg__key">Folder</span>
          <span className="msg__val">{msg.folder}</span>

          {msg.thread_id && (
            <>
              <span className="msg__key">Thread</span>
              <span className="msg__val">
                <Link to={`/threads/${msg.thread_id}`}>View conversation</Link>
              </span>
            </>
          )}
        </div>

        {msg.attachments.length > 0 && (
          <section style={{ marginTop: 24 }}>
            <h2 className="rail__heading" style={{ marginTop: 0 }}>
              {msg.attachments.length} attachment{msg.attachments.length > 1 ? "s" : ""}
            </h2>
            <div className="stack">
              {msg.attachments.map((a) => (
                <AttachmentChip key={a.blob_hash} att={a} />
              ))}
            </div>
          </section>
        )}

        <BodyView text={msg.body_text} html={msg.body_html} />
      </div>
    </article>
  );
}
