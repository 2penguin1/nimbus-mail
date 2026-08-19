/* The one runnable check. `npm test` -> `node --test src/format.test.ts`.
 *
 * No framework: Node 24 strips TypeScript and ships `node --test`, so this costs zero
 * dependencies. It covers the arithmetic a wrong answer would make invisible — byte
 * scaling, the saving ratio, and the snooze predicate.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import {
  bytes,
  exact,
  formatRatio,
  isSnoozed,
  percent,
  savingRatio,
  sender,
  when,
} from "./format.ts";

test("bytes scales in base 1000 and keeps one decimal below 10", () => {
  assert.equal(bytes(0), "0 B");
  assert.equal(bytes(999), "999 B");
  assert.equal(bytes(1000), "1.0 kB");
  assert.equal(bytes(4_467_507), "4.5 MB");
  assert.equal(bytes(1_096_278_016), "1.1 GB");
  // At or above 10 the decimal is dropped, so a column of sizes stays the same width.
  assert.equal(bytes(44_675_072), "45 MB");
  assert.equal(bytes(25_000_000), "25 MB");
});

test("exact keeps every byte, for the table view", () => {
  assert.equal(exact(1_096_278_016), "1,096,278,016");
});

test("savingRatio refuses the cases that would print a nonsense headline", () => {
  assert.equal(savingRatio(1_000_000_000, 25_000_000), 40);
  // A brand-new mailbox: 0 bytes stored. Dividing would give Infinity on the hero.
  assert.equal(savingRatio(1000, 0), null);
  assert.equal(savingRatio(0, 0), null);
  // physical > logical cannot mean a saving; refuse rather than print "0.4x less".
  assert.equal(savingRatio(100, 500), null);
});

test("formatRatio switches precision at 10", () => {
  assert.equal(formatRatio(4.24), "4.2×");
  assert.equal(formatRatio(40), "40×");
});

test("percent clamps, because a mailbox may exceed a quota that is not enforced", () => {
  assert.equal(percent(50, 200), 25);
  assert.equal(percent(300, 200), 100);
  assert.equal(percent(5, 0), 0); // no divide-by-zero on an unset quota
});

test("isSnoozed is evaluated, never remembered", () => {
  const future = new Date(Date.now() + 60_000).toISOString();
  const past = new Date(Date.now() - 60_000).toISOString();
  assert.equal(isSnoozed(future), true);
  // Nothing on the server expires a snooze; a past time simply stops being true.
  assert.equal(isSnoozed(past), false);
  assert.equal(isSnoozed(null), false);
  assert.equal(isSnoozed(undefined), false);
});

test("when never throws on a bad timestamp", () => {
  assert.equal(when("not-a-date"), "");
  const now = new Date("2026-08-10T12:00:00Z");
  assert.notEqual(when("2026-08-10T09:00:00Z", now), "");
});

test("sender pulls the display name out of a From header", () => {
  // The common case, and the reason this exists: the address is 30 characters of
  // noise in a column 150px wide.
  assert.equal(sender("Ananya Krishnan <ananya.krishnan@globex-industries.com>"), "Ananya Krishnan");

  // A quoted display name — legal, and common when the name contains a comma.
  assert.equal(sender('"Doe, John" <j@example.com>'), "Doe, John");

  // No display name: show the address, but not the angle brackets.
  assert.equal(sender("<procurement@vertex.com>"), "procurement@vertex.com");

  // A bare address is already the best thing we can show.
  assert.equal(sender("procurement@vertex.com"), "procurement@vertex.com");

  // A display name may legally contain '<', the address may not — so split on the
  // LAST one. Splitting on the first would return "a " here.
  assert.equal(sender("a <b> c <real@example.com>"), "a <b> c");
});

test("sender never returns an empty string", () => {
  // The row would collapse to nothing and the layout would shift. Every branch has
  // to produce something printable.
  for (const input of [null, undefined, "", "   ", "<>", "<   >"]) {
    assert.ok(sender(input).length > 0, `empty result for ${JSON.stringify(input)}`);
  }
});
