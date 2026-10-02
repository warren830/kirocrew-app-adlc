#!/usr/bin/env python3
"""Give the deployed Workshop harness read access to its actual private ECR image.

The pinned CLI grants ECR pull permissions for an explicitly configured container, but the
service-selected default image needs the same permissions. This fixed host helper derives the
repository from GetAgentRuntime and confines the role to the active release's namespace.
"""
from __future__ import annotations

import argparse
import json
import os
import pwd
import grp
import re
import subprocess
import time
from pathlib import Path

IMAGE_RE = re.compile(
    r"^(?P<account>\d{12})\.dkr\.ecr\.(?P<region>[a-z0-9-]+)\.amazonaws\.com(?:\.cn)?/"
    r"(?P<repository>[a-z0-9]+(?:[._/-][a-z0-9]+)*)(?::[\w.-]+|@sha256:[0-9a-f]{64})$"
)


def image_pull_policy(uri: str, partition: str = "aws") -> dict:
    match = IMAGE_RE.fullmatch(uri)
    if not match:
        raise ValueError("runtime image is not a supported private ECR image URI")
    fields = match.groupdict()
    repository = f"arn:{partition}:ecr:{fields['region']}:{fields['account']}:repository/{fields['repository']}"
    return {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["ecr:GetAuthorizationToken"], "Resource": "*"},
        {"Effect": "Allow", "Action": [
            "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability",
        ], "Resource": repository},
    ]}


def aws(region: str, service: str, operation: str, *args: str) -> dict:
    result = subprocess.run(
        ["aws", service, operation, *args, "--region", region, "--output", "json"],
        capture_output=True, text=True, timeout=90,
    )
    if result.returncode:
        raise RuntimeError(f"{service} {operation}: {result.stderr.strip()[-1500:]}")
    return json.loads(result.stdout) if result.stdout.strip() else {}


def configure(contract: dict, version: str) -> dict:
    if not re.fullmatch(r"[a-z][a-z0-9-]{2,40}-[0-9a-f]{12}", version):
        raise ValueError("invalid release version")
    root = Path(contract["targetRoot"])
    release = root / "releases" / version
    if (root / "current").resolve() != release.resolve():
        raise ValueError("requested release is not active")
    manifest = json.loads((release / "RELEASE.json").read_text())
    agent = manifest["namespace"]["agentName"]
    if not re.fullmatch(r"[a-z][a-z0-9_]{2,39}", agent):
        raise ValueError("invalid harness namespace")
    region = contract["region"]
    state = json.loads((root / agent / "agentcore/.cli/deployed-state.json").read_text())
    harness = state["targets"]["default"]["resources"]["harnesses"][agent]
    account = aws(region, "sts", "get-caller-identity")["Account"]
    role_arn = harness["roleArn"]
    partition = role_arn.split(":")[1]
    role_name = f"{agent}_{agent}"
    if role_arn != f"arn:{partition}:iam::{account}:role/{role_name}":
        raise ValueError("harness role is outside the bound Workshop namespace/account")
    live = aws(region, "bedrock-agentcore-control", "get-harness", "--harness-id", harness["harnessId"])["harness"]
    runtime_arn = harness.get("agentRuntimeArn") or live["environment"]["agentCoreRuntimeEnvironment"]["agentRuntimeArn"]
    runtime = aws(region, "bedrock-agentcore-control", "get-agent-runtime",
                  "--agent-runtime-id", runtime_arn.rsplit("/", 1)[-1])
    if runtime["roleArn"] != role_arn:
        raise ValueError("runtime and harness roles differ")
    image = runtime["agentRuntimeArtifact"]["containerConfiguration"]["containerUri"]
    policy = image_pull_policy(image, partition)
    aws(region, "iam", "put-role-policy", "--role-name", role_name,
        "--policy-name", "WorkshopHarnessImagePull", "--policy-document", json.dumps(policy))
    # Materialize the service-selected image as an explicit artifact after granting pull access.
    # This refreshes the runtime's image configuration and makes later CDK deployments retain it.
    updates = []
    if not (live.get("environmentArtifact") or {}).get("containerConfiguration"):
        updates.extend(["--environment-artifact", json.dumps({"optionalValue": {"containerConfiguration": {"containerUri": image}}})])
    environment = dict(live.get("environmentVariables") or {})
    # The pinned Workshop scripts query aws/spans. New Runtime deployments otherwise default
    # to unified telemetry in the runtime's own log group.
    if environment.get("UNIFIED_TRACES_DESTINATION_ENABLED") != "false":
        environment["UNIFIED_TRACES_DESTINATION_ENABLED"] = "false"
        updates.extend(["--environment-variables", json.dumps(environment)])
    if updates:
        aws(region, "bedrock-agentcore-control", "update-harness", "--harness-id", harness["harnessId"], *updates)
        for _ in range(60):
            live = aws(region, "bedrock-agentcore-control", "get-harness", "--harness-id", harness["harnessId"])["harness"]
            applied_image = ((live.get("environmentArtifact") or {}).get("containerConfiguration") or {}).get("containerUri")
            applied_trace_mode = (live.get("environmentVariables") or {}).get("UNIFIED_TRACES_DESTINATION_ENABLED")
            if live["status"] in ("READY", "ACTIVE") and applied_image == image and applied_trace_mode == "false":
                break
            if live["status"] in ("FAILED", "DELETING"):
                raise RuntimeError("harness could not initialize its explicit image")
            time.sleep(5)
        else:
            raise RuntimeError("harness image update did not become ready")
    config_path = root / agent / "app" / agent / "harness.json"
    config = json.loads(config_path.read_text())
    if config.get("containerUri") not in (None, image):
        raise ValueError("local harness image differs from the deployed image; deploy the intended configuration first")
    config["containerUri"] = image
    config.setdefault("environmentVariables", {})["UNIFIED_TRACES_DESTINATION_ENABLED"] = "false"
    original_mode = config_path.stat().st_mode & 0o777
    temporary = config_path.with_suffix(".runtime-permissions.tmp")
    temporary.write_text(json.dumps(config, indent=2) + "\n")
    temporary.chmod(original_mode)
    if os.geteuid() == 0 and contract.get("user"):
        os.chown(temporary, pwd.getpwnam(contract["user"]).pw_uid,
                 grp.getgrnam(contract.get("group") or contract["user"]).gr_gid)
    temporary.replace(config_path)
    return {"status": "configured", "roleArn": role_arn, "image": image,
            "repositoryArn": policy["Statement"][1]["Resource"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", default="/etc/workshop-customizer/target.json")
    parser.add_argument("--release-version", required=True)
    args = parser.parse_args()
    print(json.dumps(configure(json.loads(Path(args.contract).read_text()), args.release_version)))


if __name__ == "__main__":
    main()
