import os

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from .adapters.anthropic import AnthropicAdapter
from .adapters.bedrock import BedrockAdapter
from .adapters.openai_compatible import OpenAICompatibleAdapter
from .definitions import Definitions
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
from .runner import Runner
from .runs import (
    AdmissionRejected,
    AgentNotFound,
    DefinitionConflict,
    ExecutionDisabled,
    Runs,
    UnsupportedDialect,
)

app = FastAPI(title="MAEF Control Plane", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"], allow_methods=["GET", "POST"], allow_headers=["Content-Type"])
registry = AgentRegistry()
model_profiles = ModelProfileRegistry()

def execution_enabled() -> bool:
    # Off by default: the API must be opted in to spend money.
    return os.environ.get("MAEF_EXECUTION_ENABLED") == "1"

def allow_dirty() -> bool:
    return os.environ.get("MAEF_ALLOW_DIRTY") == "1"

def adapter_factory(profile: ModelEndpointProfile):
    if profile.api_dialect == "bedrock-converse":
        return BedrockAdapter(profile)
    if profile.api_dialect == "openai-compatible":
        return OpenAICompatibleAdapter(profile)
    if profile.api_dialect == "anthropic":
        return AnthropicAdapter(profile)
    raise UnsupportedDialect(f"No adapter for dialect {profile.api_dialect}")

# Ledger lives under the gitignored runs/ directory unless overridden.
ledger = Ledger(os.environ.get("MAEF_DB", ROOT / "runs" / "maef.db"))
ledger.ensure_account("default", int(os.environ.get("MAEF_ACCOUNT_LIMIT_TOKENS", "5000000")))
runner = Runner()
runs = Runs(
    Definitions(registry, model_profiles), ledger, adapter_factory, runner.submit,
    execution_enabled=lambda: execution_enabled(), allow_dirty=lambda: allow_dirty(),
)
runs.recover()

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
