/* Pure formatting. No React, no fetch — which is what makes it the one thing in the
 * frontend worth a test file.
 */

/** Bytes for humans. Base 1000, because storage is sold in GB and the dashboard's
 *  headline number must match what a customer would be billed for, not GiB. */
export function bytes(n: number): string {
  if (n < 1000) return `${n} B`;
  const units = ["kB", "MB", "GB", "TB"];
  let v = n;
  let i = -1;
  while (v >= 1000 && i < units.length - 1) {
    v /= 1000;
    i++;
  }
  // One decimal below 10 so "1.4 MB" and "24 MB" both read cleanly.
  return `${v < 10 ? v.toFixed(1) : Math.round(v)} ${units[i]}`;
}

/** Exact byte count with thousands separators, for the dashboard's table view. */
export const exact = (n: number): string => n.toLocaleString("en-US");

/** How an inbox shows a timestamp: time today, weekday this week, date beyond that. */
export function when(iso: string, now: Date = new Date()): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const days = Math.floor((startOfDay(now) - startOfDay(d)) / 86_400_000);
  if (days === 0) {
    return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  }
  if (days > 0 && days < 7) return d.toLocaleDateString(undefined, { weekday: "short" });
  const sameYear = d.getFullYear() === now.getFullYear();
  return d.toLocaleDateString(undefined, {
    day: "numeric",
    month: "short",
    ...(sameYear ? {} : { year: "numeric" }),
  });
}

const startOfDay = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();

/** Full timestamp for the message detail's machine column. */
export function fullWhen(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleString();
}

/** "Snoozed until Mon 09:00" — enough to act on, short enough for a row. */
export function until(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleString(undefined, {
    weekday: "short",
    hour: "2-digit",
    minute: "2-digit",
    ...(d.getFullYear() === new Date().getFullYear() ? {} : { year: "numeric" }),
    day: "numeric",
    month: "short",
  });
}

/** A snooze is in force only while its time is still ahead — there is no stored flag
 *  and nothing runs to expire one, so this must be evaluated, never remembered. */
export const isSnoozed = (iso: string | null | undefined): boolean =>
  !!iso && new Date(iso).getTime() > Date.now();

/** The dashboard's hero number: how many times more disk a naive mail server needs. */
export function savingRatio(logical: number, physical: number): number | null {
  if (physical <= 0 || logical <= 0 || logical < physical) return null;
  return logical / physical;
}

export const formatRatio = (r: number): string =>
  r < 10 ? `${r.toFixed(1)}×` : `${Math.round(r)}×`;

/** Percent of a limit, clamped — a mailbox can exceed its quota (HLD §11.3 does not
 *  enforce it), and a bar wider than its track is a rendering bug, not a fact. */
export const percent = (part: number, whole: number): number =>
  whole <= 0 ? 0 : Math.min(100, (part / whole) * 100);
