// Shapes mirror docs/API_CONTRACT.md.

export type Role = "customer" | "admin";
export type AccountType = "individual" | "organization";

export interface User {
  id: string;
  email: string;
  full_name: string | null;
  role: Role;
  email_verified: boolean;
  created_at: string;
}

export interface Org {
  id: string;
  name: string;
  type: AccountType;
  company_name: string | null;
  vat_id: string | null;
  address_line1: string | null;
  address_line2: string | null;
  postal_code: string | null;
  city: string | null;
  country: string | null;
  credit_balance: number;
}

export interface Me {
  user: User;
  org: Org;
}

export type ProjectStatus =
  | "draft"
  | "awaiting_review"
  | "payment_due"
  | "development"
  | "awaiting_customer"
  | "awaiting_admin"
  | "finished"
  | "canceled";

export type DemoState = "stopped" | "running";
export type Tier = "mvp" | "production";
export type ProjectKind = "ai" | "direct_quote" | "auto_dev" | "chat";

// §sharing: the caller's access to a project. "owner" = the owning org (or an
// admin); shared users are "contributor" (acts as the customer) or "viewer"
// (read-only - the API 403s every mutation).
export type ProjectAccess = "owner" | "contributor" | "viewer";

export interface ProjectSummary {
  id: string;
  name: string;
  kind: ProjectKind;
  speciality: string | null;
  status: ProjectStatus;
  tier: Tier | null;
  demo_state: DemoState;
  // Lets the dashboard animate the status chip while the agent is working.
  dev_run_state: DevRunState;
  access: ProjectAccess;
  created_at: string;
}

// §sharing: one grant on a project, as managed in the Share modal.
export interface ShareEntry {
  id: string;
  user_id: string;
  email: string;
  full_name: string | null;
  role: "contributor" | "viewer";
  created_at: string;
}

// §project search: `ai` is false whenever the deterministic text ranking is what
// came back (rerank disabled, org rate-capped, or the model unavailable), with
// `reason` naming which - the box works either way.
export interface ProjectSearchResult {
  results: ProjectSummary[];
  ai: boolean;
  reason: "disabled" | "rate_limited" | "unavailable" | null;
}

export type RepoProvider = "github" | "gitlab" | "other";

export interface ProjectRepo {
  id: string;
  ssh_uri: string;
  role: string;
  provider: RepoProvider;
  is_push_target: boolean;
  auto_merge: boolean;
  squash_on_merge: boolean;
  summarize_to_issue: boolean;
  can_auto_merge: boolean;
}

export interface PlatformRepo {
  web_url: string | null;
  ssh_url: string | null;
  provisioned: boolean;
  is_push_target: boolean;
}

export interface Project extends ProjectSummary {
  description: string;
  from_scratch: boolean;
  sovereign: boolean;
  sovereign_comment?: string | null;
  block_auto_development: boolean;
  dev_max_iterations?: number | null;
  // §dev-pod resources: per-project dev-run pod scheduling requests (docker-style
  // 0.5 / 512m / 4g); null = the deployer's instance defaults.
  dev_cpu_request?: string | null;
  dev_mem_request?: string | null;
  // Per-project KB selection: null = all enabled KBs, [] = none, list = those ids.
  kb_ids: string[] | null;
  // auto_dev only: the sentinel's issue-watch filters (null for other kinds).
  issue_watch: { labels: string[]; assignees: string[]; authors: string[] } | null;
  // §git identity: the per-project override (null = inheriting the instance
  // default) and what the agent's commits will actually be authored as.
  git_author_name: string | null;
  git_author_email: string | null;
  git_author_name_effective: string;
  git_author_email_effective: string;
  gitlab_url: string | null;
  subdomain: string;
  demo_url: string | null;
  demo_basic_auth_user: string | null;
  demo_basic_auth_pass: string | null;
  demo_last_started_at: string | null;
  demo_last_stopped_at: string | null;
  ssh_public_key: string;
  repos: ProjectRepo[];
  platform_repo: PlatformRepo | null;
  image_support: ImageSupport | null;
  tokens_consumed: number;
  cost_credits: number;
  quick_devs_enabled: boolean;
  // §threads: the request the in-flight/last run is scoped to (null = MVP build).
  dev_request_id: string | null;
  // §parallel-builds: raw per-project override + the resolved effective limit.
  dev_parallel_limit: number | null;
  dev_parallel_effective: number;
  // §build panel branch chip: the run's branch + its push-repo web URL.
  dev_branch: string | null;
  dev_branch_url: string | null;
  dev_run_error: string | null;
  dev_run_started_at: string | null;
  dev_pr_number: number | null;
  dev_pr_url: string | null;
  dev_can_resume: boolean;
  dev_resume_blocker: string | null;
  dev_security_review: SecurityReview | null;
  // §parallel-builds MR4: the active DevRun rows behind the stacked consoles
  // (dev_run_out shape, oldest started first). Only the project DETAIL payload
  // carries it - [] on payloads built without it.
  dev_runs: DevRunSummary[];
}

