import { useCallback, useState } from "react";
import { useSearchParams } from "react-router";
import * as api from "../api.ts";
import { useAuth } from "../auth.tsx";
import { usePaged } from "../hooks.ts";
import type { MessageRow, ParsedQuery } from "../types.ts";
import { MessageList } from "../components/MessageList.tsx";

/* Search. The query lives in the URL, so back, forward, refresh and sharing all work.
 *
 * The UI does NOT parse the DSL. The server's parser never raises and echoes back what
 * it understood, so the chips below the box are built from that echo — one map over a
 * field we already receive. A second grammar in the client is a second grammar to drift.
 */

const OPERATORS = ["from:", "has:attachment", "before:", "after:"];

function Chips({ q }: { q: ParsedQuery }) {
  const chips: string[] = [];
  if (q.from) chips.push(`from contains "${q.from}"`);
  if (q.has_attachment) chips.push("has an attachment");
  if (q.before) chips.push(`arrived before ${q.before}`);
  if (q.after) chips.push(`arrived on or after ${q.after}`);
  if (q.text) chips.push(`words: ${q.text}`);
  if (chips.length === 0) return null;
  return (
    <div className="search__chips" aria-label="How this query was read">
      {chips.map((c) => (
        <span key={c} className="chip">
          {c}
        </span>
      ))}
    </div>
  );
}

export default function Search() {
  const [params, setParams] = useSearchParams();
  const { session } = useAuth();
  const q = params.get("q") ?? "";
  const [draft, setDraft] = useState(q);
  const [parsed, setParsed] = useState<ParsedQuery | null>(null);

  const fetchPage = useCallback(
    async (cursor: string | null) => {
      // The hook fetches on mount, so landing on /search with no query would send
      // `?q=` — a 400 by design on the server, once per visit, for a result this screen
      // never renders. The hint block below is what an empty query shows.
      if (!q) return { messages: [], next_cursor: null };
      const r = await api.search(q, cursor);
      setParsed(r.query);
      return r;
    },
    [q],
  );

  const paged = usePaged<MessageRow>(fetchPage, [q]);

  return (
    <div>
      <form
        className="search__form"
        role="search"
        onSubmit={(e) => {
          e.preventDefault();
          const next = draft.trim();
          // An empty query is a 400 on the server. Do not send it; show the hints.
          if (next) setParams({ q: next });
        }}
      >
        <input
          className="input"
          type="search"
          name="q"
          aria-label="Search mail"
          placeholder="from:boss has:attachment invoice"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
        />
        <button className="btn btn--primary" type="submit">
          Search
        </button>
      </form>

      {q === "" ? (
        <div className="empty">
          <p className="empty__title">Search every folder, including trash.</p>
          <p>
            Four operators combine with your words. Anything else you type is searched
            as text — an operator it does not recognise is never an error.
          </p>
          <div className="row-flex" style={{ flexWrap: "wrap" }}>
            {OPERATORS.map((op) => (
              <button
                key={op}
                className="btn btn--bordered mono"
                onClick={() => setDraft((d) => (d ? `${d} ${op}` : op))}
              >
                {op}
              </button>
            ))}
          </div>
        </div>
      ) : (
        <>
          {parsed && <Chips q={parsed} />}
          <MessageList
            paged={paged}
            /* No `copy` in the URL here — a search row carries its mailbox_id just
               like a list row, so the reading pane still names the copy. */
            href={(row) => `/mail/${row.folder}/${row.id}?copy=${row.mailbox_id}`}
            ownMailboxId={session?.mailboxId ?? null}
            showFolder
            empty={
              <div className="empty">
                <p className="empty__title">Nothing matched.</p>
                <p>Search covers every folder and snoozed mail, so this is all of it.</p>
              </div>
            }
          />
        </>
      )}
    </div>
  );
}
