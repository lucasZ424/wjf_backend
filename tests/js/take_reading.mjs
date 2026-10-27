// Takes a real reading of the backend with the page's own rule (htsg/static/stream.js) and reports what happened.
// usage: node take_reading.mjs <input.json>   input: {good: reading, damaged: {name: reading}}
import {readFileSync} from "node:fs";
import {newLive, Rejected, take} from "../../htsg/static/stream.js";

const input = JSON.parse(readFileSync(process.argv[2], "utf8"));
const live = newLive();
take(live, input.good);
const out = {taken: live, damaged: {}};

// each damaged variant is tried on a state that has just taken the good reading: refused, and nothing changed
for (const [name, reading] of Object.entries(input.damaged)) {
  const state = newLive();
  take(state, input.good);
  const before = JSON.stringify(state);
  let rejected = false;
  try {
    take(state, reading);
  } catch (error) {
    if (!(error instanceof Rejected)) throw error;
    rejected = true;
  }
  out.damaged[name] = {rejected, unchanged: JSON.stringify(state) === before};
}
process.stdout.write(JSON.stringify(out));
