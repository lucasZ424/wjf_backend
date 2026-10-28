"""Local deployment credentials; hashes only in runtime account file."""

import copy
import hashlib
import hmac
import json
import os
import secrets
import threading
from pathlib import Path

FALLBACK = "admin-fallback.json"  # beside the account file: the hash of the administrator's initial password (修复轮07)


def _write_through(path: Path, text: str):
    """On the disk before the caller goes on: after a power loss no name points at contents that never got there."""
    with path.open("w", encoding="utf-8") as file:
        file.write(text)
        file.flush()
        os.fsync(file.fileno())


def write_fallback(path: Path, secret: str):
    """The administrator's initial password as the fallback, beside the account file `path` (修复轮07, 终版 §2.3): written
    by the deployment that makes the administrator, never by a run, which only reads it."""
    salt = secrets.token_bytes(16)
    target = path.with_name(FALLBACK)
    target.parent.mkdir(exist_ok=True, parents=True)
    temp = target.with_suffix(".tmp")
    _write_through(temp, json.dumps({"salt": salt.hex(), "digest": hashlib.scrypt(secret.encode(), salt=salt, n=2**14, r=8, p=1).hex()}))
    temp.replace(target)


class Accounts:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.unreadable = None  # why the local copy could not be read; the database copy replaces it
        try:
            self.users = json.loads(path.read_text()) if path.exists() else {}
        except (OSError, ValueError) as error:  # what a power loss can leave: empty, torn or zeroed
            self.users, self.unreadable = {}, type(error).__name__
        self.pending_path = path.with_suffix(".pending")
        self.uncertain = self.pending_path.exists()
        self.persist = None
        self.fallback = self.fallback_unreadable = None  # 修复轮07: none to be had takes only the fallback away
        try:
            if path.with_name(FALLBACK).exists():
                found = json.loads(path.with_name(FALLBACK).read_text(encoding="utf-8"))
                salt, digest = bytes.fromhex(found["salt"]), found["digest"]
                if not isinstance(digest, str):
                    raise ValueError("digest")
                self.fallback = {"salt": salt, "digest": digest}
        except (OSError, ValueError, KeyError, TypeError) as error:
            self.fallback_unreadable = type(error).__name__

    def verify(self, credential):
        if not credential or ":" not in credential:
            return None
        username, token = credential.split(":", 1)
        user = None if self.uncertain else self.users.get(username)
        if user and user["active"]:
            digest = hashlib.scrypt(token.encode(), salt=bytes.fromhex(user["salt"]), n=2**14, r=8, p=1).hex()
            if not self.uncertain and hmac.compare_digest(digest, user["digest"]):
                return {"username": username, "role": user["role"]}
        if username == "admin" and self.fallback:  # 修复轮07: the initial password signs the administrator in, whatever the accounts
            digest = hashlib.scrypt(token.encode(), salt=self.fallback["salt"], n=2**14, r=8, p=1).hex()
            if hmac.compare_digest(digest, self.fallback["digest"]):
                return {"username": "admin", "role": "admin"}
        return None

    def create(self, username, role, audit=None, token=None):
        if not username or len(username) > 64 or not all(c.isalnum() or c in "-_" for c in username):
            raise ValueError("invalid username")
        if role not in ("admin", "viewer"):
            raise ValueError("invalid role")
        with self.lock:
            if self.uncertain:
                raise RuntimeError("Account persistence requires database reconciliation")
            if username in self.users:
                raise ValueError("account already exists")
            if role == "admin" and any(user["role"] == "admin" for user in self.users.values()):
                raise ValueError("there is already an administrator: only read-only users are added")
            token, salt = token or secrets.token_urlsafe(32), secrets.token_bytes(16)  # the deployment brings its own (修复轮07)
            candidate = copy.deepcopy(self.users)
            candidate[username] = {
                "role": role,
                "salt": salt.hex(),
                "digest": hashlib.scrypt(token.encode(), salt=salt, n=2**14, r=8, p=1).hex(),
                "active": True,
            }
            self.save(candidate, audit)
        return username + ":" + token

    def set_secret(self, username, secret, audit=None):
        """A password of the account's own in place of its credential (the administrator's, 修复轮02); the old one stops
        working.  It must survive the login, which trims what is typed and takes at most 256 characters."""
        if not isinstance(secret, str) or secret != secret.strip() or not 8 <= len(secret) <= 255 - len(username):
            raise ValueError(f"a password needs 8 to {255 - len(username)} characters and no space at either end")
        with self.lock:
            if self.uncertain:
                raise RuntimeError("Account persistence requires database reconciliation")
            if username not in self.users:
                raise ValueError("account not found")
            salt = secrets.token_bytes(16)
            candidate = copy.deepcopy(self.users)
            candidate[username].update(salt=salt.hex(), digest=hashlib.scrypt(secret.encode(), salt=salt, n=2**14, r=8, p=1).hex())
            self.save(candidate, audit)

    def revoke(self, username, audit=None):
        with self.lock:
            if self.uncertain:
                raise RuntimeError("Account persistence requires database reconciliation")
            if username not in self.users:
                raise ValueError("account not found")
            if (
                self.users[username]["role"] == "admin"
                and sum(u["active"] and u["role"] == "admin" for u in self.users.values()) <= 1
            ):
                raise ValueError("cannot revoke last active admin")
            candidate = copy.deepcopy(self.users)
            candidate[username]["active"] = False
            self.save(candidate, audit)

    def save(self, candidate, audit=None):
        if self.persist:
            self.begin_update()
            if audit is None:
                self.persist(candidate)
            else:
                self.persist(candidate, audit)
        self.publish(candidate)

    def begin_update(self):
        self.uncertain = True
        self.path.parent.mkdir(exist_ok=True, parents=True)
        _write_through(self.pending_path, "Database account reconciliation required")

    def publish(self, candidate):
        self.path.parent.mkdir(exist_ok=True, parents=True)
        temp = self.path.with_suffix(".tmp")
        _write_through(temp, json.dumps(candidate))
        temp.replace(self.path)
        self.users = candidate
        self.pending_path.unlink(missing_ok=True)
        self.uncertain = False

    def reconcile(self, store):
        """Database is authoritative; while it holds no account the local file bootstraps it, over an unfinished change
        too - that change never reached the database, so it stays undone."""
        with self.lock:
            committed = store.read_accounts()
            if not committed:
                self.save(self.users)
            else:
                self.begin_update()
                self.publish(committed)
