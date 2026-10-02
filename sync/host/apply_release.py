#!/usr/bin/env python3
"""Workshop Customizer — host-side release applier.

Pre-installed on the workshop EC2 by the CloudFormation stack and invoked ONLY through the
custom SSM Document ``WorkshopCustomizerApplyRelease``. The App never ships a script body;
it passes typed parameters (S3 URI, expected SHA-256, expected version, action).

Actions
-------
apply     fetch the release bundle from an allow-listed prefix, verify its hash, extract it
          safely into staging, verify RELEASE.json (version, template commit, per-file hashes),
          move it to ``releases/<version>``, set ownership, switch ``current`` atomically,
          run the smoke check and arm the commit watchdog. Any failure before the switch leaves
          ``current`` untouched; a smoke failure after the switch rolls back immediately.
commit    confirm the pending release (stops the watchdog rollback).
rollback  switch ``current`` back to the previous release.
status    print the deployment state.
watchdog  (spawned by apply) roll back automatically if no commit arrives before the deadline.

Layout under the target root (from the target contract):

    releases/<version>/      immutable release trees
    current -> releases/<v>  atomic symlink switch (os.replace on the symlink)
    previous -> releases/<v> convenience link, derived from the state file
    deployment-manifest.json single source of truth (temp file + rename)
    .staging/                scratch, always cleaned

The script is standard-library only (python3 is on the AL2023 image); downloads use the AWS
CLI already present on the instance. ``file://`` sources are accepted only when the contract
allow-lists them (used by the local test-suite).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import sys
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

CONTRACT_PATH = os.environ.get("WSC_TARGET_CONTRACT", "/etc/workshop-customizer/target.json")
STATE_FILE = "deployment-manifest.json"
MANIFEST_NAME = "RELEASE.json"
STATE_SCHEMA = "workshop-customizer/deployment/1"

VERSION_RE = re.compile(r"^[a-z][a-z0-9-]{2,40}-[0-9a-f]{12}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")

MAX_ENTRIES = 5000
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_TOTAL_BYTES = 200 * 1024 * 1024
MAX_COMPRESSION_RATIO = 100
KEEP_RELEASES = 3
#: The scenario schema's namespace.agentName pattern, and the names the validator reserves because they
#: are entries of this target root (validator.RESERVED_AGENT_NAMES); tests keep both in step.
AGENT_NAME_RE = re.compile(r"^[a-z][a-z0-9]{2,18}$")
RESERVED_AGENT_NAMES = ("current", "previous", "releases", "run", "skills")
DEFAULT_WATCHDOG_MINUTES = 20
REQUIRED_RELEASE_FILES = (MANIFEST_NAME, "04-deploy.sh", "09-run-eval.sh", "pack/pack.env", "pack/pack.json", "lambda/fixtures.json")


class ApplyError(Exception):
    """Fail closed: the message is safe to surface to the SSM command output."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# Contract, state, hashing
# ---------------------------------------------------------------------------


def load_contract(path: str = CONTRACT_PATH) -> dict[str, Any]:
    p = Path(path)
    if not p.is_file():
        raise ApplyError(f"target contract missing: {path} (the stack must install it; refusing to apply)")
    contract = json.loads(p.read_text(encoding="utf-8"))
    for key in ("targetRoot", "user", "group", "templateCommit", "allowedReleasePrefixes", "packSchemaVersion"):
        if key not in contract:
            raise ApplyError(f"target contract lacks '{key}'")
    if not COMMIT_RE.match(str(contract["templateCommit"])):
        raise ApplyError("target contract templateCommit is not a 40-hex commit")
    if not isinstance(contract["allowedReleasePrefixes"], list) or not contract["allowedReleasePrefixes"]:
        raise ApplyError("target contract allowedReleasePrefixes must be a non-empty list")
    return contract


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def content_hash(files: dict[str, str]) -> str:
    return hashlib.sha256("\n".join(f"{k} {v}" for k, v in sorted(files.items())).encode("utf-8")).hexdigest()


def read_state(root: Path) -> dict[str, Any]:
    path = root / STATE_FILE
    if not path.is_file():
        return {"schema": STATE_SCHEMA, "active": None, "previous": None, "status": "no-active-release", "history": []}
    return json.loads(path.read_text(encoding="utf-8"))


def write_state(root: Path, state: dict[str, Any]) -> None:
    state["schema"] = STATE_SCHEMA
    state["updatedAt"] = iso(utcnow())
    tmp = root / f".{STATE_FILE}.tmp"
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, root / STATE_FILE)


