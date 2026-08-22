import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, EmailStr, Field, field_validator


# ---- auth ----
class SignupIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=256)
    account_type: Literal["individual", "organization"] = "individual"
    company_name: str | None = Field(default=None, max_length=255)
    # contact person behind an organization account; VAT and the billing address
    # are deliberately NOT asked at signup - they live on the account page
    full_name: str | None = Field(default=None, max_length=255)
    altcha: str
    accept_terms: bool = False


class AccountUpdateIn(BaseModel):
    account_type: Literal["individual", "organization"]
    full_name: str = Field(min_length=1, max_length=255)
    company_name: str | None = Field(default=None, max_length=255)
    vat_id: str | None = Field(default=None, max_length=64)
    address_line1: str | None = Field(default=None, max_length=255)
    address_line2: str | None = Field(default=None, max_length=255)
    postal_code: str | None = Field(default=None, max_length=32)
    city: str | None = Field(default=None, max_length=128)
    country: str | None = Field(default=None, max_length=128)


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class TokenIn(BaseModel):
    token: str


class ResetPasswordIn(BaseModel):
    token: str
    password: str = Field(min_length=10, max_length=256)


class EmailIn(BaseModel):
    email: EmailStr


class UserOut(BaseModel):
    id: str
    email: str
    full_name: str | None
    role: str
    email_verified: bool
    created_at: datetime


class OrgOut(BaseModel):
    id: str
    name: str
    type: str
    company_name: str | None
    vat_id: str | None
    address_line1: str | None
    address_line2: str | None
    postal_code: str | None
    city: str | None
    country: str | None
    credit_balance: float


# ---- projects ----
class RepoIn(BaseModel):
    ssh_uri: str = Field(max_length=512)


class IssueWatchIn(BaseModel):
    # §auto_dev issue-watch filters: any-of WITHIN each list, AND across the two
    # when both are set (filters only narrow, §28.10 hook-filter parity; at least
    # one of the two must be non-empty), authors an optional author allowlist.
    labels: list[str] = Field(default=[], max_length=20)
    assignees: list[str] = Field(default=[], max_length=20)
    authors: list[str] = Field(default=[], max_length=20)


class ProjectCreateIn(BaseModel):
    # No name field: the title is generated server-side from the description
    # (bootstrap heuristic at creation, LLM refinement during evaluation).
    kind: Literal["ai", "direct_quote", "auto_dev", "chat"] = "ai"
    speciality: str | None = None  # required for kind=ai/auto_dev, optional for direct_quote
    # For auto_dev this is the standing development policy ("How do you want me to
    # develop?"), editable anytime - not a one-shot deliverable spec.
    description: str = Field(min_length=1, max_length=40000)
    from_scratch: bool = True
    sovereign: bool = False
    sovereign_comment: str | None = Field(default=None, max_length=2000)
    repos: list[RepoIn] = []
    issue_watch: IssueWatchIn | None = None  # required for kind=auto_dev


class ProjectUpdateIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, min_length=1, max_length=40000)
    issue_watch: IssueWatchIn | None = None  # auto_dev only
    # §git identity: what the agent's commits are authored as. "" resets the field
    # to the instance default; omitted (null) leaves it untouched.
    git_author_name: str | None = Field(default=None, max_length=120)
    git_author_email: str | None = Field(default=None, max_length=254)

    @field_validator("git_author_name", "git_author_email")
    @classmethod
    def _no_control_chars(cls, v: str | None) -> str | None:
        # These land in `git config user.*` inside the runner: a newline would let
        # a value inject further config keys, and git itself rejects <> in a name.
        if v is not None and (any(c in v for c in "\r\n") or any(c in v for c in "<>")):
            raise ValueError("must not contain line breaks or angle brackets")
        return v

    @field_validator("git_author_email")
    @classmethod
    def _looks_like_email(cls, v: str | None) -> str | None:
        v = (v or "").strip()
        if v and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", v):
            raise ValueError("must be an email address")
        return v


class ShareIn(BaseModel):
    # §sharing: the target must already be a registered user; 'admin' is not a
    # grantable role here (it is instance-wide, never per-project).
    email: EmailStr
    role: Literal["contributor", "viewer"]


