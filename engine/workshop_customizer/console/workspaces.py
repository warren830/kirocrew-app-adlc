"""Workspaces: one AWS account and region each, reached through a local profile or an assumed (spoke) role.

A workspace is ``{id, name, accountId, region, profile?, roleArn?, externalId?, permissionsBoundaryArn?}``.
:meth:`Workspaces.session` gives a boto3 session for it: the profile's own, or the profile's credentials used to assume
``roleArn`` (the spoke role another account deploys from ``app/console/spoke-role.yaml``), cached until shortly before
the credentials expire. :meth:`Workspaces.verify` checks that the session really is in ``accountId``; every console call
goes through a verified workspace, so nothing is ever done in an account the SA did not name.

``permissionsBoundaryArn`` (the spoke stack's output ``PermissionsBoundaryArn``, a managed policy in the workspace's
account) is set on every IAM role the console creates in the workspace (``PermissionsBoundary`` on CreateRole: Harness,
knowledge-base, Gateway, runtime, build, A/B test, canary and online-evaluation roles), and a console role made before
the workspace had it gets it before the console changes its policy (PutRolePermissionsBoundary, :func:`agents.
ensure_boundary`). The spoke role creates and changes roles only with that boundary, so a spoke workspace needs it; a
profile workspace in its own account works without one. Every role the console creates, in any workspace, is on its
IAM path ``/adlc-console/`` (``agents.ROLE_PATH``); a workspace with a boundary adopts no role off that path
(``agents.refusal``), while a profile workspace keeps using the console's roles made on ``/`` before roles had a path.
"""
from __future__ import annotations

import re
import threading
import time
from typing import Any, Callable

from .common import now as _now
from .store import Store

WORKSPACE_ID = re.compile(r"^[a-z][a-z0-9-]{1,39}$")
ACCOUNT_ID = re.compile(r"^\d{12}$")
REGION = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d$")
ROLE_ARN = re.compile(r"^arn:aws:iam::\d{12}:role/[\w+=,.@/-]{1,128}$")
POLICY_ARN = re.compile(r"^arn:aws:iam::\d{12}:policy/[\w+=,.@/-]{1,128}$")
#: Assumed-role credentials are refreshed this long before they expire.
REFRESH_SECONDS = 300


class WorkspaceError(ValueError):
    pass


def _client(session: Any, name: str) -> Any:
    """A client with direct mode's standard retries (a local proxy drops TLS connections under load)."""
    try:
        from ..direct.aws import client
    except ImportError:  # pragma: no cover
        return session.client(name)
    try:
        return client(session, name)
    except TypeError:  # a test double without the config keyword
        return session.client(name)


def check(body: dict[str, Any]) -> dict[str, Any]:
    wid = str(body.get("id") or "").strip()
    if not WORKSPACE_ID.match(wid):
        raise WorkspaceError("id must be lowercase kebab-case (2-40 characters)")
    account, region = str(body.get("accountId") or ""), str(body.get("region") or "")
    if not ACCOUNT_ID.match(account):
        raise WorkspaceError("accountId must be 12 digits")
    if not REGION.match(region):
        raise WorkspaceError("region must look like us-west-2")
    profile = str(body.get("profile") or "").strip() or None
    role = str(body.get("roleArn") or "").strip() or None
    if role and not ROLE_ARN.match(role):
        raise WorkspaceError("roleArn must be an IAM role ARN")
    if role and role.split(":")[4] != account:
        raise WorkspaceError("roleArn must be a role in the workspace's account")
    if not profile and not role:
        raise WorkspaceError("give a local AWS profile, or the spoke role to assume")
    boundary = str(body.get("permissionsBoundaryArn") or "").strip() or None
    if boundary and not POLICY_ARN.match(boundary):
        raise WorkspaceError("permissionsBoundaryArn must be an IAM managed policy ARN (the spoke stack's output PermissionsBoundaryArn)")
    if boundary and boundary.split(":")[4] != account:
        raise WorkspaceError("permissionsBoundaryArn must be a policy in the workspace's account")
    return {"id": wid, "name": str(body.get("name") or wid)[:80], "accountId": account, "region": region, "profile": profile,
            "roleArn": role, "externalId": str(body.get("externalId") or "").strip() or None, "permissionsBoundaryArn": boundary}


