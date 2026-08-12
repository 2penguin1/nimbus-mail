/* Every call to the backend goes through `request`. One function, so the bearer token
 * and the 401 rule exist in exactly one place — the same reasoning that put
 * `visibility.readable_mailboxes()` in one module on the server side.
 */

import type {
  Folder,
  LoginResponse,
  MessageDetail,
  Page,
  Quota,
  SearchResult,
  Thread,
} from "./types.ts";

/** Thrown for any non-2xx. Carries the status so callers can branch on 409. */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

const TOKEN_KEY = "nimbus-token";

/* sessionStorage, not localStorage. Both are readable by any XSS — that is not the
 * difference. The difference is lifetime: sessionStorage dies with the tab, so a
 * closed browser on a shared machine is not still a live 24-hour session. The correct
 * answer is an httpOnly cookie, which needs Set-Cookie, SameSite and CSRF tokens on
 * the server; it does not exist yet, so this is the honest second best.
 * ponytail: sessionStorage. Ceiling — an XSS reads the token and gets 24h of mailbox
 * access. Upgrade path: httpOnly cookie + CSRF token, backend work, block K. */

/* The token also lives here, in the module. Not a cache — a fallback for the browsers
 * that refuse storage entirely (site data blocked, some embedded contexts). Without it
 * `set` silently does nothing, every later request goes out with no Authorization
 * header, the server answers 401, and the rule below sends the user back to /login —
 * on a login that just succeeded. */
let memToken: string | null = null;

export const token = {
  get: () => {
    try {
      return sessionStorage.getItem(TOKEN_KEY) ?? memToken;
    } catch {
      return memToken;
    }
  },
  set: (t: string) => {
    memToken = t;
    try {
      sessionStorage.setItem(TOKEN_KEY, t);
    } catch {
      /* the in-memory copy above still carries this tab */
    }
  },
  clear: () => {
    memToken = null;
    try {
      sessionStorage.removeItem(TOKEN_KEY);
    } catch {
      /* nothing to clear */
    }
  },
};

/** Set by auth.tsx so a 401 anywhere can drop the session. A callback rather than an
 *  import of the router: this module must stay usable outside React. */
let onUnauthorized: (() => void) | null = null;
export function setUnauthorizedHandler(fn: () => void) {
  onUnauthorized = fn;
}

/** Did this response end the session? Only if we actually sent one.
 *
 * `POST /v1/auth/login` answers 401 for a wrong password, so a bare `status === 401`
 * check told a user who simply mistyped that their session had ended — and, worse, the
 * redirect overwrote the `next` param holding the page they were trying to reach, so
 * one typo lost their way back. A 401 on a request that carried no token is a refused
 * login, not an expiry.
 *
 * Both fetch callers below go through this, so the rule cannot drift between them.
 */
function sessionEnded(res: Response, sent: string | null): boolean {
  if (res.status !== 401 || !sent) return false;
  // The single expiry rule. There is no refresh token (HLD §10.1), and no client-side
  // `exp` check either — a second expiry rule can drift from the server's, and only the
  // server knows the other 401 case: a token that is still cryptographically valid but
  // whose mailbox was deleted.
  token.clear();
  onUnauthorized?.();
  return true;
}