class RepoConnectIn(BaseModel):
    ssh_uri: str = Field(min_length=1, max_length=512)
    # Optional override when detection is ambiguous (a self-hosted GitHub/GitLab on
    # an unrecognised domain). Omitted → detected from the URL host.
    provider: Literal["github", "gitlab", "other"] | None = None


class RepoUpdateIn(BaseModel):
    # Make this repo the push target (exactly one across the project; the platform
    # repo is it when all are false). Server clears the others.
    is_push_target: bool | None = None
    # §14.7 auto-merge for this push repo. Enabling requires a passing auth check
    # (valid GITHUB_TOKEN/GITLAB_TOKEN with access to the repo); rejected otherwise.
    auto_merge: bool | None = None
    # Squash the agent's PR/MR into one commit when the platform auto-merges it.
    squash_on_merge: bool | None = None
    # §auto_dev: append the run's work summary to the PR-link comment posted back
    # on the issue a request was born from.
    summarize_to_issue: bool | None = None


class AnswerIn(BaseModel):
    question_id: str
    option_ids: list[str]
    comment: str | None = Field(default=None, max_length=2000)


class AnswersIn(BaseModel):
    answers: list[AnswerIn]


class MessageIn(BaseModel):
    thread: str = "main"
    body: str = Field(min_length=1, max_length=16000)
    also_email: bool = False  # admin only
    # §chat images: ids returned by POST /projects/{id}/chat-images, claimed by
    # this message. Unknown, already-claimed or someone else's ids are ignored.
    image_ids: list[str] = Field(default_factory=list, max_length=4)


class HumanAnswerIn(BaseModel):
    thread: str = "main"


class RequestIn(BaseModel):
    # No title field: it is generated server-side from the request body
    # (bootstrap heuristic at creation, LLM refinement async).
    type: Literal["feature", "edit", "bug", "production_deploy"]
    handling: Literal["ai", "manual"] = "ai"
    body: str = Field(min_length=1, max_length=16000)
    # §repo binding: the connected repo this request's builds push into
    # (null = the project's default push target at dispatch time).
    repo_id: str | None = Field(default=None, max_length=36)


class RequestUpdateIn(BaseModel):
    title: str = Field(min_length=1, max_length=255)


class MemoryIn(BaseModel):
    key: str = Field(min_length=1, max_length=255)
    value: str = Field(max_length=32000)
    is_secret: bool = False
    description: str = Field(default="", max_length=2000)


class ProjectMemorySettingsIn(BaseModel):
    # Per-project override of whether the org's global Memory is fed to dev runs.
    # null resets the project to "inherit the org default".
    use_global_memory: bool | None = None


class OrgMemorySettingsIn(BaseModel):
    # The org-wide "global memory enabled by default" switch.
    enabled_default: bool


# ---- billing ----
class TopupIn(BaseModel):
    amount: float = Field(gt=0, le=100000)


# ---- tokens ----
class ApiTokenIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)


class McpProjectCreateIn(BaseModel):
    """§MCP projects: title is the customer's own words (kept verbatim, unlike the
    wizard kinds where §9.2 derives one), the description is optional context."""
    title: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=4000)
    # Only the SPA's one-click flow mints alongside creation; the MCP tool leaves
    # it unset so no long-lived credential is written into an agent transcript.
    token_name: str | None = Field(default=None, min_length=1, max_length=128)


# ---- admin ----
class RetryBuildIn(BaseModel):
    # §run chains Start fresh: true discards the failed chain (new workspace,
    # new branch) instead of resuming it. Absent body = plain resume.
    fresh: bool = False


class RoutineIn(BaseModel):
    """§routines: a saved prompt. `schedule_cron` empty = fired by hand only."""
    title: str = Field(min_length=1, max_length=255)
    prompt: str = Field(min_length=1, max_length=8000)
    enabled: bool = True
    schedule_cron: str = Field(default="", max_length=64)
    repo_id: str | None = Field(default=None, max_length=36)


class RoutineUpdateIn(BaseModel):
    """Partial update; omit a field to leave it unchanged. An empty
    schedule_cron clears the schedule back to hand-fired."""
    title: str | None = Field(default=None, min_length=1, max_length=255)
    prompt: str | None = Field(default=None, min_length=1, max_length=8000)
    enabled: bool | None = None
    schedule_cron: str | None = Field(default=None, max_length=64)
    repo_id: str | None = Field(default=None, max_length=36)


