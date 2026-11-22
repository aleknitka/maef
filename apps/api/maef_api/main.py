import asyncio
import hmac
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
    default_artifact_store,
)
from .definitions import Definitions
from .leases import FencingError, SequenceGap
from .ledger import Ledger
from .models import (
    AgentDefinition,
    ModelEndpointProfile,
    RunDetail,
    RunPreview,
    RunRecord,
    RunRequest,
)
from .registry import (
    ROOT,
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

SWEEP_SECONDS = 10


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Startup reclaims lapsed leases and a daemon thread keeps sweeping; nothing runs at import."""
    runs.recover()
    stop = threading.Event()

    def sweep() -> None:
        while not stop.wait(SWEEP_SECONDS):
            try:
                runs.recover()
            except Exception as exc:  # noqa: BLE001  # keep sweeping; the next tick retries
                logging.getLogger("maef.api").warning("lease sweep failed: %s", type(exc).__name__)

    thread = threading.Thread(target=sweep, name="maef-lease-sweep", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=5)


app = FastAPI(title="MAEF Control Plane", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"], allow_methods=["GET", "POST"], allow_headers=["Content-Type", "Last-Event-ID"])
registry = AgentRegistry()
model_profiles = ModelProfileRegistry()

def execution_enabled() -> bool:
    # Off by default: the API must be opted in to spend money.
    return os.environ.get("MAEF_EXECUTION_ENABLED") == "1"

def allow_dirty() -> bool:
    return os.environ.get("MAEF_ALLOW_DIRTY") == "1"

# Ledger lives under the gitignored runs/ directory unless overridden.
ledger = Ledger(os.environ.get("MAEF_DB", ROOT / "runs" / "maef.db"))
ledger.ensure_account("default", int(os.environ.get("MAEF_ACCOUNT_LIMIT_TOKENS", "5000000")))
artifacts = default_artifact_store()
runs = Runs(
    Definitions(registry, model_profiles, SandboxProfileRegistry(), McpServerRegistry()), ledger,
    execution_enabled=lambda: execution_enabled(), allow_dirty=lambda: allow_dirty(),
)

@app.get("/health")
def health():
    return {"status": "ok", "execution_enabled": execution_enabled()}

@app.get("/api/v1/agents", response_model=list[AgentDefinition])
def list_agents():
    return registry.list_agents()

@app.get("/api/v1/agents/{agent_id}", response_model=AgentDefinition)
def get_agent(agent_id: str):
    agent = registry.get_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent

@app.get("/api/v1/model-profiles", response_model=list[ModelEndpointProfile])
def list_model_profiles():
    return model_profiles.list_profiles()

@app.get("/api/v1/model-profiles/{profile_id}", response_model=ModelEndpointProfile)
def get_model_profile(profile_id: str):
    profile = model_profiles.get_profile(profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Model profile not found")
    return profile

@app.get("/api/v1/agents/{agent_id}/files")
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

@app.post("/api/v1/runs/preview", response_model=RunPreview)
def preview_run(request: RunRequest):
    try:
        return runs.preview(request)
    except AgentNotFound:
        raise HTTPException(status_code=404, detail="Agent not found")

@app.get("/api/v1/runs", response_model=list[RunRecord])
def list_runs():
    return runs.list()

@app.get("/api/v1/runs/{run_id}", response_model=RunDetail)
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
    data = json.dumps({"seq": event.seq, "type": event.type, "ts": event.ts, "payload": event.payload},
                      separators=(",", ":"))
    return f"id: {event.seq}\nevent: {event.type}\ndata: {data}\n\n"


@app.get("/api/v1/runs/{run_id}/events")
async def stream_run_events(run_id: str, request: Request, after: Annotated[int, Query(ge=0)] = 0,
                            last_event_id: Annotated[str | None, Header()] = None):
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

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

@app.post("/api/v1/runs/{run_id}/cancel", status_code=202)
def cancel_run(run_id: str):
    try:
        return {"outcome": runs.cancel(run_id)}
    except RunNotFound:
        raise HTTPException(status_code=404, detail="Run not found")
    except RunAlreadyTerminal:
        raise HTTPException(status_code=409, detail={"code": "already_terminal", "message": "Run already finished."})

@app.get("/api/v1/runs/{run_id}/artifacts", response_model=list[ArtifactRef])
def list_run_artifacts(run_id: str):
    if runs.get(run_id) is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return runs.ledger.artifacts(run_id)

@app.get("/api/v1/artifacts/{digest}", response_class=Response)
def get_artifact(digest: str):
    # Only digests recorded for some run are served: the store cannot be probed.
    ref = runs.ledger.artifact_by_digest(digest) if DIGEST_RE.match(digest) else None
    if ref is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    try:
        data = artifacts.read(ref)
    except ArtifactNotFound:
        raise HTTPException(status_code=404, detail="Artifact not found")
    except ArtifactCorrupt:
        raise HTTPException(status_code=500, detail="Artifact failed verification")
    disposition = f'attachment; filename="{ref.name}"' if ref.name.isascii() else (
        f"attachment; filename=\"artifact\"; filename*=UTF-8''{quote(ref.name)}")
    return Response(content=data, headers={
        "Content-Type": ref.mime, "Content-Disposition": disposition,
        "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox"})

@app.post("/api/v1/runs", status_code=202)
def start_run(request: RunRequest):
    try:
        record = runs.submit(request)
    except (ExecutionDisabled, UnsupportedDialect) as exc:
        raise HTTPException(status_code=501, detail=str(exc))
    except AgentNotFound:
        raise HTTPException(status_code=404, detail="Agent not found")
    except DefinitionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except AdmissionRejected as exc:
        raise HTTPException(status_code=409, detail={"code": exc.code, "message": exc.message})
    return {"run_id": record.id, "status": record.status}

@app.post("/api/v1/decisions", status_code=202)
def start_decision(request: DecisionRunRequest):
    try:
        record = runs.submit_decision(request)
    except ExecutionDisabled as exc:
        raise HTTPException(status_code=501, detail=str(exc))
    except ProfileNotFound:
        raise HTTPException(status_code=404, detail="Model profile not found")
    except NotADecisionProfile as exc:
        raise HTTPException(status_code=409, detail={"code": "not_a_decision_profile", "message": str(exc)})
    except DefinitionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except AdmissionRejected as exc:
        raise HTTPException(status_code=409, detail={"code": exc.code, "message": exc.message})
    return {"run_id": record.id, "status": record.status}


# --- Worker protocol (docs/ARCHITECTURE.md "Proposed remote-worker protocol") ---
# Auth is one shared bearer token (MAEF_WORKER_TOKEN). Per-worker short-lived machine
# credentials and registration arrive in BACKEND_PLAN step 11.

def lease_seconds() -> float:
    return float(os.environ.get("MAEF_LEASE_SECONDS", "60"))

def require_worker(authorization: Annotated[str | None, Header()] = None) -> None:
    expected = os.environ.get("MAEF_WORKER_TOKEN")
    if not expected:
        raise HTTPException(status_code=503, detail="Worker routes disabled: MAEF_WORKER_TOKEN is not set.")
    scheme, _, provided = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(provided.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="Invalid worker credentials.", headers={"WWW-Authenticate": "Bearer"})

class ClaimRequest(BaseModel):
    worker_id: str = Field(min_length=1, max_length=200)

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
    tokens_used: int = Field(ge=0)
    status: Literal["succeeded", "failed", "cancelled"]
    terminal_reason: str = Field(min_length=1, max_length=200)

def _conflict(code: str, exc: Exception) -> HTTPException:
    return HTTPException(status_code=409, detail={"code": code, "message": str(exc)})

@app.post("/api/v1/worker/claim", dependencies=[Depends(require_worker)], response_class=Response,
          responses={200: {"description": "A Lease"}, 204: {"description": "Nothing queued"}})
def worker_claim(body: ClaimRequest):
    lease = runs.ledger.claim(body.worker_id, lease_seconds(), time.time())
    if lease is None:
        return Response(status_code=204)
    return Response(content=lease.model_dump_json(), media_type="application/json")

@app.post("/api/v1/worker/runs/{run_id}/heartbeat", dependencies=[Depends(require_worker)])
def worker_heartbeat(run_id: str, body: HeartbeatRequest):
    try:
        cancel_requested = runs.ledger.heartbeat(run_id, body.fencing_token, lease_seconds(), time.time())
    except FencingError as exc:
        raise _conflict("fencing", exc)
    return {"ok": True, "cancel_requested": cancel_requested}

@app.post("/api/v1/worker/runs/{run_id}/events", dependencies=[Depends(require_worker)])
def worker_events(run_id: str, body: EventRequest):
    try:
        cancel_requested = runs.ledger.report(run_id, body.fencing_token, body.wseq, body.type, body.payload, body.status)
    except FencingError as exc:
        raise _conflict("fencing", exc)
    except SequenceGap as exc:
        raise _conflict("sequence_gap", exc)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"cancel_requested": cancel_requested}

@app.post("/api/v1/worker/runs/{run_id}/finish", dependencies=[Depends(require_worker)])
def worker_finish(run_id: str, body: FinishRequest):
    try:
        runs.ledger.finish(run_id, body.fencing_token, tokens_used=body.tokens_used, status=body.status,
                           terminal_reason=body.terminal_reason)
    except FencingError as exc:
        raise _conflict("fencing", exc)
    return {"ok": True}

@app.post("/api/v1/worker/runs/{run_id}/artifacts", dependencies=[Depends(require_worker)])
async def worker_artifact(run_id: str, request: Request, name: str = Query(min_length=1, max_length=512),
                          mime: str = Query(default="application/octet-stream", max_length=200),
                          x_fencing_token: Annotated[int, Header()] = ..., x_content_digest: Annotated[str, Header()] = ...):
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
            ArtifactIntake(runs.ledger, artifacts).accept, run_id, token=x_fencing_token, name=name, mime=mime,
            data=b"".join(chunks), digest=x_content_digest)
    except FencingError as exc:
        raise _conflict("fencing", exc)
    except ArtifactRejected as exc:
        raise HTTPException(status_code=400 if exc.code == "digest_mismatch" else 413, detail={"code": exc.code})