def boundary_of(ws: Any) -> str | None:
    """The permissions boundary every role the console creates in this workspace carries (None: none)."""
    if not isinstance(ws, dict):
        return None
    return str(ws.get("permissionsBoundaryArn") or "") or None


class Workspaces:
    def __init__(self, store: Store, *, session_factory: Callable[..., Any] | None = None):
        self.store = store
        self._factory = session_factory
        self._cache: dict[str, tuple[Any, float]] = {}
        self._lock = threading.Lock()

    def list(self) -> list[dict[str, Any]]:
        return sorted(self.store.read("workspaces", {}).values(), key=lambda w: w["id"])

    def get(self, wid: str) -> dict[str, Any]:
        found = self.store.read("workspaces", {}).get(wid)
        if not found:
            raise WorkspaceError(f"no workspace {wid}")
        return found

    def put(self, body: dict[str, Any]) -> dict[str, Any]:
        ws = check(body)

        def change(all_: dict[str, Any]) -> dict[str, Any]:
            prior = all_.get(ws["id"]) or {}
            if not ws.get("externalId") and prior.get("externalId") and prior.get("roleArn") == ws.get("roleArn"):
                ws["externalId"] = prior["externalId"]  # it is never shown again: an edit that leaves it out keeps it
            all_[ws["id"]] = {**ws, "createdAt": prior.get("createdAt") or _now(), "updatedAt": _now()}
            return all_

        self.store.update("workspaces", {}, change)
        with self._lock:
            self._cache.pop(ws["id"], None)
        return self.get(ws["id"])

    def delete(self, wid: str) -> None:
        self.get(wid)
        self.store.update("workspaces", {}, lambda all_: {k: v for k, v in all_.items() if k != wid})
        with self._lock:
            self._cache.pop(wid, None)

    def _boto(self, **kwargs: Any) -> Any:
        if self._factory:
            return self._factory(**kwargs)
        import boto3

        return boto3.Session(**kwargs)

    def session(self, wid: str) -> Any:
        """A boto3 session in the workspace (cached; assumed-role credentials refreshed before they expire)."""
        ws = self.get(wid)
        with self._lock:
            cached = self._cache.get(wid)
            if cached and cached[1] > time.time() + REFRESH_SECONDS:
                return cached[0]
        base = self._boto(profile_name=ws.get("profile"), region_name=ws["region"])
        if not ws.get("roleArn"):
            session, expires = base, float("inf")
        else:
            request = {"RoleArn": ws["roleArn"], "RoleSessionName": "adlc-console", "DurationSeconds": 3600}
            if ws.get("externalId"):
                request["ExternalId"] = ws["externalId"]
            creds = _client(base, "sts").assume_role(**request)["Credentials"]
            session = self._boto(aws_access_key_id=creds["AccessKeyId"], aws_secret_access_key=creds["SecretAccessKey"],
                                 aws_session_token=creds["SessionToken"], region_name=ws["region"])
            expiry = creds["Expiration"]
            expires = expiry.timestamp() if hasattr(expiry, "timestamp") else time.time() + 3000
        with self._lock:
            self._cache[wid] = (session, expires)
        return session

    def verify(self, wid: str) -> dict[str, Any]:
        """The caller identity of the workspace's session; refuses a session in another account."""
        ws = self.get(wid)
        identity = _client(self.session(wid), "sts").get_caller_identity()
        if identity["Account"] != ws["accountId"]:
            with self._lock:
                self._cache.pop(wid, None)
            raise WorkspaceError(f"workspace {wid} resolves to account {identity['Account']}, not {ws['accountId']}")
        return {"account": identity["Account"], "arn": identity["Arn"], "region": ws["region"]}