class AppSettingsIn(BaseModel):
    # partial update: omit a field to leave that flag unchanged
    pause_ai_deposits: bool | None = None
    pause_direct_deposits: bool | None = None
    pause_auto_dev_deposits: bool | None = None
    pause_chat_deposits: bool | None = None
    # §routines: instance kill switch for scheduled saved prompts (the
    # feature is on by default; this is what a future paid tier gates).
    routines_disabled: bool | None = None
    # §chat images: the instance-default model has no ModelEndpoint row to carry a
    # probe verdict, so the admin declares it here (saved endpoints are declared or
    # probed on the Model configuration page instead).
    default_model_supports_images: bool | None = None
    # §egress: dev-sandbox egress lockdown (K8s-enforced). Applied only when sent.
    egress_lockdown_enabled: bool | None = None
    egress_allowlist: list[str] | None = None  # FQDN / *.fqdn / IP / CIDR entries
    # §fees: per-speciality engagement-fee overrides, replacing the stored map
    # when sent - {speciality_id: credits}; null clears an id back to the
    # specialities.json default.
    speciality_fee_overrides: dict[str, float | None] | None = None
    # §legal identity: the operating company named in the landing's Privacy policy
    # and Terms of service. "" clears the override (the landing's built-in value
    # applies again); omitted leaves the stored value alone.
    legal_name: str | None = Field(default=None, max_length=200)
    legal_address: str | None = Field(default=None, max_length=500)


# ---- knowledge bases (§KB, admin) ----
class KnowledgeBaseCreateIn(BaseModel):
    # `mcp` (default) or `git` KBs are created via the API (local + context7 are
    # seeded). For git: name is optional (derived from the host), auth_kind picks
    # SSH (platform generates a deploy key) vs HTTP (api_key is the PAT), ref is the
    # branch. mcp requires name; the endpoint validates the per-kind requirements.
    kind: str = "mcp"  # mcp | git
    name: str | None = Field(default=None, max_length=255)
    uri: str = Field(min_length=1, max_length=512)
    auth_kind: str | None = None  # git only: ssh | http
    ref: str | None = Field(default=None, max_length=128)  # git branch, default main
    api_key: str | None = Field(default=None, max_length=4000)  # mcp key / git HTTP PAT
    # git HTTP only - Basic username for the token: empty = oauth2 (PAT); a GitLab
    # deploy token needs its generated gitlab+deploy-token-N username.
    http_username: str | None = Field(default=None, max_length=255)


class ToolPatchIn(BaseModel):
    """§Tools global row edit: URL (self-hosted GitLab MCP endpoints), key
    (empty string clears), enable flag (enabling re-scans server-side)."""
    url: str | None = Field(default=None, max_length=512)
    api_key: str | None = Field(default=None, max_length=4000)
    enabled: bool | None = None


class ProjectToolPatchIn(BaseModel):
    """§Tools per-project override. `enabled` is TRI-STATE (None = inherit the
    global flag) - presence is detected via model_fields_set, so omitting the
    field leaves the override untouched while an explicit null resets to
    inherit. Empty url/api_key strings clear the respective override."""
    url: str | None = Field(default=None, max_length=512)
    api_key: str | None = Field(default=None, max_length=4000)
    enabled: bool | None = None


class KnowledgeBasePatchIn(BaseModel):
    # partial update: omit a field to leave it unchanged. For built-in local/
    # context7 rows only `enabled` may be set; mcp and git rows accept the relevant
    # fields. api_key: provide a new value to re-encrypt, omit to leave it untouched.
    # Enabling a git row re-runs the connection check server-side (409 if it fails).
    enabled: bool | None = None
    name: str | None = Field(default=None, min_length=1, max_length=255)
    uri: str | None = Field(default=None, min_length=1, max_length=512)
    ref: str | None = Field(default=None, max_length=128)  # git branch
    api_key: str | None = Field(default=None, max_length=4000)
    http_username: str | None = Field(default=None, max_length=255)  # git HTTP Basic username


class KbBlockOverrideIn(BaseModel):
    # §KB tiers: pin one content block's class. Stored by content hash
    # (origin='override'), beating every signal/classifier verdict until the
    # block's text - and so its hash - changes.
    content_class: Literal["fact", "rule", "procedure"]


