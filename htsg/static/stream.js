"use strict";
// The rules for applying realtime messages (终版 §6).  No DOM and no network: the page and the tests share this
// file.  A message is checked as a whole BEFORE anything changes, so a bad one leaves the state as it was and
// the caller drops the connection to take a fresh snapshot instead of showing half of it.

export class Rejected extends Error {}

export function newLive() {
  return {channels: null, seq: null, status: null};
}

const isObject = x => x !== null && typeof x === "object" && !Array.isArray(x);
const isCount = x => Number.isInteger(x) && x >= 0;

function checkView(id, view) {
  if (!isObject(view) || !["input", "derived"].includes(view.kind) || typeof view.valid !== "boolean") {
    throw new Rejected(`channel ${id} is not a channel state`);
  }
}

function checkGroup(live, group) {
  if (!isObject(group) || !isCount(group.seq) || !isObject(group.channels)
      || !Array.isArray(group.states) || !Array.isArray(group.faults)) {
    throw new Rejected("a group does not have the shape of a group");
  }
  for (const [id, view] of Object.entries(group.channels)) {
    if (!(id in live.channels)) throw new Rejected(`group names channel ${id}, which the snapshot did not have`);
    checkView(id, view);
  }
}

// Returns {snapshot, groups: [...]}: what the page needs to know about the change (the groups applied, in order).
export function apply(live, message) {
  if (!isObject(message)) throw new Rejected("a message is not an object");
  const result = {snapshot: false, groups: []};
  if (message.type === "snapshot") {
    if (!isObject(message.channels) || !isCount(message.pub_seq)) throw new Rejected("the snapshot is incomplete");
    for (const [id, view] of Object.entries(message.channels)) checkView(id, view);
    live.channels = message.channels;  // replaces everything: nothing of an older state survives
    live.seq = message.pub_seq;
    result.snapshot = true;
  } else if (live.channels === null) {
    throw new Rejected("no snapshot yet");
  } else if (message.type === "groups") {
    const groups = message.groups;
    if (!Array.isArray(groups) || groups.length === 0) throw new Rejected("a groups message without groups");
    groups.forEach(group => checkGroup(live, group));
    const seqs = groups.map(g => g.seq);
    if (message.first !== seqs[0] || message.last !== seqs[seqs.length - 1]) {
      throw new Rejected("first/last do not match the groups inside");
    }
    if (seqs.some((seq, i) => seq !== seqs[0] + i)) throw new Rejected("groups inside are missing or out of order");
    if (seqs[0] !== live.seq + 1) throw new Rejected("a group before this message is missing");
    for (const group of groups) {  // in order, each whole: a result never meets an input of another group
      for (const [id, view] of Object.entries(group.channels)) live.channels[id] = view;
      result.groups.push(group);
    }
    live.seq = seqs[seqs.length - 1];
  } else if (message.type === "heartbeat") {
    if (message.seq !== live.seq) throw new Rejected("the sender is ahead: groups were missed");
  } else {
    throw new Rejected(`unknown message ${message.type}`);
  }
  if (isObject(message.status)) live.status = message.status;
  return result;
}
