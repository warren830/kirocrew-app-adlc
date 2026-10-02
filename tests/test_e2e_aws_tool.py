"""tools/e2e_aws.py: per-project evidence directories, --template, and the offline ``rehearse`` action."""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest

import teaching_run as tr
from test_app_backend import server_mod
from test_sync import ACCOUNT
from workshop_customizer.guided_run_store import GuidedRunStore

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_tool():
    spec = importlib.util.spec_from_file_location("wc_e2e_aws_tool", REPO_ROOT / "tools" / "e2e_aws.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


e2e = _load_tool()


def test_evidence_directory_is_per_project_and_keeps_the_original_path():
    root = Path("/tmp/aws-e2e")
    assert e2e.evidence_dir("aws-it-helpdesk", root) == root
    assert e2e.evidence_dir("aws-ops-support", root) == root / "aws-ops-support"
    assert e2e.evidence_dir(e2e.LEGACY_PROJECT) == e2e.E2E_ROOT == REPO_ROOT / "build" / "aws-e2e"


def test_parser_offers_rehearse_and_the_reference_templates():
    parser = e2e.build_parser()
    args = parser.parse_args(["prepare", "--profile", "p", "--account", ACCOUNT])
    assert (args.template, args.project) == ("it-helpdesk", "aws-it-helpdesk")
    assert set(e2e.templates()) >= {"hr-default", "it-helpdesk", "maintenance"}
    args = parser.parse_args(["rehearse", "--profile", "p", "--account", ACCOUNT, "--project", "aws-ops-support", "--template", "maintenance"])
    assert (args.action, args.template) == ("rehearse", "maintenance")
    with pytest.raises(SystemExit):
        parser.parse_args(["prepare", "--profile", "p", "--account", ACCOUNT, "--template", "blank"])


def test_rehearse_posts_the_rehearsal_and_saves_it_in_the_project_evidence(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(e2e, "E2E_ROOT", tmp_path)
    service = server_mod.Service(tmp_path / "appdata", home=REPO_ROOT)
    pid = "aws-hr-rehearsal"
    service.create_project({"id": pid, "template": "hr-default", "packKind": "reference"})
    service.put_target(pid, {"profile": "p", "region": "us-west-2", "expectedAccountId": ACCOUNT})
    build = service.build(pid)
    store = GuidedRunStore(tmp_path / "appdata" / "projects" / pid)
    state = store.create(project_id=pid, release_version=build["version"], template_commit=build["templateCommit"])
    steps = tr.load_steps("hr-boundary-sp2")
    state["steps"] = {sid: copy.deepcopy(steps[sid]) for sid in state["stepOrder"]}
    state["status"] = "passed"
    store.save(state)

    code = e2e.main(["rehearse", "--profile", "p", "--account", ACCOUNT, "--project", pid])
    saved = json.loads((tmp_path / pid / "rehearsal.json").read_text(encoding="utf-8"))
    assert code == 3 and saved["verdict"] == "not_ready" and saved["releaseVersion"] == build["version"]
    printed = json.loads(capsys.readouterr().out)
    assert printed["phenomena"]["sick-leave-retrieval-gap"] == "not_reproduced" and printed["readyForClass"] is False
    persisted = json.loads((tmp_path / "appdata" / "projects" / pid / "run" / "rehearsal.json").read_text(encoding="utf-8"))
    assert persisted == saved

    with pytest.raises(RuntimeError, match="saved target differs"):
        e2e.main(["rehearse", "--profile", "other", "--account", ACCOUNT, "--project", pid])


def test_poll_retries_a_transient_network_error_then_raises_the_rest(monkeypatch):
    tool = e2e
    monkeypatch.setattr(tool.time, "sleep", lambda _s: None)
    calls = []

    def flaky(method, path, body=None):
        calls.append(path)
        if len(calls) == 1:
            raise RuntimeError('HTTP 409: {"error": "could not poll step optimize: SSLError: SSL validation failed"}')
        return {"ok": True}

    assert tool.poll_with_retry(flaky, "/projects/x") == {"ok": True} and len(calls) == 2

    def denied(method, path, body=None):
        raise RuntimeError('HTTP 409: {"error": "the Guided Run is bound to a different release"}')

    with pytest.raises(RuntimeError, match="different release"):
        tool.poll_with_retry(denied, "/projects/x")
