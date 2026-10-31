"""Local deployment credentials; hashes only in runtime account file.

The account file is the accounts: the only copy (2026-10-30 audit, user ruling - the database copy and its reconciliation are
gone).  A change takes effect only once its management record is written to the archive; one that cannot be recorded is
refused.  Each request is still authenticated, but a credential that has passed scrypt once is recognised from memory
until the next account change, so a page reading every second costs a hash lookup, not a deliberately slow scrypt.
"""

import copy
import hashlib
import hmac
import json
import os
import secrets
import threading
from pathlib import Path

FALLBACK = "admin-fallback.json"  # beside the account file: the hash of the administrator's initial password (修复轮07)
VERIFIED_LIMIT = 64  # credentials remembered as verified; a full memory is emptied, never grown


def _write_through(path: Path, text: str):
    """On the disk before the caller goes on: after a power loss no name points at contents that never got there."""
    with path.open("w", encoding="utf-8") as file:
        file.write(text)
        file.flush()
        os.fsync(file.fileno())


def _digest(secret: str, salt: bytes) -> str:
    return hashlib.scrypt(secret.encode(), salt=salt, n=2**14, r=8, p=1).hex()


def write_fallback(path: Path, secret: str):
    """The administrator's initial password as the fallback, beside the account file `path` (修复轮07, 终版 §2.3): written
    by the deployment that makes the administrator, never by a run, which only reads it."""
    salt = secrets.token_bytes(16)
    target = path.with_name(FALLBACK)
    target.parent.mkdir(exist_ok=True, parents=True)
    temp = target.with_suffix(".tmp")
    _write_through(temp, json.dumps({"salt": salt.hex(), "digest": _digest(secret, salt)}))
    temp.replace(target)


class Accounts:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.unreadable = None  # why the file could not be read: then only the fallback signs in
        try:
            self.users = json.loads(path.read_text()) if path.exists() else {}
            if not isinstance(self.users, dict):
                raise ValueError("not an account table")
        except (OSError, ValueError) as error:  # what a power loss can leave: empty, torn or zeroed
            self.users, self.unreadable = {}, type(error).__name__
        self.record = None  # writes one management record to the archive (set by the runtime); None: no archive at all
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
        self._key = secrets.token_bytes(32)  # this process only: what is remembered is never the credential itself
        self._verified: dict[bytes, dict] = {}
        self._version = 0  # every change: a check that began before it is not remembered

    def verify(self, credential):
        if not credential or ":" not in credential:
            return None
        seen = hmac.new(self._key, credential.encode(), hashlib.sha256).digest()
        known = self._verified.get(seen)
        if known is not None:
            return dict(known)
        version = self._version
        identity = self._check(credential)
        if identity is not None:
            with self.lock:
                if version == self._version:
                    if len(self._verified) >= VERIFIED_LIMIT:
                        self._verified.clear()
                    self._verified[seen] = identity
        return None if identity is None else dict(identity)

    def _check(self, credential):
        username, token = credential.split(":", 1)
        user = self.users.get(username)
        if user and user["active"]:
            if hmac.compare_digest(_digest(token, bytes.fromhex(user["salt"])), user["digest"]):
                return {"username": username, "role": user["role"]}
        if username == "admin" and self.fallback:  # 修复轮07: the initial password signs the administrator in, whatever the accounts
            if hmac.compare_digest(_digest(token, self.fallback["salt"]), self.fallback["digest"]):
                return {"username": "admin", "role": "admin"}
        return None

    def create(self, username, role, audit=None, token=None):
        if not username or len(username) > 64 or not all(c.isalnum() or c in "-_" for c in username):
            raise ValueError("用户名只能用字母、数字、- 和 _，最长 64 个字符")
        if role not in ("admin", "viewer"):
            raise ValueError("角色只能是管理员或只读用户")
        with self.lock:
            if username in self.users:
                raise ValueError("该用户名已存在")
            if role == "admin" and any(user["role"] == "admin" for user in self.users.values()):
                raise ValueError("已有管理员：只能添加只读用户")
            token, salt = token or secrets.token_urlsafe(32), secrets.token_bytes(16)  # the deployment brings its own (修复轮07)
            candidate = copy.deepcopy(self.users)
            candidate[username] = {"role": role, "salt": salt.hex(), "digest": _digest(token, salt), "active": True}
            self.save(candidate, audit)
        return username + ":" + token

    def set_secret(self, username, secret, audit=None):
        """A password of the account's own in place of its credential (the administrator's, 修复轮02); the old one stops
        working.  It must survive the login, which trims what is typed and takes at most 256 characters.  An administrator
        whose account the file no longer holds (it was lost) - signed in with the fallback - gets it back this way."""
        if not isinstance(secret, str) or secret != secret.strip() or not 8 <= len(secret) <= 255 - len(username):
            raise ValueError(f"口令须为 8 到 {255 - len(username)} 个字符，首尾不能有空格")
        with self.lock:
            if username not in self.users and username != "admin":
                raise ValueError("没有这个账户")
            salt = secrets.token_bytes(16)
            candidate = copy.deepcopy(self.users)
            candidate.setdefault(username, {"role": "admin", "active": True})
            candidate[username].update(salt=salt.hex(), digest=_digest(secret, salt))
            self.save(candidate, audit)

    def revoke(self, username, audit=None):
        with self.lock:
            if username not in self.users:
                raise ValueError("没有这个账户")
            if (
                self.users[username]["role"] == "admin"
                and sum(u["active"] and u["role"] == "admin" for u in self.users.values()) <= 1
            ):
                raise ValueError("不能停用唯一的管理员")
            candidate = copy.deepcopy(self.users)
            candidate[username]["active"] = False
            self.save(candidate, audit)

    def save(self, candidate, audit=None):
        """The new file is on the disk first, the change is recorded, and only then does the file take its place: a change
        whose record cannot be written is not made (the error goes to the caller), and a power loss leaves the old file or
        the new one, never a torn one."""
        self.path.parent.mkdir(exist_ok=True, parents=True)
        temp = self.path.with_suffix(".tmp")
        _write_through(temp, json.dumps(candidate))
        try:
            if audit is not None and self.record is not None:
                self.record(audit)
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
        temp.replace(self.path)
        self.users = candidate
        self._version += 1
        self._verified.clear()
