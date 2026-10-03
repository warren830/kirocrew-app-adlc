"""The console's code deployment (BYOC): an agent written in code, deployed to AgentCore Runtime as a staged job.

Three sources (``source``):

* ``zip`` — Python code with its entry point (``main.py``) at the zip's root: **direct code deploy**
  (``codeConfiguration``: AgentCore takes the zip from S3 and runs it on its managed Python, ARM64). A
  ``requirements.txt`` is installed first by CodeBuild on ARM, as linux/aarch64 wheels into the bundle (nothing is
  installed or run on the console's machine); a zip without one is deployed as uploaded.
* ``dockerfile`` — a Docker build context (a zip with the ``Dockerfile`` at its root): CodeBuild builds it for
  linux/arm64 and pushes it to the runtime's own ECR repository; the runtime runs the image by digest.
* ``image`` — an existing private ECR image in the workspace's account and region, pinned to its digest.

A deployment is a console job (``deploy``) of six stages — seven for a container (``dockerfile`` / ``image``), whose
image is scanned before a runtime runs it — each kept in ``progress.stages`` with its status
(``pending | running | succeeded | skipped | failed``), detail and seconds:

1. **validate** — the name (free for a new runtime), the archive (no unsafe paths or links, the entry point or the
   Dockerfile at its root; one top-level folder is lifted out), the image (in this account and region, its digest),
   and a VPC network's subnets and security groups (read-only EC2 calls: they exist, in one VPC, in an Availability
   Zone AgentCore supports there);
2. **upload** — the archive to the console bucket ``adlc-console-<account>-<region>`` under
   ``deployments/<name>/<job>/`` (the bucket is created when missing: versioned, private, tagged);
3. **build** — when needed, in the CodeBuild project ``adlc-console-build`` (ARM, its own role
   ``adlc-console-build-<region>``: IAM is account-global and the role's policy names the region's bucket and log
   group, so each region has its own; a console made before used one ``adlc-console-build``, left as it is), with the
   console's buildspec passed on every build (an uploaded buildspec.yml is never used); the runtime's own image
   repository scans on push (made so, or turned on for one made before the gate);
4. **scan** (containers) — Launchpad's gate: DescribeImageScanFindings every 5 s for up to 300 s; findings of a
   blocking severity (``scanBlock``, CRITICAL by default, ``[]`` only reports) stop the deployment, listing up to
   eight ``CVE (package version)``; a scan that cannot be read or does not finish is deployed "unscanned" (the stage
   ``skipped``, said so), never counted as clean. Basic scanning ends ``COMPLETE``; enhanced scanning (Amazon
   Inspector) ends ``ACTIVE`` (scanned, then monitored continuously), which is as complete and gated on its counts
   alike. The counts are on the job (``progress.scan``) and its record;
5. **runtime** — the execution role ``adlc-console-rt-<name>`` (model calls, logs, X-Ray, workload tokens, pulling
   from its own image repository; tagged ``adlc:runtime`` and ``adlc:region``). IAM names are account-global and a
   runtime's are not: a role whose tag or trust names another region is refused, never taken over (a runtime of the
   same name in a second region of the account needs a name of its own). Then CreateAgentRuntime — with the runtime
   options it takes: session storage
   (``filesystemConfigurations``), ``lifecycleConfiguration``, a VPC ``networkConfiguration`` — or UpdateAgentRuntime
   for a new version (whatever the request leaves out — protocol, environment, network, lifecycle, session storage —
   is carried over: an omitted field resets);
6. **ready** — the runtime READY, the endpoint serving the new version (DEFAULT follows it; a named endpoint is
   created or moved to it);
7. **smoke** — one InvokeAgentRuntime of an HTTP agent (its session stopped afterwards).

A first deployment that fails before its runtime exists takes back what it created (its sources, image repository
and role). Everything is tagged ``adlc:console=1``; the console updates and deletes only runtimes so tagged
(``undeploy``: the named endpoints, the runtime — waited until gone — its ECR repository unless kept, its execution
role and its sources). The build project and its role are shared and stay; the runtime's log groups are kept.

A record whose runtime is gone (MISSING: deleted outside the console) is cleaned up by what it names, never by name
alone: a runtime of the same name may have been deployed since and run with the same role, repository and source
prefix. Its role and repository stay while any version of a live runtime of that name (or of a canary's candidate
beside it) uses them — an endpoint or a rollback can return to each — and only the record's own job folders under
``deployments/<name>/`` and its own images that no such version runs are deleted; when a version cannot be read,
nothing is. A runtime, endpoint or candidate a runtime canary holds (``runtime_canary.holder``: production while its
canary is active; the candidate runtime and the canary's control and treatment endpoints until it is cleaned up) is
not deleted, moved or republished here: the canary's gate is about exactly those versions. While the canary's
promotion or rollback runs, production gets no new version here and none of its endpoints moves (that job is
publishing it); the check and the start of a job or a move happen under the runtime's own lock (:func:`moves`), which
the canary's actions on it take too. (An A/B experiment's control endpoint is a Harness endpoint and its treatment a
Harness: the deploy page reaches neither.)

With a permissions boundary on the workspace (a spoke workspace, ``permissionsBoundaryArn``) every role made here
carries it and an existing console role gets it before its policy is put again; such a runtime pulls only from the
console's repositories (``adlc-console/*``), so an image from another repository is deployed with a warning. Every
role made here is on the console's IAM path (``agents.ROLE_PATH``, ``/adlc-console/``) and is given to CodeBuild and
AgentCore by its own ARN; an existing role of its name is used, or deleted, only when the console may adopt it
(``agents.adopt``).

Live 2026-10-01 (us-west-2), the sample agent (``tests/fixtures/byoc_agent``) as a Dockerfile build behind a named
endpoint: validate 2 s, upload 2 s, build 50 s (CodeBuild 29 s of it: provisioning 7, docker login 9, build and
push 9), runtime 13 s (a new role and its pause; CreateAgentRuntime took the role at once), ready 13 s (READY in 6 s,
the endpoint in 5 s), smoke 5 s (a cold start): 97 s in all. Deleting took 8.6 min: the named endpoint 3 min to
go, the runtime 5 min. AgentCore removes a runtime's workload identity with it, not its log groups (one per
endpoint: ``/aws/bedrock-agentcore/runtimes/<id>-DEFAULT``, ``-<endpoint>``).

Live 2026-10-02 (us-west-2), the Claude Agent SDK template's image (``claude_sdk``: python:3.12-slim-bookworm, Node
22, the Claude Code CLI, 252 MB in ECR) with session storage and a lifecycle:

* **The scan gate**: right after the push DescribeImageScanFindings answers ScanNotFoundException for a few seconds,
  then IN_PROGRESS, then COMPLETE — 11 to 17 s after the push. Findings CRITICAL 4, HIGH 13, MEDIUM 10, LOW 4 even
  after ``apt-get upgrade``: perl 5.36.0-7+deb12u3 three times (perl-base is Essential in Debian: it cannot go) and
  python3.11 3.11.2-6+deb12u8, which NodeSource's nodejs package depends on. The default CRITICAL gate stopped it
  (the four CVEs listed, the repository and the source taken back, the shared build project kept); with
  ``scanBlock: []`` the same image deployed, the counts on the job.
* **Timings**: validate 1.9 s, upload 1.6 s, build 113 s (CodeBuild 1.4 min: provisioning 8 s, pre_build 11 s, build
  59 s), scan 17 s, runtime 13.6 s, ready 6.2 s, smoke 29.7 s (the invoke 17.8 s, StopRuntimeSession ~11 s): 3 min.
  A new version: build 86 s, scan 11 s, UpdateAgentRuntime 4 s, ready 6 s, smoke 20 s.
* **Runtime options**: CreateAgentRuntime took ``filesystemConfigurations: [{"sessionStorage": {"mountPath":
  "/mnt/workspace"}}]`` and ``lifecycleConfiguration`` 600 / 7200 s; GetAgentRuntime returns them as sent (plus
  ``metadataConfiguration: {"requireMMDSV2": true}``) and UpdateAgentRuntime took them back unchanged. The storage
  is writable by a non-root container user; it survived StopRuntimeSession (a new microVM saw the files) and was
  empty after the new version, as AgentCore documents.
* **VPC**: no VPC runtime was made live — the first one creates the account's service-linked role
  AWSServiceRoleForBedrockAgentCoreNetwork, and AgentCore keeps a deleted runtime's ENIs for up to 8 hours. The
  validate stage's checks ran live on the default VPC: two subnets (usw2-az1, usw2-az2) and its security group
  passed; usw2-az4, subnets of two VPCs, a security group of another VPC, an unknown subnet
  (InvalidSubnetID.NotFound) and an unknown group (EC2 answers InvalidGroupId.Malformed) were refused before
  anything was built. The botocore 1.43.90 model documents that CreateAgentRuntime refuses
  ``requireServiceS3Endpoint`` (only UpdateAgentRuntime of a runtime made before May 2026 takes it): so does the
  console. Since that rollout a VPC runtime reaches S3, ECR and Bedrock only through its own VPC (NAT or endpoints).
* **Delete**: the runtime took about 4 min to go. Meanwhile the console machine's proxy dropped TLS and a read of
  the wait failed past botocore's ten attempts (SSLError), failing the job with the runtime half gone — hence the
  polls read again on a network failure (``Staged._read``) and a delete takes a runtime already DELETING. Run again,
  the delete found the runtime gone and removed its repository, role and sources in 10 s.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import re
import shutil
import stat
import threading
import time
import uuid
import zipfile
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Callable, Iterator, Mapping, Sequence

from ..direct.aws import client
from .agents import CONSOLE_TAG, ROLE_PATH, adopt, ensure_boundary, refusal, role_args
from .common import error_code as _code, now as _now, pages as _pages, safe
from .kb import console_bucket  # deployments keep their sources under deployments/<runtime name>/<job>/
from .workspaces import boundary_of

SOURCES = ("zip", "dockerfile", "image")
PROTOCOLS = ("HTTP", "MCP", "A2A", "AGUI")
PYTHON_RUNTIMES = ("PYTHON_3_10", "PYTHON_3_11", "PYTHON_3_12", "PYTHON_3_13", "PYTHON_3_14")
DEFAULT_PYTHON = "PYTHON_3_13"
STAGES = ("validate", "upload", "build", "runtime", "ready", "smoke")
#: A container deployment (a Dockerfile build or an ECR image) passes its image's ECR scan before a runtime runs it.
IMAGE_STAGES = ("validate", "upload", "build", "scan", "runtime", "ready", "smoke")
DELETE_STAGES = ("check", "endpoints", "runtime", "repository", "role", "sources")

#: ECR's finding severities, heaviest first; a deployment blocks on the ones it names (``scanBlock``).
SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFORMATIONAL", "UNDEFINED")
DEFAULT_SCAN_BLOCK = ("CRITICAL",)
#: A finished scan: basic scanning's COMPLETE, enhanced scanning's ACTIVE (Inspector scanned it and keeps monitoring).
SCANNED = ("COMPLETE", "ACTIVE")
MAX_LISTED_FINDINGS = 8

#: Session storage (filesystemConfigurations[].sessionStorage): one directory level under /mnt.
MOUNT_PATH = re.compile(r"^/mnt/[A-Za-z0-9._-]+/?$")
DEFAULT_MOUNT = "/mnt/workspace"
#: lifecycleConfiguration on a microVM runtime: 60 s to 8 h (the botocore model's 1209600 is the Instances maximum),
#: and the idle timeout no longer than the maximum lifetime.
LIFECYCLE_MIN, LIFECYCLE_MAX = 60, 28_800
SUBNET = re.compile(r"^subnet-[0-9a-zA-Z]{8,17}$")
SECURITY_GROUP = re.compile(r"^sg-[0-9a-zA-Z]{8,17}$")
#: The Availability Zone IDs AgentCore Runtime's VPC mode supports (its developer guide, 2026-10-02): a subnet in
#: another zone fails CreateAgentRuntime, so it is refused before anything is built.
VPC_ZONES = {
    "us-east-1": ("use1-az1", "use1-az2", "use1-az4"), "us-east-2": ("use2-az1", "use2-az2", "use2-az3"),
    "us-west-1": ("usw1-az1", "usw1-az3"), "us-west-2": ("usw2-az1", "usw2-az2", "usw2-az3"),
    "ap-southeast-5": ("apse5-az1", "apse5-az2", "apse5-az3"), "ap-south-1": ("aps1-az1", "aps1-az2", "aps1-az3"),
    "ap-south-2": ("aps2-az1", "aps2-az2", "aps2-az3"), "ap-northeast-2": ("apne2-az1", "apne2-az2", "apne2-az3"),
    "ap-southeast-1": ("apse1-az1", "apse1-az2", "apse1-az3"), "ap-southeast-2": ("apse2-az1", "apse2-az2", "apse2-az3"),
    "ap-southeast-7": ("apse7-az1", "apse7-az2", "apse7-az3"), "ap-northeast-1": ("apne1-az1", "apne1-az2", "apne1-az4"),
    "ca-central-1": ("cac1-az1", "cac1-az2", "cac1-az4"), "eu-central-1": ("euc1-az1", "euc1-az2", "euc1-az3"),
    "eu-west-1": ("euw1-az1", "euw1-az2", "euw1-az3"), "eu-west-2": ("euw2-az1", "euw2-az2", "euw2-az3"),
    "eu-south-1": ("eus1-az1", "eus1-az2", "eus1-az3"), "eu-west-3": ("euw3-az1", "euw3-az2", "euw3-az3"),
    "eu-south-2": ("eus2-az1", "eus2-az2", "eus2-az3"), "eu-north-1": ("eun1-az1", "eun1-az2", "eun1-az3"),
    "sa-east-1": ("sae1-az1", "sae1-az2", "sae1-az3"), "us-gov-west-1": ("usgw1-az1", "usgw1-az2", "usgw1-az3"),
}


def stages_for(source: str) -> tuple[str, ...]:
    """A deployment's stages: a container (``dockerfile`` / ``image``) has its ``scan`` between build and runtime."""
    return IMAGE_STAGES if source in ("dockerfile", "image") else STAGES
JOB_KINDS = ("deploy", "undeploy")
#: How long an action waits for another one on the same runtime (:func:`moves`) before it is refused.
MOVE_WAIT = 60.0
SMOKE_PROMPT = "你好！这是 ADLC 控制台部署后的冒烟调用，请简短回复。"

NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,47}$")  # agentRuntimeName and endpoint names
RUNTIME_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,99}-[A-Za-z0-9]{10}$")
ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,99}$")
KB_ID = re.compile(r"^[0-9A-Za-z]{10}$")
MAX_KNOWLEDGE_BASES = 10
ENTRY = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]{0,124}\.py$")
VERSION = re.compile(r"^[1-9][0-9]{0,4}$")
IMAGE_URI = re.compile(r"^(?P<account>\d{12})\.dkr\.ecr\.(?P<region>[a-z0-9-]+)\.amazonaws\.com/"
                       r"(?P<repo>(?:[a-z0-9]+(?:[._-][a-z0-9]+)*/)*[a-z0-9]+(?:[._-][a-z0-9]+)*)"
                       r"(?::(?P<tag>[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}))?(?:@(?P<digest>sha256:[a-f0-9]{64}))?$")

#: The console takes JSON bodies of up to 25 MB, an archive comes base64-encoded in one: 18 MB of zip.
MAX_ARCHIVE = 18 * 1024 * 1024
MAX_UNPACKED = 250 * 1024 * 1024
MAX_ENTRIES = 20_000
MAX_SMOKE_BYTES = 256 * 1024

BUILD_IMAGE = "aws/codebuild/amazonlinux-aarch64-standard:3.0"  # Amazon Linux 2023, aarch64 (Launchpad's builder)
BUILD_COMPUTE = "BUILD_GENERAL1_SMALL"

#: docker build for linux/arm64 and push, both in the build phase (a post_build push would run after a failed build).
IMAGE_BUILDSPEC = """version: 0.2
phases:
  pre_build:
    commands:
      - aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "$ECR_REGISTRY"
  build:
    commands:
      - docker build --platform linux/arm64 --tag "$IMAGE_URI" .
      - docker push "$IMAGE_URI"
"""

#: requirements.txt as linux/aarch64 wheels for the runtime's Python, into the code's own folder (wheels only:
#: nothing is built or run), console scripts made portable, then the folder zipped to S3 as the bundle.
BUNDLE_BUILDSPEC = """version: 0.2
env:
  shell: bash
phases:
  build:
    commands:
      - |
        python3 -m pip install --no-cache-dir --no-compile --disable-pip-version-check --target . \\
          --implementation cp --python-version "$PYTHON_VERSION" --only-binary=:all: \\
          --platform manylinux_2_28_aarch64 --platform manylinux2014_aarch64 -r requirements.txt
      - |
        for script in bin/*; do
          if [ -f "$script" ]; then sed -i '1s|^#!.*python.*$|#!/usr/bin/env python3|' "$script"; fi
        done
      - |
        python3 -c '
        import os, zipfile
        with zipfile.ZipFile("/tmp/bundle.zip", "w", zipfile.ZIP_DEFLATED) as bundle:
            for folder, _dirs, files in os.walk("."):
                if "__pycache__" not in folder:
                    for name in files:
                        path = os.path.join(folder, name)
                        bundle.write(path, os.path.relpath(path))
        '
      - aws s3 cp /tmp/bundle.zip "s3://$BUNDLE_BUCKET/$BUNDLE_KEY" --only-show-errors
"""

#: Wording of an IAM change that is not visible yet (a new role is refused by CreateAgentRuntime and CreateProject
#: for a few seconds): such calls are retried.
PROPAGATION = ("role validation failed", "trust policy allows assumption", "not authorized to perform: sts:assumerole", "unable to assume",
               "cannot be assumed", "accessdenied", "access denied", "missing required permissions")


class DeployError(ValueError):
    pass


class Skip(str):
    """A stage's result when it had nothing to do (recorded as ``skipped`` with this reason)."""


def _transient(exc: BaseException) -> bool:
    """A network failure — a dropped TLS session, a timeout, a closed connection — rather than an answer from AWS.
    Live, the console machine's proxy dropped TLS for minutes and a delete's wait died on it: a poll reads again."""
    from botocore.exceptions import ConnectionError as NetworkError, HTTPClientError

    return isinstance(exc, (NetworkError, HTTPClientError))


def _missing(exc: BaseException) -> bool:
    return _code(exc) in ("ResourceNotFoundException", "NoSuchEntity", "RepositoryNotFoundException", "404", "NoSuchBucket", "NotFound")


#: More versions than this are not all read: what a runtime runs is then not known, and nothing it might run is deleted.
MAX_VERSIONS = 200


def runtime_versions(ctl: Any, runtime_id: str) -> tuple[list[dict[str, Any]], bool]:
    """Every version of a runtime as GetAgentRuntime has it (DEFAULT's first), and whether all of them were read: an
    endpoint or a rollback can return to any version, so whatever one runs is in use. Raises when the runtime itself
    cannot be read."""
    current = ctl.get_agent_runtime(agentRuntimeId=runtime_id)
    listed = _pages(ctl.list_agent_runtime_versions, "agentRuntimes", agentRuntimeId=runtime_id)
    found, complete = [current], len(listed) <= MAX_VERSIONS
    for v in listed[:MAX_VERSIONS]:
        version = str(v.get("agentRuntimeVersion") or "")
        if version == str(current.get("agentRuntimeVersion")):
            continue
        try:
            found.append(ctl.get_agent_runtime(agentRuntimeId=runtime_id, agentRuntimeVersion=version))
        except Exception:  # noqa: BLE001 - a version that cannot be read may run anything
            complete = False
    return found, complete


def _size(n: float) -> str:
    return f"{n / 1024:.1f} KB" if n < 1024 * 1024 else f"{n / 1024 / 1024:.1f} MB"


def format_counts(counts: Mapping[str, int]) -> str:
    """Finding counts heaviest first: ``CRITICAL 1, HIGH 4`` (``no findings`` when there are none)."""
    ranked = sorted(((str(k).upper(), int(v)) for k, v in counts.items() if int(v or 0)),
                    key=lambda kv: SEVERITIES.index(kv[0]) if kv[0] in SEVERITIES else len(SEVERITIES))
    return ", ".join(f"{k} {v}" for k, v in ranked) or "no findings"


def slug(name: str) -> str:
    """A runtime name as an ECR repository segment (lowercase, ``-`` between letter and digit runs)."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "agent"


@dataclass(frozen=True)
class Names:
    """What deployments create, by name. ``prefix`` is ``adlc-console``; the live probe uses ``adlc-probe``."""

    prefix: str = "adlc-console"

    @property
    def build_project(self) -> str:
        return f"{self.prefix}-build"

    def build_role(self, region: str) -> str:
        """The build project's role in ``region``: IAM is account-global and the role's policy names that region's bucket
        and log group, so two regions of one account sharing one role would overwrite each other's (a console made
        before this used ``<prefix>-build``)."""
        return f"{self.prefix}-build-{region}"[:64]

    def runtime_role(self, name: str) -> str:
        return f"{self.prefix}-rt-{name}"[:64]

    def repository(self, name: str) -> str:
        return f"{self.prefix}/{slug(name)}"


NAMES = Names()




# -- requests ---------------------------------------------------------------------------------------------------

def _junk(name: str) -> bool:
    return name.startswith("__MACOSX/") or PurePosixPath(name).name == ".DS_Store"


def inspect_archive(data: bytes, *, source: str, entry: str = "main.py") -> dict[str, Any]:
    """Check an uploaded zip and return its facts with ``data``: the archive to upload (rebuilt without its single
    top-level folder, macOS metadata or backslashes when it had any). Refuses unsafe paths, links, zip bombs and an
    archive without its entry point (``zip``) or Dockerfile (``dockerfile``) at the root."""
    if not data:
        raise DeployError("the archive is empty")
    if len(data) > MAX_ARCHIVE:
        raise DeployError(f"the archive is {_size(len(data))}: at most {_size(MAX_ARCHIVE)} (larger dependencies belong in a Dockerfile build)")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise DeployError("the archive is not a zip") from exc
    with archive as zf:
        entries = [i for i in zf.infolist() if not i.is_dir()]
        infos = [i for i in entries if not _junk(i.filename.replace("\\", "/"))]
        if not infos:
            raise DeployError("the zip holds no files")
        if len(infos) > MAX_ENTRIES:
            raise DeployError(f"the zip holds more than {MAX_ENTRIES} files")
        total = 0
        for info in infos:
            name = info.filename.replace("\\", "/")
            if name.startswith("/") or re.match(r"^[A-Za-z]:", name) or ".." in PurePosixPath(name).parts:
                raise DeployError(f"unsafe path in the zip: {info.filename}")
            if stat.S_ISLNK(info.external_attr >> 16):
                raise DeployError(f"the zip holds a symbolic link: {info.filename}")
            total += info.file_size
            if total > MAX_UNPACKED:
                raise DeployError(f"the zip unpacks to more than {_size(MAX_UNPACKED)}")
        names = [str(PurePosixPath(i.filename.replace("\\", "/"))) for i in infos]
        tops = {n.split("/", 1)[0] for n in names}
        root = f"{next(iter(tops))}/" if len(tops) == 1 and all("/" in n for n in names) else ""
        rel = [n[len(root):] for n in names]
        if source == "zip" and entry not in rel:
            at_root = sorted(n for n in rel if "/" not in n)[:12]
            raise DeployError(f"the entry point {entry} is not in the zip (at its root: {', '.join(at_root) or 'nothing'})")
        if source == "dockerfile" and "Dockerfile" not in rel:
            raise DeployError("a Dockerfile build context needs the Dockerfile at the zip's root")
        rebuild = bool(root) or len(infos) != len(entries) or any(n != i.filename for n, i in zip(names, infos))
        if rebuild:
            out = io.BytesIO()
            with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
                for info, name in zip(infos, rel):
                    target = zipfile.ZipInfo(name, date_time=info.date_time)
                    target.external_attr = info.external_attr
                    target.compress_type = zipfile.ZIP_DEFLATED
                    with zf.open(info) as src, dst.open(target, "w") as sink:
                        shutil.copyfileobj(src, sink, 1 << 20)
            data = out.getvalue()
    return {"data": data, "files": len(infos), "unpacked": total, "root": root, "rebuilt": rebuild, "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(), "requirements": "requirements.txt" in rel, "dockerfile": "Dockerfile" in rel,
            "buildspec": "buildspec.yml" in rel}


def check_environment(env: Any) -> dict[str, str]:
    if not isinstance(env, Mapping):
        raise DeployError("environment: an object of NAME: value")
    if len(env) > 50:
        raise DeployError("environment: at most 50 variables")
    out = {}
    for key, value in env.items():
        if not ENV_KEY.match(str(key)):
            raise DeployError(f"environment: {key!r} is not a variable name (letters, digits, underscores)")
        value = "" if value is None else str(value)
        if len(value) > 5000:
            raise DeployError(f"environment: {key} is longer than 5000 characters")
        out[str(key)] = value
    return out


def deploy_request(body: Mapping[str, Any], *, mode: str = "create") -> dict[str, Any]:
    """The deployment the console's form asks for, validated (the archive decoded and inspected). On an update,
    ``protocol``, ``environment`` and ``endpoint`` left out (None) keep the runtime's own."""
    source = str(body.get("source") or "")
    if source not in SOURCES:
        raise DeployError("source: zip, dockerfile or image")
    req: dict[str, Any] = {"mode": mode, "source": source, "name": None}
    if mode == "create":
        name = str(body.get("name") or "").strip()
        if not NAME.match(name):
            raise DeployError("name: a letter, then letters, digits or underscores (at most 48)")
        if name.lower().startswith("harness_"):
            raise DeployError("names starting with harness_ belong to the runtimes of Harnesses")
        req["name"] = name
    if source == "image":
        image = str(body.get("imageUri") or "").strip()
        found = IMAGE_URI.match(image)
        if not found or not (found["tag"] or found["digest"]):
            raise DeployError("imageUri: <account>.dkr.ecr.<region>.amazonaws.com/<repository>:<tag> (or @sha256:<digest>)")
        req["image"] = image
    else:
        try:
            data = base64.b64decode(str(body.get("archive") or ""), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise DeployError("archive: the zip, base64-encoded") from exc
        entry = str(body.get("entryPoint") or "main.py").strip()
        if source == "zip" and (not ENTRY.match(entry) or ".." in entry.split("/")):
            raise DeployError("entryPoint: the .py file to run, relative to the zip's root (main.py)")
        facts = inspect_archive(data, source=source, entry=entry)
        req.update(archive=facts.pop("data"), facts=facts, filename=str(body.get("filename") or "upload.zip")[:200])
        if source == "zip":
            runtime = str(body.get("pythonRuntime") or DEFAULT_PYTHON)
            if runtime not in PYTHON_RUNTIMES:
                raise DeployError(f"pythonRuntime: one of {', '.join(PYTHON_RUNTIMES)}")
            req.update(entry=entry, runtime=runtime, install=facts["requirements"] and body.get("installRequirements", True) is not False,
                       instrument=body.get("instrument") is True)
    protocol = body.get("protocol")
    if protocol is None and mode == "create":
        protocol = "HTTP"
    if protocol is not None:
        protocol = str(protocol).upper()
        if protocol not in PROTOCOLS:
            raise DeployError(f"protocol: one of {', '.join(PROTOCOLS)}")
    env = body.get("environment")
    if env is None and mode == "create":
        env = {}
    endpoint = body.get("endpoint")
    if endpoint is not None:
        endpoint = str(endpoint).strip()
        if endpoint and (not NAME.match(endpoint) or endpoint.upper() == "DEFAULT"):
            raise DeployError("endpoint: a name (a letter, then letters, digits or underscores), not DEFAULT")
    req.update(protocol=protocol, environment=check_environment(env) if env is not None else None, endpoint=endpoint,
               smoke=body.get("smoke", True) is not False, prompt=str(body.get("smokePrompt") or SMOKE_PROMPT).strip()[:2000],
               knowledgeBases=check_knowledge_bases(body.get("knowledgeBases")),
               filesystems=check_session_storage(body.get("sessionStorage")), lifecycle=check_lifecycle(body.get("lifecycle")),
               network=check_network(body.get("network")), scanBlock=check_scan_block(body.get("scanBlock")))
    return req


def check_session_storage(value: Any) -> list[dict[str, Any]] | None:
    """``sessionStorage``: true (at /mnt/workspace), a mount path or ``{"mountPath"}`` → the runtime's
    ``filesystemConfigurations`` (per-session storage that survives a stopped session); false → none. Left out
    (None), a new runtime has none and a new version keeps the runtime's own."""
    if value is None:
        return None
    if value is False:
        return []
    if value is True:
        path = DEFAULT_MOUNT
    elif isinstance(value, str):
        path = value.strip()
    elif isinstance(value, Mapping):
        if value.get("enabled") is False:
            return []
        path = str(value.get("mountPath") or DEFAULT_MOUNT).strip()
    else:
        raise DeployError("sessionStorage: true, false or {\"mountPath\": \"/mnt/<name>\"}")
    if not (6 <= len(path) <= 200) or not MOUNT_PATH.match(path):
        raise DeployError(f"sessionStorage.mountPath: one directory under /mnt, such as {DEFAULT_MOUNT} (not {path!r})")
    return [{"sessionStorage": {"mountPath": path}}]


def check_lifecycle(value: Any) -> dict[str, int] | None:
    """``lifecycle``: ``{"idleRuntimeSessionTimeout", "maxLifetime"}`` in seconds (60 to 28800 on a microVM runtime,
    idle at most the lifetime; either may be left out: AgentCore's defaults are 900 and 28800)."""
    if value is None or value == {}:
        return None
    if not isinstance(value, Mapping):
        raise DeployError("lifecycle: {\"idleRuntimeSessionTimeout\": seconds, \"maxLifetime\": seconds}")
    unknown = set(value) - {"idleRuntimeSessionTimeout", "maxLifetime"}
    if unknown:
        raise DeployError(f"lifecycle: only idleRuntimeSessionTimeout and maxLifetime (not {', '.join(sorted(unknown))})")
    out: dict[str, int] = {}
    for key in ("idleRuntimeSessionTimeout", "maxLifetime"):
        raw = value.get(key)
        if raw is None or raw == "":
            continue
        try:
            seconds = int(raw)
        except (TypeError, ValueError) as exc:
            raise DeployError(f"lifecycle.{key}: whole seconds") from exc
        if isinstance(raw, bool) or seconds != float(raw) or not LIFECYCLE_MIN <= seconds <= LIFECYCLE_MAX:
            raise DeployError(f"lifecycle.{key}: {LIFECYCLE_MIN} to {LIFECYCLE_MAX} seconds (8 hours) on a microVM runtime")
        out[key] = seconds
    if len(out) == 2 and out["idleRuntimeSessionTimeout"] > out["maxLifetime"]:
        raise DeployError("lifecycle: idleRuntimeSessionTimeout must not exceed maxLifetime")
    return out or None


def check_network(value: Any) -> dict[str, Any] | None:
    """``network``: ``"PUBLIC"`` or ``{"mode": "VPC", "subnets": [...], "securityGroups": [...]}`` (the API's own
    ``{"networkMode", "networkModeConfig"}`` too) → ``networkConfiguration``. The ids are checked by shape here and
    against EC2 in the validate stage. ``requireServiceS3Endpoint`` is refused: CreateAgentRuntime rejects it."""
    if value is None:
        return None
    if isinstance(value, str):
        value = {"mode": value}
    if not isinstance(value, Mapping):
        raise DeployError("network: \"PUBLIC\" or {\"mode\": \"VPC\", \"subnets\": [...], \"securityGroups\": [...]}")
    config = value.get("networkModeConfig") if isinstance(value.get("networkModeConfig"), Mapping) else value
    mode = str(value.get("mode") or value.get("networkMode") or "").upper()
    if mode == "PUBLIC":
        return {"networkMode": "PUBLIC"}
    if mode != "VPC":
        raise DeployError("network.mode: PUBLIC or VPC")
    if "requireServiceS3Endpoint" in config:
        raise DeployError("network.requireServiceS3Endpoint: CreateAgentRuntime refuses it (only runtimes made before May 2026 take it, on update)")

    def ids(key: str, pattern: re.Pattern[str], what: str) -> list[str]:
        raw = config.get(key)
        if isinstance(raw, str):
            raw = [p for p in re.split(r"[\s,]+", raw) if p]
        if not isinstance(raw, (list, tuple)) or not 1 <= len(raw) <= 16:
            raise DeployError(f"network.{key}: 1 to 16 {what} ids")
        out = []
        for item in raw:
            item = str(item).strip()
            if not pattern.match(item):
                raise DeployError(f"network.{key}: {item!r} is not a {what} id ({pattern.pattern[1:-1]})")
            if item not in out:
                out.append(item)
        return out

    return {"networkMode": "VPC", "networkModeConfig": {"subnets": ids("subnets", SUBNET, "subnet"),
                                                        "securityGroups": ids("securityGroups", SECURITY_GROUP, "security group")}}


def check_scan_block(value: Any) -> list[str]:
    """``scanBlock``: the ECR finding severities that stop a container deployment (CRITICAL when left out; [] only
    reports)."""
    if value is None:
        return list(DEFAULT_SCAN_BLOCK)
    if isinstance(value, str):
        value = [p for p in re.split(r"[\s,]+", value) if p]
    if not isinstance(value, (list, tuple)):
        raise DeployError(f"scanBlock: a list of severities ({', '.join(SEVERITIES)}), [] to never block")
    wanted = {str(v).strip().upper() for v in value}
    unknown = wanted - set(SEVERITIES)
    if unknown:
        raise DeployError(f"scanBlock: {', '.join(sorted(unknown))} is not a severity ({', '.join(SEVERITIES)})")
    return [s for s in SEVERITIES if s in wanted]


def check_knowledge_bases(kbs: Any) -> list[str]:
    """The knowledge bases the agent's code retrieves from (Studio's 知识库检索): its role may ``bedrock:Retrieve``
    on these only. Left out, the role has no knowledge-base grant (a new version without them drops it)."""
    if kbs is None:
        return []
    if not isinstance(kbs, (list, tuple)) or len(kbs) > MAX_KNOWLEDGE_BASES or not all(isinstance(k, str) and KB_ID.match(k) for k in kbs):
        raise DeployError(f"knowledgeBases: up to {MAX_KNOWLEDGE_BASES} knowledge base ids (10 letters and digits each)")
    return sorted(set(kbs))


def public_params(req: Mapping[str, Any]) -> dict[str, Any]:
    """What a job keeps of its request (never the archive or the variables' values)."""
    out = {"mode": req["mode"], "name": req.get("name"), "source": req["source"], "protocol": req.get("protocol"), "endpoint": req.get("endpoint"),
           "environment": sorted(req["environment"]) if req.get("environment") is not None else None, "smoke": req.get("smoke")}
    if req["source"] == "image":
        out["imageUri"] = req["image"]
    else:
        facts = req["facts"]
        out.update(filename=req["filename"], size=facts["size"], files=facts["files"], sha256=facts["sha256"])
        if req["source"] == "zip":
            out.update(entryPoint=req["entry"], pythonRuntime=req["runtime"], installRequirements=req["install"])
    if req.get("knowledgeBases"):
        out["knowledgeBases"] = list(req["knowledgeBases"])
    if req.get("filesystems") is not None:
        out["sessionStorage"] = next((f["sessionStorage"]["mountPath"] for f in req["filesystems"] if "sessionStorage" in f), False)
    if req.get("lifecycle"):
        out["lifecycle"] = dict(req["lifecycle"])
    if req.get("network"):
        out["network"] = {"mode": req["network"]["networkMode"], **(req["network"].get("networkModeConfig") or {})}
    if req["source"] in ("dockerfile", "image"):
        out["scanBlock"] = list(req.get("scanBlock") or [])
    return out


# -- policies -----------------------------------------------------------------------------------------------------

def runtime_trust(account: str, region: str) -> dict[str, Any]:
    return {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "bedrock-agentcore.amazonaws.com"}, "Action": "sts:AssumeRole",
                                                    "Condition": {"StringEquals": {"aws:SourceAccount": account},
                                                                  "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock-agentcore:{region}:{account}:*"}}}]}


