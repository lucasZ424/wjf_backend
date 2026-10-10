// Builds the replay CSV with the page's own code (htsg/static/replay.js) from a prepared result.
// usage: node build_csv.mjs <input.json>   input: {result, request, fault, timezone, labels, units}
import {readFileSync} from "node:fs";
import {buildCsv} from "../../htsg/static/replay.js";

const input = JSON.parse(readFileSync(process.argv[2], "utf8"));
const text = buildCsv({
  result: input.result, request: input.request, fault: input.fault, timezone: input.timezone,
  label: id => input.labels[id], unit: id => input.units[id],
});
process.stdout.write(text);
