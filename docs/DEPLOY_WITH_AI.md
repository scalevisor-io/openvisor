# Deploy Openvisor with your AI assistant

The fastest way to stand up your own Openvisor instance is to hand the prompt below to an AI coding assistant that can run commands on your machine or server (Claude Code, or any agent with shell access, run from a clone of this repository). It interviews you first, then does the work step by step and verifies each step before moving on.

Copy everything inside the block, paste it as your first message to the assistant, and answer its questions.

---

```text
You are deploying MY own instance of Openvisor - the open-source, self-hostable AI consulting platform - from the repository you are currently in. Work step by step, verify each step's real output before starting the next, and ASK ME whenever a decision is mine to make instead of guessing. Never commit .env or any secret, and avoid echoing secret values back into the chat.

STEP 0 - Orient yourself.
Read CLAUDE.md, docs/CODE_MAP.md (skim the architecture paragraph), and .env.example in full before touching anything. The platform is docker-compose-first (one VM), with an optional Helm chart in k8s/ for Kubernetes. The backend deliberately CRASHES on any missing required env var - treat .env.example as the checklist.

STEP 1 - Interview me. Ask me these up front (one message, grouped), then only follow-ups:
1. Local evaluation or public production?
   - Local: HTTP, *.openvisor.local domains in /etc/hosts, placeholder keys allowed, `make dev`.
   - Production: a real domain with APEX + WILDCARD DNS A records (`example.com` and `*.example.com` -> server IP; per-project demos live on `<id>-<slug>.example.com`), a WILDCARD TLS certificate (obtained via DNS-01 elsewhere - the platform does no ACME; PEMs go to traefik/certs/cert.pem + key.pem under compose, or a TLS Secret under Kubernetes), and the server's public IP.
2. Compose on a VM, or the Helm chart on Kubernetes? (Compose is the reference path. Kubernetes needs a cluster, a container registry the platform images can be pushed to, and - for hardened demos - the Sysbox runtime on nodes.)
3. My LLM endpoint: an OpenAI-compatible base URL, an API key, and the exact model name. The model's `api_model` MUST have a row in backend/app/static_data/per_model_price_table.example.json or billing refuses to run (fail-loud by design) - if mine is missing, ask me for its input/output prices per 1M tokens and add the row. Also ask for an embedding endpoint/model (knowledge retrieval) if I have one.
4. Outbound email: real SMTP credentials, or mailpit (bundled test sink, fine for evaluation)?
5. The admin account email + password I want seeded. (Use a real-TLD email format - reserved TLDs like .local are rejected.)
6. Optional: Stripe keys (billing top-ups; without them credits are granted manually via the admin API/UI), GitLab platform instance (URL + group + token) for platform-hosted project repos (without it, provisioning stays pending and customer-connected GitHub/GitLab repos still work), brand identity (BRAND_NAME, CONSULTANT_NAME, CONSULTANT_FOCUS - the one-line description of my consulting practice that the moderation/chat/knowledge prompts speak from - and BRAND_COLOR_*) or keep the neutral Openvisor defaults, and an OpenHands runtime for real AI builds (OPENHANDS_ENABLED=1) vs the scaffold fallback for a first look.

STEP 2 - Configure.
cp .env.example .env, then fill it: every empty required var, my answers from the interview, and strong generated secrets (`openssl rand -hex 32`) for SECRET_KEY, MASTER_ENCRYPTION_KEY and MEILI_MASTER_KEY. DATABASE_URL/REDIS_URL point at the compose services (see the example values). Keep values unquoted with no trailing inline comments. Show me the finished .env with secret VALUES masked and wait for my confirmation.

STEP 3 - Bring it up.
- Compose host prerequisites: Docker Engine with the compose plugin, plus GNU make and git (on a fresh Ubuntu/Debian VM, the https://get.docker.com script and `apt-get install -y make git` cover all three). The first `make dev`/`make prod` builds every platform image from source - expect several minutes and a few GB of disk (the sandboxed dev-runner image alone is ~2 GB).
- Compose local: `make dev`; add the printed /etc/hosts line; app at http://app.openvisor.local (or the configured Traefik port).
- Compose production: set DEPLOY_ENV=production, paste my wildcard PEMs into traefik/certs/cert.pem + key.pem, `make prod`.
- Kubernetes: write the gitignored .env.deploy (DEPLOY_DOMAIN, DEPLOY_ENV, GATEWAY_TLS_ENABLED=true + the TLS secret name, IMAGE_REGISTRY, postgres credentials, release/namespace - see the scripts/deploy-helm.sh header), create the wildcard TLS Secret, then `make helm-bpdr` (build+push images, deploy with migrations, rollout).
Watch the logs until every service is healthy; diagnose failures from the actual error, not guesses.

STEP 4 - First-run checklist (do these, verify each):
1. Log in at app.<domain> with the seeded admin account.
2. POST /api/admin/knowledge/reindex once (Meilisearch KB index; empty knowledge folder is fine).
3. In /admin, add my LLM endpoint as a saved Model endpoint and press its Test button - it must pass.
4. Create a test project end-to-end: sign up as a customer (emails arrive in mailpit or my SMTP inbox), create an AI project, grant credits via the admin UI if Stripe is not configured, and watch the evaluation run.
5. Make the instance MINE - the shipped defaults are deliberately neutral, and an instance that keeps them looks generic to customers: confirm the `# Brand` block in .env carries my name, colors and CONSULTANT_FOCUS; reshape the specialities catalog to the services I actually sell (backend/app/static_data/specialities.json, the live copy materialized from the .example); review the Memory placeholders + onboarding questions alongside it; and customize the landing copy (landing/src/data/site.yml from site.example.yml).
6. Production only: confirm DEMO_RUNTIME=sysbox-runc (the --privileged fallback is for local evaluation only), and confirm the wildcard DNS actually resolves a demo subdomain.