def record(state: dict[str, Any], version: str | None, action: str, result: str) -> None:
    state.setdefault("history", []).append({"version": version, "action": action, "result": result, "at": iso(utcnow())})
    state["history"] = state["history"][-50:]


# ---------------------------------------------------------------------------
# Fetch and extract
# ---------------------------------------------------------------------------


def fetch_bundle(uri: str, dest: Path, allowed_prefixes: list[str]) -> None:
    if not any(uri.startswith(prefix) for prefix in allowed_prefixes):
        raise ApplyError("source URI is outside the allow-listed release prefixes")
    if uri.startswith("s3://"):
        cmd = ["aws", "s3", "cp", "--only-show-errors", uri, str(dest)]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if proc.returncode != 0:
            raise ApplyError(f"download failed: {proc.stderr.strip()[:300]}")
    elif uri.startswith("file://"):
        shutil.copyfile(uri[len("file://"):], dest)
    else:
        raise ApplyError("unsupported source scheme (expected s3:// or an allow-listed file://)")


def safe_extract(zip_path: Path, dest: Path) -> None:
    """Extract with zip-slip, symlink, duplicate, size and ratio protections."""
    dest.mkdir(parents=True, exist_ok=False)
    seen: set[str] = set()
    total = 0
    with zipfile.ZipFile(zip_path) as archive:
        infos = archive.infolist()
        if len(infos) > MAX_ENTRIES:
            raise ApplyError(f"bundle has {len(infos)} entries; limit is {MAX_ENTRIES}")
        for info in infos:
            name = info.filename
            if name.endswith("/"):
                continue  # directories are created implicitly
            norm = os.path.normpath(name)
            if name.startswith(("/", "\\")) or norm.startswith("..") or "/../" in f"/{norm}/" or "\\" in name or norm != name.rstrip("/"):
                raise ApplyError(f"bundle entry rejected (path escape): {name!r}")
            if norm in seen:
                raise ApplyError(f"bundle entry duplicated: {name!r}")
            seen.add(norm)
            mode = (info.external_attr >> 16) & 0o170000
            if mode in (stat.S_IFLNK, stat.S_IFCHR, stat.S_IFBLK, stat.S_IFIFO, stat.S_IFSOCK):
                raise ApplyError(f"bundle entry rejected (special file / symlink): {name!r}")
            if info.file_size > MAX_FILE_BYTES:
                raise ApplyError(f"bundle entry too large: {name!r}")
            if info.compress_size and info.file_size / max(info.compress_size, 1) > MAX_COMPRESSION_RATIO:
                raise ApplyError(f"bundle entry compression ratio suspicious: {name!r}")
            total += info.file_size
            if total > MAX_TOTAL_BYTES:
                raise ApplyError("bundle uncompressed size exceeds limit")
            target = dest / norm
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as src, target.open("wb") as out:
                shutil.copyfileobj(src, out)
    # normalise permissions: files 0644, dirs 0755, scripts 0755
    for path in dest.rglob("*"):
        if path.is_dir():
            path.chmod(0o755)
        elif path.suffix == ".sh":
            path.chmod(0o755)
        else:
            path.chmod(0o644)


def verify_release_tree(release_dir: Path, *, expected_version: str, expected_commit: str, pack_schema_version: int) -> dict[str, Any]:
    manifest_path = release_dir / MANIFEST_NAME
    if not manifest_path.is_file():
        raise ApplyError(f"{MANIFEST_NAME} missing in bundle")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != "workshop-customizer/release/1":
        raise ApplyError("unsupported release manifest schema")
    if manifest.get("version") != expected_version:
        raise ApplyError(f"version mismatch: bundle is {manifest.get('version')!r}, expected {expected_version!r}")
    if manifest.get("templateCommit") != expected_commit:
        raise ApplyError("template commit mismatch between bundle and target contract")
    expected_files: dict[str, str] = manifest.get("files", {})
    # Python bytecode caches are never release content (the smoke compiles sources; operators may run python
    # inside the tree). They are ignored here and never counted as expected or unexpected files.
    actual = {
        p.relative_to(release_dir).as_posix(): sha256_file(p)
        for p in sorted(release_dir.rglob("*"))
        if p.is_file() and p.name != MANIFEST_NAME and "__pycache__" not in p.relative_to(release_dir).parts and p.suffix != ".pyc"
    }
    missing = sorted(set(expected_files) - set(actual))
    unexpected = sorted(set(actual) - set(expected_files))
    mismatched = sorted(rel for rel in expected_files if rel in actual and actual[rel] != expected_files[rel])
    if missing or unexpected or mismatched:
        raise ApplyError(f"release integrity failed: missing={missing[:5]} unexpected={unexpected[:5]} mismatched={mismatched[:5]}")
    if content_hash(actual) != manifest.get("contentHash"):
        raise ApplyError("release contentHash mismatch")
    for rel in REQUIRED_RELEASE_FILES:
        if not (release_dir / rel).is_file():
            raise ApplyError(f"release lacks required file {rel}")
    pack = json.loads((release_dir / "pack" / "pack.json").read_text(encoding="utf-8"))
    if pack.get("templateCommit") != expected_commit:
        raise ApplyError("pack.json template commit does not match the target contract")
    del pack_schema_version  # reserved: schema negotiation lands with pack schema v2
    return manifest


