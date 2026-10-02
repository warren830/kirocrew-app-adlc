"""engine console.deploy: the BYOC pipeline on a fake AWS (no AWS) — every request checked against the botocore model
of its service, the stage order, the console-tagged rules for update and delete — the routes and their admin
rule through the console server, and the sample agent's runtime contract."""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
import stat
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import botocore.session
import pytest
import yaml
from botocore import xform_name
from botocore.exceptions import ClientError
from botocore.validate import ParamValidator

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "engine"))

from workshop_customizer.console import deploy as dp  # noqa: E402

FIXTURE = REPO / "tests" / "fixtures" / "byoc_agent"
ACCOUNT, REGION = "111122223333", "us-west-2"
BUCKET = dp.console_bucket(ACCOUNT, REGION)
REGISTRY = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com"
BOTO = botocore.session.get_session()
_OPS: dict[str, tuple] = {}


def check_shape(service: str, op: str, params: dict) -> None:
    """The request is one the real API takes: names, types, required members (botocore's own validation)."""
    if service not in _OPS:
        model = BOTO.get_service_model(service)
        _OPS[service] = (model, {xform_name(n): n for n in model.operation_names})
    model, names = _OPS[service]
    report = ParamValidator().validate(params, model.operation_model(names[op]).input_shape)
    assert not report.has_errors(), f"{service}.{op}: {report.generate_report()}"


def err(code: str, message: str = "not found") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, "Fake")


def digest(tag: str) -> str:
    return "sha256:" + hashlib.sha256(tag.encode()).hexdigest()


