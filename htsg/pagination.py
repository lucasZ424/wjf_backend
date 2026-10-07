"""Scoped keyset cursors; a cursor is a position, never a SQL fragment."""

import base64
import hashlib
import json
import math


def scope(category, filters):
    return hashlib.sha256(json.dumps([category, filters], sort_keys=True).encode()).hexdigest()


def encode(key, identity):
    return base64.urlsafe_b64encode(json.dumps([1, identity, key]).encode()).decode()


def decode(token, identity, category):
    if token is None:
        return None
    try:
        if len(token) > 2048:
            raise ValueError()
        version, saved, key = json.loads(base64.urlsafe_b64decode(token))
        if version != 1 or saved != identity or not isinstance(key, list):
            raise ValueError()
        count = 4 if category in ("history", "replay") else 1 if category == "products" else 2
        if len(key) != count:
            raise ValueError()
        if category != "products":
            if not isinstance(key[0], (int, float)) or isinstance(key[0], bool) or not math.isfinite(key[0]):
                raise ValueError()
        if any(not isinstance(x, str) for x in (key if category == "products" else key[1:])):
            raise ValueError()
        return key
    except (ValueError, TypeError, UnicodeError, OverflowError) as error:
        raise ValueError("Invalid cursor or cursor belongs to another query") from error
