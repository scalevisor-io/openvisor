# MCP sidecars

The MCP servers the **dev agent** calls during a build, written here and served from one image (`sidecars-mcp`) as `POST /<name>/mcp`. Each is a §Tools row an admin enables on `/admin/tools`; the build's credential for it rides every request as `Authorization: Bearer`, so the sidecar itself holds no credentials and no state. For a vendor sidecar that credential is the row's envelope-encrypted key; for `email` it is a run token bound to the project, because the real secret (the SMTP password) must never enter the sandbox - see below.

| Sidecar | Route | §Tools kind | Key |
| --- | --- | --- | --- |
| [email_tool.py](email_tool.py) | `/email/mcp` | `email` | None to buy: a per-project run token the worker mints; SMTP settings and the recipient allowlist are set on `/admin/tools` |

## Adding one

1. Write `<name>.py` with a `TOOLS` list, an `async dispatch(tool, args, key)` and `router = mcp_router(...)` from [rpc.py](rpc.py), which carries the whole MCP handshake. Never name the module after a stdlib package (`email.py` would shadow the `email` package `http.client` imports - hence `email_tool.py`).
2. Add it to `SIDECARS` in [app.py](app.py).
3. Seed its §Tools row in `backend/app/seed.py` and give it a `backend/app/services/<name>.py` with `endpoint()` and `verify_key()`.

There is deliberately nothing to change in `compose.base.yml`, the Helm chart, the NetworkPolicies or the image-push lists - the service, its Deployment and its egress rule already exist.

## What does not belong here

- **A different base image or licence boundary.** [`donsetch/`](../donsetch) is its own image because the engine needs glibc 2.38+ and is AGPL-3.0, conveyed unmodified behind a process boundary.
- **The public MCP server.** [`mcp/main.py`](../mcp) is what customers connect their own assistants to on `mcp.<domain>`; it is a product surface, not a build-time tool. `mcp/websearch.py` is a build-time sidecar that predates this folder and still ships on the `mcp` image.

## When the credential must not reach the sandbox

Everything in a build's `mcp.json` is readable by the agent and by any code it runs. A vendor key there is fine - it only buys what the vendor sells. A credential that would let the sandbox skip a rule is not: with the SMTP password in hand, the email allowlist would be a suggestion. `email_tool.py` is the pattern for that case: the build gets a token the worker minted for its project (`backend/app/services/email_tool.mint_token`), the sidecar relays each call to the api's internal route (`/internal/email-tool/*` - not routed by Traefik or the SPA, reached as `API_INTERNAL_URL`, default `http://api:8000`), and the api applies the rule before anything leaves.

## Writing a sidecar

- `tools/list` **must answer without auth** - the worker's tool-poisoning vet fetches it unauthenticated before every build, and a sidecar that 401s there is dropped from the run.
- A provider failure is a **tool** error (`ToolError`), not a protocol error: the agent reads it and chooses differently instead of losing the server.
- **Never put the key, or anything project-derived, in an error message.** Errors reach the build console.
- **Render compactly.** Every response is charged to the agent's context; `mcp_router` clips to the sidecar's `max_chars` as a backstop, not as the plan.