# ---------------------------------------------------------------------------
# Filesystem operations
# ---------------------------------------------------------------------------


def switch_current(root: Path, version: str) -> None:
    """Atomically repoint ``current`` at ``releases/<version>``."""
    tmp = root / ".current.tmp"
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    os.symlink(f"releases/{version}", tmp)
    os.replace(tmp, root / "current")


def point_previous(root: Path, version: str | None) -> None:
    link = root / "previous"
    if link.is_symlink() or link.exists():
        link.unlink()
    if version:
        os.symlink(f"releases/{version}", link)


def set_ownership(path: Path, user: str, group: str) -> str:
    """chown recursively when running as root; report what happened."""
    if os.geteuid() != 0:
        return "skipped (not root)"
    try:
        import grp
        import pwd

        uid = pwd.getpwnam(user).pw_uid
        gid = grp.getgrnam(group).gr_gid
    except KeyError as exc:
        raise ApplyError(f"target user/group not present on host: {exc}") from exc
    os.chown(path, uid, gid)
    for p in path.rglob("*"):
        os.lchown(p, uid, gid)
    return f"chowned to {user}:{group}"


def smoke_check(release_dir: Path, *, expected_version: str, expected_commit: str, pack_schema_version: int, user: str) -> dict[str, Any]:
    """Post-switch smoke: integrity again, shell syntax, Python compile, readability by the target user."""
    verify_release_tree(release_dir, expected_version=expected_version, expected_commit=expected_commit, pack_schema_version=pack_schema_version)
    results: dict[str, Any] = {"integrity": "ok"}
    bash = shutil.which("bash")
    if bash:
        for script in sorted(release_dir.glob("*.sh")):
            proc = subprocess.run([bash, "-n", str(script)], capture_output=True, text=True, timeout=60)
            if proc.returncode != 0:
                raise ApplyError(f"smoke: shell syntax error in {script.name}: {proc.stderr.strip()[:200]}")
        results["shellSyntax"] = "ok"
    handler = release_dir / "lambda"
    for py in sorted(handler.glob("*.py")):
        # PYTHONPYCACHEPREFIX keeps the bytecode out of the release tree (integrity must stay byte-exact).
        env = {**os.environ, "PYTHONPYCACHEPREFIX": tempfile.gettempdir()}
        proc = subprocess.run([sys.executable, "-m", "py_compile", str(py)], capture_output=True, text=True, timeout=60, env=env)
        if proc.returncode != 0:
            raise ApplyError(f"smoke: python compile error in lambda/{py.name}")
    results["pythonCompile"] = "ok"
    if os.geteuid() == 0 and shutil.which("sudo"):
        probe = subprocess.run(["sudo", "-n", "-u", user, "test", "-r", str(release_dir / "pack" / "pack.env")], capture_output=True, timeout=30)
        if probe.returncode != 0:
            raise ApplyError(f"smoke: {user} cannot read the release")
        results["userReadable"] = "ok"
    else:
        results["userReadable"] = "skipped (not root)"
    return results


