import re
from ipaddress import ip_address
from typing import Annotated, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

from .artifacts import ArtifactRef
from .endings import Ending, ending_of

PROFILE_ID_PATTERN = r"^[a-z][a-z0-9-]{1,62}$"
# Dialects the worker has an adapter for; Runs refuses to admit anything else.
SUPPORTED_DIALECTS = ("bedrock-converse", "openai-compatible", "anthropic")
# Decision models answer typed questions about a state (POST /api/v1/decisions); agents cannot use them.
DECISION_DIALECTS = ("decisions-api", "workers-ai-decision")


class BedrockEndpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["amazon-bedrock"]
    region: str = Field(pattern=r"^[a-z]{2}(-[a-z]+)+-\d$")
    # Foundation model ID or inference profile ID (eu./global. prefix).
    model_id: str = Field(pattern=r"^[a-z0-9.:-]+$")
    # Where requests may be served: the selected Region only, a geography, or anywhere.
    routing: Literal["in-region", "geo", "global"]


def is_loopback_host(host: str) -> bool:
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
        # Plain http is only for loopback or an opted-in private network,
        # so prompts and keys never cross the internet unencrypted.
        if self.base_url.startswith("http://") and not (self.is_loopback or self.allow_private_network):
            raise ValueError("http base_url requires a loopback host or allow_private_network; use https")
        return self

    @property
    def is_loopback(self) -> bool:
        host = urlparse(self.base_url).hostname
        return host is not None and is_loopback_host(host)


class OpenAICompatibleEndpoint(_HttpEndpoint):
    """OpenAI (https://api.openai.com/v1) or any local server speaking chat/completions."""

    kind: Literal["openai-compatible"]
    # OpenAI's newer models reject max_tokens; many local servers only know max_tokens.
    token_param: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"  # noqa: S105  request-parameter name, not a secret
    # Sent as top-level `reasoning_effort` when set (OpenAI and OpenRouter accept it).
    reasoning_effort: Literal["minimal", "low", "medium", "high"] | None = None


class AnthropicEndpoint(_HttpEndpoint):
    kind: Literal["anthropic"]
    base_url: str = "https://api.anthropic.com"


class DecisionsApiEndpoint(_HttpEndpoint):
    """decisions-api.dev: POST /v1/systemone (Jev and other decision models)."""

    kind: Literal["decisions-api"]
    base_url: str = "https://decisions-api.dev"


class WorkersAiEndpoint(_HttpEndpoint):
    """Cloudflare Workers AI decision models (Clef): POST /accounts/{account_id}/ai/run/{model_id}."""

    kind: Literal["cloudflare-workers-ai"]
    base_url: str = "https://api.cloudflare.com/client/v4"
    account_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    model_id: str = Field(pattern=r"^@cf/[a-z0-9._-]+/[a-z0-9._-]+$", max_length=200)


Endpoint = Annotated[
    BedrockEndpoint | OpenAICompatibleEndpoint | AnthropicEndpoint | DecisionsApiEndpoint | WorkersAiEndpoint,
    Field(discriminator="kind"),
]
_KIND_DIALECT = {
    "amazon-bedrock": "bedrock-converse",
    "openai-compatible": "openai-compatible",
    "anthropic": "anthropic",
    "decisions-api": "decisions-api",
    "cloudflare-workers-ai": "workers-ai-decision",
}


def _text_only() -> list[Literal["text", "image"]]:
    return ["text"]


class ModelCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input_modalities: list[Literal["text", "image"]] = Field(default_factory=_text_only)
    streaming: bool = False


class ModelEndpointProfile(BaseModel):
    """Model endpoint profile (see docs/ARCHITECTURE.md). Never embeds credentials."""

    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["maef.dev/v1alpha1"]
    id: str = Field(pattern=PROFILE_ID_PATTERN)
    version: str
    display_name: str
    vendor: str
    api_dialect: Literal[
        "bedrock-converse", "openai-compatible", "anthropic", "cli-adapter", "decisions-api", "workers-ai-decision"
    ]
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


_PINNED_IMAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:-]*@sha256:[0-9a-f]{64}$")


def is_pinned_image(image: str) -> bool:
    return _PINNED_IMAGE.fullmatch(image) is not None


class SandboxProfile(BaseModel):
    """Sandbox profile: the OCI image and resource limits a run's commands execute under.
    Never embeds credentials or mounts. Tag-only images are checked at provisioning time
    (they need MAEF_ALLOW_UNPINNED_IMAGES=1 in the worker environment)."""

    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["maef.dev/v1alpha1"]
    id: str = Field(pattern=PROFILE_ID_PATTERN)
    image: str = Field(min_length=1, max_length=300, pattern=r"^[A-Za-z0-9][A-Za-z0-9._/:@-]*$")
    cpus: float = Field(gt=0, le=64)
    memory_mb: int = Field(ge=16, le=262_144)
    pids_limit: int = Field(ge=8, le=65_536)
    workspace_mb: int = Field(ge=1, le=65_536)
    network: Literal["none"] = "none"
    require_rootless: bool = True
    user: str = Field(default="65534:65534", pattern=r"^[0-9]+(:[0-9]+)?$")


