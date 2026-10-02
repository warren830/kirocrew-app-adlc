"""Allow-listed SSM contract for one Guided Workshop step."""
from __future__ import annotations

import json
import re
from typing import Any

from .guided_run import STEP_BY_ID

DOCUMENT_NAME = "WorkshopCustomizerRunStep"
_VERSION_RE = re.compile(r"^[a-z][a-z0-9-]{2,60}-[0-9a-f]{12}$")
_INSTANCE_RE = re.compile(r"^i-[0-9a-f]{8,17}$")


def command_request(*, step_id: str, release_version: str, instance_id: str) -> dict[str, Any]:
    if step_id not in STEP_BY_ID:
        raise ValueError("step is not in the Workshop Guide allowlist")
    if not _VERSION_RE.fullmatch(release_version):
        raise ValueError("invalid release version")
    if not _INSTANCE_RE.fullmatch(instance_id):
        raise ValueError("invalid instance id")
    step = STEP_BY_ID[step_id]
    return {
        "DocumentName": DOCUMENT_NAME,
        "InstanceIds": [instance_id],
        "Parameters": {"ReleaseVersion": [release_version], "StepId": [step_id], "Script": [step.script]},
    }


MALFORMED_OUTPUT = "step output truncated or malformed"


def parse_invocation(invocation: dict[str, Any]) -> dict[str, Any]:
    """Read one terminal GetCommandInvocation result of the RunStep document.

    The document prints exactly one JSON object (budgeted below SSM's output cap). Anything else — no
    output, a cut-off or non-JSON line, a non-object — is a failed step with :data:`MALFORMED_OUTPUT`,
    never a pass: evidence that did not arrive cannot be assumed. ``documentVersion`` records which
    version of the SSM document produced the result.
    """
    status = str(invocation.get("Status") or "")
    if status not in ("Success", "Failed", "TimedOut", "Cancelled"):
        raise ValueError(f"command is not terminal: {status or 'unknown'}")
    stdout = str(invocation.get("StandardOutputContent") or "")
    stderr = str(invocation.get("StandardErrorContent") or "")
    version = invocation.get("DocumentVersion")
    document_version = str(version) if isinstance(version, (str, int)) and str(version) else None
    payload: dict[str, Any] | None = None
    if stdout.strip():
        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError:
            parsed = None
        payload = parsed if isinstance(parsed, dict) else None
    elif status != "Success":
        payload = {}
    if payload is None:
        return {
            "passed": False,
            "summary": MALFORMED_OUTPUT,
            "outputs": {"outputError": MALFORMED_OUTPUT, "stdoutChars": len(stdout), "stdoutTail": stdout[-1000:]},
            "error": f"{MALFORMED_OUTPUT} (SSM status {status}, {len(stdout)} characters of output)"
                     + (f"; stderr: {stderr[-500:]}" if stderr.strip() else ""),
            "documentVersion": document_version,
        }
    passed = status == "Success" and payload.get("status", "passed") == "passed"
    return {
        "passed": passed,
        "summary": str(payload.get("summary") or ("step passed" if passed else "step failed")),
        "outputs": payload.get("outputs") if isinstance(payload.get("outputs"), dict) else {},
        "error": "" if passed else str(payload.get("error") or stderr or status),
        "documentVersion": document_version,
    }


def send_step(ssm: Any, *, step_id: str, release_version: str, instance_id: str) -> str:
    response = ssm.send_command(**command_request(step_id=step_id, release_version=release_version, instance_id=instance_id))
    command_id = str((response.get("Command") or {}).get("CommandId") or "")
    if not command_id:
        raise ValueError("SSM returned no command id")
    return command_id


def poll_step(ssm: Any, *, command_id: str, instance_id: str) -> dict[str, Any]:
    invocation = ssm.get_command_invocation(CommandId=command_id, InstanceId=instance_id)
    status = str(invocation.get("Status") or "")
    if status in ("Pending", "InProgress", "Delayed"):
        return {"terminal": False, "status": status}
    return {"terminal": True, "status": status, **parse_invocation(invocation)}