export interface SecurityFinding {
  severity: "critical" | "high" | "medium" | "low";
  issue: string;
  file?: string | null;
  line?: number | null;
}

export interface SecurityReview {
  verdict: string;
  findings?: SecurityFinding[];
  floor?: string[];
  attempts?: number;
  merged?: boolean;
  reviewed_at?: string;
  error?: string;
}

export type DevRunState =
  | "idle"
  | "running"
  | "awaiting_merge"
  | "deploying"
  | "merged" // change landed, demo deploy parked - demo restart finalizes it
  | "failed"
  | "done";

export interface DevLogs {
  dev_run_state: DevRunState;
  dev_run_error: string | null;
  dev_run_started_at: string | null;
  dev_pr_number: number | null;
  dev_pr_url: string | null;
  log: string;
}

// One sanitized agent-activity line from the live build feed (§14.8).
export interface DevEvent {
  ts: number | null;
  kind: string;
  title: string;
  detail?: string | null;
}

export interface DevActivityUsage {
  input_tokens: number;
  output_tokens: number;
  // Prompt-cache reads (subset of input_tokens) - already discounted in the estimate.
  cached_input_tokens: number;
  total_tokens: number;
  credits_estimate: number | null;
  // The api_model this run executes on - can change between runs.
  model: string | null;
}

// Offset-polled chunk from GET /projects/{id}/dev-activity (RunLogChunk parity).
export interface DevActivityChunk {
  state: DevRunState;
  live: boolean;
  started_at: string | null;
  events: DevEvent[];
  next_offset: number;
  reset: boolean;
  usage: DevActivityUsage | null;
}

// One DevRun ledger row from GET /projects/{id}/dev-runs - the request
// thread's development history. States beyond DevRunState are the ledger's
// own (queued, superseded).
export interface DevRunSummary {
  id: string;
  request_id: string | null;
  state: DevRunState | "queued" | "superseded";
  created_at: string;
  started_at: string | null;
  branch: string | null;
  branch_url: string | null;
  pr_number: number | null;
  pr_url: string | null;
  run_error: string | null;
  security_review: SecurityReview | null;
  tokens_consumed: number;
  cost_credits: number;
  // Whether dev-activity?run_id= can still serve this run's feed.
  has_feed: boolean;
}

// Admin overview rows carry org context.
export interface AdminProjectSummary extends ProjectSummary {
  org_name: string;
  org_id: string;
}

// Admin-only: what the customer has spent on a project so far.
export interface ProjectSpend {
  credits_spent: number;
  by_kind: Record<string, number>;
  quotes_paid: number;
  total_spent: number;
}

export interface StatusChange {
  from: string;
  to: string;
  actor: string;
  reason: string | null;
  at: string;
}

// §auto_dev: one intake decision the issue sweep took (the Issue-watch card's history).
export interface IssueWatchEvent {
  id: string;
  kind: "registered" | "deferred" | "paused" | "started" | "unpollable" | "comment_failed";
  issue_url: string | null;
  issue_title: string | null;
  request_id: string | null;
  detail: string | null;
  created_at: string;
}

