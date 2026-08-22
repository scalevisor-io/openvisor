import uuid
from datetime import datetime, timezone

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Organization(Base):
    __tablename__ = "organization"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(255))
    type: Mapped[str] = mapped_column(String(20), default="individual")  # individual|organization
    company_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    vat_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # billing address - required (except line2) while type='organization', kept when
    # switching back to individual so nothing is lost on a round-trip
    address_line1: Mapped[str | None] = mapped_column(String(255), nullable=True)
    address_line2: Mapped[str | None] = mapped_column(String(255), nullable=True)
    postal_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    city: Mapped[str | None] = mapped_column(String(128), nullable=True)
    country: Mapped[str | None] = mapped_column(String(128), nullable=True)
    credit_balance: Mapped[float] = mapped_column(Float, default=0.0)
    stripe_customer_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Whether the org's global Memory (OrgMemory) applies to a project by default.
    # A project can override per-project via Project.use_global_memory; when that
    # override is null the project follows this flag (global memory §).
    global_memory_enabled_default: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true")
    # Hub link (§hub): hub_managed marks a brokered org the hub created for one of
    # ITS customers (scopes the hub's usage/event visibility together with grant
    # history); hub_create_key is the idempotent-create ledger - a replayed create
    # returns the existing org instead of minting a duplicate.
    hub_managed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    hub_create_key: Mapped[str | None] = mapped_column(String(128), nullable=True, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    users: Mapped[list["User"]] = relationship(back_populates="org")


class User(Base):
    __tablename__ = "user"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organization.id"))
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    # the human's full name; asked at signup for organization accounts (the company
    # name alone doesn't identify the contact) and editable on the account page
    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default="customer")  # customer|admin
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    # §user blocking: admin-set lockout (the /admin/users page). Login refuses
    # with an explicit message, and existing sessions and API tokens stop
    # authenticating (deps.get_current_user, deps._resolve_api_token, the MCP
    # server's authenticate). Admin accounts can never be blocked.
    blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    # when the user accepted the ToS + privacy policy at signup (GDPR proof of consent);
    # null only for pre-consent accounts and the seeded admin
    tos_accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    org: Mapped[Organization] = relationship(back_populates="users")


class Membership(Base):
    __tablename__ = "membership"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organization.id"))
    user_id: Mapped[str] = mapped_column(ForeignKey("user.id"))
    role: Mapped[str] = mapped_column(String(20), default="owner")  # owner|member
    __table_args__ = (UniqueConstraint("org_id", "user_id"),)


