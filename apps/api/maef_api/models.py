from datetime import date
from decimal import Decimal
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

PROFILE_ID_PATTERN = r"^[a-z][a-z0-9-]{1,62}$"

class BedrockEndpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["amazon-bedrock"]
    region: str = Field(pattern=r"^[a-z]{2}(-[a-z]+)+-\d$")
    # Foundation model ID or inference profile ID (eu./global. prefix).
    model_id: str = Field(pattern=r"^[a-z0-9.:-]+$")
    # Where requests may be served: the selected Region only, a geography, or anywhere.
    routing: Literal["in-region", "geo", "global"]

class ModelCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input_modalities: list[Literal["text", "image"]] = Field(default_factory=lambda: ["text"])
    streaming: bool = False

class ModelEndpointProfile(BaseModel):
    """Model endpoint profile (see docs/ARCHITECTURE.md). Never embeds credentials."""
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["maef.dev/v1alpha1"]
    id: str = Field(pattern=PROFILE_ID_PATTERN)
    version: str
    display_name: str
    vendor: str
    api_dialect: Literal["bedrock-converse", "openai-compatible", "anthropic", "cli-adapter"]
    endpoint: BedrockEndpoint
    # A reference to where credentials come from, not a secret value.
    credential_ref: str = Field(pattern=r"^[a-z]+:[a-z0-9-]+$")
    capabilities: ModelCapabilities = Field(default_factory=ModelCapabilities)
    timeout_seconds: int = Field(default=300, ge=1, le=3600)
    max_retries: int = Field(default=2, ge=0, le=10)
    # Hard cap on output tokens per model call; bounds the worst-case cost of a turn.
    max_output_tokens: int = Field(default=4096, ge=1, le=1_000_000)
    # None means unpriced: budget admission must fail closed (ADR-0004).
    price_schedule_id: str | None = Field(default=None, pattern=PROFILE_ID_PATTERN)

class PriceSchedule(BaseModel):
    """Token prices in USD per million tokens. Committed to Git: prices are not secrets."""
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["maef.dev/v1alpha1"]
    id: str = Field(pattern=PROFILE_ID_PATTERN)
    currency: Literal["USD"] = "USD"
    effective_from: date
    input_usd_per_million_tokens: Decimal = Field(ge=0, max_digits=12, decimal_places=6)
    output_usd_per_million_tokens: Decimal = Field(ge=0, max_digits=12, decimal_places=6)

class Budget(BaseModel):
    # Decimal, never float: costs are exact (docs/BUDGETS_OBSERVABILITY.md). Serialised as a string.
    max_cost_usd: Decimal = Field(default=Decimal("0.5"), ge=0, le=100000, max_digits=12, decimal_places=6)
    max_duration_seconds: int = Field(default=300, ge=1, le=86400)
    max_turns: int = Field(default=20, ge=1, le=100000)

class Runtime(BaseModel):
    model_profile: str = "local-default"
    sandbox_profile: str = "restricted-local"

class AgentDefinition(BaseModel):
    schema_version: Literal["maef.dev/v1alpha1"]
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{1,62}$")
    version: str
    display_name: str
    description: str
    capabilities: list[str] = Field(default_factory=list)
    instructions: str = "SYSTEM.md"
    skills: list[str] = Field(default_factory=list)
    mcp_servers: list[str] = Field(default_factory=list)
    runtime: Runtime = Field(default_factory=Runtime)
    budget: Budget = Field(default_factory=Budget)
    outputs: list[str] = Field(default_factory=list)

class RunRequest(BaseModel):
    agent_id: str
    instruction: str = Field(min_length=1, max_length=20000)
    budget: Budget | None = None

class RunPreview(BaseModel):
    agent_id: str
    instruction: str
    effective_budget: Budget
    model_profile: str
    sandbox_profile: str
    skills: list[str]
    mcp_servers: list[str]
    status: Literal["preview_only"] = "preview_only"
    message: str = "No model invocation or sandbox execution has occurred."

class ResolvedDefinition(BaseModel):
    """An agent definition pinned to exact content, ready for admission."""
    agent_id: str
    agent_version: str
    git_rev: str | None
    # True when the agent directory has uncommitted changes; the digest still pins the content.
    dirty: bool
    definition_digest: str
    model_profile_id: str
    profile_digest: str
    price_schedule: PriceSchedule | None