export interface IssueWatchEventPage {
  total: number;
  events: IssueWatchEvent[];
}

export type MessageAuthor = "customer" | "admin" | "agent" | "system";

export interface Message {
  id: string;
  thread: string;
  author: MessageAuthor;
  body: string;
  meta?: import("@shared-ui").MessageQuestionMeta | Record<string, unknown> | null;
  emailed: boolean;
  created_at: string;
}

// Creatable via the form/API; the server additionally mints "mvp" (Request #0).
export type RequestType = "feature" | "edit" | "bug" | "production_deploy";
export type ServerRequestType = RequestType | "mvp";
export type RequestHandling = "ai" | "manual";
export type RequestStatus =
  | "proposed"
  | "open"
  | "quoted"
  | "in_progress"
  | "done"
  | "rejected";

export interface ProjectRequest {
  id: string;
  project_id: string;
  type: ServerRequestType;
  handling: RequestHandling;
  status: RequestStatus;
  title: string;
  price_credits: number | null;
  // §repo binding: the connected repo this request's builds push into
  // (null = the project's default push target).
  repo_id: string | null;
  tokens_consumed: number;
  cost_credits: number;
  source_issue_url: string | null;
  // §PR chips: PRs/MRs opened by this request's dev runs, oldest first.
  pr_urls: { number: number | string; url: string; provider?: "github" | "gitlab" }[] | null;
  created_at: string;
}

export interface MemoryEntry {
  id: string;
  key: string;
  is_secret: boolean;
  author: string;
  description: string;
  updated_at: string;
  value: string;
}

// Customer-imported project file (Memory & files tab), staged into every dev run.
export interface ProjectFileInfo {
  id: string;
  filename: string;
  content_type: string;
  size_bytes: number;
  author: string;
  updated_at: string;
}

export interface MemoryPlaceholder {
  key: string;
  category: string;
  is_secret: boolean;
  description: string;
}

// Per-project global-memory setting. `use_global_memory` is the raw override
// (null = inherit the org default); `effective` is what actually applies.
export interface MemorySettings {
  use_global_memory: boolean | null;
  org_default: boolean;
  effective: boolean;
}

export interface OrgMemorySettings {
  enabled_default: boolean;
}

export interface Transaction {
  id: string;
  project_id: string | null;
  amount: number;
  kind: string;
  created_at: string;
}

// Informational cost/time estimate for a request being drafted - never a quote.
export interface RequestEstimate {
  available: boolean;
  cost_credits?: number;
  time_hours?: number;
  explanation?: string;
  reason?: string;
  based_on?: { runs: number; model: string };
}

export interface QuoteAttachment {
  id: string;
  filename: string;
  content_type: string;
  size_bytes: number;
}

// status: draft|sent|paid for Stripe payment-link quotes;
// sent|accepted|denied|canceled for credit quotes (price_credits set) -
// canceled = withdrawn by the admin, with an optional refund if it was paid.
export interface Quote {
  id: string;
  project_id: string;
  request_id: string | null;
  title: string;
  details: string;
  amount: number;
  currency: string;
  price_credits: number | null;
  status: string;
  payment_link: string | null;
  decision_comment: string | null;
  decided_at: string | null;
  refunded_credits: number | null;
  created_at: string;
  attachments: QuoteAttachment[];
}

export interface ApiToken {
  id: string;
  name: string;
  scope: string;
  created_at: string;
  last_used_at: string | null;
}

export interface NewApiToken extends ApiToken {
  token: string;
}

// The admin hub-token endpoint returns only these fields (no created_at yet).
export interface NewHubToken {
  id: string;
  name: string;
  scope: string;
  token: string;
}

export interface AdminUser {
  id: string;
  email: string;
  role: Role;
  org_id: string;
  org_name: string;
  credit_balance: number;
  email_verified: boolean;
  blocked: boolean;
  created_at: string;
}