class ProjectShare(Base):
    """§sharing: the project made visible to a registered user outside the owning
    org. 'contributor' acts as the customer on every project surface (actions are
    billed to the owning org's wallet, like any customer action); 'viewer' gets
    the same surface read-only (mutating routes 403). Admin can never be granted
    here - it is a global instance role, not a per-project one. No invitation or
    confirmation step: a share row IS the access."""
    __tablename__ = "project_share"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("user.id"), index=True)
    role: Mapped[str] = mapped_column(String(16), default="viewer")  # contributor|viewer
    created_by: Mapped[str | None] = mapped_column(ForeignKey("user.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (UniqueConstraint("project_id", "user_id"),)


class Project(Base):
    __tablename__ = "project"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organization.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    # True once the customer renamed the project; stops the evaluation-time
    # LLM title pass from overwriting their choice.
    name_customized: Mapped[bool] = mapped_column(Boolean, default=False)
    kind: Mapped[str] = mapped_column(String(16), default="ai")  # ai|direct_quote|auto_dev|chat|mcp
    # §hub pass-through: 'hub' marks a project the hub created for one of ITS
    # customers - the hub-scoped API may ONLY touch source='hub' projects, and
    # only those feed the hub_project_event outbox. hub_ref is the hub's own
    # project id, echoed back in events so the hub correlates without a lookup.
    source: Mapped[str] = mapped_column(String(16), default="customer",
                                        server_default="customer")  # customer|hub
    hub_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    speciality: Mapped[str | None] = mapped_column(String(64), nullable=True)
    description: Mapped[str] = mapped_column(Text)
    tier: Mapped[str] = mapped_column(String(20), default="mvp")  # mvp|production
    status: Mapped[str] = mapped_column(String(32), default="draft", index=True)
    # Admin-set per-project agent-iteration cap; null = the instance default
    # (settings.dev_max_iterations_default). Bounded by the run wall-clock either way.
    dev_max_iterations: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # §dev-pod resources: admin-set scheduling REQUESTS for this project's dev-run
    # pods, docker-style values like Program resources ("0.5" / "512m" / "4g");
    # null = the deployer's instance defaults. On K8s a request above the instance
    # limit raises that run's limit to match (request <= limit must hold); compose
    # honors the memory request as --memory-reservation and cannot honor a cpu one.
    dev_cpu_request: Mapped[str | None] = mapped_column(String(16), nullable=True)
    dev_mem_request: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # §parallel-builds (docs/PARALLEL_BUILDS.md §2): per-project concurrency
    # entitlement override; null = instance default. Resolved ONLY through
    # dev_concurrency.effective_parallel_limit (the licensing chokepoint).
    dev_parallel_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    block_auto_development: Mapped[bool] = mapped_column(Boolean, default=False)
    # §git identity: what the agent's commits in this project are authored as
    # (git user.name / user.email in the runner). null = the instance default
    # derived from the brand; resolved ONLY through repos.git_identity().
    git_author_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    git_author_email: Mapped[str | None] = mapped_column(String(254), nullable=True)
    from_scratch: Mapped[bool] = mapped_column(Boolean, default=True)
    sovereign: Mapped[bool] = mapped_column(Boolean, default=False)
    sovereign_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Per-project override of whether the org's global Memory (OrgMemory) is fed to
    # this project's dev runs. null = inherit Organization.global_memory_enabled_default;
    # a project-level Memory key overrides a global key of the same name (global memory §).
    use_global_memory: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # Per-project KB selection (§KB): which KnowledgeBase rows feed this project's
    # dev runs (MCP servers + RAG retrieval). [] = none - THE DEFAULT for new
    # projects (KBs are opt-in per project) - a list = exactly those ids, and
    # null = all enabled KBs (legacy rows created before the opt-in default; the
    # modal migrates them to an explicit list on save). Selection only ever
    # NARROWS the globally enabled set - a KB disabled in /admin/knowledge-bases
    # stays off in every project.
    kb_ids: Mapped[list | None] = mapped_column(JSON, nullable=True, default=list)
    # §auto_dev: the sentinel's issue-watch filters on its push repo -
    # {labels: [..], assignees: [..], authors: [..]}. A matching open issue becomes
    # an ai Request. labels/assignees are any-of triggers (at least one required),
    # authors an optional allowlist (issue bodies are untrusted input to the agent).
    issue_watch: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    gitlab_project_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gitlab_ssh_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    gitlab_web_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    workspace_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    ssh_public_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    ssh_private_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    subdomain: Mapped[str | None] = mapped_column(String(128), unique=True, nullable=True)
    demo_basic_auth_user: Mapped[str | None] = mapped_column(String(64), nullable=True)
    demo_basic_auth_pass_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    demo_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    demo_state: Mapped[str] = mapped_column(String(16), default="stopped")  # stopped|running
    demo_deployed_once: Mapped[bool] = mapped_column(Boolean, default=False)
    demo_last_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    demo_last_stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    tokens_consumed: Mapped[int] = mapped_column(Integer, default=0)
    cost_credits: Mapped[float] = mapped_column(Float, default=0.0)
    evaluation: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # moderation/feasibility/estimate
    # Dev pipeline observability / fail-safe (§14.5). dev_run_state tracks the
    # build sub-lifecycle independently of the customer-facing status:
    #   idle | running | awaiting_merge | deploying | failed | done
    dev_run_state: Mapped[str] = mapped_column(String(16), default="idle")
    dev_run_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dev_run_log: Mapped[str | None] = mapped_column(Text, nullable=True)  # last runner log tail
    dev_run_error: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # The harness fingerprint (agent_eval.compute_harness_version) that produced
    # the current/last run - stamped at run start. Recorded next to the model id
    # so a build's outcome can be attributed to the harness config that made it
    # (a harness change and a model change are otherwise indistinguishable).
    dev_harness_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # The current work-unit's git branch (LLM-named from the description/request,
    # honoring any customer branch convention; sanitized). Kept across resumes so
    # a retry continues the same branch; cleared when a new request starts so
    # each change gets its own properly-named branch. Null → the legacy default.
    dev_branch: Mapped[str | None] = mapped_column(String(80), nullable=True)
    # §working method plan gate: the plan produced by the plan-only pass and its
    # lifecycle (null = no plan flow, 'proposed' = awaiting the customer's
    # one-click approval in chat, 'approved' = pinned into the implement run).
    dev_plan: Mapped[str | None] = mapped_column(Text, nullable=True)
    dev_plan_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    dev_pr_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dev_pr_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # §work answers: the agent-authored summary of the last published run (the
    # redacted .openvisor/pr.md that becomes the PR/MR description), kept so the
    # chat can explain what a build actually did long after the workspace is
    # recycled. The per-request copy lives on Request.work_summary.
    dev_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Latest security-review snapshot {verdict, findings, attempts, reviewed_at}
    # so the customer/admin can see why an auto-merge did or didn't proceed.
    dev_security_review: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # §Phase 1 #5 acceptance checks {checks, passed, total, results, at} - advisory
    # spec-conformance run against the booted demo; never gates delivery.
    dev_acceptance: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # §Phase 2 sovereign gate {clean, findings, at} - deterministic verification that
    # a sovereign project uses no US-hyperscaler technology (the moat's audit trail).
    dev_sovereign: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # §Phase 2 DevSecOps SBOM/CVE gate {scanned, components, critical, high, findings, at}
    # - the SBOM inventory + trivy CVE scan of a devsecops-hardened build.
    dev_sbom: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # When set, the active dev run builds this customer Request (§14: AI-handled
    # feature/edit/bug requests spawn a scoped dev job) instead of the full MVP.
    dev_request_id: Mapped[str | None] = mapped_column(ForeignKey("request.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    repos: Mapped[list["ProjectRepo"]] = relationship(cascade="all, delete-orphan")
    model_config_row: Mapped["ProjectModelConfig | None"] = relationship(
        back_populates="project", uselist=False
    )


class ProjectRepo(Base):
    """A git repo the customer connected to the project (their own GitHub/GitLab/
    other-host repo). The platform-auto-generated GitLab repo is NOT a row here -
    it lives on Project.gitlab_* and is the implicit push target when no connected
    repo is marked one. Exactly one repo (a row here OR the platform repo) is the
    push target = where the AI does its work; enforced in the repos API."""
    __tablename__ = "project_repo"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), index=True)
    ssh_uri: Mapped[str] = mapped_column(String(512))
    role: Mapped[str] = mapped_column(String(20), default="primary")  # primary|secondary
    # Detected from the URL host (github|gitlab|other). "other" can be built into
    # (branch pushed over the deploy key, customer merges) but never auto-merged:
    # PR/MR auto-merge is only supported on github and gitlab.
    provider: Mapped[str] = mapped_column(String(16), default="other", server_default="other")
    # The push repo - where dev runs push agent/mvp. At most one true across a
    # project's connected repos; when none is true the platform GitLab repo is it.
    is_push_target: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # §14.7 auto-merge (per push repo). When true AND the matching PAT is present
    # (GITHUB_TOKEN / GITLAB_TOKEN Memory secret, validated by the auth check), the
    # agent's PR/MR is security-reviewed and merged automatically (fixing findings
    # first). Only settable on a github/gitlab repo whose auth check passed.
    auto_merge: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # Squash the agent's PR/MR into one commit when the PLATFORM merges it (§14.7
    # auto-merge). Checkbox on the push repo, on by default; irrelevant when the
    # customer merges manually with their own tooling.
    squash_on_merge: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    # §auto_dev: when a request was born from a repo issue, append the run's work
    # summary (the redacted .openvisor/pr.md) to the PR-link comment posted back
    # on that issue. Off by default - the summary lands in an externally visible
    # issue thread, so posting it is the customer's call.
    summarize_to_issue: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")


class ModelEndpoint(Base):
    """An instance-admin-level saved LLM API endpoint + credential the admin reuses
    across projects (§model config). Global (spoke-wide, the owner's - not per
    customer org, no org_id), like KnowledgeBase. `provider` is a preset hint
    (openai|anthropic|mistral|openrouter|eurouter|carouter|custom) for the UI badge + base-URL prefill; `base_url`
    is the OpenAI-compatible endpoint the runner routes through LiteLLM; `api_key_enc`
    is the envelope-encrypted key, NEVER returned by the API (only has_api_key). A
    project's ProjectModelConfig points at one of these by endpoint_id; the model
    NAME stays on the per-project row (it varies per project)."""
    __tablename__ = "model_endpoint"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    label: Mapped[str] = mapped_column(String(128))
    provider: Mapped[str] = mapped_column(String(16), default="custom")  # openai|anthropic|mistral|openrouter|eurouter|carouter|custom
    base_url: Mapped[str] = mapped_column(String(512))
    api_key_enc: Mapped[str] = mapped_column(Text)
    # §effort: reasoning-effort override for calls through this endpoint
    # (low|medium|high; null = provider default). Dev runs fall back to HIGH,
    # tiny utility calls always request low (graceful retry strips it on
    # providers that reject the parameter).
    reasoning_effort: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # The model this endpoint runs (the exact api_model string). A project's config
    # selects the whole endpoint, so the model rides with it. Nullable only so a row
    # predating this column doesn't block the migration; the API requires it on create.
    model_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Admin-supplied price per 1,000,000 tokens (USD) used ONLY when model_name is
    # not in the static price table - so an admin can save an endpoint for a model
    # the platform doesn't yet know how to bill (services/llm.record_usage falls
    # back to these). Both null for a model already in the table (the table wins).
    input_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    output_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Per-1M price for prompt-cache READS (§18), meaningful only alongside the
    # custom input/output prices above. Null = no verified cached rate: cached
    # reads bill at input_price (no discount).
    cached_input_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    # §chat images: can this model read images? TRI-STATE, and the distinction
    # matters - null means "nobody has checked", which disables image attachments
    # just like a hard false, but says something different in the UI. There is no
    # capability-discovery API in the OpenAI-compatible contract (/models returns
    # ids, not capabilities), so the endpoint Test probe IS the discovery: it
    # sends a 1x1 PNG and reads the provider's answer. `supports_images_source`
    # records whether that verdict came from the probe or from an admin who
    # declared it by hand (providers that reject the probe for unrelated reasons).
    supports_images: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    supports_images_source: Mapped[str | None] = mapped_column(String(8), nullable=True)  # probe|admin
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ProjectModelConfig(Base):
    __tablename__ = "project_model_config"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), unique=True)
    # A new config references a saved ModelEndpoint (base_url + key rotate in one
    # place). The inline openai_* columns are the LEGACY path (rows created before
    # saved endpoints) and are nullable so a new endpoint-backed row leaves them
    # empty; resolution (_project_model_config) prefers endpoint_id, else the inline
    # pair, else the global default.
    endpoint_id: Mapped[str | None] = mapped_column(
        ForeignKey("model_endpoint.id", ondelete="SET NULL"), nullable=True)
    openai_base_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    openai_api_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Only set on a legacy inline row; an endpoint-backed row takes the model from
    # the endpoint, so this is null there.
    model_name: Mapped[str | None] = mapped_column(String(128), nullable=True)

    project: Mapped[Project] = relationship(back_populates="model_config_row")
    endpoint: Mapped["ModelEndpoint | None"] = relationship()


