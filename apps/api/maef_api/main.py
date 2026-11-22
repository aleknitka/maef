import json
import os
from decimal import Decimal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from .adapters.bedrock import BedrockAdapter
from .admission import AdmissionError, admit, to_micro
from .harness import PreparedRun
from .models import (
    AgentDefinition,
    Budget,
    ModelEndpointProfile,
    RunDetail,
    RunPreview,
    RunRecord,
    RunRequest,
)
from .registry import ROOT, AgentRegistry, ModelProfileRegistry, PriceScheduleRegistry
from .resolve import ResolveError, resolve_definition
from .runner import Runner
from .store import Store

app = FastAPI(title="MAEF Control Plane", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"], allow_methods=["GET", "POST"], allow_headers=["Content-Type"])
registry = AgentRegistry()
model_profiles = ModelProfileRegistry()
# Ledger lives under the gitignored runs/ directory unless overridden.
store = Store(os.environ.get("MAEF_DB", ROOT / "runs" / "maef.db"))

price_schedules = PriceScheduleRegistry()
store.ensure_account("default", to_micro(Decimal(os.environ.get("MAEF_ACCOUNT_LIMIT_USD", "5.00"))))
runner = Runner(store)
runner.sweep_orphans()

def execution_enabled() -> bool:
    # Off by default: the API must be opted in to spend money.
    return os.environ.get("MAEF_EXECUTION_ENABLED") == "1"

def adapter_factory(profile: ModelEndpointProfile):
    if profile.api_dialect == "bedrock-converse":
        return BedrockAdapter(profile)
    raise HTTPException(status_code=501, detail=f"No adapter for dialect {profile.api_dialect}")

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

def effective_budget(ceiling: Budget, requested: Budget | None) -> Budget:
    """A request may only tighten the agent's budget, never raise it."""
    if requested is None:
        return ceiling
    return Budget(
        max_cost_usd=min(ceiling.max_cost_usd, requested.max_cost_usd),
        max_duration_seconds=min(ceiling.max_duration_seconds, requested.max_duration_seconds),
        max_turns=min(ceiling.max_turns, requested.max_turns),
    )

@app.post("/api/v1/runs/preview", response_model=RunPreview)
def preview_run(request: RunRequest):
    agent = registry.get_agent(request.agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    b = effective_budget(agent.budget, request.budget)
    return RunPreview(
        agent_id=agent.id,
        instruction=request.instruction,
        effective_budget=b,
        model_profile=agent.runtime.model_profile,
        sandbox_profile=agent.runtime.sandbox_profile,
        skills=agent.skills,
        mcp_servers=agent.mcp_servers,
    )

def _record(row: dict) -> dict:
    return {**row, "dirty": bool(row["dirty"]), "effective_budget": json.loads(row["effective_budget"]),
            "reserved_micro_usd": row["reserved_micro"], "cost_micro_usd": row["cost_micro"]}

@app.get("/api/v1/runs", response_model=list[RunRecord])
def list_runs():
    return [_record(r) for r in store.list_runs()]

@app.get("/api/v1/runs/{run_id}", response_model=RunDetail)
def get_run(run_id: str):
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return _record(run)

@app.post("/api/v1/runs", status_code=202)
def start_run(request: RunRequest):
    if not execution_enabled():
        raise HTTPException(status_code=501, detail="Execution disabled. Set MAEF_EXECUTION_ENABLED=1 or use /api/v1/runs/preview.")
    agent = registry.get_agent(request.agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    try:
        definition = resolve_definition(agent.id, registry, model_profiles, price_schedules)
    except ResolveError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    profile = model_profiles.get_profile(definition.model_profile_id)
    adapter = adapter_factory(profile)
    system_prompt = (registry.root / agent.id / agent.instructions).read_text(encoding="utf-8")
    budget = effective_budget(agent.budget, request.budget)
    try:
        run_id = admit(
            store, definition, budget, instruction=request.instruction,
            system_prompt_bytes=len(system_prompt.encode()), max_output_tokens=profile.max_output_tokens,
            allow_dirty=os.environ.get("MAEF_ALLOW_DIRTY") == "1",
        )
    except AdmissionError as exc:
        raise HTTPException(status_code=409, detail={"code": exc.code, "message": str(exc)})
    reserved = store.get_run(run_id)["reserved_micro"]
    runner.submit(PreparedRun(run_id, definition, profile, budget, reserved, system_prompt, request.instruction), adapter)
    return {"run_id": run_id, "status": "admitted"}