def runtime_role_policy(*, account: str, region: str, repository_arn: str | None = None, knowledge_bases: Sequence[str] = ()) -> dict[str, Any]:
    """The runtime's execution role: model calls, its log groups, X-Ray and metrics, workload tokens, pulling from
    its own image repository (a container runtime) and retrieving from the knowledge bases its code reads (Studio)."""
    groups = f"arn:aws:logs:{region}:{account}:log-group:/aws/bedrock-agentcore/runtimes/*"
    directory = f"arn:aws:bedrock-agentcore:{region}:{account}:workload-identity-directory/default"
    statements: list[dict[str, Any]] = [
        {"Sid": "Models", "Effect": "Allow", "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
         "Resource": ["arn:aws:bedrock:*::foundation-model/*", "arn:aws:bedrock:*:*:inference-profile/*", f"arn:aws:bedrock:{region}:{account}:*"]},
        {"Sid": "LogGroups", "Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:DescribeLogStreams"], "Resource": groups},
        {"Sid": "LogGroupList", "Effect": "Allow", "Action": "logs:DescribeLogGroups", "Resource": f"arn:aws:logs:{region}:{account}:log-group:*"},
        {"Sid": "LogEvents", "Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"], "Resource": f"{groups}:log-stream:*"},
        {"Sid": "Traces", "Effect": "Allow", "Action": ["xray:PutTraceSegments", "xray:PutTelemetryRecords", "xray:GetSamplingRules", "xray:GetSamplingTargets"],
         "Resource": "*"},
        {"Sid": "Metrics", "Effect": "Allow", "Action": "cloudwatch:PutMetricData", "Resource": "*",
         "Condition": {"StringEquals": {"cloudwatch:namespace": "bedrock-agentcore"}}},
        {"Sid": "WorkloadIdentity", "Effect": "Allow", "Action": ["bedrock-agentcore:GetWorkloadAccessToken", "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
                                                                  "bedrock-agentcore:GetWorkloadAccessTokenForUserId"],
         "Resource": [directory, f"{directory}/workload-identity/*"]},
    ]
    if repository_arn:
        statements += [{"Sid": "ImagePull", "Effect": "Allow", "Action": ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"],
                        "Resource": repository_arn},
                       {"Sid": "ImageToken", "Effect": "Allow", "Action": "ecr:GetAuthorizationToken", "Resource": "*"}]
    if knowledge_bases:
        statements.append({"Sid": "KnowledgeBases", "Effect": "Allow", "Action": "bedrock:Retrieve",
                           "Resource": [f"arn:aws:bedrock:{region}:{account}:knowledge-base/{kb}" for kb in knowledge_bases]})
    return {"Version": "2012-10-17", "Statement": statements}


def build_trust(account: str) -> dict[str, Any]:
    return {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "codebuild.amazonaws.com"}, "Action": "sts:AssumeRole",
                                                    "Condition": {"StringEquals": {"aws:SourceAccount": account}}}]}