class ProjectMemory(Base):
    __tablename__ = "project_memory"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), index=True)
    author: Mapped[str] = mapped_column(String(20))  # customer|admin
    key: Mapped[str] = mapped_column(String(255))
    value_enc: Mapped[str] = mapped_column(Text)  # always envelope-encrypted at rest
    is_secret: Mapped[bool] = mapped_column(Boolean, default=False)
    # Free-text hint about what this value is for; fed to the dev agent
    # (key + description + value) so it isn't guessing from the key name alone.
    description: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    __table_args__ = (UniqueConstraint("project_id", "key"),)


class OrgMemory(Base):
    """Organization-scoped Memory (global memory §): shared across every project in
    the org. A project consumes it when its effective use_global_memory is on
    (Project.use_global_memory override, else Organization.global_memory_enabled_default);
    a project-level ProjectMemory key overrides a global key of the same name. Same
    shape (and same envelope-encrypted value_enc/is_secret contract) as ProjectMemory
    so the dev pipeline can merge the two transparently."""
    __tablename__ = "org_memory"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organization.id"), index=True)
    author: Mapped[str] = mapped_column(String(20))  # customer|admin
    key: Mapped[str] = mapped_column(String(255))
    value_enc: Mapped[str] = mapped_column(Text)  # always envelope-encrypted at rest
    is_secret: Mapped[bool] = mapped_column(Boolean, default=False)
    description: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    __table_args__ = (UniqueConstraint("org_id", "key"),)