class StatusIn(BaseModel):
    status: str
    note: str | None = Field(default=None, max_length=512)


# Docker-style resource quantity (shared by Program resources and the per-project
# dev-pod requests): 0.5 / 512m / 2g.
_RESOURCE_RE = r"^[0-9]+(\.[0-9]+)?[bkmgBKMG]?$"


class ProjectPatchIn(BaseModel):
    tier: Literal["mvp", "production"] | None = None
    subdomain: str | None = Field(default=None, max_length=128)
    block_auto_development: bool | None = None
    # Per-project KB selection: null resets to "all enabled KBs", [] selects none,
    # a list selects exactly those KnowledgeBase ids. None also means "field
    # omitted", so the route checks model_fields_set before applying it.
    kb_ids: list[str] | None = Field(default=None, max_length=100)
    # Per-project agent-iteration cap: null resets to the instance default; the
    # route checks model_fields_set (None also means "field omitted").
    dev_max_iterations: int | None = Field(default=None, ge=1, le=500)
    # §parallel-builds MR3: per-project concurrency entitlement (1 = serialized,
    # null resets to the instance default; server-side ceiling re-clamped by
    # effective_parallel_limit regardless).
    dev_parallel_limit: int | None = Field(default=None, ge=1, le=16)
    # §dev-pod resources: per-project dev-run pod scheduling requests, docker-style
    # like Program resources; null resets to the deployer's instance defaults
    # (model_fields_set semantics like the caps above).
    dev_cpu_request: str | None = Field(default=None, pattern=_RESOURCE_RE)
    dev_mem_request: str | None = Field(default=None, pattern=_RESOURCE_RE)


# A saved endpoint's provider preset: an auth + base-URL shape, not a capability.
# openrouter/eurouter/carouter are OpenAI-compatible gateways (the EU and CA ones
# keep the request inside their region, which is what §sovereign tracks promise).
ModelProviderName = Literal["openai", "anthropic", "mistral", "openrouter", "eurouter",
                            "carouter", "custom"]


class ModelEndpointIn(BaseModel):
    label: str = Field(min_length=1, max_length=128)
    provider: ModelProviderName = "custom"
    base_url: str = Field(min_length=1, max_length=512)
    api_key: str = Field(min_length=1)
    model_name: str = Field(min_length=1, max_length=128)
    # Per-1M-token price (USD), required only when model_name isn't in the price table.
    input_price: float | None = Field(default=None, ge=0)
    output_price: float | None = Field(default=None, ge=0)
    # Optional per-1M price for prompt-cache reads (custom-priced models only).
    cached_input_price: float | None = Field(default=None, ge=0)
    # Free-form so exotic providers' tiers work (xhigh, max, minimal, a gateway's
    # own names); the endpoint Test probe reports whether the provider accepts it.
    reasoning_effort: str | None = Field(default=None, max_length=16,
                                         pattern=r"^[A-Za-z0-9_-]{1,16}$")
    # §chat images: the admin can declare image support instead of waiting for the
    # Test probe (some providers reject the probe for unrelated reasons). null =
    # leave it to the probe.
    supports_images: bool | None = None


class ModelEndpointPatchIn(BaseModel):
    label: str | None = Field(default=None, max_length=128)
    provider: ModelProviderName | None = None
    base_url: str | None = Field(default=None, max_length=512)
    api_key: str | None = None  # blank/None → keep the existing key
    model_name: str | None = Field(default=None, max_length=128)
    input_price: float | None = Field(default=None, ge=0)
    output_price: float | None = Field(default=None, ge=0)
    cached_input_price: float | None = Field(default=None, ge=0)
    # "" resets to provider default (None = untouched); free-form for exotic tiers
    reasoning_effort: str | None = Field(default=None, max_length=16,
                                         pattern=r"^$|^[A-Za-z0-9_-]{1,16}$")
    supports_images: bool | None = None


class ModelCatalogIn(BaseModel):
    provider: ModelProviderName = "custom"
    base_url: str = Field(min_length=1, max_length=512)
    # The key typed into the form, else fall back to endpoint_id's stored key
    # (editing never re-exposes a saved key).
    api_key: str | None = None
    endpoint_id: str | None = None


