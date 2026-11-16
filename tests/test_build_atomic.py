"""A failed rebuild must not destroy the last good release (status, rollback and export read it)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tests"))
from test_app_backend import server_mod  # noqa: E402


def test_failed_rebuild_keeps_previous_release_and_build_record(tmp_path, monkeypatch):
    srv, svc = server_mod.create_server(tmp_path / "data", home=REPO_ROOT)
    srv.server_close()
    svc.create_project({"id": "atomic-build", "template": "it-helpdesk", "packKind": "reference"})
    first = svc.build("atomic-build")
    out = Path(first["releaseDir"]).parent
    build_json = (out / "build.json").read_text(encoding="utf-8")
    render = svc.engine["render"]
    assert render.verify_release(Path(first["releaseDir"]))["version"] == first["version"]

    def broken_render(pack, upstream, release_dir, *, template_commit):
        Path(release_dir).mkdir(parents=True)
        (Path(release_dir) / "half-written.txt").write_text("partial output", encoding="utf-8")
        raise RuntimeError("simulated render failure")

    monkeypatch.setattr(render, "render_release", broken_render)
    with pytest.raises(RuntimeError, match="simulated render failure"):
        svc.build("atomic-build")

    assert (out / "build.json").read_text(encoding="utf-8") == build_json
    assert render.verify_release(Path(first["releaseDir"]))["version"] == first["version"]
    assert not (Path(first["releaseDir"]) / "half-written.txt").exists()
    assert (out / "pack").is_dir() and (out / "instructor").is_dir()
    assert not [p.name for p in out.iterdir() if p.name.startswith((".staging-", ".retired-"))]
    assert svc.release_info("atomic-build")["build"]["version"] == first["version"]

    monkeypatch.undo()
    second = svc.build("atomic-build")
    assert second["version"] == first["version"] and json.loads((out / "build.json").read_text())["releaseDir"] == first["releaseDir"]
    assert not [p.name for p in out.iterdir() if p.name.startswith((".staging-", ".retired-"))]


def test_a_release_an_older_app_rendered_is_rebuilt_not_reused(tmp_path):
    # 0.4.9's render patches never reached a project built by 0.4.7: its sources were unchanged, so the
    # release counted as current and one-click reused it (VPC Harness CREATE_FAILED again).
    srv, svc = server_mod.create_server(tmp_path / "data", home=REPO_ROOT)
    srv.server_close()
    svc.create_project({"id": "stale-engine", "template": "it-helpdesk", "packKind": "reference"})
    first = svc.build("stale-engine")
    assert svc.release_info("stale-engine")["build"]["version"] == first["version"]
    path = Path(first["releaseDir"]).parent / "build.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    for stale in ({k: v for k, v in record.items() if k != "engine"}, {**record, "engine": "0" * 64}):
        path.write_text(json.dumps(stale), encoding="utf-8")
        with pytest.raises(server_mod.HttpError, match="rendered by another version of the app; rebuild"):
            svc.release_info("stale-engine")
    svc.build("stale-engine")
    assert svc.release_info("stale-engine")["build"]["engine"] == svc.engine_hash
