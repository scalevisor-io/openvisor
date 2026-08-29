<p align="center"><picture><source media="(prefers-color-scheme: dark)" srcset="landing/public/brand/openvisor-logo-white.svg"><img src="landing/public/brand/openvisor-logo.svg" height="52" alt="Openvisor"></picture></p>

# Openvisor

Scale your consulting activities with a profitable, 24/7 agentic software factory.

Your knowledge profitable 24/7, self-hosted, with a white-label client-facing platform. Customers deposit a project, a sandboxed dev agent builds it as pull/merge requests under your review, a live demo ships on your subdomain, and every LLM token is metered against prepaid credits. Optionally connect your spoke to [Scalevisor](https://github.com/scalevisor-io/scalevisor) to offer your services 24/7 to the world.

## Quick start (with your AI assistant)

Hand [docs/DEPLOY_WITH_AI.md](docs/DEPLOY_WITH_AI.md)'s copy-pastable prompt to an AI coding assistant with shell access (e.g. Claude Code run from a clone of this repo): it interviews you (domain, TLS, LLM endpoint, mail, admin account), fills `.env`, brings the stack up and walks the first-run checklist with you.

## Quick start (local)

```bash
cp .env.example .env   # fill the required vars (the backend crashes on missing ones)
echo "127.0.0.1 openvisor.local app.openvisor.local mail.openvisor.local mcp.openvisor.local" | sudo tee -a /etc/hosts
make dev
```

- Landing: http://openvisor.local - App: http://app.openvisor.local - Mail (Mailpit): http://mail.openvisor.local
- Admin login: `ADMIN_EMAIL` / `ADMIN_PASSWORD` from your `.env`.
- Placeholder Stripe/GitLab/LLM keys are fine locally: billing endpoints return 503 (grant credits from the admin UI instead), GitLab provisioning stays pending, LLM steps fall back to heuristics.

## Make it yours

- Brand & practice: the `# Brand` block in `.env` - `BRAND_NAME`, `CONSULTANT_NAME`, `CONSULTANT_FOCUS` (the one-line description of your consulting practice that the moderation, chat and knowledge prompts speak from), colors, `SITE_URL`.
- Offer catalog: the specialities customers pick from, the Memory placeholder suggestions and the onboarding questions live in `backend/app/static_data/*.example.json`. The shipped defaults are deliberately generic - reshape them to the services you actually sell (live copies are materialized from the examples on first start: edit the live `*.json` for a running instance, the `.example` for new deployments).
- Landing copy: copy `landing/src/data/site.example.yml` to `site.yml` and edit freely.
- Knowledge base: drop documents into the gitignored `./knowledge` folder, then `POST /api/admin/knowledge/reindex`.
- Agent behavior: every agent - moderation, chat, the dev workflow - speaks from a versioned prompt template in `backend/app/agents/prompts/`, yours to edit ([placeholder and rebuild rules](docs/DEPLOY_WITH_AI.md#customizing-agent-behavior)).

## Production

One VM with docker compose: real `.env`, wildcard TLS cert in `traefik/certs/`, wildcard DNS `*.<your-domain>` pointing at the host, then `make prod`. Or Kubernetes: the Helm chart in `k8s/` (`make helm-bpdr`).

## Docs

- [docs/API_CONTRACT.md](docs/API_CONTRACT.md) - frontend/backend contract.
- [CLAUDE.md](CLAUDE.md) - development guide: architecture map, workflows, gotchas.

## Tests

`make test` runs the backend suite inside the api container.

## Support

Openvisor is free and open source. If it earns you money, consider [sponsoring its development](https://github.com/sponsors/flavienbwk).

## License

[Apache-2.0](LICENSE). The "Openvisor" and "Scalevisor" names are trademarks of the project maintainers; the license does not grant rights to use them.
