"""Tokens, scopes, worker identity, audit and the admin CLI (CONTRACTS.md "Auth and audit").

conftest seeds an admin token and every TestClient sends it, so these tests remove the header
(`anon`) or replace it (`bearer`, `with_auth`) explicitly.
"""

import hashlib

import pytest
from fastapi.testclient import TestClient
from maef_api import cli, main
from maef_api.auth import (
    SCOPE_ADMIN,
    SCOPE_AUTHORING,
    SCOPE_DECISIONS,
    SCOPE_READ,
    SCOPE_RUNS,
    hash_token,
    issue_api_token,
    issue_worker_credential,
)
from maef_api.client import AsgiTransport, MaefClient, MaefClientError
from maef_api.definitions import compute_definition_digest
from maef_api.ledger import Ledger
from maef_api.models import Budget
from maef_api.runs import Runs
from test_run_execution import definition

RUN_BODY = {"agent_id": "research-analyst", "instruction": "Hi"}

# (method, route template, concrete path, required scope, request kwargs). Mirrors the scope table in
# packages/contracts/CONTRACTS.md; `/api/v1/worker/*` is covered by its own tests.
ROUTES = [
    ("GET", "/api/v1/audit", "/api/v1/audit", SCOPE_ADMIN, {}),
    ("GET", "/api/v1/metrics", "/api/v1/metrics", SCOPE_READ, {}),
    ("GET", "/api/v1/agents", "/api/v1/agents", SCOPE_READ, {}),
    ("GET", "/api/v1/agents/{agent_id}", "/api/v1/agents/research-analyst", SCOPE_READ, {}),
    ("GET", "/api/v1/agents/{agent_id}/files", "/api/v1/agents/research-analyst/files", SCOPE_READ, {}),
    ("GET", "/api/v1/model-profiles", "/api/v1/model-profiles", SCOPE_READ, {}),
    ("GET", "/api/v1/model-profiles/{profile_id}", "/api/v1/model-profiles/nope", SCOPE_READ, {}),
    ("GET", "/api/v1/schemas/{name}", "/api/v1/schemas/nope", SCOPE_READ, {}),
    ("POST", "/api/v1/runs/preview", "/api/v1/runs/preview", SCOPE_READ, {"json": RUN_BODY}),
    ("GET", "/api/v1/runs", "/api/v1/runs", SCOPE_READ, {}),
    ("GET", "/api/v1/runs/{run_id}", "/api/v1/runs/nope", SCOPE_READ, {}),
    ("GET", "/api/v1/runs/{run_id}/events", "/api/v1/runs/nope/events", SCOPE_READ, {}),
    ("GET", "/api/v1/runs/{run_id}/artifacts", "/api/v1/runs/nope/artifacts", SCOPE_READ, {}),
    ("GET", "/api/v1/artifacts/{digest}", "/api/v1/artifacts/nope", SCOPE_READ, {}),
    ("POST", "/api/v1/runs", "/api/v1/runs", SCOPE_RUNS, {"json": RUN_BODY}),
    ("POST", "/api/v1/runs/{run_id}/cancel", "/api/v1/runs/nope/cancel", SCOPE_RUNS, {}),
    ("POST", "/api/v1/decisions", "/api/v1/decisions", SCOPE_DECISIONS, {"json": {}}),
    ("GET", "/api/v1/drafts", "/api/v1/drafts", SCOPE_AUTHORING, {}),
    ("POST", "/api/v1/drafts", "/api/v1/drafts", SCOPE_AUTHORING, {"json": {"agent_id": "research-analyst"}}),
    ("GET", "/api/v1/drafts/{draft_id}/review", "/api/v1/drafts/d1/review", SCOPE_AUTHORING, {}),
    (
        "PUT",
        "/api/v1/drafts/{draft_id}/files/{path:path}",
        "/api/v1/drafts/d1/files/agents/research-analyst/SYSTEM.md",
        SCOPE_AUTHORING,
        {"content": b"x"},
    ),
    (
        "POST",
        "/api/v1/drafts/{draft_id}/publish",
        "/api/v1/drafts/d1/publish",
        SCOPE_AUTHORING,
        {"json": {"message": "m"}},
    ),
    ("DELETE", "/api/v1/drafts/{draft_id}", "/api/v1/drafts/d1", SCOPE_AUTHORING, {}),
]