class ProjectFile(Base):
    """Customer-imported project file (spec, dataset, asset…) from the Memory & files
    tab. Stored in-DB like QuoteAttachment (alpha scale); the worker stages every file
    into the dev sandbox at /workspace/.openvisor/files/<filename> on each dispatch and
    lists them in the agent task. NOT for secrets - those belong in Memory (encrypted,
    exported as env vars). Re-uploading a filename replaces it."""
    __tablename__ = "project_file"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), index=True)
    author: Mapped[str] = mapped_column(String(20))  # customer|admin
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(128), default="application/octet-stream")
    size_bytes: Mapped[int] = mapped_column(Integer)
    data: Mapped[bytes] = mapped_column(LargeBinary)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    __table_args__ = (UniqueConstraint("project_id", "filename"),)


class OnboardingAnswer(Base):
    __tablename__ = "onboarding_answer"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), index=True)
    question_id: Mapped[str] = mapped_column(String(64))
    answer: Mapped[dict] = mapped_column(JSON)  # {option_ids: [...], comment: str|null}
    __table_args__ = (UniqueConstraint("project_id", "question_id"),)


class Message(Base):
    __tablename__ = "message"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), index=True)
    thread: Mapped[str] = mapped_column(String(64), default="main")  # main | request:<id>
    author: Mapped[str] = mapped_column(String(20))  # customer|admin|agent|system
    body: Mapped[str] = mapped_column(Text)
    # Platform-authored structured payload (§12 clarifying question:
    # {kind:"question", question, options:[{label,description}], allow_free_text});
    # never accepted from a client - MessageIn carries no meta field.
    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    emailed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    # Immutable: no update/delete endpoints exist, ever.


class ProjectRoutine(Base):
    """§routines: a saved prompt on a project that can run on a schedule.

    A routine is a TEMPLATE, not a run: every firing creates an ordinary
    `Request` seeded with `prompt` and dispatches it down the normal pipeline
    (thread, dev run, PR/MR, billing) - the same thing the auto_dev sweep does
    with a repo issue. One routine therefore owns many requests over time,
    exactly as Program owns ProgramRun.

    `schedule_cron` empty = a saved prompt fired by hand; set = also fired by
    the sweep when `next_run_at` comes due. Unlike auto_dev there is no natural
    dedup key (the same prompt every Monday IS the point), so `last_request_id`
    is the guard: a firing is skipped while the previous one is still open,
    which is what stops a weekly routine stacking builds on an unmerged PR."""
    __tablename__ = "project_routine"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), index=True)
    title: Mapped[str] = mapped_column(String(255))
    prompt: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    schedule_cron: Mapped[str] = mapped_column(String(64), default="")
    next_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True)
    last_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    # The request the last firing created - the skip-while-open guard reads its
    # status. SET NULL so deleting history never deletes the routine.
    last_request_id: Mapped[str | None] = mapped_column(
        ForeignKey("request.id", ondelete="SET NULL"), nullable=True)
    # §repo binding: which connected repo the spawned request builds into.
    # Null = resolve the push target at fire time, like any other request.
    repo_id: Mapped[str | None] = mapped_column(
        ForeignKey("project_repo.id", ondelete="SET NULL"), nullable=True)
    # Why the last sweep tick did nothing (previous run still open, no credits,
    # build slots busy) - shown to the customer so a quiet routine is never a
    # mystery, cleared on a successful firing.
    last_skip_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Request(Base):
    __tablename__ = "request"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), index=True)
    type: Mapped[str] = mapped_column(String(32))  # feature|edit|bug|production_deploy
    handling: Mapped[str] = mapped_column(String(16), default="ai")  # ai|manual
    status: Mapped[str] = mapped_column(String(32), default="open")  # open|quoted|in_progress|done|rejected
    title: Mapped[str] = mapped_column(String(255))
    price_credits: Mapped[float | None] = mapped_column(Float, nullable=True)
    # LLM usage attributed to this request's thread (scoped dev runs + classifier
    # calls); a per-request view of the project-level counters.
    tokens_consumed: Mapped[int] = mapped_column(Integer, default=0)
    cost_credits: Mapped[float] = mapped_column(Float, default=0.0)
    # §auto_dev: the repo issue this request was auto-created from (sweep dedup key
    # + where the PR link is commented back). Null for chat/API-born requests.
    source_issue_iid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_issue_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # PRs/MRs opened by this request's dev runs, oldest first (§PR chips):
    # [{number, url, provider}] - a re-run appends, dedup by url. Reassign, never
    # mutate in place (SQLAlchemy JSON change tracking).
    pr_urls: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # §repo binding: the connected repo this request's builds push into - the
    # INTENT, set at filing (form picker / chat URL inference / auto_dev issue
    # origin). Null = the project's default push target at dispatch time; SET
    # NULL on repo removal dissolves the binding back to the default.
    repo_id: Mapped[str | None] = mapped_column(
        ForeignKey("project_repo.id", ondelete="SET NULL"), nullable=True)
    # §work answers: what this request's last published run says it did (the
    # redacted .openvisor/pr.md), so "what did you do?" is answerable from the DB.
    work_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ChatImage(Base):
    """§chat images: an image pasted or imported into a chat thread.

    Stored in-DB like ProjectFile and QuoteAttachment (alpha scale) and, like the
    messages it belongs to, IMMUTABLE - there is no edit path, only creation and
    the project's own deletion. `message_id` is null between upload and the post
    that references it (an image the customer picked but never sent); a sweep can
    drop those, and nothing renders them.

    Only ever created when the project's model is known to read images
    (services/vision) - the API re-checks at upload time, so a UI that got the
    gate wrong still can't create one.
    """
    __tablename__ = "chat_image"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), index=True)
    message_id: Mapped[str | None] = mapped_column(ForeignKey("message.id"), nullable=True,
                                                   index=True)
    author: Mapped[str] = mapped_column(String(20))  # customer|admin
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(Integer)
    data: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class IssueWatchEvent(Base):
    """§auto_dev: one row per intake decision the issue sweep took, the paginated
    history behind the Issue-watch card. State changes only, never one row per
    sweep pass: `registered` (issue became a Request), `deferred` (daily cap hit,
    at most one per issue per UTC day), `paused` (low-credit pause, aligned with
    the 24h-throttled customer notice), `started` (a build was dispatched),
    `unpollable` (the watch cannot poll at all - no repo/token/supported
    provider - at most one row per 24h)."""
    __tablename__ = "issue_watch_event"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # registered|deferred|paused|started|unpollable
    issue_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    issue_title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    request_id: Mapped[str | None] = mapped_column(ForeignKey("request.id"), nullable=True)
    detail: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class CreditTransaction(Base):
    __tablename__ = "credit_transaction"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organization.id"), index=True)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("project.id"), nullable=True)
    amount: Mapped[float] = mapped_column(Float)  # positive=credit, negative=debit
    kind: Mapped[str] = mapped_column(String(20))  # topup|consumption|quote|refund|adjustment|signup|mcp_query|program_run|hub_grant
    # §usage graph: tokens behind a consumption row. project.tokens_consumed is a
    # running total with no history, so this is the only per-event token record -
    # null on non-model rows (topup, quote, grant...).
    tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Of `tokens`, the input tokens the provider served from its prompt cache
    # and the row was billed at the discounted cached rate (§18); null when the
    # usage report carried none.
    tokens_cached: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stripe_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    detail: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Indexed for the §usage graph's per-day window over the ledger. The index
    # exists in every database already (created by migration a3c4d5e6f7b8), so
    # declaring it here needs no migration - it stops autogenerate proposing to
    # DROP it on every future revision.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 index=True)


