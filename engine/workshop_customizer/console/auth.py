"""Console users (admin / member, workspace grants), their sessions, and the public API's keys.

With no user defined the console is in *open* mode: bound to localhost, everything is allowed (one SA on their own
machine). Once an admin exists, every request needs a session cookie (``POST /api/console/login``); a member sees
and acts only in the workspaces granted to them; only admins manage workspaces, users and keys. Passwords are
PBKDF2-SHA256 (200 000 rounds, per-user salt). API keys for ``/v1`` are shown once (``adlc_live_<random>``) and stored
as SHA-256 hashes, each scoped to one workspace and optionally one agent.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from typing import Any

from .store import Store

USERNAME = re.compile(r"^[a-z][a-z0-9_.-]{1,31}$")
ROLES = ("admin", "member")
SESSION_SECONDS = 12 * 3600
ROUNDS = 200_000
KEY_PREFIX = "adlc_live_"


class AuthError(PermissionError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), ROUNDS).hex()
    return f"pbkdf2_sha256${ROUNDS}${salt}${digest}"


def password_ok(password: str, stored: str) -> bool:
    try:
        _algo, rounds, salt, digest = stored.split("$")
    except ValueError:
        return False
    check = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), int(rounds)).hex()
    return hmac.compare_digest(check, digest)


class Auth:
    def __init__(self, store: Store):
        self.store = store
        self._sessions: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    # -- users ----------------------------------------------------------------
    def users(self) -> list[dict[str, Any]]:
        return [{k: v for k, v in u.items() if k != "password"} for u in sorted(self.store.read("users", {}).values(), key=lambda u: u["username"])]

    def open_mode(self) -> bool:
        """No user defined yet. A users file that cannot be read raises (closed), and the last admin cannot be removed,
        so the console never falls back to open mode by accident."""
        return not self.store.read("users", {})

    def put_user(self, body: dict[str, Any]) -> dict[str, Any]:
        """Create a user, or change the fields given of an existing one (a field left out keeps its value)."""
        name = str(body.get("username") or "").strip()
        if not USERNAME.match(name):
            raise AuthError("username must be 2-32 lowercase letters, digits, '.', '_' or '-'")
        users = self.store.read("users", {})
        prior = users.get(name) or {}
        role = str(body.get("role") or prior.get("role") or "member")
        if role not in ROLES:
            raise AuthError("role must be admin or member")
        password = body.get("password") or None
        if password is not None and (not isinstance(password, str) or len(password) < 10):
            raise AuthError("a password needs at least 10 characters")
        if not prior and password is None:
            raise AuthError("a new user needs a password of at least 10 characters")
        if not users and role != "admin":
            raise AuthError("the first user must be an admin")
        if prior.get("role") == "admin" and role != "admin" and sum(u["role"] == "admin" for u in users.values()) == 1:
            raise AuthError("keep at least one admin: make another user an admin first")
        grants = sorted({str(w) for w in body["workspaces"] or []}) if "workspaces" in body else list(prior.get("workspaces") or [])

        def change(all_: dict[str, Any]) -> dict[str, Any]:
            before = all_.get(name) or {}
            all_[name] = {"username": name, "role": role, "workspaces": grants, "createdAt": before.get("createdAt") or _now(),
                          "password": hash_password(password) if password else before["password"]}
            return all_

        self.store.update("users", {}, change)
        if prior and (password or role != prior.get("role") or grants != list(prior.get("workspaces") or [])):
            with self._lock:  # a changed password, role or grant ends the user's sessions
                self._sessions = {t: s for t, s in self._sessions.items() if s[0] != name}
        return next(u for u in self.users() if u["username"] == name)

    def delete_user(self, name: str) -> None:
        users = self.store.read("users", {})
        if name not in users:
            raise AuthError(f"no user {name}")
        if users[name]["role"] == "admin" and sum(u["role"] == "admin" for u in users.values()) == 1:
            raise AuthError("the last admin cannot be removed (the console would reopen to anyone); make another admin first")
        self.store.update("users", {}, lambda all_: {k: v for k, v in all_.items() if k != name})
        with self._lock:
            self._sessions = {t: s for t, s in self._sessions.items() if s[0] != name}

    # -- sessions ---------------------------------------------------------------
    def login(self, username: str, password: str) -> str:
        user = self.store.read("users", {}).get(username)
        if not user or not password_ok(password, user["password"]):
            time.sleep(0.3)
            raise AuthError("wrong username or password")
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[token] = (username, time.time() + SESSION_SECONDS)
        return token

    def logout(self, token: str) -> None:
        with self._lock:
            self._sessions.pop(token, None)

    def whoami(self, token: str | None) -> dict[str, Any]:
        """The caller: ``{"username", "role", "workspaces"}``; open mode is an admin with every workspace."""
        if self.open_mode():
            return {"username": "local", "role": "admin", "workspaces": ["*"], "open": True}
        with self._lock:
            entry = self._sessions.get(token or "")
        if not entry or entry[1] < time.time():
            raise AuthError("sign in first")
        user = self.store.read("users", {}).get(entry[0])
        if not user:
            raise AuthError("sign in first")
        return {"username": user["username"], "role": user["role"], "workspaces": user["workspaces"], "open": False}

    @staticmethod
    def may(caller: dict[str, Any], workspace: str | None = None, *, admin: bool = False) -> None:
        if admin and caller["role"] != "admin":
            raise AuthError("this needs an admin")
        if workspace and caller["role"] != "admin" and "*" not in caller["workspaces"] and workspace not in caller["workspaces"]:
            raise AuthError(f"no access to workspace {workspace}")

    # -- API keys ----------------------------------------------------------------
    def keys(self) -> list[dict[str, Any]]:
        return [{k: v for k, v in key.items() if k != "hash"} for key in sorted(self.store.read("keys", {}).values(), key=lambda k: k["createdAt"])]

    def issue_key(self, *, workspace: str, agent: str | None, label: str) -> dict[str, Any]:
        secret = KEY_PREFIX + secrets.token_urlsafe(24)
        kid = secrets.token_hex(6)
        record = {"id": kid, "prefix": secret[:len(KEY_PREFIX) + 6], "hash": hashlib.sha256(secret.encode()).hexdigest(), "workspace": workspace,
                  "agent": agent or None, "label": label[:80], "createdAt": _now(), "lastUsedAt": None, "revoked": False}
        self.store.update("keys", {}, lambda all_: {**all_, kid: record})
        return {**{k: v for k, v in record.items() if k != "hash"}, "key": secret}

    def revoke_key(self, kid: str) -> None:
        def change(all_: dict[str, Any]) -> dict[str, Any]:
            if kid not in all_:
                raise AuthError(f"no key {kid}")
            all_[kid]["revoked"] = True
            return all_

        self.store.update("keys", {}, change)

    def key_scope(self, secret: str | None) -> dict[str, Any]:
        """The key's ``{workspace, agent}`` for a presented ``X-Api-Key``; refuses unknown and revoked keys."""
        if not secret or not secret.startswith(KEY_PREFIX):
            raise AuthError("missing or malformed X-Api-Key")
        digest = hashlib.sha256(secret.encode()).hexdigest()
        found = next((k for k in self.store.read("keys", {}).values() if hmac.compare_digest(k["hash"], digest)), None)
        if not found or found["revoked"]:
            raise AuthError("unknown or revoked API key")

        def touch(all_: dict[str, Any]) -> dict[str, Any]:
            all_[found["id"]]["lastUsedAt"] = _now()
            return all_

        self.store.update("keys", {}, touch)
        return {"workspace": found["workspace"], "agent": found["agent"], "keyId": found["id"]}
