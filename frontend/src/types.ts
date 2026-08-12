/* The API's JSON, written out by hand.
 *
 * Hand-written and not generated: every 200 response in FastAPI's /openapi.json is an
 * empty schema `{}`, because no router declares a `response_model`. openapi-typescript
 * would emit `unknown` for all twelve endpoints. Checked against the live spec.
 *
 * These mirror `backend/src/nimbus/api/routers/*.py` exactly, including which fields
 * are nullable — `subject`, `from` and both bodies are nullable columns in models.py,
 * and pretending otherwise is how you ship "undefined" into a subject line.
 */

/** ISO-8601 with offset, as FastAPI serialises a `timestamptz`. */
export type Iso = string;

export const FOLDERS = ["inbox", "archive", "trash", "spam"] as const;
export type Folder = (typeof FOLDERS)[number];

/** A row in `GET /v1/messages` or `GET /v1/search`. Same shape, one exception. */
export interface MessageRow {
  id: string;
  subject: string | null;
  from: string | null;
  received_at: Iso;
  is_read: boolean;
  folder: Folder;
  size_bytes: number;
  thread_id: string | null;
  has_attachments: boolean;
  /** Which copy this row is — the caller's own mailbox, or a shared one they read. */
  mailbox_id: string;
  /** Optional, and that is not laziness: `GET /v1/messages` returns it, `GET /v1/search`
   *  does NOT. Typing it as required would be a lie on every search result. */
  snooze_until?: Iso | null;
}

export interface Page {
  messages: MessageRow[];
  /** The only pagination signal there is. No total, no page count — so the UI must not
   *  render one. Null means this is the last page. */
  next_cursor: string | null;
}

export interface Attachment {
  blob_hash: string;
  filename: string | null;
  content_type: string | null;
  size_bytes: number;
  /** Server-built path. Needs an Authorization header, so it can never be an <a href>. */
  url: string;
}

export interface MessageDetail {
  id: string;
  /** Which copy the server resolved. Present even when the request omitted
   *  `?mailbox_id=` — it is how a message opened from a thread learns the row it is
   *  allowed to act on. Without it there is nothing to name on PATCH or DELETE. */
  mailbox_id: string;
  subject: string | null;
  from: string | null;
  sent_at: Iso | null;
  received_at: Iso;
  size_bytes: number;
  thread_id: string | null;
  message_id_header: string | null;
  in_reply_to: string | null;
  is_read: boolean;
  folder: Folder;
  snooze_until: Iso | null;
  body_text: string | null;
  /** Sender-controlled HTML. The largest XSS surface in the product — see BodyView. */
  body_html: string | null;
  attachments: Attachment[];
}

/** `GET /v1/threads/{id}` rows carry neither `mailbox_id` nor `has_attachments`. */
export interface ThreadMessage {
  id: string;
  subject: string | null;
  from: string | null;
  sent_at: Iso | null;
  received_at: Iso;
  size_bytes: number;
  is_read: boolean;
  folder: Folder;
}

export interface Thread {
  thread_id: string;
  messages: ThreadMessage[];
}

/** What the server understood from the query string. Rendered back as chips, so the
 *  UI never needs its own copy of the DSL grammar. */
export interface ParsedQuery {
  text: string;
  from: string | null;
  has_attachment: boolean;
  before: string | null;
  after: string | null;
}

export interface SearchResult extends Page {
  query: ParsedQuery;
}

export interface Quota {
  /** `mailbox.used_bytes` — what the user believes they used. What we bill. */
  logical_bytes: number;
  /** Distinct blobs this mailbox reaches. NEVER sum this across mailboxes (HLD §11.1). */
  physical_bytes: number;
  quota_bytes: number;
}

export interface LoginResponse {
  token: string;
  expires_in_hours: number;
}
