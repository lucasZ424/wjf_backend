"use strict";
// The rule for taking a reading of the current state (终版 §13.3).  No DOM and no network: the page and the tests
// share this file.  A reading is checked as a whole BEFORE anything changes, so a bad one leaves the state as it was
// and only the next good reading counts.

export class Rejected extends Error {}

export function newLive() {
  return {channels: null, status: null, run: null, taken: null};
}

const isObject = x => x !== null && typeof x === "object" && !Array.isArray(x);

function checkView(id, view) {
  if (!isObject(view) || !["input", "derived"].includes(view.kind) || typeof view.valid !== "boolean") {
    throw new Rejected(`channel ${id} is not a channel state`);
  }
}

// Whole or nothing.  An empty value, a bad quality or an invalid state is a state like any other; only a reading
// that is not a reading is refused.
export function take(live, reading) {
  if (!isObject(reading) || !isObject(reading.channels) || !Number.isFinite(reading.taken_time)
      || typeof reading.run_id !== "string" || !isObject(reading.status)) {
    throw new Rejected("the reading is incomplete");
  }
  for (const [id, view] of Object.entries(reading.channels)) checkView(id, view);
  Object.assign(live, {channels: reading.channels, status: reading.status, run: reading.run_id, taken: reading.taken_time});
}
