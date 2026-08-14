import { api } from "./api";
import type {
  AdminProgram,
  AdminProjectSummary,
  AdminSettings,
  KbTiers,
  KnowledgeBase,
  AdminUser,
  Answer,
  ApiToken,
  DevActivityChunk,
  DevRunSummary,
  ChatImage,
  McpToken,
  McpUsage,
  ProjectUsage,
  DevLogs,
  Evaluation,
  IssueWatchEventPage,
  Me,
  MemoryEntry,
  MemoryPlaceholder,
  MemorySettings,
  OrgMemorySettings,
  Message,
  MetaConfig,
  EndpointTestResult,
  ModelCatalog,
  ModelConfig,
  ModelEndpoint,
  ModelProvider,
  NewApiToken,
  NewHubToken,
  Program,
  ProgramBrief,
  ProgramInstance,
  ProgramModelOption,
  ProgramRun,
  ProgramRunFull,
  Project,
  ProjectFileInfo,
  ProjectRequest,
  ProjectSearchResult,
  ProjectSpend,
  ProjectStatus,
  ProjectSummary,
  Routine,
  PublicSettings,
  QuestionsDoc,
  Quote,
  QuoteAttachment,
  RepoProvider,
  RequestEstimate,
  RequestHandling,
  RequestType,
  RunLogChunk,
  ShareEntry,
  Speciality,
  StatusChange,
  Transaction,
} from "../types";

// --- Auth ---
export const authApi = {
  me: () => api.get<Me>("/auth/me", { redirectOn401: false }),
  altcha: () => api.get<Record<string, unknown>>("/auth/altcha"),
  login: (email: string, password: string) =>
    api.post<{ user: Me["user"] }>("/auth/login", { email, password }),
  logout: () => api.post<{ ok: boolean }>("/auth/logout"),
  signup: (body: {
    email: string;
    password: string;
    account_type: string;
    company_name?: string;
    full_name?: string;
    altcha: string;
    accept_terms: boolean;
  }) => api.post<{ ok: boolean }>("/auth/signup", body),
  verifyEmail: (token: string) => api.post<{ ok: boolean }>("/auth/verify-email", { token }),
  resendVerification: (email: string) =>
    api.post<{ ok: boolean }>("/auth/resend-verification", { email }),
  forgotPassword: (email: string) => api.post<{ ok: boolean }>("/auth/forgot-password", { email }),
  resetPassword: (token: string, password: string) =>
    api.post<{ ok: boolean }>("/auth/reset-password", { token, password }),
};

// --- Account ---
export const accountApi = {
  update: (body: {
    account_type: string;
    full_name: string;
    company_name?: string;
    vat_id?: string;
    address_line1?: string;
    address_line2?: string;
    postal_code?: string;
    city?: string;
    country?: string;
  }) => api.patch<Me>("/account", body),
};

// --- Meta ---
export const metaApi = {
  specialities: () => api.get<Speciality[]>("/meta/specialities"),
  questions: () => api.get<QuestionsDoc>("/meta/questions"),
  config: () => api.get<MetaConfig>("/meta/config"),
  memoryPlaceholders: () => api.get<MemoryPlaceholder[]>("/meta/memory-placeholders"),
};

// --- Public white-label settings ---
export const settingsApi = {
  get: () => api.get<PublicSettings>("/settings"),
};

// --- Projects ---
export const chatImageApi = {
  upload: (projectId: string, files: File[]) => {
    const fd = new FormData();
    files.forEach((f) => fd.append("files", f));
    return api.postForm<ChatImage[]>(`/projects/${projectId}/chat-images`, fd);
  },
  url: (projectId: string, imageId: string) =>
    `/api/projects/${projectId}/chat-images/${imageId}`,
};

export const mcpApi = {
  // §MCP project tokens. The plaintext token comes back ONCE, from create().
  list: (projectId: string) =>
    api.get<{ tokens: McpToken[]; usage: McpUsage }>(`/projects/${projectId}/mcp-tokens`),
  create: (projectId: string, name: string) =>
    api.post<McpToken & { token: string }>(`/projects/${projectId}/mcp-tokens`, { name }),
  revoke: (projectId: string, tokenId: string) =>
    api.del<{ ok: boolean }>(`/projects/${projectId}/mcp-tokens/${tokenId}`),
};

