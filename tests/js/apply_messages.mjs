// Applies recorded realtime messages with the page's own rules (htsg/static/stream.js) and reports what happened.
// usage: node apply_messages.mjs <input.json>   input: {messages: [...], damaged: {name: [message, ...]}}
import {readFileSync} from "node:fs";
import {apply, newLive, Rejected} from "../../htsg/static/stream.js";

const input = JSON.parse(readFileSync(process.argv[2], "utf8"));
const live = newLive();
let groups = 0;
for (const message of input.messages) groups += apply(live, message).groups.length;
const out = {channels: live.channels, seq: live.seq, groups, damaged: {}};

// each damaged variant is tried on a fresh state that stands where the variant was meant to continue
for (const [name, {base, message}] of Object.entries(input.damaged)) {
  const state = newLive();
  apply(state, base);
  const before = JSON.stringify(state);
  let rejected = false;
  try {
    apply(state, message);
  } catch (error) {
    if (!(error instanceof Rejected)) throw error;
    rejected = true;
  }
  out.damaged[name] = {rejected, unchanged: JSON.stringify(state) === before};
}
process.stdout.write(JSON.stringify(out));