export interface MetaConfig {
  deploy_env: string;
  deploy_domain: string;
  landing_base_url?: string;
  credit_currency: string;
  alpha: boolean;
  demo_timeout_minutes?: number;
  review_request_credits?: number;
  chat_upfront_credits?: number;
  // repo-path prefix shown in the admin program form (§28)
  gitlab_url?: string;
  // runtime, admin-editable pause switches for new project creation
  pause_ai_deposits: boolean;
  pause_direct_deposits: boolean;
  pause_auto_dev_deposits: boolean;
  pause_chat_deposits: boolean;
}

// public white-label settings (GET /api/settings): brand identity + activity
// catalog the SPA consumes at runtime, so a spoke rebrands with no SPA rebuild.
export interface PublicSettings {
  brand_name: string;
  brand_slug: string;
  brand_color_primary: string;
  brand_color_secondary: string;
  consultant_name: string;
  consultant_first_name: string;
  credit_currency: string;
  specialities: { id: string; label: string; description: string }[];
  // §routines: false hides the Routines tab. Advisory only - every routine
  // write re-checks the flag server-side.
  routines_enabled: boolean;
}

// §routines: a saved prompt on a project. An empty schedule_cron = fired by
// hand; last_request_status is what the skip-while-open guard reads.
export interface Routine {
  id: string;
  project_id: string;
  title: string;
  prompt: string;
  enabled: boolean;
  schedule_cron: string;
  next_run_at: string | null;
  last_run_at: string | null;
  last_request_id: string | null;
  last_request_status: string | null;
  last_skip_reason: string | null;
  repo_id: string | null;
  created_at: string;
}

// admin-editable global settings (GET/PUT /api/admin/settings)
export interface AdminSettings {
  pause_ai_deposits: boolean;
  pause_direct_deposits: boolean;
  pause_auto_dev_deposits: boolean;
  pause_chat_deposits: boolean;
  // §routines: instance kill switch (the feature is on unless this is true).
  routines_disabled: boolean;
  // §egress: dev-sandbox egress lockdown (enforced on Kubernetes deployments).
  egress_lockdown_enabled: boolean;
  egress_allowlist: string[];
  egress_enforced_on?: string; // "kubernetes" - informational
  // §fees: per-track engagement fees - rows in responses; the PUT accepts
  // speciality_fee_overrides ({id: credits}, null clears back to the default).
  speciality_fees?: SpecialityFeeRow[];
  speciality_fee_overrides?: Record<string, number | null>;
}

export interface SpecialityFeeRow {
  id: string;
  label: string;
  default_fee_credits: number;
  override_credits: number | null;
  effective_fee_credits: number;
}

// A knowledge base the admin manages (§KB). The api_key is never returned -
// only whether one is set (has_api_key).
export interface KnowledgeBase {
  id: string;
  kind: "local" | "context7" | "mcp" | "websearch" | "git";
  name: string;
  // §KB: what a dev run calls this source - the string to quote in project
  // instructions. null for retrieval-only kinds (local/git), which are read
  // into the task instead of being called; mcp_tools names the tools under the
  // server when the platform ships that server itself.
  mcp_server: string | null;
  mcp_tools: string[];
  enabled: boolean;
  uri: string | null;
  has_api_key: boolean;
  is_removable: boolean;
  sort_order: number;
  created_at: string;
  // git sources only
  auth_kind?: "ssh" | "http" | null;
  http_username?: string | null;
  ref?: string | null;
  ssh_public_key?: string | null;
  verified?: boolean;
  last_indexed_at?: string | null;
  last_index_error?: string | null;
}

// §KB tiers: one classified content block of a retrieval source, as served by
// GET /admin/knowledge-bases/{id}/tiers. origin: 'override' = admin pin,
// 'llm' = cached classifier verdict, 'auto' = path/frontmatter signal or the
// per-run fallback.
export interface KbTierBlock {
  block_hash: string;
  rel: string;
  content_class: "fact" | "rule" | "procedure";
  origin: "override" | "llm" | "auto";
  excerpt: string;
  chunks: number;
}

