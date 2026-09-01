"""The binding store survives a restart without being readable at rest."""

import json
import time

import pytest

from redmine_mcp_server._binding_store import BindingStore


@pytest.fixture
def store(tmp_path):
    return BindingStore(tmp_path / "bindings")


PAYLOAD = {
    "kind": "access",
    "login": "alr",
    "api_key": "a" * 40,
    "client_id": "client-1",
    "scopes": [],
    "expires_at": None,
    "resource": None,
}


def test_roundtrip(store):
    store.put_token("tok-1", PAYLOAD)
    assert store.get_token("tok-1") == PAYLOAD


def test_a_different_token_cannot_open_the_record(store):
    store.put_token("tok-1", PAYLOAD)
    assert store.get_token("tok-2") is None


def test_missing_token_returns_none(store):
    assert store.get_token("never-stored") is None


def test_the_file_reveals_neither_key_nor_token(store, tmp_path):
    store.put_token("tok-1", PAYLOAD)
    files = list((tmp_path / "bindings" / "tokens").glob("*.json"))
    assert len(files) == 1
    blob = files[0].read_text(encoding="utf-8")
    assert "tok-1" not in files[0].name  # named by hash
    assert "tok-1" not in blob
    assert PAYLOAD["api_key"] not in blob
    assert "alr" not in blob
    assert set(json.loads(blob)) == {"v", "salt", "nonce", "ct", "exp"}


def test_each_record_uses_a_fresh_salt_and_nonce(store, tmp_path):
    store.put_token("tok-1", PAYLOAD)
    first = json.loads(
        next((tmp_path / "bindings" / "tokens").glob("*.json")).read_text("utf-8")
    )
    store.put_token("tok-2", PAYLOAD)
    records = [
        json.loads(p.read_text("utf-8"))
        for p in (tmp_path / "bindings" / "tokens").glob("*.json")
    ]
    salts = {r["salt"] for r in records}
    nonces = {r["nonce"] for r in records}
    assert len(salts) == 2 and len(nonces) == 2
    assert first["salt"] in salts


def test_expired_record_is_refused_and_removed(store, tmp_path):
    store.put_token("tok-1", {**PAYLOAD, "expires_at": int(time.time()) - 5})
    assert store.get_token("tok-1") is None
    assert not list((tmp_path / "bindings" / "tokens").glob("*.json"))


def test_prune_removes_only_expired_records(store):
    store.put_token("live", {**PAYLOAD, "expires_at": int(time.time()) + 600})
    store.put_token("dead", {**PAYLOAD, "expires_at": int(time.time()) - 600})
    store.put_token("forever", PAYLOAD)  # expires_at None
    assert store.prune() == 1
    assert store.get_token("live") is not None
    assert store.get_token("forever") is not None
    assert store.get_token("dead") is None


def test_delete_token(store):
    store.put_token("tok-1", PAYLOAD)
    store.delete_token("tok-1")
    assert store.get_token("tok-1") is None


def test_corrupt_record_returns_none_rather_than_raising(store, tmp_path):
    store.put_token("tok-1", PAYLOAD)
    path = next((tmp_path / "bindings" / "tokens").glob("*.json"))
    record = json.loads(path.read_text("utf-8"))
    record["ct"] = record["ct"][:-8] + "AAAAAAAA"  # break the tag
    path.write_text(json.dumps(record), encoding="utf-8")
    assert store.get_token("tok-1") is None


def test_unknown_version_is_ignored(store, tmp_path):
    store.put_token("tok-1", PAYLOAD)
    path = next((tmp_path / "bindings" / "tokens").glob("*.json"))
    record = json.loads(path.read_text("utf-8"))
    record["v"] = 99
    path.write_text(json.dumps(record), encoding="utf-8")
    assert store.get_token("tok-1") is None


def test_client_registrations_roundtrip_in_the_clear(store):
    data = {"client_id": "client-1", "redirect_uris": ["http://localhost/cb"]}
    store.put_client("client-1", data)
    assert store.get_client("client-1") == data
    assert store.get_client("client-2") is None


def test_a_second_store_on_the_same_directory_sees_the_records(tmp_path):
    """What a restart amounts to: a new instance, the same directory."""
    BindingStore(tmp_path / "b").put_token("tok-1", PAYLOAD)
    assert BindingStore(tmp_path / "b").get_token("tok-1") == PAYLOAD