def release_agent_name(path: Path) -> str | None:
    """The release's RELEASE.json ``namespace.agentName``; None when it is unreadable, not of the schema's
    shape, or reserved (a legacy or hand-edited release, never kept for its namespace)."""
    try:
        name = json.loads((path / MANIFEST_NAME).read_text(encoding="utf-8"))["namespace"]["agentName"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(name, str) or not AGENT_NAME_RE.fullmatch(name) or name in RESERVED_AGENT_NAMES:
        return None
    return name


def prune_releases(root: Path, keep: int, protect: set[str]) -> list[str]:
    """Remove all but the newest ``keep`` releases, never one in ``protect`` and never the newest release
    of each namespace (RELEASE.json ``namespace.agentName``) among them.

    That release's 99-cleanup.sh --scenario-only is how the SA removes the namespace's resources before a
    new sync, and the host cannot tell whether they still exist: 01/02 create the knowledge base and the
    Gateway before 04 creates ``~/workshop/<agentName>``, and a failed cleanup removes that workdir too.
    Growth is bounded by the number of namespaces used in one environment: at most ``keep`` releases plus
    one per namespace plus ``protect``. A release without a readable namespace is pruned as any other."""
    releases = root / "releases"
    if not releases.is_dir():
        return []
    candidates = sorted((p for p in releases.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime, reverse=True)
    cleaners: dict[str, str] = {}
    for path in candidates:
        name = release_agent_name(path)
        if name and name not in cleaners:
            cleaners[name] = path.name
    protect = protect | set(cleaners.values())
    removed: list[str] = []
    for path in candidates[keep:]:
        if path.name in protect:
            continue
        shutil.rmtree(path)
        removed.append(path.name)
    return removed


def spawn_watchdog(root: Path, version: str, deadline: datetime) -> None:
    cmd = [sys.executable, os.path.abspath(__file__), "--action", "watchdog", "--version", version, "--deadline", iso(deadline), "--target-root", str(root)]
    subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


def do_rollback(root: Path, state: dict[str, Any], reason: str) -> dict[str, Any]:
    previous = state.get("previous")
    active = state.get("active")
    if previous and (root / "releases" / previous).is_dir():
        switch_current(root, previous)
        state.update({"active": previous, "previous": None, "status": "rolled-back", "commitDeadline": None})
        point_previous(root, None)
        record(state, previous, "rollback", reason)
        write_state(root, state)
        return {"status": "rolled-back", "active": previous, "rolledBackFrom": active, "reason": reason}
    # first deployment failed: leave a defined "no release active" state
    current = root / "current"
    if current.is_symlink():
        current.unlink()
    state.update({"active": None, "previous": None, "status": "no-active-release", "commitDeadline": None})
    record(state, active, "rollback", f"{reason}; no previous release")
    write_state(root, state)
    return {"status": "no-active-release", "rolledBackFrom": active, "reason": reason}


def action_apply(args: argparse.Namespace, contract: dict[str, Any]) -> dict[str, Any]:
    if not VERSION_RE.match(args.version or ""):
        raise ApplyError("version parameter has an invalid shape")
    if not SHA256_RE.match(args.sha256 or ""):
        raise ApplyError("sha256 parameter has an invalid shape")
    root = Path(args.target_root or contract["targetRoot"])
    root.mkdir(parents=True, exist_ok=True)
    (root / "releases").mkdir(exist_ok=True)
    staging = root / ".staging"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir()
    state = read_state(root)
    try:
        bundle = staging / "release.zip"
        fetch_bundle(args.source, bundle, contract["allowedReleasePrefixes"])
        actual_sha = sha256_file(bundle)
        if actual_sha != args.sha256:
            raise ApplyError("bundle sha256 does not match the expected value")
        extracted = staging / "tree"
        safe_extract(bundle, extracted)
        manifest = verify_release_tree(extracted, expected_version=args.version, expected_commit=contract["templateCommit"], pack_schema_version=int(contract["packSchemaVersion"]))

        target = root / "releases" / args.version
        if target.exists():
            existing = json.loads((target / MANIFEST_NAME).read_text(encoding="utf-8"))
            if existing.get("contentHash") != manifest.get("contentHash"):
                raise ApplyError("version conflict: a release with this version but different content is already installed; build a new version")
            if state.get("active") == args.version and state.get("status") == "committed":
                record(state, args.version, "apply", "no-op (already active and committed)")
                write_state(root, state)
                return {"status": "no-op", "active": args.version, "version": args.version}
            shutil.rmtree(extracted)
        else:
            os.replace(extracted, target)
        ownership = set_ownership(target, contract["user"], contract["group"])

        previous = state.get("active") if state.get("active") != args.version else state.get("previous")
        switch_current(root, args.version)
        point_previous(root, previous)
        deadline = utcnow() + timedelta(minutes=args.watchdog_minutes)
        state.update({"active": args.version, "previous": previous, "status": "pending-commit", "commitDeadline": iso(deadline), "appliedAt": iso(utcnow()), "source": args.source, "bundleSha256": actual_sha})
        record(state, args.version, "apply", "switched; awaiting commit")
        write_state(root, state)

        try:
            smoke = smoke_check(target, expected_version=args.version, expected_commit=contract["templateCommit"], pack_schema_version=int(contract["packSchemaVersion"]), user=contract["user"])
        except ApplyError as exc:
            rollback = do_rollback(root, state, f"smoke failed: {exc}")
            return {"status": "smoke-failed", "version": args.version, "error": str(exc), "rollback": rollback}

        if not args.no_watchdog:
            spawn_watchdog(root, args.version, deadline)
        pruned = prune_releases(root, KEEP_RELEASES, protect={v for v in (args.version, previous) if v})
        return {
            "status": "pending-commit",
            "version": args.version,
            "previous": previous,
            "commitDeadline": iso(deadline),
            "ownership": ownership,
            "smoke": smoke,
            "pruned": pruned,
            "templateCommit": contract["templateCommit"],
        }
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def action_commit(args: argparse.Namespace, contract: dict[str, Any]) -> dict[str, Any]:
    root = Path(args.target_root or contract["targetRoot"])
    state = read_state(root)
    if args.version and state.get("active") != args.version:
        raise ApplyError(f"cannot commit {args.version}: active release is {state.get('active')}")
    if state.get("status") not in ("pending-commit", "committed"):
        raise ApplyError(f"nothing to commit (status {state.get('status')})")
    state.update({"status": "committed", "commitDeadline": None, "committedAt": iso(utcnow())})
    record(state, state.get("active"), "commit", "confirmed by operator")
    write_state(root, state)
    return {"status": "committed", "active": state.get("active")}


def action_rollback(args: argparse.Namespace, contract: dict[str, Any]) -> dict[str, Any]:
    root = Path(args.target_root or contract["targetRoot"])
    state = read_state(root)
    if not state.get("active"):
        raise ApplyError("no active release to roll back")
    return do_rollback(root, state, args.reason or "operator rollback")


def action_status(args: argparse.Namespace, contract: dict[str, Any]) -> dict[str, Any]:
    root = Path(args.target_root or contract["targetRoot"])
    state = read_state(root)
    current = root / "current"
    state["currentLink"] = os.readlink(current) if current.is_symlink() else None
    state["releases"] = sorted(p.name for p in (root / "releases").iterdir()) if (root / "releases").is_dir() else []
    return state


def watchdog_expire(root: Path, version: str, now: datetime) -> dict[str, Any]:
    """One watchdog evaluation. Rolls back when the pending release passed its deadline."""
    state = read_state(root)
    if state.get("active") != version or state.get("status") != "pending-commit":
        return {"status": "idle", "reason": "release no longer pending"}
    deadline = datetime.fromisoformat(state["commitDeadline"])
    if now < deadline:
        return {"status": "waiting", "deadline": state["commitDeadline"]}
    return do_rollback(root, state, "watchdog: no commit before deadline")


def action_watchdog(args: argparse.Namespace, contract: dict[str, Any]) -> dict[str, Any]:
    root = Path(args.target_root or contract["targetRoot"])
    while True:
        result = watchdog_expire(root, args.version, utcnow())
        if result["status"] != "waiting":
            return result
        time.sleep(15)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Workshop Customizer host release applier")
    p.add_argument("--action", required=True, choices=["apply", "commit", "rollback", "status", "watchdog"])
    p.add_argument("--source", help="s3://… (or allow-listed file://…) URI of release.zip")
    p.add_argument("--sha256", help="expected SHA-256 of release.zip")
    p.add_argument("--version", help="expected release version (RELEASE.json version)")
    p.add_argument("--reason", help="rollback reason")
    p.add_argument("--deadline", help="watchdog deadline (internal)")
    p.add_argument("--watchdog-minutes", type=int, default=DEFAULT_WATCHDOG_MINUTES)
    p.add_argument("--no-watchdog", action="store_true", help="do not spawn the commit watchdog (tests)")
    p.add_argument("--target-root", help="override the contract target root (tests)")
    p.add_argument("--contract", default=CONTRACT_PATH, help="target contract path")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        contract = load_contract(args.contract)
        handler = {"apply": action_apply, "commit": action_commit, "rollback": action_rollback, "status": action_status, "watchdog": action_watchdog}[args.action]
        result = handler(args, contract)
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
        return 0 if result.get("status") not in ("smoke-failed",) else 2
    except ApplyError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, indent=2))
        return 1


if __name__ == "__main__":
    sys.exit(main())