export interface KbTiers {
  digest: { content: string; char_count: number; compiled_at: string } | null;
  counts: { fact: number; rule: number; procedure: number };
  blocks: KbTierBlock[];
  // blocks is one page of the (optionally class-filtered) listing: total is the
  // filtered block count, counts always describe the whole source.
  total: number;
  page: number;
  per_page: number;
}

// specialities.json entries (subset we render).
export interface Speciality {
  id: string;
  label: string;
  short_label: string;
  description: string;
  icon: string;
  enabled: boolean;
  sovereign_default: boolean;
  requires_existing_repo: boolean;
  deliverable_type: string;
  capabilities: string[];
  example_deliverables: string[];
  complexity_baseline: string;
  default_stack?: string[];
}

// initial-user-questions.json
export type QuestionType = "single" | "multi";

export interface QuestionOption {
  id: string;
  label: string;
}

export type ShowIf =
  | null
  | { speciality: string }
  | { answer: { question: string; any_of: string[] } };

export interface Question {
  id: string;
  prompt: string;
  type: QuestionType;
  required: boolean;
  allow_comment: boolean;
  show_if: ShowIf;
  options: QuestionOption[];
}

export interface QuestionsDoc {
  version: number;
  questions: Question[];
}

export interface Answer {
  question_id: string;
  option_ids: string[];
  comment?: string;
}

export type FeasibilityVerdict = "pass" | "needs_info" | "review_required" | "reject";

export interface Evaluation {
  state: "pending" | "done" | "failed";
  moderation?: { verdict?: string; reasons?: string[] } | null;
  feasibility?: { verdict: FeasibilityVerdict; reasons: string[] } | null;
  estimate?: {
    credits: number | null;
    tokens: number | null;
    cost_per_token: number | null;
    explanation: string;
  } | null;
}

// A saved, reusable LLM API endpoint + credential (admin-managed, instance-wide).
// The API key is never returned - only whether one is set.
export type ModelProvider = "openai" | "anthropic" | "mistral" | "custom";

export interface ModelEndpoint {
  id: string;
  label: string;
  provider: ModelProvider;
  base_url: string;
  model_name: string | null;
  // Custom per-1M-token price, set only for a model not in the price table.
  input_price: number | null;
  output_price: number | null;
  // Optional prompt-cache read rate; null = cached tokens bill at input_price.
  cached_input_price: number | null;
  // True when model_name is billable from the static price table (no custom price needed).
  model_priced: boolean;
  reasoning_effort: "low" | "medium" | "high" | null;
  // §chat images. TRI-STATE: null = never checked (attachments stay off, but the
  // tooltip differs from a hard false). `source` says whether the verdict came
  // from the Test probe or an admin declaration.
  supports_images: boolean | null;
  supports_images_source: "probe" | "admin" | null;
  has_api_key: boolean;
  created_at: string;
}

// One model pulled from the provider's live /models list; priced = billable from
// the static price table (no custom price needed).
export interface CatalogModel {
  id: string;
  priced: boolean;
}

export interface ModelCatalog {
  models: CatalogModel[];
  error: string | null;
}

// One preflight probe against an endpoint API surface.
export interface EndpointProbe {
  ok: boolean;
  status: number | null;
  error: string | null;
}

export interface EndpointTestResult {
  chat_completions: EndpointProbe;
  responses: EndpointProbe | null;
  effort: { value: string; accepted: boolean; detail: string } | null;
  // §chat images: null when the chat surface itself failed (nothing was probed);
  // `supported` null means the probe was inconclusive, so the stored verdict stands.
  vision: { supported: boolean | null; detail: string; source: string | null } | null;
}

// A project's model-config override: which saved endpoint (the model + credentials
// ride with it). endpoint_id null means "use the global default".
export interface ModelConfig {
  endpoint_id: string | null;
}

// ---- programs (§28) ----
export type ProgramRunState =
  | "queued"
  | "running"
  | "succeeded"
  | "failed"
  | "timeout"
  | "blocked";