class DevRun(Base):
    """§parallel-builds (docs/PARALLEL_BUILDS.md §3): one ledger row per dev-run
    dispatch chain. MR1 keeps it DARK: the Project.dev_* scalars stay the source
    of truth and _save_run shadow-mirrors every state change onto the project's
    active row, so the ledger tracks reality before anything reads it for
    behavior. dev_concurrency.acquire_slot is the SOLE creator of rows;
    run_development(run_id=None) create-or-adopts for messages queued across a
    deploy. request_id null = legacy pre-threads run narrating to main."""
    __tablename__ = "dev_run"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), index=True)
    request_id: Mapped[str | None] = mapped_column(ForeignKey("request.id"), nullable=True)
    # queued (slot held, worker not yet started) | running | awaiting_merge |
    # deploying | merged (change landed, demo deploy parked - the next demo
    # start finalizes it) | failed | done | idle (plan-pass terminal) |
    # superseded (§revise: its open PR was handed to the revision continuing
    # its branch)
    state: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # '' = legacy shared workspace (Project.workspace_path); parallel-mode runs
    # (MR3) get 'devruns/<project_id>/<run_id>' OUTSIDE the project checkout.
    workspace_dir: Mapped[str] = mapped_column(String(255), default="")
    branch: Mapped[str | None] = mapped_column(String(80), nullable=True)
    pr_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pr_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    run_log: Mapped[str | None] = mapped_column(Text, nullable=True)
    run_error: Mapped[str | None] = mapped_column(String(512), nullable=True)
    harness_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    security_review: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    acceptance: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    tokens_consumed: Mapped[int] = mapped_column(Integer, default=0)
    cost_credits: Mapped[float] = mapped_column(Float, default=0.0)
    # §parallel-builds MR2 exactly-once billing watermark: cumulative tokens
    # already billed for this run (usage.json is cumulative per run).
    billed_through: Mapped[int] = mapped_column(Integer, default=0)
    # Run chains: a retry/boot-fix resume inherits the predecessor's
    # workspace_dir and branch (new row, same dir).
    predecessor_id: Mapped[str | None] = mapped_column(ForeignKey("dev_run.id"), nullable=True)
    # §repo binding: the connected repo THIS run chain builds into - the
    # EXECUTION pin stamped by acquire_slot (chain -> request intent -> project
    # default) and honored by _dev_target for the whole pipeline (dispatch,
    # publish, merge sweep, branch links). Never re-resolved mid-chain, so a
    # push-target switch can't retarget history. Null = platform repo or a
    # pre-binding row (target recovered from the chain's PR URL, else live).
    repo_id: Mapped[str | None] = mapped_column(
        ForeignKey("project_repo.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    # DB backstop under the acquire_slot lock: one active run per request, ever
    # (NULL request_id rows - legacy runs - are unconstrained by design).
    __table_args__ = (
        Index("uq_devrun_active_request", "request_id", unique=True,
              postgresql_where=text("state IN ('queued', 'running', 'awaiting_merge', 'deploying')")),
    )


class DevRunRecord(Base):
    """One agent-eval record per dev-run outcome (§Phase 0). Mirrors
    agent_eval.metrics.RunRecord field-for-field so report.aggregate can measure
    production builds by harness_version. Written best-effort at each run's
    terminal state (the capture must never break a build); fields not yet emitted
    by the pipeline (contract_ok, ci_status) stay null until Phase 1 fills them."""
    __tablename__ = "dev_run_record"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("project.id"), nullable=True, index=True)
    spec_id: Mapped[str] = mapped_column(String(64), index=True)  # project id, or a corpus spec id
    speciality: Mapped[str | None] = mapped_column(String(64), nullable=True)
    harness_version: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1)  # 1-based; >1 = a Resume (pass@1 vs pass@k)
    final_state: Mapped[str] = mapped_column(String(16))  # dev_run_state at run end
    boot_result: Mapped[bool | None] = mapped_column(Boolean, nullable=True)  # None = gate unavailable/off
    contract_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    ci_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    security_blocking: Mapped[int] = mapped_column(Integer, default=0)
    security_ran: Mapped[bool] = mapped_column(Boolean, default=True)
    leak_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    leak_scanner_errored: Mapped[bool] = mapped_column(Boolean, default=False)
    # §Phase 1 #5: advisory spec-conformance checks against the booted demo.
    # null = not run (fallback / older run); else passed-of-total.
    acceptance_passed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    acceptance_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    credits: Mapped[float] = mapped_column(Float, default=0.0)
    wall_clock_s: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str | None] = mapped_column(String(512), nullable=True)
    source: Mapped[str] = mapped_column(String(16), default="live")  # live | corpus
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class HubProjectEvent(Base):
    """Transactional outbox (§hub pass-through): one row per status/message/
    evaluation/demo event on a source='hub' project, written in the SAME db
    transaction as the state it mirrors (never at the Redis publish layer, so a
    rollback can't ghost an event and a crash can't lose one). A Beat job pushes
    unsent rows to the hub claim-based (sent_at IS NULL + FOR UPDATE SKIP
    LOCKED), at-least-once; the hub dedups on this row's id."""
    __tablename__ = "hub_project_event"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), index=True)
    hub_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    etype: Mapped[str] = mapped_column(String(32))  # status|message|evaluation|demo
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 index=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True,
                                                     index=True)