class FakeAWS:
    """The services a deployment calls, as state machines: runtimes and endpoints settle after a few reads, a build
    finishes after a few polls (pushing its image or writing its bundle)."""

    SERVICES = {"bedrock-agentcore-control": "ctl", "bedrock-agentcore": "data", "s3": "s3", "iam": "iam", "ecr": "ecr", "codebuild": "cb",
                "logs": "logs", "sts": "sts", "ec2": "ec2"}

    def __init__(self, *, bucket: bool = True):
        self.calls: list[tuple[str, str, dict]] = []
        self.runtimes: dict[str, dict] = {}
        self.tags: dict[str, dict] = {}
        self.endpoints: dict[str, dict[str, dict]] = {}
        self.roles: dict[str, dict] = {}
        self.repos: dict[str, dict] = {}
        self.projects: dict[str, dict] = {}
        self.builds: dict[str, dict] = {}
        self.buckets = {BUCKET} if bucket else set()
        self.objects: dict[str, list[dict]] = {}
        self.log_groups: set[str] = set()
        self.build_outcome = "SUCCEEDED"
        self.settle = 2
        self.answer = {"result": "adlc byoc probe: received 'hi'"}
        # the image scan: a pushed image in a repository that scans on push is IN_PROGRESS for scan_after reads
        self.scans: dict[str, dict] = {}
        self.scan_after, self.scan_outcome = 2, "COMPLETE"
        self.scan_counts: dict[str, int] = {}
        self.scan_findings: list[dict] = []
        self.subnets: dict[str, dict] = {}
        self.security_groups: dict[str, dict] = {}
        self.regions: list[str] | None = ["us-east-1", REGION]  # the account's enabled regions (None: not readable)

    def client(self, service: str, region_name=None, **kw):
        return FakeClient(self, service)

    def named(self, service: str, op: str) -> list[dict]:
        return [p for s, o, p in self.calls if s == service and o == op]

    def ops(self, service: str | None = None) -> list[str]:
        return [o for s, o, _ in self.calls if service is None or s == service]

    # -- seeding ---------------------------------------------------------------------------------------------------
    def seed_runtime(self, name: str, *, console: bool = True, source: str = "dockerfile", version: int = 1, **config) -> dict:
        rid = f"{name}-AbCdEf1234"
        arn = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/{rid}"
        self.runtimes[rid] = {"agentRuntimeId": rid, "agentRuntimeArn": arn, "agentRuntimeName": name, "agentRuntimeVersion": str(version), "status": "READY",
                              "roleArn": f"arn:aws:iam::{ACCOUNT}:role/adlc-console-rt-{name}", "networkConfiguration": {"networkMode": "PUBLIC"},
                              "lifecycleConfiguration": {"idleRuntimeSessionTimeout": 900, "maxLifetime": 28800},
                              "protocolConfiguration": {"serverProtocol": "HTTP"}, "polls": 0, "versions": [str(v) for v in range(1, version + 1)], **config}
        self.tags[arn] = {"adlc:console": "1", "adlc:source": source} if console else {}
        self.endpoints[rid] = {"DEFAULT": {"name": "DEFAULT", "liveVersion": str(version), "status": "READY", "polls": 0}}
        return self.runtimes[rid]

    def seed_role(self, name: str, tags: dict, path: str = "/") -> None:
        """A role made before (on ``/``, IAM's default path, unless ``path`` is given)."""
        self.roles[name] = {"Arn": f"arn:aws:iam::{ACCOUNT}:role{path}{name}", "Path": path, "Tags": [{"Key": k, "Value": v} for k, v in tags.items()],
                            "AssumeRolePolicyDocument": {}, "policies": {"runtime-execution": "{}"}}

    def seed_repo(self, name: str, tags: dict) -> None:
        self.repos[name] = {"arn": f"arn:aws:ecr:{REGION}:{ACCOUNT}:repository/{name}", "uri": f"{REGISTRY}/{name}",
                            "tags": [{"Key": k, "Value": v} for k, v in tags.items()], "images": {"v1": digest("v1")}}

    def seed_vpc(self, vpc: str = "vpc-0123456789abcdef0", zones=(("subnet-0aaaaaaaaaaaaaaa1", "usw2-az1"), ("subnet-0bbbbbbbbbbbbbbb2", "usw2-az2")),
                 groups=("sg-0ccccccccccccccc3",)) -> None:
        for sid, zone in zones:
            self.subnets[sid] = {"SubnetId": sid, "VpcId": vpc, "AvailabilityZoneId": zone, "AvailabilityZone": "us-west-2x"}
        for gid in groups:
            self.security_groups[gid] = {"GroupId": gid, "VpcId": vpc, "GroupName": gid}

    def seed_object(self, key: str, versions: int = 1, marker: bool = False) -> None:
        self.objects[key] = [{"VersionId": f"v{i}", "Body": b"x"} for i in range(versions)] + ([{"VersionId": "dm", "marker": True}] if marker else [])

    # -- bedrock-agentcore-control -------------------------------------------------------------------------------
    def _rt(self, rid: str) -> dict:
        if rid not in self.runtimes:
            raise err("ResourceNotFoundException", f"runtime {rid}")
        return self.runtimes[rid]

    def ctl_list_agent_runtimes(self, **p):
        return {"agentRuntimes": [{k: r[k] for k in ("agentRuntimeId", "agentRuntimeArn", "agentRuntimeName", "agentRuntimeVersion", "status")}
                                  for r in self.runtimes.values()]}

    def ctl_get_agent_runtime(self, agentRuntimeId, **p):
        rt = self._rt(agentRuntimeId)
        rt["polls"] += 1
        if rt["status"] in ("CREATING", "UPDATING") and rt["polls"] >= self.settle:
            rt["status"] = "READY"
            self.endpoints[agentRuntimeId]["DEFAULT"].update(liveVersion=rt["agentRuntimeVersion"], status="READY")
        if rt["status"] == "DELETING" and rt["polls"] >= self.settle:
            del self.runtimes[agentRuntimeId]
            raise err("ResourceNotFoundException")
        return {k: v for k, v in rt.items() if k not in ("polls", "versions")}

    def ctl_create_agent_runtime(self, **p):
        rid = f"{p['agentRuntimeName']}-Zx9Yw8Vu7T"
        arn = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/{rid}"
        self.runtimes[rid] = {"agentRuntimeId": rid, "agentRuntimeArn": arn, "agentRuntimeVersion": "1", "status": "CREATING", "polls": 0, "versions": ["1"],
                              "lifecycleConfiguration": {"idleRuntimeSessionTimeout": 900, "maxLifetime": 28800},
                              **{k: v for k, v in p.items() if k != "tags"}}
        self.tags[arn] = dict(p.get("tags") or {})
        self.endpoints[rid] = {"DEFAULT": {"name": "DEFAULT", "liveVersion": None, "targetVersion": "1", "status": "CREATING", "polls": 0}}
        return {"agentRuntimeId": rid, "agentRuntimeArn": arn, "agentRuntimeVersion": "1", "status": "CREATING", "createdAt": "now"}

    def ctl_update_agent_runtime(self, agentRuntimeId, **p):
        rt = self._rt(agentRuntimeId)
        version = str(int(rt["agentRuntimeVersion"]) + 1)
        rt.update(p, agentRuntimeVersion=version, status="UPDATING", polls=0)
        rt["versions"].append(version)
        return {"agentRuntimeId": agentRuntimeId, "agentRuntimeArn": rt["agentRuntimeArn"], "agentRuntimeVersion": version, "status": "UPDATING"}

    def ctl_list_tags_for_resource(self, resourceArn):
        return {"tags": dict(self.tags.get(resourceArn) or {})}

    def ctl_list_agent_runtime_versions(self, agentRuntimeId, **p):
        rt = self._rt(agentRuntimeId)
        return {"agentRuntimes": [{"agentRuntimeVersion": v, "agentRuntimeId": agentRuntimeId, "status": "READY", "lastUpdatedAt": "t"} for v in rt["versions"]]}

    def ctl_list_agent_runtime_endpoints(self, agentRuntimeId, **p):
        return {"runtimeEndpoints": [{k: v for k, v in e.items() if k != "polls"} for e in (self.endpoints.get(agentRuntimeId) or {}).values()]}

    def ctl_get_agent_runtime_endpoint(self, agentRuntimeId, endpointName):
        endpoint = (self.endpoints.get(agentRuntimeId) or {}).get(endpointName)
        if endpoint is None:
            raise err("ResourceNotFoundException", f"endpoint {endpointName}")
        endpoint["polls"] += 1
        if endpoint["status"] in ("CREATING", "UPDATING") and endpoint["polls"] >= self.settle and endpointName != "DEFAULT":
            endpoint.update(status="READY", liveVersion=endpoint["targetVersion"])
        return {k: v for k, v in endpoint.items() if k != "polls"}

    def ctl_create_agent_runtime_endpoint(self, agentRuntimeId, name, agentRuntimeVersion=None, **p):
        self.endpoints[agentRuntimeId][name] = {"name": name, "liveVersion": None, "targetVersion": agentRuntimeVersion, "status": "CREATING", "polls": 0}
        return {"status": "CREATING", "targetVersion": agentRuntimeVersion}

    def ctl_update_agent_runtime_endpoint(self, agentRuntimeId, endpointName, agentRuntimeVersion=None, **p):
        self.endpoints[agentRuntimeId][endpointName].update(targetVersion=agentRuntimeVersion, status="UPDATING", polls=0)
        return {"status": "UPDATING"}

    def ctl_delete_agent_runtime_endpoint(self, agentRuntimeId, endpointName, **p):
        self.endpoints[agentRuntimeId].pop(endpointName)
        return {"status": "DELETING"}

    def ctl_delete_agent_runtime(self, agentRuntimeId, **p):
        self._rt(agentRuntimeId).update(status="DELETING", polls=0)
        return {"status": "DELETING"}

    # -- bedrock-agentcore -------------------------------------------------------------------------------------------
    def data_invoke_agent_runtime(self, **p):
        return {"response": io.BytesIO(json.dumps(self.answer).encode()), "contentType": "application/json", "statusCode": 200,
                "runtimeSessionId": p["runtimeSessionId"]}

    def data_stop_runtime_session(self, **p):
        return {"statusCode": 200}

    # -- s3 ------------------------------------------------------------------------------------------------------------
    def _bucket(self, Bucket):
        if Bucket not in self.buckets:
            raise err("404", "Not Found")

    def s3_head_bucket(self, Bucket, **p):
        self._bucket(Bucket)
        return {}

    def s3_create_bucket(self, Bucket, **p):
        self.buckets.add(Bucket)
        return {}

    s3_put_public_access_block = s3_put_bucket_encryption = s3_put_bucket_versioning = s3_put_bucket_tagging = lambda self, **p: {}

    def s3_put_object(self, Bucket, Key, Body=b"", **p):
        self._bucket(Bucket)
        self.objects.setdefault(Key, []).append({"VersionId": f"v{len(self.objects.get(Key, []))}", "Body": bytes(Body)})
        return {"VersionId": self.objects[Key][-1]["VersionId"]}

    def s3_head_object(self, Bucket, Key, **p):
        if not self.objects.get(Key):
            raise err("404", "Not Found")
        return {"ContentLength": len(self.objects[Key][-1].get("Body") or b"")}

    def s3_list_object_versions(self, Bucket, Prefix="", **p):
        self._bucket(Bucket)
        versions = [{"Key": k, "VersionId": v["VersionId"]} for k, vs in self.objects.items() if k.startswith(Prefix) for v in vs if not v.get("marker")]
        markers = [{"Key": k, "VersionId": v["VersionId"]} for k, vs in self.objects.items() if k.startswith(Prefix) for v in vs if v.get("marker")]
        return {"Versions": versions, "DeleteMarkers": markers, "IsTruncated": False}

    def s3_delete_objects(self, Bucket, Delete, **p):
        for o in Delete["Objects"]:
            self.objects[o["Key"]] = [v for v in self.objects.get(o["Key"], []) if v["VersionId"] != o["VersionId"]]
            if not self.objects[o["Key"]]:
                del self.objects[o["Key"]]
        return {}

    # -- iam -----------------------------------------------------------------------------------------------------------
    def _role(self, RoleName):
        if RoleName not in self.roles:
            raise err("NoSuchEntity", f"role {RoleName}")
        return self.roles[RoleName]

    def iam_get_role(self, RoleName):
        r = self._role(RoleName)
        out = {"RoleName": RoleName, "Path": r.get("Path") or "/", "Arn": r["Arn"], "AssumeRolePolicyDocument": r["AssumeRolePolicyDocument"]}
        if r.get("boundary"):
            out["PermissionsBoundary"] = {"PermissionsBoundaryType": "Policy", "PermissionsBoundaryArn": r["boundary"]}
        return {"Role": out}

    def iam_create_role(self, RoleName, AssumeRolePolicyDocument, Tags=(), PermissionsBoundary=None, Path="/", **p):
        if RoleName in self.roles:  # a role name is unique in its account, whatever the path
            raise err("EntityAlreadyExists", f"Role with name {RoleName} already exists.")
        self.roles[RoleName] = {"Arn": f"arn:aws:iam::{ACCOUNT}:role{Path}{RoleName}", "Path": Path, "Tags": list(Tags),
                                "AssumeRolePolicyDocument": json.loads(AssumeRolePolicyDocument), "policies": {}, "boundary": PermissionsBoundary}
        return {"Role": {"RoleName": RoleName, "Arn": self.roles[RoleName]["Arn"]}}

    def iam_put_role_permissions_boundary(self, RoleName, PermissionsBoundary):
        self._role(RoleName)["boundary"] = PermissionsBoundary
        return {}

    def iam_list_role_tags(self, RoleName):
        return {"Tags": list(self._role(RoleName)["Tags"])}

    def iam_update_assume_role_policy(self, RoleName, PolicyDocument):
        self._role(RoleName)["AssumeRolePolicyDocument"] = json.loads(PolicyDocument)
        return {}

    def iam_put_role_policy(self, RoleName, PolicyName, PolicyDocument):
        self._role(RoleName)["policies"][PolicyName] = PolicyDocument
        return {}

    def iam_list_role_policies(self, RoleName, **p):
        return {"PolicyNames": list(self._role(RoleName)["policies"])}

    def iam_delete_role_policy(self, RoleName, PolicyName):
        self._role(RoleName)["policies"].pop(PolicyName)
        return {}

    def iam_list_attached_role_policies(self, RoleName, **p):
        self._role(RoleName)
        return {"AttachedPolicies": []}

    def iam_delete_role(self, RoleName):
        self._role(RoleName)
        del self.roles[RoleName]
        return {}

    # -- ecr -----------------------------------------------------------------------------------------------------------
    def _repo(self, name):
        if name not in self.repos:
            raise err("RepositoryNotFoundException", f"repository {name}")
        return self.repos[name]

    def ecr_describe_repositories(self, repositoryNames, **p):
        repo = self._repo(repositoryNames[0])
        return {"repositories": [{"repositoryName": repositoryNames[0], "repositoryArn": repo["arn"], "repositoryUri": repo["uri"],
                                  "imageScanningConfiguration": {"scanOnPush": bool(repo.get("scanOnPush"))}}]}

    def ecr_create_repository(self, repositoryName, tags=(), imageScanningConfiguration=None, **p):
        self.repos[repositoryName] = {"arn": f"arn:aws:ecr:{REGION}:{ACCOUNT}:repository/{repositoryName}", "uri": f"{REGISTRY}/{repositoryName}",
                                      "tags": list(tags), "images": {}, "scanOnPush": bool((imageScanningConfiguration or {}).get("scanOnPush"))}
        return {"repository": {"repositoryName": repositoryName, "repositoryArn": self.repos[repositoryName]["arn"],
                               "repositoryUri": self.repos[repositoryName]["uri"]}}

    def ecr_list_tags_for_resource(self, resourceArn):
        return {"tags": next(r["tags"] for r in self.repos.values() if r["arn"] == resourceArn)}

    def ecr_describe_images(self, repositoryName, imageIds, **p):
        repo = self._repo(repositoryName)
        ident = imageIds[0]
        found = repo["images"].get(ident.get("imageTag")) if "imageTag" in ident else (ident["imageDigest"] if ident["imageDigest"] in repo["images"].values() else None)
        if not found:
            raise err("ImageNotFoundException", "no such image")
        return {"imageDetails": [{"imageDigest": found, "imageSizeInBytes": 52_000_000, "imageTags": [t for t, d in repo["images"].items() if d == found]}]}

    def ecr_batch_delete_image(self, repositoryName, imageIds, **p):
        images = self._repo(repositoryName)["images"]
        for ident in imageIds:
            for tag, found in list(images.items()):
                if found == ident.get("imageDigest"):
                    images.pop(tag)
        return {"imageIds": imageIds, "failures": []}

    def ecr_delete_repository(self, repositoryName, **p):
        self._repo(repositoryName)
        del self.repos[repositoryName]
        return {}

    def ecr_put_image_scanning_configuration(self, repositoryName, imageScanningConfiguration, **p):
        self._repo(repositoryName)["scanOnPush"] = bool(imageScanningConfiguration.get("scanOnPush"))
        return {"repositoryName": repositoryName, "imageScanningConfiguration": imageScanningConfiguration}

    def ecr_describe_image_scan_findings(self, repositoryName, imageId, maxResults=None, nextToken=None, **p):
        repo = self._repo(repositoryName)
        digest_ = imageId.get("imageDigest")
        if digest_ not in repo["images"].values():
            raise err("ImageNotFoundException", "no such image")
        scan = self.scans.get(digest_)
        if scan is None:
            if not repo.get("scanOnPush"):
                raise err("ScanNotFoundException", f"Image scan does not exist for image with '{{imageDigest:'{digest_}'}}'")
            scan = self.scans[digest_] = {"polls": 0, "status": "IN_PROGRESS"}
        scan["polls"] += 1
        if scan["status"] == "IN_PROGRESS" and scan["polls"] >= self.scan_after:
            scan["status"] = self.scan_outcome
        out = {"repositoryName": repositoryName, "imageId": {"imageDigest": digest_},
               "imageScanStatus": {"status": scan["status"], "description": "The scan was completed successfully." if scan["status"] == "COMPLETE" else ""}}
        if scan["status"] == "COMPLETE":
            findings = list(self.scan_findings)
            start = int(nextToken or 0)
            size = maxResults or 100
            out["imageScanFindings"] = {"findingSeverityCounts": dict(self.scan_counts), "findings": findings[start:start + size],
                                        "imageScanCompletedAt": "2026-10-02T03:00:00Z", "vulnerabilitySourceUpdatedAt": "2026-10-01T00:00:00Z"}
            if start + size < len(findings):
                out["nextToken"] = str(start + size)
        return out

    # -- ec2 (read-only) -----------------------------------------------------------------------------------------------------
    def ec2_describe_subnets(self, SubnetIds, **p):
        missing = [s for s in SubnetIds if s not in self.subnets]
        if missing:
            raise err("InvalidSubnetID.NotFound", f"The subnet ID '{missing[0]}' does not exist")
        return {"Subnets": [dict(self.subnets[s]) for s in SubnetIds]}

    def ec2_describe_security_groups(self, GroupIds, **p):
        missing = [g for g in GroupIds if g not in self.security_groups]
        if missing:
            raise err("InvalidGroup.NotFound", f"The security group '{missing[0]}' does not exist")
        return {"SecurityGroups": [dict(self.security_groups[g]) for g in GroupIds]}

    def ec2_describe_regions(self, **p):
        if self.regions is None:
            raise err("UnauthorizedOperation", "You are not authorized to perform this operation.")
        return {"Regions": [{"RegionName": r, "Endpoint": f"ec2.{r}.amazonaws.com", "OptInStatus": "opt-in-not-required"} for r in self.regions]}

    # -- codebuild -----------------------------------------------------------------------------------------------------
    def cb_list_projects(self, **p):
        return {"projects": sorted(self.projects)}

    def cb_batch_get_projects(self, names):
        return {"projects": [self.projects[n] for n in names if n in self.projects], "projectsNotFound": [n for n in names if n not in self.projects]}

    def cb_create_project(self, **p):
        self.projects[p["name"]] = dict(p)
        return {"project": dict(p)}

    def cb_update_project(self, **p):
        self.projects[p["name"]].update(p)
        return {"project": self.projects[p["name"]]}

    def cb_delete_project(self, name):
        del self.projects[name]
        return {}

    def cb_start_build(self, **p):
        bid = f"{p['projectName']}:{len(self.builds) + 1:08d}-b0b0-4c4c-8d8d-123456789abc"
        self.builds[bid] = {"id": bid, "buildStatus": "IN_PROGRESS", "currentPhase": "QUEUED", "polls": 0, "request": p,
                            "logs": {"groupName": f"/aws/codebuild/{p['projectName']}", "streamName": bid.split(":")[1]}}
        self.log_groups.add(f"/aws/codebuild/{p['projectName']}")
        return {"build": {k: v for k, v in self.builds[bid].items() if k not in ("polls", "request")}}

    def cb_batch_get_builds(self, ids):
        build = self.builds[ids[0]]
        build["polls"] += 1
        build["currentPhase"] = ["PROVISIONING", "BUILD", "COMPLETED"][min(build["polls"], 3) - 1]
        if build["polls"] >= 3 and build["buildStatus"] == "IN_PROGRESS":
            env = {e["name"]: e["value"] for e in build["request"]["environmentVariablesOverride"]}
            if self.build_outcome == "SUCCEEDED":
                if "IMAGE_URI" in env:
                    repo, tag = env["IMAGE_URI"].split("/", 1)[1].rsplit(":", 1)
                    self.repos[repo]["images"][tag] = digest(tag)
                if "BUNDLE_KEY" in env:
                    self.objects.setdefault(env["BUNDLE_KEY"], []).append({"VersionId": "v0", "Body": b"bundle" * 100})
                build["phases"] = [{"phaseType": t, "phaseStatus": "SUCCEEDED", "durationInSeconds": d}
                                   for t, d in (("SUBMITTED", 0), ("PROVISIONING", 21), ("DOWNLOAD_SOURCE", 2), ("BUILD", 48), ("COMPLETED", 0))]
            else:
                build["phases"] = [{"phaseType": "BUILD", "phaseStatus": "FAILED", "contexts": [
                    {"statusCode": "COMMAND_EXECUTION_ERROR", "message": "Error while executing command: docker build. Reason: exit status 1"}]}]
            build["buildStatus"] = self.build_outcome
        return {"builds": [{k: v for k, v in build.items() if k not in ("polls", "request")}]}

    # -- logs, sts -------------------------------------------------------------------------------------------------------
    def logs_get_log_events(self, **p):
        return {"events": [{"message": "Step 3/5 : RUN pip install -r requirements.txt\n"}, {"message": "ERROR: No matching distribution found for nope\n"}]}

    def logs_delete_log_group(self, logGroupName):
        if logGroupName not in self.log_groups:
            raise err("ResourceNotFoundException")
        self.log_groups.discard(logGroupName)
        return {}

    def sts_get_caller_identity(self, **p):
        return {"Account": ACCOUNT, "Arn": f"arn:aws:iam::{ACCOUNT}:user/sa", "UserId": "AIDA"}