def anon(**kwargs) -> TestClient:
    client = TestClient(main.app, **kwargs)
    client.headers.pop("Authorization", None)
    return client


def with_auth(value: str, **kwargs) -> TestClient:
    client = anon(**kwargs)
    client.headers["Authorization"] = value
    return client


def bearer(token: str, **kwargs) -> TestClient:
    return with_auth(f"Bearer {token}", **kwargs)


def audit_rows(store) -> list[dict]:
    return store.audit_after(0, 1000)


@pytest.fixture
def store(ledger_target, monkeypatch):
    ledger = Ledger(ledger_target("auth.db"))
    ledger.ensure_account("default", 1_000_000)
    monkeypatch.setattr(main, "ledger", ledger)
    return ledger


@pytest.fixture
def worker_api(store, monkeypatch):
    monkeypatch.setattr(
        main, "runs", Runs(main.runs.definitions, store, execution_enabled=lambda: True, allow_dirty=lambda: True)
    )
    return store


def admit(store) -> str:
    d = definition()
    d = d.model_copy(update={"definition_digest": compute_definition_digest(d.definition_bytes, d.files)})
    return store.admit(d, Budget(max_tokens=500_000, max_turns=1), prompt_bytes=28, instruction="Question?")


# --- Tokens ----------------------------------------------------------------------------------------


def test_token_prefixes_and_only_hashes_stored(tmp_path, store):
    _, token = issue_api_token(store, "ci", (SCOPE_READ,))
    worker_token = issue_worker_credential(store, "w-prefix")
    assert token.startswith("maef_") and worker_token.startswith("maefw_")
    if store.backend != "sqlite":
        pytest.skip("inspects the SQLite database files")
    blob = b"".join(p.read_bytes() for p in tmp_path.glob("auth.db*"))  # includes the WAL file
    for secret in (token, worker_token):
        assert secret.encode() not in blob
        assert hash_token(secret).encode() in blob  # positive control: the hash is what is stored


def test_revoked_token_is_401(store):
    token_id, token = issue_api_token(store, "gone", (SCOPE_ADMIN,))
    assert bearer(token).get("/api/v1/agents").status_code == 200
    assert store.revoke_api_token(token_id)
    assert bearer(token).get("/api/v1/agents").status_code == 401


def test_unknown_token_is_401_with_bearer_challenge(store):
    response = bearer("maef_never-issued").get("/api/v1/agents")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize("value", ["Basic dXNlcjpwYXNz", "Bearer"])
def test_malformed_authorization_is_401(store, value):
    response = with_auth(value).get("/api/v1/agents")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_health_is_open_and_reports_auth_required(monkeypatch):
    monkeypatch.delenv("MAEF_AUTH_DISABLED", raising=False)
    response = anon().get("/health")
    assert response.status_code == 200
    assert response.json()["auth_required"] is True


# --- Scope matrix ----------------------------------------------------------------------------------


@pytest.mark.parametrize("method,template,path,scope,kwargs", ROUTES, ids=[f"{m} {t}" for m, t, _, _, _ in ROUTES])
def test_scope_matrix(store, method, template, path, scope, kwargs):
    other = SCOPE_RUNS if scope == SCOPE_READ else SCOPE_READ
    _, under_scoped = issue_api_token(store, "under", (other,))
    _, right_scope = issue_api_token(store, "right", (scope,))
    _, admin = issue_api_token(store, "root", (SCOPE_ADMIN,))

    assert anon().request(method, path, **kwargs).status_code == 401
    assert bearer(under_scoped).request(method, path, **kwargs).status_code == 403
    assert bearer(right_scope).request(method, path, **kwargs).status_code not in (401, 403)
    assert bearer(admin).request(method, path, **kwargs).status_code not in (401, 403)