_ENV_NAME = r"^[A-Za-z_][A-Za-z0-9_]{0,63}$"
# Names that would change how the worker's own container CLI behaves, since secrets travel to it
# through its process environment (`exec -e NAME`).
_PROTECTED_ENV = frozenset({"PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "PWD", "IFS", "ENV"})
_PROTECTED_ENV_PREFIXES = ("LD_", "DYLD_", "DOCKER_", "CONTAINER", "PODMAN_", "XDG_", "DBUS_", "BASH_")


class McpServerManifest(BaseModel):
    """MCP server manifest (`mcp-servers/<id>.yaml`): a stdio server that runs in its own container.
    `env` holds non-secret values only; `secrets` maps ENV_NAME to a credential_ref that the worker
    resolves at run time (never stored or logged). Tag-only images are checked at provisioning."""

    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["maef.dev/v1alpha1"]
    id: str = Field(pattern=PROFILE_ID_PATTERN)
    version: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
    transport: Literal["stdio"]
    image: str = Field(min_length=1, max_length=300, pattern=r"^[A-Za-z0-9][A-Za-z0-9._/:@-]*$")
    command: list[Annotated[str, Field(min_length=1, max_length=1024)]] = Field(min_length=1, max_length=64)
    env: dict[str, Annotated[str, Field(max_length=4096)]] = Field(default_factory=dict)
    secrets: dict[str, Annotated[str, Field(pattern=r"^env:[A-Z][A-Z0-9_]{1,63}$")]] = Field(default_factory=dict)
    # none: no network. allowlist: only through an enforced egress proxy (see allowed_hosts/allowed_ports).
    # open: the runtime's default network, unfiltered; discouraged.
    network: Literal["none", "allowlist", "open"]
    # allowlist only: exact hostnames, `*.suffix` (subdomains only) or `*` (any PUBLIC host; addresses
    # are always vetted by the proxy). Ports are the destination ports the proxy will connect to.
    allowed_hosts: list[Annotated[str, Field(min_length=1, max_length=253)]] = Field(
        default_factory=list, max_length=256
    )
    allowed_ports: list[Annotated[int, Field(ge=1, le=65535)]] = Field(default_factory=lambda: [443, 80], max_length=16)
    permission: Literal["read-only", "side-effects"]
    max_tools: int = Field(default=32, ge=1, le=256)
    # Container user. Defaults to nobody. "0:0" is for images that only work as root (e.g. the
    # interpreter lives under /root); it is honoured only on a rootless runtime, where container root
    # is the unprivileged host user, and always with every capability dropped and no-new-privileges.
    user: str | None = Field(default=None, pattern=r"^\d{1,10}:\d{1,10}$")

    @model_validator(mode="after")
    def _env_names(self):
        if self.network == "allowlist":
            if not self.allowed_hosts or not self.allowed_ports:
                raise ValueError("network allowlist needs allowed_hosts and allowed_ports")
            from .egress_proxy import (
                normalize_pattern,  # local: the proxy module is standalone
            )

            if any(normalize_pattern(h) is None for h in self.allowed_hosts):
                raise ValueError("allowed_hosts has an invalid entry")
        elif self.allowed_hosts or self.allowed_ports != [443, 80]:
            raise ValueError("allowed_hosts and allowed_ports are only valid with network: allowlist")
        for name in (*self.env, *self.secrets):
            if not re.fullmatch(_ENV_NAME, name) or name in _PROTECTED_ENV or name.startswith(_PROTECTED_ENV_PREFIXES):
                raise ValueError(f"environment variable name not allowed: {name}")
        if set(self.env) & set(self.secrets):
            raise ValueError("a name cannot be in both env and secrets")
        return self


class Budget(BaseModel):
    # Total model tokens (input + output) a run may use. Integers: exact and provider-neutral.
    max_tokens: int = Field(default=100_000, ge=1, le=100_000_000)
    max_duration_seconds: int = Field(default=300, ge=1, le=86400)
    max_turns: int = Field(default=20, ge=1, le=100000)


class Runtime(BaseModel):
    model_profile: str = "local-default"
    sandbox_profile: str = "restricted-local"


class AgentPermissions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Lets MCP servers whose manifest says `network: allowlist` or `open` run. Default: no network anywhere.
    network_open: bool = False


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
    # "<id>@<version>" references to mcp-servers/<id>.yaml; the version must match exactly.
    mcp_servers: list[str] = Field(default_factory=list)
    permissions: AgentPermissions = Field(default_factory=AgentPermissions)
    runtime: Runtime = Field(default_factory=Runtime)
    budget: Budget = Field(default_factory=Budget)
    outputs: list[str] = Field(default_factory=list)


class BudgetOverride(BaseModel):
    """A request's budget: each field may only tighten the agent's ceiling; omitted fields keep it."""

    model_config = ConfigDict(extra="forbid")
    max_tokens: int | None = Field(default=None, ge=1, le=100_000_000)
    max_duration_seconds: int | None = Field(default=None, ge=1, le=86400)
    max_turns: int | None = Field(default=None, ge=1, le=100000)


class RunRequest(BaseModel):
    agent_id: str
    instruction: str = Field(min_length=1, max_length=20000)
    budget: BudgetOverride | None = None
    # Run the definition as committed at this revision: a 40-hex commit sha, or a branch/tag name that is
    # resolved to a sha when the run is submitted. Omitted: the working tree (dirty state applies).
    agent_revision: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")


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
    kind: Literal["agent", "decision"] = "agent"
    status: Literal["admitted", "leased", "running", "succeeded", "failed", "cancelled"]
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
    cancel_requested: bool = False

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ending(self) -> Ending | None:
        """`agent` (the agent's own outcome) or `infrastructure` (a loss); None until the run ends."""
        return ending_of(self.terminal_reason)


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
    artifacts: list[ArtifactRef] = Field(default_factory=list)
