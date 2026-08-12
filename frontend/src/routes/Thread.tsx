import { Link, useParams } from "react-router";
import * as api from "../api.ts";
import { bytes, when } from "../format.ts";
import { useAsync } from "../hooks.ts";

/* A conversation, oldest first — the opposite of the inbox, because a thread reads top
 * to bottom. The server already de-duplicates a message that reached this reader twice.
 *
 * Thread rows carry neither `mailbox_id` nor `has_attachments`, so there are no
 * per-copy actions and no paperclip here. Opening a message goes through the folder
 * route, which resolves the copy properly.
 */

export default function Thread() {
  const { threadId } = useParams();
  const { data, error, loading } = useAsync(() => api.getThread(threadId!), [threadId]);

  if (loading) return <p className="empty">Loading…</p>;
  if (error)
    return (
      <div className="empty" role="alert">
        <p className="empty__title">That conversation did not load.</p>
        <p>{error}</p>
      </div>
    );
  if (!data) return null;

  return (
    <div className="page">
      <h1 className="page__title">Conversation</h1>
      <p className="hint" style={{ marginTop: 4, marginBottom: 24 }}>
        {data.messages.length} message{data.messages.length === 1 ? "" : "s"} you can
        read. Parts of this thread you were not sent stay invisible.
      </p>

      <ul className="list">
        {data.messages.map((m) => (
          <li key={m.id}>
            {/* No `copy` param: a thread row does not carry a mailbox id. The detail
                endpoint resolves the copy itself, and answers 409 in the rare case
                where this reader holds two — which the message screen surfaces. */}
            <Link to={`/mail/${m.folder}/${m.id}`} className={`row${m.is_read ? "" : " row--unread"}`}>
              <span className="row__dot" style={{ visibility: m.is_read ? "hidden" : "visible" }} />
              <span className="row__from">{m.from ?? "(no sender)"}</span>
              <span className="row__main">
                <span className="row__subject">{m.subject || "(no subject)"}</span>
                {m.folder !== "inbox" && <span className="chip">{m.folder}</span>}
              </span>
              <span className="row__meta">
                <span>{bytes(m.size_bytes)}</span>
                <time dateTime={m.received_at}>{when(m.received_at)}</time>
              </span>
            </Link>
          </li>
        ))}
      </ul>
    </div>
  );
}
