import asyncio
import json
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .adapters.decision import DecisionRunRequest
from .artifacts import (
    DIGEST_RE,
    MAX_ARTIFACT_BYTES,
    ArtifactCorrupt,
    ArtifactIntake,
    ArtifactNotFound,
    ArtifactRef,
    ArtifactRejected,
    ArtifactStoreError,
    default_artifact_store,
)
from .auth import (
    ANONYMOUS,
    DEV_PRINCIPAL,
    SCOPE_ADMIN,
    SCOPE_AUTHORING,
    SCOPE_DECISIONS,
    SCOPE_READ,
    SCOPE_RUNS,
    SCOPES,
    Principal,
    bearer,
    verify_api_token,
    verify_worker_token,
)
from .definitions import Definitions
from .drafts import MAX_FILE_BYTES, DraftNotFound, DraftRejected, Drafts
from .leases import FencingError, SequenceGap
from .ledger import Ledger
from .metrics import CONTENT_TYPE as METRICS_CONTENT_TYPE
from .metrics import render_prometheus
from .models import (
    AgentDefinition,
    ModelEndpointProfile,
    RunDetail,
    RunPreview,
    RunRecord,
    RunRequest,
    is_loopback_host,
)
from .otel import OtlpExportError, export_pending, exporter_from_env
from .project import project_root
from .registry import (
    AgentRegistry,
    McpServerRegistry,
    ModelProfileRegistry,
    SandboxProfileRegistry,
)
from .runs import (
    AdmissionRejected,
    AgentNotFound,
    DefinitionConflict,
    ExecutionDisabled,
    NotADecisionProfile,
    ProfileNotFound,
    RunAlreadyTerminal,
    RunNotFound,
    Runs,
    UnsupportedDialect,
)
from .schemas import schema_for

