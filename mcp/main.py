"""Openvisor MCP server (PROMPT §19) - streamable HTTP transport, JSON-RPC.
Auth: `ov_` API token, `Authorization: Bearer ov_…`. The token's scope decides
which tool set it sees:
- "user" (dashboard-minted): read-only project status/info for the token owner's
  own projects, plus a billable `search_knowledge` tool proxying to the backend
  knowledge endpoint (synthesized, cited answers metered against the org wallet).
- "hub" (admin-minted, POST /api/admin/hub-token): a central Scalevisor Hub's
  control tools - spoke_info, usage_summary, list_credit_events, find_org,
  create_org, grant_credits, kb_leak_audit, run_eval, plus the §pass-through
  project tools (create_project, get_project, get_project_evaluation,
  list_project_messages, get_project_dev_activity, project_action) - each
  proxying to the backend /api/hub/* surface. No SQL for money paths runs in
  this sidecar; the backend owns validation and idempotency. kb_leak_audit
  returns a report-only KB-confidentiality risk score; run_eval returns guarded,
  unbilled eval answers.
User tools: list_projects, get_project_status, get_project_info, search_knowledge.
Project tools (§MCP project tokens): get_project_status, get_project_info,
search_knowledge (consult) + delegate_development / get_delegation /
list_delegations (hand work to the §14 build pipeline)."""
import hashlib
import json
import os
from datetime import datetime, timezone

import asyncpg
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

DATABASE_URL = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
# Backend base URL for the billable knowledge tool (same internal docker network).
API_INTERNAL_URL = os.environ.get("API_INTERNAL_URL", "http://api:8000").rstrip("/")
# White-label identity (matches the backend BRAND_*/CONSULTANT_* settings).
BRAND_NAME = os.environ.get("BRAND_NAME", "Openvisor")
CONSULTANT_NAME = os.environ.get("CONSULTANT_NAME", "Consultant")

app = FastAPI(title=f"{BRAND_NAME} MCP")
_pool: asyncpg.Pool | None = None

PROTOCOL_VERSION = "2025-03-26"