STEP 5 - Report.
Summarize for me: the URLs (landing, app, MCP endpoint), where every secret lives, what is NOT configured (Stripe, GitLab, OpenHands...) and what each missing piece disables, and the single next action you recommend.
```

---

## Notes

- The prompt assumes the assistant runs INSIDE a clone of this repository with shell access to the target machine. For a remote server, run the assistant on that server (or give it SSH).
- Everything the prompt references is normal documentation: `CLAUDE.md` (development guide), `docs/CODE_MAP.md` (architecture), `.env.example` (the full variable checklist), `scripts/deploy-helm.sh` (Kubernetes path).
- The same flow works for a branded fork: set `BRAND_NAME`, `CONSULTANT_NAME` and the color vars in `.env` - the tracked defaults are the neutral Openvisor identity by design.
- If a CDN or WAF fronts the deployment, exempt the `mcp.<domain>` host from its bot protection. MCP clients are not browsers - Claude Code, for one, ships a Bun/BoringSSL TLS stack - so bot-management rules score them as automated and answer a fraction of otherwise valid requests with an HTML block page. The client reports it as an intermittent `403` against a good token, and the request never reaches the MCP service, so nothing shows up in its logs.

## Customizing agent behavior

Every prompt that drives the platform's agents - moderation, feasibility, cost estimation, customer chat, knowledge answers, and the dev agent's working method itself - is a versioned markdown template in `backend/app/agents/prompts/`, and editing those files is the supported way to adapt how the software behaves: tighten the dev workflow's rules, change the tone of customer-facing answers, or add practice-specific constraints. Three rules when you do: keep the `{{PLACEHOLDER}}` tokens intact (they are rendered per instance by `services/brand.py` - that is how one template serves any brand), bump the `version:` number in the comment on the first line of any file you edit (run artifacts record which prompt version produced them), and know where the files live at runtime - under `make dev` they are bind-mounted so an edit applies after `docker compose -f compose.base.yml -f compose.dev.yml restart worker beat`, while production compose and the Helm chart bake them into the backend images, so a change reaches a production instance through a rebuild and redeploy (`make prod` / `make helm-bpdr`).