SWEEP_SECONDS = 10
OTLP_EXPORT_SECONDS = 15


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Startup reclaims lapsed leases and a daemon thread keeps sweeping; nothing runs at import."""
    runs.recover()
    if auth_disabled():
        logging.getLogger("maef.api").warning(
            "MAEF_AUTH_DISABLED=1: API-token checks are off for loopback clients. Never use this on a shared host."
        )
    stop = threading.Event()

    def sweep() -> None:
        while not stop.wait(SWEEP_SECONDS):
            try:
                runs.recover()
            except Exception as exc:  # noqa: BLE001  # keep sweeping; the next tick retries
                logging.getLogger("maef.api").warning("lease sweep failed: %s", type(exc).__name__)

    thread = threading.Thread(target=sweep, name="maef-lease-sweep", daemon=True)
    thread.start()
    threads = [thread]
    try:
        exporter = exporter_from_env()
    except OtlpExportError as exc:
        exporter = None
        logging.getLogger("maef.api").warning("MAEF_OTLP_ENDPOINT ignored: %s", exc)
    if exporter is not None:

        def export() -> None:
            while not stop.wait(OTLP_EXPORT_SECONDS):
                try:
                    export_pending(ledger, exporter)
                except Exception as exc:  # noqa: BLE001  # never affects runs; the next tick retries
                    logging.getLogger("maef.api").warning("otlp export tick failed: %s", type(exc).__name__)

        threads.append(threading.Thread(target=export, name="maef-otlp-export", daemon=True))
        threads[-1].start()
    try:
        yield
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=5)


app = FastAPI(title="MAEF Control Plane", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "Last-Event-ID", "Authorization"],
)
registry = AgentRegistry()
model_profiles = ModelProfileRegistry()


def execution_enabled() -> bool:
    # Off by default: the API must be opted in to spend money.
    return os.environ.get("MAEF_EXECUTION_ENABLED") == "1"


def auth_disabled() -> bool:
    # Dev escape hatch, honoured only for loopback clients (see `authorize`).
    return os.environ.get("MAEF_AUTH_DISABLED") == "1"


def authoring_enabled() -> bool:
    # Off by default: draft routes write to the repository.
    return os.environ.get("MAEF_AUTHORING_ENABLED") == "1"


def allow_dirty() -> bool:
    return os.environ.get("MAEF_ALLOW_DIRTY") == "1"


# The API serves one project, resolved when the app starts (MAEF_HOME, the folder's maef.toml, or the checkout).
PROJECT = project_root()

# Ledger lives under the gitignored runs/ directory unless overridden.
ledger = Ledger.open(os.environ.get("MAEF_DB") or PROJECT / "runs" / "maef.db")
ledger.ensure_account("default", int(os.environ.get("MAEF_ACCOUNT_LIMIT_TOKENS", "5000000")))
artifacts = default_artifact_store()
runs = Runs(
    Definitions(registry, model_profiles, SandboxProfileRegistry(), McpServerRegistry()),
    ledger,
    execution_enabled=lambda: execution_enabled(),
    allow_dirty=lambda: allow_dirty(),
)

drafts = Drafts(PROJECT)

# --- Authentication and audit (BACKEND_PLAN step 11; scope table in packages/contracts/CONTRACTS.md) ---

_log = logging.getLogger("maef.api")


def _client_host(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _is_loopback_client(host: str) -> bool:
    return is_loopback_host(host.removeprefix("::ffff:"))


def _audit(principal: str, request: Request, outcome: str, *, required: bool) -> None:
    """One audit row: route template, concrete path, outcome, client address. Never bodies or tokens.
    A mutating request that cannot be audited is refused; a failed denial record is only logged."""
    route = request.scope.get("route")
    action = f"{request.method} {getattr(route, 'path', request.url.path)}"
    try:
        ledger.audit(principal, action, request.url.path, outcome, _client_host(request))
    except Exception as exc:  # noqa: BLE001
        _log.error("audit write failed: %s", type(exc).__name__)
        if required:
            raise HTTPException(status_code=500, detail="Audit log unavailable; request refused.") from None


def _deny(request: Request, principal: str, status: int, detail: str) -> HTTPException:
    _audit(principal, request, f"denied:{status}", required=False)
    return HTTPException(
        status_code=status, detail=detail, headers={"WWW-Authenticate": "Bearer"} if status == 401 else None
    )


def authorize(scope: str, *, audit: bool = False):
    """Dependency factory: the request needs `Authorization: Bearer <token>` holding `scope`
    (`admin` implies all). `audit=True` records the request, for mutating routes."""

    def dependency(request: Request) -> Principal:
        host = _client_host(request)
        header = request.headers.get("authorization")
        principal = verify_api_token(ledger, bearer(header))
        # The dev hatch covers only requests with NO credentials: anything presented must be valid.
        if principal is None and header is None and auth_disabled():
            if _is_loopback_client(host):
                # Never admin: audit and token management always need a real admin token.
                principal = Principal(DEV_PRINCIPAL, frozenset(s for s in SCOPES if s != SCOPE_ADMIN))
            else:
                raise _deny(
                    request, ANONYMOUS, 403, "MAEF_AUTH_DISABLED only applies to loopback clients; send a valid token."
                )
        if principal is None:
            raise _deny(request, ANONYMOUS, 401, "Missing, invalid or revoked API token.")
        if not principal.allows(scope):
            raise _deny(request, principal.name, 403, f"Token lacks the required scope: {scope}.")
        if audit:
            # Written before the handler runs: it means authorized, not succeeded. Handlers that
            # create runs add a `run.created` row with the run id.
            _audit(principal.name, request, "authorized", required=True)
        return principal

    return dependency


read_access = Depends(authorize(SCOPE_READ))
runs_write = Depends(authorize(SCOPE_RUNS, audit=True))
decisions_write = Depends(authorize(SCOPE_DECISIONS, audit=True))
authoring_read = Depends(authorize(SCOPE_AUTHORING))
authoring_write = Depends(authorize(SCOPE_AUTHORING, audit=True))
admin_access = Depends(authorize(SCOPE_ADMIN))


@app.get("/health")
def health():
    return {"status": "ok", "execution_enabled": execution_enabled(), "auth_required": not auth_disabled()}


@app.get("/api/v1/audit", dependencies=[admin_access])
def get_audit(after: Annotated[int, Query(ge=0)] = 0, limit: Annotated[int, Query(ge=1, le=1000)] = 100):
    return ledger.audit_after(after, limit)


@app.get("/api/v1/metrics", response_class=Response, dependencies=[read_access])
def get_metrics():
    return Response(content=render_prometheus(ledger.metrics()), media_type=METRICS_CONTENT_TYPE)


@app.get("/api/v1/agents", response_model=list[AgentDefinition], dependencies=[read_access])
def list_agents():
    return registry.list_agents()


@app.get("/api/v1/agents/{agent_id}", response_model=AgentDefinition, dependencies=[read_access])
def get_agent(agent_id: str):
    agent = registry.get_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent


@app.get("/api/v1/model-profiles", response_model=list[ModelEndpointProfile], dependencies=[read_access])
def list_model_profiles():
    return model_profiles.list_profiles()


@app.get("/api/v1/model-profiles/{profile_id}", response_model=ModelEndpointProfile, dependencies=[read_access])
def get_model_profile(profile_id: str):
    profile = model_profiles.get_profile(profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Model profile not found")
    return profile


@app.get("/api/v1/agents/{agent_id}/files", dependencies=[read_access])
def agent_files(agent_id: str):
    agent = registry.get_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    base = registry.root / agent_id
    paths = [agent.instructions, *agent.skills]
    results = []
    for relative in paths:
        path = (base / relative).resolve()
        if not path.is_relative_to(base.resolve()):
            continue
        if path.is_file():
            results.append({"path": relative, "content": path.read_text(encoding="utf-8")})
    return results


@app.get("/api/v1/schemas/{name}", dependencies=[read_access])
def get_schema(name: str):
    schema = schema_for(name)
    if schema is None:
        raise HTTPException(status_code=404, detail="Schema not found")
    return schema


# --- Drafts: Git-backed definition authoring (MAEF_AUTHORING_ENABLED=1; `authoring` scope) ---


def require_authoring() -> None:
    if not authoring_enabled():
        raise HTTPException(status_code=501, detail="Authoring disabled. Set MAEF_AUTHORING_ENABLED=1.")


_DRAFT_STATUS = {
    "too_large": 413,
    "invalid_path": 400,
    "invalid_content": 400,
    "invalid_request": 400,
    "invalid_message": 400,
    "unknown_agent": 404,
}


def _draft_error(exc: DraftRejected) -> HTTPException:
    return HTTPException(
        status_code=_DRAFT_STATUS.get(exc.code, 409), detail={"code": exc.code, "message": exc.message}
    )


class DraftCreate(BaseModel):
    agent_id: str | None = Field(default=None, max_length=63)
    new_agent_id: str | None = Field(default=None, max_length=63)


class DraftPublish(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


@app.post("/api/v1/drafts", status_code=201, dependencies=[authoring_write, Depends(require_authoring)])
def create_draft(body: DraftCreate):
    try:
        return drafts.create(body.agent_id, body.new_agent_id)
    except DraftRejected as exc:
        raise _draft_error(exc) from exc


@app.get("/api/v1/drafts", dependencies=[authoring_read, Depends(require_authoring)])
def list_drafts():
    return drafts.list()


@app.put("/api/v1/drafts/{draft_id}/files/{path:path}", dependencies=[authoring_write, Depends(require_authoring)])
async def write_draft_file(draft_id: str, path: str, request: Request):
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail={"code": "too_large", "message": "File too large."})
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_FILE_BYTES:
            raise HTTPException(status_code=413, detail={"code": "too_large", "message": "File too large."})
        chunks.append(chunk)
    try:
        text = b"".join(chunks).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=400, detail={"code": "invalid_content", "message": "Body must be UTF-8 text."}
        ) from exc
    try:
        await run_in_threadpool(drafts.write, draft_id, path, text)
    except DraftNotFound as exc:
        raise HTTPException(status_code=404, detail="Draft not found") from exc
    except DraftRejected as exc:
        raise _draft_error(exc) from exc
    return {"path": path, "bytes": size}


@app.get("/api/v1/drafts/{draft_id}/review", dependencies=[authoring_read, Depends(require_authoring)])
def review_draft(draft_id: str):
    try:
        return drafts.review(draft_id)
    except DraftNotFound as exc:
        raise HTTPException(status_code=404, detail="Draft not found") from exc
    except DraftRejected as exc:
        raise _draft_error(exc) from exc


@app.post("/api/v1/drafts/{draft_id}/publish", dependencies=[authoring_write, Depends(require_authoring)])
def publish_draft(draft_id: str, body: DraftPublish):
    try:
        return {"draft_id": draft_id, "revision": drafts.publish(draft_id, body.message)}
    except DraftNotFound as exc:
        raise HTTPException(status_code=404, detail="Draft not found") from exc
    except DraftRejected as exc:
        raise _draft_error(exc) from exc


@app.delete("/api/v1/drafts/{draft_id}", dependencies=[authoring_write, Depends(require_authoring)])
def discard_draft(draft_id: str):
    try:
        drafts.discard(draft_id)
    except DraftNotFound as exc:
        raise HTTPException(status_code=404, detail="Draft not found") from exc
    except DraftRejected as exc:
        raise _draft_error(exc) from exc
    return {"discarded": draft_id}


@app.post("/api/v1/runs/preview", response_model=RunPreview, dependencies=[read_access])
def preview_run(request: RunRequest):
    try:
        return runs.preview(request)
    except AgentNotFound as exc:
        raise HTTPException(status_code=404, detail="Agent not found") from exc


@app.get("/api/v1/runs", response_model=list[RunRecord], dependencies=[read_access])
def list_runs():
    return runs.list()


@app.get("/api/v1/runs/{run_id}", response_model=RunDetail, dependencies=[read_access])
def get_run(run_id: str):
    run = runs.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


@dataclass
class SseSettings:
    """Timing for GET /runs/{id}/events; a module-level object so tests can shrink it."""

    poll_seconds: float = 0.5
    keepalive_seconds: float = 15.0
    max_seconds: float = 3600.0
    batch: int = 500


sse = SseSettings()
TERMINAL_EVENTS = ("task.succeeded", "task.failed", "task.cancelled")


def _poll_events(run_id: str, after: int) -> tuple[list, bool]:
    """Status is read before the events, so a run seen terminal has all its events in the batch
    that follows (the terminal event is the last one written)."""
    terminal = runs.status(run_id) in ("succeeded", "failed", "cancelled")
    return runs.events_after(run_id, after, sse.batch), terminal


def _frame(event) -> str:
    data = json.dumps(
        {"seq": event.seq, "type": event.type, "ts": event.ts, "payload": event.payload}, separators=(",", ":")
    )
    return f"id: {event.seq}\nevent: {event.type}\ndata: {data}\n\n"


@app.get("/api/v1/runs/{run_id}/events", dependencies=[read_access])
async def stream_run_events(
    run_id: str,
    request: Request,
    after: Annotated[int, Query(ge=0)] = 0,
    last_event_id: Annotated[str | None, Header()] = None,
):
    if await run_in_threadpool(runs.status, run_id) is None:
        raise HTTPException(status_code=404, detail="Run not found")
    cursor = int(last_event_id) if last_event_id and last_event_id.strip().isdigit() else after

    async def generate():
        nonlocal cursor
        started = last_write = time.monotonic()
        while time.monotonic() - started < sse.max_seconds:
            if await request.is_disconnected():
                return
            events, terminal = await run_in_threadpool(_poll_events, run_id, cursor)
            for event in events:
                yield _frame(event)
                cursor = event.seq
                last_write = time.monotonic()
                if event.type in TERMINAL_EVENTS:
                    return
            if terminal and not events:
                return
            if len(events) >= sse.batch:
                continue
            if time.monotonic() - last_write >= sse.keepalive_seconds:
                yield ": keepalive\n\n"
                last_write = time.monotonic()
            await asyncio.sleep(sse.poll_seconds)

    return StreamingResponse(
        generate(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"}
    )


@app.post("/api/v1/runs/{run_id}/cancel", status_code=202, dependencies=[runs_write])
def cancel_run(run_id: str):
    try:
        return {"outcome": runs.cancel(run_id)}
    except RunNotFound as exc:
        raise HTTPException(status_code=404, detail="Run not found") from exc
    except RunAlreadyTerminal as exc:
        raise HTTPException(
            status_code=409, detail={"code": "already_terminal", "message": "Run already finished."}
        ) from exc


@app.get("/api/v1/runs/{run_id}/artifacts", response_model=list[ArtifactRef], dependencies=[read_access])
def list_run_artifacts(run_id: str):
    if runs.get(run_id) is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return runs.ledger.artifacts(run_id)


@app.get("/api/v1/artifacts/{digest}", response_class=Response, dependencies=[read_access])
def get_artifact(digest: str):
    # Only digests recorded for some run are served: the store cannot be probed.
    ref = runs.ledger.artifact_by_digest(digest) if DIGEST_RE.match(digest) else None
    if ref is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    try:
        data = artifacts.read(ref)
    except ArtifactNotFound as exc:
        raise HTTPException(status_code=404, detail="Artifact not found") from exc
    except ArtifactCorrupt as exc:
        raise HTTPException(status_code=500, detail="Artifact failed verification") from exc
    disposition = (
        f'attachment; filename="{ref.name}"'
        if ref.name.isascii()
        else (f"attachment; filename=\"artifact\"; filename*=UTF-8''{quote(ref.name)}")
    )
    return Response(
        content=data,
        headers={
            "Content-Type": ref.mime,
            "Content-Disposition": disposition,
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox",
        },
    )


@app.post("/api/v1/runs", status_code=202)
def start_run(request: RunRequest, http: Request, principal: Principal = runs_write):
    try:
        record = runs.submit(request)
    except (ExecutionDisabled, UnsupportedDialect) as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    except AgentNotFound as exc:
        raise HTTPException(status_code=404, detail="Agent not found") from exc
    except DefinitionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except AdmissionRejected as exc:
        raise HTTPException(status_code=409, detail={"code": exc.code, "message": exc.message}) from exc
    ledger.audit(principal.name, "run.created", record.id, "created", _client_host(http))
    return {"run_id": record.id, "status": record.status}


@app.post("/api/v1/decisions", status_code=202)
def start_decision(request: DecisionRunRequest, http: Request, principal: Principal = decisions_write):
    try:
        record = runs.submit_decision(request)
    except ExecutionDisabled as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    except ProfileNotFound as exc:
        raise HTTPException(status_code=404, detail="Model profile not found") from exc
    except NotADecisionProfile as exc:
        raise HTTPException(status_code=409, detail={"code": "not_a_decision_profile", "message": str(exc)}) from exc
    except DefinitionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except AdmissionRejected as exc:
        raise HTTPException(status_code=409, detail={"code": exc.code, "message": exc.message}) from exc
    ledger.audit(principal.name, "run.created", record.id, "created", _client_host(http))
    return {"run_id": record.id, "status": record.status}


# --- Worker protocol (docs/ARCHITECTURE.md "Proposed remote-worker protocol") ---
# Each worker holds its own credential (`maef admin worker create`); the bearer token identifies the
# worker_id, which the lease is bound to. MAEF_AUTH_DISABLED does not apply to these routes.


def lease_seconds() -> float:
    return float(os.environ.get("MAEF_LEASE_SECONDS", "60"))


def worker_auth(*, audit: bool = False):
    def dependency(request: Request) -> str:
        worker_id = verify_worker_token(ledger, bearer(request.headers.get("authorization")))
        if worker_id is None:
            raise _deny(request, ANONYMOUS, 401, "Invalid worker credentials.")
        if audit:
            _audit(f"worker:{worker_id}", request, "authorized", required=True)
        return worker_id

    return dependency


require_worker = worker_auth()
require_worker_audited = worker_auth(audit=True)


class ClaimRequest(BaseModel):
    """The body's worker_id is ignored: the lease binds to the authenticated credential's worker."""

    worker_id: str | None = Field(default=None, max_length=200)


class HeartbeatRequest(BaseModel):
    fencing_token: int


class EventRequest(BaseModel):
    fencing_token: int
    wseq: int = Field(ge=1)
    type: str = Field(min_length=1, max_length=64)
    payload: dict = Field(default_factory=dict)
    status: Literal["running"] | None = None


class FinishRequest(BaseModel):
    fencing_token: int
    # Upper bound only guards against absurd values; the ledger clamps to the run's reservation (budget.overreport).
    tokens_used: int = Field(ge=0, le=10_000_000_000)
    status: Literal["succeeded", "failed", "cancelled"]
    terminal_reason: str = Field(min_length=1, max_length=200)


def _conflict(code: str, exc: Exception) -> HTTPException:
    return HTTPException(status_code=409, detail={"code": code, "message": str(exc)})


@app.post(
    "/api/v1/worker/claim",
    response_class=Response,
    responses={200: {"description": "A Lease"}, 204: {"description": "Nothing queued"}},
)
def worker_claim(body: ClaimRequest, request: Request, worker_id: Annotated[str, Depends(require_worker)]):
    lease = runs.ledger.claim(worker_id, lease_seconds(), time.time())
    if lease is None:
        return Response(status_code=204)  # idle polls are not audited; granted leases are
    _audit(f"worker:{worker_id}", request, "authorized", required=False)
    return Response(content=lease.model_dump_json(), media_type="application/json")


@app.post("/api/v1/worker/runs/{run_id}/heartbeat")
def worker_heartbeat(run_id: str, body: HeartbeatRequest, worker_id: Annotated[str, Depends(require_worker)]):
    try:
        cancel_requested = runs.ledger.heartbeat(
            run_id, body.fencing_token, lease_seconds(), time.time(), worker_id=worker_id
        )
    except FencingError as exc:
        raise _conflict("fencing", exc) from exc
    return {"ok": True, "cancel_requested": cancel_requested}


@app.post("/api/v1/worker/runs/{run_id}/events")
def worker_events(run_id: str, body: EventRequest, worker_id: Annotated[str, Depends(require_worker)]):
    try:
        cancel_requested = runs.ledger.report(
            run_id, body.fencing_token, body.wseq, body.type, body.payload, body.status, worker_id=worker_id
        )
    except FencingError as exc:
        raise _conflict("fencing", exc) from exc
    except SequenceGap as exc:
        raise _conflict("sequence_gap", exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"cancel_requested": cancel_requested}


@app.post("/api/v1/worker/runs/{run_id}/finish")
def worker_finish(run_id: str, body: FinishRequest, worker_id: Annotated[str, Depends(require_worker_audited)]):
    try:
        runs.ledger.finish(
            run_id,
            body.fencing_token,
            tokens_used=body.tokens_used,
            status=body.status,
            terminal_reason=body.terminal_reason,
            worker_id=worker_id,
        )
    except FencingError as exc:
        raise _conflict("fencing", exc) from exc
    return {"ok": True}


@app.post("/api/v1/worker/runs/{run_id}/artifacts")
async def worker_artifact(
    run_id: str,
    request: Request,
    worker_id: Annotated[str, Depends(require_worker)],
    x_fencing_token: Annotated[int, Header()],
    x_content_digest: Annotated[str, Header()],
    name: str = Query(min_length=1, max_length=512),
    mime: str = Query(default="application/octet-stream", max_length=200),
):
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_ARTIFACT_BYTES:
        raise HTTPException(status_code=413, detail={"code": "too_large"})
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_ARTIFACT_BYTES:
            raise HTTPException(status_code=413, detail={"code": "too_large"})
        chunks.append(chunk)
    try:
        return await run_in_threadpool(
            ArtifactIntake(runs.ledger, artifacts).accept,
            run_id,
            token=x_fencing_token,
            name=name,
            mime=mime,
            data=b"".join(chunks),
            digest=x_content_digest,
            worker_id=worker_id,
        )
    except FencingError as exc:
        raise _conflict("fencing", exc) from exc
    except ArtifactStoreError as exc:
        raise HTTPException(status_code=507, detail={"code": exc.code}) from exc
    except ArtifactRejected as exc:
        raise HTTPException(
            status_code=400 if exc.code == "digest_mismatch" else 413, detail={"code": exc.code}
        ) from exc