export const usageApi = {
  series: (projectId: string, days = 30) =>
    api.get<ProjectUsage>(`/projects/${projectId}/usage?days=${days}`),
};

export const routinesApi = {
  // §routines: saved prompts on a project. Each firing creates an ordinary
  // request, so the run itself shows up in the Requests tab like any other.
  list: (projectId: string) => api.get<Routine[]>(`/projects/${projectId}/routines`),
  create: (projectId: string, body: {
    title: string; prompt: string; enabled?: boolean;
    schedule_cron?: string; repo_id?: string | null;
  }) => api.post<Routine>(`/projects/${projectId}/routines`, body),
  update: (projectId: string, id: string, body: {
    title?: string; prompt?: string; enabled?: boolean;
    schedule_cron?: string; repo_id?: string | null;
  }) => api.put<Routine>(`/projects/${projectId}/routines/${id}`, body),
  remove: (projectId: string, id: string) =>
    api.del<{ ok: boolean }>(`/projects/${projectId}/routines/${id}`),
  run: (projectId: string, id: string) =>
    api.post<Routine>(`/projects/${projectId}/routines/${id}/run`),
};

export const projectsApi = {
  list: (all?: boolean) => api.get<ProjectSummary[]>(`/projects${all ? "?all=1" : ""}`),
  // AI-ranked search over the caller's own projects (§project search). `ai` tells
  // the caller whether the model ranked this response or it fell back to text.
  search: (q: string) =>
    api.get<ProjectSearchResult>(`/projects/search?q=${encodeURIComponent(q)}`),
  get: (id: string) => api.get<Project>(`/projects/${id}`),
  create: (body: {
    kind: "ai" | "direct_quote" | "auto_dev" | "chat";
    speciality?: string | null;
    description: string;
    from_scratch: boolean;
    sovereign: boolean;
    sovereign_comment?: string;
    repos?: { ssh_uri: string }[];
    issue_watch?: { labels: string[]; assignees: string[]; authors: string[] };
  }) => api.post<Project>("/projects", body),
  update: (
    id: string,
    body: {
      name?: string;
      description?: string;
      issue_watch?: { labels: string[]; assignees: string[]; authors: string[] };
      // §git identity: "" resets the field to the instance default.
      git_author_name?: string;
      git_author_email?: string;
    },
  ) => api.patch<Project>(`/projects/${id}`, body),
  // Repos (§multi-repo): connect/switch push target/toggle auto-merge/verify auth.
  connectRepo: (id: string, ssh_uri: string, provider?: RepoProvider) =>
    api.post<Project>(`/projects/${id}/repos`, { ssh_uri, ...(provider ? { provider } : {}) }),
  updateRepo: (id: string, repoId: string,
               body: { is_push_target?: boolean; auto_merge?: boolean; squash_on_merge?: boolean;
                       summarize_to_issue?: boolean }) =>
    api.patch<Project>(`/projects/${id}/repos/${repoId}`, body),
  removeRepo: (id: string, repoId: string) =>
    api.del<Project>(`/projects/${id}/repos/${repoId}`),
  usePlatformRepo: (id: string) => api.post<Project>(`/projects/${id}/repos/use-platform`),
  verifyRepoAuth: (id: string, repoId: string) =>
    api.post<{ ok: boolean; detail: string }>(`/projects/${id}/repos/${repoId}/verify-auth`),
  // SSH reachability of a connected remote repo over the project deploy key (distinct
  // from verifyRepoAuth, which checks the auto-merge PAT).
  verifySsh: (id: string, repoId: string) =>
    api.post<{ ok: boolean; detail: string }>(`/projects/${id}/repos/${repoId}/verify-ssh`),
  saveAnswers: (id: string, answers: Answer[]) =>
    api.post<{ ok: boolean }>(`/projects/${id}/answers`, { answers }),
  evaluate: (id: string) => api.post<{ task_id: string }>(`/projects/${id}/evaluate`),
  evaluation: (id: string) => api.get<Evaluation>(`/projects/${id}/evaluation`),
  submit: (id: string) => api.post<Project>(`/projects/${id}/submit`),
  requireReview: (id: string) => api.post<Project>(`/projects/${id}/require-review`),
  retryBuild: (id: string) => api.post<Project>(`/projects/${id}/retry-build`),
  stopBuild: (id: string, runId?: string) =>
    api.post<{ ok: boolean }>(
      `/projects/${id}/stop-build${runId ? `?run_id=${runId}` : ""}`,
    ),
  approveDelivery: (id: string) => api.post<Project>(`/projects/${id}/approve-delivery`),
  devLogs: (id: string, runId?: string) =>
    api.get<DevLogs>(`/projects/${id}/dev-logs${runId ? `?run_id=${runId}` : ""}`),
  devActivity: (id: string, offset: number, runId?: string) =>
    api.get<DevActivityChunk>(
      `/projects/${id}/dev-activity?offset=${offset}${runId ? `&run_id=${runId}` : ""}`,
    ),
  // §threads: the request thread's development history (one row per dev run).
  devRuns: (id: string, requestId?: string) =>
    api.get<DevRunSummary[]>(
      `/projects/${id}/dev-runs${requestId ? `?request_id=${requestId}` : ""}`,
    ),
  statusHistory: (id: string) => api.get<StatusChange[]>(`/projects/${id}/status-history`),
  // §auto_dev: the Issue-watch card's paginated intake history, newest first.
  issueEvents: (id: string, offset: number, limit = 10) =>
    api.get<IssueWatchEventPage>(`/projects/${id}/issue-events?limit=${limit}&offset=${offset}`),
  demoStart: (id: string) => api.post<Project>(`/projects/${id}/demo/start`),
  demoStop: (id: string) => api.post<Project>(`/projects/${id}/demo/stop`),
  // §sharing: owner-org only. addShare is create-or-update (re-adding the same
  // email just changes the role); no invitation or email is ever sent.
  shares: (id: string) => api.get<ShareEntry[]>(`/projects/${id}/shares`),
  addShare: (id: string, email: string, role: "contributor" | "viewer") =>
    api.post<ShareEntry>(`/projects/${id}/shares`, { email, role }),
  removeShare: (id: string, shareId: string) =>
    api.del<{ ok: boolean }>(`/projects/${id}/shares/${shareId}`),
};