export type ProgramRunKind = "manual" | "schedule" | "check" | "hook";
export type InputFieldType = "text" | "multiline" | "number" | "boolean" | "choice";

export interface InputTemplateField {
  name: string;
  label: string;
  description: string;
  type: InputFieldType;
  required: boolean;
  default: string | number | boolean | null;
  secret: boolean;
  placeholder: string;
  options: string[] | null;
}

export interface ProgramBrief {
  id: string;
  title: string;
  short_description: string;
  schedulable: boolean;
  is_published: boolean;
  created_at: string;
}

export interface Program extends ProgramBrief {
  readme_md: string;
  input_template: InputTemplateField[];
}

export interface AdminProgram extends Program {
  gitlab_repo_path: string;
  gitlab_web_url: string | null;
  default_branch: string;
  model_endpoint_id: string | null;
  has_legacy_model_config: boolean;
  credit_markup: number | null;
  credit_markup_effective: number;
  timeout_minutes: number;
  cpu_request: string;
  cpu_limit: string;
  mem_request: string;
  mem_limit: string;
  last_check_state: "passed" | "failed" | null;
  last_check_at: string | null;
  last_check_run_id: string | null;
  updated_at: string;
  instances_count?: number;
}

export interface ProgramRun {
  id: string;
  instance_id: string | null;
  kind: ProgramRunKind;
  state: ProgramRunState;
  exit_code: string | null;
  error: string | null;
  tokens_input: number;
  tokens_output: number;
  cost_credits: number;
  webhook_status: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface ProgramRunFull extends ProgramRun {
  output_text: string | null;
  output_files: { path: string; size: number }[];
  log_tail: string | null;
}

// §28 per-instance model: a saved model endpoint an instance can be pinned to.
// Deliberately narrow - the API never exposes the URL, the key or the prices.
export interface ProgramModelOption {
  id: string;
  label: string;
  model_name: string;
}

export interface ProgramInstance {
  id: string;
  program: Program;
  label: string;
  inputs: Record<string, string>;
  // null = run on the program's admin-set default model.
  model_endpoint_id: string | null;
  webhook_url: string;
  hook_enabled: boolean;
  hook_url: string;
  hook_secret: string | null;
  hook_filters: { actions: string[]; labels: string[]; assignees: string[]; authors: string[] };
  schedule_enabled: boolean;
  schedule_cron: string;
  next_run_at: string | null;
  ssh_public_key: string;
  created_at: string;
  latest_run: ProgramRun | null;
}

// §chat images: whether this project may attach images, and the tooltip to show
// on the disabled button when it may not.
// §chat images: an image attached to a chat message.
export interface ChatImage {
  id: string;
  filename: string;
  content_type: string;
  size_bytes: number;
}

export interface ImageSupport {
  enabled: boolean;
  reason: string | null;
  model: string;
}

export interface RunLogChunk {
  content: string;
  next_offset: number;
  done: boolean;
}

// §MCP project tokens: a token minted from one project's MCP tab. The secret
// itself is only ever present on the create response.
export interface McpToken {
  id: string;
  name: string;
  created_at: string;
  last_used_at: string | null;
}

// Counters only - what callers asked is never stored (see the MCP tab).
export interface McpUsage {
  queries_total: number;
  credits_total: number;
  queries_30d: number;
  credits_30d: number;
}

// §usage graph: one bucket per UTC day, zeros included so gaps read as gaps.
export interface UsageBucket {
  day: string;
  tokens: number;
  credits: number;
  mcp_tokens: number;
  requests_done: number;
  requests_canceled: number;
}

export interface ProjectUsage {
  days: number;
  series: UsageBucket[];
  totals: {
    tokens: number;
    credits: number;
    mcp_tokens: number;
    lifetime_tokens: number;
    lifetime_credits: number;
    requests_done: number;
    requests_canceled: number;
    lifetime_requests_done: number;
    lifetime_requests_canceled: number;
  };
}
