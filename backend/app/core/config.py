"""Application settings. OCPA rule: secrets come from env with NO defaults -
missing required vars crash at startup."""
from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Core - required, no defaults
    database_url: str
    redis_url: str
    secret_key: str
    master_encryption_key: str
    admin_email: str
    admin_password: str
    app_base_url: str
    landing_base_url: str

    # Deployment
    deploy_env: str = "production"  # production | local
    deploy_base_ip: str = "127.0.0.1"
    deploy_domain: str = "openvisor.local"
    demo_port_range: str = "20000-30000"
    demo_cpu_limit: str = "1"
    demo_mem_limit: str = "2g"
    demo_timeout_minutes: int = 15

    # Models (OpenAI-compatible) - required
    openai_base_url: str
    openai_api_key: str
    openai_model: str
    embedding_base_url: str
    embedding_api_key: str
    embedding_model: str
    reranker_base_url: str
    reranker_api_key: str
    reranker_model: str
    # Meilisearch: the retrieval engine for the local /knowledge KB (hybrid BM25 +
    # vector). meili_url has a sane in-network default; meili_master_key is a secret
    # with NO default (OCPA rule) so a missing key crashes at startup.
    meili_url: str = "http://meilisearch:7700"
    meili_master_key: str
    # KB retrieval floor (anti-extraction, §KB hardening): drop any retrieved chunk
    # whose Meilisearch hybrid ranking score is below this. Top-k retrieval always
    # returns SOMETHING, so an off-topic corpus-sweep query - the core RAG knowledge-
    # extraction vector - otherwise walks the KB one irrelevant chunk at a time. With a
    # floor, an off-topic query returns nothing: search_knowledge answers "not in the
    # KB" and the dev agent's RAG block stays empty. Scores run 0..1; the literature
    # (arXiv:2602.09319) finds 0.5-0.7 drives extraction success toward zero at a recall
    # cost - tune per corpus. 0 disables the floor (keep every hit). A hit with no score
    # (older Meili, a test stub) is always kept, so the floor never blanks a result set.
    kb_retrieval_min_score: float = 0.5
    # §KB tiers: budget (in characters) of one KB root's standing-rules digest - the
    # rule-class blocks injected verbatim into every dev run whose project selects
    # that source. Rule blocks past the budget are demoted to the procedure tier at
    # ingest (still indexed for retrieval, never silently dropped). Has a default so
    # existing deployments need no env change.
    kb_rules_digest_max_chars: int = 12_000
    # §KB tiers: how many matching procedures load (full body) into a dev run.
    kb_procedures_k: int = 3
    # §repo binding part B: LLM fallback inferring WHICH connected repo a
    # request targets when URL/name matching finds nothing. Deterministic
    # passes always run; 0 disables only the LLM fallback.
    request_repo_infer_enabled: bool = True
    # Retries on a transient chat-completions failure (429 rate limit / 5xx /
    # transport error) with exponential backoff. 0 disables. Optional: the
    # default keeps every deployment working without an env change.
    llm_max_retries: int = 4

    # Agent
    context7_mcp_url: str
    context7_api_key: str = ""
    openhands_url: str = ""  # optional: external OpenHands runtime endpoint
    openhands_enabled: bool = False
    openhands_image: str = "docker.openhands.dev/openhands/openhands:1.8"
    openhands_agent_server_image: str = "ghcr.io/openhands/agent-server:1.26.0-python"
    # "host:ip" injected as --add-host into the runner so it can resolve the
    # self-hosted GitLab (e.g. over Tailscale) for clone/push. Empty = none.
    git_extra_host: str = ""
    # Max automatic build attempts when the MR's OCPA CI fails (fix → re-push →
    # re-run). After this, the project goes to awaiting_admin. 0 = no retries.
    ci_max_retries: int = 2
    # Fail-safe caps on a single sandboxed dev run (§14.5). A run that exceeds
    # the wall-clock timeout is force-killed and reported as a failure (logs kept,
    # customer can resume); the iteration cap bounds token consumption per run.
    dev_run_timeout_minutes: int = 20
    # Instance DEFAULT agent-iteration cap; Project.dev_max_iterations overrides
    # it per project (admin-set). Legacy env name DEV_MAX_ITERATIONS still read.
    dev_max_iterations_default: int = Field(
        40, validation_alias=AliasChoices("DEV_MAX_ITERATIONS_DEFAULT", "DEV_MAX_ITERATIONS"))
    # §14.x stale dev-run reaper: run_development is synchronous and Celery is
    # not acks_late, so a worker that dies mid-run leaves the project stranded in
    # an in-flight sub-state forever (Resume blocked, orphaned dev job burning
    # unmetered tokens). The reaper recovers a run still in-flight past the
    # deployer's kill deadline (dev_run_timeout_minutes) plus this grace - a run
    # this far past its own timeout can only be an orphan (the deployer would
    # have force-killed a live one), so the grace can't false-positive.
    dev_run_reap_grace_minutes: int = 10
    # §parallel-builds (docs/PARALLEL_BUILDS.md §2): instance default and hard
    # ceiling for the per-project concurrency entitlement (1 = serialized,
    # today's behavior); the wallet admission floor requires
    # balance >= (active_runs + 1) * dev_run_credit_floor at acquire (0 disables).
    dev_parallel_runs_default: int = 1
    dev_parallel_runs_max: int = 3
    dev_run_credit_floor: float = 0.0
    dev_run_dir_retention: int = 10
    # §14.5 boot gate: after a dev run pushed its branch, the demo stack is
    # test-booted in a throwaway sandbox and must answer HTTP before the PR is
    # opened (GitHub) / the MR merged (GitLab). A failed boot is fed back to the
    # agent at most dev_boot_fix_attempts times per run (0 = gate without
    # retrying); the gate itself can be switched off for fast local tests.
    dev_boot_check: bool = True
    dev_boot_fix_attempts: int = 1
    # §dev-docker: whether dev-run sandboxes get an INNER docker daemon (the
    # deployer reads the same env to grant the runtime - Sysbox in production,
    # privileged fallback locally - and the task file tells the agent which way
    # it is). Off by default: plain sandboxes cannot run dockerd.
    dev_sandbox_docker: bool = False
    # §working method plan gate: a fresh MVP build first runs a bounded PLAN-ONLY
    # sandbox pass (explore + plan, no edits/push), posts the plan in chat for the
    # customer's one-click approval, and only then dispatches the implement run
    # with the approved plan pinned in its task. Exploration is the dominant agent
    # cost (~48% of turns in the SWE-bench literature), so the plan pass is where
    # a misunderstanding is cheapest to catch. Scoped requests keep their own §12
    # confirm flow (no double confirmation); auto_dev and scaffold runs skip it.
    dev_plan_confirm: bool = True
    dev_plan_max_iterations: int = 25
    # §Phase 1 #5: generate spec-derived acceptance checks and run them against the
    # booted demo (advisory conformance signal, never gates). Off disables the LLM
    # generation call + execution entirely.
    acceptance_checks_enabled: bool = True
    # §12 chat-intent classifier: on a human message in the main thread (while
    # the project is in an actionable state) the agent detects a "the blocker is
    # fixed, continue" signal (resume the failed build) or a newly-described
    # feature/edit/bug (register a proposed Request, confirm before building).
    # 0 disables the classifier entirely (fast tests / kill switch).
    chat_classify_enabled: bool = True
    # §chat kind: the KB-grounded conversational responder on chat projects.
    # 0 disables answering entirely (fast tests / kill switch); the rate cap is
    # the per-org runaway/abuse backstop (fixed 10-minute window).
    chat_answer_enabled: bool = True
    chat_answer_rate_per_10min: int = 20
    # §work answers: the agent replying about its OWN work on a build project
    # (what a run did, where it stands, what it cost) - the `answer` chat intent
    # plus the guaranteed reply to an "@agent"/"@ai" mention. 0 disables answering
    # (the classifier falls back to the previous silence); the cap is the per-org
    # runaway/abuse backstop (fixed 10-minute window).
    work_answer_enabled: bool = True
    work_answer_rate_per_10min: int = 12
    # §MCP delegate: the only MCP tool that spends build-sized money, called by a
    # token living in someone's terminal - so a per-project daily ceiling on top
    # of the wallet floor and the §12 in-flight/slot gates.
    mcp_delegate_daily_max: int = 8
    # §project search: the LLM rerank behind the dashboard search box. Free to the
    # customer (no usage row is recorded), so this per-org cap is the only spend
    # control - past it the box keeps serving deterministic text matches instead.
    # 0 disables the rerank entirely (fast tests / kill switch).
    project_search_ai_enabled: bool = True
    project_search_rate_per_10min: int = 40
    # §sharing: per-user cap on share creation (fixed 1-hour window). Sharing
    # necessarily confirms whether an email is registered (the share appears or
    # it doesn't), so this cap is what keeps the endpoint useless as a bulk
    # email-enumeration oracle. Legitimate use is a handful of grants.
    share_rate_per_hour: int = 20
    # §spend floor: the three session-authed routes that dispatch a model call
    # before anything is paid for - project evaluation, filing a Request (its LLM
    # title) and the pre-creation Request estimate. Each is legitimate a handful
    # of times per project; without a cap one account can replay them forever, and
    # the ledger only records a debt nobody collects. Per-org, fixed 10-minute
    # window. The hub routes keep their own hub-keyed caps and are unaffected.
    evaluate_rate_per_10min: int = 10
    request_create_rate_per_10min: int = 20
    request_estimate_rate_per_10min: int = 20
    # How far an org's wallet may go negative before the platform stops spending
    # model tokens on it at all (`services.llm.spend_allowed`). The wallet gates on
    # the billable paths already refuse at <= 0; this is the backstop for the paths
    # that MUST run before payment, so an unpaid account cannot bill indefinitely.
    credit_debt_limit: float = 25.0

    # GitLab
    gitlab_url: str
    gitlab_token: str
    gitlab_group: str
    # §ssh remotes: the hostname the platform's OWN GitLab serves SSH on, when it
    # differs from gitlab_url's host (`git.example.com` vs `gitlab.example.com`).
    # Repos cloned over that name are still ours: recognising it makes them
    # detect as `gitlab` and routes their API calls to gitlab_url with the
    # platform token, instead of deriving an API base from the SSH host that may
    # not serve /api/v4 (or, on a tailnet, may not be routable at all). Optional:
    # empty means the two hostnames are the same and nothing changes.
    gitlab_ssh_host: str = ""
    # GitHub (customer-owned repos). When a project's push repo is a github.com
    # URL, development pushes the agent branch there over the project deploy key
    # and opens a PR with a token (deploy keys can't open PRs). The token is
    # resolved per project first (a GITHUB_TOKEN Memory secret), and this
    # platform-wide token is only the fallback. Empty here AND no project token =
    # PR creation is skipped, the branch push stands, and the customer opens/merges
    # the PR manually (merge detected over SSH, no token needed). A customer GitLab
    # push repo works the same way with a per-project GITLAB_TOKEN (no platform
    # fallback - settings.gitlab_token is for the platform host).
    github_token: str = ""
    # §14.7 AI security review + auto-merge (GitHub PR / customer-GitLab MR, opt-in
    # per push repo via ProjectRepo.auto_merge). When the push repo has a validated
    # token AND its auto_merge is on, the agent's PR/MR diff is security-reviewed; a
    # clean review auto-merges, a critical/high finding re-dispatches a scoped fix
    # run up to security_fix_attempts times before parking for customer review. 0
    # disables the review entirely (auto-merge then only checks the deterministic floor).
    security_review_enabled: bool = True
    security_fix_attempts: int = 10

    # §auto_dev: max issue-triggered Requests the sweep auto-creates per project per
    # UTC day - the wallet-drain backstop against a bulk-labeled issue flood.
    auto_dev_daily_max_starts: int = 10

    # §28 inbound trigger hooks: per-instance receiver rate cap (fixed window)
    # and the max hook runs allowed to WAIT in the queue (bursts beyond are
    # dropped with a 204 + log - an unbounded queue is a wallet-drain vector).
    program_hook_rate_per_minute: int = 30
    program_hook_max_pending: int = 5

    # Stripe
    stripe_secret_key: str
    stripe_webhook_secret: str
    # Stripe Tax computes VAT (or GST/HST, or a US state rate) from the
    # customer's billing address against the registrations configured in the
    # Stripe dashboard. On by default because the failure this guards is
    # silent: an account with no registration does not error, it sells at zero
    # tax and accrues a liability nobody sees until a filing. Since Stripe will
    # not make that noise, `stripe_svc.tax_registrations()` does.
    stripe_automatic_tax: bool = True
    # Stripe product tax code for the credits line. "General - Electronically
    # Supplied Services" suits prepaid platform credits; the operator's
    # accountant owns this value, and changing it changes what every future
    # invoice charges.
    stripe_tax_code: str = "txcd_10000000"

    # SMTP
    smtp_host: str
    smtp_port: int = 25
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str

    # Brand (white-label spoke identity; not secrets, so a deployment without
    # env changes runs under the neutral defaults below).
    # The landing site bakes these at BUILD time; the SPA reads them at runtime
    # via GET /api/settings.
    brand_name: str = "Openvisor"
    brand_color_primary: str = "#22d3ee"
    brand_color_secondary: str = "#7c3aed"
    consultant_name: str = "Consultant"
    # One-line description of the consulting practice, injected into the
    # moderation/chat/KB prompts via {{CONSULTANT_FOCUS}} (services/brand.py).
    consultant_focus: str = "software & infrastructure consulting"
    # Prefix stamped on newly minted API tokens (§MCP tokens) - purely cosmetic
    # branding on the plaintext: validation hashes the whole presented string, so
    # changing it never invalidates existing tokens.
    token_prefix: str = "ov_"

    # Hub (optional; empty = standalone spoke). When hub_mcp_url is set, the
    # spoke registers with and heartbeats to a central Scalevisor Hub over MCP;
    # hub_spoke_token authenticates those calls. hub_max_grant_credits caps a
    # single hub-initiated credit grant (fail-loud guard against a bad hub);
    # hub_max_daily_grant_credits caps the rolling-24h TOTAL granted (0 disables)
    # - the backstop against a stolen hub token minting credits in a loop of
    # unique idempotency keys.
    hub_mcp_url: str = ""
    hub_spoke_token: str = ""
    hub_max_grant_credits: float = 10000.0
    hub_max_daily_grant_credits: float = 0.0
    # What this instance offers the hub network: "development" executes
    # projects on its own agents, "project_management" fronts engagements that
    # orchestrate other spokes. Comma-separated; unknown values are dropped at
    # parse time and an empty result falls back to "development".
    instance_capabilities: str = "development"

    # Captcha (PROMPT §2): self-hosted Altcha proof-of-work on the public auth
    # forms. Off only for an environment that cannot run the browser widget (a
    # headless smoke test against a deployed stack); the test suite solves the
    # challenge for real instead of switching it off.
    altcha_enabled: bool = True
    # Difficulty. The browser hashes until it finds the server's number, so the
    # expected work is maxnumber/2 hashes - a second or two of one visitor's
    # CPU, and that same cost per attempt for a script, which is the point.
    # Raising it weakens nothing (a wrong or missing solution is rejected
    # either way), so the ceiling is patience on a slow phone. Read per
    # challenge, so ALTCHA_MAX_NUMBER takes effect on a restart.
    altcha_max_number: int = 3_000_000

    # Billing
    credit_currency: str = "EUR"
    credit_markup: float = 1.3
    # Credits charged when a customer pulls the consultant into an AI project
    # via the request-review button. Refundable by the admin.
    review_request_credits: float = 120.0
    # Welcome credits granted to every new account at signup (ledger kind
    # "signup"). Set to 0 to disable the grant.
    signup_credits: float = 5.0
    # One-time fee debited when a chat project is opened (ledger kind
    # "chat_upfront"); per-answer LLM usage is metered on top. 0 disables the fee.
    chat_upfront_credits: float = 10.0

    # Programs (§28): admin-defined runnable repos customers instantiate and run.
    # Retention = how many run artifact dirs (logs + output/) are kept per
    # instance; the schedule floor is the fastest cadence a customer cron may
    # request (protects the platform from per-minute schedules).
    program_run_retention: int = 20
    program_min_schedule_minutes: int = 15

    # §routines: minimum gap between two firings of one saved prompt. Higher than
    # the program floor on purpose - a routine starts a real dev run (agent time,
    # tokens, a PR), so a mistyped `* * * * *` is expensive rather than merely
    # noisy. The skip-while-open guard bounds concurrency; this bounds frequency.
    routine_min_schedule_minutes: int = 60

    # Internal
    deployer_url: str = "http://deployer:8500"
    browser_mcp_url: str = "http://browser-mcp:3000/mcp"
    # Base URL of the websearch-mcp sidecar (§KB websearch kind); the worker
    # appends /<provider>/mcp per enabled row. Default resolves in compose and
    # in-namespace K8s alike.
    websearch_mcp_url: str = "http://websearch-mcp:3000"
    workspaces_dir: str = "/workspaces"
    # Public Traefik HTTP port. 80 in production; a per-instance port in local
    # multi-instance dev (e.g. 8090). Demo URLs must carry it when it isn't 80,
    # or the customer's demo link resolves to the wrong port.
    traefik_http_port: int = 80

    @property
    def is_local(self) -> bool:
        return self.deploy_env == "local"

    @property
    def capabilities_list(self) -> list[str]:
        allowed = ("development", "project_management")
        picked = [c for c in (p.strip() for p in self.instance_capabilities.split(","))
                  if c in allowed]
        return picked or ["development"]

    @property
    def sync_database_url(self) -> str:
        return self.database_url.replace("+asyncpg", "+psycopg2")

    @property
    def demo_port_bounds(self) -> tuple[int, int]:
        lo, hi = self.demo_port_range.split("-")
        return int(lo), int(hi)

    @property
    def http_scheme(self) -> str:
        return "http" if self.is_local else "https"

    @property
    def public_port_suffix(self) -> str:
        """":8090" style suffix for public URLs, empty for the standard ports."""
        return "" if self.traefik_http_port in (80, 443) else f":{self.traefik_http_port}"

    @property
    def consultant_first_name(self) -> str:
        return self.consultant_name.split()[0]

    @property
    def brand_slug(self) -> str:
        """Machine-safe short name (MCP server registration, identifiers in
        user-facing copy): first dot-label of the brand name, lowercased
        alphanumerics only. "acme.ai" -> "acme"."""
        label = self.brand_name.split(".")[0]
        slug = "".join(c for c in label.lower() if c.isalnum())
        return slug or "spoke"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