// --- Chat ---
export const chatApi = {
  messages: (id: string, thread: string) =>
    api.get<Message[]>(`/projects/${id}/messages?thread=${encodeURIComponent(thread)}`),
  send: (id: string, thread: string, body: string, alsoEmail?: boolean,
         imageIds?: string[]) =>
    api.post<Message>(`/projects/${id}/messages`, {
      thread,
      body,
      ...(alsoEmail ? { also_email: true } : {}),
      ...(imageIds?.length ? { image_ids: imageIds } : {}),
    }),
  requestHumanAnswer: (id: string, thread: string) =>
    api.post<{ ok: boolean }>(`/projects/${id}/request-human-answer`, { thread }),
};

// --- Requests ---
export const requestsApi = {
  list: (id: string) => api.get<ProjectRequest[]>(`/projects/${id}/requests`),
  create: (
    id: string,
    body: { type: RequestType; handling: RequestHandling; body: string; repo_id?: string },
  ) => api.post<ProjectRequest>(`/projects/${id}/requests`, body),
  update: (id: string, reqId: string, body: { title: string }) =>
    api.patch<ProjectRequest>(`/projects/${id}/requests/${reqId}`, body),
  // Start an agent-proposed request (the alternative to replying "go ahead" in chat).
  start: (id: string, reqId: string) =>
    api.post<ProjectRequest>(`/projects/${id}/requests/${reqId}/start`, {}),
  // §requests: mark the request delivered by hand (never touches a live build).
  validate: (id: string, reqId: string) =>
    api.post<ProjectRequest>(`/projects/${id}/requests/${reqId}/validate`, {}),
  // §requests: cancel the request (never touches a live build).
  cancel: (id: string, reqId: string) =>
    api.post<ProjectRequest>(`/projects/${id}/requests/${reqId}/cancel`, {}),
  estimate: (
    id: string,
    body: { type: RequestType; handling: RequestHandling; body: string },
  ) => api.post<{ task_id: string }>(`/projects/${id}/requests/estimate`, body),
  estimateResult: (id: string, taskId: string) =>
    api.get<{ state: "pending" | "done" | "failed"; estimate?: RequestEstimate }>(
      `/projects/${id}/requests/estimate/${taskId}`,
    ),
};