class FakeClient:
    def __init__(self, aws: FakeAWS, service: str):
        self.aws, self.service = aws, service

    def __getattr__(self, op: str):
        handler = getattr(self.aws, f"{FakeAWS.SERVICES[self.service]}_{op}", None)
        if handler is None:
            raise AttributeError(f"{self.service}.{op}")

        def call(**params):
            check_shape(self.service, op, params)
            self.aws.calls.append((self.service, op, params))
            return handler(**params)
        return call


class FakeSession:
    def __init__(self, aws: FakeAWS):
        self.aws = aws

    def client(self, name, region_name=None, **kw):
        return self.aws.client(name, region_name, **kw)


class FakeJob:
    def __init__(self, jid: str = "deploy-0123456789"):
        self.job = {"id": jid}
        self.lines: list[str] = []
        self.progressed: dict = {}
        self.running: list[str] = []

    def log(self, line: str) -> None:
        self.lines.append(line)

    def progress(self, **fields) -> None:
        self.progressed.update(json.loads(json.dumps(fields, default=str)))
        if "stage" in fields:
            self.running.append(fields["stage"])


def zip_of(files: dict[str, bytes | str], *, root: str = "") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(f"{root}{name}", data)
    return buffer.getvalue()


def sample(**extra) -> dict[str, bytes]:
    return {"main.py": (FIXTURE / "main.py").read_bytes(), "Dockerfile": (FIXTURE / "Dockerfile").read_bytes(), **extra}


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def run(aws: FakeAWS, body: dict, *, mode: str = "create", current=None, record=None, names=dp.NAMES, job=None):
    req = dp.deploy_request(body, mode=mode)
    if current:
        req["name"] = current["agentRuntimeName"]
    job = job or FakeJob()
    seen = []
    pipeline = dp.Pipeline(FakeSession(aws), account=ACCOUNT, region=REGION, request=req, job=job, names=names, current=current, record=record,
                           on_runtime=seen.append, sleep=lambda s: None)
    return pipeline, job, seen


def statuses(stages) -> dict[str, str]:
    return {s["name"]: s["status"] for s in stages}


# -- requests --------------------------------------------------------------------------------------------------------------

def test_an_archive_is_checked_and_its_single_folder_lifted():
    finder = {**{f"byoc_agent/{k}": v for k, v in sample().items()}, "byoc_agent/.DS_Store": b"x", "__MACOSX/byoc_agent/._main.py": b"junk"}
    facts = dp.inspect_archive(zip_of(finder), source="zip")  # what macOS's "Compress" makes of a folder
    assert facts["root"] == "byoc_agent/" and facts["rebuilt"] and facts["files"] == 2 and facts["dockerfile"] and not facts["requirements"]
    with zipfile.ZipFile(io.BytesIO(facts["data"])) as rebuilt:
        assert sorted(rebuilt.namelist()) == ["Dockerfile", "main.py"] and rebuilt.read("main.py") == (FIXTURE / "main.py").read_bytes()
    plain = zip_of(sample())
    assert dp.inspect_archive(plain, source="dockerfile")["data"] == plain  # nothing to lift: uploaded as it came
    link = io.BytesIO()
    with zipfile.ZipFile(link, "w") as archive:
        info = zipfile.ZipInfo("main.py")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, "/etc/passwd")
    for bad, source, message in ((zip_of({"../main.py": "x"}), "zip", "unsafe path"), (zip_of({"/abs/main.py": "x"}), "zip", "unsafe path"),
                                 (link.getvalue(), "zip", "symbolic link"), (zip_of({"app.py": "x"}), "zip", "main.py is not in the zip"),
                                 (zip_of({"main.py": "x"}), "dockerfile", "Dockerfile at the zip's root"), (b"not a zip", "zip", "not a zip"),
                                 (b"", "zip", "empty")):
        with pytest.raises(dp.DeployError, match=message):
            dp.inspect_archive(bad, source=source)


def test_requests_are_validated_and_jobs_keep_no_archive_or_secret():
    archive = b64(zip_of(sample()))
    for body, message in (({"source": "zip", "name": "1x", "archive": archive}, "name"), ({"source": "zip", "name": "harness_x", "archive": archive}, "harness_"),
                          ({"source": "git", "name": "ok"}, "source"), ({"source": "image", "name": "ok", "imageUri": f"{REGISTRY}/repo"}, "imageUri"),
                          ({"source": "image", "name": "ok", "imageUri": "docker.io/library/python:3"}, "imageUri"),
                          ({"source": "zip", "name": "ok", "archive": "@@"}, "base64"), ({"source": "zip", "name": "ok", "archive": archive, "protocol": "GRPC"}, "protocol"),
                          ({"source": "zip", "name": "ok", "archive": archive, "environment": {"1X": "v"}}, "variable name"),
                          ({"source": "zip", "name": "ok", "archive": archive, "endpoint": "DEFAULT"}, "endpoint"),
                          ({"source": "zip", "name": "ok", "archive": archive, "entryPoint": "../x.py"}, "entryPoint"),
                          ({"source": "zip", "name": "ok", "archive": archive, "pythonRuntime": "PYTHON_2_7"}, "pythonRuntime")):
        with pytest.raises(dp.DeployError, match=message):
            dp.deploy_request(body)
    created = dp.deploy_request({"source": "zip", "name": "probe", "archive": archive, "environment": {"API_KEY": "s3cret"}})
    assert (created["protocol"], created["environment"], created["endpoint"], created["smoke"], created["install"]) == ("HTTP", {"API_KEY": "s3cret"}, None, True, False)
    updated = dp.deploy_request({"source": "image", "imageUri": f"{REGISTRY}/repo:v2"}, mode="update")
    assert updated["protocol"] is None and updated["environment"] is None and updated["endpoint"] is None  # left out: the runtime keeps its own
    params = dp.public_params(created)
    assert "archive" not in json.dumps(params) and "s3cret" not in json.dumps(params) and params["environment"] == ["API_KEY"]


# -- the three sources ------------------------------------------------------------------------------------------------

def test_a_zip_is_deployed_directly_in_stage_order_with_the_shapes_the_service_takes():
    aws = FakeAWS()
    pipeline, job, seen = run(aws, {"source": "zip", "name": "probe_agent", "archive": b64(zip_of(sample(), root="byoc_agent/")),
                                    "filename": "byoc_agent.zip", "environment": {"LOG_LEVEL": "info"}})
    out = pipeline.run()
    assert job.running == list(dp.STAGES)  # validate → upload → build → runtime → ready → smoke
    assert statuses(out["stages"]) == {"validate": "succeeded", "upload": "succeeded", "build": "skipped", "runtime": "succeeded", "ready": "succeeded",
                                       "smoke": "succeeded"}
    key = "deployments/probe_agent/deploy-0123456789/code.zip"
    [put] = aws.named("s3", "put_object")
    assert (put["Bucket"], put["Key"], put["ExpectedBucketOwner"]) == (BUCKET, key, ACCOUNT)
    with zipfile.ZipFile(io.BytesIO(put["Body"])) as uploaded:
        assert sorted(uploaded.namelist()) == ["Dockerfile", "main.py"]  # the folder lifted: main.py at the root
    assert "codebuild" not in {s for s, _, _ in aws.calls} and "ecr" not in {s for s, _, _ in aws.calls}
    [role] = aws.named("iam", "create_role")
    trust = json.loads(role["AssumeRolePolicyDocument"])["Statement"][0]
    assert role["RoleName"] == "adlc-console-rt-probe_agent" and trust["Principal"] == {"Service": "bedrock-agentcore.amazonaws.com"}
    assert trust["Condition"] == {"StringEquals": {"aws:SourceAccount": ACCOUNT}, "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:*"}}
    assert {t["Key"]: t["Value"] for t in role["Tags"]} == {"adlc:console": "1", "adlc:runtime": "probe_agent", "adlc:region": REGION}
    assert "PermissionsBoundary" not in role and role["Path"] == "/adlc-console/"  # a profile workspace: no boundary; the console's path
    sids = [s["Sid"] for s in json.loads(aws.named("iam", "put_role_policy")[0]["PolicyDocument"])["Statement"]]
    assert sids == ["Models", "LogGroups", "LogGroupList", "LogEvents", "Traces", "Metrics", "WorkloadIdentity"]  # no image to pull
    [create] = aws.named("bedrock-agentcore-control", "create_agent_runtime")
    assert create == {"agentRuntimeName": "probe_agent", "roleArn": f"arn:aws:iam::{ACCOUNT}:role/adlc-console/adlc-console-rt-probe_agent",  # IAM's ARN
                      "agentRuntimeArtifact": {"codeConfiguration": {"code": {"s3": {"bucket": BUCKET, "prefix": key}}, "runtime": "PYTHON_3_13",
                                                                     "entryPoint": ["main.py"]}},
                      "networkConfiguration": {"networkMode": "PUBLIC"}, "protocolConfiguration": {"serverProtocol": "HTTP"},
                      "environmentVariables": {"LOG_LEVEL": "info"}, "description": "ADLC console: zip, job deploy-0123456789",
                      "tags": {"adlc:console": "1", "adlc:source": "zip"}}
    [invoke] = aws.named("bedrock-agentcore", "invoke_agent_runtime")
    assert set(invoke) == {"agentRuntimeArn", "runtimeSessionId", "payload"} and json.loads(invoke["payload"]) == {"prompt": dp.SMOKE_PROMPT}
    assert len(invoke["runtimeSessionId"]) >= 33
    assert aws.named("bedrock-agentcore", "stop_runtime_session") == [{"agentRuntimeArn": invoke["agentRuntimeArn"], "runtimeSessionId": invoke["runtimeSessionId"]}]
    assert out["smoke"]["answer"] == "adlc byoc probe: received 'hi'" and out["version"] == "1" and out["status"] == "READY"
    assert seen[0]["runtimeId"] == out["runtimeId"] and seen[0]["artifact"] == f"s3://{BUCKET}/{key}" and seen[0]["role"] == "adlc-console-rt-probe_agent"
    assert job.progressed["runtimeId"] == out["runtimeId"] and all(s.get("seconds") is not None for s in job.progressed["stages"])


def test_a_zip_with_requirements_is_bundled_for_arm_by_codebuild():
    aws = FakeAWS(bucket=False)
    pipeline, _job, _ = run(aws, {"source": "zip", "name": "deps_agent", "archive": b64(zip_of(sample(**{"requirements.txt": b"bedrock-agentcore==1.17.0\n"}))),
                                  "pythonRuntime": "PYTHON_3_12", "instrument": True})
    out = pipeline.run()
    assert statuses(out["stages"])["build"] == "succeeded"
    assert {"create_bucket", "put_public_access_block", "put_bucket_encryption", "put_bucket_versioning", "put_bucket_tagging"} <= set(aws.ops("s3"))
    assert aws.named("s3", "put_bucket_versioning")[0]["VersioningConfiguration"] == {"Status": "Enabled"}
    prefix = "deployments/deps_agent/deploy-0123456789"
    assert aws.named("s3", "put_object")[0]["Key"] == f"{prefix}/source.zip"
    build_role = next(r for r in aws.named("iam", "create_role") if r["RoleName"] == f"adlc-console-build-{REGION}")  # per region
    assert json.loads(build_role["AssumeRolePolicyDocument"])["Statement"][0]["Condition"] == {"StringEquals": {"aws:SourceAccount": ACCOUNT}}
    [project] = aws.named("codebuild", "create_project")
    assert project["environment"] == {"type": "ARM_CONTAINER", "image": dp.BUILD_IMAGE, "computeType": "BUILD_GENERAL1_SMALL", "privilegedMode": True}
    assert project["name"] == "adlc-console-build" and project["tags"] == [{"key": "adlc:console", "value": "1"}] and project["artifacts"] == {"type": "NO_ARTIFACTS"}
    [start] = aws.named("codebuild", "start_build")
    assert start["sourceTypeOverride"] == "S3" and start["sourceLocationOverride"] == f"{BUCKET}/{prefix}/source.zip"
    assert start["buildspecOverride"] == dp.BUNDLE_BUILDSPEC and start["idempotencyToken"] == "deploy-0123456789-bundle"
    assert {e["name"]: e["value"] for e in start["environmentVariablesOverride"]} == {"PYTHON_VERSION": "3.12", "BUNDLE_BUCKET": BUCKET,
                                                                                    "BUNDLE_KEY": f"{prefix}/bundle.zip"}
    code = aws.named("bedrock-agentcore-control", "create_agent_runtime")[0]["agentRuntimeArtifact"]["codeConfiguration"]
    assert code == {"code": {"s3": {"bucket": BUCKET, "prefix": f"{prefix}/bundle.zip"}}, "runtime": "PYTHON_3_12",
                    "entryPoint": ["opentelemetry-instrument", "main.py"]}  # instrument: the ADOT launcher the bundle installed
    policy = json.loads(next(p for p in aws.named("iam", "put_role_policy") if p["RoleName"] == f"adlc-console-build-{REGION}")["PolicyDocument"])
    grants = {s["Sid"]: s["Resource"] for s in policy["Statement"]}
    assert grants["Sources"] == f"arn:aws:s3:::{BUCKET}/deployments/*" and grants["ImagePush"] == f"arn:aws:ecr:{REGION}:{ACCOUNT}:repository/adlc-console/*"


