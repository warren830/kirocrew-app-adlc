"""The console's ``/api/console`` routes (``app/console/server.py`` serves them; each handler gets a ``Request``).

Workspaces, users and API keys are admin routes; everything under ``/workspaces/{wid}/...`` checks the caller's
grant to that workspace and runs in its verified session. The platform modules add their own routes here.
"""
from __future__ import annotations

import re
import secrets
from pathlib import Path
from typing import Any

from . import (agents, assistant, autopilot, claude_sdk, deploy, evaluation, experiments, governance, kb, observability, registry, runtime_canary,
               skills_lab, studio)
from .web import sse
from .workspaces import _client

SPOKE_TEMPLATE = Path(__file__).resolve().parents[3] / "app" / "console" / "spoke-role.yaml"

#: The capability modules; each adds its own routes (``register(router)``).
MODULES = (kb, deploy, studio, claude_sdk, assistant, autopilot, evaluation, experiments, runtime_canary, registry, governance, observability, skills_lab)


def hub_principal(console: Any) -> dict[str, Any]:
    """The IAM ARN a spoke role must trust for the console's own credentials: a user as it is, an assumed role as its
    role (with the role's path, which the session ARN leaves out: an SSO role lives under aws-reserved/…)."""
    base = console.workspaces._boto()
    arn = _client(base, "sts").get_caller_identity()["Arn"]
    m = re.match(r"^arn:aws:sts::(\d{12}):assumed-role/([^/]+)/", arn)
    if not m:
        return {"arn": arn, "exact": True}
    try:
        return {"arn": _client(base, "iam").get_role(RoleName=m.group(2))["Role"]["Arn"], "exact": True}
    except Exception:  # noqa: BLE001 - no iam:GetRole: the ARN without its path, which a pathed role will not match
        return {"arn": f"arn:aws:iam::{m.group(1)}:role/{m.group(2)}", "exact": False}


SPOKE_STACK = "adlc-console-spoke"


def spoke_role(console: Any) -> dict[str, Any]:
    """The template another account deploys, with this console's identity and a fresh External ID filled in, and the
    steps after it: the stack's outputs ``RoleArn`` and ``PermissionsBoundaryArn`` go into the workspace with the same
    External ID (the spoke role creates and changes roles only with that boundary, so the workspace must name it)."""
    try:
        hub, problem = hub_principal(console), None
    except Exception as exc:  # noqa: BLE001 - shown on the page
        hub, problem = {"arn": None, "exact": False}, f"{type(exc).__name__}: {str(exc)[:200]}"
    external = secrets.token_urlsafe(24)
    command = (f"aws cloudformation deploy --template-file spoke-role.yaml --stack-name {SPOKE_STACK} --capabilities CAPABILITY_NAMED_IAM "
               f"--parameter-overrides HubPrincipalArn={hub['arn'] or '<the console role or user ARN>'} ExternalId={external}")
    outputs = f"aws cloudformation describe-stacks --stack-name {SPOKE_STACK} --query 'Stacks[0].Outputs' --output table"
    runbook = [f"1. In the other account and region: {command}",
               f"2. Read its outputs: {outputs}",
               "3. In 工作区, add the workspace with that account and region: 跨账号角色 ARN = RoleArn, External ID = " + external
               + ", 权限边界 ARN = PermissionsBoundaryArn (every role the console creates there carries it; without it the spoke role refuses to "
               "create any).",
               "4. A spoke stack deployed from an older template: deploy this one over it with the same command. Every role the console creates "
               "is on the IAM path /adlc-console/, the only roles the spoke role creates, tags, passes or deletes; a console role made there "
               "before on / is refused (the account's owner deletes it, and the console creates it again on the path)."]
    return {"template": SPOKE_TEMPLATE.read_text(encoding="utf-8"), "hubPrincipal": hub["arn"], "exact": hub["exact"], "externalId": external,
            "command": command, "outputsCommand": outputs, "boundaryOutput": "PermissionsBoundaryArn", "runbook": runbook, "error": problem}