class ModelConfigIn(BaseModel):
    # endpoint_id None (or empty) clears the per-project override → global default.
    # The model comes from the selected endpoint, so there is no separate field.
    endpoint_id: str | None = None


class PriceIn(BaseModel):
    price_credits: float = Field(ge=0)


class QuoteIn(BaseModel):
    amount: float = Field(gt=0)


class QuoteCreateIn(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    details: str = Field(min_length=1, max_length=40000)
    price_credits: float = Field(gt=0)


class QuotePatchIn(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    details: str | None = Field(default=None, min_length=1, max_length=40000)
    price_credits: float | None = Field(default=None, gt=0)


class QuoteDecisionIn(BaseModel):
    comment: str | None = Field(default=None, max_length=4000)


class QuoteCancelIn(BaseModel):
    comment: str | None = Field(default=None, max_length=4000)
    refund_credits: float | None = Field(default=None, ge=0)


class CreditAdjustIn(BaseModel):
    amount: float
    reason: str = Field(min_length=1, max_length=512)


# ---- hub ----
class HubGrantIn(BaseModel):
    # Optional in the body: the MCP sidecar forwards org_id as a QUERY param (like
    # every other hub route), so the endpoint accepts it from either place. A direct
    # caller may still pass it in the body.
    org_id: str | None = Field(default=None, max_length=36)
    amount: float
    idempotency_key: str = Field(min_length=1, max_length=128)
    detail: str | None = Field(default=None, max_length=512)


class HubOrgCreateIn(BaseModel):
    """Brokered-org creation (§hub pass-through): the hub creates a dedicated
    spoke org for one of ITS customers. Idempotent on idempotency_key - a
    replayed create returns the existing org. The name is hub-chosen (a neutral
    alias in anonymous mode), never customer PII by contract."""
    name: str = Field(min_length=1, max_length=255)
    idempotency_key: str = Field(min_length=8, max_length=128)


class HubProjectCreateIn(BaseModel):
    """From-hub project creation (§pass-through P1). The hub acts as the
    'customer' actor on a brokered (hub_managed) org; hub_ref is the hub's own
    project id, echoed back in outbox events.

    `repos` connects the customer's (or the hub's own) git remotes exactly as the
    customer create does - first one is the push target - so several from-hub
    projects can build into ONE shared repository (§hub shared repo): the hub
    provisions an engagement-level repo, installs each project's deploy key on it,
    and passes it here. With a push-target repo connected, no platform GitLab repo
    is provisioned - the connected repo is where the work lives."""
    spoke_org_id: str = Field(min_length=1, max_length=36)
    kind: str = Field(default="ai", pattern="^(ai|direct_quote|chat)$")
    speciality: str | None = Field(default=None, max_length=64)
    description: str = Field(min_length=1, max_length=20000)
    from_scratch: bool = True
    sovereign: bool = False
    sovereign_comment: str | None = Field(default=None, max_length=2000)
    hub_ref: str | None = Field(default=None, max_length=64)
    repos: list[RepoIn] = Field(default=[], max_length=5)
    # Turn the platform-merge flow on for the push repo: with a valid GITLAB_TOKEN/
    # GITHUB_TOKEN Memory secret (the hub feeds one per project) the agent's MR is
    # security-reviewed and merged automatically; without one the flow degrades to
    # the customer-merge path exactly as it does for direct customers.
    auto_merge: bool = False


class HubProjectActionIn(BaseModel):
    """One customer-actor action on a from-hub project, wrapping the SAME
    services/project_actions functions the SPA routes use."""
    action: str = Field(
        pattern="^(evaluate|submit|approve-delivery|retry-build|stop-build|require-admin|demo-start|demo-stop)$")


class HubProjectMessageIn(BaseModel):
    """A chat message posted by the hub on the customer's behalf (author is
    always 'customer' - the hub never impersonates the admin or the agent)."""
    thread: str = "main"
    body: str = Field(min_length=1, max_length=16000)


class HubRequestIn(BaseModel):
    type: Literal["feature", "edit", "bug", "production_deploy"]
    handling: Literal["ai", "manual"] = "ai"
    body: str = Field(min_length=1, max_length=16000)
    # §repo binding (additive): pins the request's builds to one connected repo.
    repo_id: str | None = Field(default=None, max_length=36)


class McpDelegateIn(BaseModel):
    """§MCP delegate: a slice of work handed to the platform from someone's
    terminal agent. `spec` is stored - it is the work order the build runs from
    (unlike a consult question, which is never persisted)."""
    spec: str = Field(min_length=1, max_length=16000)
    type: Literal["feature", "edit", "bug"] = "feature"


class AdminUserPatchIn(BaseModel):
    """§user blocking: the only admin-editable user field so far. None = leave
    unchanged, so the shape can grow more fields without breaking callers."""
    blocked: bool | None = None


class McpConsultIn(BaseModel):
    """§MCP consult: a question about the project's own codebase, answered by a
    read-only harness pass. Never persisted - see api/mcp_delegate.consult."""
    question: str = Field(min_length=1, max_length=4000)


class HubEvalIn(BaseModel):
    """Owner-consented eval questions run through the spoke's knowledge stack,
    unbilled. Batch and per-question length are capped so a runaway hub can't burn
    the owner's LLM budget; each question mirrors KnowledgeQueryIn's 2000-char cap."""
    questions: list[str] = Field(min_length=1, max_length=4)
    k: int = Field(default=6, ge=1, le=12)

    @field_validator("questions")
    @classmethod
    def _cap_question_length(cls, v: list[str]) -> list[str]:
        for q in v:
            if not (1 <= len(q) <= 2000):
                raise ValueError("each question must be 1-2000 characters")
        return v


# ---- programs (§28) ----


class ProgramCreateIn(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    short_description: str = Field(default="", max_length=512)
    # "group/name" in the platform GitLab; immutable after creation
    gitlab_repo_path: str = Field(min_length=3, max_length=255,
                                  pattern=r"^[\w.-]+(/[\w.-]+)+$")


class ProgramUpdateIn(BaseModel):
    """Partial update - omitted fields stay unchanged. gitlab_repo_path is
    immutable by design. model_endpoint_id picks a saved model endpoint (the
    §model-endpoints library); null resets to the global default - the route
    checks model_fields_set so only an explicitly sent field is applied."""
    title: str | None = Field(default=None, min_length=1, max_length=255)
    short_description: str | None = Field(default=None, max_length=512)
    default_branch: str | None = Field(default=None, min_length=1, max_length=128)
    is_published: bool | None = None
    schedulable: bool | None = None
    model_endpoint_id: str | None = Field(default=None, max_length=36)
    credit_markup: float | None = Field(default=None, gt=0, le=100)
    timeout_minutes: int | None = Field(default=None, ge=1, le=240)
    cpu_request: str | None = Field(default=None, pattern=_RESOURCE_RE)
    cpu_limit: str | None = Field(default=None, pattern=_RESOURCE_RE)
    mem_request: str | None = Field(default=None, pattern=_RESOURCE_RE)
    mem_limit: str | None = Field(default=None, pattern=_RESOURCE_RE)


class ProgramInstanceCreateIn(BaseModel):
    label: str = Field(default="", max_length=255)


class HookFiltersIn(BaseModel):
    # §28 inbound hooks: AND-composed allowlists; an empty list = no constraint
    # for that dimension (the per-instance secret is the auth, filters narrow).
    actions: list[str] = Field(default=[], max_length=20)
    labels: list[str] = Field(default=[], max_length=20)
    assignees: list[str] = Field(default=[], max_length=20)
    authors: list[str] = Field(default=[], max_length=20)


class ProgramInstanceUpdateIn(BaseModel):
    """Partial update. inputs replaces the whole value map (keys = template
    input names; values as strings, coerced server-side)."""
    label: str | None = Field(default=None, max_length=255)
    inputs: dict[str, str] | None = None
    # §28 per-instance model: a saved ModelEndpoint id, or "" to clear the pick
    # back to the program's admin-set default (absent = leave unchanged).
    model_endpoint_id: str | None = Field(default=None, max_length=36)
    webhook_url: str | None = Field(default=None, max_length=512)
    schedule_enabled: bool | None = None
    schedule_cron: str | None = Field(default=None, max_length=64)
    hook_enabled: bool | None = None
    hook_filters: HookFiltersIn | None = None


# ---- knowledge (MCP) ----
class KnowledgeQueryIn(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    k: int = Field(default=6, ge=1, le=12)