def test_the_buildspecs_are_yaml_codebuild_reads():
    image, bundle = yaml.safe_load(dp.IMAGE_BUILDSPEC), yaml.safe_load(dp.BUNDLE_BUILDSPEC)
    assert image["version"] == 0.2 and image["phases"]["build"]["commands"] == ['docker build --platform linux/arm64 --tag "$IMAGE_URI" .', 'docker push "$IMAGE_URI"']
    assert "docker login" in image["phases"]["pre_build"]["commands"][0] and "post_build" not in image["phases"]
    commands = bundle["phases"]["build"]["commands"]
    assert all(isinstance(c, str) for c in commands)  # an unquoted ": " (as in --only-binary=:all: --platform) would make a command a mapping
    assert len(commands) == 4 and "--only-binary=:all:" in commands[0] and "--platform manylinux_2_28_aarch64" in commands[0] and "--target ." in commands[0]
    assert bundle["env"] == {"shell": "bash"} and "/tmp/bundle.zip" in commands[2] and commands[3].startswith("aws s3 cp /tmp/bundle.zip")


def test_a_dockerfile_is_built_into_its_own_repository_and_deployed_by_digest_behind_a_named_endpoint():
    aws = FakeAWS()
    names = dp.Names("adlc-probe")
    pipeline, _job, seen = run(aws, {"source": "dockerfile", "name": "adlc_probe_byoc", "archive": b64(zip_of(sample())), "endpoint": "live"}, names=names)
    out = pipeline.run()
    assert statuses(out["stages"]) == {s: "succeeded" for s in dp.stages_for("dockerfile")}  # the scan stage: a clean image
    [repo] = aws.named("ecr", "create_repository")
    assert repo["repositoryName"] == "adlc-probe/adlc-probe-byoc" and {t["Key"]: t["Value"] for t in repo["tags"]} == {"adlc:console": "1",
                                                                                                                    "adlc:runtime": "adlc_probe_byoc"}
    [start] = aws.named("codebuild", "start_build")
    assert start["projectName"] == "adlc-probe-build" and start["buildspecOverride"] == dp.IMAGE_BUILDSPEC
    assert start["sourceLocationOverride"] == f"{BUCKET}/deployments/adlc_probe_byoc/deploy-0123456789/context.zip"
    assert {e["name"]: e["value"] for e in start["environmentVariablesOverride"]} == {"AWS_REGION": REGION, "ECR_REGISTRY": REGISTRY,
                                                                                    "IMAGE_URI": f"{REGISTRY}/adlc-probe/adlc-probe-byoc:deploy-0123456789"}
    assert aws.named("ecr", "describe_images")[-1] == {"repositoryName": "adlc-probe/adlc-probe-byoc", "imageIds": [{"imageTag": "deploy-0123456789"}]}
    uri = f"{REGISTRY}/adlc-probe/adlc-probe-byoc@{digest('deploy-0123456789')}"
    [create] = aws.named("bedrock-agentcore-control", "create_agent_runtime")
    assert create["agentRuntimeArtifact"] == {"containerConfiguration": {"containerUri": uri}} and "environmentVariables" not in create
    assert create["roleArn"].endswith(":role/adlc-console/adlc-probe-rt-adlc_probe_byoc")  # the role's own ARN, with its path
    policy = json.loads(next(p for p in aws.named("iam", "put_role_policy") if p["RoleName"] == "adlc-probe-rt-adlc_probe_byoc")["PolicyDocument"])
    pull = next(s for s in policy["Statement"] if s["Sid"] == "ImagePull")
    assert pull["Resource"] == f"arn:aws:ecr:{REGION}:{ACCOUNT}:repository/adlc-probe/adlc-probe-byoc"  # its own repository only
    [endpoint] = aws.named("bedrock-agentcore-control", "create_agent_runtime_endpoint")
    assert endpoint == {"agentRuntimeId": out["runtimeId"], "name": "live", "agentRuntimeVersion": "1", "description": "ADLC console, job deploy-0123456789",
                        "tags": {"adlc:console": "1"}}
    [invoke] = aws.named("bedrock-agentcore", "invoke_agent_runtime")
    assert invoke["qualifier"] == "live" and aws.named("bedrock-agentcore", "stop_runtime_session")[0]["qualifier"] == "live"
    assert out["image"] == uri and out["endpoint"] == "live" and seen[0]["repository"] == "adlc-probe/adlc-probe-byoc" and seen[0]["endpoint"] == "live"


def test_an_existing_image_is_pinned_to_its_digest_without_upload_or_build():
    aws = FakeAWS()
    aws.seed_repo("team/agent", {})
    pipeline, _job, _ = run(aws, {"source": "image", "name": "team_agent", "imageUri": f"{REGISTRY}/team/agent:v1"})
    out = pipeline.run()
    assert statuses(out["stages"])["upload"] == "skipped" and statuses(out["stages"])["build"] == "skipped"
    assert not aws.named("s3", "put_object") and not aws.named("codebuild", "start_build") and not aws.named("ecr", "create_repository")
    create = aws.named("bedrock-agentcore-control", "create_agent_runtime")[0]
    assert create["agentRuntimeArtifact"] == {"containerConfiguration": {"containerUri": f"{REGISTRY}/team/agent@{digest('v1')}"}}
    policy = json.loads(aws.named("iam", "put_role_policy")[0]["PolicyDocument"])
    assert next(s for s in policy["Statement"] if s["Sid"] == "ImagePull")["Resource"] == f"arn:aws:ecr:{REGION}:{ACCOUNT}:repository/team/agent"
    elsewhere = FakeAWS()
    pipeline, job, _ = run(elsewhere, {"source": "image", "name": "team_agent", "imageUri": f"999999999999.dkr.ecr.{REGION}.amazonaws.com/team/agent:v1"})
    with pytest.raises(dp.DeployError, match="999999999999"):
        pipeline.run()
    assert statuses(pipeline.stages) == {"validate": "failed", "upload": "pending", "build": "pending", "scan": "pending", "runtime": "pending", "ready": "pending",
                                         "smoke": "pending"}
    assert not elsewhere.named("iam", "create_role") and not elsewhere.named("bedrock-agentcore-control", "create_agent_runtime")
    taken = FakeAWS()
    taken.seed_runtime("team_agent")
    pipeline, _job, _ = run(taken, {"source": "image", "name": "team_agent", "imageUri": f"{REGISTRY}/team/agent:v1"})
    with pytest.raises(dp.DeployError, match="publish a new version"):
        pipeline.run()


def test_a_new_version_carries_over_what_the_request_leaves_out_and_moves_the_endpoint():
    aws = FakeAWS()
    current = aws.seed_runtime("team_agent", source="image", environmentVariables={"API_KEY": "kept"}, metadataConfiguration={"requireMMDSV2": True},
                               lifecycleConfiguration={"idleRuntimeSessionTimeout": 300, "maxLifetime": 3600})
    aws.endpoints[current["agentRuntimeId"]]["live"] = {"name": "live", "liveVersion": "1", "status": "READY", "polls": 0}
    aws.seed_repo("team/agent", {})
    aws.repos["team/agent"]["images"]["v2"] = digest("v2")
    aws.seed_role("adlc-console-rt-team_agent", {"adlc:console": "1", "adlc:runtime": "team_agent"})
    snapshot = aws.ctl_get_agent_runtime(current["agentRuntimeId"])
    pipeline, job, seen = run(aws, {"source": "image", "imageUri": f"{REGISTRY}/team/agent:v2"}, mode="update", current=snapshot, record={"endpoint": "live"})
    out = pipeline.run()
    [update] = aws.named("bedrock-agentcore-control", "update_agent_runtime")
    assert update == {"agentRuntimeId": current["agentRuntimeId"], "roleArn": f"arn:aws:iam::{ACCOUNT}:role/adlc-console-rt-team_agent",
                      "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": f"{REGISTRY}/team/agent@{digest('v2')}"}},
                      "networkConfiguration": {"networkMode": "PUBLIC"}, "protocolConfiguration": {"serverProtocol": "HTTP"},
                      "environmentVariables": {"API_KEY": "kept"}, "lifecycleConfiguration": {"idleRuntimeSessionTimeout": 300, "maxLifetime": 3600},
                      "metadataConfiguration": {"requireMMDSV2": True}, "description": "ADLC console: image, job deploy-0123456789"}
    assert not aws.named("iam", "create_role") and not aws.named("bedrock-agentcore-control", "create_agent_runtime")  # same runtime, same role
    assert aws.named("bedrock-agentcore-control", "update_agent_runtime_endpoint") == [{"agentRuntimeId": current["agentRuntimeId"], "endpointName": "live",
                                                                                         "agentRuntimeVersion": "2"}]
    assert out["version"] == "2" and aws.named("bedrock-agentcore", "invoke_agent_runtime")[0]["qualifier"] == "live" and seen[0]["version"] == "2"
    mcp = FakeAWS()
    current = mcp.seed_runtime("tools_server", source="image")
    mcp.seed_repo("team/agent", {})
    mcp.seed_role("adlc-console-rt-tools_server", {"adlc:console": "1", "adlc:runtime": "tools_server"})
    pipeline, _job, _ = run(mcp, {"source": "image", "imageUri": f"{REGISTRY}/team/agent:v1", "protocol": "MCP", "environment": {}, "endpoint": ""},
                            mode="update", current=mcp.ctl_get_agent_runtime(current["agentRuntimeId"]))
    out = pipeline.run()
    update = mcp.named("bedrock-agentcore-control", "update_agent_runtime")[0]
    assert update["protocolConfiguration"] == {"serverProtocol": "MCP"} and update["environmentVariables"] == {}
    assert statuses(out["stages"])["smoke"] == "skipped" and not mcp.named("bedrock-agentcore", "invoke_agent_runtime")  # the HTTP contract only
    foreign = FakeAWS()
    other = foreign.seed_runtime("team_agent", source="image")
    foreign.seed_role("adlc-console-rt-team_agent", {"owner": "someone"})
    pipeline, _job, _ = run(foreign, {"source": "image", "imageUri": f"{REGISTRY}/team/agent:v1"}, mode="update",
                            current=foreign.ctl_get_agent_runtime(other["agentRuntimeId"]))
    foreign.seed_repo("team/agent", {})
    with pytest.raises(dp.DeployError, match="not the console's"):
        pipeline.run()  # a role the console did not make is never adopted


def test_a_failed_build_shows_its_log_and_a_first_deployment_takes_back_what_it_made():
    aws = FakeAWS()
    aws.build_outcome = "FAILED"
    pipeline, job, seen = run(aws, {"source": "dockerfile", "name": "broken_agent", "archive": b64(zip_of(sample()))})
    with pytest.raises(dp.DeployError, match="COMMAND_EXECUTION_ERROR|docker build"):
        pipeline.run()
    assert statuses(pipeline.stages) == {"validate": "succeeded", "upload": "succeeded", "build": "failed", "scan": "pending", "runtime": "pending",
                                         "ready": "pending", "smoke": "pending"}
    assert any("No matching distribution" in line for line in job.lines)  # the build log's tail is in the job's log
    assert not aws.named("bedrock-agentcore-control", "create_agent_runtime") and not seen
    assert aws.named("ecr", "delete_repository") == [{"repositoryName": "adlc-console/broken-agent", "force": True}]
    assert not [k for k in aws.objects if k.startswith("deployments/broken_agent/")] and any("took back" in line for line in job.lines)
    assert "adlc-console-build" in aws.projects  # the shared build project stays


