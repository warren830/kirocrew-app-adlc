"""Read-only routes the four-step UI reads: GET scenario (the live scenario.yaml as JSON) and GET files/<rel>."""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tests"))
from test_app_backend import Client, server_mod  # noqa: E402


@pytest.fixture()
def client(tmp_path):
    srv, _svc = server_mod.create_server(tmp_path / "data", home=REPO_ROOT)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    c = Client(srv.server_address[1])
    status, body, _ = c.call("POST", "/projects", {"id": "ui-read", "template": "it-helpdesk", "packKind": "reference"})
    assert status == 201, body
    yield c, tmp_path / "data" / "projects" / "ui-read"
    srv.shutdown()
    srv.server_close()


def test_scenario_view_is_the_live_yaml_as_json(client):
    c, pdir = client
    status, view, _ = c.call("GET", "/projects/ui-read/scenario")
    assert status == 200 and view["error"] is None
    assert view["scenario"] == yaml.safe_load((pdir / "scenario.yaml").read_text(encoding="utf-8"))
    assert view["scenario"]["labs"]["teaching"]["phenomena"]
    assert view["scenario"]["prompts"]["baselineFile"] in view["referenced"]
    assert set(view["referenced"]) <= set(view["files"]) and view["orphans"] == [] and len(view["sha256"]) == 64
    (pdir / "scenario.yaml").write_text("id: [unclosed\n", encoding="utf-8")
    status, broken, _ = c.call("GET", "/projects/ui-read/scenario")
    assert status == 200 and broken["scenario"] is None and "does not parse" in broken["error"]


def test_get_file_reads_pack_files_only(client):
    c, pdir = client
    rel = yaml.safe_load((pdir / "scenario.yaml").read_text(encoding="utf-8"))["prompts"]["baselineFile"]
    status, body, _ = c.call("GET", f"/projects/ui-read/files/{rel}")
    assert status == 200 and body["path"] == rel and body["content"] == (pdir / rel).read_text(encoding="utf-8")
    status, saved, _ = c.call("PUT", f"/projects/ui-read/files/{rel}", {"content": "# edited\n"})
    assert status == 200 and c.call("GET", f"/projects/ui-read/files/{rel}")[1]["content"] == "# edited\n"
    assert c.call("GET", "/projects/ui-read/files/agent/missing.md")[0] == 404
    for managed in ("project.json", "scenario.yaml", "build/validate.json", "materials/index.json", "../other/project.json"):
        assert c.call("GET", f"/projects/ui-read/files/{managed}")[0] in (400, 404), managed
    (pdir / "agent" / "binary.md").write_bytes(b"\xff\xfe\x00")
    assert c.call("GET", "/projects/ui-read/files/agent/binary.md")[0] == 415