def test_every_api_route_is_in_scope_matrix():
    listed = {(m, t) for m, t, _, _, _ in ROUTES}
    found = set()
    for route in main.app.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/api/v1/") or path.startswith("/api/v1/worker/"):
            continue
        for method in getattr(route, "methods", None) or ():
            if method not in ("HEAD", "OPTIONS"):
                found.add((method, path))
    assert found - listed == set(), "API routes missing from ROUTES"
    assert listed - found == set(), "ROUTES entries with no matching route"


# --- Worker routes ---------------------------------------------------------------------------------

HEARTBEAT = {"fencing_token": 1}
EVENT = {"fencing_token": 1, "wseq": 1, "type": "run.started", "payload": {}}
FINISH = {"fencing_token": 1, "tokens_used": 0, "status": "failed", "terminal_reason": "x"}


def upload(client: TestClient, run_id: str, data: bytes = b"x", token: int = 1):
    headers = {
        "X-Fencing-Token": str(token),
        "X-Content-Digest": "sha256:" + hashlib.sha256(data).hexdigest(),
        "Content-Type": "application/octet-stream",
    }
    return client.post(
        f"/api/v1/worker/runs/{run_id}/artifacts",
        params={"name": "out.txt", "mime": "text/plain"},
        content=data,
        headers=headers,
    )


def worker_calls(client: TestClient, run_id: str):
    return {
        "claim": client.post("/api/v1/worker/claim", json={}),
        "heartbeat": client.post(f"/api/v1/worker/runs/{run_id}/heartbeat", json=HEARTBEAT),
        "events": client.post(f"/api/v1/worker/runs/{run_id}/events", json=EVENT),
        "finish": client.post(f"/api/v1/worker/runs/{run_id}/finish", json=FINISH),
        "artifact": upload(client, run_id),
    }


def test_api_tokens_never_reach_worker_routes(worker_api, store):
    run_id = admit(store)
    _, admin = issue_api_token(store, "root", (SCOPE_ADMIN,))
    _, reader = issue_api_token(store, "reader", (SCOPE_READ,))
    for token in (admin, reader):
        for name, response in worker_calls(bearer(token), run_id).items():
            assert response.status_code == 401, name


def test_other_worker_gets_409_on_lease_and_claim_ignores_body(worker_api, store):
    run_id = admit(store)
    a_token = issue_worker_credential(store, "wA")
    b_token = issue_worker_credential(store, "wB")

    claim = bearer(a_token).post("/api/v1/worker/claim", json={"worker_id": "wB"})
    assert claim.status_code == 200 and claim.json()["run_id"] == run_id
    fencing = claim.json()["fencing_token"]
    granted = [e.payload for e in store.get(run_id).events if e.type == "lease.granted"]
    assert granted == [{"worker_id": "wA", "fencing_token": fencing}]

    other = bearer(b_token)
    responses = {
        "heartbeat": other.post(f"/api/v1/worker/runs/{run_id}/heartbeat", json={"fencing_token": fencing}),
        "events": other.post(f"/api/v1/worker/runs/{run_id}/events", json={**EVENT, "fencing_token": fencing}),
        "finish": other.post(f"/api/v1/worker/runs/{run_id}/finish", json={**FINISH, "fencing_token": fencing}),
        "artifact": upload(other, run_id, token=fencing),
    }
    for name, response in responses.items():
        assert response.status_code == 409, name
        assert response.json()["detail"]["code"] == "fencing", name
    assert store.get(run_id).status == "leased"

    holder = bearer(a_token).post(f"/api/v1/worker/runs/{run_id}/heartbeat", json={"fencing_token": fencing})
    assert holder.status_code == 200


def test_revoked_worker_is_401(worker_api, store):
    run_id = admit(store)
    token = issue_worker_credential(store, "wR")
    assert bearer(token).post("/api/v1/worker/claim", json={}).status_code == 200
    store.revoke_worker("wR")
    assert bearer(token).post(f"/api/v1/worker/runs/{run_id}/heartbeat", json=HEARTBEAT).status_code == 401
    assert bearer(token).post("/api/v1/worker/claim", json={}).status_code == 401