# -- the image scan gate --------------------------------------------------------------------------------------------------

def finding(cve: str, severity: str, package: str = "perl", version: str = "5.36.0-7+deb12u3") -> dict:
    return {"name": cve, "severity": severity, "uri": f"https://security-tracker.debian.org/tracker/{cve}",
            "attributes": [{"key": "package_name", "value": package}, {"key": "package_version", "value": version}]}


def test_a_dockerfile_image_scans_on_push_and_its_counts_are_on_the_job():
    aws = FakeAWS()
    aws.scan_counts = {"HIGH": 3, "MEDIUM": 1}
    pipeline, job, seen = run(aws, {"source": "dockerfile", "name": "clean_agent", "archive": b64(zip_of(sample()))})
    out = pipeline.run()
    assert job.running == list(dp.IMAGE_STAGES) and statuses(out["stages"])["scan"] == "succeeded"
    [repo] = aws.named("ecr", "create_repository")
    assert repo["imageScanningConfiguration"] == {"scanOnPush": True}
    reads = aws.named("ecr", "describe_image_scan_findings")
    assert len(reads) == aws.scan_after and reads[0] == {"repositoryName": "adlc-console/clean-agent", "imageId": {"imageDigest": digest("deploy-0123456789")},
                                                         "maxResults": 1000}
    scan = job.progressed["scan"]
    assert (scan["status"], scan["counts"], scan["blocking"], scan["block"]) == ("COMPLETE", {"HIGH": 3, "MEDIUM": 1}, {}, ["CRITICAL"])
    assert out["scan"] == scan and seen[0]["scan"] == {"status": "COMPLETE", "counts": {"HIGH": 3, "MEDIUM": 1}, "block": ["CRITICAL"]}
    assert "HIGH 3, MEDIUM 1 · gate blocks CRITICAL: passed" in out["stages"][3]["detail"]
    assert aws.calls.index(next(c for c in aws.calls if c[1] == "describe_image_scan_findings")) < aws.calls.index(
        next(c for c in aws.calls if c[1] == "create_agent_runtime"))  # gated before the runtime exists


def test_a_finding_at_a_blocking_severity_stops_the_deploy_listing_up_to_eight_and_taking_back_the_repository():
    aws = FakeAWS()
    aws.scan_counts = {"CRITICAL": 10, "HIGH": 2}
    aws.scan_findings = [finding(f"CVE-2026-{i:05d}", "HIGH" if i < 2 else "CRITICAL") for i in range(12)]
    pipeline, job, seen = run(aws, {"source": "dockerfile", "name": "risky_agent", "archive": b64(zip_of(sample()))})
    with pytest.raises(dp.DeployError, match=r"CRITICAL 10 finding\(s\) at a blocking severity \(CRITICAL\)") as caught:
        pipeline.run()
    listed = job.progressed["scan"]["findings"]
    assert len(listed) == dp.MAX_LISTED_FINDINGS and listed[0] == "CVE-2026-00002 (perl 5.36.0-7+deb12u3, CRITICAL)" and all("CRITICAL" in f for f in listed)
    assert listed[0] in str(caught.value) and "scanBlock" in str(caught.value)
    assert statuses(pipeline.stages)["scan"] == "failed" and statuses(pipeline.stages)["runtime"] == "pending"
    assert job.progressed["scan"]["blocking"] == {"CRITICAL": 10} and not seen and not aws.named("bedrock-agentcore-control", "create_agent_runtime")
    assert aws.named("ecr", "delete_repository") == [{"repositoryName": "adlc-console/risky-agent", "force": True}] and "adlc-console/risky-agent" not in aws.repos
    assert not [r for r in aws.named("iam", "create_role") if r["RoleName"].startswith("adlc-console-rt-")]  # the runtime role was never made
    # the severities are the deploy's: [] only reports; CRITICAL and HIGH blocks on HIGH alone
    lenient = FakeAWS()
    lenient.scan_counts, lenient.scan_findings = dict(aws.scan_counts), list(aws.scan_findings)
    pipeline, job, _ = run(lenient, {"source": "dockerfile", "name": "risky_agent", "archive": b64(zip_of(sample())), "scanBlock": []})
    out = pipeline.run()
    assert out["scan"]["block"] == [] and out["scan"]["blocking"] == {} and "gate reports only: passed" in out["stages"][3]["detail"]
    strict = FakeAWS()
    strict.scan_counts, strict.scan_findings = {"HIGH": 1}, [finding("CVE-2026-11111", "HIGH", "openssl", "3.0.15-1~deb12u1")]
    pipeline, job, _ = run(strict, {"source": "dockerfile", "name": "risky_agent", "archive": b64(zip_of(sample())), "scanBlock": "critical, high"})
    with pytest.raises(dp.DeployError, match=r"HIGH 1 .*CVE-2026-11111 \(openssl 3.0.15-1~deb12u1, HIGH\)"):
        pipeline.run()
    for bad in (["SEVERE"], {"CRITICAL": True}):
        with pytest.raises(dp.DeployError, match="scanBlock"):
            dp.deploy_request({"source": "dockerfile", "name": "x", "archive": b64(zip_of(sample())), "scanBlock": bad})


def test_an_image_that_cannot_be_scanned_is_deployed_as_unscanned_and_said_so():
    aws = FakeAWS()
    aws.seed_repo("team/agent", {})  # a repository that does not scan on push, the image never scanned
    pipeline, job, _ = run(aws, {"source": "image", "name": "team_agent", "imageUri": f"{REGISTRY}/team/agent:v1"})
    out = pipeline.run()
    assert statuses(out["stages"])["scan"] == "skipped" and "deploying unscanned" in out["stages"][3]["detail"]
    assert out["scan"]["status"] == "UNSCANNED" and "never scanned" in out["scan"]["reason"] and aws.named("bedrock-agentcore-control", "create_agent_runtime")
    assert len(aws.named("ecr", "describe_image_scan_findings")) == 1  # no push scan to wait for
    slow = FakeAWS()  # a push scan that does not finish in time
    slow.scan_after = 10_000
    pipeline, job, _ = run(slow, {"source": "dockerfile", "name": "slow_agent", "archive": b64(zip_of(sample()))})
    ticks = iter(range(0, 10_000, 30))
    pipeline.clock = lambda: float(next(ticks))
    pipeline.SCAN_TIMEOUT = 120.0
    out = pipeline.run()
    assert statuses(out["stages"])["scan"] == "skipped" and "still IN_PROGRESS after 120 s" in out["scan"]["reason"]
    old = FakeAWS()  # a console repository from before the gate: it scans on push from now on
    old.repos["adlc-console/old-agent"] = {"arn": f"arn:aws:ecr:{REGION}:{ACCOUNT}:repository/adlc-console/old-agent", "uri": f"{REGISTRY}/adlc-console/old-agent",
                                           "tags": [{"Key": "adlc:console", "Value": "1"}, {"Key": "adlc:runtime", "Value": "old_agent"}], "images": {}, "scanOnPush": False}
    pipeline, job, _ = run(old, {"source": "dockerfile", "name": "old_agent", "archive": b64(zip_of(sample()))})
    out = pipeline.run()
    assert old.named("ecr", "put_image_scanning_configuration") == [{"repositoryName": "adlc-console/old-agent", "imageScanningConfiguration": {"scanOnPush": True}}]
    assert out["scan"]["status"] == "COMPLETE" and not old.named("ecr", "create_repository")


def test_ecr_scan_counts_read_heaviest_first():
    assert dp.format_counts({"LOW": 4, "CRITICAL": 4, "HIGH": 13, "MEDIUM": 10, "UNDEFINED": 0}) == "CRITICAL 4, HIGH 13, MEDIUM 10, LOW 4"
    assert dp.format_counts({}) == "no findings" and dp.stages_for("zip") == dp.STAGES and dp.stages_for("image") == dp.IMAGE_STAGES


# -- runtime options: session storage, lifecycle, VPC ----------------------------------------------------------------------

def test_runtime_options_are_checked_by_shape():
    archive = b64(zip_of(sample()))
    base = {"source": "zip", "name": "opts_agent", "archive": archive}
    req = dp.deploy_request({**base, "sessionStorage": True, "lifecycle": {"idleRuntimeSessionTimeout": "600", "maxLifetime": 7200},
                             "network": {"mode": "vpc", "subnets": "subnet-0aaaaaaaaaaaaaaa1, subnet-0bbbbbbbbbbbbbbb2 subnet-0aaaaaaaaaaaaaaa1",
                                         "securityGroups": ["sg-0ccccccccccccccc3"]}})
    assert req["filesystems"] == [{"sessionStorage": {"mountPath": "/mnt/workspace"}}] and req["lifecycle"] == {"idleRuntimeSessionTimeout": 600, "maxLifetime": 7200}
    assert req["network"] == {"networkMode": "VPC", "networkModeConfig": {"subnets": ["subnet-0aaaaaaaaaaaaaaa1", "subnet-0bbbbbbbbbbbbbbb2"],
                                                                         "securityGroups": ["sg-0ccccccccccccccc3"]}}
    params = dp.public_params(req)
    assert params["sessionStorage"] == "/mnt/workspace" and params["lifecycle"]["maxLifetime"] == 7200 and params["network"]["mode"] == "VPC"
    assert dp.deploy_request({**base, "sessionStorage": "/mnt/data/"})["filesystems"] == [{"sessionStorage": {"mountPath": "/mnt/data/"}}]
    assert dp.deploy_request({**base, "sessionStorage": False})["filesystems"] == [] and dp.deploy_request(base)["filesystems"] is None
    assert dp.deploy_request({**base, "network": "PUBLIC"})["network"] == {"networkMode": "PUBLIC"} and dp.deploy_request(base)["network"] is None
    for body, message in (({"sessionStorage": "/tmp/x"}, "one directory under /mnt"), ({"sessionStorage": "/mnt/a/b"}, "one directory under /mnt"),
                          ({"sessionStorage": 5}, "sessionStorage"), ({"lifecycle": {"idleRuntimeSessionTimeout": 30}}, "60 to 28800"),
                          ({"lifecycle": {"maxLifetime": 1209600}}, "60 to 28800"), ({"lifecycle": {"idleRuntimeSessionTimeout": 900, "maxLifetime": 600}}, "must not exceed"),
                          ({"lifecycle": {"idle": 600}}, "only idleRuntimeSessionTimeout"), ({"lifecycle": {"maxLifetime": 600.5}}, "60 to 28800"),
                          ({"network": {"mode": "VPC", "subnets": [], "securityGroups": ["sg-0ccccccccccccccc3"]}}, "1 to 16 subnet"),
                          ({"network": {"mode": "VPC", "subnets": ["subnet-1"], "securityGroups": ["sg-0ccccccccccccccc3"]}}, "not a subnet id"),
                          ({"network": {"mode": "VPC", "subnets": ["subnet-0aaaaaaaaaaaaaaa1"], "securityGroups": ["group-1"]}}, "not a security group id"),
                          ({"network": {"networkMode": "VPC", "networkModeConfig": {"subnets": ["subnet-0aaaaaaaaaaaaaaa1"], "securityGroups": ["sg-0ccccccccccccccc3"],
                                                                                    "requireServiceS3Endpoint": False}}}, "requireServiceS3Endpoint"),
                          ({"network": "PRIVATE"}, "PUBLIC or VPC")):
        with pytest.raises(dp.DeployError, match=message):
            dp.deploy_request({**base, **body})


