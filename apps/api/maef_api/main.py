from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import json
import os
from .models import AgentDefinition, ModelEndpointProfile, RunDetail, RunRecord, RunRequest, RunPreview, Budget
from .registry import AgentRegistry, ModelProfileRegistry, ROOT
from .store import Store

app = FastAPI(title="MAEF Control Plane", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"], allow_methods=["GET", "POST"], allow_headers=["Content-Type"])
registry = AgentRegistry()
model_profiles = ModelProfileRegistry()
# Ledger lives under the gitignored runs/ directory unless overridden.
store = Store(os.environ.get("MAEF_DB", ROOT / "runs" / "maef.db"))

@app.get("/health")
def health():
    return {"status": "ok", "execution_enabled": False}

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
    agent = registry.get_agent(request.agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    b = agent.budget
    if request.budget:
        b = Budget(
            max_cost_usd=min(b.max_cost_usd, request.budget.max_cost_usd),
            max_duration_seconds=min(b.max_duration_seconds, request.budget.max_duration_seconds),
            max_turns=min(b.max_turns, request.budget.max_turns),
        )
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

@app.post("/api/v1/runs", status_code=501)
def start_run(_: RunRequest):
    raise HTTPException(status_code=501, detail="Execution not implemented in the bootstrap. Use /api/v1/runs/preview.")