# --- MAEF_AUTH_DISABLED ----------------------------------------------------------------------------


def test_auth_disabled_trusts_only_loopback_clients(store, monkeypatch):
    monkeypatch.setenv("MAEF_AUTH_DISABLED", "1")
    assert anon().get("/health").json()["auth_required"] is False

    remote = anon()  # TestClient's default host is 'testclient', which is not loopback
    refused = remote.get("/api/v1/agents")
    assert refused.status_code == 403
    assert "loopback" in refused.json()["detail"]

    loopback = anon(client=("127.0.0.1", 1234))
    assert loopback.get("/api/v1/agents").status_code == 200
    assert loopback.post("/api/v1/runs", json=RUN_BODY).status_code == 501  # passed auth; execution is off
    assert [r["principal"] for r in audit_rows(store) if r["action"] == "POST /api/v1/runs"] == ["dev-unauthenticated"]

    assert loopback.post("/api/v1/worker/claim", json={}).status_code == 401  # workers always need a credential


def test_auth_disabled_never_grants_admin_or_accepts_bad_tokens(store, monkeypatch):
    monkeypatch.setenv("MAEF_AUTH_DISABLED", "1")
    loopback = anon(client=("127.0.0.1", 1234))
    assert loopback.get("/api/v1/audit").status_code == 403
    assert loopback.get("/api/v1/agents", headers={"Authorization": "Bearer maef_bogus"}).status_code == 401
    assert loopback.get("/api/v1/agents", headers={"Authorization": "Basic x"}).status_code == 401


# --- Audit -----------------------------------------------------------------------------------------


def test_successful_submit_is_audited_with_token_principal(live_api):
    store = live_api.ledger
    token_id, token = issue_api_token(store, "submitter", (SCOPE_RUNS, SCOPE_READ))
    response = bearer(token).post("/api/v1/runs", json={"agent_id": "research-analyst", "instruction": "Hello"})
    assert response.status_code == 202
    rows = [r for r in audit_rows(store) if r["action"] == "POST /api/v1/runs"]
    assert len(rows) == 1
    assert (rows[0]["principal"], rows[0]["target"], rows[0]["outcome"]) == (
        f"token:{token_id}",
        "/api/v1/runs",
        "authorized",
    )
    created = [r for r in audit_rows(store) if r["action"] == "run.created"]
    assert [(r["principal"], r["target"]) for r in created] == [(f"token:{token_id}", response.json()["run_id"])]


def test_denials_are_audited(store):
    anon().get("/api/v1/agents")
    token_id, token = issue_api_token(store, "reader", (SCOPE_READ,))
    bearer(token).get("/api/v1/audit")
    rows = audit_rows(store)
    assert any(
        r["principal"] == "anonymous" and r["action"] == "GET /api/v1/agents" and r["outcome"] == "denied:401"
        for r in rows
    )
    assert any(
        r["principal"] == f"token:{token_id}" and r["action"] == "GET /api/v1/audit" and r["outcome"] == "denied:403"
        for r in rows
    )


def test_audit_rows_never_contain_tokens_or_bodies(live_api):
    store = live_api.ledger
    _, token = issue_api_token(store, "submitter", (SCOPE_RUNS,))
    marker = "MARKER-BODY-7f3a91"
    bearer(token).post("/api/v1/runs", json={"agent_id": "research-analyst", "instruction": marker})
    bogus = "maef_BOGUS-SECRET-VALUE-42"
    with_auth(f"Bearer {bogus}").post("/api/v1/runs", json={"agent_id": "research-analyst", "instruction": marker})
    dump = repr(audit_rows(store))
    assert token not in dump and bogus not in dump and marker not in dump
    assert len(audit_rows(store)) >= 2