// --- Memory ---
export type MemoryInput = { key: string; value: string; is_secret: boolean; description: string };

export const memoryApi = {
  list: (id: string) => api.get<MemoryEntry[]>(`/projects/${id}/memory`),
  upsert: (id: string, body: MemoryInput) =>
    api.put<MemoryEntry>(`/projects/${id}/memory`, body),
  remove: (id: string, entryId: string) =>
    api.del<{ ok: boolean }>(`/projects/${id}/memory/${entryId}`),
  settings: (id: string) => api.get<MemorySettings>(`/projects/${id}/memory/settings`),
  updateSettings: (id: string, body: { use_global_memory: boolean | null }) =>
    api.put<MemorySettings>(`/projects/${id}/memory/settings`, body),
};

// --- Project files (Memory & files tab) ---
export const projectFilesApi = {
  list: (id: string) => api.get<ProjectFileInfo[]>(`/projects/${id}/files`),
  upload: (id: string, files: File[]) => {
    const fd = new FormData();
    for (const f of files) fd.append("files", f);
    return api.postForm<ProjectFileInfo[]>(`/projects/${id}/files`, fd);
  },
  remove: (id: string, fileId: string) =>
    api.del<{ ok: boolean }>(`/projects/${id}/files/${fileId}`),
  downloadUrl: (id: string, fileId: string) => `/api/projects/${id}/files/${fileId}`,
};

// --- Global (organization-scoped) Memory ---
export const orgMemoryApi = {
  list: () => api.get<MemoryEntry[]>("/org-memory"),
  upsert: (body: MemoryInput) => api.put<MemoryEntry>("/org-memory", body),
  remove: (entryId: string) => api.del<{ ok: boolean }>(`/org-memory/${entryId}`),
  settings: () => api.get<OrgMemorySettings>("/org-memory/settings"),
  updateSettings: (body: { enabled_default: boolean }) =>
    api.put<OrgMemorySettings>("/org-memory/settings", body),
};

// --- Quotes (credit quotes live in the project Quotes tab) ---
export const quotesApi = {
  list: (id: string) => api.get<Quote[]>(`/projects/${id}/quotes`),
  accept: (id: string, quoteId: string, comment?: string) =>
    api.post<Quote>(`/projects/${id}/quotes/${quoteId}/accept`, { comment: comment || null }),
  deny: (id: string, quoteId: string, comment?: string) =>
    api.post<Quote>(`/projects/${id}/quotes/${quoteId}/deny`, { comment: comment || null }),
  attachmentUrl: (id: string, quoteId: string, attId: string) =>
    `/api/projects/${id}/quotes/${quoteId}/attachments/${attId}`,
  // admin
  create: (id: string, body: { title: string; details: string; price_credits: number }) =>
    api.post<Quote>(`/admin/projects/${id}/quotes`, body),
  patch: (quoteId: string, body: { title?: string; details?: string; price_credits?: number }) =>
    api.patch<Quote>(`/admin/quotes/${quoteId}`, body),
  cancel: (quoteId: string, body: { comment?: string; refund_credits?: number }) =>
    api.post<Quote>(`/admin/quotes/${quoteId}/cancel`, body),
  uploadAttachments: (quoteId: string, files: File[]) => {
    const fd = new FormData();
    for (const f of files) fd.append("files", f);
    return api.postForm<QuoteAttachment[]>(`/admin/quotes/${quoteId}/attachments`, fd);
  },
  deleteAttachment: (quoteId: string, attId: string) =>
    api.del<{ ok: boolean }>(`/admin/quotes/${quoteId}/attachments/${attId}`),
};

