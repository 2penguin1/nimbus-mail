/* The whole data layer: two hooks.
 *
 * No TanStack Query. Six screens, no shared cross-route cache, no optimistic writes.
 * ponytail: no cache and no request dedup, so navigating back to the inbox refetches
 * it. Ceiling — wasted round trips on a chatty user. Upgrade path: TanStack Query,
 * when two routes genuinely need the same cached list.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "./api.ts";
import type { Page } from "./types.ts";

export interface Async<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  reload: () => void;
}

/** Run an async function on mount and whenever `deps` change.
 *
 * `fn` is intentionally NOT a dependency — an inline arrow is a new function every
 * render, so depending on it would refetch forever. The caller states what actually
 * changed via `deps`, which is the same contract useEffect already has.
 */
export function useAsync<T>(fn: () => Promise<T>, deps: unknown[]): Async<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [nonce, setNonce] = useState(0);
  const latest = useRef(0);

  useEffect(() => {
    const call = ++latest.current;
    setLoading(true);
    setError(null);
    fn()
      .then((d) => {
        // A slow first request must not overwrite a fast second one. Without this
        // guard, switching folders quickly can leave the previous folder's mail on
        // screen under the new folder's heading.
        if (call === latest.current) setData(d);
      })
      .catch((e: unknown) => {
        // A 401 has already redirected to /login; showing its message underneath
        // would be a second, redundant error on a page that is going away.
        if (call === latest.current && !(e instanceof ApiError && e.status === 401)) {
          setError(e instanceof Error ? e.message : String(e));
        }
      })
      .finally(() => {
        if (call === latest.current) setLoading(false);
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce]);

  const reload = useCallback(() => setNonce((n) => n + 1), []);
  return { data, error, loading, reload };
}

export interface Paged<T> {
  items: T[];
  error: string | null;
  loading: boolean;
  loadingMore: boolean;
  /** Null once there are no more pages. There is no total and no page count — the API
   *  returns only a cursor, so the UI must not render progress it cannot know. */
  cursor: string | null;
  loadMore: () => void;
  reload: () => void;
  /** Drop one row locally after a delete, so the list does not refetch to lose a row. */
  remove: (id: string, mailboxId: string) => void;
  /** Patch one row locally after a successful PATCH, for the same reason. */
  update: (id: string, mailboxId: string, patch: Partial<T>) => void;
}

/** Keyset pagination against any endpoint returning `{messages, next_cursor}`. */
export function usePaged<T extends { id: string; mailbox_id: string }>(
  fetchPage: (cursor: string | null) => Promise<Page>,
  deps: unknown[],
): Paged<T> {
  const [items, setItems] = useState<T[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [nonce, setNonce] = useState(0);
  const latest = useRef(0);

  useEffect(() => {
    const call = ++latest.current;
    setLoading(true);
    setError(null);
    setItems([]);
    setCursor(null);
    fetchPage(null)
      .then((p) => {
        if (call !== latest.current) return;
        setItems(p.messages as unknown as T[]);
        setCursor(p.next_cursor);
      })
      .catch((e: unknown) => {
        if (call === latest.current && !(e instanceof ApiError && e.status === 401)) {
          setError(e instanceof Error ? e.message : String(e));
        }
      })
      .finally(() => {
        if (call === latest.current) setLoading(false);
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce]);

  const loadMore = useCallback(() => {
    if (!cursor || loadingMore) return;
    const call = latest.current;
    setLoadingMore(true);
    fetchPage(cursor)
      .then((p) => {
        if (call !== latest.current) return; // the folder changed mid-flight
        setItems((prev) => [...prev, ...(p.messages as unknown as T[])]);
        setCursor(p.next_cursor);
      })
      .catch((e: unknown) => {
        // Same staleness guard as the success path above, and for the same reason:
        // click "Load older" in the inbox, switch to archive, and the inbox's failed
        // second page would otherwise post its error over the archive list.
        if (call === latest.current && !(e instanceof ApiError && e.status === 401)) {
          setError(e instanceof Error ? e.message : String(e));
        }
      })
      .finally(() => setLoadingMore(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cursor, loadingMore]);

  // Keyed on (id, mailbox_id), never id alone: one message can legitimately appear
  // twice in one list — the reader's own copy and a shared mailbox's — and removing
  // by id would drop a copy the user did not touch.
  const remove = useCallback((id: string, mailboxId: string) => {
    setItems((prev) => prev.filter((r) => !(r.id === id && r.mailbox_id === mailboxId)));
  }, []);

  const update = useCallback((id: string, mailboxId: string, patch: Partial<T>) => {
    setItems((prev) =>
      prev.map((r) => (r.id === id && r.mailbox_id === mailboxId ? { ...r, ...patch } : r)),
    );
  }, []);

  const reload = useCallback(() => setNonce((n) => n + 1), []);
  return { items, error, loading, loadingMore, cursor, loadMore, reload, remove, update };
}