def test_session_storage_lifecycle_and_a_checked_vpc_reach_create_agent_runtime():
    aws = FakeAWS()
    aws.seed_vpc()
    vpc = {"mode": "VPC", "subnets": ["subnet-0aaaaaaaaaaaaaaa1", "subnet-0bbbbbbbbbbbbbbb2"], "securityGroups": ["sg-0ccccccccccccccc3"]}
    pipeline, job, _ = run(aws, {"source": "zip", "name": "opts_agent", "archive": b64(zip_of(sample())), "sessionStorage": {"mountPath": "/mnt/workspace"},
                                 "lifecycle": {"idleRuntimeSessionTimeout": 600, "maxLifetime": 7200}, "network": vpc})
    out = pipeline.run()
    assert aws.named("ec2", "describe_subnets") == [{"SubnetIds": vpc["subnets"]}] and aws.named("ec2", "describe_security_groups") == [{"GroupIds": vpc["securityGroups"]}]
    assert "VPC vpc-0123456789abcdef0: subnets subnet-0aaaaaaaaaaaaaaa1@usw2-az1, subnet-0bbbbbbbbbbbbbbb2@usw2-az2" in out["stages"][0]["detail"]
    [create] = aws.named("bedrock-agentcore-control", "create_agent_runtime")  # botocore-validated by the fake client
    assert create["filesystemConfigurations"] == [{"sessionStorage": {"mountPath": "/mnt/workspace"}}]
    assert create["lifecycleConfiguration"] == {"idleRuntimeSessionTimeout": 600, "maxLifetime": 7200}
    assert create["networkConfiguration"] == {"networkMode": "VPC", "networkModeConfig": {"subnets": vpc["subnets"], "securityGroups": vpc["securityGroups"]}}
    assert out["options"] == {"network": "VPC", "sessionStorage": "/mnt/workspace", "lifecycle": {"idleRuntimeSessionTimeout": 600, "maxLifetime": 7200}}
    assert "session storage /mnt/workspace · idle 600 s, max lifetime 7200 s · VPC" in out["stages"][3]["detail"]
    # a new version: what it names replaces the runtime's own, what it leaves out is kept; sessionStorage false removes it
    current = aws.ctl_get_agent_runtime(out["runtimeId"])
    pipeline, job, _ = run(aws, {"source": "zip", "archive": b64(zip_of(sample())), "lifecycle": {"maxLifetime": 3600}, "sessionStorage": False},
                           mode="update", current=current)
    pipeline.run()
    [update] = aws.named("bedrock-agentcore-control", "update_agent_runtime")
    assert update["lifecycleConfiguration"] == {"maxLifetime": 3600} and "filesystemConfigurations" not in update
    assert update["networkConfiguration"] == create["networkConfiguration"]  # kept


def test_a_vpc_that_does_not_check_out_is_refused_before_anything_is_made():
    vpc = {"mode": "VPC", "subnets": ["subnet-0aaaaaaaaaaaaaaa1", "subnet-0bbbbbbbbbbbbbbb2"], "securityGroups": ["sg-0ccccccccccccccc3"]}
    cases = (
        (lambda aws: aws.seed_vpc(zones=(("subnet-0aaaaaaaaaaaaaaa1", "usw2-az1"),)), "does not exist in"),
        (lambda aws: (aws.seed_vpc(), aws.subnets["subnet-0bbbbbbbbbbbbbbb2"].update(VpcId="vpc-0fffffffffffffff9")), "several VPCs"),
        (lambda aws: (aws.seed_vpc(), aws.security_groups["sg-0ccccccccccccccc3"].update(VpcId="vpc-0fffffffffffffff9")), "not in the subnets' VPC"),
        (lambda aws: aws.seed_vpc(zones=(("subnet-0aaaaaaaaaaaaaaa1", "usw2-az1"), ("subnet-0bbbbbbbbbbbbbbb2", "usw2-az4"))), "supports usw2-az1, usw2-az2, usw2-az3"),
        (lambda aws: aws.seed_vpc(groups=()), "does not exist in"),
    )
    for seed, message in cases:
        aws = FakeAWS()
        seed(aws)
        pipeline, job, _ = run(aws, {"source": "dockerfile", "name": "vpc_agent", "archive": b64(zip_of(sample())), "network": vpc})
        with pytest.raises(dp.DeployError, match=message):
            pipeline.run()
        assert statuses(pipeline.stages)["validate"] == "failed" and not aws.named("s3", "put_object") and not aws.named("iam", "create_role")
        assert not aws.named("codebuild", "start_build") and not aws.named("ecr", "create_repository")


# -- delete -----------------------------------------------------------------------------------------------------------------

def teardown(aws: FakeAWS, runtime_id: str, *, name: str, keep: bool = False, record=None):
    runtime = aws.ctl_get_agent_runtime(runtime_id) if runtime_id in aws.runtimes else None
    job = FakeJob("undeploy-0123456789")
    return dp.Teardown(FakeSession(aws), account=ACCOUNT, region=REGION, runtime_id=runtime_id, name=name, runtime=runtime, job=job, keep_repository=keep,
                       record=record, sleep=lambda s: None), job


def test_only_console_runtimes_are_changed_or_deleted():
    aws = FakeAWS()
    theirs = aws.seed_runtime("someone_elses", console=False)
    harness = aws.seed_runtime("harness_hr_agent")
    for runtime in (theirs, harness):
        with pytest.raises(dp.DeployError, match="adlc:console=1"):
            dp._console_runtime(aws.client("bedrock-agentcore-control"), runtime["agentRuntimeId"])
    down, _job = teardown(aws, theirs["agentRuntimeId"], name="someone_elses")
    with pytest.raises(dp.DeployError, match="adlc:console=1"):
        down.run()
    assert statuses(down.stages)["check"] == "failed" and not [o for o in aws.ops() if o.startswith("delete")]


def test_deleting_a_console_runtime_removes_its_endpoints_runtime_repository_role_and_sources_in_order():
    aws = FakeAWS()
    rt = aws.seed_runtime("adlc_probe_byoc")
    rid = rt["agentRuntimeId"]
    aws.endpoints[rid]["live"] = {"name": "live", "liveVersion": "1", "status": "READY", "polls": 0}
    aws.seed_repo("adlc-console/adlc-probe-byoc", {"adlc:console": "1", "adlc:runtime": "adlc_probe_byoc"})
    aws.seed_role("adlc-console-rt-adlc_probe_byoc", {"adlc:console": "1", "adlc:runtime": "adlc_probe_byoc"})
    aws.seed_object("deployments/adlc_probe_byoc/deploy-1/context.zip", versions=2, marker=True)
    aws.seed_object("deployments/adlc_probe_byoc_two/deploy-2/context.zip")  # another runtime's prefix: untouched
    down, job = teardown(aws, rid, name="adlc_probe_byoc")
    out = down.run()
    assert job.running == list(dp.DELETE_STAGES) and statuses(out["stages"]) == {s: "succeeded" for s in dp.DELETE_STAGES}
    control = aws.ops("bedrock-agentcore-control")
    assert control.index("delete_agent_runtime_endpoint") < control.index("delete_agent_runtime")
    assert rid not in aws.runtimes and "adlc-console/adlc-probe-byoc" not in aws.repos and "adlc-console-rt-adlc_probe_byoc" not in aws.roles
    assert aws.named("ecr", "delete_repository") == [{"repositoryName": "adlc-console/adlc-probe-byoc", "force": True}]
    assert sorted(o["VersionId"] for o in aws.named("s3", "delete_objects")[0]["Delete"]["Objects"]) == ["dm", "v0", "v1"]
    assert list(aws.objects) == ["deployments/adlc_probe_byoc_two/deploy-2/context.zip"]
    assert out["deleted"] == {"endpoints": ["live"], "runtime": rid, "repository": "adlc-console/adlc-probe-byoc", "role": "adlc-console-rt-adlc_probe_byoc",
                              "objects": 3}


def test_a_kept_or_foreign_repository_and_a_foreign_role_are_left_alone():
    aws = FakeAWS()
    rid = aws.seed_runtime("team_agent", source="image")["agentRuntimeId"]
    aws.seed_repo("adlc-console/team-agent", {"adlc:console": "1", "adlc:runtime": "Team_Agent"})  # another runtime's repository
    aws.seed_role("adlc-console-rt-team_agent", {"owner": "platform-team"})
    down, _job = teardown(aws, rid, name="team_agent")
    out = down.run()
    assert statuses(out["stages"])["repository"] == "skipped" and statuses(out["stages"])["role"] == "skipped"
    assert "adlc-console/team-agent" in aws.repos and "adlc-console-rt-team_agent" in aws.roles and not aws.named("iam", "delete_role")
    kept = FakeAWS()
    rid = kept.seed_runtime("adlc_probe_byoc")["agentRuntimeId"]
    kept.seed_repo("adlc-console/adlc-probe-byoc", {"adlc:console": "1", "adlc:runtime": "adlc_probe_byoc"})
    down, _job = teardown(kept, rid, name="adlc_probe_byoc", keep=True)
    out = down.run()
    assert out["stages"][3]["detail"] == "adlc-console/adlc-probe-byoc kept" and "adlc-console/adlc-probe-byoc" in kept.repos
    gone = FakeAWS()  # deleted outside the console: what the console made for it still goes
    gone.seed_role("adlc-console-rt-lost_agent", {"adlc:console": "1", "adlc:runtime": "lost_agent"})
    down, _job = teardown(gone, "lost_agent-AbCdEf1234", name="lost_agent", record={"name": "lost_agent", "source": "zip"})
    out = down.run()
    assert statuses(out["stages"])["runtime"] == "skipped" and "adlc-console-rt-lost_agent" not in gone.roles


def test_a_poll_survives_a_dropped_connection_and_a_delete_resumes_one_already_going():
    from botocore.exceptions import SSLError

    class Flaky(FakeAWS):
        """The console machine's proxy drops TLS for a while (live, 2026-10-02): the first reads of a runtime fail."""

        def __init__(self, drops: int):
            super().__init__()
            self.drops = drops

        def ctl_get_agent_runtime(self, agentRuntimeId, **p):
            if self.drops:
                self.drops -= 1
                raise SSLError(endpoint_url="https://bedrock-agentcore-control.us-west-2.amazonaws.com", error="EOF occurred in violation of protocol")
            return super().ctl_get_agent_runtime(agentRuntimeId, **p)

    aws = Flaky(drops=0)
    rid = aws.seed_runtime("adlc_probe_byoc")["agentRuntimeId"]
    aws.runtimes[rid]["status"] = "DELETING"  # an earlier delete's wait was cut off
    original = aws.ctl_delete_agent_runtime
    aws.ctl_delete_agent_runtime = lambda agentRuntimeId, **p: (_ for _ in ()).throw(err("ConflictException", "the runtime is being deleted"))
    down, job = teardown(aws, rid, name="adlc_probe_byoc")
    aws.drops = 2  # the wait's first two reads fail on the network
    out = down.run()
    assert statuses(out["stages"])["runtime"] == "succeeded" and rid not in aws.runtimes
    assert sum("read failed on the network (SSLError)" in line for line in job.lines) == 2
    aws.ctl_delete_agent_runtime = original
    gone = Flaky(drops=0)  # a network that stays down fails the stage, after its tries
    rid = gone.seed_runtime("adlc_probe_byoc")["agentRuntimeId"]
    down, job = teardown(gone, rid, name="adlc_probe_byoc")
    gone.drops = dp.Staged.READ_ATTEMPTS
    with pytest.raises(SSLError):
        down.run()
    assert statuses(down.stages)["runtime"] == "failed"
    refused = FakeAWS()  # an answer from AWS is never read again
    rid = refused.seed_runtime("adlc_probe_byoc")["agentRuntimeId"]
    refused.ctl_get_agent_runtime = lambda agentRuntimeId, **p: (_ for _ in ()).throw(err("AccessDeniedException", "no"))
    down = dp.Teardown(FakeSession(refused), account=ACCOUNT, region=REGION, runtime_id=rid, name="adlc_probe_byoc",
                       runtime=refused.runtimes[rid], job=FakeJob("undeploy-0123456789"), sleep=lambda s: None)
    with pytest.raises(ClientError):
        down.run()
    assert not [line for line in down.job.lines if "read failed" in line]