// --- Billing ---
export const billingApi = {
  balance: () => api.get<{ credit_balance: number; currency: string }>("/billing/balance"),
  transactions: () => api.get<Transaction[]>("/billing/transactions"),
  topup: (amount: number) => api.post<{ checkout_url: string }>("/billing/topup", { amount }),
  quotes: (id: string) => api.get<Quote[]>(`/projects/${id}/quotes`),
};

// --- Programs (§28) ---
export const programsApi = {
  catalog: (q?: string) =>
    api.get<ProgramBrief[]>(`/programs${q ? `?q=${encodeURIComponent(q)}` : ""}`),
  get: (id: string) => api.get<Program>(`/programs/${id}`),
  createInstance: (programId: string, label: string) =>
    api.post<ProgramInstance>(`/programs/${programId}/instances`, { label }),
  instances: () => api.get<ProgramInstance[]>("/program-instances"),
  instance: (id: string) => api.get<ProgramInstance>(`/program-instances/${id}`),
  // §28 per-instance model: the models an instance may be pinned to. Passing
  // "" as model_endpoint_id below clears the pick back to the program default.
  modelEndpoints: () => api.get<ProgramModelOption[]>("/program-model-endpoints"),
  updateInstance: (
    id: string,
    body: {
      label?: string;
      inputs?: Record<string, string>;
      model_endpoint_id?: string;
      webhook_url?: string;
      schedule_enabled?: boolean;
      schedule_cron?: string;
      hook_enabled?: boolean;
      hook_filters?: { actions: string[]; labels: string[]; assignees: string[]; authors: string[] };
    },
  ) => api.put<ProgramInstance>(`/program-instances/${id}`, body),
  rotateHookSecret: (id: string) =>
    api.post<{ hook_secret: string }>(`/program-instances/${id}/hook-secret`),
  deleteInstance: (id: string) => api.del<{ ok: boolean }>(`/program-instances/${id}`),
  run: (id: string) => api.post<ProgramRun>(`/program-instances/${id}/run`),
  runs: (id: string) => api.get<ProgramRun[]>(`/program-instances/${id}/runs`),
  runDetail: (id: string, runId: string) =>
    api.get<ProgramRunFull>(`/program-instances/${id}/runs/${runId}`),
  runLog: (id: string, runId: string, offset: number) =>
    api.get<RunLogChunk>(`/program-instances/${id}/runs/${runId}/log?offset=${offset}`),
  // plain URL for <a download> (attachment Content-Disposition server-side)
  fileUrl: (id: string, runId: string, path: string) =>
    `/api/program-instances/${id}/runs/${runId}/files/${path
      .split("/")
      .map(encodeURIComponent)
      .join("/")}`,
};

export const adminProgramsApi = {
  list: () => api.get<AdminProgram[]>("/admin/programs"),
  create: (body: { title: string; short_description: string; gitlab_repo_path: string }) =>
    api.post<AdminProgram>("/admin/programs", body),
  get: (id: string) => api.get<AdminProgram>(`/admin/programs/${id}`),
  update: (
    id: string,
    body: Partial<{
      title: string;
      short_description: string;
      default_branch: string;
      is_published: boolean;
      schedulable: boolean;
      model_endpoint_id: string | null;
      credit_markup: number;
      timeout_minutes: number;
      cpu_request: string;
      cpu_limit: string;
      mem_request: string;
      mem_limit: string;
    }>,
  ) => api.put<AdminProgram>(`/admin/programs/${id}`, body),
  remove: (id: string) => api.del<{ ok: boolean }>(`/admin/programs/${id}`),
  refresh: (id: string) => api.post<AdminProgram>(`/admin/programs/${id}/refresh`),
  check: (id: string) => api.post<ProgramRun>(`/admin/programs/${id}/check`),
  runs: (id: string) => api.get<ProgramRun[]>(`/admin/programs/${id}/runs`),
  runDetail: (id: string, runId: string) =>
    api.get<ProgramRunFull>(`/admin/programs/${id}/runs/${runId}`),
  runLog: (id: string, runId: string, offset: number) =>
    api.get<RunLogChunk>(`/admin/programs/${id}/runs/${runId}/log?offset=${offset}`),
};