def test_audit_table_is_append_only(store):
    store.audit("p", "a", "t", "ok", "c")
    with store.connect() as conn:
        with pytest.raises(store.database_error):
            conn.execute("UPDATE audit_log SET outcome='changed'")
        with pytest.raises(store.database_error):
            conn.execute("DELETE FROM audit_log")


def test_get_audit_requires_admin_and_pages(store):
    store.audit("p", "a", "t", "ok", "c")
    for i in range(3):
        store.audit(f"p{i}", "a", "t", "ok", "c")
    _, admin = issue_api_token(store, "root", (SCOPE_ADMIN,))
    _, reader = issue_api_token(store, "reader", (SCOPE_READ,))
    assert anon().get("/api/v1/audit").status_code == 401
    assert bearer(reader).get("/api/v1/audit").status_code == 403

    first_page = bearer(admin).get("/api/v1/audit", params={"after": 0, "limit": 2}).json()
    assert len(first_page) == 2 and first_page[0]["seq"] < first_page[1]["seq"]
    rest = bearer(admin).get("/api/v1/audit", params={"after": first_page[-1]["seq"], "limit": 100}).json()
    assert rest and all(r["seq"] > first_page[-1]["seq"] for r in rest)
    assert bearer(admin).get("/api/v1/audit", params={"limit": 0}).status_code == 422


# --- Admin CLI -------------------------------------------------------------------------------------


def test_cli_token_create_list_revoke(store, monkeypatch, capsys):
    monkeypatch.setenv("MAEF_DB", store.url)
    assert cli.main(["admin", "token", "create", "--name", "ops", "--scopes", "read"]) == 0
    out, err = capsys.readouterr()
    token = out.strip()
    assert token.startswith("maef_") and out.count("\n") == 1
    assert token not in err
    token_id = next(t["id"] for t in store.list_api_tokens() if t["name"] == "ops")
    assert bearer(token).get("/api/v1/agents").status_code == 200

    assert cli.main(["admin", "token", "list"]) == 0
    listed = capsys.readouterr().out
    assert token_id in listed and "ops" in listed and "read" in listed and token not in listed

    assert cli.main(["admin", "token", "revoke", token_id]) == 0
    capsys.readouterr()
    assert bearer(token).get("/api/v1/agents").status_code == 401

    cli_rows = [(r["action"], r["target"], r["outcome"]) for r in audit_rows(store) if r["principal"] == "local-cli"]
    assert cli_rows == [("token.create", f"token:{token_id}", "ok"), ("token.revoke", f"token:{token_id}", "ok")]


def test_cli_worker_create_list_revoke(worker_api, monkeypatch, capsys):
    store = worker_api
    monkeypatch.setenv("MAEF_DB", store.url)
    assert cli.main(["admin", "worker", "create", "--id", "wcli"]) == 0
    out, err = capsys.readouterr()
    token = out.strip()
    assert token.startswith("maefw_") and token not in err

    assert cli.main(["admin", "worker", "list"]) == 0
    listed = capsys.readouterr().out
    assert "wcli" in listed and token not in listed

    assert bearer(token).post("/api/v1/worker/claim", json={}).status_code == 204  # credential accepted
    assert cli.main(["admin", "worker", "revoke", "wcli"]) == 0
    capsys.readouterr()
    assert bearer(token).post("/api/v1/worker/claim", json={}).status_code == 401

    cli_rows = [(r["action"], r["target"], r["outcome"]) for r in audit_rows(store) if r["principal"] == "local-cli"]
    assert cli_rows == [("worker.create", "worker:wcli", "ok"), ("worker.revoke", "worker:wcli", "ok")]


# --- Client ----------------------------------------------------------------------------------------


def test_client_without_token_gets_hint(store, monkeypatch):
    monkeypatch.delenv("MAEF_API_TOKEN", raising=False)
    transport = AsgiTransport(main.app, token="")
    transport._client.headers.pop("Authorization", None)
    with pytest.raises(MaefClientError) as exc:
        MaefClient(transport).list_agents()
    assert exc.value.status == 401
    assert "MAEF_API_TOKEN" in exc.value.message