const EXPIRED = "Your session ended. Sign in to continue.";

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  const t = token.get();
  if (t) headers.set("Authorization", `Bearer ${t}`);
  if (init.body) headers.set("Content-Type", "application/json");

  const res = await fetch(path, { ...init, headers });

  if (sessionEnded(res, t)) throw new ApiError(401, EXPIRED);

  if (!res.ok) {
    throw new ApiError(res.status, await detail(res));
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

/** FastAPI puts the human-readable reason in `detail`. Fall back to the status text
 *  rather than showing "[object Object]" when it is a 422 validation array. */
async function detail(res: Response): Promise<string> {
  try {
    const body = await res.json();
    if (typeof body?.detail === "string") return body.detail;
    if (Array.isArray(body?.detail) && body.detail[0]?.msg) return body.detail[0].msg;
  } catch {
    /* not JSON */
  }
  return `${res.status} ${res.statusText}`;
}

/* ─────────────────────────── endpoints ─────────────────────────── */

export const login = (address: string, password: string) =>
  request<LoginResponse>("/v1/auth/login", {
    method: "POST",
    body: JSON.stringify({ address, password }),
  });

export function listMessages(opts: {
  folder: Folder;
  cursor?: string | null;
  snoozed?: boolean;
  limit?: number;
}) {
  const q = new URLSearchParams({ folder: opts.folder });
  if (opts.cursor) q.set("cursor", opts.cursor);
  if (opts.snoozed) q.set("snoozed", "true");
  if (opts.limit) q.set("limit", String(opts.limit));
  return request<Page>(`/v1/messages?${q}`);
}

/* Ids are encoded, never concatenated raw. They arrive from `useParams()` and from the
 * query string — the address bar — so a deep link carrying a `#` or a `?` in the id
 * would otherwise truncate the request and silently drop `mailbox_id`, turning a
 * mistyped URL into a 409 the user cannot read their way out of. */
const msgPath = (id: string) => `/v1/messages/${encodeURIComponent(id)}`;
const copyQuery = (mailboxId: string) => `?mailbox_id=${encodeURIComponent(mailboxId)}`;

/** `mailbox_id` is optional only so a deep link that lacks it still works. Always pass
 *  it when a list row is at hand: a message id does not identify a row (HLD §10.3), and
 *  omitting it is a 409 whenever the reader holds two copies of the message. */
export const getMessage = (id: string, mailboxId?: string | null) =>
  request<MessageDetail>(msgPath(id) + (mailboxId ? copyQuery(mailboxId) : ""));

export type Patch = {
  is_read?: boolean;
  folder?: Folder;
  /** ISO string WITH offset, or null to un-snooze. A naive datetime is a 422 — the
   *  server would otherwise read it in its own timezone and wake the mail early.
   *  `Date.prototype.toISOString()` always ends in "Z", so it is always valid. */
  snooze_until?: string | null;
};

export const patchMessage = (id: string, mailboxId: string, body: Patch) =>
  request<Record<string, unknown>>(msgPath(id) + copyQuery(mailboxId), {
    method: "PATCH",
    body: JSON.stringify(body),
  });

export const deleteMessage = (id: string, mailboxId: string) =>
  request<void>(msgPath(id) + copyQuery(mailboxId), { method: "DELETE" });

export const getThread = (id: string) =>
  request<Thread>(`/v1/threads/${encodeURIComponent(id)}`);

export function search(q: string, cursor?: string | null) {
  const p = new URLSearchParams({ q });
  if (cursor) p.set("cursor", cursor);
  return request<SearchResult>(`/v1/search?${p}`);
}

export const getQuota = () => request<Quota>("/v1/quota");

/** Download an attachment's bytes.
 *
 * The endpoint requires `Authorization: Bearer`, and a browser sends no header on an
 * <a href> or <img src>, so the bytes have to come through fetch. That costs us the
 * whole of block E's streaming design: no progress, no resume, no Range, and the full
 * response is buffered before anything is saved.
 *
 * ponytail: buffer the whole body. Ceiling — a 25 MB attachment is 25 MB held (large
 * blobs spill to disk in Chrome/Firefox, so it is not all JS heap, but nothing appears
 * until the transfer completes). Upgrade path: a short-lived signed URL endpoint, which
 * makes a plain <a href> work and hands streaming back to the browser. Requested; not
 * built, so this is what exists.
 */
export async function fetchAttachment(url: string): Promise<Blob> {
  const headers = new Headers();
  const t = token.get();
  if (t) headers.set("Authorization", `Bearer ${t}`);
  const res = await fetch(url, { headers });
  if (sessionEnded(res, t)) throw new ApiError(401, EXPIRED);
  if (!res.ok) throw new ApiError(res.status, await detail(res));
  return res.blob();
}
