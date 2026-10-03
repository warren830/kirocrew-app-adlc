"""Remove a pack's direct-mode resources (only those: by their direct names and the ids the run recorded).

Order matters: the Harness first (it holds the runtime), then the memory, the Gateway target and Gateway, the
tools Lambda, the knowledge base (through the release's own create_kb.py ``delete_kb``, which also removes its
vector bucket, index and roles), the pack's objects in the direct bucket, and the three roles. Each step
reports what it did; a resource that is already gone is not an error. The Workshop's resources are never
touched: no direct name equals a Workshop name (``names.DirectNames``).
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from .aws import RETRY_ENV, client, tools_python, with_vendor
from .names import DirectNames


def _gone(exc: BaseException) -> bool:
    code = str(((getattr(exc, "response", None) or {}).get("Error") or {}).get("Code") or "")
    return code in ("ResourceNotFoundException", "NoSuchEntity", "NoSuchBucket", "NotFoundException")


def cleanup(session: Any, names: DirectNames, *, release_dir: Path, region: str, profile: str | None, state_path: Path,
            log: Callable[[str], None] = print, sleep: Callable[[float], None] = time.sleep) -> dict[str, str]:
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
    ctl = client(session, "bedrock-agentcore-control", region)
    done: dict[str, str] = {}

    def step(name: str, action: Callable[[], str]) -> None:
        try:
            done[name] = action()
        except Exception as exc:  # noqa: BLE001
            done[name] = "already gone" if _gone(exc) else f"failed: {type(exc).__name__}: {str(exc)[:200]}"
        log(f"  {name}: {done[name]}")

    def harness() -> str:
        # The current name, the recorded id, and the name used before 2026-10-01 (``<agent>_direct``, which the
        # Workshop's selectors match: a leftover one trips the preflight guard and 09/13's runtime lookup).
        recorded = str((state.get("harness") or {}).get("id") or "")
        wanted = {names.harness, f"{names.agent}_direct"}
        found = [h for h in ctl.list_harnesses().get("harnesses") or []
                 if h.get("harnessName") in wanted or (recorded and h.get("harnessId") == recorded)]
        for h in found:
            ctl.delete_harness(harnessId=h["harnessId"])
            for _ in range(60):
                if not [x for x in ctl.list_harnesses().get("harnesses") or [] if x.get("harnessId") == h["harnessId"]]:
                    break
                sleep(5)
        return f"deleted {len(found)}" if found else "already gone"

    def memory() -> str:
        found = [m for m in ctl.list_memories().get("memories") or [] if str(m.get("id", "")).startswith(names.memory + "-")]
        for m in found:
            ctl.delete_memory(memoryId=m["id"])
        return f"deleted {len(found)}" if found else "already gone"

    def gateway() -> str:
        found = [g for g in ctl.list_gateways().get("items") or [] if g.get("name") == names.gateway]
        for g in found:
            for t in ctl.list_gateway_targets(gatewayIdentifier=g["gatewayId"]).get("items") or []:
                ctl.delete_gateway_target(gatewayIdentifier=g["gatewayId"], targetId=t["targetId"])
            for _ in range(36):
                if not ctl.list_gateway_targets(gatewayIdentifier=g["gatewayId"]).get("items"):
                    break
                sleep(5)
            ctl.delete_gateway(gatewayIdentifier=g["gatewayId"])
        return f"deleted {len(found)}" if found else "already gone"

    def tools_lambda() -> str:
        client(session, "lambda", region).delete_function(FunctionName=names.lambda_function)
        return "deleted"

    def knowledge_base() -> str:
        script = ("import importlib.util, sys\n"
                  "spec = importlib.util.spec_from_file_location('create_kb', sys.argv[1]); m = importlib.util.module_from_spec(spec)\n"
                  "spec.loader.exec_module(m)\n"
                  "m.KnowledgeBasesForAmazonBedrock().delete_kb(sys.argv[2], delete_s3_bucket=False, delete_iam_roles_and_policies=True)\n")
        env = with_vendor({**os.environ, **RETRY_ENV, "AWS_DEFAULT_REGION": region, "AWS_REGION": region, "PYTHONDONTWRITEBYTECODE": "1"})
        if profile:
            env["AWS_PROFILE"] = profile
        out = subprocess.run([tools_python(), "-c", script, str(release_dir / "knowledge-base" / "create_kb.py"), names.knowledge_base],
                             capture_output=True, text=True, env=env, timeout=900)
        if out.returncode != 0:
            raise RuntimeError((out.stderr or out.stdout).strip()[-300:])
        return "deleted"

    def objects() -> str:
        s3 = client(session, "s3", region)
        count = 0
        for prefix in (names.kb_prefix, names.skills_prefix):
            for page in s3.get_paginator("list_objects_v2").paginate(Bucket=names.bucket, Prefix=prefix):
                keys = [{"Key": o["Key"]} for o in page.get("Contents") or []]
                if keys:
                    s3.delete_objects(Bucket=names.bucket, Delete={"Objects": keys})
                    count += len(keys)
        return f"deleted {count} object(s) (the shared bucket {names.bucket} stays)"

    def roles() -> str:
        iam = client(session, "iam")
        removed = []
        for role in (names.harness_role, names.gateway_role, names.lambda_role):
            try:
                for policy in iam.list_role_policies(RoleName=role).get("PolicyNames") or []:
                    iam.delete_role_policy(RoleName=role, PolicyName=policy)
                for attached in iam.list_attached_role_policies(RoleName=role).get("AttachedPolicies") or []:
                    iam.detach_role_policy(RoleName=role, PolicyArn=attached["PolicyArn"])
                iam.delete_role(RoleName=role)
                removed.append(role)
            except Exception as exc:  # noqa: BLE001
                if not _gone(exc):
                    raise
        return f"deleted {', '.join(removed)}" if removed else "already gone"

    for name, action in (("harness", harness), ("memory", memory), ("gateway", gateway), ("tools lambda", tools_lambda),
                         ("knowledge base", knowledge_base), ("objects", objects), ("roles", roles)):
        step(name, action)
    if state_path.is_file() and not any(v.startswith("failed") for v in done.values()):
        state_path.unlink()
    return done