def test_the_build_infrastructure_is_removed_only_when_the_console_made_it():
    aws = FakeAWS()
    aws.projects["adlc-probe-build"] = {"name": "adlc-probe-build", "tags": [{"key": "adlc:console", "value": "1"}]}
    aws.seed_role(f"adlc-probe-build-{REGION}", {"adlc:console": "1"})
    aws.seed_role("adlc-probe-build", {"adlc:console": "1"})  # the one build role of a console before per-region roles
    aws.projects["adlc-probe-build-old"] = {"name": "adlc-probe-build-old", "serviceRole": f"arn:aws:iam::{ACCOUNT}:role/adlc-probe-build"}  # runs with it
    aws.log_groups.add("/aws/codebuild/adlc-probe-build")
    out = dp.remove_build_infrastructure(FakeSession(aws), account=ACCOUNT, region=REGION, names=dp.Names("adlc-probe"))
    assert out == {"project": "adlc-probe-build", "role": f"adlc-probe-build-{REGION}", "logGroup": "/aws/codebuild/adlc-probe-build",
                   "legacyRole": {"kept": "adlc-probe-build", "usedBy": ["us-east-1/adlc-probe-build-old", f"{REGION}/adlc-probe-build-old"]}}
    assert set(aws.projects) == {"adlc-probe-build-old"} and set(aws.roles) == {"adlc-probe-build"} and not aws.log_groups
    aws.regions = None  # the account's regions cannot be read (a spoke role): kept, said so
    assert dp.remove_build_infrastructure(FakeSession(aws), account=ACCOUNT, region=REGION, names=dp.Names("adlc-probe"))["legacyRole"]["usedBy"].startswith(
        "unknown") and "adlc-probe-build" in aws.roles
    aws.regions = ["us-east-1", REGION]
    del aws.projects["adlc-probe-build-old"]  # no project in any region runs with it any more: it goes, never orphaned
    assert dp.remove_build_infrastructure(FakeSession(aws), account=ACCOUNT, region=REGION, names=dp.Names("adlc-probe"))["legacyRole"] == "adlc-probe-build"
    assert not aws.roles
    aws.projects["adlc-console-build"] = {"name": "adlc-console-build", "tags": []}
    with pytest.raises(dp.DeployError, match="not the console's"):
        dp.remove_build_infrastructure(FakeSession(aws), account=ACCOUNT, region=REGION)
    assert "adlc-console-build" in aws.projects


def test_a_runtime_answer_is_read_from_json_an_event_stream_or_text():
    assert dp.answer_text(b'{"result": "15 days", "sessionId": "s"}') == "15 days"
    assert dp.answer_text(b'data: "Annual "\n\ndata: "leave"\n\n', "text/event-stream") == "Annual leave"
    assert dp.answer_text(b'{"answer": {"text": "nested"}}') == "nested" and dp.answer_text(b"plain") == "plain"
    assert dp.answer_text(b'{"turns": 3}') == '{"turns": 3}'


# -- the routes, through the console server ---------------------------------------------------------------------------

spec = importlib.util.spec_from_file_location("adlc_console_server_deploy", REPO / "app" / "console" / "server.py")
console_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(console_mod)  # type: ignore[union-attr]


class Client:
    def __init__(self, port):
        self.base, self.cookie = f"http://127.0.0.1:{port}", None

    def call(self, method, path, body=None):
        headers = {"Content-Type": "application/json", **({"Cookie": self.cookie} if self.cookie else {})}
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                if resp.headers.get("Set-Cookie"):
                    self.cookie = resp.headers["Set-Cookie"].split(";")[0]
                return resp.status, json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")


@pytest.fixture()
def server(tmp_path, monkeypatch):
    for owner, attrs in ((dp.Pipeline, ("BUILD_POLL", "RUNTIME_POLL", "ENDPOINT_POLL", "IAM_PAUSE")), (dp.Teardown, ("POLL",))):
        for attr in attrs:
            monkeypatch.setattr(owner, attr, 0.0)
    aws = FakeAWS()
    c = console_mod.Console(tmp_path / "data", session_factory=lambda **kw: FakeSession(aws), clients_factory=lambda cfg: None)
    srv = console_mod.create_server(c)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    client = Client(srv.server_address[1])
    assert client.call("POST", "/api/console/workspaces", {"id": "dev", "accountId": ACCOUNT, "region": REGION, "profile": "default"})[0] == 201
    yield client, aws, c
    srv.shutdown()
    srv.server_close()


def finished(client: Client, job_id: str) -> dict:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        status, job = client.call("GET", f"/api/console/jobs/{job_id}")
        if status == 200 and job["status"] != "running":
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} still running")


def test_deploy_update_and_delete_run_as_console_jobs_for_admins_only(server):
    client, aws, console = server
    body = {"source": "zip", "name": "probe_agent", "archive": b64(zip_of(sample())), "filename": "byoc_agent.zip"}
    status, job = client.call("POST", "/api/console/workspaces/dev/deployments", body)
    assert status == 202 and job["kind"] == "deploy" and job["params"]["name"] == "probe_agent" and "archive" not in job["params"]
    done = finished(client, job["id"])
    assert done["status"] == "succeeded", done.get("error")
    assert [s["status"] for s in done["progress"]["stages"]] == ["succeeded", "succeeded", "skipped", "succeeded", "succeeded", "succeeded"]
    rid = done["result"]["runtimeId"]
    status, listed = client.call("GET", "/api/console/workspaces/dev/deployments")
    assert status == 200 and [(d["name"], d["status"], d["source"], d["version"]) for d in listed["deployments"]] == [("probe_agent", "READY", "zip", "1")]
    assert client.call("POST", "/api/console/workspaces/dev/deployments", body)[0] == 400  # the name is taken: publish a version instead
    status, update = client.call("POST", f"/api/console/workspaces/dev/deployments/{rid}/versions", {**body, "name": None})
    assert status == 202 and finished(client, update["id"])["result"]["version"] == "2"
    status, detail = client.call("GET", f"/api/console/workspaces/dev/deployments/{rid}")
    assert status == 200 and [v["version"] for v in detail["versions"]] == ["2", "1"] and detail["console"] and len(detail["record"]["history"]) == 2
    assert client.call("GET", "/api/console/workspaces/dev/deployments/missing_agent-AbCdEf1234")[0] == 404
    status, moved = client.call("POST", f"/api/console/workspaces/dev/deployments/{rid}/endpoints", {"name": "stable", "version": "1"})
    assert status == 200 and moved["action"] == "created"  # pin an endpoint to an older version (a rollback)
    theirs = aws.seed_runtime("someone_elses", console=False)["agentRuntimeId"]
    assert client.call("DELETE", f"/api/console/workspaces/dev/deployments/{theirs}")[0] == 400
    assert client.call("POST", f"/api/console/workspaces/dev/deployments/{theirs}/versions", body)[0] == 400
    jobs = client.call("GET", "/api/console/workspaces/dev/deployments/jobs")[1]["jobs"]
    assert [j["kind"] for j in jobs] == ["deploy", "deploy"]
    # a member may look but not deploy or delete
    assert client.call("POST", "/api/console/users", {"username": "admin", "password": "0123456789", "role": "admin"})[0] == 201
    assert client.call("POST", "/api/console/login", {"username": "admin", "password": "0123456789"})[0] == 200
    assert client.call("POST", "/api/console/users", {"username": "ann", "password": "0123456789", "role": "member", "workspaces": ["dev"]})[0] == 201
    admin_cookie = client.cookie
    client.cookie = None
    assert client.call("POST", "/api/console/login", {"username": "ann", "password": "0123456789"})[0] == 200
    assert client.call("GET", "/api/console/workspaces/dev/deployments")[0] == 200
    assert client.call("POST", "/api/console/workspaces/dev/deployments", {**body, "name": "member_agent"})[0] == 403
    assert client.call("POST", f"/api/console/workspaces/dev/deployments/{rid}/versions", body)[0] == 403
    assert client.call("DELETE", f"/api/console/workspaces/dev/deployments/{rid}")[0] == 403
    client.cookie = admin_cookie
    status, gone = client.call("DELETE", f"/api/console/workspaces/dev/deployments/{rid}?keepRepo=1")
    assert status == 202 and gone["kind"] == "undeploy" and gone["params"]["keepRepository"] is True
    assert finished(client, gone["id"])["status"] == "succeeded"
    assert rid not in aws.runtimes and client.call("GET", "/api/console/workspaces/dev/deployments")[1]["deployments"] == []
    assert not [k for k in aws.objects if k.startswith("deployments/probe_agent/")]


# -- the sample agent -----------------------------------------------------------------------------------------------------

def test_the_sample_agent_serves_the_runtime_http_contract():
    loaded = importlib.util.spec_from_file_location("byoc_probe_agent", FIXTURE / "main.py")
    agent = importlib.util.module_from_spec(loaded)
    loaded.loader.exec_module(agent)  # type: ignore[union-attr]
    srv = agent.serve(port=0, host="127.0.0.1")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        with urllib.request.urlopen(base + "/ping", timeout=5) as resp:
            ping = json.loads(resp.read())
        assert resp.headers["Content-Type"] == "application/json" and ping["status"] == "Healthy" and isinstance(ping["time_of_last_update"], int)
        req = urllib.request.Request(base + "/invocations", data=json.dumps({"prompt": "hi"}).encode(), method="POST",
                                     headers={"Content-Type": "application/json", agent.SESSION_HEADER: "s" * 40})
        with urllib.request.urlopen(req, timeout=5) as resp:
            answer = json.loads(resp.read())
        assert answer["result"] == "adlc byoc probe: received 'hi'" and answer["sessionId"] == "s" * 40 and answer["machine"]
        bad = urllib.request.Request(base + "/invocations", data=b"not json", method="POST")
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(bad, timeout=5)
        assert caught.value.code == 400
    finally:
        srv.shutdown()
        srv.server_close()
    dockerfile = (FIXTURE / "Dockerfile").read_text()
    assert "EXPOSE 8080" in dockerfile and 'CMD ["python", "main.py"]' in dockerfile and "public.ecr.aws/" in dockerfile


# -- the review's findings, each pinned ------------------------------------------------------------------------------------

class EnhancedScanning(FakeAWS):
    """ECR with enhanced (Inspector) scanning: a scanned image is ACTIVE (monitored continuously), its findings enhanced."""

    counts: dict = {"CRITICAL": 4, "HIGH": 13}

    def ecr_describe_image_scan_findings(self, repositoryName, imageId, maxResults=None, nextToken=None, **p):
        found = {"enhancedFindings": [{"severity": "CRITICAL", "title": "CVE-2026-0001", "packageVulnerabilityDetails": {
            "vulnerabilityId": "CVE-2026-0001", "vulnerablePackages": [{"name": "perl", "version": "5.36.0"}]}}] * 4 + [{"severity": "HIGH"}]}
        if self.counts is not None:
            found["findingSeverityCounts"] = dict(self.counts)
        return {"repositoryName": repositoryName, "imageId": imageId, "imageScanFindings": found,
                "imageScanStatus": {"status": "ACTIVE", "description": "Continuous scan is selected for image."}}


def test_an_enhanced_scan_that_is_active_is_complete_and_gated_on_its_counts():
    """review M1: ACTIVE (enhanced scanning) is a finished scan: CRITICAL findings stop the deploy, never "unscanned"."""
    aws = EnhancedScanning()
    pipeline, job, _ = run(aws, {"source": "dockerfile", "name": "risky_agent", "archive": b64(zip_of(sample()))})
    with pytest.raises(dp.DeployError, match="CRITICAL 4 finding"):
        pipeline.run()
    assert statuses(pipeline.stages)["scan"] == "failed" and not aws.named("bedrock-agentcore-control", "create_agent_runtime")
    assert job.progressed["scan"]["status"] == "ACTIVE" and job.progressed["scan"]["blocking"] == {"CRITICAL": 4}
    reports = EnhancedScanning()
    reports.counts = None  # no counts in the answer: the findings themselves are counted
    pipeline, job, _ = run(reports, {"source": "dockerfile", "name": "risky_agent", "archive": b64(zip_of(sample())), "scanBlock": []})
    out = pipeline.run()
    assert statuses(out["stages"])["scan"] == "succeeded" and out["scan"]["counts"] == {"CRITICAL": 4, "HIGH": 1}
    assert "ACTIVE (enhanced scanning" in next(s["detail"] for s in out["stages"] if s["name"] == "scan")