class HubCreditGrant(Base):
    """Idempotency ledger for credits pushed in by a central Scalevisor Hub
    (PROMPT hub link). One row per hub grant, keyed by the hub-supplied
    idempotency_key: a replayed key returns the original grant and writes no new
    CreditTransaction, so an at-least-once hub never double-credits a wallet."""
    __tablename__ = "hub_credit_grant"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("organization.id"), index=True)
    amount: Mapped[float] = mapped_column(Float)
    detail: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Quote(Base):
    """Two payment paths share this table: EUR quotes settled via a Stripe
    payment link (amount + stripe_payment_link) and credit quotes settled from
    the org wallet (price_credits, customer accepts/denies in the Quotes tab).
    An accepted credit quote commits the consultant to deliver it to the customer repo."""
    __tablename__ = "quote"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), index=True)
    request_id: Mapped[str | None] = mapped_column(ForeignKey("request.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(255), default="")
    details: Mapped[str] = mapped_column(Text, default="")  # admin-editable at all times
    amount: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(8), default="EUR")
    price_credits: Mapped[float | None] = mapped_column(Float, nullable=True)
    stripe_payment_link: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # canceled = withdrawn by the admin (who won't do the work), with an
    # optional partial/total refund when the quote had been accepted.
    status: Mapped[str] = mapped_column(String(16), default="draft")  # draft|sent|paid|accepted|denied|canceled
    decision_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    refunded_credits: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    attachments: Mapped[list["QuoteAttachment"]] = relationship(
        back_populates="quote", cascade="all, delete-orphan")


class QuoteAttachment(Base):
    """Admin-uploaded quote document (image, PDF, ...), downloadable by the
    customer. Stored in-DB to keep platform state in one place (alpha scale)."""
    __tablename__ = "quote_attachment"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    quote_id: Mapped[str] = mapped_column(ForeignKey("quote.id"), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(128), default="application/octet-stream")
    size_bytes: Mapped[int] = mapped_column(Integer)
    data: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    quote: Mapped["Quote"] = relationship(back_populates="attachments")


class DeploymentEvent(Base):
    __tablename__ = "deployment_event"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), index=True)
    action: Mapped[str] = mapped_column(String(20))  # start|stop|timeout|redeploy
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    detail: Mapped[str | None] = mapped_column(String(512), nullable=True)


class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunk"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    source: Mapped[str] = mapped_column(String(64), index=True)  # kb|cve
    path: Mapped[str] = mapped_column(String(512))
    content: Mapped[str] = mapped_column(Text)
    embedding = mapped_column(Vector(1024), nullable=True)
    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class Tool(Base):
    """An instance-admin-level MCP tool the dev agent can call during builds
    (§Tools) - distinct from knowledge bases: a tool ACTS (GitHub/GitLab PR,
    issue and review operations), a KB informs. Global rows seeded per kind
    (github, gitlab - disabled until configured); per-project overrides live in
    ProjectToolConfig. The GitLab row's `url` points at the instance's MCP
    endpoint (https://<host>/api/v4/mcp), so self-hosted instances are plain
    configuration. `api_key_enc` is envelope-encrypted and never leaves the
    server (the API exposes has_api_key only); `tools_fingerprint` supports the
    §KB rug-pull detection across builds."""
    __tablename__ = "tool"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    slug: Mapped[str] = mapped_column(String(40), unique=True)
    name: Mapped[str] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(16))  # github | gitlab | custom
    url: Mapped[str] = mapped_column(String(512))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    params: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    api_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    tools_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ProjectToolConfig(Base):
    """Per-project override of a Tool (§Tools): tri-state enable (null =
    inherit the global flag), an optional URL override (a customer's own
    self-hosted GitLab instance) and an optional key override. Falls back to
    the project's Memory secret (GITHUB_TOKEN/GITLAB_TOKEN) before the global
    tool key, so per-customer credentials need no re-entry."""
    __tablename__ = "project_tool_config"
    __table_args__ = (UniqueConstraint("project_id", "tool_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(String(36), ForeignKey("project.id"))
    tool_id: Mapped[str] = mapped_column(String(36), ForeignKey("tool.id"))
    enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    api_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class KnowledgeBase(Base):
    """An instance-admin-level knowledge source the dev agent can consult (§KB).
    Global (spoke-wide, the owner's - not per customer org, no org_id). Five kinds:
    `local` (the /knowledge Meilisearch KB), `context7` (the repo's Context7 MCP),
    `mcp` (a generic MCP endpoint the admin adds by URI + optional API key),
    `websearch` (a seeded web-search provider - `uri` holds the provider slug, e.g.
    `serper` - served to dev runs by the websearch-mcp sidecar with the row's key;
    seeded disabled, non-removable, enable requires a verified key), and `git` (a
    git repo cloned by the worker and indexed into the local KB index alongside
    /knowledge). The `local` and `context7` rows are seeded once, non-removable,
    and only their `enabled` flag is editable; `mcp` and `git` rows are user-added
    and fully editable/removable. At most one `local` and one `context7` row, and
    one `websearch` row per provider, is enforced by the seed, the API, and the
    partial-unique indexes below. `git` rows are unconstrained (many allowed).

    Git-source columns (all nullable so local/context7/mcp rows are unaffected):
    `auth_kind` ssh|http, `ref` the branch, `ssh_public_key` (the platform-generated
    deploy public key, shown to the admin to install as a read-only deploy key),
    `ssh_private_key_enc` (its private half, envelope-encrypted). For an HTTP source
    the PAT reuses `api_key_enc`. `verified` gates enabling: the connection check
    (git ls-remote) must pass first, re-run server-side on every enable (never trust
    the client). `last_indexed_at` / `last_index_error` carry per-source ingest
    status. MR2 wires the `local` enable flag into retrieval (services/rag); MR3
    wires the MCP kinds into dev runs; git sources feed the multi-root reindex."""
    __tablename__ = "knowledge_base"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    kind: Mapped[str] = mapped_column(String(16), index=True)  # local|context7|mcp|websearch|git
    name: Mapped[str] = mapped_column(String(255))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    uri: Mapped[str | None] = mapped_column(String(512), nullable=True)  # mcp URI / git repo URL
    api_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)  # mcp key / git HTTP PAT, envelope-encrypted
    is_removable: Mapped[bool] = mapped_column(Boolean, default=True)  # false for local+context7
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    # git-source fields (nullable; only populated for kind='git')
    auth_kind: Mapped[str | None] = mapped_column(String(8), nullable=True)  # ssh|http
    # HTTP Basic username paired with the token: default oauth2 (PAT); a GitLab
    # deploy token requires its generated gitlab+deploy-token-N, Bitbucket the real one.
    http_username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ref: Mapped[str | None] = mapped_column(String(128), nullable=True, default="main")  # branch
    ssh_public_key: Mapped[str | None] = mapped_column(Text, nullable=True)  # deploy public key (safe to show)
    ssh_private_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)  # envelope-encrypted
    verified: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    last_indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_index_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # Belt-and-braces against a double seed racing across containers: at most one
    # singleton row per built-in kind (mcp rows are unconstrained).
    __table_args__ = (
        Index("uq_kb_singleton_kind", "kind", unique=True,
              postgresql_where=text("kind IN ('local', 'context7')")),
        Index("uq_kb_websearch_provider", "uri", unique=True,
              postgresql_where=text("kind = 'websearch'")),
    )


