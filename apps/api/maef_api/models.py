from ipaddress import ip_address
from typing import Annotated, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PROFILE_ID_PATTERN = r"^[a-z][a-z0-9-]{1,62}$"

class BedrockEndpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["amazon-bedrock"]
    region: str = Field(pattern=r"^[a-z]{2}(-[a-z]+)+-\d$")
    # Foundation model ID or inference profile ID (eu./global. prefix).
    model_id: str = Field(pattern=r"^[a-z0-9.:-]+$")
    # Where requests may be served: the selected Region only, a geography, or anywhere.
    routing: Literal["in-region", "geo", "global"]

def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False

class _HttpEndpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_url: str
    model_id: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._:/-]+$")

    # Explicit opt-in for LAN hosts (LM Studio/Ollama on another box, or the Windows host seen from WSL2).
    allow_private_network: bool = False

    @field_validator("base_url")
    @classmethod
    def _safe_url(cls, v: str) -> str:
        u = urlparse(v)
        if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password or u.query or u.fragment:
            raise ValueError("base_url must be http(s)://host[:port][/path] with no credentials or query")
        return v.rstrip("/")

    @model_validator(mode="after")
    def _http_only_for_local(self):
        # Plain http is only for loopback or an opted-in private network, so prompts and keys never cross the internet unencrypted.
        if self.base_url.startswith("http://") and not (self.is_loopback or self.allow_private_network):
            raise ValueError("http base_url requires a loopback host or allow_private_network; use https")
        return self

    @property
    def is_loopback(self) -> bool:
        return _is_loopback(urlparse(self.base_url).hostname)

class OpenAICompatibleEndpoint(_HttpEndpoint):
    """OpenAI (https://api.openai.com/v1) or any local server speaking chat/completions."""
    kind: Literal["openai-compatible"]
    # OpenAI's newer models reject max_tokens; many local servers only know max_tokens.
    token_param: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"

class AnthropicEndpoint(_HttpEndpoint):
    kind: Literal["anthropic"]
    base_url: str = "https://api.anthropic.com"

Endpoint = Annotated[BedrockEndpoint | OpenAICompatibleEndpoint | AnthropicEndpoint, Field(discriminator="kind")]
_KIND_DIALECT = {"amazon-bedrock": "bedrock-converse", "openai-compatible": "openai-compatible", "anthropic": "anthropic"}

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
    endpoint: Endpoint
    # Where credentials come from, never a secret value: aws:default-chain, env:NAME, none:local.
    credential_ref: str = Field(pattern=r"^(aws:default-chain|none:local|env:[A-Z][A-Z0-9_]{1,63})$")
    capabilities: ModelCapabilities = Field(default_factory=ModelCapabilities)
    timeout_seconds: int = Field(default=300, ge=1, le=3600)
    max_retries: int = Field(default=2, ge=0, le=10)
    # Hard cap on output tokens per model call; bounds the worst-case token use of a turn.
    max_output_tokens: int = Field(default=4096, ge=1, le=1_000_000)

    @model_validator(mode="after")
    def _dialect_matches_endpoint(self):
        if _KIND_DIALECT[self.endpoint.kind] != self.api_dialect:
            raise ValueError(f"api_dialect {self.api_dialect} does not match endpoint kind {self.endpoint.kind}")
        return self

class Budget(BaseModel):
    # Total model tokens (input + output) a run may use. Integers: exact and provider-neutral.
    max_tokens: int = Field(default=100_000, ge=1, le=100_000_000)
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
    # Built-in tool names (see tools.BUILTIN_TOOL_NAMES), validated when the definition is pinned.
    tools: list[str] = Field(default_factory=list)
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

class ExecutionEvent(BaseModel):
    run_id: str
    seq: int
    type: str
    ts: str
    payload: dict = Field(default_factory=dict)

class RunRecord(BaseModel):
    id: str
    status: Literal["admitted", "running", "succeeded", "failed", "cancelled"]
    agent_id: str
    git_rev: str | None
    dirty: bool
    definition_digest: str
    profile_digest: str
    effective_budget: Budget
    reserved_tokens: int
    tokens_used: int | None
    terminal_reason: str | None
    created_at: str
    finished_at: str | None

class RunDetail(RunRecord):
    events: list[ExecutionEvent]

class ResultManifest(BaseModel):
    """Written as the final run event. Usage is in tokens as reported by the provider."""
    run_id: str
    status: Literal["succeeded", "failed", "cancelled"]
    agent_id: str
    agent_revision: str | None
    dirty: bool
    definition_digest: str
    model_profile_revision: str
    input_tokens: int = 0
    output_tokens: int = 0
    tokens_used: int
    terminal_reason: str