def build_role_policy(*, account: str, region: str, bucket: str, names: Names = NAMES) -> dict[str, Any]:
    """The build project's role: its log group, the sources (read) and bundles (write) under ``deployments/``, and
    pushing to the console's image repositories."""
    group = f"arn:aws:logs:{region}:{account}:log-group:/aws/codebuild/{names.build_project}"
    return {"Version": "2012-10-17", "Statement": [
        {"Sid": "Logs", "Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"], "Resource": [group, f"{group}:*"]},
        {"Sid": "Sources", "Effect": "Allow", "Action": ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject"], "Resource": f"arn:aws:s3:::{bucket}/deployments/*"},
        {"Sid": "Bucket", "Effect": "Allow", "Action": ["s3:ListBucket", "s3:GetBucketLocation", "s3:GetBucketAcl"], "Resource": f"arn:aws:s3:::{bucket}"},
        {"Sid": "ImageToken", "Effect": "Allow", "Action": "ecr:GetAuthorizationToken", "Resource": "*"},
        {"Sid": "ImagePush", "Effect": "Allow", "Action": ["ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer",
                                                           "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload", "ecr:PutImage"],
         "Resource": f"arn:aws:ecr:{region}:{account}:repository/{names.prefix}/*"},
    ]}


def _document(value: Any) -> dict[str, Any]:
    """A policy document as IAM returns it (boto3 decodes it; a URL-encoded string otherwise)."""
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str) and value.strip():
        from urllib.parse import unquote

        try:
            found = json.loads(unquote(value))
        except ValueError:
            return {}
        return found if isinstance(found, dict) else {}
    return {}


def trust_regions(trust: Any) -> set[str]:
    """The regions whose AgentCore a role's trust lets assume it (its ``aws:SourceArn`` conditions)."""
    statements = _document(trust).get("Statement") or []
    regions: set[str] = set()
    for statement in [statements] if isinstance(statements, Mapping) else statements:
        for test in ((statement or {}).get("Condition") or {}).values():
            for key, value in (test or {}).items() if isinstance(test, Mapping) else ():
                if str(key).lower() != "aws:sourcearn":
                    continue
                for arn in value if isinstance(value, list) else [value]:
                    parts = str(arn).split(":")
                    if len(parts) > 3 and parts[2] == "bedrock-agentcore" and parts[3] not in ("", "*"):
                        regions.add(parts[3])
    return regions


def role_regions(tags: Mapping[str, str], trust: Any) -> set[str]:
    """The region(s) a console execution role serves: its ``adlc:region`` tag, else the regions its trust names."""
    return {str(tags["adlc:region"])} if tags.get("adlc:region") else trust_regions(trust)


# -- the smoke call's answer ------------------------------------------------------------------------------------

TEXT_KEYS = ("result", "response", "answer", "output", "output_text", "text", "message", "content", "completion", "reply", "delta")


def _text_of(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        for key in TEXT_KEYS:
            found = value.get(key)
            if isinstance(found, Mapping):
                found = found.get("text")
            if isinstance(found, str) and found.strip():
                return found
    return json.dumps(value, ensure_ascii=False, default=str)[:2000]


def answer_text(raw: bytes, content_type: str = "") -> str:
    """The text of a runtime's answer: a JSON body's conventional key (``result``…), the joined ``data:`` lines of
    an event stream, or the body itself."""
    text = raw.decode("utf-8", "replace")
    if "text/event-stream" in content_type or text.lstrip().startswith("data:"):
        parts = []
        for line in text.splitlines():
            if line.startswith("data:"):
                chunk = line[5:].strip()
                try:
                    parts.append(_text_of(json.loads(chunk)))
                except ValueError:
                    parts.append(chunk)
        return "".join(parts).strip()
    try:
        return _text_of(json.loads(text)).strip()
    except ValueError:
        return text.strip()


# -- the staged job -----------------------------------------------------------------------------------------------

class Staged:
    """Stages recorded on a console job: ``progress.stages`` (and ``progress.stage``, the current one)."""

    #: A poll's read that fails on the network is tried again this many times, this far apart (on top of botocore's
    #: own retries), before the stage fails.
    READ_ATTEMPTS, READ_PAUSE = 6, 10.0

    def __init__(self, job: Any, names: tuple[str, ...], clock: Callable[[], float]):
        self.job, self.clock = job, clock
        self.stages = [{"name": n, "status": "pending"} for n in names]

    def _read(self, call: Callable[..., Any], **kwargs: Any) -> Any:
        """One read of a poll, tried again when it fails on the network (never when AWS answered an error)."""
        for attempt in range(self.READ_ATTEMPTS):
            try:
                return call(**kwargs)
            except Exception as exc:  # noqa: BLE001
                if not _transient(exc) or attempt == self.READ_ATTEMPTS - 1:
                    raise
                self.job.log(f"  read failed on the network ({type(exc).__name__}), again in {int(self.READ_PAUSE)} s")
                getattr(self, "sleep", time.sleep)(self.READ_PAUSE)
        raise AssertionError("unreachable")

    def stage(self, name: str, fn: Callable[[], Any]) -> Any:
        entry = next(s for s in self.stages if s["name"] == name)
        entry.update(status="running", startedAt=_now())
        self.job.progress(stage=name, stages=self.stages)
        self.job.log(f"[{name}] started")
        started = self.clock()
        try:
            result = fn()
        except Exception as exc:
            entry.update(status="failed", detail=f"{type(exc).__name__}: {str(exc)[:500]}", seconds=round(self.clock() - started, 1), endedAt=_now())
            self.job.progress(stages=self.stages)
            self.job.log(f"[{name}] failed: {str(exc)[:500]}")
            raise
        entry.update(status="skipped" if isinstance(result, Skip) else "succeeded", detail=str(result or ""), seconds=round(self.clock() - started, 1),
                     endedAt=_now())
        self.job.progress(stages=self.stages)
        self.job.log(f"[{name}] {entry['status']} in {entry['seconds']} s: {entry['detail']}")
        return result


class Pipeline(Staged):
    """One deployment: create a runtime (``mode`` create) or publish a new version of one (``current``: its
    GetAgentRuntime). ``on_runtime`` gets the runtime as soon as it exists (the console keeps it)."""

    BUILD_POLL, BUILD_TIMEOUT = 5.0, 45 * 60.0
    RUNTIME_POLL, RUNTIME_TIMEOUT = 5.0, 20 * 60.0
    ENDPOINT_POLL, ENDPOINT_TIMEOUT = 5.0, 10 * 60.0
    IAM_PAUSE, IAM_ATTEMPTS = 10.0, 8
    #: Launchpad's gate: DescribeImageScanFindings every 5 s for up to 300 s; a push scan not registered yet
    #: (ScanNotFoundException) is waited for this long in a repository that scans on push.
    SCAN_POLL, SCAN_TIMEOUT, SCAN_REGISTER = 5.0, 300.0, 60.0

    def __init__(self, session: Any, *, account: str, region: str, request: Mapping[str, Any], job: Any, names: Names = NAMES,
                 current: Mapping[str, Any] | None = None, record: Mapping[str, Any] | None = None,
                 on_runtime: Callable[[dict[str, Any]], None] | None = None, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic, boundary: str | None = None):
        super().__init__(job, stages_for(str(request["source"])), clock)
        self.session = session
        self.boundary = boundary  # the workspace's permissions boundary: every role made here carries it
        self.scan_on_push: bool | None = None
        self.account, self.region, self.req, self.names, self.sleep = account, region, dict(request), names, sleep
        self.current, self.record, self.on_runtime = current, record or {}, on_runtime
        self.mode = "update" if current else "create"
        self.name = str(current["agentRuntimeName"]) if current else str(self.req["name"])
        self.job_id = str(job.job["id"])
        self.bucket = console_bucket(account, region)
        self.prefix = f"deployments/{self.name}/{self.job_id}"
        self.runtime_id = str(current["agentRuntimeId"]) if current else None
        self.runtime_arn = str(current["agentRuntimeArn"]) if current else None
        self.version: str | None = None
        self.image_uri: str | None = None
        self.repository_arn: str | None = None
        self.code_key: str | None = None
        self.source_key: str | None = None
        self.endpoint = self.req.get("endpoint") if self.req.get("endpoint") is not None else (self.record.get("endpoint") or "")
        self.created: dict[str, Any] = {}
        self.out: dict[str, Any] = {"name": self.name, "mode": self.mode, "source": self.req["source"]}
        self.ctl = client(session, "bedrock-agentcore-control", region)
        self.data = client(session, "bedrock-agentcore", region)
        self.s3 = client(session, "s3", region)
        self.iam = client(session, "iam")
        self.ecr = client(session, "ecr", region)
        self.cb = client(session, "codebuild", region)
        self.logs = client(session, "logs", region)

    def log(self, line: str) -> None:
        self.job.log(line)

    def run(self) -> dict[str, Any]:
        try:
            for name in [s["name"] for s in self.stages]:
                self.stage(name, getattr(self, f"_{name}"))
        except Exception:
            if self.mode == "create" and not self.runtime_id:
                self._take_back()
            raise
        return {**self.out, "stages": self.stages}

    # -- validate ----------------------------------------------------------------------------------------------------
    def _validate(self) -> str:
        if self.mode == "create":
            taken = next((r for r in _pages(self.ctl.list_agent_runtimes, "agentRuntimes") if r.get("agentRuntimeName") == self.name), None)
            if taken:
                raise DeployError(f"a runtime named {self.name} exists ({taken.get('agentRuntimeId')}): publish a new version of it instead")
        else:
            status = str(self.ctl.get_agent_runtime(agentRuntimeId=self.runtime_id).get("status"))
            if status in ("CREATING", "UPDATING", "DELETING"):
                raise DeployError(f"{self.name} is {status}: wait until it settles")
        detail = self._resolve_image() if self.req["source"] == "image" else self._archive_facts()
        vpc = self._check_vpc() if (self.req.get("network") or {}).get("networkMode") == "VPC" else ""
        return " · ".join(p for p in (detail, vpc) if p)

    def _check_vpc(self) -> str:
        """The VPC mode's subnets and security groups exist (read-only EC2 calls), in one VPC and in Availability Zones
        AgentCore supports there."""
        config = self.req["network"]["networkModeConfig"]
        ec2 = client(self.session, "ec2", self.region)

        def described(call: Callable[..., Any], key: str, what: str, **kwargs: Any) -> list[dict[str, Any]]:
            try:
                return list(call(**kwargs).get(key) or [])
            except Exception as exc:  # noqa: BLE001
                code = _code(exc)
                if code.endswith(".NotFound") or code.endswith(".Malformed"):
                    raise DeployError(f"network: {what} does not exist in {self.account} / {self.region} ({str(exc)[:240]})") from exc
                if code in ("UnauthorizedOperation", "AccessDenied", "AccessDeniedException"):
                    raise DeployError(f"network: this workspace may not read {what} ({code}): it needs ec2:DescribeSubnets and "
                                      "ec2:DescribeSecurityGroups to check a VPC configuration") from exc
                raise

        subnets = described(ec2.describe_subnets, "Subnets", "a subnet", SubnetIds=list(config["subnets"]))
        groups = described(ec2.describe_security_groups, "SecurityGroups", "a security group", GroupIds=list(config["securityGroups"]))
        subnets.sort(key=lambda s: config["subnets"].index(s["SubnetId"]) if s.get("SubnetId") in config["subnets"] else len(config["subnets"]))
        missing = sorted(set(config["subnets"]) - {s.get("SubnetId") for s in subnets}) + sorted(
            set(config["securityGroups"]) - {g.get("GroupId") for g in groups})
        if missing:
            raise DeployError(f"network: {', '.join(missing)} not found in {self.account} / {self.region}")
        vpcs = sorted({str(s.get("VpcId")) for s in subnets})
        if len(vpcs) != 1:
            raise DeployError(f"network: the subnets are in several VPCs ({', '.join(vpcs)}): one VPC per runtime")
        strangers = [g["GroupId"] for g in groups if g.get("VpcId") != vpcs[0]]
        if strangers:
            raise DeployError(f"network: the security groups {', '.join(strangers)} are not in the subnets' VPC {vpcs[0]}")
        supported = VPC_ZONES.get(self.region)
        if supported:
            outside = [f"{s['SubnetId']} ({s.get('AvailabilityZoneId')})" for s in subnets if s.get("AvailabilityZoneId") not in supported]
            if outside:
                raise DeployError(f"network: AgentCore Runtime's VPC mode supports {', '.join(supported)} in {self.region}, not "
                                  f"{', '.join(outside)}")
        zones = ", ".join(f"{s['SubnetId']}@{s.get('AvailabilityZoneId')}" for s in subnets)
        self.out["network"] = {"mode": "VPC", "vpc": vpcs[0], "subnets": list(config["subnets"]), "securityGroups": list(config["securityGroups"])}
        return f"VPC {vpcs[0]}: subnets {zones} · security groups {', '.join(config['securityGroups'])}"

    def _archive_facts(self) -> str:
        facts = self.req["facts"]
        notes = [f"{self.req['source']} {self.req['filename']}", f"{facts['files']} files", _size(facts["unpacked"]), f"sha256 {facts['sha256'][:12]}"]
        if facts["root"]:
            notes.append(f"folder {facts['root']} lifted to the root")
        if self.req["source"] == "zip":
            notes.append(f"entry {self.req['entry']} on {self.req['runtime']}")
            notes.append("requirements.txt → CodeBuild" if self.req["install"] else ("requirements.txt left as is" if facts["requirements"] else "no requirements.txt"))
        elif facts["buildspec"]:
            notes.append("its buildspec.yml is ignored (the console's own builds it)")
        return " · ".join(notes)

    def _resolve_image(self) -> str:
        found = IMAGE_URI.match(self.req["image"])
        assert found is not None  # checked by deploy_request
        if found["account"] != self.account or found["region"] != self.region:
            raise DeployError(f"the image is in {found['account']} / {found['region']}: this workspace deploys images of {self.account} / {self.region} only")
        repo = found["repo"]
        image_id = {"imageDigest": found["digest"]} if found["digest"] else {"imageTag": found["tag"]}
        try:
            details = self.ecr.describe_images(repositoryName=repo, imageIds=[image_id]).get("imageDetails") or []
        except Exception as exc:  # noqa: BLE001
            if _code(exc) in ("ImageNotFoundException", "RepositoryNotFoundException"):
                raise DeployError(f"no image {self.req['image']} in ECR") from exc
            raise
        if not details or not details[0].get("imageDigest"):
            raise DeployError(f"no image {self.req['image']} in ECR")
        digest = details[0]["imageDigest"]
        self.image_uri = f"{self.account}.dkr.ecr.{self.region}.amazonaws.com/{repo}@{digest}"
        self.repository_arn = f"arn:aws:ecr:{self.region}:{self.account}:repository/{repo}"
        self.out.update(image=self.image_uri)
        detail = f"image {repo}{':' + found['tag'] if found['tag'] else ''} → {digest[:19]}… ({_size(details[0].get('imageSizeInBytes') or 0)})"
        if self.boundary and not repo.startswith(f"{self.names.prefix}/"):
            warning = (f"the workspace's roles carry the permissions boundary {self.boundary}, which lets a runtime pull only from the console's "
                       f"repositories ({self.names.prefix}/*): {repo} is pulled only if the account's owner added it to the boundary")
            self.log(f"  warning: {warning}")
            self.out["warning"] = warning
            detail += " · warning: outside the console's repositories (the permissions boundary)"
        return detail

    # -- upload ------------------------------------------------------------------------------------------------------
    def _upload(self) -> str:
        if self.req["source"] == "image":
            return Skip("an existing image: nothing to upload")
        self._ensure_bucket()
        leaf = "context.zip" if self.req["source"] == "dockerfile" else ("source.zip" if self.req["install"] else "code.zip")
        key = f"{self.prefix}/{leaf}"
        put = self.s3.put_object(Bucket=self.bucket, Key=key, Body=self.req["archive"], ContentType="application/zip", ExpectedBucketOwner=self.account)
        self.created["sources"] = True
        self.source_key = key
        if self.req["source"] == "zip" and not self.req["install"]:
            self.code_key = key
        return f"s3://{self.bucket}/{key} ({_size(len(self.req['archive']))}{', version ' + put['VersionId'] if put.get('VersionId') else ''})"

    def _ensure_bucket(self) -> None:
        try:
            self.s3.head_bucket(Bucket=self.bucket, ExpectedBucketOwner=self.account)
            return
        except Exception as exc:  # noqa: BLE001
            if not _missing(exc):
                raise DeployError(f"the console bucket {self.bucket} is not usable from this account ({_code(exc) or exc})") from exc
        kwargs: dict[str, Any] = {"Bucket": self.bucket}
        if self.region != "us-east-1":
            kwargs["CreateBucketConfiguration"] = {"LocationConstraint": self.region}
        self.s3.create_bucket(**kwargs)
        owner = {"Bucket": self.bucket, "ExpectedBucketOwner": self.account}
        self.s3.put_public_access_block(**owner, PublicAccessBlockConfiguration={"BlockPublicAcls": True, "IgnorePublicAcls": True,
                                                                                 "BlockPublicPolicy": True, "RestrictPublicBuckets": True})
        self.s3.put_bucket_encryption(**owner, ServerSideEncryptionConfiguration={"Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]})
        self.s3.put_bucket_versioning(**owner, VersioningConfiguration={"Status": "Enabled"})
        self.s3.put_bucket_tagging(**owner, Tagging={"TagSet": [{"Key": k, "Value": v} for k, v in CONSOLE_TAG.items()]})
        self.log(f"created the console bucket {self.bucket} (versioned, private, tagged)")

    # -- build -------------------------------------------------------------------------------------------------------
    def _build(self) -> str:
        source = self.req["source"]
        if source == "image":
            return Skip("an existing image: no build")
        if source == "zip" and not self.req["install"]:
            return Skip("direct code deploy of the zip as uploaded" + (" (installRequirements off)" if self.req["facts"]["requirements"] else ""))
        self._ensure_build_project()
        started = self.clock()
        if source == "dockerfile":
            repo_arn, repo_uri = self._ensure_repository()
            tag = self.job_id
            build = self._run_build("image", IMAGE_BUILDSPEC, {"AWS_REGION": self.region, "ECR_REGISTRY": repo_uri.split("/", 1)[0], "IMAGE_URI": f"{repo_uri}:{tag}"})
            repo = repo_uri.split("/", 1)[1]
            details = self.ecr.describe_images(repositoryName=repo, imageIds=[{"imageTag": tag}]).get("imageDetails") or []
            if not details or not details[0].get("imageDigest"):
                raise DeployError(f"the build pushed no {repo}:{tag}")
            digest = details[0]["imageDigest"]
            self.image_uri, self.repository_arn = f"{repo_uri.split('/', 1)[0]}/{repo}@{digest}", repo_arn
            self.out.update(image=self.image_uri, repository=repo, build=build["id"])
            return f"CodeBuild {build['id'].split(':')[-1][:8]} · {(self.clock() - started) / 60:.1f} min → {repo}:{tag} @ {digest[:19]}…"
        key = f"{self.prefix}/bundle.zip"
        python = self.req["runtime"].replace("PYTHON_", "").replace("_", ".")
        build = self._run_build("bundle", BUNDLE_BUILDSPEC, {"PYTHON_VERSION": python, "BUNDLE_BUCKET": self.bucket, "BUNDLE_KEY": key})
        head = self.s3.head_object(Bucket=self.bucket, Key=key, ExpectedBucketOwner=self.account)
        self.code_key = key
        self.out.update(build=build["id"])
        return f"CodeBuild {build['id'].split(':')[-1][:8]} · {(self.clock() - started) / 60:.1f} min → s3://{self.bucket}/{key} ({_size(head.get('ContentLength') or 0)})"

    def _ensure_build_project(self) -> None:
        n = self.names
        role = self._ensure_role(n.build_role(self.region), build_trust(self.account), "console-build",
                                 build_role_policy(account=self.account, region=self.region, bucket=self.bucket, names=n),
                                 f"ADLC console: builds agent images and code bundles in {self.region}")
        found = self.cb.batch_get_projects(names=[n.build_project]).get("projects") or []
        if found:
            tags = {t.get("key"): t.get("value") for t in found[0].get("tags") or []}
            if tags.get("adlc:console") != "1":
                raise DeployError(f"a CodeBuild project {n.build_project} exists that the console did not create")
            if found[0].get("serviceRole") != role:
                self.cb.update_project(name=n.build_project, serviceRole=role)
            return
        self._retry_iam(lambda: self.cb.create_project(
            name=n.build_project, description="ADLC console: agent images (linux/arm64) and code bundles",
            source={"type": "S3", "location": f"{self.bucket}/deployments/placeholder.zip", "buildspec": IMAGE_BUILDSPEC},
            artifacts={"type": "NO_ARTIFACTS"},
            environment={"type": "ARM_CONTAINER", "image": BUILD_IMAGE, "computeType": BUILD_COMPUTE, "privilegedMode": True},
            serviceRole=role, timeoutInMinutes=30, queuedTimeoutInMinutes=30,
            logsConfig={"cloudWatchLogs": {"status": "ENABLED"}}, tags=[{"key": k, "value": v} for k, v in CONSOLE_TAG.items()]))
        self.log(f"created the CodeBuild project {n.build_project} ({BUILD_IMAGE}, {BUILD_COMPUTE})")

    def _ensure_repository(self) -> tuple[str, str]:
        repo = self.names.repository(self.name)
        try:
            found = self.ecr.describe_repositories(repositoryNames=[repo])["repositories"][0]
        except Exception as exc:  # noqa: BLE001
            if not _missing(exc):
                raise
            tags = [{"Key": k, "Value": v} for k, v in {**CONSOLE_TAG, "adlc:runtime": self.name}.items()]
            found = self.ecr.create_repository(repositoryName=repo, tags=tags, imageTagMutability="MUTABLE",
                                               imageScanningConfiguration={"scanOnPush": True})["repository"]
            self.created["repository"] = repo
            self.scan_on_push = True
            self.log(f"created the ECR repository {repo} (scan on push)")
            return str(found["repositoryArn"]), str(found["repositoryUri"])
        tags = {t["Key"]: t["Value"] for t in self.ecr.list_tags_for_resource(resourceArn=found["repositoryArn"]).get("tags") or []}
        if tags.get("adlc:console") != "1" or tags.get("adlc:runtime", self.name) != self.name:
            raise DeployError(f"the ECR repository {repo} exists and is not this runtime's (tags {tags})")
        self.scan_on_push = bool((found.get("imageScanningConfiguration") or {}).get("scanOnPush"))
        if not self.scan_on_push:  # a repository made before the scan gate: it scans on push from now on
            try:
                self.ecr.put_image_scanning_configuration(repositoryName=repo, imageScanningConfiguration={"scanOnPush": True})
                self.scan_on_push = True
                self.log(f"turned on scan on push for {repo}")
            except Exception as exc:  # noqa: BLE001 - the scan stage then reports the image unscanned
                self.log(f"could not turn on scan on push for {repo}: {_code(exc) or type(exc).__name__}")
        return str(found["repositoryArn"]), str(found["repositoryUri"])

    def _run_build(self, kind: str, buildspec: str, env: Mapping[str, str]) -> dict[str, Any]:
        build = self.cb.start_build(projectName=self.names.build_project, sourceTypeOverride="S3", sourceLocationOverride=f"{self.bucket}/{self.source_key}",
                                    buildspecOverride=buildspec, idempotencyToken=f"{self.job_id}-{kind}",
                                    environmentVariablesOverride=[{"name": k, "value": v, "type": "PLAINTEXT"} for k, v in env.items()])["build"]
        self.log(f"CodeBuild {build['id']} started ({kind})")
        self.out["build"] = build["id"]
        deadline, last = self.clock() + self.BUILD_TIMEOUT, None
        while True:
            build = self._read(self.cb.batch_get_builds, ids=[build["id"]])["builds"][0]
            phase = build.get("currentPhase")
            if phase != last:
                self.log(f"  build phase {phase}")
                last = phase
            status = build.get("buildStatus")
            if status == "SUCCEEDED":
                self.log("  phases: " + ", ".join(f"{p.get('phaseType')} {p.get('durationInSeconds', 0)}s" for p in build.get("phases") or []
                                                  if p.get("phaseType") != "COMPLETED"))
                return build
            if status in ("FAILED", "FAULT", "TIMED_OUT", "STOPPED"):
                failed = [f"{p.get('phaseType')}: {c.get('message')}" for p in build.get("phases") or [] if p.get("phaseStatus") not in (None, "SUCCEEDED")
                          for c in p.get("contexts") or [] if c.get("message")]
                for line in self._build_log_tail(build):
                    self.log(f"  | {line}")
                raise DeployError(f"CodeBuild {status}: {'; '.join(failed) or 'see the build log'}")
            if self.clock() > deadline:
                raise DeployError(f"CodeBuild still {phase} after {int(self.BUILD_TIMEOUT / 60)} min ({build['id']})")
            self.sleep(self.BUILD_POLL)

    def _build_log_tail(self, build: Mapping[str, Any], lines: int = 25) -> list[str]:
        logs = build.get("logs") or {}
        if not logs.get("groupName") or not logs.get("streamName"):
            return []
        try:
            events = self.logs.get_log_events(logGroupName=logs["groupName"], logStreamName=logs["streamName"], limit=lines, startFromHead=False)
        except Exception as exc:  # noqa: BLE001 - the tail is a courtesy
            return [f"(the build log could not be read: {type(exc).__name__})"]
        return [str(e.get("message") or "").rstrip() for e in events.get("events") or []][-lines:]

    # -- scan --------------------------------------------------------------------------------------------------------
    def _scan(self) -> str:
        """The image's ECR scan gate: wait for the push scan (DescribeImageScanFindings every SCAN_POLL s, at most
        SCAN_TIMEOUT s), refuse the image when it has findings of a blocking severity (listing up to eight), and deploy
        an image whose scan cannot be read or does not finish as "unscanned" — said so, never counted as clean."""
        if not self.image_uri or "@" not in self.image_uri:
            return Skip("no image to scan")
        repo, digest = self.image_uri.split("/", 1)[1].split("@", 1)
        block = list(self.req.get("scanBlock") if self.req.get("scanBlock") is not None else DEFAULT_SCAN_BLOCK)
        summary: dict[str, Any] = {"repository": repo, "digest": digest, "block": block, "status": "PENDING", "counts": {}, "blocking": {},
                                   "findings": []}
        self.job.progress(scan=summary)
        if self.scan_on_push is None:  # an existing image: does its repository scan on push?
            try:
                found = self.ecr.describe_repositories(repositoryNames=[repo])["repositories"][0]
                self.scan_on_push = bool((found.get("imageScanningConfiguration") or {}).get("scanOnPush"))
            except Exception:  # noqa: BLE001 - unknown: read the findings once, do not wait for one to appear
                self.scan_on_push = False
        started = self.clock()
        register_by = started + (self.SCAN_REGISTER if self.scan_on_push else 0.0)
        deadline, last = started + self.SCAN_TIMEOUT, None
        while True:
            try:
                page = self._read(self.ecr.describe_image_scan_findings, repositoryName=repo, imageId={"imageDigest": digest}, maxResults=1000)
                state = page.get("imageScanStatus") or {}
                status, why = str(state.get("status") or "UNKNOWN"), str(state.get("description") or "")
            except Exception as exc:  # noqa: BLE001 - any read failure means there is no gate: said so below
                if _code(exc) != "ScanNotFoundException" or self.clock() >= register_by:
                    if _code(exc) != "ScanNotFoundException":
                        reason = f"{_code(exc) or type(exc).__name__}: {str(exc)[:200]}"
                    elif self.scan_on_push:
                        reason = f"no scan of the image appeared within {int(self.SCAN_REGISTER)} s (it may predate scan on push)"
                    else:
                        reason = "the repository does not scan on push and the image was never scanned"
                    return self._unscanned(summary, started, reason)
                page, status, why = {}, "PENDING", "the push scan is not registered yet"
            if status != last:
                self.log(f"  image scan {status}{': ' + why if why else ''}")
                last = status
            if status in SCANNED:  # basic scanning's COMPLETE, enhanced scanning's ACTIVE (scanned, monitored continuously)
                return self._scan_gate(summary, started, repo, digest, page, status)
            if status not in ("IN_PROGRESS", "PENDING"):
                return self._unscanned(summary, started, f"the scan is {status}{': ' + why if why else ''}")
            if self.clock() > deadline:
                return self._unscanned(summary, started, f"the scan is still {status} after {int(self.SCAN_TIMEOUT)} s")
            self.sleep(self.SCAN_POLL)

    def _unscanned(self, summary: dict[str, Any], started: float, reason: str) -> Skip:
        summary.update(status="UNSCANNED", reason=reason, seconds=round(self.clock() - started, 1))
        self.out["scan"] = summary
        self.job.progress(scan=summary)
        self.log(f"  the image scan gate did NOT complete ({reason}): deploying unscanned")
        return Skip(f"deploying unscanned: {reason}")

    def _scan_gate(self, summary: dict[str, Any], started: float, repo: str, digest: str, page: Mapping[str, Any], status: str = "COMPLETE") -> str:
        found = page.get("imageScanFindings") or {}
        counts = {str(k).upper(): int(v) for k, v in (found.get("findingSeverityCounts") or {}).items() if int(v or 0)}
        if not counts and (found.get("findings") or found.get("enhancedFindings")):  # no counts given: the findings themselves are counted
            counts = self._count_findings(repo, digest, page)
        block = summary["block"]
        blocking = {s: counts[s] for s in block if counts.get(s)}
        listed = self._findings(repo, digest, page, blocking) if blocking else []
        summary.update(status=status, counts=counts, blocking=blocking, findings=listed, seconds=round(self.clock() - started, 1),
                       completedAt=str(found.get("imageScanCompletedAt") or ""), sourceUpdatedAt=str(found.get("vulnerabilitySourceUpdatedAt") or ""))
        self.out["scan"] = summary
        self.job.progress(scan=summary)
        self.log(f"  image scan findings: {format_counts(counts)}")
        if blocking:
            for line in listed:
                self.log(f"  | {line}")
            raise DeployError(f"the image {repo}@{digest[:19]}… has {format_counts(blocking)} finding(s) at a blocking severity ({', '.join(block)})"
                              f"{': ' + '; '.join(listed) if listed else ''}. Rebuild it on a patched base image, or deploy with scanBlock "
                              "set to fewer severities ([] reports only)")
        gate = f"blocks {', '.join(block)}" if block else "reports only"
        kind = " (enhanced scanning, monitored continuously)" if status == "ACTIVE" else ""
        return f"ECR scan {status}{kind} in {summary['seconds']} s · {format_counts(counts)} · gate {gate}: passed"

    def _count_findings(self, repo: str, digest: str, page: Mapping[str, Any]) -> dict[str, int]:
        """Severity counts from the findings themselves (basic and enhanced alike), every page read (at most 20)."""
        counts: dict[str, int] = {}
        for _ in range(20):
            found = page.get("imageScanFindings") or {}
            for f in [*(found.get("findings") or []), *(found.get("enhancedFindings") or [])]:
                severity = str(f.get("severity") or "UNDEFINED").upper()
                counts[severity] = counts.get(severity, 0) + 1
            if not page.get("nextToken"):
                break
            page = self._read(self.ecr.describe_image_scan_findings, repositoryName=repo, imageId={"imageDigest": digest}, maxResults=1000,
                              nextToken=page["nextToken"])
        return counts

    def _findings(self, repo: str, digest: str, page: Mapping[str, Any], blocking: Mapping[str, int]) -> list[str]:
        """``CVE (package version)`` of the findings that block, heaviest first, at most MAX_LISTED_FINDINGS (basic
        scanning's ``findings`` and enhanced scanning's ``enhancedFindings`` alike; more pages read if needed)."""
        rows: list[tuple[int, str]] = []
        pages = 0
        while True:
            found = page.get("imageScanFindings") or {}
            for f in found.get("findings") or []:
                severity = str(f.get("severity") or "").upper()
                if severity in blocking:
                    attrs = {a.get("key"): a.get("value") for a in f.get("attributes") or []}
                    rows.append((SEVERITIES.index(severity), f"{f.get('name')} ({attrs.get('package_name') or '?'} {attrs.get('package_version') or '?'}, {severity})"))
            for f in found.get("enhancedFindings") or []:
                severity = str(f.get("severity") or "").upper()
                if severity in blocking:
                    details = f.get("packageVulnerabilityDetails") or {}
                    package = (details.get("vulnerablePackages") or [{}])[0]
                    rows.append((SEVERITIES.index(severity), f"{details.get('vulnerabilityId') or f.get('title')} ({package.get('name') or '?'} "
                                                             f"{package.get('version') or '?'}, {severity})"))
            pages += 1
            if len(rows) >= MAX_LISTED_FINDINGS * 4 or not page.get("nextToken") or pages >= 5:
                break
            try:
                page = self.ecr.describe_image_scan_findings(repositoryName=repo, imageId={"imageDigest": digest}, maxResults=1000,
                                                             nextToken=page["nextToken"])
            except Exception:  # noqa: BLE001 - the listing is a courtesy, the counts decided
                break
        return [text for _rank, text in sorted(rows, key=lambda r: r[0])][:MAX_LISTED_FINDINGS]

    # -- runtime -----------------------------------------------------------------------------------------------------
    def _runtime(self) -> str:
        role = self._ensure_role(self.names.runtime_role(self.name), runtime_trust(self.account, self.region), "runtime-execution",
                                 runtime_role_policy(account=self.account, region=self.region, repository_arn=self.repository_arn,
                                                     knowledge_bases=self.req.get("knowledgeBases") or ()),
                                 f"ADLC console: execution role of the runtime {self.name}", runtime=self.name)
        if self.image_uri:
            artifact = {"containerConfiguration": {"containerUri": self.image_uri}}
        else:
            entry = ["opentelemetry-instrument", self.req["entry"]] if self.req.get("instrument") else [self.req["entry"]]
            artifact = {"codeConfiguration": {"code": {"s3": {"bucket": self.bucket, "prefix": self.code_key}}, "runtime": self.req["runtime"], "entryPoint": entry}}
        description = f"ADLC console: {self.req['source']}, job {self.job_id}"
        if self.mode == "create":
            request: dict[str, Any] = {"agentRuntimeName": self.name, "agentRuntimeArtifact": artifact, "roleArn": role,
                                       "networkConfiguration": self.req.get("network") or {"networkMode": "PUBLIC"},
                                       "protocolConfiguration": {"serverProtocol": self.req["protocol"]},
                                       "description": description, "tags": {**CONSOLE_TAG, "adlc:source": self.req["source"]}}
            if self.req["environment"]:
                request["environmentVariables"] = dict(self.req["environment"])
            if self.req.get("filesystems"):
                request["filesystemConfigurations"] = [dict(f) for f in self.req["filesystems"]]
            if self.req.get("lifecycle"):
                request["lifecycleConfiguration"] = dict(self.req["lifecycle"])
            out = self._retry_iam(lambda: self.ctl.create_agent_runtime(**request))
            self.runtime_id, self.runtime_arn = str(out["agentRuntimeId"]), str(out["agentRuntimeArn"])
            call = "CreateAgentRuntime"
        else:
            cur = self.current or {}
            protocol = self.req.get("protocol") or (cur.get("protocolConfiguration") or {}).get("serverProtocol") or "HTTP"
            env = self.req["environment"] if self.req.get("environment") is not None else dict(cur.get("environmentVariables") or {})
            request = {"agentRuntimeId": self.runtime_id, "agentRuntimeArtifact": artifact, "roleArn": role,
                       "networkConfiguration": self.req.get("network") or cur.get("networkConfiguration") or {"networkMode": "PUBLIC"},
                       "protocolConfiguration": {"serverProtocol": protocol}, "environmentVariables": env, "description": description}
            for kept in ("lifecycleConfiguration", "metadataConfiguration", "authorizerConfiguration", "requestHeaderConfiguration", "filesystemConfigurations"):
                if cur.get(kept):
                    request[kept] = cur[kept]
            if self.req.get("lifecycle"):  # what the request names replaces the runtime's own
                request["lifecycleConfiguration"] = dict(self.req["lifecycle"])
            if self.req.get("filesystems") is not None:
                request.pop("filesystemConfigurations", None)
                if self.req["filesystems"]:
                    request["filesystemConfigurations"] = [dict(f) for f in self.req["filesystems"]]
            out = self._retry_iam(lambda: self.ctl.update_agent_runtime(**request))
            call = "UpdateAgentRuntime"
        self.version = str(out.get("agentRuntimeVersion") or "")
        options = {"network": (request.get("networkConfiguration") or {}).get("networkMode"),
                   "sessionStorage": next((f["sessionStorage"]["mountPath"] for f in request.get("filesystemConfigurations") or [] if "sessionStorage" in f), None),
                   "lifecycle": request.get("lifecycleConfiguration")}
        self.out.update(runtimeId=self.runtime_id, runtimeArn=self.runtime_arn, version=self.version, role=role, artifact=artifact, options=options)
        self.job.progress(runtimeId=self.runtime_id, version=self.version)
        if self.on_runtime:
            self.on_runtime({"runtimeId": self.runtime_id, "runtimeArn": self.runtime_arn, "name": self.name, "source": self.req["source"],
                             "role": self.names.runtime_role(self.name), "repository": self.out.get("repository") or self.record.get("repository"),
                             "endpoint": self.endpoint or None, "version": self.version, "jobId": self.job_id,
                             "artifact": self.image_uri or f"s3://{self.bucket}/{self.code_key}", "scan": _scan_brief(self.out.get("scan"))})
        life = options["lifecycle"] or {}
        extra = [f"session storage {options['sessionStorage']}" if options["sessionStorage"] else "",
                 ", ".join(f"{label} {life[key]} s" for key, label in (("idleRuntimeSessionTimeout", "idle"), ("maxLifetime", "max lifetime"))
                           if key in life) if life else "",
                 "VPC" if options["network"] == "VPC" else ""]
        return f"{call} → {self.runtime_id} version {self.version}" + "".join(f" · {e}" for e in extra if e)

    def _ensure_role(self, name: str, trust: Mapping[str, Any], policy_name: str, policy: Mapping[str, Any], description: str,
                     runtime: str | None = None) -> str:
        """The role, made on the console's path with the workspace's permissions boundary (``self.boundary``) and tagged
        with the region it serves, or the console's existing one (``agents.adopt``; given the boundary first, then its
        trust and policy put again); its ARN as IAM gives it. A runtime role of another region is refused: IAM names are
        account-global, a runtime's name is per region."""
        try:
            role = self.iam.get_role(RoleName=name)["Role"]
        except Exception as exc:  # noqa: BLE001
            if not _missing(exc):
                raise
            tags = {**CONSOLE_TAG, **({"adlc:runtime": runtime} if runtime else {}), "adlc:region": self.region}
            role = self.iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(trust), Description=description[:1000],
                                        Tags=[{"Key": k, "Value": v} for k, v in tags.items()], **role_args(self.boundary))["Role"]
            self.iam.put_role_policy(RoleName=name, PolicyName=policy_name, PolicyDocument=json.dumps(policy))
            self.created.setdefault("roles", []).append(name)
            self.log(f"created the role {name} on {ROLE_PATH}" + (" (with the workspace's permissions boundary)" if self.boundary else ""))
            self.sleep(self.IAM_PAUSE)  # a new role is refused for a few seconds
            return str(role["Arn"])
        role, tags = adopt(self.iam, name, self.boundary, role, error=DeployError)
        if runtime and tags.get("adlc:runtime") != runtime:
            raise DeployError(f"the role {name} exists and is not the console's for {runtime}")
        if runtime:
            regions = role_regions(tags, role.get("AssumeRolePolicyDocument"))
            if regions and self.region not in regions:
                raise DeployError(f"the role {name} is the execution role of {runtime} in {', '.join(sorted(regions))} (its trust names that region): "
                                  f"IAM names are account-wide, so a runtime of the same name in {self.region} needs a name of its own")
        if ensure_boundary(self.iam, name, self.boundary, role, tags):
            self.log(f"gave the role {name} the workspace's permissions boundary")
        if _document(role.get("AssumeRolePolicyDocument")) != dict(trust):
            self.iam.update_assume_role_policy(RoleName=name, PolicyDocument=json.dumps(trust))
        self.iam.put_role_policy(RoleName=name, PolicyName=policy_name, PolicyDocument=json.dumps(policy))
        return str(role["Arn"])

    def _retry_iam(self, call: Callable[[], Any]) -> Any:
        for attempt in range(self.IAM_ATTEMPTS):
            try:
                return call()
            except Exception as exc:  # noqa: BLE001
                text = f"{_code(exc)} {exc}".lower()
                if attempt == self.IAM_ATTEMPTS - 1 or not any(marker in text for marker in PROPAGATION):
                    raise
                self.log(f"  IAM not visible yet, retry {attempt + 1}/{self.IAM_ATTEMPTS - 1} in {int(self.IAM_PAUSE)} s: {str(exc)[:160]}")
                self.sleep(self.IAM_PAUSE)
        raise AssertionError("unreachable")

    # -- ready -------------------------------------------------------------------------------------------------------
    def _ready(self) -> str:
        deadline, last = self.clock() + self.RUNTIME_TIMEOUT, None
        while True:
            got = self._read(self.ctl.get_agent_runtime, agentRuntimeId=self.runtime_id)
            status = str(got.get("status"))
            if status != last:
                self.log(f"  runtime {status}")
                last = status
            if status == "READY" and (not self.version or str(got.get("agentRuntimeVersion")) == self.version):
                break
            if status.endswith("FAILED"):
                raise DeployError(f"the runtime is {status}: {got.get('failureReason') or 'no reason given'}")
            if self.clock() > deadline:
                raise DeployError(f"the runtime is still {status} after {int(self.RUNTIME_TIMEOUT / 60)} min")
            self.sleep(self.RUNTIME_POLL)
        self.version = str(got.get("agentRuntimeVersion") or self.version)
        self.out.update(version=self.version, status="READY")
        if self.endpoint:
            try:
                self.ctl.get_agent_runtime_endpoint(agentRuntimeId=self.runtime_id, endpointName=self.endpoint)
                self.ctl.update_agent_runtime_endpoint(agentRuntimeId=self.runtime_id, endpointName=self.endpoint, agentRuntimeVersion=self.version)
                verb = "moved"
            except Exception as exc:  # noqa: BLE001
                if not _missing(exc):
                    raise
                self.ctl.create_agent_runtime_endpoint(agentRuntimeId=self.runtime_id, name=self.endpoint, agentRuntimeVersion=self.version,
                                                       description=f"ADLC console, job {self.job_id}", tags=dict(CONSOLE_TAG))
                verb = "created"
            self._wait_endpoint(self.endpoint)
            self.out["endpoint"] = self.endpoint
            return f"READY version {self.version} · endpoint {self.endpoint} {verb} → {self.version}"
        try:
            self._wait_endpoint("DEFAULT")
        except Exception as exc:  # noqa: BLE001
            if not _missing(exc):
                raise
            return f"READY version {self.version}"
        return f"READY version {self.version} · DEFAULT → {self.version}"

    def _wait_endpoint(self, name: str) -> None:
        deadline, last = self.clock() + self.ENDPOINT_TIMEOUT, None
        while True:
            got = self._read(self.ctl.get_agent_runtime_endpoint, agentRuntimeId=self.runtime_id, endpointName=name)
            status, live = str(got.get("status")), str(got.get("liveVersion") or "")
            if (status, live) != last:
                self.log(f"  endpoint {name} {status}, live version {live or '-'}")
                last = (status, live)
            if status == "READY" and live == self.version:
                return
            if status.endswith("FAILED"):
                raise DeployError(f"the endpoint {name} is {status}: {got.get('failureReason') or 'no reason given'}")
            if self.clock() > deadline:
                raise DeployError(f"the endpoint {name} still serves {live or 'nothing'} ({status}) after {int(self.ENDPOINT_TIMEOUT / 60)} min")
            self.sleep(self.ENDPOINT_POLL)

    # -- smoke -------------------------------------------------------------------------------------------------------
    def _smoke(self) -> str:
        protocol = self.req.get("protocol") or ((self.current or {}).get("protocolConfiguration") or {}).get("serverProtocol") or "HTTP"
        if not self.req.get("smoke"):
            return Skip("off")
        if protocol != "HTTP":
            return Skip(f"{protocol} runtime: the smoke call speaks the HTTP contract only")
        session_id = f"adlc-smoke-{uuid.uuid4().hex}"
        params: dict[str, Any] = {"agentRuntimeArn": self.runtime_arn, "runtimeSessionId": session_id,
                                  "payload": json.dumps({"prompt": self.req["prompt"]}, ensure_ascii=False).encode("utf-8")}
        if self.endpoint:
            params["qualifier"] = self.endpoint
        started = self.clock()
        response = self.data.invoke_agent_runtime(**params)
        body = response.get("response")
        try:
            raw = body.read(MAX_SMOKE_BYTES) if hasattr(body, "read") else (body or b"")
        finally:
            if hasattr(body, "close"):
                body.close()
        seconds = round(self.clock() - started, 1)
        kind = str(response.get("contentType") or "")
        answer = answer_text(raw if isinstance(raw, bytes) else str(raw).encode(), kind)
        try:
            stop = {k: v for k, v in params.items() if k in ("agentRuntimeArn", "runtimeSessionId", "qualifier")}
            self.data.stop_runtime_session(**stop)
        except Exception as exc:  # noqa: BLE001 - the session idles out anyway
            self.log(f"  session not stopped: {_code(exc) or type(exc).__name__}")
        self.out["smoke"] = {"answer": answer[:2000], "seconds": seconds, "contentType": kind, "statusCode": response.get("statusCode"),
                             "sessionId": session_id, "qualifier": self.endpoint or "DEFAULT", "prompt": self.req["prompt"]}
        if int(response.get("statusCode") or 200) >= 400:
            raise DeployError(f"the smoke call answered {response.get('statusCode')}: {answer[:300]}")
        return f"{seconds} s · {answer[:160]}"

    # -- a failed first deployment ----------------------------------------------------------------------------------
    def _take_back(self) -> None:
        """Delete what this job created before its runtime existed (sources, image repository, runtime role)."""
        done = []
        try:
            if self.created.get("sources"):
                done.append(f"{delete_versions(self.s3, self.bucket, self.account, self.prefix + '/')} source object(s)")
            if self.created.get("repository"):
                self.ecr.delete_repository(repositoryName=self.created["repository"], force=True)
                done.append(f"repository {self.created['repository']}")
            for role in self.created.get("roles") or []:
                if role == self.names.runtime_role(self.name):
                    delete_role(self.iam, role, self.boundary)
                    done.append(f"role {role}")
        except Exception as exc:  # noqa: BLE001 - the original failure matters more
            self.log(f"taking back what this job created failed: {type(exc).__name__}: {str(exc)[:200]}")
        if done:
            self.log("took back: " + ", ".join(done))


# -- delete -------------------------------------------------------------------------------------------------------

def delete_role(iam: Any, name: str, boundary: str | None = None) -> None:
    """A console role and its policies (the caller has checked it is the console's to delete). With the workspace's
    permissions boundary, a role made before it gets it first: a spoke role deletes a role's policies only while it
    carries the boundary."""
    ensure_boundary(iam, name, boundary)
    for policy in iam.list_role_policies(RoleName=name).get("PolicyNames") or []:
        iam.delete_role_policy(RoleName=name, PolicyName=policy)
    for attached in iam.list_attached_role_policies(RoleName=name).get("AttachedPolicies") or []:
        iam.detach_role_policy(RoleName=name, PolicyArn=attached["PolicyArn"])
    iam.delete_role(RoleName=name)


def delete_versions(s3: Any, bucket: str, account: str, prefix: str) -> int:
    """Delete every version (and delete marker) of the objects under ``prefix`` (the bucket is versioned)."""
    count, kwargs = 0, {"Bucket": bucket, "Prefix": prefix, "ExpectedBucketOwner": account}
    while True:
        page = s3.list_object_versions(**kwargs)
        doomed = [{"Key": v["Key"], "VersionId": v["VersionId"]} for v in (page.get("Versions") or []) + (page.get("DeleteMarkers") or [])
                  if str(v.get("Key") or "").startswith(prefix)]
        for i in range(0, len(doomed), 1000):
            s3.delete_objects(Bucket=bucket, ExpectedBucketOwner=account, Delete={"Objects": doomed[i:i + 1000], "Quiet": True})
        count += len(doomed)
        if not page.get("IsTruncated"):
            return count
        kwargs.update(KeyMarker=page.get("NextKeyMarker"), VersionIdMarker=page.get("NextVersionIdMarker"))


class Teardown(Staged):
    """Delete a console runtime (``undeploy``): its named endpoints, the runtime, its image repository (unless
    kept), its execution role and its sources. ``runtime`` is its GetAgentRuntime, or None when it is gone already
    (the console's record of it then names what is left: only that, never what a live runtime of the same name — or
    a canary's candidate beside it — uses now, :meth:`_users`). A role whose tag or trust names another region is
    another region's runtime's and is left alone."""

    POLL, TIMEOUT = 5.0, 20 * 60.0  # live: a named endpoint took 3 min to go, the runtime 5 min

    def __init__(self, session: Any, *, account: str, region: str, runtime_id: str, name: str, runtime: Mapping[str, Any] | None, job: Any,
                 keep_repository: bool = False, record: Mapping[str, Any] | None = None, names: Names = NAMES,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic, boundary: str | None = None):
        super().__init__(job, DELETE_STAGES, clock)
        self.boundary = boundary  # the workspace's permissions boundary: a role made before it gets it before it is deleted
        self.account, self.region, self.runtime_id, self.name, self.runtime = account, region, runtime_id, name, runtime
        self.keep_repository, self.record, self.names, self.sleep = keep_repository, record or {}, names, sleep
        self.ctl = client(session, "bedrock-agentcore-control", region)
        self.s3 = client(session, "s3", region)
        self.iam = client(session, "iam")
        self.ecr = client(session, "ecr", region)
        self.deleted: dict[str, Any] = {}
        self._in_use: dict[str, Any] | None = None

    def _users(self) -> dict[str, Any]:
        """What live runtimes of this name, and canary candidates beside it (``<name>_c<hex>``, which run with
        production's role and images), use in any of their versions (an endpoint or a rollback can return to each):
        their roles, repositories, image digests and code keys; ``complete`` False when a version could not be read
        (then nothing they might use is deleted). Read only for a record whose runtime is gone (a runtime that exists
        is the only one of its name in the region)."""
        if self._in_use is None:
            found: dict[str, Any] = {"runtimes": [], "roles": set(), "repositories": set(), "images": set(), "code": set(), "complete": True}
            if self.runtime is None:
                twin = re.compile(rf"^{re.escape(self.name[:38])}_c[0-9a-f]{{8}}$")
                for r in _pages(self.ctl.list_agent_runtimes, "agentRuntimes"):
                    rid, rname = str(r.get("agentRuntimeId") or ""), str(r.get("agentRuntimeName") or "")
                    if rid == self.runtime_id or not (rname == self.name or twin.match(rname)):
                        continue
                    try:
                        versions, complete = runtime_versions(self.ctl, rid)
                    except Exception as exc:  # noqa: BLE001
                        if _missing(exc):
                            continue
                        raise
                    found["runtimes"].append(rid)
                    found["complete"] = found["complete"] and complete
                    for got in versions:
                        role = str(got.get("roleArn") or "").rsplit("/", 1)[-1]
                        if role:
                            found["roles"].add(role)
                        artifact = got.get("agentRuntimeArtifact") or {}
                        uri = str((artifact.get("containerConfiguration") or {}).get("containerUri") or "")
                        image = IMAGE_URI.match(uri)
                        if image:
                            found["repositories"].add(image["repo"])
                            if image["digest"]:
                                found["images"].add(image["digest"])
                        key = str((((artifact.get("codeConfiguration") or {}).get("code") or {}).get("s3") or {}).get("prefix") or "")
                        if key:
                            found["code"].add(key)
            self._in_use = found
        return self._in_use

    def _unread(self, what: str) -> str | None:
        """Why ``what`` is kept when a version of a live runtime of this name could not be read, else None."""
        users = self._users()
        if users["complete"]:
            return None
        return f"{what} kept: a version of {', '.join(users['runtimes'])} could not be read, and it may run from it"

    def _record_images(self, repo: str) -> list[str]:
        """The image digests in ``repo`` the record's own deployments ran (its history's artifacts)."""
        out = []
        for entry in self.record.get("history") or []:
            image = IMAGE_URI.match(str(entry.get("artifact") or ""))
            if image and image["repo"] == repo and image["digest"] and image["digest"] not in out:
                out.append(image["digest"])
        return out

    def _record_prefixes(self) -> list[str]:
        """The record's own folders under ``deployments/<name>/``: each deployment job's, and each artifact's (a
        promoted canary candidate's lives in the candidate's job folder)."""
        base = f"deployments/{self.name}/"
        out: list[str] = []
        for entry in self.record.get("history") or []:
            folders = [f"{base}{entry['jobId']}/"] if entry.get("jobId") else []
            artifact = str(entry.get("artifact") or "")
            if artifact.startswith("s3://"):
                key = artifact[5:].split("/", 1)[-1]
                if key.startswith(base) and "/" in key[len(base):]:
                    folders.append(base + key[len(base):].split("/", 1)[0] + "/")
            out += [f for f in folders if f not in out]
        return out

    def run(self) -> dict[str, Any]:
        for name in DELETE_STAGES:
            self.stage(name, getattr(self, f"_{name.replace('-', '_')}"))
        return {"runtimeId": self.runtime_id, "name": self.name, "deleted": self.deleted, "stages": self.stages}

    def _check(self) -> str:
        if self.runtime is None:
            return f"{self.name}: the runtime is gone already; removing what the console made for it"
        tags = self.ctl.list_tags_for_resource(resourceArn=self.runtime["agentRuntimeArn"]).get("tags") or {}
        if tags.get("adlc:console") != "1":
            raise DeployError("only runtimes deployed from this console (tag adlc:console=1) can be deleted here")
        return f"{self.name} · {tags.get('adlc:source') or self.record.get('source') or '?'} · version {self.runtime.get('agentRuntimeVersion')} · console-tagged"

    def _endpoints(self) -> str:
        if self.runtime is None:
            return Skip("no runtime")
        named = [e["name"] for e in _pages(self.ctl.list_agent_runtime_endpoints, "runtimeEndpoints", agentRuntimeId=self.runtime_id)
                 if e.get("name") != "DEFAULT"]
        if not named:
            return Skip("no named endpoints")
        for endpoint in named:
            self.ctl.delete_agent_runtime_endpoint(agentRuntimeId=self.runtime_id, endpointName=endpoint)
        deadline = self.clock() + self.TIMEOUT
        while [e for e in self._read(lambda: _pages(self.ctl.list_agent_runtime_endpoints, "runtimeEndpoints", agentRuntimeId=self.runtime_id))
               if e.get("name") in named]:
            if self.clock() > deadline:
                raise DeployError(f"the endpoints {', '.join(named)} are still there after {int(self.TIMEOUT / 60)} min")
            self.sleep(self.POLL)
        self.deleted["endpoints"] = named
        return f"deleted {', '.join(named)}"

    def _runtime(self) -> str:
        if self.runtime is None:
            return Skip("gone already")
        try:
            self.ctl.delete_agent_runtime(agentRuntimeId=self.runtime_id)
        except Exception as exc:  # noqa: BLE001 - gone already, or still going from an earlier delete (its wait was cut off)
            if not _missing(exc) and not (_code(exc) == "ConflictException" and str(self.runtime.get("status")) == "DELETING"):
                raise
        deadline, last = self.clock() + self.TIMEOUT, None
        while True:
            try:
                status = str(self._read(self.ctl.get_agent_runtime, agentRuntimeId=self.runtime_id).get("status"))
            except Exception as exc:  # noqa: BLE001
                if not _missing(exc):
                    raise
                break
            if status != last:
                self.job.log(f"  runtime {status}")
                last = status
            if self.clock() > deadline:
                raise DeployError(f"the runtime is still {status} after {int(self.TIMEOUT / 60)} min")
            self.sleep(self.POLL)
        self.deleted["runtime"] = self.runtime_id
        return f"deleted {self.runtime_id}"

    def _repository(self) -> str:
        repo = self.record.get("repository") or self.names.repository(self.name)
        try:
            found = self.ecr.describe_repositories(repositoryNames=[repo])["repositories"][0]
        except Exception as exc:  # noqa: BLE001
            if not _missing(exc):
                raise
            return Skip(f"no image repository ({repo})")
        tags = {t["Key"]: t["Value"] for t in self.ecr.list_tags_for_resource(resourceArn=found["repositoryArn"]).get("tags") or []}
        if tags.get("adlc:console") != "1" or tags.get("adlc:runtime") != self.name:
            return Skip(f"{repo} is not this runtime's console repository: left alone")
        if self.keep_repository:
            return Skip(f"{repo} kept")
        users = self._users()
        unread = self._unread(repo)
        if unread:
            return Skip(unread)
        if repo in users["repositories"]:  # a stale record: a version of a live runtime of the same name pulls from it
            mine = [d for d in self._record_images(repo) if d not in users["images"]]
            if mine:
                self.ecr.batch_delete_image(repositoryName=repo, imageIds=[{"imageDigest": d} for d in mine])
                self.deleted["images"] = mine
            return Skip(f"{repo} is used by {', '.join(users['runtimes'])}: kept"
                        + (f"; deleted the {len(mine)} image(s) of this record no live runtime runs" if mine else ""))
        self.ecr.delete_repository(repositoryName=repo, force=True)
        self.deleted["repository"] = repo
        return f"deleted {repo} and its images"

    def _role(self) -> str:
        role = self.record.get("role") or self.names.runtime_role(self.name)
        try:
            tags = {t["Key"]: t["Value"] for t in self.iam.list_role_tags(RoleName=role).get("Tags") or []}
        except Exception as exc:  # noqa: BLE001
            if not _missing(exc):
                raise
            return Skip(f"no role {role}")
        if tags.get("adlc:console") != "1" or tags.get("adlc:runtime") != self.name:
            return Skip(f"{role} is not this runtime's console role: left alone")
        found = self.iam.get_role(RoleName=role)["Role"]
        why = refusal(role, found, tags, self.boundary)  # off the console's path (in a spoke workspace, any role off it)
        if why:
            return Skip(why)
        regions = role_regions(tags, found.get("AssumeRolePolicyDocument"))
        if regions and self.region not in regions:
            return Skip(f"{role} is the execution role of {self.name} in {', '.join(sorted(regions))}: left alone")
        users = self._users()
        unread = self._unread(role)
        if unread:
            return Skip(unread)
        if role in users["roles"]:
            return Skip(f"{role} is used by {', '.join(users['runtimes'])}: kept")
        delete_role(self.iam, role, self.boundary)
        self.deleted["role"] = role
        return f"deleted {role}"

    def _sources(self) -> str:
        bucket = console_bucket(self.account, self.region)
        if self.runtime is None:  # a stale record: its own job folders only, none a version of a live runtime runs from
            users = self._users()
            unread = self._unread("the record's sources")
            if unread:
                return Skip(unread)
            folders = [f for f in self._record_prefixes() if not any(key.startswith(f) for key in users["code"])]
            if not folders:
                return Skip(f"the record names no sources of its own under s3://{bucket}/deployments/{self.name}/")
        else:
            folders = [f"deployments/{self.name}/"]
        count = 0
        try:
            for folder in folders:
                count += delete_versions(self.s3, bucket, self.account, folder)
        except Exception as exc:  # noqa: BLE001
            if not _missing(exc):
                raise
            return Skip("no console bucket")
        self.deleted["objects"] = count
        where = folders[0] if len(folders) == 1 else f"{len(folders)} folders of deployments/{self.name}/"
        return f"{count} object version(s) under s3://{bucket}/{where} deleted" if count else Skip("no sources")


# -- the console's side ----------------------------------------------------------------------------------------------

def _console_runtime(ctl: Any, runtime_id: str) -> tuple[dict[str, Any], dict[str, str]]:
    """A runtime and its tags; refuses one the console did not deploy (or a Harness's)."""
    if not RUNTIME_ID.match(runtime_id or ""):
        raise DeployError(f"not a runtime id: {runtime_id}")
    got = ctl.get_agent_runtime(agentRuntimeId=runtime_id)
    tags = ctl.list_tags_for_resource(resourceArn=got["agentRuntimeArn"]).get("tags") or {}
    if tags.get("adlc:console") != "1" or str(got.get("agentRuntimeName") or "").startswith("harness_"):
        raise DeployError("only runtimes deployed from this console (tag adlc:console=1) can be changed or deleted here")
    return got, tags


def _scan_brief(scan: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """What a deployment's record keeps of its image scan."""
    if not scan:
        return None
    return {k: scan.get(k) for k in ("status", "counts", "block", "reason") if scan.get(k) not in (None, "")}


def _remember(store: Any, workspace: str, fields: Mapping[str, Any]) -> None:
    def change(all_: dict[str, Any]) -> dict[str, Any]:
        rid = str(fields["runtimeId"])
        rec = dict(all_.get(rid) or {"workspace": workspace, "createdAt": _now(), "history": []})
        rec.update({k: fields[k] for k in ("runtimeId", "runtimeArn", "name", "source", "role", "repository", "endpoint") if fields.get(k) is not None})
        rec["updatedAt"] = _now()
        entry = {"version": fields.get("version"), "jobId": fields.get("jobId"), "source": fields.get("source"), "artifact": fields.get("artifact"), "at": _now()}
        if fields.get("scan"):
            entry["scan"] = fields["scan"]
        rec["history"] = (rec.get("history") or [])[-49:] + [entry]
        return {**all_, rid: rec}

    store.update("deployments", {}, change)


def _records(store: Any, workspace: str) -> dict[str, dict[str, Any]]:
    return {k: v for k, v in (store.read("deployments", {}) or {}).items() if v.get("workspace") == workspace}


_MOVES: dict[str, threading.Lock] = {}
_MOVES_GUARD = threading.Lock()


@contextmanager
def moves(runtime_id: str, *, busy: Callable[[str], Exception] = DeployError, what: str | None = None) -> Iterator[None]:
    """One action at a time on a runtime (by its id; or any key, e.g. ``create:<name>``, an experiment's
    ``harness:<id>``): held while an action checks that nothing else is moving it and starts moving it — a deploy page
    job or endpoint move, or a runtime canary's start, ramp, promotion, rollback or cleanup on it. Another action waits
    up to :data:`MOVE_WAIT` s, then is refused (``busy``; ``what`` names the thing in the message)."""
    with _MOVES_GUARD:
        lock = _MOVES.setdefault(runtime_id, threading.Lock())
    if not lock.acquire(timeout=MOVE_WAIT):
        raise busy(f"another action on {what or f'the runtime {runtime_id}'} is still running: try again in a minute")
    try:
        yield
    finally:
        lock.release()


def _busy(console: Any, workspace: str, name: str) -> None:
    for kind in JOB_KINDS:
        for job in console.jobs.list(workspace=workspace, kind=kind):
            if job.get("status") == "running" and (job.get("params") or {}).get("name") == name:
                raise DeployError(f"{name} has a {kind} job running ({job['id']}): wait for it")


def _context(console: Any, workspace: str) -> tuple[dict[str, Any], Any]:
    ws = console.workspaces.get(workspace)
    console.workspaces.verify(workspace)
    return ws, console.workspaces.session(workspace)


def _held(console: Any, workspace: str, runtime_id: str, endpoint: str | None = None, *, but: str | None = None) -> dict[str, Any] | None:
    """The runtime canary that holds this runtime (or this endpoint of it), if any (``runtime_canary.holder``; ``but``:
    a canary whose own job is asking)."""
    from . import runtime_canary  # it builds on this module

    return runtime_canary.holder(console, workspace, runtime_id, endpoint, but=but)


def _mover(console: Any, workspace: str, runtime_id: str, *, but: str | None = None) -> dict[str, Any] | None:
    """The runtime canary whose promotion or rollback job is moving this runtime now, if any (``runtime_canary.mover``)."""
    from . import runtime_canary

    return runtime_canary.mover(console, workspace, runtime_id, but=but)


def _held_error(rec: Mapping[str, Any], what: str, *, moving: bool = False) -> DeployError:
    if moving:
        return DeployError(f"{what}: the runtime canary {rec['id']} of {(rec.get('agent') or {}).get('name')} is {rec.get('status')}, and its job is "
                           "publishing production and moving its endpoint now: wait for it")
    return DeployError(f"{what} belongs to the runtime canary {rec['id']} ({rec.get('status')}) of {(rec.get('agent') or {}).get('name')}: "
                       "its gate is about exactly these versions; roll the canary back or promote it, and clean it up, on the A/B page")


def _refuse_held(console: Any, workspace: str, runtime_id: str, endpoint: str | None, what: str, *, but: str | None = None) -> None:
    """Refused while a canary's promotion or rollback moves the runtime, or while a canary holds it (or this endpoint)."""
    mover = _mover(console, workspace, runtime_id, but=but)
    if mover:
        raise _held_error(mover, what, moving=True)
    rec = _held(console, workspace, runtime_id, endpoint, but=but)
    if rec:
        raise _held_error(rec, what)


def start_deploy(console: Any, workspace: str, body: Mapping[str, Any], *, runtime_id: str | None = None, names: Names = NAMES) -> dict[str, Any]:
    """Start a ``deploy`` job: a new runtime, or (``runtime_id``) a new version of a console runtime."""
    req = deploy_request(body, mode="update" if runtime_id else "create")
    ws, session = _context(console, workspace)
    ctl = client(session, "bedrock-agentcore-control", ws["region"])
    current, record = None, None
    if runtime_id:
        current, _tags = _console_runtime(ctl, runtime_id)
        req["name"] = str(current["agentRuntimeName"])
        record = _records(console.store, workspace).get(runtime_id)
    elif any(r.get("agentRuntimeName") == req["name"] for r in _pages(ctl.list_agent_runtimes, "agentRuntimes")):
        raise DeployError(f"a runtime named {req['name']} exists: publish a new version of it instead")
    params = {**public_params(req), **({"runtimeId": runtime_id} if runtime_id else {})}

    def work(job: Any) -> dict[str, Any]:
        return Pipeline(session, account=ws["accountId"], region=ws["region"], request=req, job=job, names=names, current=current, record=record,
                        on_runtime=lambda fields: _remember(console.store, workspace, fields), boundary=boundary_of(ws)).run()

    with moves(runtime_id) if runtime_id else moves(f"create:{req['name']}"):  # a new runtime: one create of a name at a time
        if runtime_id:
            mover = _mover(console, workspace, runtime_id)  # a canary's promotion or rollback is publishing it now
            if mover:
                raise _held_error(mover, f"the runtime {req['name']}", moving=True)
            held = _held(console, workspace, runtime_id)
            if held and (held.get("copy") or {}).get("id") == runtime_id:  # a new version of production itself: the gate's (c) refuses it later
                raise _held_error(held, f"the runtime {req['name']} (a canary's candidate)")
        _busy(console, workspace, req["name"])
        return console.jobs.start("deploy", workspace, params, work, label=f"{'新版本' if runtime_id else '部署'} {req['name']}")


def start_delete(console: Any, workspace: str, runtime_id: str, *, keep_repository: bool = False, names: Names = NAMES) -> dict[str, Any]:
    """Start an ``undeploy`` job for a console runtime (or what is left of one the console recorded)."""
    if not RUNTIME_ID.match(runtime_id or ""):
        raise DeployError(f"not a runtime id: {runtime_id}")
    ws, session = _context(console, workspace)
    ctl = client(session, "bedrock-agentcore-control", ws["region"])
    record = _records(console.store, workspace).get(runtime_id)
    try:
        runtime, _tags = _console_runtime(ctl, runtime_id)
        name = str(runtime["agentRuntimeName"])
    except Exception as exc:  # noqa: BLE001
        if not _missing(exc) or not record:
            raise
        runtime, name = None, str(record["name"])

    def work(job: Any) -> dict[str, Any]:
        out = Teardown(session, account=ws["accountId"], region=ws["region"], runtime_id=runtime_id, name=name, runtime=runtime, job=job,
                       keep_repository=keep_repository, record=record, names=names, boundary=boundary_of(ws)).run()
        console.store.update("deployments", {}, lambda all_: {k: v for k, v in all_.items() if k != runtime_id})
        return out

    with moves(runtime_id):
        _refuse_held(console, workspace, runtime_id, None, f"the runtime {name}")
        _busy(console, workspace, name)
        return console.jobs.start("undeploy", workspace, {"runtimeId": runtime_id, "name": name, "keepRepository": keep_repository}, work,
                                  label=f"删除 {name}")


def deployments(console: Any, workspace: str) -> list[dict[str, Any]]:
    """The console's runtimes in the workspace, with their live status (MISSING: deleted outside the console)."""
    records = _records(console.store, workspace)
    if not records:
        return []
    ws, session = _context(console, workspace)
    live = {r["agentRuntimeId"]: r for r in _pages(client(session, "bedrock-agentcore-control", ws["region"]).list_agent_runtimes, "agentRuntimes")}
    out = []
    for rid, rec in records.items():
        r = live.get(rid) or {}
        out.append({"runtimeId": rid, "name": rec.get("name"), "source": rec.get("source"), "endpoint": rec.get("endpoint"), "repository": rec.get("repository"),
                    "status": r.get("status") or "MISSING", "version": r.get("agentRuntimeVersion"), "arn": rec.get("runtimeArn"),
                    "updatedAt": str(r.get("lastUpdatedAt") or rec.get("updatedAt") or ""), "deploys": len(rec.get("history") or [])})
    return sorted(out, key=lambda d: str(d["name"]))


def deployment(console: Any, workspace: str, runtime_id: str) -> dict[str, Any]:
    """One runtime: its configuration (variable names only), versions (newest first), endpoints and the console's
    record of its deployments."""
    if not RUNTIME_ID.match(runtime_id or ""):
        raise DeployError(f"not a runtime id: {runtime_id}")
    ws, session = _context(console, workspace)
    ctl = client(session, "bedrock-agentcore-control", ws["region"])
    record = _records(console.store, workspace).get(runtime_id)
    try:
        r = ctl.get_agent_runtime(agentRuntimeId=runtime_id)
    except Exception as exc:  # noqa: BLE001
        if not _missing(exc) or not record:
            raise
        return {"runtimeId": runtime_id, "name": record.get("name"), "status": "MISSING", "console": True, "record": record, "versions": [], "endpoints": []}
    tags = ctl.list_tags_for_resource(resourceArn=r["agentRuntimeArn"]).get("tags") or {}
    versions = _pages(ctl.list_agent_runtime_versions, "agentRuntimes", agentRuntimeId=runtime_id)
    endpoints = _pages(ctl.list_agent_runtime_endpoints, "runtimeEndpoints", agentRuntimeId=runtime_id)
    return {"runtimeId": runtime_id, "name": r.get("agentRuntimeName"), "arn": r.get("agentRuntimeArn"), "status": r.get("status"),
            "version": r.get("agentRuntimeVersion"), "failureReason": r.get("failureReason"), "artifact": r.get("agentRuntimeArtifact"),
            "protocol": (r.get("protocolConfiguration") or {}).get("serverProtocol"), "roleArn": r.get("roleArn"),
            "network": (r.get("networkConfiguration") or {}).get("networkMode"), "lifecycle": r.get("lifecycleConfiguration"),
            "vpc": (r.get("networkConfiguration") or {}).get("networkModeConfig"),
            "sessionStorage": next((f["sessionStorage"].get("mountPath") for f in r.get("filesystemConfigurations") or [] if "sessionStorage" in f), None),
            "filesystems": r.get("filesystemConfigurations") or [],
            "environment": sorted((r.get("environmentVariables") or {}).keys()), "description": r.get("description"),
            "updatedAt": str(r.get("lastUpdatedAt") or ""), "tags": tags, "console": tags.get("adlc:console") == "1", "record": record,
            "versions": sorted(({"version": v.get("agentRuntimeVersion"), "status": v.get("status"), "description": v.get("description"),
                                 "updatedAt": str(v.get("lastUpdatedAt") or "")} for v in versions), key=lambda v: -int(v["version"] or 0)),
            "endpoints": [{"name": e.get("name"), "liveVersion": e.get("liveVersion"), "targetVersion": e.get("targetVersion"), "status": e.get("status"),
                           "updatedAt": str(e.get("lastUpdatedAt") or "")} for e in endpoints]}


def point_endpoint(console: Any, workspace: str, runtime_id: str, body: Mapping[str, Any], *, but: str | None = None) -> dict[str, Any]:
    """Create a named endpoint on a version, or move one to another version (a rollback is a move back). ``but``: the
    runtime canary whose own rollback job moves it, without the runtime's lock: its rolling-back state already refuses
    every other move of the runtime (``runtime_canary.mover``), and the lock may be held by another canary's ramp."""
    name, version = str(body.get("name") or "").strip(), str(body.get("version") or "").strip()
    if not NAME.match(name) or name.upper() == "DEFAULT":
        raise DeployError("name: an endpoint name (a letter, then letters, digits or underscores), not DEFAULT")
    if not VERSION.match(version):
        raise DeployError("version: a version number of the runtime")
    ws, session = _context(console, workspace)
    ctl = client(session, "bedrock-agentcore-control", ws["region"])
    _console_runtime(ctl, runtime_id)
    with nullcontext() if but else moves(runtime_id):
        _refuse_held(console, workspace, runtime_id, name, f"the endpoint {name}", but=but)
        try:
            ctl.get_agent_runtime_endpoint(agentRuntimeId=runtime_id, endpointName=name)
            out = ctl.update_agent_runtime_endpoint(agentRuntimeId=runtime_id, endpointName=name, agentRuntimeVersion=version)
            verb = "moved"
        except Exception as exc:  # noqa: BLE001
            if not _missing(exc):
                raise
            out = ctl.create_agent_runtime_endpoint(agentRuntimeId=runtime_id, name=name, agentRuntimeVersion=version, description="ADLC console",
                                                    tags=dict(CONSOLE_TAG))
            verb = "created"
    return {"name": name, "version": version, "status": out.get("status"), "action": verb}


def delete_endpoint(console: Any, workspace: str, runtime_id: str, name: str) -> dict[str, Any]:
    if not NAME.match(name or "") or name.upper() == "DEFAULT":
        raise DeployError("only a named endpoint can be deleted (DEFAULT goes with the runtime)")
    ws, session = _context(console, workspace)
    ctl = client(session, "bedrock-agentcore-control", ws["region"])
    _console_runtime(ctl, runtime_id)
    with moves(runtime_id):
        _refuse_held(console, workspace, runtime_id, name, f"the endpoint {name}")
        out = ctl.delete_agent_runtime_endpoint(agentRuntimeId=runtime_id, endpointName=name)
    return {"name": name, "status": out.get("status")}


def deploy_jobs(console: Any, workspace: str) -> list[dict[str, Any]]:
    jobs = [j for kind in JOB_KINDS for j in console.jobs.list(workspace=workspace, kind=kind)]
    return sorted(jobs, key=lambda j: str(j.get("createdAt") or ""), reverse=True)


def _safe(fn: Callable[[Any], Any]) -> Callable[[Any], Any]:
    """A route whose refusals answer 400 and whose missing runtime answers 404 (not an internal error)."""
    return safe(fn, (DeployError,))


def register(router: Any) -> None:
    """``/workspaces/{wid}/deployments``: list, deploy (admin), one runtime, a new version (admin), endpoints
    (admin), delete (admin, ``?keepRepo=1`` keeps the image repository); ``/deployments/jobs`` the jobs."""
    add = router.add
    add("GET", "/workspaces/{wid}/deployments", _safe(lambda r: (200, {"deployments": deployments(r.console, r.workspace())})))
    add("GET", "/workspaces/{wid}/deployments/jobs", _safe(lambda r: (200, {"jobs": deploy_jobs(r.console, r.workspace())})))
    add("POST", "/workspaces/{wid}/deployments", _safe(lambda r: (202, start_deploy(r.console, r.workspace(), r.body))), admin=True)
    add("GET", "/workspaces/{wid}/deployments/{rid}", _safe(lambda r: (200, deployment(r.console, r.workspace(), r.params["rid"]))))
    add("POST", "/workspaces/{wid}/deployments/{rid}/versions",
        _safe(lambda r: (202, start_deploy(r.console, r.workspace(), r.body, runtime_id=r.params["rid"]))), admin=True)
    add("POST", "/workspaces/{wid}/deployments/{rid}/endpoints",
        _safe(lambda r: (200, point_endpoint(r.console, r.workspace(), r.params["rid"], r.body))), admin=True)
    add("DELETE", "/workspaces/{wid}/deployments/{rid}/endpoints/{name}",
        _safe(lambda r: (200, delete_endpoint(r.console, r.workspace(), r.params["rid"], r.params["name"]))), admin=True)
    add("DELETE", "/workspaces/{wid}/deployments/{rid}",
        _safe(lambda r: (202, start_delete(r.console, r.workspace(), r.params["rid"], keep_repository=r.query.get("keepRepo") in ("1", "true")))),
        admin=True)