class KbBlockClass(Base):
    """Classification of one KB content block (§KB tiers): fact informs retrieval,
    rule joins the standing-rules digest injected into every dev run in scope,
    procedure is a task-shaped workflow (trigger-loaded). Keyed by the blake2b hash
    of the block's whitespace-normalized text, so it doubles as the ingest-time
    classification CACHE (unchanged blocks never re-hit the classifier LLM) and as
    the admin override store: origin='override' rows are written from the admin KB
    page and are never overwritten by ingest - an override therefore survives
    reindexes until the block's content (and so its hash) actually changes."""
    __tablename__ = "kb_block_class"
    content_hash: Mapped[str] = mapped_column(String(32), primary_key=True)
    content_class: Mapped[str] = mapped_column(String(16))  # fact | rule | procedure
    origin: Mapped[str] = mapped_column(String(16))  # signal | llm | fallback | override
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 onupdate=utcnow)


class KbRulesDigest(Base):
    """The compiled standing-rules digest of one KB root (§KB tiers): every
    rule-class block of that source concatenated at ingest time, bounded by
    settings.kb_rules_digest_max_chars (overflow blocks are demoted to the
    procedure tier rather than silently truncated). root_key matches the Meili doc
    namespace ('local' or a git KnowledgeBase id) so per-project kb_ids selection
    narrows digest injection exactly like retrieval. Rows are replaced as a set on
    every successful ingest, mirroring the atomic index swap."""
    __tablename__ = "kb_rules_digest"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    root_key: Mapped[str] = mapped_column(String(36), unique=True)
    content: Mapped[str] = mapped_column(Text)
    char_count: Mapped[int] = mapped_column(Integer, default=0)
    compiled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class KbProcedure(Base):
    """One procedure-class KB block (§KB tiers): a task-shaped workflow whose FULL
    body loads into a dev run only when the task matches (selection is hybrid
    retrieval over the procedure-class docs; the chunk hits map back here through
    `block_hash`, so the agent always sees the whole procedure, never a chunk
    window). Includes rule blocks demoted past the digest budget. Rows are replaced
    as a set on every successful ingest, like kb_rules_digest."""
    __tablename__ = "kb_procedure"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    root_key: Mapped[str] = mapped_column(String(36), index=True)
    rel: Mapped[str] = mapped_column(String(512))
    title: Mapped[str] = mapped_column(String(255))
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(32), index=True)
    compiled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ApiToken(Base):
    __tablename__ = "api_token"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("user.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    # "user" tokens authenticate a customer to the MCP/knowledge surface (bill
    # their org). "hub" tokens authenticate a central Scalevisor Hub to the
    # /api/hub control surface (read usage, grant credits); they can never bill
    # knowledge queries. server_default backfills every pre-existing row to user.
    # "project" tokens are §MCP project tokens: minted from ONE project's MCP tab,
    # they see only that project and bill their queries to it. They still carry
    # the minting user (the MCP sidecar's auth query inner-joins "user", so a
    # user-less token would silently 401).
    scope: Mapped[str] = mapped_column(String(16), default="user", server_default="user")
    project_id: Mapped[str | None] = mapped_column(ForeignKey("project.id"), nullable=True,
                                                   index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class StatusChange(Base):
    __tablename__ = "status_change"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), index=True)
    from_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_status: Mapped[str] = mapped_column(String(32))
    actor: Mapped[str] = mapped_column(String(20))  # customer|admin|agent|system
    reason: Mapped[str | None] = mapped_column(String(512), nullable=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AppSetting(Base):
    """Global, admin-editable key/value settings (runtime, not env). One row per key.

    The only runtime-mutable global config in the app - `core/config.py` is env-only
    and cached. Values are JSON so a key can hold a bool, string, or small object.
    """
    __tablename__ = "app_setting"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Program(Base):
    """Admin-defined runnable program (PROMPT §28): an admin-managed repo in the
    platform GitLab, executed as `docker compose build && docker compose run
    program` inside a throwaway DinD sandbox. Customers instantiate published
    programs from the catalog; the repo itself is never exposed to them."""
    __tablename__ = "program"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    title: Mapped[str] = mapped_column(String(255), index=True)
    short_description: Mapped[str] = mapped_column(String(512), default="")
    readme_md: Mapped[str] = mapped_column(Text, default="")  # cached repo README.md
    # "group/name" in the platform GitLab; immutable after creation (the repo IS
    # the program - swapping it would silently change what instances run).
    gitlab_repo_path: Mapped[str] = mapped_column(String(255), unique=True)
    gitlab_project_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gitlab_web_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    default_branch: Mapped[str] = mapped_column(String(128), default="main")
    is_published: Mapped[bool] = mapped_column(Boolean, default=False)
    schedulable: Mapped[bool] = mapped_column(Boolean, default=False)
    # Per-program model config: a saved ModelEndpoint (the admin picker; null =
    # global settings). The inline openai_* trio below is the LEGACY fallback for
    # programs configured before saved endpoints - resolved only while
    # model_endpoint_id is null, and cleared whenever an endpoint (or the global
    # default) is picked so it can never shadow the choice.
    model_endpoint_id: Mapped[str | None] = mapped_column(
        ForeignKey("model_endpoint.id", ondelete="SET NULL"), nullable=True)
    openai_base_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    openai_api_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    credit_markup: Mapped[float | None] = mapped_column(Float, nullable=True)  # None → settings.credit_markup
    timeout_minutes: Mapped[int] = mapped_column(Integer, default=15)
    # Sandbox resources. Limits cap the DinD in both orchestrators; requests are
    # honored on Kubernetes (docker only supports a soft memory reservation).
    # Deliberately independent from DEMO_CPU_LIMIT/DEMO_MEM_LIMIT (demos only).
    cpu_request: Mapped[str] = mapped_column(String(16), default="0.5")
    cpu_limit: Mapped[str] = mapped_column(String(16), default="1")
    mem_request: Mapped[str] = mapped_column(String(16), default="256m")
    mem_limit: Mapped[str] = mapped_column(String(16), default="1g")
    input_template: Mapped[list | None] = mapped_column(JSON, nullable=True)  # parsed input.template.yml cache
    last_check_state: Mapped[str | None] = mapped_column(String(16), nullable=True)  # passed|failed
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_check_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ProgramInstance(Base):
    """A customer org's configured copy of a Program (several per org allowed).
    Holds the filled inputs, webhook, schedule and the per-instance SSH keypair
    whose private key is mounted into the program container as a compose secret."""
    __tablename__ = "program_instance"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    program_id: Mapped[str] = mapped_column(ForeignKey("program.id"), index=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("organization.id"), index=True)
    label: Mapped[str] = mapped_column(String(255), default="")
    # §28 per-instance model: which of the instance-admin's saved ModelEndpoints
    # THIS copy runs on. Null = inherit the program's admin-set endpoint (the
    # default, and what every pre-existing instance keeps). Precedence lives in
    # services/programs.resolve_model_config.
    model_endpoint_id: Mapped[str | None] = mapped_column(
        ForeignKey("model_endpoint.id", ondelete="SET NULL"), nullable=True)
    inputs_enc: Mapped[str | None] = mapped_column(Text, nullable=True)  # envelope-encrypted JSON {name: value}
    webhook_url: Mapped[str] = mapped_column(String(512), default="")
    # §28 inbound trigger hooks: a signed GitHub/GitLab webhook can enqueue a run.
    # hook_secret_enc is server-generated per instance (envelope-encrypted);
    # hook_filters are AND-composed allowlists {actions, labels, assignees,
    # authors} where an empty list means "no constraint" (the secret is the auth).
    hook_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    hook_secret_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    hook_filters: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    schedule_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    schedule_cron: Mapped[str] = mapped_column(String(64), default="")
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    ssh_public_key: Mapped[str] = mapped_column(Text)
    ssh_private_key_enc: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ProgramRun(Base):
    """One execution of a program instance - or an admin "Check Program run"
    when instance_id is NULL (kind="check"). Runs are immutable history; the
    output dir and full log live on the workspaces volume, with a log tail and
    the output.txt text kept here for durability."""
    __tablename__ = "program_run"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    program_id: Mapped[str] = mapped_column(ForeignKey("program.id"), index=True)
    instance_id: Mapped[str | None] = mapped_column(ForeignKey("program_instance.id"), nullable=True, index=True)
    org_id: Mapped[str | None] = mapped_column(ForeignKey("organization.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(16), default="manual")  # manual|schedule|check|hook
    state: Mapped[str] = mapped_column(String(16), default="queued", index=True)  # queued|running|succeeded|failed|timeout|blocked
    exit_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    error: Mapped[str | None] = mapped_column(String(512), nullable=True)
    log_tail: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_text: Mapped[str | None] = mapped_column(Text, nullable=True)  # output/output.txt (capped)
    output_files: Mapped[list | None] = mapped_column(JSON, nullable=True)  # [{path, size}]
    tokens_input: Mapped[int] = mapped_column(Integer, default=0)
    tokens_output: Mapped[int] = mapped_column(Integer, default=0)
    cost_credits: Mapped[float] = mapped_column(Float, default=0.0)
    webhook_status: Mapped[str | None] = mapped_column(String(16), nullable=True)  # delivered|failed
    # §28 hooks: the normalized inbound event that triggered a kind='hook' run -
    # staged into the sandbox as input/event.json by the worker.
    hook_event: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