def register(router: Any) -> None:
    add = router.add

    # -- who, workspaces, users, keys ------------------------------------------------
    add("GET", "/me", lambda r: (200, r.caller))
    def shown(w: dict[str, Any]) -> dict[str, Any]:  # the External ID guards the spoke role: never sent back once saved
        return {**{k: v for k, v in w.items() if k != "externalId"}, "externalIdSet": bool(w.get("externalId"))}

    add("GET", "/workspaces", lambda r: (200, {"workspaces": [shown(w) for w in r.console.workspaces.list()
                                                              if r.caller["role"] == "admin" or "*" in r.caller["workspaces"]
                                                              or w["id"] in r.caller["workspaces"]]}))
    add("POST", "/workspaces", lambda r: (201, shown(r.console.workspaces.put(r.body))), admin=True)
    add("DELETE", "/workspaces/{wid}", lambda r: (r.console.workspaces.delete(r.params["wid"]), (200, {"deleted": r.params["wid"]}))[1], admin=True)
    add("POST", "/workspaces/{wid}/verify", lambda r: (200, r.console.workspaces.verify(r.workspace())))
    add("GET", "/users", lambda r: (200, {"users": r.console.auth.users(), "open": r.console.auth.open_mode()}), admin=True)
    add("POST", "/users", lambda r: (201, r.console.auth.put_user(r.body)), admin=True)
    add("DELETE", "/users/{name}", lambda r: (r.console.auth.delete_user(r.params["name"]), (200, {"deleted": r.params["name"]}))[1], admin=True)
    add("GET", "/keys", lambda r: (200, {"keys": r.console.auth.keys()}), admin=True)
    add("POST", "/keys", lambda r: (r.console.workspaces.get(str(r.body.get("workspace") or "")),  # a key for a workspace that exists
                                    (201, r.console.auth.issue_key(workspace=str(r.body.get("workspace") or ""), agent=r.body.get("agent"),
                                                                   label=str(r.body.get("label") or "key"))))[1], admin=True)
    add("DELETE", "/keys/{kid}", lambda r: (r.console.auth.revoke_key(r.params["kid"]), (200, {"revoked": r.params["kid"]}))[1], admin=True)
    add("GET", "/spoke-role", lambda r: (200, spoke_role(r.console)), admin=True)

    # -- jobs (a page polls the long ones) -------------------------------------------------
    def job(r: Any):
        try:
            found = r.console.jobs.get(r.params["jid"])
        except (KeyError, OSError):
            return 404, {"error": f"no job {r.params['jid']}"}
        from .auth import Auth

        Auth.may(r.caller, found.get("workspace"))
        return 200, found

    add("GET", "/jobs/{jid}", job)

    # -- agents and chat --------------------------------------------------------------
    def region(r: Any) -> str:
        return r.console.workspaces.get(r.workspace())["region"]

    def account(r: Any) -> str:
        return r.console.workspaces.get(r.workspace())["accountId"]

    add("GET", "/workspaces/{wid}/agents", lambda r: (200, {"agents": agents.list_agents(r.session(), region(r))}))
    add("GET", "/workspaces/{wid}/agents/{kind}/{ident}", lambda r: (200, agents.get_agent(r.session(), region(r), r.params["kind"], r.params["ident"])))
    add("POST", "/workspaces/{wid}/agents", lambda r: (201, agents.create_harness(
        r.session(), account=account(r), region=region(r), body=r.body,
        boundary=r.console.workspaces.get(r.workspace()).get("permissionsBoundaryArn"))))

    def update(r: Any):
        with experiments.one_at_a_time(r.params["ident"]):  # an experiment starting on it now waits for this edit, or this edit for it
            active = [e for e in (r.console.store.read("experiments", {}) or {}).values() if e.get("workspace") == r.workspace()
                      and (e.get("agent") or {}).get("id") == r.params["ident"] and e.get("status") in experiments.ACTIVE + ("promoting",)]
            if active:  # the A/B test's evidence is about the version it compared, paused or promoting as much as running
                raise agents.AgentError(f"this agent is in an A/B test ({active[0]['status']}): promote, stop or clean it up before changing it "
                                        "directly")
            twin = experiments.twin_of(r.console, r.workspace(), r.params["ident"])
            if twin:  # the treatment Harness the A/B test runs: its evidence is about the version set up
                raise agents.AgentError(f"this Harness is the treatment of the A/B test {twin['id']} ({twin['status']}): change the treatment "
                                        "through a new bundle version and experiment, not here")
            return 200, agents.update_harness(r.session(), region=region(r), ident=r.params["ident"], body=r.body)

    # a direct change skips the evidence an A/B promotion needs: admins only (and another team's agent only when acknowledged)
    add("PUT", "/workspaces/{wid}/agents/harness/{ident}", update, admin=True)
    add("DELETE", "/workspaces/{wid}/agents/harness/{ident}", lambda r: (200, agents.delete_agent(r.session(), region=region(r), ident=r.params["ident"])))

    def chat(r: Any) -> Any:
        session = r.session()
        agent = agents.get_agent(session, region(r), r.params["kind"], r.params["ident"])
        message = str(r.body.get("message") or "")
        sid = agents.check_turn(message, r.body.get("sessionId") or None)  # refused as a 400 now, not inside a started stream
        actor = str(r.body.get("actorId") or r.caller["username"])
        events, _route = runtime_canary.routed_turn(r.console, r.workspace(), session, region(r), agent, message=message, session_id=sid, actor=actor,
                                                    bypass=bool(r.body.get("bypassExperiment")), prompt=r.body.get("systemPrompt") or None,
                                                    model=r.body.get("model") or None)
        return sse(events)

    add("POST", "/workspaces/{wid}/agents/{kind}/{ident}/chat", chat)

    for module in MODULES:
        module.register(router)
