import { useCallback } from "react";
import { Navigate, useNavigate, useParams, useSearchParams } from "react-router";
import * as api from "../api.ts";
import { useAuth } from "../auth.tsx";
import { usePaged } from "../hooks.ts";
import { FOLDERS, type Folder, type MessageRow } from "../types.ts";
import { MessageList } from "../components/MessageList.tsx";
import { Message } from "./Message.tsx";

/* List plus reading pane. Both are driven entirely by the URL:
 *   /mail/inbox                          the folder
 *   /mail/inbox/<id>?copy=<mailbox_id>   a message, and WHICH copy of it
 *   /mail/inbox?snoozed=1                the snoozed mail in that folder
 *
 * `copy` is in the query string rather than router state so a refresh or a shared link
 * still names the copy. Without it the server has to guess, and it answers 409 instead
 * of guessing — which is correct, and which the UI should never provoke.
 */

const isFolder = (v: string | undefined): v is Folder =>
  !!v && (FOLDERS as readonly string[]).includes(v);

export default function Mail() {
  const { folder, messageId } = useParams();
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const { session } = useAuth();

  const snoozed = params.get("snoozed") === "1";
  const copy = params.get("copy");

  const valid = isFolder(folder);
  const fetchPage = useCallback(
    (cursor: string | null) =>
      api.listMessages({ folder: folder as Folder, cursor, snoozed }),
    [folder, snoozed],
  );

  const paged = usePaged<MessageRow>(fetchPage, [folder, snoozed]);

  // Hooks must run before any early return, so the guard sits here rather than at the
  // top of the component.
  if (!valid) return <Navigate to="/mail/inbox" replace />;

  const href = (row: MessageRow) =>
    `/mail/${folder}/${row.id}?copy=${row.mailbox_id}${snoozed ? "&snoozed=1" : ""}`;

  const backToList = () => navigate(`/mail/${folder}${snoozed ? "?snoozed=1" : ""}`);

  return (
    <div className={`split${messageId ? " split--detail-open" : ""}`}>
      <div className="split__list">
        <div className="topbar">
          <span className="topbar__title" style={{ textTransform: "capitalize" }}>
            {snoozed ? `Snoozed · ${folder}` : folder}
          </span>
          <span className="hint" style={{ marginLeft: "auto" }}>
            {/* No "1–50 of 3,412". The API returns a cursor and nothing else, so a
                total would be invented. */}
            {paged.items.length} loaded
          </span>
        </div>

        <MessageList
          paged={paged}
          href={href}
          selectedId={messageId}
          selectedCopy={copy}
          ownMailboxId={session?.mailboxId ?? null}
          empty={
            <div className="empty">
              <p className="empty__title">
                {snoozed ? "Nothing is snoozed." : `Nothing in ${folder}.`}
              </p>
              <p>
                {snoozed
                  ? "Snoozed mail waits here until its time passes, then returns to the folder it came from."
                  : "Nimbus receives and stores mail. It does not send — there is no compose, reply or forward, by design."}
              </p>
            </div>
          }
        />
      </div>

      <div className="split__detail">
        {messageId ? (
          <Message
            key={`${messageId}:${copy}`}
            messageId={messageId}
            mailboxId={copy}
            backHref={`/mail/${folder}${snoozed ? "?snoozed=1" : ""}`}
            backLabel={snoozed ? "snoozed" : folder}
            onChanged={(id, mailboxId, patch) => {
              paged.update(id, mailboxId, patch as Partial<MessageRow>);
              // Moving or snoozing takes the message out of the list being viewed;
              // dropping the row is what makes the two panes agree.
              const leaves =
                (patch.folder && patch.folder !== folder) ||
                (patch.snooze_until !== undefined && !snoozed && patch.snooze_until !== null) ||
                (snoozed && patch.snooze_until === null);
              if (leaves) {
                paged.remove(id, mailboxId);
                backToList();
              }
            }}
            onDeleted={(id, mailboxId) => {
              paged.remove(id, mailboxId);
              backToList();
            }}
          />
        ) : (
          <div className="empty">
            <p className="empty__title">No message selected.</p>
            <p>Pick one from the list. Use j and k to move between rows.</p>
          </div>
        )}
      </div>
    </div>
  );
}
