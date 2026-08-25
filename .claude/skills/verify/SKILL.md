---
name: verify
description: Build, launch, and drive this repo's SPA/landing/API to observe a change at its surface (browser or HTTP), with seeded auth and screenshots.
---

# Verifying changes in the running stack

## Launch

`make dev` in the working copy brings the whole stack up (worktrees get their own slot: ports and `*.openvisorN.local` domains from `.env`; the worktree script prints the app URL). The SPA is baked into the nginx `app` image, so after SPA edits rebuild it: `docker compose -f compose.base.yml -f compose.dev.yml up -d --build app`. The `app` container has no host port - reach it through Traefik at `http://app.openvisor<N>.local:<TRAEFIK_HTTP_PORT>` (instance 1: `app.openvisor.local:80`). `/etc/hosts` maps the `*.openvisor2/3.local` worktree domains to 127.0.0.1.

## Seed auth + data over the API

All endpoints are under `/api` on the app vhost (payloads in `docs/API_CONTRACT.md`):

- CSRF: `GET /api/auth/csrf` → send the token back as `X-CSRF-Token` on every mutation, cookies on.
- Signup AND login need an Altcha proof-of-work: `GET /api/auth/altcha`, brute-force `n` in `0..maxnumber` until `sha256(salt + str(n)) == challenge`, then POST the base64 of `{algorithm, challenge, number, salt, signature}` as `altcha` on `/api/auth/signup` (plus `accept_terms: true` - ToS/privacy consent, 400 without it) or on `/api/auth/login`. One challenge is worth one attempt, so fetch a fresh one per call; at the default difficulty a solve costs a couple of seconds of CPU. For a long scripted run, `ALTCHA_ENABLED=0` in the instance `.env` (restart `api`) turns the gate off.
- The verification token arrives in mailpit: `http://mail.openvisor<N>.local:<port>/api/v1/messages`, fetch the message, regex `token=...` from the link, POST `/api/auth/verify-email`.
- Standard test users (CLAUDE.md): admin `admin@example.org` / `$ADMIN_PASSWORD`; customer `jean.dupont@example.com` / `customer-local-secret1` (create if missing).
- Projects: `POST /api/projects` - `kind: "ai"` requires a `speciality` (take one from `GET /api/meta/specialities`); `direct_quote` doesn't.

## Drive the browser

Python Playwright works on this host (`playwright install chromium` once per pinned build). Log in through the real `/login` form; after login the SPA lands on `/`, navigate to `/projects` explicitly. Screenshots to the session scratchpad. Working example (login + seed + tooltip hover + measurements): a past session's `seed.py`/`drive.py` pattern - login form fill, `wait_for_load_state("networkidle")`, then `page.goto(.../projects)`.

## Gotchas

- Traefik routes by Host header; curl with `-H "Host: app.openvisorN.local"` against `localhost:<port>` also works.
- Celery workers don't hot-reload; restart `worker beat` after touching `backend/app/workers/`.
- Keep the stack up while an MR is open; `scripts/worktree.sh rm <branch>` tears it down after merge.
