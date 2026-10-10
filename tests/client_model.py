"""What a client must do with the realtime messages (终版 §6).  The browser code of D38 mirrors this.

A message is checked as a whole BEFORE anything changes, so a bad one leaves the state as it was and the client
asks for a fresh snapshot instead of showing half of it.
"""

import copy


class Rejected(Exception):
    """The message cannot be applied; the connection must be dropped and a snapshot taken again."""


class ClientModel:
    def __init__(self):
        self.channels = None
        self.seq = None
        self.status = None
        self.applied = []  # (seq, channel ids) per group, in application order

    def apply(self, message: dict):
        kind = message["type"]
        if kind == "snapshot":
            self.channels, self.seq, self.applied = copy.deepcopy(message["channels"]), message["pub_seq"], []
        elif self.channels is None:
            raise Rejected("no snapshot yet")
        elif kind == "groups":
            groups = message["groups"]
            seqs = [g["seq"] for g in groups]
            if not seqs or message["first"] != seqs[0] or message["last"] != seqs[-1]:
                raise Rejected("first/last do not match the groups inside")
            if seqs != list(range(seqs[0], seqs[0] + len(seqs))):
                raise Rejected("groups inside are missing or out of order")
            if seqs[0] != self.seq + 1:
                raise Rejected("a group between the last applied one and this message is missing")
            for group in groups:  # in order, each whole: a result never meets an input of another group
                self.channels.update(copy.deepcopy(group["channels"]))
                self.applied.append((group["seq"], sorted(group["channels"])))
            self.seq = seqs[-1]
        elif kind == "heartbeat":
            if message["seq"] != self.seq:
                raise Rejected("the sender is ahead: groups were missed")
        else:
            raise Rejected("unknown message " + kind)
        self.status = message["status"] if "status" in message else self.status