// --- API tokens ---
export const tokensApi = {
  list: () => api.get<ApiToken[]>("/tokens"),
  create: (name: string) => api.post<NewApiToken>("/tokens", { name }),
  remove: (id: string) => api.del<{ ok: boolean }>(`/tokens/${id}`),
};

// --- Admin ---
export const adminApi = {
  overview: () =>
    api.get<{ projects: AdminProjectSummary[]; counts: { by_status: Record<string, number> } }>(
      "/admin/overview",
    ),
  setStatus: (id: string, status: ProjectStatus, note?: string) =>
    api.post<Project>(`/admin/projects/${id}/status`, { status, note }),
  patchProject: (
    id: string,
    body: {
      tier?: string;
      subdomain?: string;
      block_auto_development?: boolean;
      kb_ids?: string[] | null;
      dev_max_iterations?: number | null;
      dev_parallel_limit?: number | null;
      dev_cpu_request?: string | null;
      dev_mem_request?: string | null;
    },
  ) => api.patch<Project>(`/admin/projects/${id}`, body),
  getModelConfig: (id: string) => api.get<ModelConfig>(`/admin/projects/${id}/model-config`),
  putModelConfig: (id: string, body: { endpoint_id: string | null }) =>
    api.put<{ ok: boolean }>(`/admin/projects/${id}/model-config`, body),
  priceRequest: (reqId: string, priceCredits: number) =>
    api.post<ProjectRequest>(`/admin/requests/${reqId}/price`, { price_credits: priceCredits }),
  quoteRequest: (reqId: string, amount: number) =>
    api.post<Quote>(`/admin/requests/${reqId}/quote`, { amount }),
  quoteProject: (id: string, amount: number) =>
    api.post<Quote>(`/admin/projects/${id}/quote`, { amount }),
  projectSpend: (id: string) => api.get<ProjectSpend>(`/admin/projects/${id}/spend`),
  refundReview: (id: string) =>
    api.post<{ credit_balance: number; refunded: number }>(
      `/admin/projects/${id}/refund-review`,
    ),
  adjustCredits: (orgId: string, amount: number, reason: string) =>
    api.post<{ credit_balance: number }>(`/admin/orgs/${orgId}/credits`, { amount, reason }),
  users: () => api.get<AdminUser[]>("/admin/users"),
  settings: () => api.get<AdminSettings>("/admin/settings"),
  updateSettings: (body: Partial<AdminSettings>) =>
    api.put<AdminSettings>("/admin/settings", body),
  // Mint a hub-scoped token authorizing a central Scalevisor Hub to orchestrate
  // this spoke (grant credits, run evals, KB audit). Admin only (require_admin).
  mintHubToken: () => api.post<NewHubToken>("/admin/hub-token", {}),
};

// Admin management of the instance's knowledge bases (§KB). The api_key is never
// returned by the API - only has_api_key - so it is never rendered client-side.
export type Tool = {
  id: string;
  slug: string;
  name: string;
  kind: "github" | "gitlab" | "custom";
  url: string;
  enabled: boolean;
  has_api_key: boolean;
  // §Tools: the MCP server key the agent addresses this tool by - the string to
  // quote in project instructions.
  mcp_server: string;
};

export type ProjectTool = Tool & {
  override_enabled: boolean | null;
  override_url: string | null;
  override_has_api_key: boolean;
  effective_enabled: boolean;
};