USER_TOOLS = [
    {
        "name": "list_projects",
        "description": "List the authenticated user's projects with id, name, status and demo state.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_project_status",
        "description": "Get the current lifecycle status of one project.",
        "inputSchema": {
            "type": "object",
            "properties": {"project_id": {"type": "string"}},
            "required": ["project_id"],
        },
    },
    {
        "name": "get_project_info",
        "description": "Get detailed read-only information about one project (speciality, tier, demo, costs).",
        "inputSchema": {
            "type": "object",
            "properties": {"project_id": {"type": "string"}},
            "required": ["project_id"],
        },
    },
    {
        "name": "search_knowledge",
        "description": (
            f"Search {CONSULTANT_NAME}'s private consulting knowledge base (sovereign-AI, "
            "cloud & platform infrastructure, OCPA, defence/gov) and get a synthesized, "
            f"cited answer. Metered: each call is billed to your {BRAND_NAME} org wallet at "
            "model cost + markup."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Your question."},
                "k": {"type": "integer", "minimum": 1, "maximum": 12,
                      "description": "Passages to retrieve (1-12, default 6)."},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
]

# Project tools (scope="project", §MCP project tokens). A token minted from ONE
# project's MCP tab: no project_id argument anywhere - the token IS the project,
# so it can never name a sibling - and its knowledge queries bill that project.
PROJECT_TOOLS = [
    {
        "name": "get_project_status",
        "description": "Get the current lifecycle status of the project this token belongs to.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_project_info",
        "description": ("Get read-only information about the project this token belongs to "
                        "(speciality, tier, demo, costs)."),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "consult_codebase",
        "description": (
            f"Ask {BRAND_NAME}'s agent a question about THIS project's actual code: it "
            "clones the repositories and reads them before answering, so use it when the "
            "answer depends on how this codebase really works rather than on general "
            "knowledge. It reads only - no edits, no commits, no pull request. Takes a few "
            "minutes and is priced like a build, so prefer search_knowledge for questions "
            "the knowledge base can answer. Returns a job id to poll with get_consult; the "
            "question is not stored."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"question": {"type": "string",
                                        "description": "What you want to know about this codebase."}},
            "required": ["question"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_consult",
        "description": ("Poll a consult started with consult_codebase: queued/running means "
                        "keep waiting, done carries the answer."),
        "inputSchema": {
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "delegate_development",
        "description": (
            f"Hand a slice of development to {BRAND_NAME}: describe what you want built and "
            "the platform's agent implements it in this project's repository and opens a "
            "pull request for you to review. Returns immediately with a delegation id - the "
            "build takes minutes, so poll get_delegation. Billed as a normal build (model "
            "usage at cost + markup), unlike the cheap search_knowledge lookup. The spec you "
            "send IS stored: it is the work order the build runs from."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "spec": {"type": "string",
                         "description": ("What to build, in as much detail as you would give "
                                         "a contractor: the change, the constraints, how to "
                                         "tell it worked.")},
                "type": {"type": "string", "enum": ["feature", "edit", "bug"],
                         "description": "What kind of work this is (default feature)."},
            },
            "required": ["spec"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_delegation",
        "description": ("Follow a delegated piece of work: its status, the pull request(s) it "
                        "opened, and what it has cost so far."),
        "inputSchema": {
            "type": "object",
            "properties": {"request_id": {"type": "string"}},
            "required": ["request_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_delegations",
        "description": "List this project's delegated work, newest first.",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 50}},
            "additionalProperties": False,
        },
    },
    {
        "name": "search_knowledge",
        "description": (
            f"Search {CONSULTANT_NAME}'s private consulting knowledge base (sovereign-AI, "
            "cloud & platform infrastructure, OCPA, defence/gov) and get a synthesized, "
            "cited answer. Metered: each call is billed to this project at model cost + "
            "markup. Your question is not stored."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Your question."},
                "k": {"type": "integer", "minimum": 1, "maximum": 12,
                      "description": "Passages to retrieve (1-12, default 6)."},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
]

# Hub control tools (scope="hub"). Each proxies to the backend /api/hub/* surface.
HUB_TOOLS = [
    {
        "name": "spoke_info",
        "description": "Get this spoke's identity (deploy domain, credit currency, org/project counts).",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "usage_summary",
        "description": "Credit-transaction rollup grouped by kind, optionally since an ISO-8601 cursor.",
        "inputSchema": {
            "type": "object",
            "properties": {"since": {"type": "string",
                                     "description": "ISO-8601 cursor; omit for all-time."}},
            "additionalProperties": False,
        },
    },
    {
        "name": "list_credit_events",
        "description": "Raw credit-transaction feed after an ISO-8601 cursor (oldest first, paged).",
        "inputSchema": {
            "type": "object",
            "properties": {"since": {"type": "string",
                                     "description": "ISO-8601 cursor; omit for the oldest page."}},
            "additionalProperties": False,
        },
    },
    {
        "name": "find_org",
        "description": "Resolve a customer email to its org id and name.",
        "inputSchema": {
            "type": "object",
            "properties": {"email": {"type": "string"}},
            "required": ["email"],
            "additionalProperties": False,
        },
    },
    {
        "name": "create_org",
        "description": (
            "Create a brokered, hub-managed org for one of the hub's customers "
            "(no spoke user is created). Idempotent on idempotency_key: replaying "
            "a key returns the existing org."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string",
                         "description": "Org display name (a neutral alias in anonymous mode)."},
                "idempotency_key": {"type": "string", "minLength": 8},
            },
            "required": ["name", "idempotency_key"],
            "additionalProperties": False,
        },
    },
    {
        "name": "create_project",
        "description": (
            "Create a from-hub project in a brokered (hub-managed) org, acting as the "
            "'customer'. Returns the full project payload (id, subdomain, demo creds...)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "spoke_org_id": {"type": "string"},
                "kind": {"type": "string", "enum": ["ai", "direct_quote", "chat"]},
                "speciality": {"type": "string"},
                "description": {"type": "string"},
                "sovereign": {"type": "boolean"},
                "sovereign_comment": {"type": "string"},
                "hub_ref": {"type": "string",
                            "description": "The hub's own project id, echoed in outbox events."},
            },
            "required": ["spoke_org_id", "description"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_project",
        "description": "Full view of a from-hub project (status, evaluation state, demo URL/creds, dev-run state).",
        "inputSchema": {
            "type": "object",
            "properties": {"project_id": {"type": "string"},
                           "org_id": {"type": "string",
                                      "description": "Expected spoke org id (403 on mismatch - defense in depth)."}},
            "required": ["project_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_project_evaluation",
        "description": "The project's §7 evaluation payload (state, moderation, feasibility verdict, estimate).",
        "inputSchema": {
            "type": "object",
            "properties": {"project_id": {"type": "string"}, "org_id": {"type": "string"}},
            "required": ["project_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_project_messages",
        "description": (
            "A from-hub project's chat messages (thread='main' or 'request:<id>'), oldest first. "
            "An agent message may carry meta {kind:'question', question, options:[{label,description}], "
            "allow_free_text} - a §12 clarifying question the customer answers by posting a plain "
            "message (an option's label, or free text) via post_project_message."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"project_id": {"type": "string"}, "thread": {"type": "string"},
                           "org_id": {"type": "string"}},
            "required": ["project_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_project_dev_activity",
        "description": "Live build-console chunk (offset-polled sanitized agent events + token snapshot).",
        "inputSchema": {
            "type": "object",
            "properties": {"project_id": {"type": "string"}, "offset": {"type": "integer"},
                           "org_id": {"type": "string"}},
            "required": ["project_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "project_action",
        "description": (
            "Run one customer-actor action on a from-hub project: 'evaluate' (run the §7 "
            "evaluation), 'submit' (draft → awaiting_review once the verdict allows), "
            "build/delivery controls, or 'demo-start'/'demo-stop' (the demo container)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"project_id": {"type": "string"},
                           "action": {"type": "string", "enum": ["evaluate", "submit", "approve-delivery", "retry-build", "stop-build", "require-admin", "demo-start", "demo-stop"]},
                           "org_id": {"type": "string"}},
            "required": ["project_id", "action"],
            "additionalProperties": False,
        },
    },
    {
        "name": "post_project_message",
        "description": "Post a chat message on a from-hub project as the customer (thread 'main' or 'request:<id>').",
        "inputSchema": {
            "type": "object",
            "properties": {"project_id": {"type": "string"}, "thread": {"type": "string"},
                           "body": {"type": "string"}, "org_id": {"type": "string"}},
            "required": ["project_id", "body"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_project_requests",
        "description": "A from-hub project's Requests (features/edits/bugs), newest first.",
        "inputSchema": {
            "type": "object",
            "properties": {"project_id": {"type": "string"}, "org_id": {"type": "string"}},
            "required": ["project_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "create_project_request",
        "description": "File a Request on a from-hub project as the customer (AI-handled requests spawn a scoped dev job).",
        "inputSchema": {
            "type": "object",
            "properties": {"project_id": {"type": "string"},
                           "type": {"type": "string", "enum": ["feature", "edit", "bug", "production_deploy"]},
                           "handling": {"type": "string", "enum": ["ai", "manual"]},
                           "body": {"type": "string"}, "org_id": {"type": "string"}},
            "required": ["project_id", "type", "body"],
            "additionalProperties": False,
        },
    },
    {
        "name": "start_project_request",
        "description": "Start a proposed AI-handled request (the 'go ahead' confirmation).",
        "inputSchema": {
            "type": "object",
            "properties": {"project_id": {"type": "string"}, "request_id": {"type": "string"},
                           "org_id": {"type": "string"}},
            "required": ["project_id", "request_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_project_memory",
        "description": "A from-hub project's Memory entries (values in clear, as the customer API serves them).",
        "inputSchema": {
            "type": "object",
            "properties": {"project_id": {"type": "string"}, "org_id": {"type": "string"}},
            "required": ["project_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "upsert_project_memory",
        "description": "Create or update one Memory entry on a from-hub project (author 'customer').",
        "inputSchema": {
            "type": "object",
            "properties": {"project_id": {"type": "string"}, "key": {"type": "string"},
                           "value": {"type": "string"}, "is_secret": {"type": "boolean"},
                           "description": {"type": "string"}, "org_id": {"type": "string"}},
            "required": ["project_id", "key", "value"],
            "additionalProperties": False,
        },
    },
    {
        "name": "delete_project_memory",
        "description": "Delete one Memory entry on a from-hub project.",
        "inputSchema": {
            "type": "object",
            "properties": {"project_id": {"type": "string"}, "entry_id": {"type": "string"},
                           "org_id": {"type": "string"}},
            "required": ["project_id", "entry_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "grant_credits",
        "description": (
            "Grant credits to an org wallet. Idempotent on idempotency_key: replaying a "
            "key returns the original grant and applies nothing."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "org_id": {"type": "string"},
                "amount": {"type": "number", "description": "Credits to add (> 0)."},
                "idempotency_key": {"type": "string"},
                "detail": {"type": "string", "description": "Optional ledger note."},
            },
            "required": ["org_id", "amount", "idempotency_key"],
            "additionalProperties": False,
        },
    },
    {
        "name": "kb_leak_audit",
        "description": (
            "Self-audit this spoke's knowledge-base confidentiality boundary: red-teams its "
            "own retrieval+synthesis path with adversarial extraction probes and returns a "
            "report-only risk assessment (risk_score, level, per-probe leaked flags). Runs "
            "entirely inside the spoke; never returns KB text, chunks or answers, and bills "
            "nothing."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "run_eval",
        "description": (
            "Run owner-consented eval questions through this spoke's knowledge stack; "
            "returns guarded answer texts (unbilled). Answers pass the same verbatim "
            "guard as search_knowledge, so no raw KB text is returned."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "questions": {"type": "array", "items": {"type": "string"},
                              "minItems": 1, "maxItems": 4,
                              "description": "1-4 eval questions."},
                "k": {"type": "integer", "minimum": 1, "maximum": 12,
                      "description": "Passages to retrieve per question (1-12, default 6)."},
            },
            "required": ["questions"],
            "additionalProperties": False,
        },
    },
]

USER_TOOL_NAMES = {t["name"] for t in USER_TOOLS}
PROJECT_TOOL_NAMES = {t["name"] for t in PROJECT_TOOLS}
HUB_TOOL_NAMES = {t["name"] for t in HUB_TOOLS}

# Hub tool -> backend endpoint (method, path). `{placeholders}` are substituted
# from the tool args (and removed); GET args become query params; POST tools'
# remaining args become the JSON body, except HUB_QUERY_KEYS which always travel
# as query params (they are query-declared on the backend routes).
HUB_TOOL_ROUTES = {
    "spoke_info": ("GET", "/api/hub/info"),
    "usage_summary": ("GET", "/api/hub/usage"),
    "list_credit_events": ("GET", "/api/hub/credit-events"),
    "find_org": ("GET", "/api/hub/orgs/find"),
    "create_org": ("POST", "/api/hub/orgs"),
    "grant_credits": ("POST", "/api/hub/credits/grant"),
    "kb_leak_audit": ("POST", "/api/hub/kb-audit"),
    "run_eval": ("POST", "/api/hub/eval"),
    "create_project": ("POST", "/api/hub/projects"),
    "get_project": ("GET", "/api/hub/projects/{project_id}"),
    "get_project_evaluation": ("GET", "/api/hub/projects/{project_id}/evaluation"),
    "list_project_messages": ("GET", "/api/hub/projects/{project_id}/messages"),
    "get_project_dev_activity": ("GET", "/api/hub/projects/{project_id}/dev-activity"),
    "project_action": ("POST", "/api/hub/projects/{project_id}/actions"),
    "post_project_message": ("POST", "/api/hub/projects/{project_id}/messages"),
    "list_project_requests": ("GET", "/api/hub/projects/{project_id}/requests"),
    "create_project_request": ("POST", "/api/hub/projects/{project_id}/requests"),
    "start_project_request": ("POST", "/api/hub/projects/{project_id}/requests/{request_id}/start"),
    "list_project_memory": ("GET", "/api/hub/projects/{project_id}/memory"),
    "upsert_project_memory": ("PUT", "/api/hub/projects/{project_id}/memory"),
    "delete_project_memory": ("DELETE", "/api/hub/projects/{project_id}/memory/{entry_id}"),
}
HUB_QUERY_KEYS = {"org_id"}

# Per-tool proxy timeout (seconds). run_eval / kb_leak_audit drive the spoke's
# knowledge stack, which makes several 60-120s LLM/embedding calls per request, so
# a flat 30s would abort the sidecar mid-run while the backend keeps burning the
# owner's LLM budget with the result thrown away. These match the hub client's own
# allowances for those tools; every other hub tool keeps the snappy default.
HUB_TOOL_DEFAULT_TIMEOUT = 30.0
HUB_TOOL_TIMEOUTS = {
    "run_eval": 150.0,
    "kb_leak_audit": 130.0,
}


def _hub_timeout(name: str) -> float:
    return HUB_TOOL_TIMEOUTS.get(name, HUB_TOOL_DEFAULT_TIMEOUT)


async def pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    return _pool


async def authenticate(request: Request) -> dict | None:
    """Returns {user_id, org_id, role, scope} or None."""
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return None
    token_hash = hashlib.sha256(auth[7:].strip().encode()).hexdigest()
    p = await pool()
    row = await p.fetchrow(
        'SELECT t.id AS token_id, t.scope, t.project_id, u.id AS user_id, u.org_id, u.role '
        'FROM api_token t JOIN "user" u ON u.id = t.user_id WHERE t.token_hash = $1',
        token_hash)
    if row is None:
        return None
    await p.execute("UPDATE api_token SET last_used_at = $1 WHERE id = $2",
                    datetime.now(timezone.utc), row["token_id"])
    return dict(row)


async def call_tool(ident: dict, name: str, args: dict) -> dict:
    p = await pool()
    where_org = "" if ident["role"] == "admin" else "AND org_id = $2"
    if name == "list_projects":
        if ident["role"] == "admin":
            rows = await p.fetch("SELECT id, name, status, demo_state, speciality FROM project ORDER BY created_at DESC")
        else:
            rows = await p.fetch(
                "SELECT id, name, status, demo_state, speciality FROM project WHERE org_id = $1 ORDER BY created_at DESC",
                ident["org_id"])
        return {"projects": [dict(r) for r in rows]}
    if name in ("get_project_status", "get_project_info"):
        pid = args.get("project_id", "")
        params = [pid] if ident["role"] == "admin" else [pid, ident["org_id"]]
        row = await p.fetchrow(
            f"SELECT * FROM project WHERE id = $1 {where_org}", *params)
        if row is None:
            raise ValueError(f"Unknown project {pid}")
        if name == "get_project_status":
            return {"project_id": row["id"], "status": row["status"],
                    "demo_state": row["demo_state"]}
        return {
            "project_id": row["id"], "name": row["name"], "status": row["status"],
            "speciality": row["speciality"], "tier": row["tier"],
            "subdomain": row["subdomain"], "demo_state": row["demo_state"],
            "demo_last_started_at": str(row["demo_last_started_at"]),
            "demo_last_stopped_at": str(row["demo_last_stopped_at"]),
            "tokens_consumed": row["tokens_consumed"],
            "cost_credits": row["cost_credits"],
            "created_at": str(row["created_at"]),
        }
    raise ValueError(f"Unknown tool {name}")


# §MCP delegate: the project-scope write path. Like call_knowledge, the backend
# re-validates the token and owns every guard (repo present, wallet, daily cap,
# the §12 slot gate) - the sidecar just forwards and formats.
DELEGATE_ROUTES = {
    "consult_codebase": ("POST", "/api/mcp/consult"),
    "get_consult": ("GET", "/api/mcp/consult/{job_id}"),
    "delegate_development": ("POST", "/api/mcp/delegate"),
    "get_delegation": ("GET", "/api/mcp/delegations/{request_id}"),
    "list_delegations": ("GET", "/api/mcp/delegations"),
}


async def call_delegate(auth_header: str, name: str, args: dict) -> tuple[str, bool]:
    method, path = DELEGATE_ROUTES[name]
    for field in ("request_id", "job_id"):
        token = "{" + field + "}"
        if token in path:
            val = str(args.get(field, "")).strip()
            if not val:
                return f"{field} is required", True
            path = path.replace(token, val)
    payload = None
    params = None
    if name == "delegate_development":
        payload = {"spec": args.get("spec", ""), "type": args.get("type", "feature")}
    elif name == "consult_codebase":
        payload = {"question": args.get("question", "")}
    elif name == "list_delegations" and args.get("limit"):
        params = {"limit": args["limit"]}
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.request(
                method, f"{API_INTERNAL_URL}{path}",
                headers={"Authorization": auth_header, "Content-Type": "application/json"},
                json=payload, params=params)
    except httpx.HTTPError as exc:
        return f"{BRAND_NAME} is unreachable: {exc}", True
    if resp.status_code in (200, 201, 202):
        return json.dumps(resp.json(), indent=2, default=str), False
    try:
        detail = resp.json().get("detail", resp.text)
    except ValueError:
        detail = resp.text
    return f"{detail}", True


async def call_knowledge(auth_header: str, args: dict) -> tuple[str, bool]:
    """Proxy search_knowledge to the backend (RAG + synthesis + wallet metering).
    Returns (text, is_error). Forwards the caller's Bearer token; the backend
    re-validates it, checks credits, and bills the org."""
    payload: dict = {"query": args.get("query", "")}
    if args.get("k") is not None:
        payload["k"] = args["k"]
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{API_INTERNAL_URL}/api/knowledge/answer",
                headers={"Authorization": auth_header, "Content-Type": "application/json"},
                json=payload)
    except httpx.HTTPError as exc:
        return f"Knowledge service unavailable: {exc}", True
    if resp.status_code == 200:
        data = resp.json()
        lines = [data.get("answer", "")]
        cites = data.get("citations") or []
        if cites:
            lines.append("\nSources:")
            lines += [f"  [{c['n']}] {c.get('ref', '')} ({c.get('source', '')})" for c in cites]
        credits = data.get("credits_charged")
        if credits is not None:
            lines.append(f"\n(billed {round(float(credits), 4)} credits to your org wallet)")
        return "\n".join(lines), False
    # error: surface the backend detail (a 402 already carries the top-up link)
    try:
        detail = resp.json().get("detail", resp.text)
    except Exception:
        detail = resp.text
    return str(detail), True


async def call_hub(auth_header: str, name: str, args: dict) -> tuple[str, bool]:
    """Proxy a hub tool to the backend /api/hub/* surface, forwarding the caller's
    Bearer token (the backend re-validates the hub scope and owns money-path
    validation/idempotency). `{placeholders}` in the route are filled from args;
    HUB_QUERY_KEYS ride as query params even on POST. Returns (text, is_error)."""
    import json as _json
    method, path = HUB_TOOL_ROUTES[name]
    body = dict(args)
    for key in [k for k in body if "{" + k + "}" in path]:
        path = path.replace("{" + key + "}", str(body.pop(key)))
    if "{" in path:
        return f"Missing path argument for {name}", True
    query = {k: body.pop(k) for k in list(body) if k in HUB_QUERY_KEYS and body[k] is not None}
    url = f"{API_INTERNAL_URL}{path}"
    try:
        async with httpx.AsyncClient(timeout=_hub_timeout(name)) as client:
            if method == "GET":
                params = {**{k: v for k, v in body.items() if v is not None}, **query}
                resp = await client.get(url, headers={"Authorization": auth_header},
                                        params=params)
            elif method == "DELETE":
                resp = await client.delete(url, headers={"Authorization": auth_header},
                                           params=query or None)
            else:
                resp = await client.request(
                    method, url, headers={"Authorization": auth_header,
                                          "Content-Type": "application/json"},
                    params=query or None, json=body)
    except httpx.HTTPError as exc:
        return f"Hub service unavailable: {exc}", True
    if resp.status_code < 400:
        return _json.dumps(resp.json(), default=str, indent=2), False
    try:
        detail = resp.json().get("detail", resp.text)
    except Exception:
        detail = resp.text
    return str(detail), True


def rpc_error(id_, code: int, message: str, status: int = 200) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}},
                        status_code=status)


@app.post("/mcp")
async def mcp_endpoint(request: Request):
    body = await request.json()
    id_ = body.get("id")
    method = body.get("method", "")

    if method == "initialize":
        return JSONResponse({"jsonrpc": "2.0", "id": id_, "result": {
            "protocolVersion": body.get("params", {}).get("protocolVersion", PROTOCOL_VERSION),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": BRAND_NAME, "version": "0.1.0"},
        }})
    if method.startswith("notifications/"):
        return Response(status_code=202)
    if method == "ping":
        return JSONResponse({"jsonrpc": "2.0", "id": id_, "result": {}})

    ident = await authenticate(request)
    if ident is None:
        return rpc_error(id_, -32001, "Unauthorized: provide a Bearer API token from the dashboard", 401)

    is_hub = ident["scope"] == "hub"
    # A "project" token is a customer token narrowed to one project: its own tool
    # list, and no project_id argument to point elsewhere.
    is_project = ident["scope"] == "project" and ident.get("project_id")
    if method == "tools/list":
        tools = HUB_TOOLS if is_hub else PROJECT_TOOLS if is_project else USER_TOOLS
        return JSONResponse({"jsonrpc": "2.0", "id": id_, "result": {"tools": tools}})
    if method == "tools/call":
        params = body.get("params", {})
        name = params.get("name", "")
        args = params.get("arguments", {})
        allowed = (HUB_TOOL_NAMES if is_hub
                   else PROJECT_TOOL_NAMES if is_project else USER_TOOL_NAMES)
        if name not in allowed:
            return JSONResponse({"jsonrpc": "2.0", "id": id_, "result": {
                "content": [{"type": "text",
                             "text": f"Tool '{name}' is not available to a {ident['scope']}-scope token."}],
                "isError": True}})
        if is_hub:
            text, is_error = await call_hub(request.headers.get("authorization", ""), name, args)
            return JSONResponse({"jsonrpc": "2.0", "id": id_, "result": {
                "content": [{"type": "text", "text": text}], "isError": is_error}})
        if name in DELEGATE_ROUTES:
            text, is_error = await call_delegate(
                request.headers.get("authorization", ""), name, args)
            return JSONResponse({"jsonrpc": "2.0", "id": id_, "result": {
                "content": [{"type": "text", "text": text}], "isError": is_error}})
        if name == "search_knowledge":
            text, is_error = await call_knowledge(request.headers.get("authorization", ""), args)
            return JSONResponse({"jsonrpc": "2.0", "id": id_, "result": {
                "content": [{"type": "text", "text": text}], "isError": is_error}})
        if is_project:
            # The token's project, never the caller's argument.
            args = {**args, "project_id": ident["project_id"]}
        try:
            result = await call_tool(ident, name, args)
            import json as _json
            return JSONResponse({"jsonrpc": "2.0", "id": id_, "result": {
                "content": [{"type": "text", "text": _json.dumps(result, default=str, indent=2)}],
                "isError": False,
            }})
        except ValueError as exc:
            return JSONResponse({"jsonrpc": "2.0", "id": id_, "result": {
                "content": [{"type": "text", "text": str(exc)}], "isError": True}})
    return rpc_error(id_, -32601, f"Method not found: {method}")


@app.get("/health")
async def health():
    return {"ok": True}
