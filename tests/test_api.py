from fastapi.testclient import TestClient

from maef_api.main import app

client = TestClient(app)


def test_health():
    result = client.get("/health")
    assert result.status_code == 200
    assert result.json()["execution_enabled"] is False


def test_agents():
    result = client.get("/api/v1/agents")
    assert result.status_code == 200
    assert {"research-analyst"} <= {a["id"] for a in result.json()}


def test_preview_clamps_budget():
    result = client.post(
        "/api/v1/runs/preview",
        json={
            "agent_id": "research-analyst",
            "instruction": "Summarise",
            "budget": {"max_tokens": 50000, "max_duration_seconds": 5000, "max_turns": 10},
        },
    )
    assert result.status_code == 200
    body = result.json()
    assert body["status"] == "preview_only"
    assert body["effective_budget"] == {"max_tokens": 50000, "max_duration_seconds": 900, "max_turns": 10}


def test_execution_disabled_by_default():
    result = client.post("/api/v1/runs", json={"agent_id": "research-analyst", "instruction": "Hi"})
    assert result.status_code == 501


def test_path_traversal_rejected():
    assert client.get("/api/v1/agents/..%2F..%2Fetc").status_code in (404, 400)


import pytest
from pydantic import ValidationError

from maef_api import main
from maef_api.registry import ModelProfileRegistry

PROFILE = """schema_version: maef.dev/v1alpha1
id: {id}
version: 0.1.0
display_name: Test Model
vendor: Test
api_dialect: bedrock-converse
endpoint: {{kind: amazon-bedrock, region: eu-north-1, model_id: global.test.model-1, routing: global}}
credential_ref: aws:default-chain
{extra}"""


@pytest.fixture
def profiles_dir(tmp_path, monkeypatch):
    # Real profiles live in the gitignored model-profiles/ dir; tests use their own.
    (tmp_path / "test-model.yaml").write_text(PROFILE.format(id="test-model", extra=""))
    monkeypatch.setattr(main, "model_profiles", ModelProfileRegistry(tmp_path))
    return tmp_path


def test_model_profiles_listed(profiles_dir):
    result = client.get("/api/v1/model-profiles")
    assert result.status_code == 200
    assert [p["id"] for p in result.json()] == ["test-model"]


def test_model_profile_has_no_secrets(profiles_dir):
    body = client.get("/api/v1/model-profiles/test-model").json()
    assert body["endpoint"] == {
        "kind": "amazon-bedrock",
        "region": "eu-north-1",
        "model_id": "global.test.model-1",
        "routing": "global",
    }
    assert body["credential_ref"] == "aws:default-chain"


def test_model_profile_unknown_and_traversal(profiles_dir):
    assert client.get("/api/v1/model-profiles/nope").status_code == 404
    assert client.get("/api/v1/model-profiles/..%2Fagents").status_code in (404, 400)


def test_model_profiles_missing_dir_is_empty(tmp_path):
    assert ModelProfileRegistry(tmp_path / "absent").list_profiles() == []


def test_model_profile_rejects_unknown_fields(tmp_path):
    (tmp_path / "x.yaml").write_text(PROFILE.format(id="x-model", extra="api_key: sk-leak\n"))
    with pytest.raises(ValidationError):
        ModelProfileRegistry(tmp_path).list_profiles()