export const toolsApi = {
  list: () => api.get<Tool[]>("/admin/tools"),
  patch: (id: string, body: { url?: string; api_key?: string; enabled?: boolean }) =>
    api.patch<Tool>(`/admin/tools/${id}`, body),
  verify: (id: string) =>
    api.post<{ ok: boolean; detail: string }>(`/admin/tools/${id}/verify`, {}),
  projectList: (projectId: string) => api.get<ProjectTool[]>(`/admin/projects/${projectId}/tools`),
  projectPut: (projectId: string, toolId: string,
               body: { url?: string; api_key?: string; enabled?: boolean | null }) =>
    api.put<{ ok: boolean }>(`/admin/projects/${projectId}/tools/${toolId}`, body),
};

export const kbApi = {
  list: () => api.get<KnowledgeBase[]>("/admin/knowledge-bases"),
  create: (body: { name: string; uri: string; api_key?: string }) =>
    api.post<KnowledgeBase>("/admin/knowledge-bases", { kind: "mcp", ...body }),
  createGit: (body: {
    name?: string;
    uri: string;
    auth_kind: "ssh" | "http";
    ref?: string;
    api_key?: string;
    http_username?: string;
  }) => api.post<KnowledgeBase>("/admin/knowledge-bases", { kind: "git", ...body }),
  verify: (id: string) =>
    api.post<{ ok: boolean; detail: string }>(`/admin/knowledge-bases/${id}/verify`, {}),
  update: (
    id: string,
    body: { enabled?: boolean; name?: string; uri?: string; ref?: string; api_key?: string; http_username?: string },
  ) => api.patch<KnowledgeBase>(`/admin/knowledge-bases/${id}`, body),
  remove: (id: string) => api.del<{ ok: boolean }>(`/admin/knowledge-bases/${id}`),
  reindex: () => api.post<{ status: string }>("/admin/knowledge/reindex", {}),
  tiers: (id: string, opts?: { content_class?: "fact" | "rule" | "procedure"; page?: number }) => {
    const q = new URLSearchParams();
    if (opts?.content_class) q.set("content_class", opts.content_class);
    if (opts?.page && opts.page > 1) q.set("page", String(opts.page));
    const qs = q.toString();
    return api.get<KbTiers>(`/admin/knowledge-bases/${id}/tiers${qs ? `?${qs}` : ""}`);
  },
  overrideBlock: (hash: string, content_class: "fact" | "rule" | "procedure") =>
    api.put<{ content_hash: string; content_class: string; origin: string }>(
      `/admin/knowledge-bases/blocks/${hash}`, { content_class }),
  clearOverride: (hash: string) =>
    api.del<{ ok: boolean }>(`/admin/knowledge-bases/blocks/${hash}`),
};

// Saved model endpoints (§model config): reusable API endpoints + credentials the
// project Model-config modal selects from.
type ModelEndpointBody = {
  label: string;
  provider: ModelProvider;
  base_url: string;
  api_key: string;
  model_name: string;
  input_price?: number | null;
  output_price?: number | null;
  cached_input_price?: number | null;
  reasoning_effort?: "low" | "medium" | "high" | "" | null;
};

export const modelEndpointApi = {
  list: () => api.get<ModelEndpoint[]>("/admin/model-endpoints"),
  create: (body: ModelEndpointBody) => api.post<ModelEndpoint>("/admin/model-endpoints", body),
  update: (id: string, body: Partial<ModelEndpointBody>) =>
    api.patch<ModelEndpoint>(`/admin/model-endpoints/${id}`, body),
  remove: (id: string) => api.del<{ ok: boolean }>(`/admin/model-endpoints/${id}`),
  // Pull the provider's live model list for the form's picker. The key is the one
  // typed into the form, else the stored key of the endpoint being edited.
  models: (body: {
    provider: ModelProvider;
    base_url: string;
    api_key?: string;
    endpoint_id?: string;
  }) => api.post<ModelCatalog>("/admin/model-endpoints/models", body),
  pricedModels: () => api.get<{ models: string[] }>("/admin/model-endpoints/priced-models"),
  test: (id: string) =>
    api.post<EndpointTestResult>(`/admin/model-endpoints/${id}/test`, {}),
};