def test_a_runtime_role_of_another_region_is_never_taken_over_and_each_region_builds_with_its_own_role():
    """review M2: IAM names are account-wide: a runtime role whose trust (or tag) names another region is refused, and
    an undeploy in this region leaves it; the build role is per region."""
    aws = FakeAWS()
    aws.seed_role("adlc-console-rt-faq_bot", {"adlc:console": "1", "adlc:runtime": "faq_bot"})
    aws.roles["adlc-console-rt-faq_bot"]["AssumeRolePolicyDocument"] = dp.runtime_trust(ACCOUNT, "us-west-2")
    req = dp.deploy_request({"source": "zip", "name": "faq_bot", "archive": b64(zip_of({"main.py": "print(1)"}))})
    pipeline = dp.Pipeline(FakeSession(aws), account=ACCOUNT, region="us-east-1", request=req, job=FakeJob(), sleep=lambda s: None)
    with pytest.raises(dp.DeployError, match="execution role of faq_bot in us-west-2"):
        pipeline.run()
    assert not aws.named("iam", "update_assume_role_policy") and not aws.named("iam", "put_role_policy")
    assert not aws.named("bedrock-agentcore-control", "create_agent_runtime")
    down = dp.Teardown(FakeSession(aws), account=ACCOUNT, region="us-east-1", runtime_id="faq_bot-OldOld1234", name="faq_bot", runtime=None,
                       job=FakeJob("undeploy-0123456789"), record={"name": "faq_bot", "role": "adlc-console-rt-faq_bot"}, sleep=lambda s: None)
    out = down.run()
    assert "in us-west-2: left alone" in next(s["detail"] for s in out["stages"] if s["name"] == "role") and "adlc-console-rt-faq_bot" in aws.roles
    tagged = FakeAWS()  # a role made by this console carries its region
    tagged.seed_role("adlc-console-rt-faq_bot", {"adlc:console": "1", "adlc:runtime": "faq_bot", "adlc:region": "eu-west-1"})
    pipeline = dp.Pipeline(FakeSession(tagged), account=ACCOUNT, region=REGION, request=req, job=FakeJob(), sleep=lambda s: None)
    with pytest.raises(dp.DeployError, match="in eu-west-1"):
        pipeline.run()
    assert dp.NAMES.build_role("us-west-2") == "adlc-console-build-us-west-2" != dp.NAMES.build_role("us-east-1")


def test_a_runtime_role_off_the_console_path_is_refused_in_a_spoke_workspace_and_a_legacy_one_still_serves_a_profile_one():
    """The console's IAM path: with a permissions boundary (a spoke workspace) a same-name role off /adlc-console/ — the
    console's own from before roles had a path, or anyone else's — is refused before anything is written, and an undeploy
    leaves it; in a profile workspace the console's legacy role on / still serves, given by its own ARN (no path)."""
    boundary = f"arn:aws:iam::{ACCOUNT}:policy/adlc-console-spoke-boundary"
    req = dp.deploy_request({"source": "zip", "name": "faq_bot", "archive": b64(zip_of({"main.py": "print(1)"}))})
    for tags, why in (({"adlc:console": "1", "adlc:runtime": "faq_bot"}, "on the path /, not the console's /adlc-console/"),
                      ({"adlc:probe": "foreign"}, "not tagged adlc:console=1")):
        aws = FakeAWS()
        aws.seed_role("adlc-console-rt-faq_bot", tags)  # on /
        pipeline = dp.Pipeline(FakeSession(aws), account=ACCOUNT, region=REGION, request=req, job=FakeJob(), sleep=lambda s: None, boundary=boundary)
        with pytest.raises(dp.DeployError, match=why):
            pipeline.run()
        assert set(aws.ops("iam")) == {"get_role", "list_role_tags"} and not aws.named("bedrock-agentcore-control", "create_agent_runtime")
    aws = FakeAWS()
    aws.seed_role("adlc-console-rt-faq_bot", {"adlc:console": "1", "adlc:runtime": "faq_bot"})
    down = dp.Teardown(FakeSession(aws), account=ACCOUNT, region=REGION, runtime_id="faq_bot-OldOld1234", name="faq_bot", runtime=None,
                       job=FakeJob("undeploy-0123456789"), record={"name": "faq_bot", "role": "adlc-console-rt-faq_bot"}, sleep=lambda s: None,
                       boundary=boundary)
    out = down.run()
    assert "on the path /" in next(s["detail"] for s in out["stages"] if s["name"] == "role") and "adlc-console-rt-faq_bot" in aws.roles
    assert not aws.named("iam", "delete_role_policy") and not aws.named("iam", "put_role_permissions_boundary") and not aws.named("iam", "delete_role")
    profile = FakeAWS()
    profile.seed_role("adlc-console-rt-faq_bot", {"adlc:console": "1", "adlc:runtime": "faq_bot"})
    pipeline, _job, _ = run(profile, {"source": "zip", "name": "faq_bot", "archive": b64(zip_of({"main.py": "print(1)"}))})
    pipeline.run()
    [create] = profile.named("bedrock-agentcore-control", "create_agent_runtime")
    assert create["roleArn"] == f"arn:aws:iam::{ACCOUNT}:role/adlc-console-rt-faq_bot" and not profile.named("iam", "create_role")


def test_cleaning_up_a_missing_record_leaves_what_a_live_runtime_of_its_name_uses(server):
    """review H2: a runtime deleted outside the console, then deployed again under its name: cleaning the stale record
    deletes only its own job folders and images, never the role, repository or sources the live runtime uses."""
    client, aws, console = server
    old = "faq_bot-OldOld1234"
    console.store.write("deployments", {old: {"workspace": "dev", "runtimeId": old, "name": "faq_bot", "source": "dockerfile", "role": "adlc-console-rt-faq_bot",
                                              "repository": "adlc-console/faq-bot", "history": [
                                                  {"version": "1", "jobId": "deploy-0000000aaa", "artifact": f"{REGISTRY}/adlc-console/faq-bot@{digest('deploy-0000000aaa')}"}]}})
    status, job = client.call("POST", "/api/console/workspaces/dev/deployments", {"source": "dockerfile", "name": "faq_bot", "archive": b64(zip_of(sample()))})
    assert status == 202 and finished(client, job["id"])["status"] == "succeeded"
    live = next(r for r in aws.runtimes.values() if r["agentRuntimeName"] == "faq_bot")
    aws.repos["adlc-console/faq-bot"]["images"]["deploy-0000000aaa"] = digest("deploy-0000000aaa")  # the old runtime's image
    aws.seed_object("deployments/faq_bot/deploy-0000000aaa/context.zip")  # the old runtime's sources
    status, gone = client.call("DELETE", f"/api/console/workspaces/dev/deployments/{old}")
    assert status == 202
    done = finished(client, gone["id"])
    detail = {s["name"]: s["detail"] for s in done["result"]["stages"]}
    assert done["status"] == "succeeded" and "used by" in detail["repository"] and "used by" in detail["role"]
    assert "adlc-console-rt-faq_bot" in aws.roles and "adlc-console/faq-bot" in aws.repos  # the live runtime's role and repository
    assert aws.repos["adlc-console/faq-bot"]["images"] == {job["id"]: digest(job["id"])}  # only the old record's image went
    assert [k for k in aws.objects if k.startswith("deployments/faq_bot/")] == [f"deployments/faq_bot/{job['id']}/context.zip"]
    assert aws.runtimes[live["agentRuntimeId"]]["status"] == "READY" and old not in console.store.read("deployments", {})


def test_a_workspace_boundary_is_on_every_role_a_deployment_creates_and_put_on_an_older_one(server):
    """review H3: with permissionsBoundaryArn the runtime and build roles are created with it, and a console role made
    before it gets it (PutRolePermissionsBoundary) before its policy is put again."""
    client, aws, console = server
    boundary = f"arn:aws:iam::{ACCOUNT}:policy/adlc-console-spoke-boundary"
    assert client.call("POST", "/api/console/workspaces", {"id": "spoke", "accountId": ACCOUNT, "region": REGION, "profile": "default",
                                                           "permissionsBoundaryArn": boundary})[0] == 201
    body = {"source": "zip", "name": "deps_agent", "archive": b64(zip_of(sample(**{"requirements.txt": b"httpx\n"})))}
    status, job = client.call("POST", "/api/console/workspaces/spoke/deployments", body)
    assert status == 202 and finished(client, job["id"])["status"] == "succeeded"
    made = {r["RoleName"]: r.get("PermissionsBoundary") for r in aws.named("iam", "create_role")}
    assert made == {f"adlc-console-build-{REGION}": boundary, "adlc-console-rt-deps_agent": boundary}
    aws.seed_role("adlc-console-rt-old_agent", {"adlc:console": "1", "adlc:runtime": "old_agent"}, path="/adlc-console/")  # made before the workspace had one
    rid = aws.seed_runtime("old_agent", source="zip")["agentRuntimeId"]
    status, job = client.call("POST", f"/api/console/workspaces/spoke/deployments/{rid}/versions", {"source": "zip", "archive": b64(zip_of(sample()))})
    assert status == 202 and finished(client, job["id"])["status"] == "succeeded"
    calls = [(o, p.get("RoleName")) for s, o, p in aws.calls if s == "iam" and p.get("RoleName") == "adlc-console-rt-old_agent"]
    assert calls.index(("put_role_permissions_boundary", "adlc-console-rt-old_agent")) < calls.index(("put_role_policy", "adlc-console-rt-old_agent"))
    assert aws.roles["adlc-console-rt-old_agent"]["boundary"] == boundary
    aws.seed_role("adlc-console-rt-older_agent", {"adlc:console": "1", "adlc:runtime": "older_agent"}, path="/adlc-console/")  # deleted with it: given it first
    rid = aws.seed_runtime("older_agent", source="zip")["agentRuntimeId"]
    status, gone = client.call("DELETE", f"/api/console/workspaces/spoke/deployments/{rid}")
    assert status == 202 and finished(client, gone["id"])["status"] == "succeeded" and "adlc-console-rt-older_agent" not in aws.roles
    calls = [o for s, o, p in aws.calls if s == "iam" and p.get("RoleName") == "adlc-console-rt-older_agent"]
    assert calls.index("put_role_permissions_boundary") < calls.index("delete_role_policy") < calls.index("delete_role")
    status, out = client.call("POST", "/api/console/workspaces", {"id": "bad", "accountId": ACCOUNT, "region": REGION, "profile": "default",
                                                                  "permissionsBoundaryArn": "arn:aws:iam::999999999999:policy/x"})
    assert status == 409 and "workspace's account" in out["error"]  # the server answers a WorkspaceError 409


def test_a_stale_records_cleanup_keeps_what_any_version_of_a_live_runtime_of_its_name_runs(monkeypatch):
    """review 3 #12: a MISSING record's cleanup deleted an image an older version of the live runtime of the same name
    runs (an endpoint or a rollback can return to it): every version is read, and when one cannot be, nothing it
    might run is deleted."""
    repo = "adlc-console/faq-bot"
    d1, d2 = digest("old"), digest("new")
    record = {"name": "faq_bot", "role": "adlc-console-rt-faq_bot", "repository": repo,
              "history": [{"version": "1", "jobId": "deploy-0000000aaa", "artifact": f"{REGISTRY}/{repo}@{d1}"}]}

    class Versioned(FakeAWS):
        unreadable: set = set()

        def ctl_get_agent_runtime(self, agentRuntimeId, agentRuntimeVersion=None, **p):
            out = super().ctl_get_agent_runtime(agentRuntimeId)
            if agentRuntimeVersion in self.unreadable:
                raise err("ThrottlingException", "Rate exceeded")
            if agentRuntimeVersion == "1":  # the old record's image, still version 1 of the live runtime
                out["agentRuntimeArtifact"] = {"containerConfiguration": {"containerUri": f"{REGISTRY}/{repo}@{d1}"}}
            return out

    def cleanup(aws):
        aws.seed_repo(repo, {"adlc:console": "1", "adlc:runtime": "faq_bot"})
        aws.repos[repo]["images"] = {"old": d1, "new": d2}
        aws.seed_role("adlc-console-rt-faq_bot", {"adlc:console": "1", "adlc:runtime": "faq_bot"})
        aws.seed_runtime("faq_bot", version=2, agentRuntimeArtifact={"containerConfiguration": {"containerUri": f"{REGISTRY}/{repo}@{d2}"}})
        down = dp.Teardown(FakeSession(aws), account=ACCOUNT, region=REGION, runtime_id="faq_bot-OldOld1234", name="faq_bot", runtime=None,
                           job=FakeJob("undeploy-0123456789"), record=record, sleep=lambda s: None)
        return {s["name"]: s.get("detail") for s in down.run()["stages"]}

    aws = Versioned()
    stages = cleanup(aws)
    assert aws.repos[repo]["images"] == {"old": d1, "new": d2} and not aws.named("ecr", "batch_delete_image")
    assert "kept" in stages["repository"] and "adlc-console-rt-faq_bot" in aws.roles
    aws = Versioned()
    aws.unreadable = {"1"}
    stages = cleanup(aws)
    assert "could not be read" in stages["repository"] and "could not be read" in stages["sources"]
    assert aws.repos[repo]["images"] == {"old": d1, "new": d2} and not aws.named("s3", "delete_objects")
