"""Batch SA review (POST /projects/{pid}/items/confirm-batch) and packKind sync on scenario save."""

from __future__ import annotations

import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tests"))
from test_app_backend import Client, server_mod  # noqa: E402
from test_sync import StubClients  # noqa: E402

PROJECT = "acme-review"
CONFIRMATION_KEYS = ("confirmedBy", "confirmedAt", "confirmationRef", "confirmedRef")


@pytest.fixture()
def env(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    stub = StubClients(expiry=datetime.now(timezone.utc) + timedelta(hours=2))
    srv, svc = server_mod.create_server(data_dir, home=REPO_ROOT, clients_factory=lambda cfg: stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    client = Client(srv.server_address[1])
    status, body, _ = client.call("POST", "/projects", {"id": PROJECT, "template": "it-helpdesk", "packKind": "reference", "displayName": "ACME Review"})
    assert status == 201, body
    yield {"client": client, "service": svc, "path": svc.store.project_dir(PROJECT) / "scenario.yaml"}
    srv.shutdown()
    srv.server_close()


def _load(env) -> dict:
    return yaml.safe_load(env["path"].read_text(encoding="utf-8"))


def _draft_everything(env) -> list[str]:
    """Reset every provenance-bearing item to ai_draft; return the ids the review endpoints accept."""
    data = _load(env)
    ids: list[str] = []
    for item in server_mod._all_items(data):
        item["provenance"] = "ai_draft"
        key = item.get("id") or item.get("name")
        if isinstance(key, str) and server_mod.ITEM_ID_RE.match(key) and key not in ids:
            ids.append(key)
    env["path"].write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return ids


def _batch(env, body: dict):
    status, payload, _ = env["client"].call("POST", f"/projects/{PROJECT}/items/confirm-batch", body)
    return status, payload


def test_batch_marks_every_selected_item_sa_synthetic(env):
    ids = _draft_everything(env)
    assert len(ids) >= 4, ids
    # A single customer confirmation first: the batch must never downgrade it (P4, SPEC D12).
    fact_id = next(f["id"] for f in _load(env).get("facts", []) if f.get("id") in ids)
    status, body, _ = env["client"].call(
        "POST",
        f"/projects/{PROJECT}/items/{fact_id}/confirm",
        {"provenance": "customer_confirmed", "confirmedBy": "Ops lead", "confirmationRef": "meeting 2026-09-25"},
    )
    assert status == 200 and body == {"itemId": fact_id, "provenance": "customer_confirmed"}, body
    fact = next(f for f in _load(env)["facts"] if f.get("id") == fact_id)
    assert fact["confirmedBy"] == "Ops lead" and fact["confirmationRef"] == "meeting 2026-09-25" and fact["confirmedAt"]

    before = env["path"].read_bytes()
    status, body = _batch(env, {"ids": ids, "acknowledged": True})
    assert status == 409 and "downgrade a customer confirmation" in body["error"] and body["itemIds"] == [fact_id], body
    assert env["path"].read_bytes() == before

    rest = [i for i in ids if i != fact_id]
    status, body = _batch(env, {"ids": rest, "acknowledged": True})
    assert status == 200, body
    assert body == {"confirmed": len(rest), "provenance": "sa_synthetic", "itemIds": rest}
    for item in server_mod._all_items(_load(env)):
        key = item.get("id") or item.get("name")
        if key in rest:
            assert item["provenance"] == "sa_synthetic", item
            assert not any(k in item for k in CONFIRMATION_KEYS), item
        elif key == fact_id:
            assert item["provenance"] == "customer_confirmed" and item["confirmedBy"] == "Ops lead"
    assert not list(env["path"].parent.glob(".scenario.yaml.*.tmp")), "atomic write left a temp file behind"


@pytest.mark.parametrize("ack", [None, False, "true", 1])
def test_batch_requires_boolean_true_acknowledgement(env, ack):
    ids = _draft_everything(env)
    before = env["path"].read_bytes()
    body = {"ids": ids} if ack is None else {"ids": ids, "acknowledged": ack}
    status, payload = _batch(env, body)
    assert status == 400 and "acknowledged" in str(payload), payload
    assert env["path"].read_bytes() == before


@pytest.mark.parametrize(
    "override",
    [
        {"provenance": "customer_confirmed", "confirmedBy": "x", "confirmationRef": "y"},
        {"ids": []},
        {"ids": "fact-1"},
        {"ids": ["bad id!"]},
        {"ids": ["dup", "dup"]},
    ],
)
def test_batch_rejects_bad_requests_without_writing(env, override):
    ids = _draft_everything(env)
    before = env["path"].read_bytes()
    status, payload = _batch(env, {"ids": ids, "acknowledged": True, **override})
    assert status == 400, payload
    assert env["path"].read_bytes() == before


def test_batch_is_all_or_nothing_when_an_id_is_unknown(env):
    ids = _draft_everything(env)
    before = env["path"].read_bytes()
    status, payload = _batch(env, {"ids": [*ids, "no-such-item"], "acknowledged": True})
    assert status == 404 and "no-such-item" in str(payload), payload
    assert env["path"].read_bytes() == before


def test_scenario_save_keeps_meta_pack_kind_in_step(env):
    client, svc = env["client"], env["service"]
    data = _load(env)
    for kind in ("customer", "reference"):
        data["packKind"] = kind
        status, body, _ = client.call("PUT", f"/projects/{PROJECT}/scenario", {"yaml": yaml.safe_dump(data, sort_keys=False, allow_unicode=True)})
        assert status == 200, body
        assert svc.store.read_meta(PROJECT)["packKind"] == kind
    data["packKind"] = "not-a-kind"  # saved as text, but never copied into the project record
    status, body, _ = client.call("PUT", f"/projects/{PROJECT}/scenario", {"yaml": yaml.safe_dump(data, sort_keys=False, allow_unicode=True)})
    assert status == 200, body
    assert svc.store.read_meta(PROJECT)["packKind"] == "reference"


def test_list_items_reports_kind_provenance_and_pack_kind(env):
    ids = _draft_everything(env)
    status, body, _ = env["client"].call("GET", f"/projects/{PROJECT}/items")
    assert status == 200, body
    assert body["packKind"] == "reference"
    kinds = {it["kind"] for it in body["items"]}
    assert {"fact", "tool", "golden"} <= kinds, kinds
    assert all(it["provenance"] == "ai_draft" for it in body["items"]), body["items"]
    assert {it["id"] for it in body["items"] if it["confirmable"]} == set(ids)
    assert all(isinstance(it["label"], str) and len(it["label"]) <= 200 for it in body["items"])
    status, _ = _batch(env, {"ids": ids[:2], "acknowledged": True})
    assert status == 200
    status, body, _ = env["client"].call("GET", f"/projects/{PROJECT}/items")
    assert status == 200, body
    assert set(ids[:2]) <= {it["id"] for it in body["items"] if it["provenance"] == "sa_synthetic"}
