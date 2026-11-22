import hmac
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

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
from .registry import ROOT, AgentRegistry, ModelProfileRegistry
from .runs import (
    AdmissionRejected,
    AgentNotFound,
    DefinitionConflict,
    ExecutionDisabled,
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
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"], allow_methods=["GET", "POST"], allow_headers=["Content-Type"])
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
runs = Runs(
    Definitions(registry, model_profiles), ledger,
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
        runs.ledger.heartbeat(run_id, body.fencing_token, lease_seconds(), time.time())
    except FencingError as exc:
        raise _conflict("fencing", exc)
    return {"ok": True}

@app.post("/api/v1/worker/runs/{run_id}/events", dependencies=[Depends(require_worker)])
def worker_events(run_id: str, body: EventRequest):
    try:
        appended = runs.ledger.report(run_id, body.fencing_token, body.wseq, body.type, body.payload, body.status)
    except FencingError as exc:
        raise _conflict("fencing", exc)
    except SequenceGap as exc:
        raise _conflict("sequence_gap", exc)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"appended": appended}

@app.post("/api/v1/worker/runs/{run_id}/finish", dependencies=[Depends(require_worker)])
def worker_finish(run_id: str, body: FinishRequest):
    try:
        runs.ledger.finish(run_id, body.fencing_token, tokens_used=body.tokens_used, status=body.status,
                           terminal_reason=body.terminal_reason)
    except FencingError as exc:
        raise _conflict("fencing", exc)
    return {"ok": True}
