"""Local deployment credentials; hashes only in runtime account file."""

import copy
import hashlib
import hmac
import json
import secrets
import threading
from pathlib import Path


class Accounts:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.users = json.loads(path.read_text()) if path.exists() else {}
        self.pending_path = path.with_suffix(".pending")
        self.uncertain = self.pending_path.exists()
        self.persist = None

    def verify(self, credential):
        if self.uncertain or not credential or ":" not in credential:
            return None
        username, token = credential.split(":", 1)
        user = self.users.get(username)
        if not user or not user["active"]:
            return None
        digest = hashlib.scrypt(token.encode(), salt=bytes.fromhex(user["salt"]), n=2**14, r=8, p=1).hex()
        return (
            {"username": username, "role": user["role"]}
            if not self.uncertain and hmac.compare_digest(digest, user["digest"])
            else None
        )

    def create(self, username, role, audit=None):
        if not username or len(username) > 64 or not all(c.isalnum() or c in "-_" for c in username):
            raise ValueError("invalid username")
        if role not in ("admin", "viewer"):
            raise ValueError("invalid role")
        with self.lock:
            if self.uncertain:
                raise RuntimeError("Account persistence requires database reconciliation")
            if username in self.users:
                raise ValueError("account already exists")
            token, salt = secrets.token_urlsafe(32), secrets.token_bytes(16)
            candidate = copy.deepcopy(self.users)
            candidate[username] = {
                "role": role,
                "salt": salt.hex(),
                "digest": hashlib.scrypt(token.encode(), salt=salt, n=2**14, r=8, p=1).hex(),
                "active": True,
            }
            self.save(candidate, audit)
        return username + ":" + token

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
        self.pending_path.write_text("Database account reconciliation required", encoding="utf-8")

    def publish(self, candidate):
        self.path.parent.mkdir(exist_ok=True, parents=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(candidate), encoding="utf-8")
        temp.replace(self.path)
        self.users = candidate
        self.pending_path.unlink(missing_ok=True)
        self.uncertain = False

    def reconcile(self, store):
        """Database is authoritative; a clean local file may bootstrap an empty database."""
        with self.lock:
            committed = store.read_accounts()
            if not committed:
                if self.uncertain:
                    raise RuntimeError("Pending account update cannot reconcile with empty database")
                self.save(self.users)
            else:
                self.begin_update()
                self.publish(committed)
